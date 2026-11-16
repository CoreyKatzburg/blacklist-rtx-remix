#!/usr/bin/env python3
"""Separate automatic-exposure / tone-mapping lift from published-light strength,
and score Worker D's -Variants exposure sweeps against no-Remix reference runs.

Modes:
  python exposure_vs_lights.py --self-test
  python exposure_vs_lights.py --run <gate_run_dir> [--run <gate_run_dir2> ...] --reference <ref_dir> [--reference <ref_dir2> ...]
  python exposure_vs_lights.py --score-sweep <settings_run_dir> --reference <ref_dir> [--reference <ref_dir2> ...]

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


def analyze_gate_runs(
    run_dirs: list[str],
    reference_dirs: list[str],
    max_pose_error_deg: float = PASS_THRESHOLDS["max_pose_error_deg"],
) -> dict[str, Any]:
    """Runs across-stop elasticity + drift-gated switch analysis across saved Remix gate runs."""
    spots_out: dict[str, Any] = {}
    pooled_rx: list[float] = []
    pooled_fx: list[float] = []

    for run_dir in run_dirs:
        run_name = os.path.basename(os.path.normpath(run_dir))
        summary = pb.summarize_gate_run(
            run_dir, reference_dirs=reference_dirs, max_pose_error_deg=max_pose_error_deg
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

            y0_entry = next((s for s in sp.get("stops", []) if s.get("tag") == "y0_p0"), None)
            spots_out[key] = {
                "run": run_name,
                "spot": spot_name,
                "ray_traced": spot_rt,
                "gate_status": sp.get("status"),
                "gate_reasons": sp.get("reasons", []),
                "n_on_pose": sp.get("n_on_pose", 0),
                "off_pose_stops": sp.get("off_pose_stops", []),
                "elasticity": fit_across_stop_elasticity(rx, fx),
                "y0_p0": {
                    "on_pose": y0_entry.get("on_pose") if y0_entry else None,
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


def _write_uniform_pfm(path: str, val: float, h: int = 32, w: int = 32) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    arr = np.full((h, w, 3), float(val), dtype=np.float32)
    gate.write_pfm(path, arr)


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

    print("exposure_vs_lights.py: ALL SELF-TESTS PASSED")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true", help="Run synthetic unit tests and exit.")
    ap.add_argument("--run", action="append", default=[], help="Saved Remix gate run folder(s) to analyze.")
    ap.add_argument("--score-sweep", default=None, help="Score a verify_runtime.ps1 -Variants output directory.")
    ap.add_argument("--reference", action="append", default=[], help="No-Remix reference run folder(s).")
    ap.add_argument("--spot", default=None, help="Optional spot name override for --score-sweep.")
    ap.add_argument("--out", default=None, help="Optional path to write JSON output.")
    args = ap.parse_args()

    if args.self_test:
        run_self_test()
        return 0

    if args.score_sweep:
        if not args.reference:
            ap.error("--score-sweep requires at least one --reference <ref_dir>")
        result = score_variants_sweep(args.score_sweep, args.reference, spot_override=args.spot)
    elif args.run:
        if not args.reference:
            ap.error("--run requires at least one --reference <ref_dir>")
        result = analyze_gate_runs(args.run, args.reference)
    else:
        ap.error("Specify --self-test, --run, or --score-sweep")
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
