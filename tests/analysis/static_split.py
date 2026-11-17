"""Where does an original-against-original brightness difference sit: in the parts of the picture that changed between
the two launches (Sam's pose, screens with other content), or in the parts that did not (walls, floor, ceiling)?

Coordinator, Nov 17: today's no-Remix retake reference-20261117-133918 reads OUTSIDE against reference-20261116-175705
at paladin_1 (orig_floor.py: 11 stops, median 0.9627) and INSIDE at con_custodial. This splits each stop orig_floor.py
scores (same view rule, same pictures, same HUD crop) into a static and a moving part and gives the brightness ratio
new / reference of each part and the share of the picture each covers.

The split is computed from the two pictures, never drawn by hand:
  1. Display-gamma BT.709 luma, clipped to [0, 1], HUD border cut (pfm_brightness.crop_hud_roi, 8 % on every edge),
     box-averaged by DS (4) per side. Box averaging keeps the gamma mean, so ratios match orig_floor.py's statistic.
  2. The residual camera shift (views within 1 degree are about 30 px apart at 2560) is found by phase correlation
     on the luma gradient and taken out; only the overlap of the two pictures is compared.
  3. The overlap is cut into BLOCK x BLOCK blocks (8 at DS 4, 32 px at full size). A block with texture
     (either picture's std >= FLAT_STD) is static when its two halves correlate at >= CORR_MIN. Correlation ignores a
     brightness gain, so a dimmer copy of the same wall stays static.
  4. A flat block (both stds below FLAT_STD) has no structure to compare. It is static when its brightness ratio is
     within FLAT_BAND of the ratio of the textured static blocks, else moving. This is the only place the ratio enters
     the split, and only for flat blocks; textured blocks are never trimmed by their ratio.
  Ratio of a part = mean luma of its blocks in the new picture / in the reference (blocks are the same size).
  Share = the part's blocks / all compared blocks.

GUESSES (not measured, chosen before the paladin_1 numbers were read): BLOCK 8 (32 px), CORR_MIN 0.80, FLAT_STD 0.01,
FLAT_BAND 0.10, MAX_SHIFT 16 (64 px). The con_custodial pair (orig_floor INSIDE) is the control: its static ratio
must sit near 1.00.

Verdict (coordinator's wording, fixed before measuring): "static part matches" when the spot's static median lies inside
orig_floor.MEDIAN_BAND (0.996 to 1.012), else "static part differs". It names no cause.

  static_split.py <new no-Remix spot folder> <reference spot folder> [--json <out>]
  static_split.py --self-test

Exit 0 = static part matches, 3 = static part differs, 1 = no stop scored, 2 = bad input.
"""
import json
import os
import statistics
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import orig_floor  # noqa: E402
from orig_floor import gate, pfm_brightness  # noqa: E402

DS = 4
BLOCK = 8          # GUESS, in downsampled px (32 px at full size)
CORR_MIN = 0.80    # GUESS
FLAT_STD = 0.01    # GUESS, display-gamma luma
FLAT_BAND = 0.10   # GUESS
MAX_SHIFT = 16     # downsampled px (64 px at full size); views within 1 degree move about 30 px


def luma_ds(img: np.ndarray) -> np.ndarray:
    roi = pfm_brightness.crop_hud_roi(img)
    lum = pfm_brightness.bt709_luma(np.clip(np.nan_to_num(roi, nan=0.0), 0.0, 1.0)).astype(np.float64)
    h, w = (lum.shape[0] // DS) * DS, (lum.shape[1] // DS) * DS
    return lum[:h, :w].reshape(h // DS, DS, w // DS, DS).mean(axis=(1, 3))


def find_shift(a: np.ndarray, b: np.ndarray) -> tuple[int, int]:
    """(dy, dx) with a[y, x] ~ b[y - dy, x - dx], by phase correlation of the gradient magnitude."""
    def grad(x):
        gy, gx = np.gradient(x)
        g = np.hypot(gy, gx)
        return (g - g.mean()) * np.outer(np.hanning(x.shape[0]), np.hanning(x.shape[1]))
    fa, fb = np.fft.fft2(grad(a)), np.fft.fft2(grad(b))
    cross = fa * np.conj(fb)
    r = np.real(np.fft.ifft2(cross / np.maximum(np.abs(cross), 1e-12)))
    h, w = r.shape
    best, at = -np.inf, (0, 0)
    for dy in range(-MAX_SHIFT, MAX_SHIFT + 1):
        for dx in range(-MAX_SHIFT, MAX_SHIFT + 1):
            v = r[dy % h, dx % w]
            if v > best:
                best, at = v, (dy, dx)
    return at


def overlap(a: np.ndarray, b: np.ndarray, dy: int, dx: int) -> tuple[np.ndarray, np.ndarray]:
    h, w = a.shape
    ay0, by0 = max(dy, 0), max(-dy, 0)
    ax0, bx0 = max(dx, 0), max(-dx, 0)
    hh, ww = h - abs(dy), w - abs(dx)
    return a[ay0:ay0 + hh, ax0:ax0 + ww], b[by0:by0 + hh, bx0:bx0 + ww]


def split(a: np.ndarray, b: np.ndarray, use_rule: bool = True) -> dict:
    """a = new luma, b = reference luma (downsampled). use_rule False calls every block static (for the self-test)."""
    dy, dx = find_shift(a, b)
    a, b = overlap(a, b, dy, dx)
    ny, nx = a.shape[0] // BLOCK, a.shape[1] // BLOCK
    ab = a[:ny * BLOCK, :nx * BLOCK].reshape(ny, BLOCK, nx, BLOCK).transpose(0, 2, 1, 3).reshape(ny, nx, -1)
    bb = b[:ny * BLOCK, :nx * BLOCK].reshape(ny, BLOCK, nx, BLOCK).transpose(0, 2, 1, 3).reshape(ny, nx, -1)
    ma, mb, sa, sb = ab.mean(-1), bb.mean(-1), ab.std(-1), bb.std(-1)
    cov = ((ab - ma[..., None]) * (bb - mb[..., None])).mean(-1)
    corr = np.where((sa > 0) & (sb > 0), cov / np.maximum(sa * sb, 1e-12), 0.0)
    textured = np.maximum(sa, sb) >= FLAT_STD
    static_t = textured & (corr >= CORR_MIN)
    r0 = ma[static_t].sum() / mb[static_t].sum() if static_t.any() and mb[static_t].sum() > 0 else ma.sum() / mb.sum()
    ratio_f = np.where(mb > 1e-6, ma / np.maximum(mb, 1e-6), np.inf)
    static_f = ~textured & (np.abs(ratio_f / r0 - 1.0) <= FLAT_BAND)
    static = (static_t | static_f) if use_rule else np.ones_like(textured)
    moving = ~static

    def part(m):
        return round(float(ma[m].sum() / mb[m].sum()), 4) if m.any() and mb[m].sum() > 0 else None
    return {"shift": [dy * DS, dx * DS], "blocks": int(ny * nx),
            "staticRatio": part(static), "staticShare": round(float(static.mean()), 4),
            "movingRatio": part(moving), "movingShare": round(float(moving.mean()), 4),
            "wholeRatio": round(float(ma.sum() / mb.sum()), 4)}


def score_spot(new_spot: str, ref_spot: str) -> tuple[int, dict]:
    rows = []
    for stop in orig_floor.stops_of(new_spot):
        s = orig_floor.stop_ratio(new_spot, ref_spot, stop)
        if s["ratio"] is None:
            continue
        a, _, _ = orig_floor.load(new_spot, stop)
        b, _, _ = orig_floor.load(ref_spot, stop)
        row = {"stop": stop, "origFloorRatio": s["ratio"], "degApart": s["degApart"]}
        row.update(split(luma_ds(a), luma_ds(b)))
        rows.append(row)
    out: dict = {"new": new_spot, "reference": ref_spot, "stops": rows, "medianBand": list(orig_floor.MEDIAN_BAND),
                 "guesses": {"BLOCK_px": BLOCK * DS, "CORR_MIN": CORR_MIN, "FLAT_STD": FLAT_STD,
                             "FLAT_BAND": FLAT_BAND}}
    st = [r["staticRatio"] for r in rows if r["staticRatio"] is not None]
    if not st:
        out["verdict"] = "NOT MEASURED: no stop scored"
        return 1, out
    mv = [r["movingRatio"] for r in rows if r["movingRatio"] is not None]
    out.update(staticMedian=round(statistics.median(st), 4),
               movingMedian=round(statistics.median(mv), 4) if mv else None,
               staticShareMedian=round(statistics.median(r["staticShare"] for r in rows), 4))
    lo, hi = orig_floor.MEDIAN_BAND
    inside = lo <= out["staticMedian"] <= hi
    out["verdict"] = "static part matches" if inside else "static part differs"
    return (0 if inside else 3), out


def table(out: dict) -> str:
    lines = ["stop | orig_floor ratio | static ratio | static share | moving ratio | moving share | shift px (y, x)",
             "---|---|---|---|---|---|---"]
    for r in out["stops"]:
        mv = "none" if r["movingRatio"] is None else f"{r['movingRatio']:.4f}"
        lines.append(f"{r['stop']} | {r['origFloorRatio']:.4f} | {r['staticRatio']:.4f} | {r['staticShare']:.1%} | "
                     f"{mv} | {r['movingShare']:.1%} | {r['shift'][0]}, {r['shift'][1]}")
    if "staticMedian" in out:
        lines.append(f"median: static {out['staticMedian']:.4f}, moving {out['movingMedian']}, static share "
                     f"{out['staticShareMedian']:.1%}; band {out['medianBand']} -> {out['verdict']}")
    else:
        lines.append(out["verdict"])
    return "\n".join(lines)


def self_test() -> int:
    rng = np.random.default_rng(11)
    big_h, big_w = 720 + 64, 1280 + 64
    tex = np.kron(rng.uniform(0.3, 1.0, (big_h // 8 + 1, big_w // 8 + 1)), np.ones((8, 8)))[:big_h, :big_w]
    tex = tex + rng.normal(0, 0.03, tex.shape)
    tex[:, : big_w // 2] *= 0.3                                   # dark left half, bright right half
    tex = np.clip(tex * 0.6, 0, 1)

    def pic(y0=32, x0=32, fig_x=None, gain=1.0):
        p = tex.copy()
        if fig_x is not None:                                     # a striped figure 160 x 100 px, mean 0.6
            stripes = np.where((np.arange(100) // 6) % 2 == 0, 0.9, 0.3)
            p[300:460, fig_x:fig_x + 100] = stripes[None, :]
        p = p[y0:y0 + 720, x0:x0 + 1280] * gain
        return np.repeat(p[..., None], 3, axis=2).astype(np.float32)

    ref = pic()
    cases = [("identical", pic(), ref, True, lambda r: abs(r["staticRatio"] - 1) <= 0.002 and r["movingShare"] == 0
              and abs(r["wholeRatio"] - 1) <= 0.002),
             ("4 % darker everywhere", pic(gain=0.96), ref, True,
              lambda r: abs(r["staticRatio"] - 0.96) <= 0.002 and r["movingShare"] <= 0.01),
             ("only a pasted figure moves (dark half to bright half)", pic(fig_x=1000), pic(fig_x=250), True,
              lambda r: abs(r["staticRatio"] - 1) <= 0.002 and r["movingShare"] > 0.01),
             ("figure moves and the camera is 12 px off", pic(y0=40, x0=44, fig_x=1000), pic(fig_x=250), True,
              lambda r: abs((r["staticRatio"] or 0) - 1) <= 0.003 and r["shift"] == [-8, -12]),
             # the same pair with the split switched off must not read 1.00: the case can tell a split from none
             ("figure moves, split switched off", pic(fig_x=1000), pic(fig_x=250), False,
              lambda r: abs(r["staticRatio"] - 1) > 0.01)]
    ok = True
    for name, a, b, rule, want in cases:
        r = split(luma_ds(a), luma_ds(b), use_rule=rule)
        good = bool(want(r))
        ok &= good
        print(f"{name}: static {r['staticRatio']} ({r['staticShare']:.1%}), moving {r['movingRatio']} "
              f"({r['movingShare']:.1%}), whole {r['wholeRatio']}, shift {r['shift']} -> {'PASS' if good else 'FAIL'}")
    print("self-test", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    if argv[:1] == ["--self-test"]:
        return self_test()
    out_json = None
    if "--json" in argv:
        i = argv.index("--json")
        out_json, argv = argv[i + 1], argv[:i] + argv[i + 2:]
    if len(argv) != 2:
        print(__doc__)
        return 2
    code, out = score_spot(argv[0], argv[1])
    print(table(out))
    if out_json:
        gate.save_json(out_json, out)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
