"""PFM brightness inspector and original-game (`<tag>_raster.pfm`) vs Remix (`<tag>_final.pfm`) scorer.

Unit contract (verified against `rtx_context.cpp:743-746, 3081-3150`,
`tonemapping_apply_tonemapping.comp.slang:172`, `local_tonemap/final_combine.comp.slang:112`,
and `color.slangh:96`):
  - With `ModernFilter=1` (tonemapping active), Remix writes `rtOutput.m_finalOutput` AFTER
    `dispatchToneMapping(..., performSRGBConversion=true)`, which applies `pow(linear, 1/2.2)` at
    full output resolution (2560x1440).
  - Therefore `<tag>_final.pfm` and `<tag>_raster.pfm` MUST both store display/gamma-encoded RGB
    floats in [0, 1] (`uint8_rgb / 255.0` for screen grabs).
  - HUD exclusion is applied symmetrically as a central ROI crop on BOTH images at scoring time
    (never by zero-filling `<tag>_raster.pfm`, which would collapse 10th-percentile luma to 0).

Usage:
  python -I tools/remix/tests/analysis/brightness/pfm_brightness.py --self-test
  python -I tools/remix/tests/analysis/brightness/pfm_brightness.py <gate_run_dir> [<gate_run_dir> ...]
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys

import numpy as np

RUNTIME_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "runtime"))
if RUNTIME_DIR not in sys.path:
    sys.path.insert(0, RUNTIME_DIR)
import gate  # noqa: E402

REMIX_GAMMA = 2.2
# Default central ROI crop excluding outer 8% on each edge where Blacklist draws goggles/weapon/radar HUD.
DEFAULT_HUD_MARGIN_Y = 0.08
DEFAULT_HUD_MARGIN_X = 0.08
NEAR_BLACK_GAMMA = 0.05   # ~13/255 in display gamma (~0.0014 linear)
PURE_WHITE_GAMMA = 0.99   # >= 253/255 in display gamma


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


def image_brightness_stats(img: np.ndarray, mask_hud: bool = True) -> dict[str, float]:
    """Computes display-gamma and linear-decoded (pow 2.2) brightness statistics on a 3-channel PFM."""
    roi = crop_hud_roi(img) if mask_hud else img
    finite = np.isfinite(roi).all(axis=2)
    n_nonfinite = int((~finite).sum())
    if not finite.any():
        return {
            "nonfinite": n_nonfinite,
            "gamma_mean": float("nan"),
            "gamma_p10": float("nan"),
            "gamma_p50": float("nan"),
            "gamma_p90": float("nan"),
            "gamma_p99": float("nan"),
            "linear_mean": float("nan"),
            "linear_p10": float("nan"),
            "linear_p50": float("nan"),
            "near_black_share": float("nan"),
            "pure_white_share": float("nan"),
        }
    safe = np.clip(roi[finite], 0.0, None)
    g_lum = bt709_luma(safe)
    lin_rgb = np.power(safe, REMIX_GAMMA)
    l_lum = bt709_luma(lin_rgb)
    # Pure white matches recorder/perception: all 3 channels >= PURE_WHITE_GAMMA (or luma >= PURE_WHITE_GAMMA).
    pure_white = (safe.min(axis=1) >= PURE_WHITE_GAMMA)
    near_black = (g_lum <= NEAR_BLACK_GAMMA)
    return {
        "nonfinite": n_nonfinite,
        "gamma_mean": float(g_lum.mean()),
        "gamma_p10": float(np.percentile(g_lum, 10)),
        "gamma_p50": float(np.percentile(g_lum, 50)),
        "gamma_p90": float(np.percentile(g_lum, 90)),
        "gamma_p99": float(np.percentile(g_lum, 99)),
        "linear_mean": float(l_lum.mean()),
        "linear_p10": float(np.percentile(l_lum, 10)),
        "linear_p50": float(np.percentile(l_lum, 50)),
        "near_black_share": float(near_black.mean()),
        "pure_white_share": float(pure_white.mean()),
    }


def compare_final_to_raster(final_img: np.ndarray, raster_img: np.ndarray,
                            mask_hud: bool = True, eps: float = 1e-4) -> dict[str, float]:
    """Compares Remix `<tag>_final.pfm` against original-game `<tag>_raster.pfm` in matched units."""
    if final_img.shape != raster_img.shape:
        raise ValueError(f"shape mismatch: final {final_img.shape} vs raster {raster_img.shape}")
    fs = image_brightness_stats(final_img, mask_hud=mask_hud)
    rs = image_brightness_stats(raster_img, mask_hud=mask_hud)
    return {
        "final_gamma_mean": fs["gamma_mean"],
        "raster_gamma_mean": rs["gamma_mean"],
        "gamma_mean_ratio": fs["gamma_mean"] / max(rs["gamma_mean"], eps),
        "gamma_p10_ratio": fs["gamma_p10"] / max(rs["gamma_p10"], eps),
        "gamma_p50_ratio": fs["gamma_p50"] / max(rs["gamma_p50"], eps),
        "linear_mean_ratio": fs["linear_mean"] / max(rs["linear_mean"], eps),
        "near_black_delta": fs["near_black_share"] - rs["near_black_share"],
        "pure_white_delta": fs["pure_white_share"] - rs["pure_white_share"],
        "final_near_black": fs["near_black_share"],
        "raster_near_black": rs["near_black_share"],
        "final_pure_white": fs["pure_white_share"],
        "raster_pure_white": rs["pure_white_share"],
    }


def summarize_gate_run(run_dir: str) -> dict:
    spots_out = {}
    for spot_dir in sorted(glob.glob(os.path.join(run_dir, "*"))):
        gate_dir = os.path.join(spot_dir, "gate")
        if not os.path.isdir(gate_dir):
            continue
        spot_name = os.path.basename(spot_dir)
        finals = sorted(glob.glob(os.path.join(gate_dir, "*_final.pfm")))
        if not finals:
            continue
        stops = []
        for fp in finals:
            tag = os.path.basename(fp)[:-len("_final.pfm")]
            f_img = gate.read_pfm(fp)
            st = image_brightness_stats(f_img, mask_hud=True)
            entry = {"tag": tag, "shape": list(f_img.shape), **st}
            rp = os.path.join(gate_dir, f"{tag}_raster.pfm")
            if os.path.exists(rp):
                r_img = gate.read_pfm(rp)
                entry["vs_raster"] = compare_final_to_raster(f_img, r_img, mask_hud=True)
            stops.append(entry)
        sweep = [s for s in stops if not s["tag"].startswith("switch_")]
        switches = [s for s in stops if s["tag"].startswith("switch_")]
        spots_out[spot_name] = {
            "n_sweep": len(sweep),
            "n_switch": len(switches),
            "sweep_gamma_mean_min": min((s["gamma_mean"] for s in sweep), default=None),
            "sweep_gamma_mean_median": float(np.median([s["gamma_mean"] for s in sweep])) if sweep else None,
            "sweep_gamma_mean_max": max((s["gamma_mean"] for s in sweep), default=None),
            "sweep_near_black_max": max((s["near_black_share"] for s in sweep), default=None),
            "sweep_pure_white_max": max((s["pure_white_share"] for s in sweep), default=None),
            "stops": stops,
        }
    return {"run_dir": run_dir, "spots": spots_out}


def run_self_test() -> None:
    # 1. Prove why mismatched sRGB-decoded raster vs gamma-encoded final causes a ~2.3x bug.
    u8 = np.full((180, 320, 3), 128, dtype=np.uint8)
    gamma_img = u8.astype(np.float32) / 255.0
    wrong_linear_raster = np.power(gamma_img, REMIX_GAMMA)
    matched = compare_final_to_raster(gamma_img, gamma_img)
    mismatched = compare_final_to_raster(gamma_img, wrong_linear_raster)
    assert abs(matched["gamma_mean_ratio"] - 1.0) < 1e-6, matched
    assert mismatched["gamma_mean_ratio"] > 2.2, f"expected >2.2x unit bug, got {mismatched}"

    # 2. Prove symmetric HUD crop ignores HUD border differences without collapsing p10 to 0.
    raster_with_hud = gamma_img.copy()
    raster_with_hud[:10, :, :] = 1.0     # bright HUD bar at top
    raster_with_hud[-10:, :, :] = 0.0    # black border at bottom
    cropped = compare_final_to_raster(gamma_img, raster_with_hud, mask_hud=True)
    assert abs(cropped["gamma_mean_ratio"] - 1.0) < 1e-6, cropped
    assert abs(cropped["gamma_p10_ratio"] - 1.0) < 1e-6, cropped

    # 3. Prove lower and upper fake cases separate cleanly from baseline.
    too_dark = gamma_img * 0.40
    too_bright = np.clip(gamma_img * 1.85, 0.0, 1.0)
    dark_cmp = compare_final_to_raster(too_dark, gamma_img)
    bright_cmp = compare_final_to_raster(too_bright, gamma_img)
    assert dark_cmp["gamma_mean_ratio"] < 0.50 and dark_cmp["linear_mean_ratio"] < 0.20, dark_cmp
    assert bright_cmp["gamma_mean_ratio"] > 1.70 and bright_cmp["linear_mean_ratio"] > 3.0, bright_cmp
    print("pfm_brightness self-test: PASS")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="*", help="Gate run directories to inspect")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json-out", help="Optional JSON output path")
    args = ap.parse_args()
    if args.self_test:
        run_self_test()
        if not args.runs:
            return 0
    results = [summarize_gate_run(r) for r in args.runs]
    for res in results:
        print(f"=== {res['run_dir']} ===")
        for spot, info in res["spots"].items():
            print(
                f"  {spot:18s} sweep={info['n_sweep']:2d} switch={info['n_switch']:2d} "
                f"gamma_mean=[{info['sweep_gamma_mean_min']} .. {info['sweep_gamma_mean_median']} .. {info['sweep_gamma_mean_max']}] "
                f"max_near_black={info['sweep_near_black_max']} max_pure_white={info['sweep_pure_white_max']}"
            )
            for s in info["stops"]:
                if s["tag"].startswith("switch_") or s["tag"] in ("y0_p0", "y180_p0"):
                    print(
                        f"    {s['tag']:24s} gamma(mean={s['gamma_mean']:.4f}, p10={s['gamma_p10']:.4f}, p90={s['gamma_p90']:.4f}) "
                        f"lin_mean={s['linear_mean']:.4f} nearBlack={s['near_black_share']:.4f} pureWhite={s['pure_white_share']:.4f}"
                    )
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
