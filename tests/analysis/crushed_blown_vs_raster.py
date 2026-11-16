"""Surface markers: blocks the original game draws lit but Remix draws near black (crushed), and the mirror,
blocks the game draws dark but Remix draws light (blown, e.g. clay-white albedo on dark metal).

Compares <tag>_final.pfm (Remix) with <tag>_raster.pfm (no-Remix reference) at the same gate stop.
Both are display-gamma RGB in [0, 1]. The HUD border is cropped the same way as pfm_brightness.py.
To tolerate small camera or guard differences, it works on 16x16 pixel blocks (median luma per block).

  crushedShare    : raster block luma >= LIT and Remix block luma <= DARK
  relCrushedShare : the same on each image divided by its own median block luma (exposure-independent)
  relBlownShare   : raster block <= 0.5x its median and Remix block >= 1.5x its median
Verdict per stop (provisional pass values, Nov 16): FAIL when crushedShare > 0.5%, relCrushedShare > 1.0% or
relBlownShare > 1.0%. NOT MEASURED when the two pictures do not show the same view: pose error above
MAX_POSE_ERROR_DEG (Remix gamePose from spot.json and the reference relPose from <tag>_raster.json), or
rankCorr of block luma below MIN_RANK_CORR.

Usage:
  python -I crushed_blown_vs_raster.py --self-test
  python -I crushed_blown_vs_raster.py <remix spot dir> <raster spot dir> [--mask-noise] [--dump <out dir>]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "brightness"))
import pfm_brightness as pb  # noqa: E402

BLOCK = 16
LIT = 0.15   # gamma luma ~38/255: clearly lit in the original game
DARK = 0.05  # gamma luma ~13/255: the same near-black cut pfm_brightness uses
REL_LIT = 0.75   # raster block at least 0.75x the raster frame median
REL_DARK = 0.15  # Remix block at most 0.15x the Remix frame median
# Mirror marker (blownShare): the game draws a block dark, Remix draws it light.
GAME_DARK = 0.10    # gamma luma ~26/255 in the game's picture
REMIX_LIGHT = 0.30  # gamma luma ~77/255 in Remix
BLOWN_RATIO = 3.0   # and Remix at least 3x the game
REL_GAME_DARK = 0.5     # relBlownShare: game block at most 0.5x its frame median
REL_REMIX_LIGHT = 1.5   # Remix block at least 1.5x its frame median
# Noise masks (--mask-noise): particles in the reference and depth silhouettes are left out of every share.
SPARK_DELTA = 0.25
SPARK_MAX_FRAC = 0.25
EDGE_RATIO = 1.5
# Provisional pass values (Nov 16); none has yet been shown to FAIL a real stop except relBlownShare at con_boiler.
MAX_CRUSHED = 0.005
MAX_REL_CRUSHED = 0.01
MAX_REL_BLOWN = 0.01
# View-match rule. The reference pose separates a wrong view; rankCorr alone does not. In the Nov 15/16 runs, the
# one mismatched stop (con_bright_hall y270_p0, reference relPose -62.2 for -90) is 27.8 deg off with rankCorr 0.38,
# while matched stops are at most 7.2 deg off (con_custodial y90_p-30) but go as low as rankCorr 0.26 (con_bright_hall y315_p30), and the
# known clay-white failure (con_boiler y0_p0) has 0.53. The rankCorr floor only catches pictures with no pose data.
MAX_POSE_ERROR_DEG = 10.0
MIN_RANK_CORR = 0.20


def block_luma(img: np.ndarray) -> np.ndarray:
    roi = pb.crop_hud_roi(img)
    lum = pb.bt709_luma(np.clip(np.nan_to_num(roi, nan=0.0), 0.0, 1.0))
    h, w = (lum.shape[0] // BLOCK) * BLOCK, (lum.shape[1] // BLOCK) * BLOCK
    b = lum[:h, :w].reshape(h // BLOCK, BLOCK, w // BLOCK, BLOCK).transpose(0, 2, 1, 3)
    return np.median(b.reshape(b.shape[0], b.shape[1], -1), axis=2)


def particle_blocks(raster_img: np.ndarray) -> np.ndarray:
    """Blocks holding small bright spots in the reference (sparks, flare balls, glints), dilated by one block for
    their glow. A spot: pixels brighter than the block median by SPARK_DELTA covering under SPARK_MAX_FRAC."""
    roi = pb.crop_hud_roi(raster_img)
    lum = pb.bt709_luma(np.clip(np.nan_to_num(roi, nan=0.0), 0.0, 1.0))
    h, w = (lum.shape[0] // BLOCK) * BLOCK, (lum.shape[1] // BLOCK) * BLOCK
    b = lum[:h, :w].reshape(h // BLOCK, BLOCK, w // BLOCK, BLOCK).transpose(0, 2, 1, 3).reshape(h // BLOCK, w // BLOCK, -1)
    frac = (b > np.median(b, axis=2, keepdims=True) + SPARK_DELTA).mean(axis=2)
    spot = (frac > 0) & (frac < SPARK_MAX_FRAC)
    return dilate(spot)


def edge_blocks(depth: np.ndarray | None, shape: tuple) -> np.ndarray:
    """Blocks on a Remix depth silhouette (max/min depth in the block > EDGE_RATIO), dilated by one block: a
    character or prop that moved a little between the reference grab and the Remix dump differs only there."""
    if depth is None:
        return np.zeros(shape, bool)
    dh, dw = depth.shape[:2]
    y0, x0 = int(round(dh * pb.DEFAULT_HUD_MARGIN_Y)), int(round(dw * pb.DEFAULT_HUD_MARGIN_X))
    d = np.nan_to_num(depth[y0:dh - y0, x0:dw - x0], nan=1e6)
    ys = np.linspace(0, d.shape[0], shape[0] + 1).astype(int)
    xs = np.linspace(0, d.shape[1], shape[1] + 1).astype(int)
    out = np.zeros(shape, bool)
    for i in range(shape[0]):
        for j in range(shape[1]):
            c = d[ys[i]:max(ys[i + 1], ys[i] + 1), xs[j]:max(xs[j + 1], xs[j] + 1)]
            out[i, j] = c.max() > EDGE_RATIO * max(c.min(), 1e-3)
    return dilate(out)


def dilate(m: np.ndarray) -> np.ndarray:
    o = m.copy()
    o[1:] |= m[:-1]; o[:-1] |= m[1:]; o[:, 1:] |= m[:, :-1]; o[:, :-1] |= m[:, 1:]
    return o


def score(final_img: np.ndarray, raster_img: np.ndarray, depth: np.ndarray | None = None,
          mask_noise: bool = False) -> dict:
    f, r = block_luma(final_img), block_luma(raster_img)
    keep = np.ones(f.shape, bool)
    if mask_noise:
        keep = ~(particle_blocks(raster_img) | edge_blocks(depth, f.shape))
    n = max(int(keep.sum()), 1)
    crushed = (r >= LIT) & (f <= DARK) & keep
    # Mirror: the game draws it dark, Remix draws it light (clay-white albedo on dark metal).
    blown = (r <= GAME_DARK) & (f >= REMIX_LIGHT) & (f >= BLOWN_RATIO * r) & keep
    lit = r >= LIT
    # Alignment witness: rank correlation of block luma. Low values mean the views differ.
    fr = np.argsort(np.argsort(f.ravel())).astype(np.float64)
    rr = np.argsort(np.argsort(r.ravel())).astype(np.float64)
    corr = float(np.corrcoef(fr, rr)[0, 1])
    # Exposure-independent variant: each image divided by its own median block luma.
    fn, rn = f / max(np.median(f), 1e-4), r / max(np.median(r), 1e-4)
    rel = (rn >= REL_LIT) & (fn <= REL_DARK) & keep
    relblown = (rn <= REL_GAME_DARK) & (fn >= REL_REMIX_LIGHT) & keep
    return {
        "relCrushedShare": float(rel.sum() / n),
        "crushedShare": float(crushed.sum() / n),
        "blownShare": float(blown.sum() / n),
        "relBlownShare": float(relblown.sum() / n),
        "maskedShare": float(1.0 - keep.mean()),
        "blownMask": blown,
        "crushedOfLit": float(crushed.sum() / max(lit.sum(), 1)),
        "litShare": float(lit.mean()),
        "remixDarkShare": float((f <= DARK).mean()),
        "rasterDarkShare": float((r <= DARK).mean()),
        "rankCorr": corr,
        "mask": crushed,
    }


def verdict(s: dict, pose_error_deg: float | None) -> tuple[str, str]:
    """(PASS | FAIL | NOT MEASURED, reason) for one stop's score() result."""
    if pose_error_deg is not None and pose_error_deg > MAX_POSE_ERROR_DEG:
        return "NOT MEASURED", f"views differ: pose error {pose_error_deg:.1f} deg > {MAX_POSE_ERROR_DEG:g}"
    if s["rankCorr"] < MIN_RANK_CORR:
        return "NOT MEASURED", f"views differ: rankCorr {s['rankCorr']:.2f} < {MIN_RANK_CORR:g}"
    over = [f"{k} {s[k]*100:.2f}% > {lim*100:g}%" for k, lim in
            (("crushedShare", MAX_CRUSHED), ("relCrushedShare", MAX_REL_CRUSHED), ("relBlownShare", MAX_REL_BLOWN))
            if s[k] > lim]
    return ("FAIL", "; ".join(over)) if over else ("PASS", "")


def self_test() -> int:
    rng = np.random.default_rng(1)
    h, w = 720, 1280
    base = np.clip(0.3 + 0.2 * rng.random((h, w, 1)), 0, 1).repeat(3, axis=2).astype(np.float32)
    same = score(base * 1.1, base)
    assert same["crushedShare"] == 0.0, same
    bad = base.copy()
    bad[200:400, 300:700] = 0.01  # fake flat-black floor patch
    s = score(bad, base)
    roi_area = (h * 0.84) * (w * 0.84)
    expect = 200 * 400 / roi_area
    assert abs(s["crushedShare"] - expect) < 0.02, (s["crushedShare"], expect)
    assert abs(s["relCrushedShare"] - expect) < 0.02, (s["relCrushedShare"], expect)
    # Same patch under a 4x brighter Remix exposure: absolute cut misses it, relative cut still finds it.
    lifted = np.clip(base * 2.0, 0, 1); lifted[200:400, 300:700] = 0.08
    s2 = score(lifted, base)
    assert s2["crushedShare"] == 0.0 and abs(s2["relCrushedShare"] - expect) < 0.02, s2
    # Dark in both images is not crushed.
    both = base.copy(); both[200:400, 300:700] = 0.01
    assert score(both, both)["crushedShare"] == 0.0
    # Mirror fake: a clay-white surface (0.8) where the game draws dark metal (0.04) -> blown, not crushed.
    metal = base.copy(); metal[200:400, 300:700] = 0.04
    clay = base.copy(); clay[200:400, 300:700] = 0.8
    sb = score(clay, metal)
    assert abs(sb["blownShare"] - expect) < 0.02 and sb["crushedShare"] == 0.0, sb
    assert abs(sb["relBlownShare"] - expect) < 0.02, sb
    assert score(metal, metal)["blownShare"] == 0.0 and score(clay, clay)["blownShare"] == 0.0
    # A dark surface lit a bit more by Remix (0.04 -> 0.08) is not blown.
    lit_more = base.copy(); lit_more[200:400, 300:700] = 0.08
    assert score(lit_more, metal)["blownShare"] == 0.0
    # Particle mask: sparks in the reference only (Remix shows plain floor) make crushed blocks unless masked.
    floor = np.full((h, w, 3), 0.03, np.float32)
    sparks = floor.copy()
    for yy in range(150, 600, 48):
        for xx in range(150, 1100, 48):
            sparks[yy:yy + 3, xx:xx + 3] = 1.0
    pmask = particle_blocks(sparks)
    assert pmask.sum() > 0 and particle_blocks(floor).sum() == 0, pmask.sum()
    # Masking must not hide a real fake patch: the clay patch is still found with mask_noise on.
    assert abs(score(clay, metal, None, True)["blownShare"] - expect) < 0.03
    # View-match rule. The matched clay pair FAILs; the same pair with the reference pose 28 deg off (con_bright_hall
    # y270_p0) is NOT MEASURED; an unrelated reference with the same dark patch has the same share and no pose data,
    # and is NOT MEASURED by rankCorr. Under 10 deg pose error still counts as the same view.
    assert verdict(sb, 0.0)[0] == "FAIL" and verdict(sb, 6.4)[0] == "FAIL", verdict(sb, 0.0)
    assert verdict(sb, 27.8) == ("NOT MEASURED", "views differ: pose error 27.8 deg > 10"), verdict(sb, 27.8)
    other = np.clip(0.3 + 0.2 * np.random.default_rng(2).random((h, w, 1)), 0, 1).repeat(3, axis=2).astype(np.float32)
    other[200:400, 300:700] = 0.04
    so = score(clay, other)
    assert abs(so["relBlownShare"] - sb["relBlownShare"]) < 0.005 and so["rankCorr"] < MIN_RANK_CORR, so["rankCorr"]
    assert verdict(so, None)[0] == "NOT MEASURED" and verdict(sb, None)[0] == "FAIL"
    assert verdict(score(metal, metal), 0.0)[0] == "PASS"
    print(f"self-test ok: clean 0.0, fake black patch {s['crushedShare']:.4f}, fake clay patch {sb['blownShare']:.4f} "
          f"(expected {expect:.4f}), spark blocks masked {int(pmask.sum())}")
    return 0


def save_mask(path: str, final_img: np.ndarray, mask: np.ndarray) -> None:
    roi = pb.crop_hud_roi(final_img)
    h, w = mask.shape[0] * BLOCK, mask.shape[1] * BLOCK
    rgb = np.clip(np.nan_to_num(roi[:h, :w]), 0, 1)[::4, ::4].copy()
    m = np.kron(mask, np.ones((BLOCK // 4, BLOCK // 4), dtype=bool))
    rgb[m] = rgb[m] * 0.3 + np.array([0.7, 0.0, 0.7]) * 0.7
    out = (rgb * 255).astype(np.uint8)
    with open(path, "wb") as fh:
        fh.write(f"P6 {out.shape[1]} {out.shape[0]} 255\n".encode())
        fh.write(out.tobytes())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("remix", nargs="?")
    ap.add_argument("raster", nargs="?")
    ap.add_argument("--dump")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--mask-noise", action="store_true", help="leave reference particles and depth silhouettes out")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    rows = []
    windows = pb._load_spot_windows(a.remix)
    for rp in sorted(glob.glob(os.path.join(a.raster, "gate", "*_raster.pfm"))):
        tag = os.path.basename(rp)[: -len("_raster.pfm")]
        fp = os.path.join(a.remix, "gate", f"{tag}_final.pfm")
        if not os.path.exists(fp):
            continue
        fi, ri = pb.gate.read_pfm(fp), pb.gate.read_pfm(rp)
        dp = os.path.join(a.remix, "gate", f"{tag}_depth.pfm")
        depth = pb.gate.read_pfm(dp) if a.mask_noise and os.path.exists(dp) else None
        s = score(fi, ri, depth, a.mask_noise)
        rj = os.path.join(a.raster, "gate", f"{tag}_raster.json")
        s["poseErrorDeg"] = pb._window_pose_error_deg(tag, windows.get(tag), rj if os.path.exists(rj) else None)
        s["verdict"], s["reason"] = verdict(s, s["poseErrorDeg"])
        if a.dump:
            os.makedirs(a.dump, exist_ok=True)
            save_mask(os.path.join(a.dump, f"{tag}_crushed.ppm"), fi, s["mask"])
            save_mask(os.path.join(a.dump, f"{tag}_blown.ppm"), fi, s["blownMask"])
        s.pop("mask"); s.pop("blownMask")
        rows.append({"tag": tag, **s})
    for r in rows:
        print(f"{r['tag']:>16}  rel {r['relCrushedShare']*100:6.2f}%  crushed {r['crushedShare']*100:6.2f}%  "
              f"blown {r['blownShare']*100:6.2f}%  relBlown {r['relBlownShare']*100:6.2f}%  masked {r['maskedShare']*100:5.1f}%  "
              f"rankCorr {r['rankCorr']:.2f}  pose {'-' if r['poseErrorDeg'] is None else format(r['poseErrorDeg'], '.1f')}  "
              f"{r['verdict']} {r['reason']}")
    if rows:
        cs = np.array([r["crushedShare"] for r in rows])
        rs = np.array([r["relCrushedShare"] for r in rows])
        bs = np.array([r["blownShare"] for r in rows])
        print(f"stops {len(rows)}  crushed median {np.median(cs)*100:.2f}% max {cs.max()*100:.2f}%  "
              f"rel median {np.median(rs)*100:.2f}% max {rs.max()*100:.2f}%  blown median {np.median(bs)*100:.2f}% max {bs.max()*100:.2f}%")
    print(json.dumps(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
