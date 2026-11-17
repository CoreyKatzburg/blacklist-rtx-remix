"""Symptom check for problem ledger row 2 ("orbs with no lamp, floating or shining through walls"), pictures only.

Worker B, Nov 16. Finds small bright blobs in the Remix picture that the original game's picture at the same pose does
not show. It never reads a light list, a flare list or the stand-in ball list, so it judges what Corey sees whatever
the cause turns out to be.

Inputs: two stills of the same pose, each a .gray file (320x180 u8 luma, one or more frames; their mean is used).
  --remix   the Remix still          --orig   the original game's still (a no-Remix run)
  --pose    optional: "x,y,z,yaw,pitch;x,y,z,yaw,pitch" for the two cameras (from proxy_frames.csv); the check
            refuses to judge (NOT MEASURED, exit 2) when they differ by more than POSE_CM, POSE_DEG (yaw) or POSE_PITCH_DEG.

Rule:
  1. Local contrast of each picture: the pixel minus the median of the 25x25 window around it (its background).
  2. A Remix blob is a 4-connected group of pixels whose contrast is at least BLOB_STEPS (0..255 luma steps), at most
     BLOB_MAX_PX pixels, with its brightest pixel at least BORDER_PX inside the frame. A blob whose ring 6 to 12 px
     out is more than RING_BRIGHT_MAX bright (above halfway between background and peak) is the edge or corner of a
     bigger bright area (window, lit wall, sky), not an orb, and is skipped.
  3. Remix lights the whole scene brighter than the original (picture means 118 against 47 at con_bright_hall), so
     the original is compared after matching brightness locally: gain = Remix background / original background at
     the blob, clamped to 1..GAIN_MAX. The original's bump is its highest contrast within MATCH_PX of the blob's peak
     (MATCH_PX covers a 1.5 degree yaw difference: 2.8 px per degree at the screen centre).
  Pose tolerance, measured (worker B, Nov 16) by shifting the 142638 y225_p0 Remix still against the original's y225_p0
  (0.3 degrees apart): the lost and dimmed counts hold up to 5 px sideways and 3 px up or down; at 6 px sideways the
  lost count reports a glow that is there, at 8 px the dimmed count misses an erased lamp, and at 4 px vertical it
  misses one of 5. At 2.8 px per degree that is 1.8 degrees of yaw and 1.1 of pitch, so POSE_DEG (yaw) is 1.5 and
  POSE_PITCH_DEG 1.0. A camera that moved sideways shifts a lamp 2.5 m away by 5 px at 7.8 cm, so POSE_CM is 8.
  Two original stills 3.2 degrees apart (reference-20261116-191120 lampsB against y225_p0) count 1 lost glow and
  3 orbs that are not there.
  4. The original shows the blob when its bump reaches ORIG_STEPS (a distinct bright spot of its own), or when
     excess = Remix peak contrast - gain x original bump stays under BLOB_STEPS (Remix only brightened what the
     original has). Any other blob is counted. PASS when the count is at most ALLOWED, else FAIL.
Output: one line "glow_blobs: unmatched N of M blobs (...)", then one line per unmatched blob.

Second count, puffs (coordinator, Nov 16: Corey's "lights floating in mid air" may be large soft discs or starbursts,
which rule 2 skips as too big):
  5. Both pictures are averaged into 4x4 pixel cells; each cell's contrast is its value minus the median of the
     15x15 cells (60x60 px) around it.
  6. A cell is unexplained when its Remix contrast is at least PUFF_STEPS and the original's contrast in the same cell
     stays under ORIG_STEPS (the original has no bright area there). A 4-connected group of unexplained cells
     covering more than BLOB_MAX_PX pixels is one puff. The count is printed as report only and does not enter
     the verdict (coordinator, Nov 16: a count that cannot separate a floating light from a lit surface in code
     must not decide PASS or FAIL; it shows 2 puffs at con_custodial with the balls off).
  Limit: this also counts an area Remix lights that the original leaves dark (a lit room seen through a doorway),
  because pictures alone cannot tell a glow from a lit surface.

Third count, lost glows (the reverse direction; coordinator, Nov 16: switching every ball off must not pass when a
lamp glow the original shows goes missing):
  7. An original glow is a small blob found in the original by rules 1 and 2 (contrast >= BLOB_STEPS, <= BLOB_MAX_PX
     pixels, inside the border, not the rim of a bigger bright area).
  8. Remix shows it when, within MATCH_PX of its peak, Remix has its own bump of at least ORIG_STEPS, or Remix's
     brightest pixel reaches KEEP_FRAC of the original's peak luma (the glow merged into a Remix area at least about
     as bright). For a strong glow (original contrast >= STRONG_STEPS, a light source) the original's luma is first
     scaled by the same local gain as rule 3, capped at 255, because Remix lights every wall brighter and a plain
     brightened wall must not count as showing a lamp. Weaker original spots (Sam's buckles, wet-floor glints,
     shadow edges; up to 90 steps at con_bright_hall) are compared unscaled, since Sam's idle pose and the
     reflections differ between runs. Otherwise the glow is lost. PASS when at most ALLOWED are lost.
  Measured on 142638 against 000454 (24 stills with a matching pose): erasing each original glow of contrast
  >= 100 from the Remix still is counted lost 97 times of 105 (the 8 misses: 5 lamps wider than the erased disc,
  3 in Remix areas above 191 luma); unedited stills count 1 lost, a lamp reflection in a wet-floor stain that
  Remix does not draw (y180_p-30, x 305 y 104).

Fourth count, dimmed lamps (a must-stay check for a change that removes something, such as the stand-in balls;
coordinator, Nov 16). The lost count misses a lamp whose Remix glow sits on a wall Remix lights brightly, so this
count compares Remix with itself before and after the change, at the lamps the original shows:
  9. Inputs: the original still and three Remix stills at the same pose: on (before the change), changed, on_again
     (change undone). For each strong original glow (rule 7 with contrast >= STRONG_STEPS), Remix's glow in each
     still is its highest contrast within MATCH_PX of the original's peak. The lamp is DIMMED when Remix shows it
     before the change (glow on >= ORIG_STEPS) and on - changed > max(DROP_MIN, 3 x |on - on_again|), so a drop
     inside the run's own on/on_again noise does not count. PASS when at most ALLOWED lamps are dimmed.
  Usage: --dimmed CHANGED --again ON_AGAIN with --remix-spot, --orig-spot and --still ON (window names).

HUD: with --orig-spot, pixels that stand out (contrast >= ORIG_STEPS) at the same screen place in at least half of
the original spot's stills, although the camera turned between them, are the HUD; the mask is that set grown by 2 px
with enclosed holes (the radar disc) filled. Blobs, puffs and glows whose peak lies on the HUD are skipped in all
three counts. Without --orig-spot (two files) there is no HUD mask, and the output says so.
The verdict is PASS only when the orb count and the lost-glow count pass. The puff count is report only until it can
separate a floating glow from a surface Remix lights, with a fake case for each kind.

  --remix-spot / --orig-spot / --still [--orig-still]: instead of the files, two gate spot folders and a window name;
            the stills and both cameras come from spot.json and proxy_frames.csv, and the pose test always runs.
Exit codes: 0 PASS, 1 FAIL, 2 NOT MEASURED.
Limits: the orb count skips blobs over BLOB_MAX_PX and the rim of larger bright areas (the skipped counts are
printed); those are what the puff count is for. The puff count cannot tell a glow from a surface Remix lights and
the original leaves dark. The lost count misses a lamp whose place Remix fills with an area of 191 luma or more.

Usage: glow_blobs.py --remix R.gray --orig O.gray [--pose P] [--crops DIR]
       glow_blobs.py --remix-spot DIR --orig-spot DIR --still NAME [--orig-still NAME] [--crops DIR]
       glow_blobs.py --self-test
"""
import argparse, json, math, pathlib, re, sys
import numpy as np

W, H = 320, 180
WIN = 25
BLOB_STEPS, ORIG_STEPS, BLOB_MAX_PX, MATCH_PX, BORDER_PX, ALLOWED = 24.0, 12.0, 200, 6, 3, 0
GAIN_MAX, RING_BRIGHT_MAX = 6.0, 0.2
POSE_CM, POSE_DEG, POSE_PITCH_DEG = 8.0, 1.5, 1.0
CELL, CELL_WIN, PUFF_STEPS = 4, 15, 24.0
KEEP_FRAC, STRONG_STEPS, DROP_MIN = 0.75, 100.0, 12.0
HUD_MIN_STILLS = 8


def still(path: pathlib.Path) -> np.ndarray:
    raw = np.fromfile(path, dtype=np.uint8)
    n = raw.size // (W * H)
    if n == 0:
        raise SystemExit(f"{path}: not a 320x180 .gray still")
    return raw[: n * W * H].reshape(n, H, W).astype(np.float32).mean(axis=0)


def contrast(img: np.ndarray):
    """(contrast, background): background is the median of the WIN x WIN window around each pixel."""
    r = WIN // 2
    pad = np.pad(img, r, mode="edge")
    win = np.lib.stride_tricks.sliding_window_view(pad, (WIN, WIN))
    bg = np.median(win.reshape(H, W, -1), axis=2)
    return img - bg, bg


def blobs(c: np.ndarray):
    """4-connected groups of pixels with contrast >= BLOB_STEPS: list of (pixel count, peak (y, x), peak value)."""
    mask = c >= BLOB_STEPS
    seen = np.zeros_like(mask)
    out = []
    for y0, x0 in zip(*np.nonzero(mask)):
        if seen[y0, x0]:
            continue
        stack, px = [(y0, x0)], []
        seen[y0, x0] = True
        while stack:
            y, x = stack.pop()
            px.append((y, x))
            for yy, xx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= yy < H and 0 <= xx < W and mask[yy, xx] and not seen[yy, xx]:
                    seen[yy, xx] = True
                    stack.append((yy, xx))
        ys, xs = np.array(px).T
        k = int(np.argmax(c[ys, xs]))
        out.append((len(px), (int(ys[k]), int(xs[k])), float(c[ys[k], xs[k]])))
    return out


YY, XX = np.mgrid[0:H, 0:W]


def small_blobs(img, c, bg, hud, skipped):
    """Blobs of contrast map c that rule 2 keeps, with the distance map around each peak; skips are tallied."""
    for area, (y, x), peak in blobs(c):
        if area > BLOB_MAX_PX:
            skipped["big"] += 1
            continue
        if not (BORDER_PX <= x < W - BORDER_PX and BORDER_PX <= y < H - BORDER_PX):
            skipped["border"] += 1
            continue
        if hud is not None and hud[y, x]:
            skipped["hud"] += 1
            continue
        d = np.hypot(YY - y, XX - x)
        ring = img[(d >= 6) & (d <= 12)]
        if np.mean(ring > bg[y, x] + 0.5 * peak) > RING_BRIGHT_MAX:
            skipped["area edge"] += 1
            continue
        yield area, y, x, peak, d


def judge(remix: np.ndarray, orig: np.ndarray, hud=None):
    """Rows for every candidate blob, and counts of the blobs skipped by each rule."""
    cr, br = contrast(remix)
    co, bo = contrast(orig)
    rows, skipped = [], {"big": 0, "border": 0, "hud": 0, "area edge": 0}
    for area, y, x, peak, d in small_blobs(remix, cr, br, hud, skipped):
        orig_bump = float(co[d <= MATCH_PX].max())
        gain = float(np.clip((br[y, x] + 1.0) / (bo[y, x] + 1.0), 1.0, GAIN_MAX))
        excess = peak - gain * max(orig_bump, 0.0)
        rows.append({"x": x, "y": y, "area": area, "peak": peak, "orig": orig_bump, "gain": gain,
                     "excess": excess, "matched": orig_bump >= ORIG_STEPS or excess < BLOB_STEPS})
    return rows, skipped


def lost_glows(remix: np.ndarray, orig: np.ndarray, hud=None):
    """Rules 7 and 8: rows for every original glow, with "lost" set when Remix does not show it."""
    cr, br = contrast(remix)
    co, bo = contrast(orig)
    rows, skipped = [], {"big": 0, "border": 0, "hud": 0, "area edge": 0}
    for area, y, x, peak, d in small_blobs(orig, co, bo, hud, skipped):
        near = d <= MATCH_PX
        bump, top = float(cr[near].max()), float(remix[near].max())
        gain = float(np.clip((br[y, x] + 1.0) / (bo[y, x] + 1.0), 1.0, GAIN_MAX)) if peak >= STRONG_STEPS else 1.0
        need = KEEP_FRAC * min(255.0, gain * float(orig[y, x]))
        rows.append({"x": x, "y": y, "area": area, "peak": peak, "orig_luma": float(orig[y, x]), "gain": gain,
                     "need": need, "remix_bump": bump, "remix_top": top, "lost": bump < ORIG_STEPS and top < need})
    return rows, skipped


def cell_contrast(img: np.ndarray):
    h, w = H // CELL, W // CELL
    s = img[: h * CELL, : w * CELL].reshape(h, CELL, w, CELL).mean(axis=(1, 3))
    r = CELL_WIN // 2
    win = np.lib.stride_tricks.sliding_window_view(np.pad(s, r, mode="edge"), (CELL_WIN, CELL_WIN))
    return s - np.median(win.reshape(h, w, -1), axis=2)


def puffs(remix: np.ndarray, orig: np.ndarray, hud=None):
    """Rules 5 and 6: one row per group of unexplained cells larger than BLOB_MAX_PX pixels."""
    cr, co = cell_contrast(remix), cell_contrast(orig)
    mask = (cr >= PUFF_STEPS) & (co < ORIG_STEPS)
    rows, skipped = [], {"hud": 0}
    h, w = mask.shape
    seen = np.zeros_like(mask)
    for y0, x0 in zip(*np.nonzero(mask)):
        if seen[y0, x0]:
            continue
        stack, cells = [(y0, x0)], []
        seen[y0, x0] = True
        while stack:
            y, x = stack.pop()
            cells.append((y, x))
            for yy, xx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= yy < h and 0 <= xx < w and mask[yy, xx] and not seen[yy, xx]:
                    seen[yy, xx] = True
                    stack.append((yy, xx))
        if len(cells) * CELL * CELL <= BLOB_MAX_PX:
            continue
        ys, xs = np.array(cells).T
        k = int(np.argmax(cr[ys, xs]))
        py, px = int(ys[k] * CELL + CELL // 2), int(xs[k] * CELL + CELL // 2)
        if hud is not None and hud[py, px]:
            skipped["hud"] += 1
            continue
        rows.append({"x": px, "y": py, "area": len(cells) * CELL * CELL, "x0": int(xs.min() * CELL),
                     "y0": int(ys.min() * CELL), "x1": int(xs.max() * CELL + CELL), "y1": int(ys.max() * CELL + CELL),
                     "remix": float(cr[ys, xs].mean()), "orig": float(co[ys, xs].mean())})
    return rows, skipped


def hud_mask(stills):
    """Pixels that stand out at the same screen place in at least half of the stills (the HUD), grown by 2 px, with
    enclosed holes filled; None when there are fewer than HUD_MIN_STILLS stills to tell the HUD from the scene."""
    if len(stills) < HUD_MIN_STILLS:
        return None
    m = (np.stack([contrast(s)[0] for s in stills]) >= ORIG_STEPS).mean(axis=0) >= 0.5
    grown = m.copy()
    for dy in range(-2, 3):
        for dx in range(-2, 3):
            grown |= np.roll(np.roll(m, dy, 0), dx, 1)
    outside = np.zeros_like(grown)
    stack = [(y, x) for y in range(H) for x in (0, W - 1)] + [(y, x) for x in range(W) for y in (0, H - 1)]
    stack = [(y, x) for y, x in stack if not grown[y, x]]
    for y, x in stack:
        outside[y, x] = True
    while stack:
        y, x = stack.pop()
        for yy, xx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
            if 0 <= yy < H and 0 <= xx < W and not grown[yy, xx] and not outside[yy, xx]:
                outside[yy, xx] = True
                stack.append((yy, xx))
    return ~outside


def spot_hud(spot: pathlib.Path):
    """HUD mask from the spot's turn stills (windows named y<yaw>_p<pitch>, each at a different camera direction)."""
    wins = json.loads((spot / "spot.json").read_text()).get("windows", [])
    return hud_mask([still(spot / w["frames"]) for w in wins if re.fullmatch(r"y-?\d+_p-?\d+", w["name"])
                     and w.get("frames") and (spot / w["frames"]).exists()])


def spot_still(spot: pathlib.Path, name: str):
    """(mean still, camera x,y,z,yaw,pitch) of window `name` in a gate spot folder: frames from spot.json, camera as the
    median over the window's proxy frames of the render camera in proxy_frames.csv column 7 (as stray_balls.py)."""
    win = next((w for w in json.loads((spot / "spot.json").read_text()).get("windows", []) if w["name"] == name), None)
    if not win or not win.get("frames") or not win.get("proxy"):
        raise SystemExit(f"{spot}: no window {name} with frames and proxy frames")
    lo, hi = win["proxy"]
    cams = []
    for line in (spot / "proxy_frames.csv").open(errors="replace"):
        c = line.rstrip("\n").split(",")
        if len(c) < 7 or not c[0].isdigit() or not lo <= int(c[0]) <= hi or not c[6]:
            continue
        for pub in c[6].split("|"):
            p = pub.split(":")
            if len(p) >= 9 and p[2] == "0":
                cams.append([float(v) for v in p[3:9]])
                break
    if not cams:
        raise SystemExit(f"{spot}: no camera rows for window {name}")
    m = np.median(np.array(cams), axis=0)
    pose = np.array([*m[:3], math.degrees(math.atan2(m[4], m[3])), math.degrees(math.asin(np.clip(m[5], -1, 1)))])
    return still(spot / win["frames"]), pose


def pose_ok(spec):
    a, b = spec if not isinstance(spec, str) else [np.array([float(v) for v in s.split(",")]) for s in spec.split(";")]
    dist = float(np.linalg.norm(a[:3] - b[:3]))
    dyaw = abs((a[3] - b[3] + 180) % 360 - 180)
    dpitch = abs(a[4] - b[4])
    return dist <= POSE_CM and dyaw <= POSE_DEG and dpitch <= POSE_PITCH_DEG, dist, dyaw, dpitch


def report(rows, skipped, out=print) -> bool:
    bad = [r for r in rows if not r["matched"]]
    ok = len(bad) <= ALLOWED
    skip = ", ".join(f"{v} {k}" for k, v in skipped.items())
    out(f"glow_blobs: unmatched {len(bad)} of {len(rows)} blobs (skipped {skip}; excess >= {BLOB_STEPS:g} steps and "
        f"original bump < {ORIG_STEPS:g}): {'PASS' if ok else 'FAIL'}")
    for r in sorted(bad, key=lambda r: -r["excess"]):
        out(f"  unmatched at x {r['x']} y {r['y']}: area {r['area']} px, Remix contrast {r['peak']:.1f}, "
            f"original bump {r['orig']:.1f} x gain {r['gain']:.2f}, excess {r['excess']:.1f}")
    return ok


def report_puffs(rows, skipped, out=print) -> None:
    out(f"glow_blobs: puffs {len(rows)} (bright areas over {BLOB_MAX_PX} px, Remix cell contrast >= {PUFF_STEPS:g} "
        f"where the original's stays < {ORIG_STEPS:g}; skipped {skipped['hud']} hud): report only")
    for r in sorted(rows, key=lambda r: -r["area"]):
        out(f"  puff at x {r['x']} y {r['y']} (box {r['x0']},{r['y0']} to {r['x1']},{r['y1']}): area {r['area']} px, "
            f"Remix contrast {r['remix']:.1f}, original {r['orig']:.1f}")


def report_lost(rows, skipped, out=print) -> bool:
    bad = [r for r in rows if r["lost"]]
    ok = len(bad) <= ALLOWED
    skip = ", ".join(f"{v} {k}" for k, v in skipped.items())
    out(f"glow_blobs: lost {len(bad)} of {len(rows)} original glows (skipped {skip}; Remix bump < {ORIG_STEPS:g} and "
        f"Remix luma < {KEEP_FRAC:g} x the original's, gain-matched from contrast {STRONG_STEPS:g}): "
        f"{'PASS' if ok else 'FAIL'}")
    for r in sorted(bad, key=lambda r: -r["peak"]):
        out(f"  lost at x {r['x']} y {r['y']}: area {r['area']} px, original contrast {r['peak']:.1f} luma "
            f"{r['orig_luma']:.0f} x gain {r['gain']:.2f}, Remix bump {r['remix_bump']:.1f} luma {r['remix_top']:.0f} "
            f"(needed {r['need']:.0f})")
    return ok


def dimmed_lamps(orig, on, changed, on_again, hud=None):
    """Rule 9: rows for every strong original glow, with "dimmed" set when the change takes Remix's glow there away."""
    co, bo = contrast(orig)
    skipped = {"big": 0, "border": 0, "hud": 0, "area edge": 0}
    near = lambda y, x: np.hypot(YY - y, XX - x) <= MATCH_PX
    cs = [contrast(img)[0] for img in (on, changed, on_again)]
    rows = []
    for area, y, x, peak, d in small_blobs(orig, co, bo, hud, skipped):
        if peak < STRONG_STEPS:
            continue
        a, b, c = (float(ci[near(y, x)].max()) for ci in cs)
        noise = abs(a - c)
        rows.append({"x": x, "y": y, "peak": peak, "on": a, "changed": b, "on_again": c,
                     "shown": a >= ORIG_STEPS, "dimmed": a >= ORIG_STEPS and a - b > max(DROP_MIN, 3 * noise)})
    return rows, skipped


def report_dimmed(rows, out=print) -> bool:
    bad = [r for r in rows if r["dimmed"]]
    ok = len(bad) <= ALLOWED
    out(f"glow_blobs: dimmed {len(bad)} of {len(rows)} lamps the original shows ({sum(r['shown'] for r in rows)} shown "
        f"by Remix before the change; drop > max({DROP_MIN:g}, 3 x |on - on_again|)): {'PASS' if ok else 'FAIL'}")
    for r in rows:
        out(f"  lamp at x {r['x']} y {r['y']}: original contrast {r['peak']:.0f}, Remix glow on {r['on']:.0f}, "
            f"changed {r['changed']:.0f}, on_again {r['on_again']:.0f}{'  DIMMED' if r['dimmed'] else ''}")
    return ok


def check_all(remix, orig, hud, out=print):
    """All three counts; returns (verdict, (orbs unmatched, puffs, lost)). The verdict is orbs and lost glows only."""
    out(f"glow_blobs: hud mask {'none' if hud is None else f'{int(hud.sum())} px'}")
    rows, skipped = judge(remix, orig, hud)
    ok1 = report(rows, skipped, out)
    prow, pskip = puffs(remix, orig, hud)
    report_puffs(prow, pskip, out)
    lrow, lskip = lost_glows(remix, orig, hud)
    ok3 = report_lost(lrow, lskip, out)
    ok = ok1 and ok3
    n = (sum(not r["matched"] for r in rows), len(prow), sum(r["lost"] for r in lrow))
    out(f"glow_blobs: {'PASS' if ok else 'FAIL'} (orbs unmatched {n[0]}, lost glows {n[2]}; puffs {n[1]} report only)")
    return ok, n


def crops(rows, remix, orig, outdir: pathlib.Path, k=3, half=20, scale=6):
    """Remix | original crop pairs around the k blobs with the largest excess."""
    from PIL import Image
    outdir.mkdir(parents=True, exist_ok=True)
    paths = []
    for i, r in enumerate(sorted((r for r in rows if not r["matched"]), key=lambda r: -r["excess"])[:k]):
        y0, x0 = max(0, r["y"] - half), max(0, r["x"] - half)
        a = remix[y0:y0 + 2 * half, x0:x0 + 2 * half]
        b = orig[y0:y0 + 2 * half, x0:x0 + 2 * half]
        pair = np.concatenate([a, np.full((a.shape[0], 2), 255.0), b], axis=1)
        img = Image.fromarray(np.clip(pair, 0, 255).astype(np.uint8)).resize(
            (pair.shape[1] * scale, pair.shape[0] * scale), Image.NEAREST)
        p = outdir / f"glow_blob_{i + 1}_x{r['x']}_y{r['y']}.png"
        img.save(p)
        paths.append(p)
    return paths


# ---- self-test: fake pictures ---------------------------------------------------------------------------------

def _scene(seed=1):
    rng = np.random.default_rng(seed)
    img = 40 + 20 * (XX / W) + rng.normal(0, 1.5, (H, W))   # a dim wall with a gradient and noise
    img[:, 250:] += 60                                       # a lit wall area much bigger than an orb
    return img


def _dot(img, x, y, amp=80.0, sigma=1.6):
    return img + amp * np.exp(-((XX - x) ** 2 + (YY - y) ** 2) / (2 * sigma ** 2))


def self_test() -> int:
    fails = []

    def tally(name, good, line):
        print(f"{'ok  ' if good else 'FAIL'} {name}: {line}")
        if not good:
            fails.append(name)

    def case(name, remix, orig, want_ok, want_unmatched):
        rows, skipped = judge(remix.astype(np.float32), orig.astype(np.float32))
        lines = []
        ok = report(rows, skipped, out=lines.append)
        tally(name, ok == want_ok and sum(not r["matched"] for r in rows) == want_unmatched, lines[0])

    def puff_case(name, remix, orig, want):
        rows, skipped = puffs(remix.astype(np.float32), orig.astype(np.float32))
        lines = []
        report_puffs(rows, skipped, out=lines.append)
        tally(name, len(rows) == want, lines[0])

    def lost_case(name, remix, orig, want, hud=None):
        rows, skipped = lost_glows(remix.astype(np.float32), orig.astype(np.float32), hud)
        lines = []
        report_lost(rows, skipped, out=lines.append)
        tally(name, sum(r["lost"] for r in rows) == want, lines[0])

    base = _scene()
    orig = base * 0.4                                         # the original is darker overall
    lamp_r, lamp_o = _dot(base, 100, 60), _dot(orig, 101, 61, amp=32)
    case("lamp in both pictures (original 2.5x darker, 1 px pose shift) is not counted", lamp_r, lamp_o, True, 0)
    case("orb on a blank dark wall that the original does not show is counted",
         _dot(lamp_r, 200, 120), lamp_o, False, 1)
    case("no orb: Remix and original agree", base, orig, True, 0)
    case("dim orb under the threshold is not counted", _dot(base, 160, 90, amp=15), orig, True, 0)
    case("a lamp the original shows 10 px away does not excuse an orb here",
         _dot(base, 160, 90), _dot(orig, 170, 90, amp=32), False, 1)
    case("Remix brightens a highlight the original also has (gain-matched) is not counted",
         _dot(base, 120, 100, amp=40), _dot(orig, 120, 100, amp=14), True, 0)
    case("Remix makes a lamp far brighter than the original's clear lamp: not counted",
         _dot(base, 120, 100, amp=150), _dot(orig, 120, 100, amp=14), True, 0)
    case("the original's faint texture bump (under 12 steps) does not excuse an orb",
         _dot(base, 120, 100, amp=150), _dot(orig, 120, 100, amp=6), False, 1)
    case("a big bright area (window) is skipped, not counted",
         base + np.where((abs(XX - 60) < 15) & (abs(YY - 40) < 15), 90, 0), orig, True, 0)
    case("an orb in the 3 px border is skipped", _dot(base, 1, 90), orig, True, 0)
    ok, *_ = pose_ok("0,0,0,-90,0;0,0,0,-87,0")
    tally("a 3 degree pose difference is refused", not ok, "pose_ok says " + str(ok))
    ok, *_ = pose_ok("0,0,0,-90,0;0,0,0,-90,1.2")
    tally("a 1.2 degree pitch difference is refused", not ok, "pose_ok says " + str(ok))
    ok, *_ = pose_ok("0,0,0,-90,0;0,10,0,-90,0")
    tally("a camera 10 cm to the side is refused", not ok, "pose_ok says " + str(ok))

    # Puffs (rules 5 and 6).
    disc = lambda img, x, y, amp=50.0, r=12: img + amp / (1 + np.exp((np.hypot(XX - x, YY - y) - r) / 1.5))
    puff_case("a soft disc of radius 12 px that only Remix shows is one puff", disc(base, 150, 90), orig, 1)
    puff_case("the same disc in both pictures (original 2.5x darker) is not a puff",
              disc(base, 150, 90), disc(orig, 150, 90, amp=20), 0)
    puff_case("a small orb is not a puff (the orb count has it)", _dot(base, 150, 90), orig, 0)
    puff_case("no puff: Remix and original agree", base, orig, 0)
    lines = []
    pv, pn = check_all(disc(base, 150, 90).astype(np.float32), orig.astype(np.float32), None, out=lines.append)
    tally("a puff alone is report only: the verdict stays PASS with puffs 1", pv and pn == (0, 1, 0), lines[-1])

    # Lost glows (rules 7 and 8).
    lamp = _dot(orig, 100, 60, amp=120)                       # a strong lamp glow in the original, contrast about 120
    lost_case("a lamp the original shows on a dark wall, Remix shows only the wall (2.5x) is lost", base, lamp, 1)
    lost_case("a lamp on a wall Remix lights 6x (above 0.75 x the lamp's own luma) is still lost", base * 2.5, lamp, 1)
    lost_case("Remix shows the lamp (brighter) is not lost", _dot(base, 100, 60, amp=150), lamp, 0)
    lost_case("Remix shows the lamp 2 px off is not lost", _dot(base, 102, 61, amp=60), lamp, 0)
    merged = np.where((abs(XX - 100) < 20) & (abs(YY - 60) < 20), 230.0, base)
    lost_case("the lamp merged into a Remix area of 230 luma is not lost", merged, lamp, 0)
    lost_case("a weak original highlight (contrast 60, a buckle or glint) on a wall Remix lights brighter is not lost",
              base * 2.5, _dot(orig, 100, 60, amp=60), 0)
    hud = (np.hypot(XX - 100, YY - 60) <= 8)
    lost_case("an original-only glow on the HUD mask is not lost", base, lamp, 0, hud)
    lost_case("no glow lost: Remix and original agree", base, orig, 0)

    # Dimmed lamps (rule 9).
    def dim_case(name, on, changed, on_again, want):
        rows, _ = dimmed_lamps(lamp.astype(np.float32), on.astype(np.float32), changed.astype(np.float32),
                               on_again.astype(np.float32))
        lines = []
        report_dimmed(rows, out=lines.append)
        tally(name, sum(r["dimmed"] for r in rows) == want, lines[0])

    shown = _dot(base, 100, 60, amp=80)
    dim_case("the change takes away Remix's glow at a lamp the original shows: dimmed", shown, base, shown, 1)
    dim_case("the change keeps the glow: not dimmed", shown, shown, shown, 0)
    dim_case("the change halves the glow (80 to 40): dimmed", shown, _dot(base, 100, 60, amp=40), shown, 1)
    dim_case("the drop is inside the run's own on / on_again difference: not dimmed", shown, base, base, 0)
    dim_case("Remix never showed the lamp (the lost count's job): not dimmed", base, base, base, 0)

    # Real pictures (coordinator, Nov 16): run 20261115-142638 con_bright_hall against the original game's picture in
    # reference-20261116-000454 at the same pose. Missing data fails the self-test rather than skipping it.
    global STRONG_STEPS
    gate = pathlib.Path(r"D:\BlacklistOverhaul\experiments\remix-gate")
    rx_spot, og_spot = gate / "20261115-142638" / "con_bright_hall", gate / "reference-20261116-000454" / "con_bright_hall"
    try:
        og_hud = spot_hud(og_spot)
        og_y0, og_y0_pose = spot_still(og_spot, "y0_p0")

        def real(name, remix_win, want, check=None, orig=(og_y0, og_y0_pose), spot=rx_spot, edit=None):
            """want: (verdict, orbs unmatched, puffs, lost), each None for "any"."""
            img, pose = spot_still(spot, remix_win)
            if edit:
                img = edit(img)
            pok, *_ = pose_ok((pose, orig[1]))
            lines = []
            ok, n = check_all(img, orig[0], og_hud, out=lines.append)
            got = (ok, *n)
            good = pok and all(w is None or w == g for w, g in zip(want, got)) and (check is None or check(img, orig[0]))
            tally(name, good, ("" if pok else "pose mismatch; ") + lines[-1])

        tally("the HUD mask is built from the original spot's turn stills", og_hud is not None and og_hud.sum() > 500,
              f"{'none' if og_hud is None else int(og_hud.sum())} px")
        real("142638 y0_p0 (stand-in balls on) fails on orbs", "y0_p0", (False, 12, None, 0))
        real("142638 switch step with the balls off (FlareStandIns=0) passes all three counts",
             "switch_flare_balls_off", (True, 0, 0, 0))
        real("142638 switch_base fails, and has a puff", "switch_base", (False, None, None, 0),
             lambda r, o: len(puffs(r, o, og_hud)[0]) >= 1)
        lamp = lambda r, o: any(abs(x["x"] - 196) <= 3 and abs(x["y"] - 57) <= 3 and x["matched"]
                                for x in judge(r, o, og_hud)[0])
        real("142638 y180_p0: the ceiling strip lamp at x 196 y 57, which the original shows, is judged and not counted",
             "y180_p0", (False, None, None, 0), lamp, spot_still(og_spot, "y180_p0"))
        real("063147 y0_p0 (scene black while shaders compile, only glows drawn) fails the lost count",
             "y0_p0", (False, None, None, None), lambda r, o: sum(x["lost"] for x in lost_glows(r, o, og_hud)[0]) >= 3,
             spot=gate / "20261116-063147" / "con_bright_hall")

        def erase(x, y, r=9):
            def f(img):
                return np.where(np.hypot(YY - y, XX - x) <= r, contrast(img)[1], img)
            return f
        # The original's ceiling lamp at x 37 y 8 (contrast 129, luma 137) erased from the Remix still: Remix's wall
        # there is brighter than 0.75 x 137, so only the gain-matched rule for strong glows sees it go.
        real("142638 y0_p0 with the original's ceiling lamp at x 37 y 8 erased from Remix: lost 1",
             "y0_p0", (False, 12, None, 1), edit=erase(37, 8))
        saved, STRONG_STEPS = STRONG_STEPS, float("inf")
        try:
            real("broken version (no gain matching for strong glows) misses that erased lamp: lost 0",
                 "y0_p0", (False, 12, None, 0), edit=erase(37, 8))
        finally:
            STRONG_STEPS = saved

        # Dimmed lamps on real pictures: the original's ceiling lamp at x 214 y 57 in y225_p0 sits on a wall Remix
        # lights above the lost count's need, so erasing Remix's glow there is missed by the lost count.
        og_225 = spot_still(og_spot, "y225_p0")
        rx_225, _ = spot_still(rx_spot, "y225_p0")
        gone = erase(214, 57)(rx_225)
        n_lost = sum(r["lost"] for r in lost_glows(gone, og_225[0], og_hud)[0])
        tally("142638 y225_p0 with Remix's lamp glow at x 214 y 57 erased: the lost count misses it (lost 0)",
              n_lost == 0, f"lost {n_lost}")
        for name, ch, ag, want in (("unedited (changed = on = on_again): dimmed 0", rx_225, rx_225, 0),
                                   ("that lamp erased in the changed step: dimmed 1", gone, rx_225, 1),
                                   ("erased in on_again too, so the drop is inside the noise: dimmed 0", gone, gone, 0)):
            rows, _ = dimmed_lamps(og_225[0], rx_225, ch, ag, og_hud)
            lines = []
            report_dimmed(rows, out=lines.append)
            n = sum(r["dimmed"] for r in rows)
            hit = any(r["dimmed"] and abs(r["x"] - 214) <= 3 and abs(r["y"] - 57) <= 3 for r in rows)
            tally("142638 y225_p0 " + name, n == want and (want == 0 or hit), lines[0])
        sweep = [r for r in dimmed_lamps(og_225[0], rx_225, rx_225, rx_225, og_hud)[0] if r["shown"]]
        caught = sum(any(q["dimmed"] and q["x"] == r["x"] and q["y"] == r["y"] for q in
                         dimmed_lamps(og_225[0], rx_225, erase(r["x"], r["y"])(rx_225), rx_225, og_hud)[0])
                     for r in sweep)
        tally("142638 y225_p0 erase sweep: every lamp Remix shows is caught when erased", caught == len(sweep) > 0,
              f"{caught} of {len(sweep)}")
        # Real change: the balls-off switch step at y0_p0. No lamp there is close, so 0 here does not prove the
        # balls are unneeded; it only shows the count does not fire on a real change that keeps the lamps.
        on, _ = spot_still(rx_spot, "switch_base")
        off, _ = spot_still(rx_spot, "switch_flare_balls_off")
        again, _ = spot_still(rx_spot, "switch_base_again")
        rows, _ = dimmed_lamps(og_y0, on, off, again, og_hud)
        lines = []
        report_dimmed(rows, out=lines.append)
        tally("142638 balls off at y0_p0 dims no lamp the original shows (no close lamp in this view)",
              sum(r["dimmed"] for r in rows) == 0 and len(rows) > 0, lines[0])

        # Pose tolerance (POSE_DEG, POSE_PITCH_DEG): inside it the erased lamp at x 214 y 57 is still caught, outside it
        # the comparison breaks. Remix is shifted against the original y225_p0 of reference-20261116-191120.
        ref3 = gate / "reference-20261116-191120" / "con_bright_hall"
        o3, o3_pose = spot_still(ref3, "y225_p0")

        def moved(dx, dy):
            img = np.roll(rx_225, (dy, dx), (0, 1))
            img[:, :dx], img[:dy] = rx_225[:, :1], rx_225[:1]
            rows = dimmed_lamps(o3, img, erase(214 + dx, 57 + dy)(img), img, og_hud)[0]
            return any(r["dimmed"] and abs(r["x"] - 214) <= 3 and abs(r["y"] - 57) <= 3 for r in rows)
        tally("Remix 5 px sideways (1.8 deg yaw at the centre): the erased lamp at x 214 y 57 is still dimmed",
              moved(5, 0), "")
        tally("Remix 3 px down (1.1 deg pitch): still dimmed", moved(0, 3), "")
        tally("outside the tolerance, Remix 8 px sideways (2.9 deg): the erased lamp is missed", not moved(8, 0), "")
        b3, b3_pose = spot_still(ref3, "lampsB")
        pok, dist, dyaw, dpitch = pose_ok((b3_pose, o3_pose))
        false = sum(r["lost"] for r in lost_glows(b3, o3, og_hud)[0]) + sum(not r["matched"] for r in judge(b3, o3, og_hud)[0])
        tally("two original stills 3.2 deg apart (191120 lampsB against y225_p0): refused, and compared anyway they "
              "count glows and orbs that are not there", not pok and dyaw > 3 and false >= 1,
              f"yaw {dyaw:.2f} deg, false counts {false}")
    except (OSError, SystemExit) as e:
        tally("real-picture cases", False, f"data missing ({e})")
    print("self-test:", "all passed" if not fails else f"FAILED {fails}")
    return 1 if fails else 0


def main():
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    ap = argparse.ArgumentParser()
    ap.add_argument("--remix", type=pathlib.Path)
    ap.add_argument("--orig", type=pathlib.Path)
    ap.add_argument("--pose")
    ap.add_argument("--remix-spot", type=pathlib.Path)
    ap.add_argument("--orig-spot", type=pathlib.Path)
    ap.add_argument("--still")
    ap.add_argument("--orig-still")
    ap.add_argument("--crops", type=pathlib.Path)
    ap.add_argument("--dimmed", help="window name of the changed Remix step (fourth count only)")
    ap.add_argument("--again", help="window name of the Remix step with the change undone")
    a = ap.parse_args()
    pose, hud = a.pose, None
    if a.dimmed:
        if not (a.remix_spot and a.orig_spot and a.still and a.again):
            ap.error("--dimmed needs --remix-spot, --orig-spot, --still and --again")
        on, rpose = spot_still(a.remix_spot, a.still)
        orig, opose = spot_still(a.orig_spot, a.orig_still or a.still)
        changed, cpose = spot_still(a.remix_spot, a.dimmed)
        again, apose = spot_still(a.remix_spot, a.again)
        for name, p in (("original", opose), ("changed", cpose), ("on_again", apose)):
            ok, dist, dyaw, dpitch = pose_ok((rpose, p))
            if not ok:
                print(f"glow_blobs: NOT MEASURED, {name} pose differs from on by {dist:.0f} cm, yaw {dyaw:.2f}, "
                      f"pitch {dpitch:.2f} deg")
                sys.exit(2)
        rows, _ = dimmed_lamps(orig, on, changed, again, spot_hud(a.orig_spot))
        sys.exit(0 if report_dimmed(rows) else 1)
    if a.remix_spot and a.orig_spot and a.still:
        remix, rpose = spot_still(a.remix_spot, a.still)
        orig, opose = spot_still(a.orig_spot, a.orig_still or a.still)
        pose, hud = (rpose, opose), spot_hud(a.orig_spot)
    elif a.remix and a.orig:
        remix, orig = still(a.remix), still(a.orig)
    else:
        ap.error("give --remix and --orig, or --remix-spot, --orig-spot and --still")
    if pose is not None:
        ok, dist, dyaw, dpitch = pose_ok(pose)
        if not ok:
            print(f"glow_blobs: NOT MEASURED, poses differ by {dist:.0f} cm, yaw {dyaw:.2f}, pitch {dpitch:.2f} deg")
            sys.exit(2)
    ok, _ = check_all(remix, orig, hud)
    if a.crops:
        for p in crops(judge(remix, orig, hud)[0], remix, orig, a.crops):
            print(f"crop {p}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
