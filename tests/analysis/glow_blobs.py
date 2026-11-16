"""Symptom check for problem ledger row 2 ("orbs with no lamp, floating or shining through walls"), pictures only.

Worker B, Nov 16. Finds small bright blobs in the Remix picture that the original game's picture at the same pose does
not show. It never reads a light list, a flare list or the stand-in ball list, so it judges what Corey sees whatever
the cause turns out to be.

Inputs: two stills of the same pose, each a .gray file (320x180 u8 luma, one or more frames; their mean is used).
  --remix   the Remix still          --orig   the original game's still (a no-Remix run)
  --pose    optional: "x,y,z,yaw,pitch;x,y,z,yaw,pitch" for the two cameras (from proxy_frames.csv); the check
            refuses to judge (NOT MEASURED, exit 2) when they differ by more than POSE_CM or POSE_DEG.

Rule:
  1. Local contrast of each picture: the pixel minus the median of the 25x25 window around it (its background).
  2. A Remix blob is a 4-connected group of pixels whose contrast is at least BLOB_STEPS (0..255 luma steps), at most
     BLOB_MAX_PX pixels, with its brightest pixel at least BORDER_PX inside the frame. A blob whose ring 6 to 12 px
     out is more than RING_BRIGHT_MAX bright (above halfway between background and peak) is the edge or corner of a
     bigger bright area (window, lit wall, sky), not an orb, and is skipped.
  3. Remix lights the whole scene brighter than the original (picture means 118 against 47 at con_bright_hall), so
     the original is compared after matching brightness locally: gain = Remix background / original background at
     the blob, clamped to 1..GAIN_MAX. The original's bump is its highest contrast within MATCH_PX of the blob's peak
     (MATCH_PX covers a 1.5 degree pose difference: 1.5 / 90 x 320 = 5.3 px).
  4. The original shows the blob when its bump reaches ORIG_STEPS (a distinct bright spot of its own), or when
     excess = Remix peak contrast - gain x original bump stays under BLOB_STEPS (Remix only brightened what the
     original has). Any other blob is counted. PASS when the count is at most ALLOWED, else FAIL.
Output: one line "glow_blobs: unmatched N of M blobs (...)", then one line per unmatched blob.

  --remix-spot / --orig-spot / --still [--orig-still]: instead of the files, two gate spot folders and a window name;
            the stills and both cameras come from spot.json and proxy_frames.csv, and the pose test always runs.
Exit codes: 0 PASS, 1 FAIL, 2 NOT MEASURED.
Limits: blobs over BLOB_MAX_PX and the rim of larger bright areas are skipped, so a large soft puff or a starburst
merged into a bright area is not counted; the skipped counts are printed.

Usage: glow_blobs.py --remix R.gray --orig O.gray [--pose P] [--crops DIR]
       glow_blobs.py --remix-spot DIR --orig-spot DIR --still NAME [--orig-still NAME] [--crops DIR]
       glow_blobs.py --self-test
"""
import argparse, json, math, pathlib, sys
import numpy as np

W, H = 320, 180
WIN = 25
BLOB_STEPS, ORIG_STEPS, BLOB_MAX_PX, MATCH_PX, BORDER_PX, ALLOWED = 24.0, 12.0, 200, 6, 3, 0
GAIN_MAX, RING_BRIGHT_MAX = 6.0, 0.2
POSE_CM, POSE_DEG = 25.0, 1.5


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


def judge(remix: np.ndarray, orig: np.ndarray):
    """Rows for every candidate blob, and counts of the blobs skipped by each rule."""
    cr, br = contrast(remix)
    co, bo = contrast(orig)
    rows, skipped = [], {"big": 0, "border": 0, "area edge": 0}
    for area, (y, x), peak in blobs(cr):
        if area > BLOB_MAX_PX:
            skipped["big"] += 1
            continue
        if not (BORDER_PX <= x < W - BORDER_PX and BORDER_PX <= y < H - BORDER_PX):
            skipped["border"] += 1
            continue
        d = np.hypot(YY - y, XX - x)
        ring = remix[(d >= 6) & (d <= 12)]
        if np.mean(ring > br[y, x] + 0.5 * peak) > RING_BRIGHT_MAX:
            skipped["area edge"] += 1
            continue
        orig_bump = float(co[d <= MATCH_PX].max())
        gain = float(np.clip((br[y, x] + 1.0) / (bo[y, x] + 1.0), 1.0, GAIN_MAX))
        excess = peak - gain * max(orig_bump, 0.0)
        rows.append({"x": x, "y": y, "area": area, "peak": peak, "orig": orig_bump, "gain": gain,
                     "excess": excess, "matched": orig_bump >= ORIG_STEPS or excess < BLOB_STEPS})
    return rows, skipped


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
    return dist <= POSE_CM and dyaw <= POSE_DEG and dpitch <= POSE_DEG, dist, dyaw, dpitch


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

    def case(name, remix, orig, want_ok, want_unmatched):
        rows, skipped = judge(remix.astype(np.float32), orig.astype(np.float32))
        lines = []
        ok = report(rows, skipped, out=lines.append)
        n = sum(not r["matched"] for r in rows)
        good = ok == want_ok and n == want_unmatched
        print(f"{'ok  ' if good else 'FAIL'} {name}: {lines[0]}")
        if not good:
            fails.append(name)

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
    print(f"{'ok  ' if not ok else 'FAIL'} a 3 degree pose difference is refused")
    if ok:
        fails.append("pose")

    # Real pictures (coordinator, Nov 16): run 20261115-142638 con_bright_hall against the original game's picture in
    # reference-20261116-000454 at the same pose. Missing data fails the self-test rather than skipping it.
    gate = pathlib.Path(r"D:\BlacklistOverhaul\experiments\remix-gate")
    rx_spot, og_spot = gate / "20261115-142638" / "con_bright_hall", gate / "reference-20261116-000454" / "con_bright_hall"
    try:
        og_y0, og_y0_pose = spot_still(og_spot, "y0_p0")

        def real(name, remix_win, want_ok, check=None, orig=(og_y0, og_y0_pose)):
            img, pose = spot_still(rx_spot, remix_win)
            pok, *_ = pose_ok((pose, orig[1]))
            rows, skipped = judge(img, orig[0])
            lines = []
            ok = report(rows, skipped, out=lines.append)
            good = pok and ok == want_ok and (check is None or check(rows))
            print(f"{'ok  ' if good else 'FAIL'} {name}: {'' if pok else 'pose mismatch; '}{lines[0]}")
            if not good:
                fails.append(name)

        real("142638 y0_p0 (stand-in balls on) fails", "y0_p0", False)
        real("142638 switch step with the balls off (FlareStandIns=0) passes", "switch_flare_balls_off", True)
        lamp = lambda rows: any(abs(r["x"] - 196) <= 3 and abs(r["y"] - 57) <= 3 and r["matched"] for r in rows)
        real("142638 y180_p0: the ceiling strip lamp at x 196 y 57, which the original shows, is judged and not counted",
             "y180_p0", False, lamp, spot_still(og_spot, "y180_p0"))
    except (OSError, SystemExit) as e:
        print(f"FAIL real-picture cases: data missing ({e})")
        fails.append("real-picture data")
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
    a = ap.parse_args()
    pose = a.pose
    if a.remix_spot and a.orig_spot and a.still:
        remix, rpose = spot_still(a.remix_spot, a.still)
        orig, opose = spot_still(a.orig_spot, a.orig_still or a.still)
        pose = (rpose, opose)
    elif a.remix and a.orig:
        remix, orig = still(a.remix), still(a.orig)
    else:
        ap.error("give --remix and --orig, or --remix-spot, --orig-spot and --still")
    if pose is not None:
        ok, dist, dyaw, dpitch = pose_ok(pose)
        if not ok:
            print(f"glow_blobs: NOT MEASURED, poses differ by {dist:.0f} cm, yaw {dyaw:.2f}, pitch {dpitch:.2f} deg")
            sys.exit(2)
    rows, skipped = judge(remix, orig)
    ok = report(rows, skipped)
    if a.crops:
        for p in crops(rows, remix, orig, a.crops):
            print(f"crop {p}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
