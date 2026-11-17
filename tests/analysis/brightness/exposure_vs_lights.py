#!/usr/bin/env python3
"""Separate automatic-exposure / tone-mapping lift from published-light strength,
and score Worker D's -Variants exposure sweeps against no-Remix reference runs.

Modes:
  python exposure_vs_lights.py --self-test
  python exposure_vs_lights.py --run <gate_run_dir> [--run <gate_run_dir2> ...] --reference <ref_dir> [--reference <ref_dir2> ...]
  python exposure_vs_lights.py --score-sweep <settings_run_dir> --reference <ref_dir> [--reference <ref_dir2> ...]
  python exposure_vs_lights.py --score-live <spot_or_sweep_dir> --reference <ref_dir> [--reference <ref_dir2> ...]

Key metrics:
  - Across-stop log-log elasticity (`log2_elasticity_slope`): OLS slope `b` in
      log2(Remix_gamma_mean) = a + b * log2(Original_gamma_mean)
    across on-pose (`<= 1.0 deg`), non-black (`raster_gamma_mean >= 0.02`) stops.
    * Pure light-strength error (proportional scene radiance multiplier `k`) gives `b ~ 1.0`
      (`> 0.65` even with highlight shoulder roll-off).
    * Auto-exposure + local-tonemap clamping to a constant target (~0.47-0.49 gamma) gives
      `b ~ 0.0` (`|b| < 0.35`) and `log2(ratio)` vs `log2(Original)` slope `b - 1 ~ -1.0`.
  - Base-to-base_end drift gate (`base_drift_abs`, `base_drift_rel`):
      base_drift_abs = |luma(base_end) - luma(base)|
      base_drift_rel = base_drift_abs / max(luma(base), 1e-6)
    Any switch variant or `-Variants` setting whose `effect_abs = |luma(variant) - luma(base)|`
    does not exceed `base_drift_abs` (`base_drift_abs >= effect_abs`), or that lacks a closing
    base measurement, is reported `NOT_MEASURED` (`reason = "base_drift_exceeds_effect"` or
    `"missing_base_end"`).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from typing import Any

import numpy as np

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_RUNTIME_DIR = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", "runtime"))
if _RUNTIME_DIR not in sys.path:
    sys.path.insert(0, _RUNTIME_DIR)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import gate  # noqa: E402
import pfm_brightness as pb  # noqa: E402

ELASTICITY_EXPOSURE_MAX_SLOPE = 0.35
ELASTICITY_LIGHT_STRENGTH_MIN_SLOPE = 0.65

PASS_THRESHOLDS = {
    "min_scene_luma": 0.03,
    "min_stop_ratio": 0.50,
    "max_stop_ratio": 1.85,
    "min_spot_median_ratio": 0.60,
    "max_spot_median_ratio": 1.50,
    "max_near_white_delta": 0.10,
    "min_raster_luma_for_ratio": 0.02,
    "max_pose_error_deg": 1.0,
}


def fit_across_stop_elasticity(
    raster_lum: list[float] | np.ndarray,
    remix_lum: list[float] | np.ndarray,
) -> dict[str, Any]:
    """Fits log2(Remix) = a + b * log2(Original) and compares constant-target vs proportional-light models."""
    rx = np.asarray(raster_lum, dtype=np.float64)
    fx = np.asarray(remix_lum, dtype=np.float64)
    valid = np.isfinite(rx) & np.isfinite(fx) & (rx >= PASS_THRESHOLDS["min_raster_luma_for_ratio"]) & (fx > 1e-6)
    rx = rx[valid]
    fx = fx[valid]
    n = int(rx.size)
    if n < 3:
        return {
            "n_stops": n,
            "diagnosis": "NOT_MEASURED",
            "reason": "insufficient_on_pose_stops",
        }

    log_rx = np.log2(rx)
    log_fx = np.log2(fx)
    rx_span = float(rx.max() / rx.min())
    fx_span = float(fx.max() / fx.min())
    if float(np.std(log_rx)) < 0.05:
        return {
            "n_stops": n,
            "raster_min": float(rx.min()),
            "raster_median": float(np.median(rx)),
            "raster_max": float(rx.max()),
            "raster_max_over_min": rx_span,
            "remix_min": float(fx.min()),
            "remix_median": float(np.median(fx)),
            "remix_max": float(fx.max()),
            "remix_max_over_min": fx_span,
            "diagnosis": "NOT_MEASURED",
            "reason": "insufficient_original_dynamic_range",
        }

    slope, intercept = np.polyfit(log_rx, log_fx, 1)
    slope = float(slope)
    intercept = float(intercept)
    ratio = fx / rx
    log_ratio = np.log2(ratio)
    corr_fx = float(np.corrcoef(log_rx, log_fx)[0, 1]) if float(np.std(log_fx)) > 1e-9 else 0.0
    corr_ratio = float(np.corrcoef(log_rx, log_ratio)[0, 1]) if float(np.std(log_ratio)) > 1e-9 else 0.0

    # Competing 1-parameter models:
    #   Model A (exposure clamp): Remix_hat = median(Remix)
    #   Model B (light multiplier): Remix_hat = clip(median(Remix / Orig) * Orig, 0, 1)
    target_const = float(np.median(fx))
    scale_mult = float(np.median(ratio))
    pred_exposure = np.full_like(fx, target_const)
    pred_lights = np.clip(scale_mult * rx, 0.0, 1.0)
    rmse_exposure = float(np.sqrt(np.mean((fx - pred_exposure) ** 2)))
    rmse_lights = float(np.sqrt(np.mean((fx - pred_lights) ** 2)))

    if abs(slope) <= ELASTICITY_EXPOSURE_MAX_SLOPE and rmse_exposure < rmse_lights:
        diagnosis = "exposure"
    elif slope >= ELASTICITY_LIGHT_STRENGTH_MIN_SLOPE and rmse_lights < rmse_exposure:
        diagnosis = "light_strength"
    else:
        diagnosis = "mixed_or_ambiguous"

    return {
        "n_stops": n,
        "diagnosis": diagnosis,
        "raster_min": float(rx.min()),
        "raster_median": float(np.median(rx)),
        "raster_max": float(rx.max()),
        "raster_max_over_min": rx_span,
        "remix_min": float(fx.min()),
        "remix_median": target_const,
        "remix_max": float(fx.max()),
        "remix_max_over_min": fx_span,
        "ratio_min": float(ratio.min()),
        "ratio_median": scale_mult,
        "ratio_max": float(ratio.max()),
        "log2_elasticity_slope": slope,
        "log2_elasticity_intercept": intercept,
        "log2_elasticity_r": corr_fx,
        "log2_ratio_vs_orig_slope": slope - 1.0,
        "log2_ratio_vs_orig_r": corr_ratio,
        "rmse_constant_exposure_model": rmse_exposure,
        "rmse_proportional_light_model": rmse_lights,
    }


def evaluate_drift_gated_effects(
    base_luma: float | None,
    base_end_luma: float | None,
    variants: dict[str, float],
) -> dict[str, Any]:
    """Evaluates variant brightness deltas against base-to-base_end drift.

    Named drift metrics:
      base_drift_abs = |base_end_luma - base_luma|
      base_drift_rel = base_drift_abs / max(base_luma, 1e-6)

    Rule: if base_end_luma is missing, or base_drift_abs >= effect_abs (base and base_end
    differ by at least as much as the variant's effect from base), the variant's effect is
    marked NOT_MEASURED.
    """
    if base_luma is None or not math.isfinite(base_luma) or base_luma <= 0.0:
        return {
            "status": "NOT_MEASURED",
            "reason": "missing_base",
            "base_luma": base_luma,
            "base_end_luma": base_end_luma,
            "base_drift_abs": None,
            "base_drift_rel": None,
            "variants": {},
        }
    if base_end_luma is None or not math.isfinite(base_end_luma):
        return {
            "status": "NOT_MEASURED",
            "reason": "missing_base_end",
            "base_luma": float(base_luma),
            "base_end_luma": None,
            "base_drift_abs": None,
            "base_drift_rel": None,
            "variants": {
                k: {"luma": float(v), "status": "NOT_MEASURED", "reason": "missing_base_end"}
                for k, v in variants.items()
            },
        }

    base_drift_abs = abs(float(base_end_luma) - float(base_luma))
    base_drift_rel = base_drift_abs / max(float(base_luma), 1e-6)

    scored: dict[str, Any] = {}
    n_measured = 0
    for name, val in variants.items():
        val_f = float(val)
        delta = val_f - float(base_luma)
        effect_abs = abs(delta)
        effect_rel = effect_abs / max(float(base_luma), 1e-6)
        if base_drift_abs >= effect_abs:
            scored[name] = {
                "luma": val_f,
                "delta_vs_base": delta,
                "effect_abs": effect_abs,
                "effect_rel": effect_rel,
                "status": "NOT_MEASURED",
                "reason": "base_drift_exceeds_effect",
            }
        else:
            n_measured += 1
            scored[name] = {
                "luma": val_f,
                "delta_vs_base": delta,
                "effect_abs": effect_abs,
                "effect_rel": effect_rel,
                "effect_to_drift_ratio": (effect_abs / base_drift_abs) if base_drift_abs > 0.0 else float("inf"),
                "status": "MEASURED",
            }

    overall = "MEASURED" if (n_measured > 0 or not variants) else "NOT_MEASURED"
    return {
        "status": overall,
        "base_luma": float(base_luma),
        "base_end_luma": float(base_end_luma),
        "base_drift_abs": base_drift_abs,
        "base_drift_rel": base_drift_rel,
        "n_measured_variants": n_measured,
        "n_total_variants": len(variants),
        "variants": scored,
    }


def analyze_spot_switches(spot_dir: str) -> dict[str, Any] | None:
    """Scores switch_*_final.pfm files in <spot_dir>/gate using the base vs base_again drift rule."""
    gate_dir = os.path.join(spot_dir, "gate")
    base_pfm = os.path.join(gate_dir, "switch_base_final.pfm")
    if not os.path.isfile(base_pfm):
        return None
    base_end_pfm = os.path.join(gate_dir, "switch_base_again_final.pfm")
    base_luma = float(pb.image_brightness_stats(gate.read_pfm(base_pfm), mask_hud=True)["gamma_mean"])
    base_end_luma = (
        float(pb.image_brightness_stats(gate.read_pfm(base_end_pfm), mask_hud=True)["gamma_mean"])
        if os.path.isfile(base_end_pfm)
        else None
    )
    variants: dict[str, float] = {}
    for fname in sorted(os.listdir(gate_dir)):
        if not (fname.startswith("switch_") and fname.endswith("_final.pfm")):
            continue
        sw_name = fname[len("switch_") : -len("_final.pfm")]
        if sw_name in ("base", "base_again"):
            continue
        st = pb.image_brightness_stats(gate.read_pfm(os.path.join(gate_dir, fname)), mask_hud=True)
        variants[sw_name] = float(st["gamma_mean"])
    return evaluate_drift_gated_effects(base_luma, base_end_luma, variants)


def _make_gate_stop_ok(run_dir: str, reference_dirs: list[str]):
    """Builds gate._reference_stop_ok across run_dir and one or more reference_dirs.

    Supports both full gate runs (spot.json + proxy_frames.csv present, using gate.reference_position)
    and lightweight synthetic fixtures where neither run has proxy_frames.csv (falling back to angular
    pose checks inside pfm_brightness).
    """
    run_spots: dict[str, dict[str, Any]] = {}
    if os.path.isdir(run_dir):
        for s in gate.load_spots(run_dir):
            run_spots[os.path.normcase(os.path.abspath(s["_dir"]))] = s

    ref_spots: dict[str, dict[str, Any]] = {}
    for rdir in reference_dirs:
        if os.path.isdir(rdir):
            for r in gate.load_spots(rdir):
                ref_spots[os.path.normcase(os.path.abspath(r["_dir"]))] = r

    def ok(spot_dir: str, ref_spot_dir: str, tag: str) -> tuple[bool, str]:
        s = run_spots.get(os.path.normcase(os.path.abspath(spot_dir)))
        r = ref_spots.get(os.path.normcase(os.path.abspath(ref_spot_dir)))
        if s is None or r is None:
            s_proxy = os.path.isfile(os.path.join(spot_dir, "proxy_frames.csv"))
            r_proxy = os.path.isfile(os.path.join(ref_spot_dir, "proxy_frames.csv"))
            if not s_proxy and not r_proxy:
                return True, f"{tag}: synthetic fixture without proxy_frames.csv"
            return False, f"{tag}: spot not loaded for the reference position rule"
        placed, why = gate.reference_position(s, tag, r, tag)
        return bool(placed), why

    return ok


EXPOSURE_SETTLE_WINDOW_MS = 2000.0
MAX_SETTLED_EXPOSURE_REL_SPAN = 0.025
MAX_SETTLED_EXPOSURE_F2F_REL = 0.005

EXPOSURE_SETTLE_SPEC = {
    "window_seconds": EXPOSURE_SETTLE_WINDOW_MS / 1000.0,
    "max_rel_span": MAX_SETTLED_EXPOSURE_REL_SPAN,
    "max_frame_to_frame_rel": MAX_SETTLED_EXPOSURE_F2F_REL,
    "provenance": (
        "Measured on 20261117-152548 con_custodial held windows: single-bucket held base steps "
        "switch_base (52 frames, 1.72 s) and switch_base_again (50 frames, 1.64 s) have 0.0000% 2s span "
        "and 0.0000% frame-to-frame wobble, and y0_p0 (41 frames, 1.33 s) has 0.0093% 2s span and "
        "0.0009% frame-to-frame wobble; when Median-mode autoExposure (256 histogram bins across 7.0 EV, "
        "1 bin = 1.87% at 4.2318..4.3108) straddles one bin boundary on held con_custodial windows "
        "(switch_fill_lights_off 1.85%, y90_p30 1.69%, and 45 s held y0_p0 in 20261117-105336-settings "
        "live_step1 1.82%), 2s span reaches 1.85% and max frame-to-frame step reaches 0.294%. "
        "Thresholds 2.5% (last-2s relative span) and 0.5% (last-2s max frame-to-frame relative step) "
        "on both effectiveExposure and autoExposure accept <=1 histogram-bin steady-state wobble and "
        "reject multi-bin exposure ramps as NOT MEASURED ('exposure not settled')."
    ),
}


def _select_last_window_rows(rows: list[dict[str, Any]], window_ms: float = EXPOSURE_SETTLE_WINDOW_MS) -> tuple[list[dict[str, Any]], float | None]:
    """Selects rows within the last `window_ms` of `rows` using qpcMs or frameTimeMs (fallback: tail 60 frames)."""
    if not rows:
        return [], None
    if all(r.get("qpcMs") is not None for r in rows):
        q_end = float(rows[-1]["qpcMs"])
        tail = [r for r in rows if q_end - float(r["qpcMs"]) <= window_ms]
        dur = float(tail[-1]["qpcMs"]) - float(tail[0]["qpcMs"]) if len(tail) >= 2 else 0.0
        return tail, dur
    dts = [float(r.get("frameTimeMs") or 0.0) for r in rows]
    if any(d > 0.0 for d in dts):
        acc = 0.0
        k = len(rows) - 1
        for i in range(len(rows) - 1, -1, -1):
            acc += dts[i] if dts[i] > 0.0 else 33.33
            k = i
            if acc >= window_ms:
                break
        return rows[k:], acc
    tail_n = min(len(rows), 60)
    return rows[-tail_n:], None


def _series_wobble(vals: list[float]) -> tuple[float | None, float | None, float | None]:
    """Returns (median, rel_span, max_frame_to_frame_rel) for a series of exposure samples."""
    if not vals:
        return None, None, None
    arr = np.asarray(vals, dtype=np.float64)
    med = float(np.median(arr))
    denom = max(abs(med), 1e-6)
    rel_span = float(arr.max() - arr.min()) / denom
    f2f_rel = float(np.max(np.abs(np.diff(arr)))) / denom if arr.size >= 2 else 0.0
    return med, rel_span, f2f_rel


def extract_stop_exposure_telemetry(spot_dir: str) -> dict[str, dict[str, Any]]:
    """Extracts per-window autoExposure, effectiveExposure, last-2s settle wobble, and rayTraced share."""
    tel_path = os.path.join(spot_dir, "telemetry.jsonl")
    spot_path = os.path.join(spot_dir, "spot.json")
    if not (os.path.isfile(tel_path) and os.path.isfile(spot_path)):
        return {}
    tel, _ = gate.read_telemetry(tel_path)
    if not tel:
        return {}
    spot = gate.load_json(spot_path)
    out: dict[str, dict[str, Any]] = {}
    for w in spot.get("windows", []):
        wname = w.get("name")
        if not wname or not w.get("telemetry"):
            continue
        rows = [tel[f] for f in gate.frames_in(w, "telemetry") if f in tel]
        if not rows:
            continue
        rt_vals = [bool(r.get("rayTraced")) for r in rows]
        rt_share = float(sum(rt_vals)) / len(rt_vals)
        tail_rows, tail_ms = _select_last_window_rows(rows, EXPOSURE_SETTLE_WINDOW_MS)
        ae_tail = [
            float(r["autoExposure"])
            for r in tail_rows
            if r.get("autoExposure") is not None and math.isfinite(float(r["autoExposure"]))
        ]
        eff_tail = [
            float(r["effectiveExposure"])
            for r in tail_rows
            if r.get("effectiveExposure") is not None and math.isfinite(float(r["effectiveExposure"]))
        ]
        ae_med, ae_span_rel, ae_f2f_rel = _series_wobble(ae_tail)
        eff_med, eff_span_rel, eff_f2f_rel = _series_wobble(eff_tail)
        if len(ae_tail) >= 2 and len(eff_tail) >= 2:
            settled_ok = (
                max(float(ae_span_rel or 0.0), float(eff_span_rel or 0.0)) <= MAX_SETTLED_EXPOSURE_REL_SPAN
                and max(float(ae_f2f_rel or 0.0), float(eff_f2f_rel or 0.0)) <= MAX_SETTLED_EXPOSURE_F2F_REL
            )
        else:
            settled_ok = None
        entry: dict[str, Any] = {
            "n_frames": len(rows),
            "last2s_frames": len(tail_rows),
            "last2s_duration_ms": tail_ms,
            "ray_traced_share": rt_share,
            "auto_exposure_median": ae_med,
            "effective_exposure_median": eff_med,
            "last2s_auto_exp_rel_span": ae_span_rel,
            "last2s_eff_exp_rel_span": eff_span_rel,
            "last2s_auto_exp_f2f_max_rel": ae_f2f_rel,
            "last2s_eff_exp_f2f_max_rel": eff_f2f_rel,
            "exposure_settled": settled_ok,
            "ceiling_clamped_share": (
                float(np.mean([v >= 7.020 for v in ae_tail])) if ae_tail else None
            ),
        }
        out[wname] = entry
    return out


def summarize_spot_exposure_telemetry(
    on_pose_stops: list[dict[str, Any]],
    stop_tel: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Summarizes Stage-1 autoExposure / effectiveExposure behavior across valid stops."""
    if not stop_tel:
        return None
    orig_g: list[float] = []
    pre_tm_ratios: list[float] = []
    ae_list: list[float] = []
    eff_list: list[float] = []
    clamp_shares: list[float] = []
    for s in on_pose_stops:
        tag = s.get("tag")
        t = stop_tel.get(tag or "")
        if not t or t.get("effective_exposure_median") is None:
            continue
        vr = s.get("vs_raster") or {}
        rg = vr.get("raster_gamma_mean")
        lin_ratio = vr.get("linear_mean_ratio")
        eff = float(t["effective_exposure_median"])
        ae = float(t["auto_exposure_median"]) if t.get("auto_exposure_median") is not None else eff
        if rg is None or rg < PASS_THRESHOLDS["min_raster_luma_for_ratio"] or eff <= 1e-6:
            continue
        orig_g.append(float(rg))
        if lin_ratio is not None and math.isfinite(float(lin_ratio)):
            pre_tm_ratios.append(float(lin_ratio) / eff)
        ae_list.append(ae)
        eff_list.append(eff)
        if t.get("ceiling_clamped_share") is not None:
            clamp_shares.append(float(t["ceiling_clamped_share"]))

    if not eff_list:
        return None

    ae_arr = np.asarray(ae_list, dtype=np.float64)
    eff_arr = np.asarray(eff_list, dtype=np.float64)
    og_arr = np.asarray(orig_g, dtype=np.float64)
    slope: float | None = None
    if eff_arr.size >= 3 and float(np.std(np.log2(og_arr))) >= 0.05:
        if float(np.std(np.log2(eff_arr))) < 1e-5:
            slope = 0.0
        else:
            slope = float(np.polyfit(np.log2(og_arr), np.log2(eff_arr), 1)[0])

    clamp_mean = float(np.mean(clamp_shares)) if clamp_shares else 0.0
    if clamp_mean >= 0.90 and (slope is None or abs(slope) < 0.05):
        regime = "clamped_at_ceiling"
    elif slope is not None and slope <= -0.25:
        regime = "active_inverse_gain"
    else:
        regime = "partial_or_flat"

    pre_tm_ratio: float | None = (
        float(np.median(np.asarray(pre_tm_ratios, dtype=np.float64))) if pre_tm_ratios else None
    )

    return {
        "n_stops_with_telemetry": int(eff_arr.size),
        "exposure_telemetry_regime": regime,
        "auto_exposure_min": float(ae_arr.min()),
        "auto_exposure_median": float(np.median(ae_arr)),
        "auto_exposure_max": float(ae_arr.max()),
        "effective_exposure_min": float(eff_arr.min()),
        "effective_exposure_median": float(np.median(eff_arr)),
        "effective_exposure_max": float(eff_arr.max()),
        "ceiling_clamped_stop_share": clamp_mean,
        "log2_eff_exp_vs_orig_slope": slope,
        "normalized_pre_tonemap_radiance_ratio_median": pre_tm_ratio,
    }


def analyze_gate_runs(
    run_dirs: list[str],
    reference_dirs: list[str],
    max_pose_error_deg: float = PASS_THRESHOLDS["max_pose_error_deg"],
    stop_ok=None,
) -> dict[str, Any]:
    """Runs across-stop elasticity + exposure telemetry + drift-gated switch analysis on saved Remix gate runs."""
    spots_out: dict[str, Any] = {}
    pooled_rx: list[float] = []
    pooled_fx: list[float] = []

    for run_dir in run_dirs:
        run_name = os.path.basename(os.path.normpath(run_dir))
        run_stop_ok = stop_ok if stop_ok is not None else _make_gate_stop_ok(run_dir, reference_dirs)
        summary = pb.summarize_gate_run(
            run_dir,
            reference_dirs=reference_dirs,
            max_pose_error_deg=max_pose_error_deg,
            stop_ok=run_stop_ok,
        )
        for spot_name, sp in summary.get("spots", {}).items():
            key = f"{run_name}/{spot_name}" if spot_name in spots_out else spot_name
            spot_dir = os.path.join(run_dir, spot_name)
            spot_rt = _variant_ray_traced(spot_dir)
            if spot_rt is False:
                spots_out[key] = {
                    "run": run_name,
                    "spot": spot_name,
                    "ray_traced": False,
                    "gate_status": "NOT_MEASURED",
                    "gate_reasons": ["not_ray_traced"],
                    "n_on_pose": 0,
                    "off_pose_stops": [],
                    "elasticity": {"n_stops": 0, "diagnosis": "NOT_MEASURED", "reason": "not_ray_traced"},
                    "exposure_telemetry": None,
                    "y0_p0": None,
                    "switch_drift_analysis": None,
                }
                continue
            on_pose_pairs = [
                s
                for s in sp.get("stops", [])
                if not s.get("is_switch")
                and s.get("on_pose") is True
                and "vs_raster" in s
                and not s["vs_raster"].get("black_raster", False)
            ]
            rx = [float(s["vs_raster"]["raster_gamma_mean"]) for s in on_pose_pairs]
            fx = [float(s["vs_raster"]["final_gamma_mean"]) for s in on_pose_pairs]
            pooled_rx.extend(rx)
            pooled_fx.extend(fx)

            stop_tel = extract_stop_exposure_telemetry(spot_dir)
            exp_tel_summary = summarize_spot_exposure_telemetry(on_pose_pairs, stop_tel)
            y0_entry = next((s for s in sp.get("stops", []) if s.get("tag") == "y0_p0"), None)
            y0_tel = stop_tel.get("y0_p0") or {}
            spots_out[key] = {
                "run": run_name,
                "spot": spot_name,
                "ray_traced": spot_rt,
                "gate_status": sp.get("status"),
                "gate_reasons": sp.get("reasons", []),
                "n_on_pose": sp.get("n_on_pose", 0),
                "off_pose_stops": sp.get("off_pose_stops", []),
                "elasticity": fit_across_stop_elasticity(rx, fx),
                "exposure_telemetry": exp_tel_summary,
                "y0_p0": {
                    "on_pose": y0_entry.get("on_pose") if y0_entry else None,
                    "placed": y0_entry.get("placed") if y0_entry else None,
                    "pose_error_deg": y0_entry.get("pose_error_deg") if y0_entry else None,
                    "final_gamma_mean": y0_entry.get("gamma_mean") if y0_entry else None,
                    "raster_gamma_mean": (
                        y0_entry["vs_raster"]["raster_gamma_mean"]
                        if (y0_entry and "vs_raster" in y0_entry)
                        else None
                    ),
                    "gamma_mean_ratio": (
                        y0_entry["vs_raster"]["gamma_mean_ratio"]
                        if (y0_entry and "vs_raster" in y0_entry)
                        else None
                    ),
                    "auto_exposure_median": y0_tel.get("auto_exposure_median"),
                    "effective_exposure_median": y0_tel.get("effective_exposure_median"),
                },
                "switch_drift_analysis": analyze_spot_switches(spot_dir),
            }

    return {
        "reference_dirs": [os.path.abspath(r) for r in reference_dirs],
        "run_dirs": [os.path.abspath(r) for r in run_dirs],
        "pooled_elasticity": fit_across_stop_elasticity(pooled_rx, pooled_fx),
        "spots": spots_out,
    }


def _find_on_pose_reference_pfm(
    spot_name: str,
    tag: str,
    reference_dirs: list[str],
    max_pose_error_deg: float = PASS_THRESHOLDS["max_pose_error_deg"],
) -> tuple[str | None, float | None, str | None]:
    """Finds an on-pose, non-black <tag>_raster.pfm across reference_dirs; returns (path, pose_err_deg, reason).

    When tag == "y0_p0" and y0_p0_raster.pfm is off-pose or black, also checks same-pose y0_p0_after_raster.pfm
    (target (0, 0)), which is on-pose (0.05 deg) in reference-20261116-000454/con_custodial.
    """
    candidate_tags = [tag, "y0_p0_after"] if tag == "y0_p0" else [tag]
    best_off_err: float | None = None
    seen_black = False
    for ref_dir in reference_dirs:
        ref_spot = os.path.join(ref_dir, spot_name)
        wins = pb._load_spot_windows(ref_spot)
        for cand_tag in candidate_tags:
            cand = os.path.join(ref_spot, "gate", f"{cand_tag}_raster.pfm")
            if not os.path.isfile(cand):
                continue
            target = pb.parse_tag_pose(cand_tag)
            rjson = os.path.join(ref_spot, "gate", f"{cand_tag}_raster.json")
            err = pb._window_pose_error_deg(cand_tag, wins.get(cand_tag), rjson if os.path.isfile(rjson) else None)
            if err is None and target is not None and os.path.isfile(rjson):
                try:
                    meta = json.load(open(rjson, encoding="utf-8"))
                    if "relPose" in meta and len(meta["relPose"]) >= 2:
                        err = pb.angular_error_deg(target, (float(meta["relPose"][0]), float(meta["relPose"][1])))
                except (OSError, ValueError, KeyError, TypeError):
                    err = None
            if err is not None and err > max_pose_error_deg:
                best_off_err = err if best_off_err is None else min(best_off_err, err)
                continue
            r_stats = pb.image_brightness_stats(gate.read_pfm(cand), mask_hud=True)
            if float(r_stats["gamma_mean"]) < PASS_THRESHOLDS["min_raster_luma_for_ratio"]:
                seen_black = True
                continue
            return cand, err, None
    if seen_black and best_off_err is None:
        return None, None, "black_raster"
    if best_off_err is not None:
        return None, best_off_err, f"reference_off_pose ({best_off_err:.2f} deg)"
    return None, None, "missing_reference"


def _variant_ray_traced(variant_dir: str) -> bool | None:
    """Checks telemetry.jsonl in variant_dir when present; returns False if 0 ray-traced frames recorded."""
    tel_path = os.path.join(variant_dir, "telemetry.jsonl")
    if not os.path.isfile(tel_path):
        return None
    tel, _ = gate.read_telemetry(tel_path)
    if not tel:
        return False
    spot_path = os.path.join(variant_dir, "spot.json")
    if os.path.isfile(spot_path):
        spot = gate.load_json(spot_path)
        w = next((w for w in spot.get("windows", []) if w.get("name") in ("bench_y0_p0", "y0_p0")), None)
        if w:
            w_frames = [tel[f] for f in gate.frames_in(w, "telemetry") if f in tel]
            if w_frames:
                return any(bool(r.get("rayTraced")) for r in w_frames)
    return any(bool(r.get("rayTraced")) for r in tel.values())


def _variant_pose_error_deg(variant_dir: str, tag: str = "y0_p0") -> float | None:
    spot_json = os.path.join(variant_dir, "spot.json")
    if not os.path.isfile(spot_json):
        return None
    try:
        data = json.load(open(spot_json, encoding="utf-8"))
    except (OSError, ValueError):
        return None
    target = pb.parse_tag_pose(tag) or (0.0, 0.0)
    for w in data.get("windows", []):
        if w.get("name") in ("bench_y0_p0", tag) and w.get("gamePose"):
            errs = [
                pb.angular_error_deg(target, (float(p[0]), float(p[1])))
                for p in w["gamePose"]
                if isinstance(p, (list, tuple)) and len(p) >= 2
            ]
            if errs:
                return max(errs)
    return None


def score_variants_sweep(
    sweep_run_dir: str,
    reference_dirs: list[str],
    spot_override: str | None = None,
    tag: str = "y0_p0",
) -> dict[str, Any]:
    """Scores a `verify_runtime.ps1 -Variants` output directory against the original-game reference."""
    manifest_path = os.path.join(sweep_run_dir, "manifest.json")
    spot_name = spot_override
    variant_names: list[str] = []
    if os.path.isfile(manifest_path):
        manifest = json.load(open(manifest_path, encoding="utf-8"))
        if not spot_name and manifest.get("spots"):
            spot_name = manifest["spots"][0]
        variant_names = list(manifest.get("variants") or [])
    if not variant_names:
        variant_names = sorted(
            d
            for d in os.listdir(sweep_run_dir)
            if os.path.isdir(os.path.join(sweep_run_dir, d))
            and os.path.isdir(os.path.join(sweep_run_dir, d, "gate"))
        )
    if not spot_name:
        for v in variant_names:
            sc = os.path.join(sweep_run_dir, v, "spot_config.json")
            if os.path.isfile(sc):
                spot_name = json.load(open(sc, encoding="utf-8")).get("name")
                if spot_name:
                    break
    if not spot_name:
        raise ValueError(f"Could not determine spot name in {sweep_run_dir}")

    if len(variant_names) == 1:
        only_vdir = os.path.join(sweep_run_dir, variant_names[0])
        if os.path.isfile(os.path.join(only_vdir, "gate", "live_step1_final.pfm")):
            return score_live_steps(sweep_run_dir, reference_dirs, spot_override=spot_name, ref_tag=tag)

    raster_pfm, ref_pose_err, ref_err_reason = _find_on_pose_reference_pfm(spot_name, tag, reference_dirs)
    raster_img = gate.read_pfm(raster_pfm) if raster_pfm else None

    raw_entries: dict[str, dict[str, Any]] = {}
    base_candidates: list[str] = []
    for vname in variant_names:
        vdir = os.path.join(sweep_run_dir, vname)
        f_pfm = os.path.join(vdir, "gate", f"{tag}_final.pfm")
        if not os.path.isfile(f_pfm):
            raw_entries[vname] = {"status": "MISSING_FINAL", "final_pfm": f_pfm}
            continue
        vjson_path = os.path.join(vdir, "variant.json")
        vmeta = json.load(open(vjson_path, encoding="utf-8")) if os.path.isfile(vjson_path) else {}
        rtx_keys = vmeta.get("rtx") or {}
        proxy_keys = vmeta.get("proxy") or {}
        rtx_loaded = vmeta.get("rtxLoaded", True)
        is_base = (not rtx_keys and not proxy_keys) and ("base" in vname.lower())
        if is_base:
            base_candidates.append(vname)

        f_img = gate.read_pfm(f_pfm)
        f_stats = pb.image_brightness_stats(f_img, mask_hud=True)
        pose_err = _variant_pose_error_deg(vdir, tag=tag)
        on_pose = (pose_err is None) or (pose_err <= PASS_THRESHOLDS["max_pose_error_deg"])
        rt_ok = _variant_ray_traced(vdir)
        if is_base and rt_ok is False:
            base_candidates.pop()
        cmp_res = pb.compare_final_to_raster(f_img, raster_img, mask_hud=True) if raster_img is not None else None
        raw_entries[vname] = {
            "is_base": is_base,
            "rtx": rtx_keys,
            "rtx_loaded": rtx_loaded,
            "ray_traced": rt_ok,
            "pose_error_deg": pose_err,
            "on_pose": on_pose,
            "gamma_mean": float(f_stats["gamma_mean"]),
            "linear_mean": float(f_stats["linear_mean"]),
            "near_white_share": float(f_stats["near_white_share"]),
            "nonfinite_pixels": int(f_stats["nonfinite"]),
            "vs_raster": cmp_res,
        }

    base_first_name = base_candidates[0] if len(base_candidates) >= 1 else None
    base_end_name = base_candidates[-1] if len(base_candidates) >= 2 else None
    base_luma = raw_entries[base_first_name]["gamma_mean"] if base_first_name else None
    base_end_luma = raw_entries[base_end_name]["gamma_mean"] if base_end_name else None

    non_base_lumas = {
        k: v["gamma_mean"]
        for k, v in raw_entries.items()
        if not v.get("is_base") and "gamma_mean" in v
    }
    drift_eval = evaluate_drift_gated_effects(base_luma, base_end_luma, non_base_lumas)

    variants_scored: dict[str, Any] = {}
    passing_variants: list[str] = []
    for vname, entry in raw_entries.items():
        if entry.get("status") == "MISSING_FINAL":
            variants_scored[vname] = entry
            continue
        out = dict(entry)
        if entry["is_base"]:
            if entry["ray_traced"] is False:
                out["measurement_status"] = "NOT_MEASURED"
                out["reasons"] = ["not_ray_traced"]
            else:
                out["measurement_status"] = "BASE_ANCHOR"
            variants_scored[vname] = out
            continue

        de = drift_eval["variants"][vname]
        out["delta_vs_base"] = de.get("delta_vs_base")
        out["effect_abs"] = de.get("effect_abs")
        out["effect_rel"] = de.get("effect_rel")
        if entry["ray_traced"] is False:
            out["measurement_status"] = "NOT_MEASURED"
            out["gate_pass"] = False
            out["reasons"] = ["not_ray_traced"]
        elif not entry["rtx_loaded"]:
            out["measurement_status"] = "NOT_MEASURED"
            out["gate_pass"] = False
            out["reasons"] = ["rtx_option_not_loaded_in_log"]
        elif not entry["on_pose"]:
            out["measurement_status"] = "NOT_MEASURED"
            out["gate_pass"] = False
            out["reasons"] = [f"variant_off_pose ({entry['pose_error_deg']:.2f} deg)"]
        elif de["status"] == "NOT_MEASURED":
            out["measurement_status"] = "NOT_MEASURED"
            out["gate_pass"] = None
            out["reasons"] = [de["reason"]]
        elif raster_img is None or entry["vs_raster"] is None:
            out["measurement_status"] = "NOT_MEASURED"
            out["gate_pass"] = None
            out["reasons"] = [ref_err_reason or "missing_reference"]
        elif entry["vs_raster"].get("black_raster"):
            out["measurement_status"] = "NOT_MEASURED"
            out["gate_pass"] = None
            out["reasons"] = ["black_raster"]
        else:
            out["measurement_status"] = "MEASURED"
            vr = entry["vs_raster"]
            ratio = vr["gamma_mean_ratio"]
            nw_delta = vr["near_white_delta"]
            reasons: list[str] = []
            if entry["nonfinite_pixels"] > 0 or vr["nonfinite_pixels"] > 0:
                reasons.append("nonfinite_pixels")
            if entry["gamma_mean"] < PASS_THRESHOLDS["min_scene_luma"]:
                reasons.append(f"below_luma_floor ({entry['gamma_mean']:.4f} < {PASS_THRESHOLDS['min_scene_luma']})")
            if ratio is None or ratio < PASS_THRESHOLDS["min_stop_ratio"]:
                reasons.append(f"too_dark_stop ({ratio:.3f}x < {PASS_THRESHOLDS['min_stop_ratio']}x)")
            elif ratio > PASS_THRESHOLDS["max_stop_ratio"]:
                reasons.append(f"too_bright_stop ({ratio:.3f}x > {PASS_THRESHOLDS['max_stop_ratio']}x)")
            if ratio is not None and (
                ratio < PASS_THRESHOLDS["min_spot_median_ratio"]
                or ratio > PASS_THRESHOLDS["max_spot_median_ratio"]
            ):
                reasons.append(
                    f"outside_spot_median_target ({ratio:.3f}x not in "
                    f"[{PASS_THRESHOLDS['min_spot_median_ratio']}, {PASS_THRESHOLDS['max_spot_median_ratio']}])"
                )
            if nw_delta > PASS_THRESHOLDS["max_near_white_delta"]:
                reasons.append(f"near_white_excess (+{nw_delta:.4f} > +{PASS_THRESHOLDS['max_near_white_delta']})")
            out["gate_pass"] = len(reasons) == 0
            out["reasons"] = reasons
            if out["gate_pass"]:
                passing_variants.append(vname)

        variants_scored[vname] = out

    # Sweep-level causal verdict
    measured_non_base = [
        v for v in variants_scored.values() if not v.get("is_base") and v.get("measurement_status") == "MEASURED"
    ]
    ae_off_entry = next(
        (
            v
            for k, v in variants_scored.items()
            if not v.get("is_base")
            and str(v.get("rtx", {}).get("rtx.autoExposure.enabled", "")).lower() == "false"
        ),
        None,
    )
    if not measured_non_base:
        verdict = "NOT_MEASURED"
    elif ae_off_entry and ae_off_entry.get("measurement_status") == "MEASURED" and ae_off_entry.get("vs_raster"):
        ae_ratio = ae_off_entry["vs_raster"].get("gamma_mean_ratio")
        all_ratios = [
            v["vs_raster"]["gamma_mean_ratio"]
            for v in measured_non_base
            if v.get("vs_raster") and v["vs_raster"].get("gamma_mean_ratio") is not None
        ]
        if ae_ratio is not None and ae_ratio > PASS_THRESHOLDS["max_stop_ratio"] and min(all_ratios) > PASS_THRESHOLDS["max_stop_ratio"]:
            verdict = "exposure_falsified_lights_too_strong"
        elif passing_variants or (ae_ratio is not None and ae_ratio <= PASS_THRESHOLDS["max_stop_ratio"]):
            verdict = "exposure_confirmed"
        else:
            verdict = "mixed_exposure_and_lights"
    elif passing_variants:
        verdict = "exposure_confirmed"
    else:
        verdict = "inconclusive"

    return {
        "sweep_run_dir": os.path.abspath(sweep_run_dir),
        "spot": spot_name,
        "tag": tag,
        "reference_pfm": raster_pfm,
        "reference_pose_error_deg": ref_pose_err,
        "base_first": base_first_name,
        "base_end": base_end_name,
        "base_luma": base_luma,
        "base_end_luma": base_end_luma,
        "base_drift_abs": drift_eval["base_drift_abs"],
        "base_drift_rel": drift_eval["base_drift_rel"],
        "passing_variants": passing_variants,
        "causal_verdict": verdict,
        "variants": variants_scored,
    }


POSE_DEPTH_MAX_PIXELS = 0.05  # matches picture_cost.py POSE_MAX_PIXELS

DEFAULT_LIVE_RTX = {
    "rtx.autoExposure.enabled": "True",
    "rtx.autoExposure.evMinValue": "-2.0",
    "rtx.localtonemap.exposure": "0.75",
    "rtx.localtonemap.shadows": "2.0",
    "rtx.tonemappingMode": "1",
    "rtx.tonemap.finalizeWithACES": "False",
    "rtx.tonemap.exposureBias": "0.0",
}


def _describe_live_rtx(rtx_keys: dict[str, Any], stag: str) -> str:
    diffs = []
    for k, v in rtx_keys.items():
        dflt = DEFAULT_LIVE_RTX.get(k)
        if dflt is None or str(v).strip().lower() != str(dflt).strip().lower():
            short_k = k.replace("rtx.autoExposure.", "ae.").replace("rtx.localtonemap.", "ltm.").replace("rtx.tonemap.", "tm.").replace("rtx.", "")
            diffs.append(f"{short_k}={v}")
    return "base" if not diffs else ",".join(diffs)


def _pose_depth_diff_share(depth_a: np.ndarray | None, depth_b: np.ndarray | None) -> float | None:
    """Share of pixels whose game depth differs by > 0.01, or None when either depth dump is absent."""
    if depth_a is None or depth_b is None or depth_a.shape != depth_b.shape:
        return None
    return float((np.abs(depth_a - depth_b) > 0.01).mean())


def _read_depth_pfm(gate_dir: str, tag: str) -> np.ndarray | None:
    p = os.path.join(gate_dir, f"{tag}_gamedepth.pfm")
    if not os.path.isfile(p):
        return None
    img = gate.read_pfm(p)
    return img[:, :, 0] if img.ndim == 3 else img


def _proxy_window_camera(spot_dir: str, window_names: tuple[str, ...]) -> np.ndarray | None:
    """Median render camera [x, y, z, yaw, pitch] from proxy_frames.csv over the first matching window."""
    spot_path = os.path.join(spot_dir, "spot.json")
    proxy_path = os.path.join(spot_dir, "proxy_frames.csv")
    if not (os.path.isfile(spot_path) and os.path.isfile(proxy_path)):
        return None
    spot = gate.load_json(spot_path)
    target_w = next(
        (w for w in spot.get("windows", []) if w.get("name") in window_names and w.get("proxy")),
        None,
    )
    if not target_w:
        return None
    lo, hi = int(target_w["proxy"][0]), int(target_w["proxy"][1])
    rows: list[list[float]] = []
    with open(proxy_path, errors="replace") as f:
        for line in f:
            c = line.rstrip("\n").split(",")
            if len(c) < 7 or not c[0].isdigit() or not c[6]:
                continue
            fr = int(c[0])
            if not (lo <= fr <= hi):
                continue
            for pub in c[6].split("|"):
                p = pub.split(":")
                if len(p) >= 9 and p[2] == "0":
                    rows.append([float(v) for v in p[3:9]])
                    break
    if not rows:
        return None
    m = np.median(np.asarray(rows, dtype=np.float64), axis=0)
    return np.array(
        [
            m[0],
            m[1],
            m[2],
            math.degrees(math.atan2(m[4], m[3])),
            math.degrees(math.asin(float(np.clip(m[5], -1.0, 1.0)))),
        ],
        dtype=np.float64,
    )


def _check_variant_vs_reference_camera(
    variant_dir: str,
    ref_spot_dir: str,
    win_names: tuple[str, ...] = ("live_step1", "bench_y0_p0", "y0_p0"),
    ref_tag: str = "y0_p0",
) -> tuple[bool, str | None]:
    """Checks camera placement against reference when both sides have proxy_frames.csv + spot.json."""
    cam_v = _proxy_window_camera(variant_dir, win_names)
    cam_r = _proxy_window_camera(ref_spot_dir, (ref_tag, "y0_p0_after"))
    if cam_v is None or cam_r is None:
        return True, None
    dc = float(np.linalg.norm(cam_v[:3] - cam_r[:3]))
    dyaw = abs(gate.wrap(float(cam_v[3] - cam_r[3])))
    dpitch = abs(float(cam_v[4] - cam_r[4]))
    if dc <= gate.REF_CAM_UNITS and dyaw <= 1.5 and dpitch <= 1.0:
        return True, None
    return (
        False,
        f"reference_pose_mismatch (camera {dc:.2f}u, yaw {dyaw:.2f} deg, pitch {dpitch:.2f} deg)",
    )


def _expected_effective_exposure_ceiling(rtx_keys: dict[str, Any]) -> float | None:
    """Closed-form effectiveExposure ceiling implied by liveRtx / variant rtx keys.

    From auto_exposure.comp.slang:123-130 and rtx_context.cpp:3436-3437:
      autoExposure <= 0.18 / max(0.001, (0.125 * 2^evMinValue) * 0.82) when enabled else 1.0
      effectiveExposure = autoExposure * localtonemap.exposure (mode 1) or autoExposure * 2^exposureBias (mode 0)
    """
    ae_en = str(rtx_keys.get("rtx.autoExposure.enabled", "True")).strip().lower() != "false"
    ev_min = float(rtx_keys.get("rtx.autoExposure.evMinValue", -2.0))
    tm_mode = int(rtx_keys.get("rtx.tonemappingMode", 1))
    ltm_exp = float(rtx_keys.get("rtx.localtonemap.exposure", 0.75))
    tm_bias = float(rtx_keys.get("rtx.tonemap.exposureBias", 0.0))
    if not ae_en:
        max_ae = 1.0
    else:
        min_lum = 0.125 * (2.0 ** ev_min)
        max_ae = 0.18 / max(0.001, min_lum * 0.82)
    mult = ltm_exp if tm_mode == 1 else (2.0 ** tm_bias)
    return float(max_ae * mult)


def _classify_live_step_role(
    rtx_keys: dict[str, Any],
    is_base: bool,
    n_non_base_steps: int,
) -> dict[str, Any]:
    """Classifies whether a live step is a base anchor, a single-stage diagnostic isolator,
    a multi-variant screening candidate, or a dedicated 3-step off/on/off fix test."""
    if is_base:
        return {
            "step_role": "base_anchor",
            "diagnostic_stage": None,
            "narrows_cause_only": False,
            "requires_dedicated_off_on_off": False,
        }
    ae_en = str(rtx_keys.get("rtx.autoExposure.enabled", "True")).strip().lower() != "false"
    ev_min = float(rtx_keys.get("rtx.autoExposure.evMinValue", -2.0))
    tm_mode = int(rtx_keys.get("rtx.tonemappingMode", 1))
    ltm_exp = float(rtx_keys.get("rtx.localtonemap.exposure", 0.75))
    ltm_sh = float(rtx_keys.get("rtx.localtonemap.shadows", 2.0))

    if (not ae_en) and tm_mode == 1 and abs(ltm_sh - 2.0) < 1e-6:
        return {
            "step_role": "diagnostic_cause_isolation",
            "diagnostic_stage": "stage1_auto_exposure_off_fixed_1x",
            "narrows_cause_only": True,
            "requires_dedicated_off_on_off": True,
        }
    if ae_en and abs(ev_min - (-2.0)) < 1e-6 and tm_mode == 1 and abs(ltm_exp - 0.75) < 1e-6 and abs(ltm_sh) < 1e-6:
        return {
            "step_role": "diagnostic_cause_isolation",
            "diagnostic_stage": "stage2_localtonemap_shadows_zero_only",
            "narrows_cause_only": True,
            "requires_dedicated_off_on_off": True,
        }
    if n_non_base_steps > 1:
        return {
            "step_role": "screening_fix_candidate",
            "diagnostic_stage": None,
            "narrows_cause_only": True,
            "requires_dedicated_off_on_off": True,
        }
    return {
        "step_role": "dedicated_off_on_off_candidate",
        "diagnostic_stage": None,
        "narrows_cause_only": False,
        "requires_dedicated_off_on_off": False,
    }


def score_live_steps(
    live_dir: str,
    reference_dirs: list[str],
    spot_override: str | None = None,
    ref_tag: str = "y0_p0",
) -> dict[str, Any]:
    """Scores a single-launch `liveSteps` + `liveStepDump: true` spot/variant folder against reference."""
    # Accept either the sweep root (<stamp>-settings containing 1 variant folder) or the variant folder itself.
    vdir = live_dir
    spot_name = spot_override
    manifest_path = os.path.join(live_dir, "manifest.json")
    if os.path.isfile(manifest_path) and not os.path.isfile(os.path.join(live_dir, "spot.json")):
        manifest = json.load(open(manifest_path, encoding="utf-8"))
        if not spot_name and manifest.get("spots"):
            spot_name = manifest["spots"][0]
        vlist = list(manifest.get("variants") or [])
        if len(vlist) == 1 and os.path.isdir(os.path.join(live_dir, vlist[0])):
            vdir = os.path.join(live_dir, vlist[0])
    if not spot_name:
        for cand_json in ("spot.json", "spot_config.json"):
            p = os.path.join(vdir, cand_json)
            if os.path.isfile(p):
                spot_name = json.load(open(p, encoding="utf-8")).get("name")
                if spot_name:
                    break
    if not spot_name:
        raise ValueError(f"Could not determine spot name in {live_dir}")

    spot_path = os.path.join(vdir, "spot.json")
    if not os.path.isfile(spot_path):
        raise ValueError(f"Missing spot.json in {vdir}")
    spot = gate.load_json(spot_path)
    live_wins = [
        w
        for w in spot.get("windows", [])
        if str(w.get("name", "")).startswith("live_step")
        and not str(w.get("name", "")).endswith("_dump")
    ]
    if len(live_wins) < 3:
        raise ValueError(f"Need at least 3 live_step windows (base, variant..., base_end) in {spot_path}")
    n_non_base_steps = len(live_wins) - 2
    sweep_design = "dedicated_off_on_off" if n_non_base_steps == 1 else "multi_variant_screening"

    raster_pfm, ref_pose_err, ref_err_reason = _find_on_pose_reference_pfm(spot_name, ref_tag, reference_dirs)
    raster_img = gate.read_pfm(raster_pfm) if raster_pfm else None
    ref_spot_dir = os.path.dirname(os.path.dirname(raster_pfm)) if raster_pfm else None
    ref_placed, ref_place_reason = (
        _check_variant_vs_reference_camera(vdir, ref_spot_dir, ref_tag=ref_tag)
        if ref_spot_dir
        else (False, ref_err_reason or "missing_reference")
    )

    stop_tel = extract_stop_exposure_telemetry(vdir)
    gate_dir = os.path.join(vdir, "gate")
    base_tag = str(live_wins[0]["name"])
    end_tag = str(live_wins[-1]["name"])
    base_depth = _read_depth_pfm(gate_dir, base_tag)

    raw_steps: dict[str, dict[str, Any]] = {}
    for idx, w in enumerate(live_wins):
        stag = str(w["name"])
        rtx_keys = dict(w.get("liveRtx") or {})
        label = _describe_live_rtx(rtx_keys, stag)
        is_base = idx in (0, len(live_wins) - 1)
        role_info = _classify_live_step_role(rtx_keys, is_base=is_base, n_non_base_steps=n_non_base_steps)
        f_pfm = os.path.join(gate_dir, f"{stag}_final.pfm")
        if not os.path.isfile(f_pfm):
            raw_steps[stag] = {
                "step": stag,
                "label": label,
                "is_base": is_base,
                **role_info,
                "status": "MISSING_FINAL",
            }
            continue
        f_img = gate.read_pfm(f_pfm)
        f_stats = pb.image_brightness_stats(f_img, mask_hud=True)
        tel_info = stop_tel.get(stag) or {}
        rt_share = tel_info.get("ray_traced_share")
        rt_ok = None if rt_share is None else (rt_share >= 0.90)
        eff_med = tel_info.get("effective_exposure_median")
        ae_med = tel_info.get("auto_exposure_median")
        depth_diff = _pose_depth_diff_share(base_depth, _read_depth_pfm(gate_dir, stag))
        cmp_res = (
            pb.compare_final_to_raster(f_img, raster_img, mask_hud=True)
            if (raster_img is not None and ref_placed)
            else None
        )
        raw_steps[stag] = {
            "step": stag,
            "label": label,
            "is_base": is_base,
            **role_info,
            "rtx": rtx_keys,
            "ray_traced": rt_ok,
            "ray_traced_share": rt_share,
            "auto_exposure_median": ae_med,
            "effective_exposure_median": eff_med,
            "exposure_settled": tel_info.get("exposure_settled"),
            "last2s_frames": tel_info.get("last2s_frames"),
            "last2s_duration_ms": tel_info.get("last2s_duration_ms"),
            "last2s_auto_exp_rel_span": tel_info.get("last2s_auto_exp_rel_span"),
            "last2s_eff_exp_rel_span": tel_info.get("last2s_eff_exp_rel_span"),
            "last2s_auto_exp_f2f_max_rel": tel_info.get("last2s_auto_exp_f2f_max_rel"),
            "last2s_eff_exp_f2f_max_rel": tel_info.get("last2s_eff_exp_f2f_max_rel"),
            "depth_diff_vs_step1": depth_diff,
            "gamma_mean": float(f_stats["gamma_mean"]),
            "linear_mean": float(f_stats["linear_mean"]),
            "near_white_share": float(f_stats["near_white_share"]),
            "nonfinite_pixels": int(f_stats["nonfinite"]),
            "vs_raster": cmp_res,
        }

    b1 = raw_steps.get(base_tag, {})
    bN = raw_steps.get(end_tag, {})
    b1_settled = b1.get("exposure_settled") is True
    bN_settled = bN.get("exposure_settled") is True
    base_luma = (
        b1.get("gamma_mean")
        if (b1.get("ray_traced") is not False and b1_settled)
        else None
    )
    base_end_luma = (
        bN.get("gamma_mean")
        if (bN.get("ray_traced") is not False and bN_settled)
        else None
    )
    base_eff_exp = b1.get("effective_exposure_median")

    non_base_lumas = {
        k: v["gamma_mean"]
        for k, v in raw_steps.items()
        if not v.get("is_base") and "gamma_mean" in v
    }
    drift_eval = evaluate_drift_gated_effects(base_luma, base_end_luma, non_base_lumas)

    steps_scored: dict[str, Any] = {}
    passing_steps: list[str] = []
    for stag, entry in raw_steps.items():
        if entry.get("status") == "MISSING_FINAL":
            steps_scored[stag] = entry
            continue
        out = dict(entry)
        if entry["is_base"]:
            if entry["ray_traced"] is False:
                out["measurement_status"] = "NOT_MEASURED"
                out["reasons"] = ["not_ray_traced"]
            elif entry["exposure_settled"] is not True:
                out["measurement_status"] = "NOT_MEASURED"
                out["reasons"] = ["exposure not settled"]
            elif entry["depth_diff_vs_step1"] is not None and entry["depth_diff_vs_step1"] > POSE_DEPTH_MAX_PIXELS:
                out["measurement_status"] = "NOT_MEASURED"
                out["reasons"] = ["intra_launch_pose_moved"]
            else:
                out["measurement_status"] = "BASE_ANCHOR"
            steps_scored[stag] = out
            continue

        de = drift_eval["variants"][stag]
        out["delta_vs_base"] = de.get("delta_vs_base")
        out["effect_abs"] = de.get("effect_abs")
        out["effect_rel"] = de.get("effect_rel")

        # Verify SetConfigVariable took effect when the requested rtx ceiling is well below base effectiveExposure.
        ceil_eff = _expected_effective_exposure_ceiling(entry["rtx"])
        out["expected_effective_exposure_ceiling"] = ceil_eff
        took_ok = True
        if (
            ceil_eff is not None
            and base_eff_exp is not None
            and entry["effective_exposure_median"] is not None
            and ceil_eff < 0.85 * float(base_eff_exp)
            and float(entry["effective_exposure_median"]) > ceil_eff * 1.08
        ):
            took_ok = False

        if entry["ray_traced"] is False:
            out["measurement_status"] = "NOT_MEASURED"
            out["gate_pass"] = False
            out["counts_as_proven_fix"] = False
            out["reasons"] = ["not_ray_traced"]
        elif entry["exposure_settled"] is not True:
            out["measurement_status"] = "NOT_MEASURED"
            out["gate_pass"] = None
            out["counts_as_proven_fix"] = False
            out["reasons"] = ["exposure not settled"]
        elif not took_ok:
            out["measurement_status"] = "NOT_MEASURED"
            out["gate_pass"] = False
            out["counts_as_proven_fix"] = False
            out["reasons"] = ["live_rtx_did_not_take"]
        elif entry["depth_diff_vs_step1"] is not None and entry["depth_diff_vs_step1"] > POSE_DEPTH_MAX_PIXELS:
            out["measurement_status"] = "NOT_MEASURED"
            out["gate_pass"] = False
            out["counts_as_proven_fix"] = False
            out["reasons"] = ["intra_launch_pose_moved"]
        elif not b1_settled or not bN_settled:
            out["measurement_status"] = "NOT_MEASURED"
            out["gate_pass"] = None
            out["counts_as_proven_fix"] = False
            out["reasons"] = ["exposure not settled"]
        elif de["status"] == "NOT_MEASURED":
            out["measurement_status"] = "NOT_MEASURED"
            out["gate_pass"] = None
            out["counts_as_proven_fix"] = False
            out["reasons"] = [de["reason"]]
        elif not ref_placed or raster_img is None or entry["vs_raster"] is None:
            out["measurement_status"] = "NOT_MEASURED"
            out["gate_pass"] = None
            out["counts_as_proven_fix"] = False
            out["reasons"] = [ref_place_reason or ref_err_reason or "missing_reference"]
        elif entry["vs_raster"].get("black_raster"):
            out["measurement_status"] = "NOT_MEASURED"
            out["gate_pass"] = None
            out["counts_as_proven_fix"] = False
            out["reasons"] = ["black_raster"]
        else:
            out["measurement_status"] = "MEASURED"
            vr = entry["vs_raster"]
            ratio = vr["gamma_mean_ratio"]
            nw_delta = vr["near_white_delta"]
            reasons: list[str] = []
            if entry["nonfinite_pixels"] > 0 or vr["nonfinite_pixels"] > 0:
                reasons.append("nonfinite_pixels")
            if entry["gamma_mean"] < PASS_THRESHOLDS["min_scene_luma"]:
                reasons.append(f"below_luma_floor ({entry['gamma_mean']:.4f} < {PASS_THRESHOLDS['min_scene_luma']})")
            if ratio is None or ratio < PASS_THRESHOLDS["min_stop_ratio"]:
                reasons.append(f"too_dark_stop ({ratio:.3f}x < {PASS_THRESHOLDS['min_stop_ratio']}x)")
            elif ratio > PASS_THRESHOLDS["max_stop_ratio"]:
                reasons.append(f"too_bright_stop ({ratio:.3f}x > {PASS_THRESHOLDS['max_stop_ratio']}x)")
            if ratio is not None and (
                ratio < PASS_THRESHOLDS["min_spot_median_ratio"]
                or ratio > PASS_THRESHOLDS["max_spot_median_ratio"]
            ):
                reasons.append(
                    f"outside_spot_median_target ({ratio:.3f}x not in "
                    f"[{PASS_THRESHOLDS['min_spot_median_ratio']}, {PASS_THRESHOLDS['max_spot_median_ratio']}])"
                )
            if nw_delta > PASS_THRESHOLDS["max_near_white_delta"]:
                reasons.append(f"near_white_excess (+{nw_delta:.4f} > +{PASS_THRESHOLDS['max_near_white_delta']})")
            out["gate_pass"] = len(reasons) == 0
            out["counts_as_proven_fix"] = bool(
                out["gate_pass"]
                and not out.get("narrows_cause_only")
                and not out.get("requires_dedicated_off_on_off")
            )
            out["reasons"] = reasons
            if out["gate_pass"]:
                passing_steps.append(stag)

        steps_scored[stag] = out

    measured_non_base = [
        v for v in steps_scored.values() if not v.get("is_base") and v.get("measurement_status") == "MEASURED"
    ]
    ae_off_entry = next(
        (
            v
            for v in steps_scored.values()
            if not v.get("is_base")
            and str(v.get("rtx", {}).get("rtx.autoExposure.enabled", "")).lower() == "false"
        ),
        None,
    )
    if not measured_non_base:
        verdict = "NOT_MEASURED"
    elif ae_off_entry and ae_off_entry.get("measurement_status") == "MEASURED" and ae_off_entry.get("vs_raster"):
        ae_ratio = ae_off_entry["vs_raster"].get("gamma_mean_ratio")
        all_ratios = [
            v["vs_raster"]["gamma_mean_ratio"]
            for v in measured_non_base
            if v.get("vs_raster") and v["vs_raster"].get("gamma_mean_ratio") is not None
        ]
        if (
            ae_ratio is not None
            and ae_ratio > PASS_THRESHOLDS["max_stop_ratio"]
            and min(all_ratios) > PASS_THRESHOLDS["max_stop_ratio"]
        ):
            verdict = "exposure_falsified_lights_too_strong"
        elif passing_steps or (ae_ratio is not None and ae_ratio <= PASS_THRESHOLDS["max_stop_ratio"]):
            verdict = "exposure_confirmed"
        else:
            verdict = "mixed_exposure_and_lights"
    elif passing_steps:
        verdict = "exposure_confirmed"
    else:
        verdict = "inconclusive"

    diagnostic_isolation_variants = [
        stag
        for stag, v in steps_scored.items()
        if not v.get("is_base") and v.get("step_role") == "diagnostic_cause_isolation"
    ]
    cause_narrowing_only_variants = [
        stag
        for stag, v in steps_scored.items()
        if not v.get("is_base") and v.get("narrows_cause_only")
    ]
    candidate_fixes_needing_off_on_off = [
        stag for stag in passing_steps if steps_scored[stag].get("requires_dedicated_off_on_off")
    ]
    confirmed_fixes = [stag for stag in passing_steps if steps_scored[stag].get("counts_as_proven_fix")]
    if sweep_design == "multi_variant_screening":
        cause_narrowing_note = (
            "Multi-variant liveSteps launch narrows the cause and screens candidates only "
            "(diagnostic_isolation_variants isolate Stage 1 autoExposure vs Stage 2 localtonemap.shadows; "
            "remaining steps screen combined exposure/tonemap candidates). "
            "base, variant, base_end in one dedicated 3-step off / on / off launch is required before any "
            "passing candidate counts as a proven fix."
        )
    else:
        cause_narrowing_note = (
            "Single-variant 3-step base -> variant -> base_end (off / on / off) launch on one build; "
            "a settled MEASURED non-diagnostic candidate in confirmed_fixes counts as a proven fix on this spot."
        )

    return {
        "mode": "live_steps",
        "sweep_design": sweep_design,
        "settle_guard": EXPOSURE_SETTLE_SPEC,
        "live_dir": os.path.abspath(vdir),
        "spot": spot_name,
        "reference_tag": ref_tag,
        "reference_pfm": raster_pfm,
        "reference_placed": ref_placed,
        "reference_pose_error_deg": ref_pose_err,
        "base_first": base_tag,
        "base_end": end_tag,
        "base_luma": base_luma,
        "base_end_luma": base_end_luma,
        "base_drift_abs": drift_eval["base_drift_abs"],
        "base_drift_rel": drift_eval["base_drift_rel"],
        "diagnostic_isolation_variants": diagnostic_isolation_variants,
        "cause_narrowing_only_variants": cause_narrowing_only_variants,
        "passing_variants": passing_steps,
        "candidate_fixes_needing_off_on_off": candidate_fixes_needing_off_on_off,
        "confirmed_fixes": confirmed_fixes,
        "cause_narrowing_note": cause_narrowing_note,
        "causal_verdict": verdict,
        "variants": steps_scored,
    }


def _write_uniform_pfm(path: str, val: float, h: int = 32, w: int = 32) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    arr = np.full((h, w, 3), float(val), dtype=np.float32)
    gate.write_pfm(path, arr)


def _write_depth_pfm(path: str, arr2d: np.ndarray) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    arr3 = np.repeat(arr2d[:, :, None].astype(np.float32), 3, axis=2)
    gate.write_pfm(path, arr3)


def run_self_test() -> None:
    # 1. Fake where Remix output stays constant (~0.47) across a 4x original-game range -> "exposure"
    orig_stops = np.linspace(0.07, 0.28, 20)
    const_remix = np.full_like(orig_stops, 0.47) + np.linspace(-0.008, 0.008, 20)
    res_exp = fit_across_stop_elasticity(orig_stops, const_remix)
    assert res_exp["diagnosis"] == "exposure", f"Expected exposure, got {res_exp}"
    assert abs(res_exp["log2_elasticity_slope"]) < 0.15, f"Expected near-zero slope, got {res_exp}"
    assert res_exp["rmse_constant_exposure_model"] < res_exp["rmse_proportional_light_model"]

    # 2. Fake where Remix output follows the original proportionally (2.4x light multiplier) -> "light_strength"
    prop_remix = np.clip(2.4 * orig_stops, 0.0, 1.0)
    res_light = fit_across_stop_elasticity(orig_stops, prop_remix)
    assert res_light["diagnosis"] == "light_strength", f"Expected light_strength, got {res_light}"
    assert res_light["log2_elasticity_slope"] > 0.85, f"Expected ~1.0 slope, got {res_light}"
    assert res_light["rmse_proportional_light_model"] < res_light["rmse_constant_exposure_model"]

    # 3. Fake where base and base_end differ by more than the effect -> NOT_MEASURED (with named base_drift_*)
    drift_res = evaluate_drift_gated_effects(
        base_luma=0.4272,
        base_end_luma=0.4639,  # +8.59% drift, matching con_bright_hall base -> base_again
        variants={"fill_lights_off": 0.4253, "flare_balls_off": 0.4270},
    )
    assert drift_res["status"] == "NOT_MEASURED", f"Expected NOT_MEASURED on drift > effect, got {drift_res}"
    assert abs(drift_res["base_drift_abs"] - 0.0367) < 1e-6
    assert abs(drift_res["base_drift_rel"] - (0.0367 / 0.4272)) < 1e-6
    for v in drift_res["variants"].values():
        assert v["status"] == "NOT_MEASURED" and v["reason"] == "base_drift_exceeds_effect"

    # 4. End-to-end --score-sweep self-test:
    #    (a) noisy sweep where 01_base=0.42 -> 07_base_end=0.52 (drift=0.10) drowns a 0.36 variant (effect=0.06) -> NOT_MEASURED
    #    (b) clean sweep where 01_base=0.48, 07_base_end=0.482 (drift=0.002), raster=0.17, and 05_ltm=0.20 (1.18x) -> PASS & exposure_confirmed
    #    (c) falsification sweep where raster=0.17 and even ae_off_fixed stays at 0.38 (2.24x > 1.85x) -> exposure_falsified_lights_too_strong
    with tempfile.TemporaryDirectory() as tmp:
        ref_dir = os.path.join(tmp, "ref")
        _write_uniform_pfm(os.path.join(ref_dir, "con_bright_hall", "gate", "y0_p0_raster.pfm"), 0.17)
        with open(os.path.join(ref_dir, "con_bright_hall", "gate", "y0_p0_raster.json"), "w", encoding="utf-8") as f:
            json.dump({"relPose": [0.0, 0.0]}, f)

        # (a) Drift exceeds effect -> NOT_MEASURED
        noisy_dir = os.path.join(tmp, "sweep_noisy")
        os.makedirs(noisy_dir, exist_ok=True)
        with open(os.path.join(noisy_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"spots": ["con_bright_hall"], "variants": ["01_base", "02_test", "07_base_end"]}, f)
        for vname, val, rtx in [
            ("01_base", 0.42, {}),
            ("02_test", 0.36, {"rtx.localtonemap.exposure": "0.5"}),
            ("07_base_end", 0.52, {}),
        ]:
            _write_uniform_pfm(os.path.join(noisy_dir, vname, "gate", "y0_p0_final.pfm"), val)
            with open(os.path.join(noisy_dir, vname, "variant.json"), "w", encoding="utf-8") as f:
                json.dump({"name": vname, "rtx": rtx, "rtxLoaded": True}, f)
        sc_noisy = score_variants_sweep(noisy_dir, [ref_dir])
        assert sc_noisy["causal_verdict"] == "NOT_MEASURED", f"Expected NOT_MEASURED, got {sc_noisy}"
        assert sc_noisy["variants"]["02_test"]["measurement_status"] == "NOT_MEASURED"
        assert abs(sc_noisy["base_drift_abs"] - 0.10) < 1e-5

        # (b) Clean sweep -> PASS & exposure_confirmed
        clean_dir = os.path.join(tmp, "sweep_clean")
        os.makedirs(clean_dir, exist_ok=True)
        with open(os.path.join(clean_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(
                {"spots": ["con_bright_hall"], "variants": ["01_base", "02_ae_off_fixed", "05_ltm", "07_base_end"]},
                f,
            )
        for vname, val, rtx in [
            ("01_base", 0.48, {}),
            ("02_ae_off_fixed", 0.18, {"rtx.autoExposure.enabled": "False"}),
            ("05_ltm", 0.20, {"rtx.localtonemap.exposure": "0.35", "rtx.localtonemap.shadows": "0.0"}),
            ("07_base_end", 0.482, {}),
        ]:
            _write_uniform_pfm(os.path.join(clean_dir, vname, "gate", "y0_p0_final.pfm"), val)
            with open(os.path.join(clean_dir, vname, "variant.json"), "w", encoding="utf-8") as f:
                json.dump({"name": vname, "rtx": rtx, "rtxLoaded": True}, f)
        sc_clean = score_variants_sweep(clean_dir, [ref_dir])
        assert sc_clean["causal_verdict"] == "exposure_confirmed", f"Expected exposure_confirmed, got {sc_clean}"
        assert sc_clean["passing_variants"] == ["02_ae_off_fixed", "05_ltm"], f"Unexpected passing: {sc_clean}"

        # (c) Falsification sweep -> exposure_falsified_lights_too_strong
        fals_dir = os.path.join(tmp, "sweep_falsified")
        os.makedirs(fals_dir, exist_ok=True)
        with open(os.path.join(fals_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"spots": ["con_bright_hall"], "variants": ["01_base", "02_ae_off_fixed", "07_base_end"]}, f)
        for vname, val, rtx in [
            ("01_base", 0.48, {}),
            ("02_ae_off_fixed", 0.38, {"rtx.autoExposure.enabled": "False"}),
            ("07_base_end", 0.481, {}),
        ]:
            _write_uniform_pfm(os.path.join(fals_dir, vname, "gate", "y0_p0_final.pfm"), val)
            with open(os.path.join(fals_dir, vname, "variant.json"), "w", encoding="utf-8") as f:
                json.dump({"name": vname, "rtx": rtx, "rtxLoaded": True}, f)
        sc_fals = score_variants_sweep(fals_dir, [ref_dir])
        assert sc_fals["causal_verdict"] == "exposure_falsified_lights_too_strong", f"Got {sc_fals}"
        assert sc_fals["passing_variants"] == []

        # (d) Black-reference skip + y0_p0_after fallback + rayTraced=false rejection
        black_ref = os.path.join(tmp, "ref_black")
        _write_uniform_pfm(os.path.join(black_ref, "con_custodial", "gate", "y0_p0_raster.pfm"), 0.0)
        with open(os.path.join(black_ref, "con_custodial", "gate", "y0_p0_raster.json"), "w", encoding="utf-8") as f:
            json.dump({"relPose": [0.0, 0.0]}, f)
        good_ref = os.path.join(tmp, "ref_000454_style")
        _write_uniform_pfm(os.path.join(good_ref, "con_custodial", "gate", "y0_p0_raster.pfm"), 0.14)
        with open(os.path.join(good_ref, "con_custodial", "gate", "y0_p0_raster.json"), "w", encoding="utf-8") as f:
            json.dump({"relPose": [0.0, 4.62]}, f)  # off-pose y0_p0
        _write_uniform_pfm(os.path.join(good_ref, "con_custodial", "gate", "y0_p0_after_raster.pfm"), 0.14)
        with open(
            os.path.join(good_ref, "con_custodial", "gate", "y0_p0_after_raster.json"), "w", encoding="utf-8"
        ) as f:
            json.dump({"relPose": [-0.05, 0.0]}, f)  # on-pose y0_p0_after

        cust_dir = os.path.join(tmp, "sweep_cust")
        os.makedirs(cust_dir, exist_ok=True)
        with open(os.path.join(cust_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(
                {"spots": ["con_custodial"], "variants": ["01_base", "02_untraced", "03_ok", "07_base_end"]}, f
            )
        for vname, val, rtx, rt_flag in [
            ("01_base", 0.48, {}, True),
            ("02_untraced", 0.04, {"rtx.autoExposure.enabled": "False"}, False),
            ("03_ok", 0.16, {"rtx.localtonemap.exposure": "0.35"}, True),
            ("07_base_end", 0.482, {}, True),
        ]:
            _write_uniform_pfm(os.path.join(cust_dir, vname, "gate", "y0_p0_final.pfm"), val)
            with open(os.path.join(cust_dir, vname, "variant.json"), "w", encoding="utf-8") as f:
                json.dump({"name": vname, "rtx": rtx, "rtxLoaded": True}, f)
            with open(os.path.join(cust_dir, vname, "telemetry.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps({"frame": 100, "rayTraced": rt_flag}) + "\n")

        sc_cust = score_variants_sweep(cust_dir, [black_ref, good_ref])
        assert sc_cust["reference_pfm"] and sc_cust["reference_pfm"].endswith("y0_p0_after_raster.pfm"), sc_cust
        assert sc_cust["variants"]["02_untraced"]["measurement_status"] == "NOT_MEASURED"
        assert sc_cust["variants"]["02_untraced"]["reasons"] == ["not_ray_traced"]
        assert sc_cust["passing_variants"] == ["03_ok"], sc_cust

        # 5. analyze_gate_runs stop_ok placement rejection + per-stop exposure telemetry regimes
        gate_run = os.path.join(tmp, "gate_run")
        gate_ref = os.path.join(tmp, "gate_ref")
        tags4 = ["y0_p0", "y45_p0", "y90_p0", "y135_p0"]
        orig4 = [0.06, 0.10, 0.16, 0.25]
        tel_lines_active: list[str] = []
        tel_lines_clamped: list[str] = []
        win_defs: list[dict[str, Any]] = []
        for i, (tname, ov) in enumerate(zip(tags4, orig4)):
            f0 = 100 + i * 20
            _write_uniform_pfm(os.path.join(gate_ref, "spot_act", "gate", f"{tname}_raster.pfm"), ov)
            _write_uniform_pfm(os.path.join(gate_ref, "spot_clamp", "gate", f"{tname}_raster.pfm"), ov)
            _write_uniform_pfm(os.path.join(gate_run, "spot_act", "gate", f"{tname}_final.pfm"), 0.48)
            _write_uniform_pfm(os.path.join(gate_run, "spot_clamp", "gate", f"{tname}_final.pfm"), 0.24)
            win_defs.append({"name": tname, "kind": "still", "telemetry": [f0, f0 + 9]})
            ae_act = min(7.0244, 0.70 / ov)
            for fr in range(f0, f0 + 10):
                tel_lines_active.append(
                    json.dumps(
                        {
                            "frame": fr,
                            "rayTraced": True,
                            "autoExposure": ae_act,
                            "effectiveExposure": 0.75 * ae_act,
                        }
                    )
                )
                tel_lines_clamped.append(
                    json.dumps(
                        {
                            "frame": fr,
                            "rayTraced": True,
                            "autoExposure": 7.0244,
                            "effectiveExposure": 5.2683,
                        }
                    )
                )
        for sname, tlines in (("spot_act", tel_lines_active), ("spot_clamp", tel_lines_clamped)):
            with open(os.path.join(gate_run, sname, "spot.json"), "w", encoding="utf-8") as f:
                json.dump({"name": sname, "windows": win_defs}, f)
            with open(os.path.join(gate_run, sname, "telemetry.jsonl"), "w", encoding="utf-8") as f:
                f.write("\n".join(tlines) + "\n")

        # Reject y135_p0 via stop_ok -> only 3 stops remain
        ag = analyze_gate_runs(
            [gate_run],
            [gate_ref],
            stop_ok=lambda _sd, _rd, tg: (tg != "y135_p0", "misplaced_test" if tg == "y135_p0" else "ok"),
        )
        assert ag["spots"]["spot_act"]["n_on_pose"] == 3, ag["spots"]["spot_act"]
        assert (
            ag["spots"]["spot_act"]["exposure_telemetry"]["exposure_telemetry_regime"] == "active_inverse_gain"
        ), ag["spots"]["spot_act"]
        assert (
            ag["spots"]["spot_clamp"]["exposure_telemetry"]["exposure_telemetry_regime"] == "clamped_at_ceiling"
        ), ag["spots"]["spot_clamp"]
        assert (
            ag["spots"]["spot_clamp"]["exposure_telemetry"]["normalized_pre_tonemap_radiance_ratio_median"] is not None
        ), ag["spots"]["spot_clamp"]

        # 6. score_live_steps: clean PASS + failing fake cases for live_rtx_did_not_take,
        #    intra_launch_pose_moved, and reference_pose_mismatch
        yy, xx = np.mgrid[0:32, 0:32]
        depth_ok = (100.0 + 10.0 * ((xx // 4) % 2) + yy * 0.5).astype(np.float32)
        depth_moved = np.roll(depth_ok, 1, axis=1)

        live_ref = os.path.join(tmp, "live_ref")
        _write_uniform_pfm(os.path.join(live_ref, "con_custodial", "gate", "y0_p0_raster.pfm"), 0.14)
        with open(os.path.join(live_ref, "con_custodial", "spot.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "con_custodial", "windows": [{"name": "y0_p0", "kind": "still", "proxy": [10, 20]}]}, f)
        with open(os.path.join(live_ref, "con_custodial", "proxy_frames.csv"), "w", encoding="utf-8") as f:
            f.write("frame,dt,draws,prims,tex,lights,pub\n")
            for fr in range(10, 21):
                f.write(f"{fr},16.6,100,1000,10,5,c:0:0:10.0:20.0:30.0:1.0:0.0:0.0\n")

        def _build_live_case(
            case_name: str,
            stuck_rtx: bool = False,
            move_depth: bool = False,
            far_cam: bool = False,
            ramp_step3: bool = False,
            ramp_base_end: bool = False,
            single_candidate_off_on_off: bool = False,
        ):
            cdir = os.path.join(tmp, case_name, "01_live_exposure")
            os.makedirs(cdir, exist_ok=True)
            with open(os.path.join(tmp, case_name, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump({"spots": ["con_custodial"], "variants": ["01_live_exposure"]}, f)
            if single_candidate_off_on_off:
                steps_meta = [
                    ("live_step1", 0.48, 5.75, 4.31, {"rtx.autoExposure.enabled": "True", "rtx.autoExposure.evMinValue": "-2.0", "rtx.localtonemap.exposure": "0.75"}),
                    ("live_step2", 0.16, 1.75, 1.31, {"rtx.autoExposure.enabled": "True", "rtx.autoExposure.evMinValue": "0.0", "rtx.localtonemap.exposure": "0.75", "rtx.localtonemap.shadows": "0.0"}),
                    ("live_step3", 0.481, 5.75, 4.31, {"rtx.autoExposure.enabled": "True", "rtx.autoExposure.evMinValue": "-2.0", "rtx.localtonemap.exposure": "0.75"}),
                ]
            else:
                steps_meta = [
                    ("live_step1", 0.48, 5.75, 4.31, {"rtx.autoExposure.enabled": "True", "rtx.autoExposure.evMinValue": "-2.0", "rtx.localtonemap.exposure": "0.75"}),
                    ("live_step2", 0.17, (5.75 if stuck_rtx else 1.0), (4.31 if stuck_rtx else 0.75), {"rtx.autoExposure.enabled": "False", "rtx.localtonemap.exposure": "0.75"}),
                    ("live_step3", 0.16, 1.75, 1.31, {"rtx.autoExposure.enabled": "True", "rtx.autoExposure.evMinValue": "0.0", "rtx.localtonemap.exposure": "0.75", "rtx.localtonemap.shadows": "0.0"}),
                    ("live_step4", 0.481, 5.75, 4.31, {"rtx.autoExposure.enabled": "True", "rtx.autoExposure.evMinValue": "-2.0", "rtx.localtonemap.exposure": "0.75"}),
                ]
            wins = []
            t_rows = []
            n_fr = 120  # 4.0 s at 33.33 ms/frame -> last 2.0 s covers ~61 frames
            for idx, (st, val, ae_v, eff_v, rtx_d) in enumerate(steps_meta):
                f0 = 100 + idx * (n_fr + 10)
                wins.append({"name": st, "kind": "still", "telemetry": [f0, f0 + n_fr - 1], "proxy": [f0, f0 + n_fr - 1], "liveRtx": rtx_d})
                _write_uniform_pfm(os.path.join(cdir, "gate", f"{st}_final.pfm"), val)
                _write_depth_pfm(
                    os.path.join(cdir, "gate", f"{st}_gamedepth.pfm"),
                    depth_moved if (move_depth and st == "live_step3") else depth_ok,
                )
                for k in range(n_fr):
                    fr = f0 + k
                    qpc_ms = float(fr) * (1000.0 / 30.0)
                    ae_cur, eff_cur = float(ae_v), float(eff_v)
                    if st == "live_step3" and not single_candidate_off_on_off:
                        if ramp_step3:
                            # Still ramping across the entire 4 s step (last 2 s moves > 20% > 2.5% threshold)
                            u = k / float(n_fr - 1)
                            ae_cur = 2.80 + (ae_v - 2.80) * u
                            eff_cur = 0.75 * ae_cur
                        elif k < 40:
                            # Early adaptation finishes in the first 1.3 s; last 2.0 s is flat and settled
                            u = k / 40.0
                            ae_cur = 2.80 + (ae_v - 2.80) * u
                            eff_cur = 0.75 * ae_cur
                    if ramp_base_end and idx == len(steps_meta) - 1:
                        u = k / float(n_fr - 1)
                        ae_cur = 4.20 + (ae_v - 4.20) * u
                        eff_cur = 0.75 * ae_cur
                    t_rows.append(
                        json.dumps(
                            {
                                "frame": fr,
                                "qpcMs": round(qpc_ms, 3),
                                "frameTimeMs": 33.333,
                                "rayTraced": True,
                                "autoExposure": ae_cur,
                                "effectiveExposure": eff_cur,
                            }
                        )
                    )
            with open(os.path.join(cdir, "spot.json"), "w", encoding="utf-8") as f:
                json.dump({"name": "con_custodial", "windows": wins}, f)
            with open(os.path.join(cdir, "telemetry.jsonl"), "w", encoding="utf-8") as f:
                f.write("\n".join(t_rows) + "\n")
            cam_x = 25.0 if far_cam else 10.5
            with open(os.path.join(cdir, "proxy_frames.csv"), "w", encoding="utf-8") as f:
                f.write("frame,dt,draws,prims,tex,lights,pub\n")
                for fr in range(100, 100 + len(steps_meta) * (n_fr + 10)):
                    f.write(f"{fr},16.6,100,1000,10,5,c:0:0:{cam_x}:20.0:30.0:1.0:0.0:0.0\n")
            return os.path.join(tmp, case_name)

        live_ok = score_variants_sweep(_build_live_case("live_ok"), [live_ref])
        assert live_ok["mode"] == "live_steps" and live_ok["causal_verdict"] == "exposure_confirmed", live_ok
        assert live_ok["sweep_design"] == "multi_variant_screening", live_ok
        assert live_ok["passing_variants"] == ["live_step2", "live_step3"], live_ok
        assert live_ok["diagnostic_isolation_variants"] == ["live_step2"], live_ok
        assert live_ok["cause_narrowing_only_variants"] == ["live_step2", "live_step3"], live_ok
        assert live_ok["candidate_fixes_needing_off_on_off"] == ["live_step2", "live_step3"], live_ok
        assert live_ok["confirmed_fixes"] == [], live_ok
        assert live_ok["variants"]["live_step3"]["exposure_settled"] is True, live_ok["variants"]["live_step3"]

        # Step whose exposure is still ramping over last 2 s -> NOT_MEASURED ("exposure not settled")
        live_ramp = score_live_steps(_build_live_case("live_ramp", ramp_step3=True), [live_ref])
        assert live_ramp["variants"]["live_step3"]["exposure_settled"] is False, live_ramp["variants"]["live_step3"]
        assert live_ramp["variants"]["live_step3"]["measurement_status"] == "NOT_MEASURED", live_ramp
        assert live_ramp["variants"]["live_step3"]["reasons"] == ["exposure not settled"], live_ramp
        # Flat step in the same run is still scored
        assert live_ramp["variants"]["live_step2"]["measurement_status"] == "MEASURED", live_ramp

        # Unsettled base_end invalidates anchor -> NOT_MEASURED ("exposure not settled")
        live_base_ramp = score_live_steps(_build_live_case("live_base_ramp", ramp_base_end=True), [live_ref])
        assert live_base_ramp["variants"]["live_step4"]["measurement_status"] == "NOT_MEASURED", live_base_ramp
        assert live_base_ramp["variants"]["live_step4"]["reasons"] == ["exposure not settled"], live_base_ramp
        assert live_base_ramp["variants"]["live_step2"]["measurement_status"] == "NOT_MEASURED", live_base_ramp
        assert live_base_ramp["variants"]["live_step2"]["reasons"] == ["exposure not settled"], live_base_ramp
        assert live_base_ramp["causal_verdict"] == "NOT_MEASURED", live_base_ramp

        # Dedicated 3-step off / on / off launch confirms a settled non-diagnostic candidate fix
        live_3step = score_live_steps(
            _build_live_case("live_3step", single_candidate_off_on_off=True), [live_ref]
        )
        assert live_3step["sweep_design"] == "dedicated_off_on_off", live_3step
        assert live_3step["cause_narrowing_only_variants"] == [], live_3step
        assert live_3step["candidate_fixes_needing_off_on_off"] == [], live_3step
        assert live_3step["confirmed_fixes"] == ["live_step2"], live_3step

        live_stuck = score_live_steps(_build_live_case("live_stuck", stuck_rtx=True), [live_ref])
        assert live_stuck["variants"]["live_step2"]["measurement_status"] == "NOT_MEASURED", live_stuck
        assert live_stuck["variants"]["live_step2"]["reasons"] == ["live_rtx_did_not_take"], live_stuck

        live_slip = score_live_steps(_build_live_case("live_slip", move_depth=True), [live_ref])
        assert live_slip["variants"]["live_step3"]["measurement_status"] == "NOT_MEASURED", live_slip
        assert live_slip["variants"]["live_step3"]["reasons"] == ["intra_launch_pose_moved"], live_slip

        live_far = score_live_steps(_build_live_case("live_far", far_cam=True), [live_ref])
        assert live_far["reference_placed"] is False and live_far["causal_verdict"] == "NOT_MEASURED", live_far
        assert live_far["variants"]["live_step2"]["reasons"][0].startswith("reference_pose_mismatch"), live_far

    print("exposure_vs_lights.py: ALL SELF-TESTS PASSED")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true", help="Run synthetic unit tests and exit.")
    ap.add_argument("--run", action="append", default=[], help="Saved Remix gate run folder(s) to analyze.")
    ap.add_argument("--score-sweep", default=None, help="Score a verify_runtime.ps1 -Variants output directory.")
    ap.add_argument("--score-live", default=None, help="Score a single-launch liveSteps + liveStepDump folder.")
    ap.add_argument("--reference", action="append", default=[], help="No-Remix reference run folder(s).")
    ap.add_argument("--spot", default=None, help="Optional spot name override for --score-sweep / --score-live.")
    ap.add_argument("--out", default=None, help="Optional path to write JSON output.")
    args = ap.parse_args()

    if args.self_test:
        run_self_test()
        return 0

    if args.score_live:
        if not args.reference:
            ap.error("--score-live requires at least one --reference <ref_dir>")
        result = score_live_steps(args.score_live, args.reference, spot_override=args.spot)
    elif args.score_sweep:
        if not args.reference:
            ap.error("--score-sweep requires at least one --reference <ref_dir>")
        result = score_variants_sweep(args.score_sweep, args.reference, spot_override=args.spot)
    elif args.run:
        if not args.reference:
            ap.error("--run requires at least one --reference <ref_dir>")
        result = analyze_gate_runs(args.run, args.reference)
    else:
        ap.error("Specify --self-test, --run, --score-sweep, or --score-live")
        return 2

    text = json.dumps(result, indent=2)
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
