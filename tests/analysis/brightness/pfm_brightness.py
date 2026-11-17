"""PFM brightness inspector and original-game (`<tag>_raster.pfm`) vs Remix (`<tag>_final.pfm`) two-sided scorer.

Unit contract (verified against `rtx_context.cpp:743-746, 3081-3150`,
`tonemapping_apply_tonemapping.comp.slang:172`, `local_tonemap/final_combine.comp.slang:112`,
and `color.slangh:96`):
  - With `ModernFilter=1` (tonemapping active), Remix writes `rtOutput.m_finalOutput` AFTER
    `dispatchToneMapping(..., performSRGBConversion=true)`, which applies `pow(linear, 1/2.2)` at
    full output resolution (2560x1440).
  - Therefore `<tag>_final.pfm` and `<tag>_raster.pfm` MUST both store display/gamma-encoded RGB
    floats in [0, 1] (`uint8_rgb / 255.0` for screen grabs). Values above 1.0 in `<tag>_final.pfm`
    are clipped to [0, 1] before scoring display brightness so HDR overshoot cannot inflate ratios.
  - HUD exclusion is applied symmetrically as a central ROI crop on BOTH images at scoring time
    (never by zero-filling `<tag>_raster.pfm`, which would collapse 10th-percentile luma to 0).

Usage:
  python -I tools/remix/tests/analysis/brightness/pfm_brightness.py --self-test
  python -I tools/remix/tests/analysis/brightness/pfm_brightness.py --reference <ref_run_dir> <gate_run_dir>
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import re
import sys
import tempfile

import numpy as np

RUNTIME_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "runtime"))
if RUNTIME_DIR not in sys.path:
    sys.path.insert(0, RUNTIME_DIR)
import gate  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
NOT_MEASURED = "NOT_MEASURED"

REMIX_GAMMA = 2.2
# Central ROI crop excluding outer 8% on each edge where Blacklist draws goggles/weapon/radar HUD.
DEFAULT_HUD_MARGIN_Y = 0.08
DEFAULT_HUD_MARGIN_X = 0.08
NEAR_BLACK_GAMMA = 0.05           # ~13/255 in display gamma (~0.0014 linear)
NEAR_WHITE_GAMMA = 230.0 / 255.0  # >= 230/255 BT.709 luma (matches cinematic.csv nearWhite)
FULL_WHITE_GAMMA = 250.0 / 255.0  # >= 250/255 BT.709 luma (matches cinematic.csv fullWhite)
PURE_WHITE_GAMMA = 0.99           # all 3 RGB channels >= 0.99 (~253/255)
MIN_RASTER_LUMA = 0.02            # below this (~5/255), raster is near-black so a ratio is undefined
DEFAULT_MAX_POSE_ERROR_DEG = 1.0  # stops > 1.0 deg from target pose are excluded as off-pose

DEFAULT_THRESHOLDS = {
    "min_scene_luma": 0.03,
    "min_raster_luma_ratio": 0.50,
    "max_raster_luma_ratio": 1.85,
    "min_spot_median_ratio": 0.60,
    "max_spot_median_ratio": 1.50,
    "max_near_white_delta": 0.10,
    "max_cinematic_luma_ratio": 1.50,
    "min_cinematic_luma_ratio": 0.60,
    "max_cinematic_near_white_delta": 0.10,
}

_TAG_POSE_RE = re.compile(r"^y(-?\d+(?:\.\d+)?)_p(-?\d+(?:\.\d+)?)(?:_after)?$")


def wrap_deg(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def angular_error_deg(pose_a: tuple[float, float], pose_b: tuple[float, float]) -> float:
    y1, p1 = math.radians(float(pose_a[0])), math.radians(float(pose_a[1]))
    y2, p2 = math.radians(float(pose_b[0])), math.radians(float(pose_b[1]))
    dot = math.cos(p1) * math.cos(p2) * math.cos(y1 - y2) + math.sin(p1) * math.sin(p2)
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def parse_tag_pose(tag: str) -> tuple[float, float] | None:
    m = _TAG_POSE_RE.match(tag)
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def crop_hud_roi(img: np.ndarray, margin_y: float = DEFAULT_HUD_MARGIN_Y,
                 margin_x: float = DEFAULT_HUD_MARGIN_X) -> np.ndarray:
    """Returns central interior view of (H, W, ...) excluding HUD border bands symmetrically."""
    h, w = img.shape[:2]
    y0, y1 = int(round(h * margin_y)), int(round(h * (1.0 - margin_y)))
    x0, x1 = int(round(w * margin_x)), int(round(w * (1.0 - margin_x)))
    if y1 <= y0 or x1 <= x0:
        return img
    return img[y0:y1, x0:x1]


def bt709_luma(rgb: np.ndarray) -> np.ndarray:
    return 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]


def image_brightness_stats(img: np.ndarray, mask_hud: bool = True) -> dict[str, float | int]:
    """Computes display-gamma [0, 1] and linear-decoded (pow 2.2) brightness statistics on a 3-channel PFM."""
    full_nonfinite = int((~np.isfinite(img).all(axis=2)).sum())
    roi = crop_hud_roi(img) if mask_hud else img
    finite = np.isfinite(roi).all(axis=2)
    roi_nonfinite = int((~finite).sum())
    if not finite.any():
        return {
            "nonfinite": full_nonfinite,
            "roi_nonfinite": roi_nonfinite,
            "over_one_share": float("nan"),
            "gamma_mean": float("nan"),
            "gamma_p10": float("nan"),
            "gamma_p50": float("nan"),
            "gamma_p90": float("nan"),
            "gamma_p99": float("nan"),
            "linear_mean": float("nan"),
            "linear_p10": float("nan"),
            "linear_p50": float("nan"),
            "near_black_share": float("nan"),
            "near_white_share": float("nan"),
            "full_white_share": float("nan"),
            "pure_white_share": float("nan"),
        }
    raw_valid = roi[finite]
    over_one = (raw_valid.max(axis=1) > 1.0)
    # Clip to display [0, 1] so unclipped HDR values (> 1.0) cannot inflate display luma or ratios.
    safe = np.clip(raw_valid, 0.0, 1.0)
    g_lum = bt709_luma(safe)
    lin_rgb = np.power(safe, np.float32(REMIX_GAMMA))
    l_lum = bt709_luma(lin_rgb)
    near_black = (g_lum <= NEAR_BLACK_GAMMA)
    near_white = (g_lum >= NEAR_WHITE_GAMMA)
    full_white = (g_lum >= FULL_WHITE_GAMMA)
    # All 3 RGB channels >= PURE_WHITE_GAMMA (achromatic pure white).
    pure_white = (safe.min(axis=1) >= PURE_WHITE_GAMMA)
    return {
        "nonfinite": full_nonfinite,
        "roi_nonfinite": roi_nonfinite,
        "over_one_share": float(over_one.mean()),
        "gamma_mean": float(g_lum.mean()),
        "gamma_p10": float(np.percentile(g_lum, 10)),
        "gamma_p50": float(np.percentile(g_lum, 50)),
        "gamma_p90": float(np.percentile(g_lum, 90)),
        "gamma_p99": float(np.percentile(g_lum, 99)),
        "linear_mean": float(l_lum.mean()),
        "linear_p10": float(np.percentile(l_lum, 10)),
        "linear_p50": float(np.percentile(l_lum, 50)),
        "near_black_share": float(near_black.mean()),
        "near_white_share": float(near_white.mean()),
        "full_white_share": float(full_white.mean()),
        "pure_white_share": float(pure_white.mean()),
    }


def compare_final_to_raster(final_img: np.ndarray, raster_img: np.ndarray,
                            mask_hud: bool = True,
                            min_raster_luma: float = MIN_RASTER_LUMA) -> dict[str, float | int | bool | None]:
    """Compares Remix `<tag>_final.pfm` against original-game `<tag>_raster.pfm` in matched [0, 1] display units."""
    if final_img.ndim != 3 or raster_img.ndim != 3 or final_img.shape[2] != 3 or raster_img.shape[2] != 3:
        raise ValueError(f"expected 3-channel images, got final {final_img.shape} vs raster {raster_img.shape}")
    fh, fw = final_img.shape[:2]
    rh, rw = raster_img.shape[:2]
    if abs((fw / max(fh, 1)) - (rw / max(rh, 1))) > 0.05:
        raise ValueError(f"aspect mismatch: final {final_img.shape} vs raster {raster_img.shape}")
    fs = image_brightness_stats(final_img, mask_hud=mask_hud)
    rs = image_brightness_stats(raster_img, mask_hud=mask_hud)
    rm = float(rs["gamma_mean"])
    rp10 = float(rs["gamma_p10"])
    rp50 = float(rs["gamma_p50"])
    rlin = float(rs["linear_mean"])
    min_lin = min_raster_luma ** REMIX_GAMMA
    black_raster = (not math.isfinite(rm)) or (rm < min_raster_luma)
    return {
        "final_nonfinite": int(fs["nonfinite"]),
        "raster_nonfinite": int(rs["nonfinite"]),
        "nonfinite_pixels": int(fs["nonfinite"]) + int(rs["nonfinite"]),
        "final_over_one_share": float(fs["over_one_share"]),
        "black_raster": black_raster,
        "final_gamma_mean": float(fs["gamma_mean"]),
        "raster_gamma_mean": rm,
        "gamma_mean_delta": float(fs["gamma_mean"]) - rm if math.isfinite(rm) else None,
        "gamma_mean_ratio": None if black_raster else float(fs["gamma_mean"]) / rm,
        "gamma_p10_ratio": None if (not math.isfinite(rp10) or rp10 < min_raster_luma) else float(fs["gamma_p10"]) / rp10,
        "gamma_p50_ratio": None if (not math.isfinite(rp50) or rp50 < min_raster_luma) else float(fs["gamma_p50"]) / rp50,
        "linear_mean_ratio": None if (not math.isfinite(rlin) or rlin < min_lin) else float(fs["linear_mean"]) / rlin,
        "near_black_delta": float(fs["near_black_share"]) - float(rs["near_black_share"]),
        "near_white_delta": float(fs["near_white_share"]) - float(rs["near_white_share"]),
        "pure_white_delta": float(fs["pure_white_share"]) - float(rs["pure_white_share"]),
        "final_near_black": float(fs["near_black_share"]),
        "raster_near_black": float(rs["near_black_share"]),
        "final_near_white": float(fs["near_white_share"]),
        "raster_near_white": float(rs["near_white_share"]),
        "final_pure_white": float(fs["pure_white_share"]),
        "raster_pure_white": float(rs["pure_white_share"]),
    }


def _load_spot_windows(spot_dir: str) -> dict[str, dict]:
    sp = os.path.join(spot_dir, "spot.json")
    if not os.path.exists(sp):
        return {}
    try:
        with open(sp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, dict] = {}
    for w in data.get("windows", []):
        if w.get("kind") == "still":
            key = w.get("dump") or w.get("name")
            if key:
                out[str(key)] = w
    return out


def _window_pose_error_deg(tag: str, win: dict | None, raster_json_path: str | None = None) -> float | None:
    """Returns worst angular error (deg) from target (yaw, pitch) for a stop, or None if no pose metadata exists."""
    target = None
    if win is not None and "yaw" in win and "pitch" in win:
        target = (float(win["yaw"]), float(win["pitch"]))
    if target is None:
        target = parse_tag_pose(tag)
    if target is None:
        return None

    errs: list[float] = []
    if win is not None and isinstance(win.get("gamePose"), list):
        for p in win["gamePose"]:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                errs.append(angular_error_deg((float(p[0]), float(p[1])), target))
    if raster_json_path and os.path.exists(raster_json_path):
        try:
            with open(raster_json_path, "r", encoding="utf-8") as f:
                rj = json.load(f)
            rp = rj.get("relPose")
            if isinstance(rp, (list, tuple)) and len(rp) >= 2:
                errs.append(angular_error_deg((float(rp[0]), float(rp[1])), target))
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            pass
    return max(errs) if errs else None


def _find_raster_for_stop(spot_dir: str, spot_name: str, tag: str,
                          reference_dirs: list[str]) -> tuple[str | None, str | None, str | None]:
    """Returns (raster_pfm_path, raster_json_path, ref_spot_dir) from local spot_dir or first matching reference_dir."""
    local_pfm = os.path.join(spot_dir, "gate", f"{tag}_raster.pfm")
    if os.path.exists(local_pfm):
        local_json = os.path.join(spot_dir, "gate", f"{tag}_raster.json")
        return local_pfm, (local_json if os.path.exists(local_json) else None), spot_dir
    for ref_root in reference_dirs:
        cand_spot = os.path.join(ref_root, spot_name)
        cand_pfm = os.path.join(cand_spot, "gate", f"{tag}_raster.pfm")
        if os.path.exists(cand_pfm):
            cand_json = os.path.join(cand_spot, "gate", f"{tag}_raster.json")
            return cand_pfm, (cand_json if os.path.exists(cand_json) else None), cand_spot
    return None, None, None


def _read_cinematic_csv(csv_path: str) -> dict[str, float | int] | None:
    if not os.path.exists(csv_path):
        return None
    means, nb, nw, fw = [], [], [], []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if "inCutscene" in row and row["inCutscene"] not in ("", "True", "true", "1"):
                continue
            m_key = next(
                (k for k in ("activeBrightness", "brightness", "activeMean", "mean") if k in row and row[k] != ""),
                None,
            )
            nb_key = next(
                (k for k in ("activeNearBlack", "nearBlack", "below8") if k in row and row[k] != ""),
                None,
            )
            nw_key = next(
                (k for k in ("activeNearWhite", "nearWhite", "above230") if k in row and row[k] != ""),
                None,
            )
            fw_key = next(
                (k for k in ("activeFullWhite", "fullWhite", "above250") if k in row and row[k] != ""),
                None,
            )
            if m_key is None:
                continue
            means.append(float(row[m_key]))
            if nb_key is not None:
                nb.append(float(row[nb_key]))
            if nw_key is not None:
                nw.append(float(row[nw_key]))
            if fw_key is not None:
                fw.append(float(row[fw_key]))
    if not means:
        return None
    arr_m = np.array(means, dtype=np.float64)
    arr_nb = np.array(nb, dtype=np.float64) if nb else np.zeros(1)
    arr_nw = np.array(nw, dtype=np.float64) if nw else np.zeros(1)
    arr_fw = np.array(fw, dtype=np.float64) if fw else np.zeros(1)
    return {
        "frames": int(len(means)),
        "mean_luma": float(arr_m.mean()),
        "median_luma": float(np.median(arr_m)),
        "max_luma": float(arr_m.max()),
        "near_black_mean": float(arr_nb.mean()),
        "near_white_mean": float(arr_nw.mean()),
        "near_white_median": float(np.median(arr_nw)),
        "near_white_max": float(arr_nw.max()),
        "full_white_mean": float(arr_fw.mean()),
        "full_white_max": float(arr_fw.max()),
    }


def score_cinematic_spot(spot_dir: str, spot_name: str, reference_dirs: list[str],
                         thresholds: dict[str, float] | None = None) -> dict | None:
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    run_csv = os.path.join(spot_dir, "cinematic", "cinematic.csv")
    run_stats = _read_cinematic_csv(run_csv)
    if run_stats is None:
        return None
    ref_csv = None
    ref_stats = None
    for ref_root in reference_dirs:
        cand = os.path.join(ref_root, spot_name, "cinematic", "cinematic.csv")
        st = _read_cinematic_csv(cand)
        if st is not None:
            ref_csv = cand
            ref_stats = st
            break
    if ref_stats is None:
        return {
            "status": NOT_MEASURED,
            "reasons": ["missing_reference_cinematic"],
            "run_csv": run_csv,
            "ref_csv": None,
            "run": run_stats,
            "reference": None,
        }
    rm = ref_stats["mean_luma"]
    ratio = None if rm < MIN_RASTER_LUMA else run_stats["mean_luma"] / rm
    nw_delta = run_stats["near_white_mean"] - ref_stats["near_white_mean"]
    fw_delta = run_stats["full_white_mean"] - ref_stats["full_white_mean"]
    reasons = []
    if ratio is None:
        return {
            "status": NOT_MEASURED,
            "reasons": ["black_reference_cinematic"],
            "run_csv": run_csv,
            "ref_csv": ref_csv,
            "run": run_stats,
            "reference": ref_stats,
        }
    if ratio < th["min_cinematic_luma_ratio"]:
        reasons.append(f"too_dark_vs_raster ({ratio:.2f}x < {th['min_cinematic_luma_ratio']:.2f}x)")
    if ratio > th["max_cinematic_luma_ratio"]:
        reasons.append(f"too_bright_vs_raster ({ratio:.2f}x > {th['max_cinematic_luma_ratio']:.2f}x)")
    if nw_delta > th["max_cinematic_near_white_delta"]:
        reasons.append(f"too_white_vs_raster (nearWhite +{nw_delta:.3f} > +{th['max_cinematic_near_white_delta']:.3f})")
    return {
        "status": FAIL if reasons else PASS,
        "reasons": reasons,
        "run_csv": run_csv,
        "ref_csv": ref_csv,
        "mean_luma_ratio": ratio,
        "near_white_delta": nw_delta,
        "full_white_delta": fw_delta,
        "run": run_stats,
        "reference": ref_stats,
    }


def summarize_gate_run(run_dir: str, reference_dirs: list[str] | str | None = None,
                       max_pose_error_deg: float = DEFAULT_MAX_POSE_ERROR_DEG,
                       thresholds: dict[str, float] | None = None, stop_ok=None) -> dict:
    """stop_ok(spot_dir, ref_spot_dir, tag) -> (bool, why), optional (gate.py's reference position rule): a raster
    taken from a reference run counts only when it returns True; otherwise the stop is off pose, with the reason in
    the stop's "position" field."""
    if reference_dirs is None:
        refs: list[str] = []
    elif isinstance(reference_dirs, str):
        refs = [reference_dirs]
    else:
        refs = list(reference_dirs)
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}

    spots_out = {}
    for spot_dir in sorted(glob.glob(os.path.join(run_dir, "*"))):
        if not os.path.isdir(spot_dir):
            continue
        spot_name = os.path.basename(spot_dir)
        if spot_name == "save-backup":
            continue
        gate_dir = os.path.join(spot_dir, "gate")
        finals = sorted(glob.glob(os.path.join(gate_dir, "*_final.pfm"))) if os.path.isdir(gate_dir) else []
        cin = score_cinematic_spot(spot_dir, spot_name, refs, thresholds=th)
        if not finals and cin is None:
            continue

        run_wins = _load_spot_windows(spot_dir)
        ref_wins_cache: dict[str, dict[str, dict]] = {}
        stops = []
        for fp in finals:
            tag = os.path.basename(fp)[:-len("_final.pfm")]
            f_img = gate.read_pfm(fp)
            st = image_brightness_stats(f_img, mask_hud=True)
            is_switch = tag.startswith("switch_")
            run_pose_err = _window_pose_error_deg(tag, run_wins.get(tag), None)
            entry: dict = {
                "tag": tag,
                "is_switch": is_switch,
                "shape": list(f_img.shape),
                "run_pose_error_deg": run_pose_err,
                **st,
            }
            if not is_switch:
                rp, rjson, ref_spot_dir = _find_raster_for_stop(spot_dir, spot_name, tag, refs)
                if rp is not None and ref_spot_dir is not None:
                    if ref_spot_dir not in ref_wins_cache:
                        ref_wins_cache[ref_spot_dir] = (
                            run_wins if os.path.abspath(ref_spot_dir) == os.path.abspath(spot_dir)
                            else _load_spot_windows(ref_spot_dir)
                        )
                    ref_pose_err = _window_pose_error_deg(tag, ref_wins_cache[ref_spot_dir].get(tag), rjson)
                    pose_errs = [e for e in (run_pose_err, ref_pose_err) if e is not None]
                    worst_pose_err = max(pose_errs) if pose_errs else None
                    on_pose = (worst_pose_err is None) or (worst_pose_err <= max_pose_error_deg)
                    if stop_ok is not None and os.path.abspath(ref_spot_dir) != os.path.abspath(spot_dir):
                        placed, entry["position"] = stop_ok(spot_dir, ref_spot_dir, tag)
                        on_pose = on_pose and placed
                    r_img = gate.read_pfm(rp)
                    cmp = compare_final_to_raster(f_img, r_img, mask_hud=True)
                    entry["raster_path"] = rp
                    entry["ref_pose_error_deg"] = ref_pose_err
                    entry["pose_error_deg"] = worst_pose_err
                    entry["on_pose"] = on_pose
                    entry["vs_raster"] = cmp
                else:
                    entry["raster_path"] = None
                    entry["ref_pose_error_deg"] = None
                    entry["pose_error_deg"] = run_pose_err
                    entry["on_pose"] = None
            stops.append(entry)

        sweep = [s for s in stops if not s["is_switch"]]
        switches = [s for s in stops if s["is_switch"]]
        with_raster = [s for s in sweep if "vs_raster" in s]
        missing_raster_stops = [s["tag"] for s in sweep if "vs_raster" not in s]
        off_pose_stops = [s["tag"] for s in with_raster if s.get("on_pose") is False]
        on_pose_entries = [s for s in with_raster if s.get("on_pose") is not False]
        black_raster_stops = [s["tag"] for s in on_pose_entries if s["vs_raster"]["black_raster"]]
        valid_entries = [
            s for s in on_pose_entries
            if not s["vs_raster"]["black_raster"] and s["vs_raster"]["gamma_mean_ratio"] is not None
        ]
        nonfinite_total = sum(int(s["nonfinite"]) for s in sweep) + sum(
            int(s["vs_raster"]["raster_nonfinite"]) for s in with_raster
        )

        # Evaluate spot brightness status against reference raster stops.
        reasons: list[str] = []
        too_dark_stops: list[str] = []
        too_bright_stops: list[str] = []
        too_white_stops: list[str] = []
        if not sweep:
            spot_status = cin["status"] if cin is not None else NOT_MEASURED
            if cin is not None:
                reasons.extend(cin["reasons"])
        elif nonfinite_total > 0:
            spot_status = FAIL
            reasons.append(f"nonfinite_pixels ({nonfinite_total})")
        elif not with_raster:
            spot_status = NOT_MEASURED
            reasons.append("missing_reference")
        elif not on_pose_entries:
            spot_status = NOT_MEASURED
            reasons.append("no_on_pose_stops")
        elif not valid_entries:
            spot_status = NOT_MEASURED
            reasons.append("black_raster")
        else:
            med_r = float(np.median([float(s["vs_raster"]["gamma_mean_ratio"]) for s in valid_entries]))
            for s in valid_entries:
                tag = s["tag"]
                vr = s["vs_raster"]
                r = float(vr["gamma_mean_ratio"])
                nwd = float(vr["near_white_delta"])
                if s["gamma_mean"] < th["min_scene_luma"] or r < th["min_raster_luma_ratio"]:
                    too_dark_stops.append(f"{tag}:{r:.2f}x(luma={s['gamma_mean']:.3f})")
                if r > th["max_raster_luma_ratio"]:
                    too_bright_stops.append(f"{tag}:{r:.2f}x")
                if nwd > th["max_near_white_delta"]:
                    too_white_stops.append(f"{tag}:+{nwd:.3f}")
            if too_dark_stops or med_r < th["min_spot_median_ratio"]:
                reasons.append(
                    f"too_dark_vs_raster (median={med_r:.2f}x"
                    + (": " + ", ".join(too_dark_stops[:5]) if too_dark_stops else "")
                    + ")"
                )
            if too_bright_stops or med_r > th["max_spot_median_ratio"]:
                reasons.append(
                    f"too_bright_vs_raster (median={med_r:.2f}x"
                    + (": " + ", ".join(too_bright_stops[:5]) if too_bright_stops else "")
                    + ")"
                )
            if too_white_stops:
                reasons.append("too_white_vs_raster: " + ", ".join(too_white_stops[:5]))
            if cin is not None and cin["status"] == FAIL:
                reasons.extend("cinematic_" + r for r in cin["reasons"])
            if reasons:
                spot_status = FAIL
            elif cin is not None and cin["status"] == NOT_MEASURED:
                spot_status = NOT_MEASURED
                reasons.extend(cin["reasons"])
            else:
                spot_status = PASS

        ratios = [float(s["vs_raster"]["gamma_mean_ratio"]) for s in valid_entries]
        lin_ratios = [
            float(s["vs_raster"]["linear_mean_ratio"]) for s in valid_entries
            if s["vs_raster"]["linear_mean_ratio"] is not None
        ]
        nw_deltas = [float(s["vs_raster"]["near_white_delta"]) for s in valid_entries]

        spots_out[spot_name] = {
            "status": spot_status,
            "reasons": reasons,
            "n_sweep": len(sweep),
            "n_switch": len(switches),
            "n_with_raster": len(with_raster),
            "n_on_pose": len(on_pose_entries),
            "n_valid_compared": len(valid_entries),
            "on_pose_stops": [s["tag"] for s in valid_entries],
            "off_pose_stops": off_pose_stops,
            "black_raster_stops": black_raster_stops,
            "missing_raster_stops": missing_raster_stops,
            "nonfinite_pixels": nonfinite_total,
            "sweep_gamma_mean_min": min((float(s["gamma_mean"]) for s in sweep), default=None),
            "sweep_gamma_mean_median": float(np.median([s["gamma_mean"] for s in sweep])) if sweep else None,
            "sweep_gamma_mean_max": max((float(s["gamma_mean"]) for s in sweep), default=None),
            "sweep_near_black_max": max((float(s["near_black_share"]) for s in sweep), default=None),
            "sweep_near_white_max": max((float(s["near_white_share"]) for s in sweep), default=None),
            "sweep_pure_white_max": max((float(s["pure_white_share"]) for s in sweep), default=None),
            "raster_gamma_ratio_min": min(ratios) if ratios else None,
            "raster_gamma_ratio_median": float(np.median(ratios)) if ratios else None,
            "raster_gamma_ratio_max": max(ratios) if ratios else None,
            "raster_linear_ratio_median": float(np.median(lin_ratios)) if lin_ratios else None,
            "near_white_delta_max": max(nw_deltas) if nw_deltas else None,
            "too_dark_stops": too_dark_stops,
            "too_bright_stops": too_bright_stops,
            "too_white_stops": too_white_stops,
            "cinematic": cin,
            "stops": stops,
        }

    statuses = [v["status"] for v in spots_out.values()]
    if not statuses or any(s == NOT_MEASURED for s in statuses):
        run_status = FAIL if any(s == FAIL for s in statuses) else NOT_MEASURED
    elif any(s == FAIL for s in statuses):
        run_status = FAIL
    else:
        run_status = PASS
    return {
        "run_dir": run_dir,
        "reference_dirs": refs,
        "max_pose_error_deg": max_pose_error_deg,
        "thresholds": th,
        "status": run_status,
        "spots": spots_out,
    }


def _make_fake_run(root: str, spot: str, finals: dict[str, np.ndarray],
                   rasters: dict[str, tuple[np.ndarray, tuple[float, float] | None]] | None = None) -> str:
    gate_dir = os.path.join(root, spot, "gate")
    os.makedirs(gate_dir, exist_ok=True)
    for tag, img in finals.items():
        gate.write_pfm(os.path.join(gate_dir, f"{tag}_final.pfm"), img.astype(np.float32))
    if rasters:
        for tag, (r_img, rel_pose) in rasters.items():
            gate.write_pfm(os.path.join(gate_dir, f"{tag}_raster.pfm"), r_img.astype(np.float32))
            if rel_pose is not None:
                with open(os.path.join(gate_dir, f"{tag}_raster.json"), "w", encoding="utf-8") as f:
                    json.dump({"tag": tag, "method": "no_remix", "relPose": [rel_pose[0], rel_pose[1]]}, f)
    return root


def run_self_test() -> None:
    # 1. Prove why mismatched sRGB-decoded raster vs gamma-encoded final causes a ~2.3x bug.
    u8 = np.full((64, 96, 3), 128, dtype=np.uint8)
    gamma_img = u8.astype(np.float32) / 255.0
    wrong_linear_raster = np.power(gamma_img, REMIX_GAMMA)
    matched = compare_final_to_raster(gamma_img, gamma_img)
    mismatched = compare_final_to_raster(gamma_img, wrong_linear_raster)
    assert abs(float(matched["gamma_mean_ratio"]) - 1.0) < 1e-6, matched
    assert float(mismatched["gamma_mean_ratio"]) > 2.2, f"expected >2.2x unit bug, got {mismatched}"

    # 2. Prove symmetric HUD crop ignores HUD border differences without collapsing p10 to 0.
    raster_with_hud = gamma_img.copy()
    raster_with_hud[:4, :, :] = 1.0
    raster_with_hud[-4:, :, :] = 0.0
    cropped = compare_final_to_raster(gamma_img, raster_with_hud, mask_hud=True)
    assert abs(float(cropped["gamma_mean_ratio"]) - 1.0) < 1e-6, cropped
    assert abs(float(cropped["gamma_p10_ratio"]) - 1.0) < 1e-6, cropped

    # 3. Reviewer edge cases:
    # 3a. Unclipped HDR final (3.0) against white raster (1.0) clips to [0, 1] and scores 1.0x (not 3.0x).
    hdr_final = np.full((64, 96, 3), 3.0, dtype=np.float32)
    white_raster = np.full((64, 96, 3), 1.0, dtype=np.float32)
    hdr_cmp = compare_final_to_raster(hdr_final, white_raster)
    assert abs(float(hdr_cmp["gamma_mean_ratio"]) - 1.0) < 1e-6, hdr_cmp
    assert abs(float(hdr_cmp["final_over_one_share"]) - 1.0) < 1e-6, hdr_cmp

    # 3b. Black raster (0.0) returns black_raster=True and gamma_mean_ratio=None (never 4000x).
    black_raster = np.zeros((64, 96, 3), dtype=np.float32)
    black_cmp = compare_final_to_raster(gamma_img, black_raster)
    assert black_cmp["black_raster"] is True and black_cmp["gamma_mean_ratio"] is None, black_cmp

    # 3c. Non-finite pixels are counted and fail the spot check.
    nan_final = gamma_img.copy()
    nan_final[20, 30, 0] = np.nan
    nan_cmp = compare_final_to_raster(nan_final, gamma_img)
    assert nan_cmp["final_nonfinite"] == 1 and nan_cmp["nonfinite_pixels"] == 1, nan_cmp

    # 4. End-to-end fake run checks: missing_reference -> NOT_MEASURED, plus too_dark_vs_raster,
    #    too_bright_vs_raster, too_white_vs_raster, black_raster -> NOT_MEASURED, nonfinite -> FAIL,
    #    and off-pose reference stop exclusion.
    with tempfile.TemporaryDirectory() as tmp:
        ref_dir = _make_fake_run(
            os.path.join(tmp, "ref"),
            "spot_a",
            finals={},
            rasters={
                "y0_p0": (np.full((64, 96, 3), 0.25, np.float32), (0.1, 0.0)),
                "y45_p0": (np.full((64, 96, 3), 0.80, np.float32), (52.0, 0.0)),  # 7 deg off-pose: must be excluded
            },
        )
        # Missing reference -> NOT_MEASURED
        good_run = _make_fake_run(
            os.path.join(tmp, "good"),
            "spot_a",
            finals={
                "y0_p0": np.full((64, 96, 3), 0.26, np.float32),
                "y45_p0": np.full((64, 96, 3), 0.26, np.float32),
            },
        )
        no_ref_res = summarize_gate_run(good_run)
        assert no_ref_res["spots"]["spot_a"]["status"] == NOT_MEASURED, no_ref_res
        assert "missing_reference" in no_ref_res["spots"]["spot_a"]["reasons"], no_ref_res

        # With --reference: y0_p0 (1.04x) passes and off-pose y45_p0 is excluded!
        ok_res = summarize_gate_run(good_run, reference_dirs=[ref_dir])
        sp_ok = ok_res["spots"]["spot_a"]
        assert sp_ok["status"] == PASS, sp_ok
        assert sp_ok["on_pose_stops"] == ["y0_p0"] and sp_ok["off_pose_stops"] == ["y45_p0"], sp_ok

        # Fake case: too_dark_vs_raster (0.08 vs 0.25 = 0.32x < 0.55x -> FAIL)
        dark_run = _make_fake_run(
            os.path.join(tmp, "too_dark_vs_raster"),
            "spot_a",
            finals={"y0_p0": np.full((64, 96, 3), 0.08, np.float32)},
        )
        dark_res = summarize_gate_run(dark_run, reference_dirs=[ref_dir])
        assert dark_res["spots"]["spot_a"]["status"] == FAIL, dark_res
        assert any("too_dark_vs_raster" in r for r in dark_res["spots"]["spot_a"]["reasons"]), dark_res

        # Fake case: too_bright_vs_raster (0.50 vs 0.25 = 2.00x > 1.65x -> FAIL)
        bright_run = _make_fake_run(
            os.path.join(tmp, "too_bright_vs_raster"),
            "spot_a",
            finals={"y0_p0": np.full((64, 96, 3), 0.50, np.float32)},
        )
        bright_res = summarize_gate_run(bright_run, reference_dirs=[ref_dir])
        assert bright_res["spots"]["spot_a"]["status"] == FAIL, bright_res
        assert any("too_bright_vs_raster" in r for r in bright_res["spots"]["spot_a"]["reasons"]), bright_res

        # Fake case: too_white_vs_raster (25% near-white slab with modest mean 0.34 -> 1.36x mean ok, nearWhite fails)
        white_slab = np.full((64, 96, 3), 0.12, np.float32)
        white_slab[15:35, 20:70, :] = 0.96
        white_run = _make_fake_run(
            os.path.join(tmp, "too_white_vs_raster"),
            "spot_a",
            finals={"y0_p0": white_slab},
        )
        white_res = summarize_gate_run(white_run, reference_dirs=[ref_dir])
        assert white_res["spots"]["spot_a"]["status"] == FAIL, white_res
        assert any("too_white_vs_raster" in r for r in white_res["spots"]["spot_a"]["reasons"]), white_res

        # Fake case: black_raster -> NOT_MEASURED (never 4000x)
        black_ref = _make_fake_run(
            os.path.join(tmp, "black_ref"),
            "spot_a",
            finals={},
            rasters={"y0_p0": (black_raster, (0.0, 0.0))},
        )
        black_res = summarize_gate_run(good_run, reference_dirs=[black_ref])
        assert black_res["spots"]["spot_a"]["status"] == NOT_MEASURED, black_res
        assert "black_raster" in black_res["spots"]["spot_a"]["reasons"], black_res

        # Fake case: nonfinite_pixels -> FAIL with count surfaced
        nan_run = _make_fake_run(
            os.path.join(tmp, "nan_run"),
            "spot_a",
            finals={"y0_p0": nan_final},
        )
        nan_res = summarize_gate_run(nan_run, reference_dirs=[ref_dir])
        assert nan_res["spots"]["spot_a"]["status"] == FAIL, nan_res
        assert nan_res["spots"]["spot_a"]["nonfinite_pixels"] == 1, nan_res

        # Fake case: cinematic whiteout vs normal cinematic (using real column names brightness/nearWhite/inCutscene)
        cin_ref_dir = os.path.join(tmp, "cin_ref", "ben_interrogation", "cinematic")
        os.makedirs(cin_ref_dir, exist_ok=True)
        with open(os.path.join(cin_ref_dir, "cinematic.csv"), "w", encoding="utf-8") as f:
            f.write("shot,brightness,nearBlack,nearWhite,fullWhite,inCutscene,activeBrightness,activeNearWhite\n")
            for k in range(10):
                f.write(f"{k},0.150,0.215,0.012,0.007,True,0.150,0.012\n")
        cin_bad_dir = os.path.join(tmp, "cin_bad", "ben_interrogation", "cinematic")
        os.makedirs(cin_bad_dir, exist_ok=True)
        with open(os.path.join(cin_bad_dir, "cinematic.csv"), "w", encoding="utf-8") as f:
            f.write("shot,brightness,nearBlack,nearWhite,fullWhite,inCutscene\n")
            f.write("0,0.180,0.300,0.010,0.005,False\n")  # outside cutscene: ignored
            for k in range(1, 11):
                f.write(f"{k},0.666,0.097,0.370,0.270,True\n")
        cin_bad_res = summarize_gate_run(os.path.join(tmp, "cin_bad"), reference_dirs=[os.path.join(tmp, "cin_ref")])
        assert cin_bad_res["spots"]["ben_interrogation"]["status"] == FAIL, cin_bad_res
        cin_ok_dir = os.path.join(tmp, "cin_ok", "ben_interrogation", "cinematic")
        os.makedirs(cin_ok_dir, exist_ok=True)
        with open(os.path.join(cin_ok_dir, "cinematic.csv"), "w", encoding="utf-8") as f:
            f.write("shot,brightness,nearBlack,nearWhite,fullWhite,inCutscene\n")
            for k in range(10):
                f.write(f"{k},0.180,0.150,0.025,0.010,True\n")
        cin_ok_res = summarize_gate_run(os.path.join(tmp, "cin_ok"), reference_dirs=[os.path.join(tmp, "cin_ref")])
        assert cin_ok_res["spots"]["ben_interrogation"]["status"] == PASS, cin_ok_res

    print("pfm_brightness self-test: PASS")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="*", help="Gate run directories to inspect")
    ap.add_argument("--reference", action="append", default=[],
                    help="No-Remix reference run folder(s) holding <spot>/gate/<tag>_raster.pfm and cinematic.csv")
    ap.add_argument("--max-pose-error-deg", type=float, default=DEFAULT_MAX_POSE_ERROR_DEG,
                    help="Exclude stops whose camera pose differs from target by more than this (default: 1.0 deg)")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json-out", help="Optional JSON output path")
    args = ap.parse_args()
    if args.self_test:
        run_self_test()
        if not args.runs:
            return 0
    results = [
        summarize_gate_run(r, reference_dirs=args.reference, max_pose_error_deg=args.max_pose_error_deg)
        for r in args.runs
    ]
    for res in results:
        print(f"=== {res['run_dir']} (status={res['status']}, refs={res['reference_dirs']}) ===")
        for spot, info in res["spots"].items():
            print(
                f"  {spot:18s} [{info['status']}] sweep={info['n_sweep']:2d} "
                f"on_pose={info['n_valid_compared']}/{info['n_with_raster']} "
                f"nonfinite={info['nonfinite_pixels']} "
                f"gamma_mean=[{info['sweep_gamma_mean_min']} .. {info['sweep_gamma_mean_median']} .. {info['sweep_gamma_mean_max']}] "
                f"ratio=[{info['raster_gamma_ratio_min']} .. {info['raster_gamma_ratio_median']} .. {info['raster_gamma_ratio_max']}] "
                f"max_nw_delta={info['near_white_delta_max']}"
            )
            if info["off_pose_stops"]:
                print(f"    excluded off-pose ({len(info['off_pose_stops'])}): {info['off_pose_stops']}")
            if info["on_pose_stops"]:
                print(f"    used on-pose ({len(info['on_pose_stops'])}): {info['on_pose_stops']}")
            if info["reasons"]:
                print(f"    reasons: {'; '.join(info['reasons'])}")
            if info["cinematic"] is not None:
                c = info["cinematic"]
                print(
                    f"    cinematic [{c['status']}]: ratio={c.get('mean_luma_ratio')} "
                    f"nw_delta={c.get('near_white_delta')} reasons={c.get('reasons')}"
                )
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
