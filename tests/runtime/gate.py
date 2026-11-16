"""Hard numeric pass/fail for one verify_runtime run.  usage: gate.py <run folder> [--previous <run folder>] [--reference <run folder>]  (exit 0 = PASS, 1 = FAIL)

Reads <run>/manifest.json (the spots asked for) and each <run>/<spot>/spot.json (written by drive.py: timed windows
with frame ranges, plus the data files copied from the game), and writes <run>/result.json and <run>/result.md.
Every check is PASS, WARN, FAIL or NOT MEASURED. WARN is shown and does not fail the gate. NOT MEASURED does: a check whose data is missing or
incomplete never passes. Frame numbers restart at every game launch, so each spot is judged on its own files.

Checks (thresholds in THRESHOLDS):
  nan_pixels               non-finite pixels in Remix's final image dumps (<tag>_final.pfm), all sweep stops
  neon_fraction            worst dump's share of clipped, strongly coloured pixels: max channel >= 1.0 and
                           (max - min) / max >= 0.5 (white highlights are allowed, neon colours are not)
  skinned_min              fewest active character meshes in any frame (telemetry skinnedInstances)
  outline_overlap          Sam's near-field depth outline, the game's own depth (= Remix off) against Remix's depth
                           of the same frame, before and after the close-camera sweep: share of the game's outline
                           pixels that have a Remix outline pixel within 2 px
  inward_fraction          worst frame's share of character triangles facing inward (telemetry
                           inwardSkinnedTriangles / skinnedTriangles)
  raster_fallback_frames   frames with a 3D scene where the ray-traced image was not shown (proxy gate CSV)
  flashes                  frames (runs of 1-3) that differ from both neighbours far more than the neighbours differ
                           from each other, after motion compensation, in still stops and the 360 pan
  still_flicker            sustained local flicker (worst 8x8 block median frame-to-frame |delta|) and slow whole-frame
                           brightness pulsing (trimmed mean-luma drift) across still camera stops
  static_blas_rebuilds     static-geometry acceleration structure rebuilds while the camera rotates (telemetry)
  frames_over_50ms         telemetry frameTimeMs > 50 in any measured window (goggle windows included: no freezes)
  min_median_fps           median FPS over the scene windows: WARN below 90 (Corey's target: a goal, not a limit)
  fps_regression           each spot's median FPS against the same spot in --previous: FAIL when more than 10% slower;
                           WARN when no previous run is given
  input_latency_ms         each spot's median mouse-to-screen time against the same spot in --reference (the no-Remix
                           run): FAIL when more than the GUESS margin above it; NOT MEASURED without --reference
  step_timing              per-step GPU times (telemetry passMs): untimed steps, and nested steps longer than their parent
  skip_occlusion_share     worst scene window's share of draws skipped by occlusionQuery (telemetry skips / proxy draws)
  skip_other_share         worst scene window's share of draws skipped for any other reason
  material_share           share of scene draws that used a mod replacement material, in the worst spot
  textures_demoted         share of scene frames where the texture budget shrank textures (telemetry texturesDemoted)
  clean_quit               no VK_ERROR_DEVICE_LOST in each spot's remix-dxvk.log (copied into the spot folder)
  pose                     the camera really went where it was told (proxy camera trace vs requested yaw/pitch)
  goggles                  each goggle mode toggled (proxy log) and looks the same with ray tracing on and off
                           (no missing geometry, no different picture)
Every per-frame check also requires the data to cover every frame of its windows.
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import sys

import numpy as np

THRESHOLDS = {
    "nan_pixels": 0, "neon_fraction": 0.001, "skinned_min": 1, "outline_overlap": 0.85, "inward_fraction": 0.005,
    "raster_fallback_frames": 0, "flashes": 0, "still_block_jitter": 4.0, "still_luma_drift": 6.0,
    "static_blas_rebuilds": 0,
    # Zero hitches (Corey, Nov 15: none outside loading screens). Loading screens are NOT excluded: nothing in
    # drive.py ends a window when a level load starts, so a load during a move, pan, combat or goggles window counts
    # as a hitch here. Not verified whether the game ever loads inside one; the teleport hold is outside every window.
    "frames_over_50ms": 0,
    "min_median_fps": 90.0,  # Corey's target, Nov 15: WARN below it, not a fail. Median FPS over the SCENE windows (goggles excluded).
    "max_wait_overhead_ms": 5.0,
    # Input lag (input_latency_ms) is judged per spot against the no-Remix reference run, not a fixed limit. The margin
    # is a GUESS until reference numbers exist (Corey, Nov 15: the original game has almost no noticeable lag).
    "input_lag_margin_ms": 5.0,
    # Speed FAIL (fps_regression): a spot whose median FPS is more than this below the same spot in the previous run.
    "max_fps_drop": 0.10,  # Corey, Nov 15
    "min_scene_luma": 0.03, "min_raster_luma_ratio": 0.75, "min_angled_sharpness": 0.02,
    "pose_error_deg": 3.0, "pan_degrees": 350.0, "goggles_corr": 0.8, "goggles_mean_gap": 0.06,
    "goggles_missing_blocks": 0.02, "still_frames": 15, "pan_coverage": 0.9,
    # Recorder checks (recorder_checks). Every limit below is a GUESS, not a value from the game or the source. The
    # comment beside each is what the Nov 14 run (20261114-153401) measured on that check; that run fails other checks.
    "step_untimed_share": 0.02,     # GUESS. Measured 0 of 4,069 scene frames with an untimed step.
    "step_child_excess_ms": 0.5,    # GUESS, tolerance for timer rounding. Measured: no child above its parent.
    "skip_share_occlusion": 0.6,    # GUESS. Measured worst scene window 54.6% skipped by occlusionQuery.
    "skip_share_other": 0.05,       # GUESS. Every other reason measured at most 0.24% of a window's draws.
    "min_replaced_share": 0.2,      # GUESS. Measured 81% and 90% with the mod loaded, 0% without it (Nov 14 bug).
    "max_demoted_share": 0.05,      # GUESS. Measured 0 scene frames with a texture shrunk by the budget.
    }
TELEMETRY_FIELDS = ("frame", "frameTimeMs", "gpuTimeMs", "instances", "skinnedInstances", "staticBlasRebuilds",
                    "cameraCut", "textureMemoryMB", "vramUsedMB")
THUMB_W, THUMB_H = 128, 72
PASS, WARN, FAIL, NOT_MEASURED = "PASS", "WARN", "FAIL", "NOT MEASURED"
MEASURED = ("still", "settle", "move", "pan", "combat", "goggles")
SCENE = ("still", "settle", "move", "pan", "combat")
ROTATING = ("move", "pan")

# --- files ----------------------------------------------------------------------------------------------------
def load_json(path: str):
    with open(path, encoding="utf-8-sig") as f:   # -sig: PowerShell 5.1 may write a byte-order mark
        return json.load(f)


def save_json(path: str, obj) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)


def save_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def read_lines(path: str, encoding: str = "utf-8") -> list[str]:
    with open(path, encoding=encoding, errors="replace") as f:
        return f.read().splitlines()


def read_pfm(path: str) -> np.ndarray:
    """Portable float map -> float32 array (H, W) or (H, W, 3), top row first."""
    with open(path, "rb") as f:
        kind = f.readline().strip()
        w, h = (int(v) for v in f.readline().split())
        scale = float(f.readline())
        channels = {b"PF": 3, b"Pf": 1}[kind]
        data = np.frombuffer(f.read(w * h * channels * 4), dtype="<f4" if scale < 0 else ">f4")
    img = data.reshape(h, w, channels)[::-1].astype(np.float32)
    return img[..., 0] if channels == 1 else img


def write_pfm(path: str, img: np.ndarray) -> None:
    img = np.asarray(img, np.float32)
    channels = 1 if img.ndim == 2 else 3
    h, w = img.shape[:2]
    with open(path, "wb") as f:
        f.write(f"{'Pf' if channels == 1 else 'PF'}\n{w} {h}\n-1.0\n".encode())
        f.write(img[::-1].astype("<f4").tobytes())


def read_gray(path: str, w: int, h: int) -> np.ndarray:
    """Raw 8-bit frames (ffmpeg -pix_fmt gray -f rawvideo) -> (N, h, w) float32."""
    raw = np.fromfile(path, np.uint8)
    n = raw.size // (w * h)
    return raw[: n * w * h].reshape(n, h, w).astype(np.float32)


def read_telemetry(path: str | None) -> tuple[dict[int, dict], list[str]]:
    """frame -> record, and the problems found (missing contract fields, unreadable lines)."""
    if not path or not os.path.exists(path):
        return {}, [f"telemetry file missing ({path})"]
    out, problems, missing = {}, [], set()
    for n, line in enumerate(read_lines(path), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            problems.append(f"line {n} is not JSON"); continue
        missing |= {k for k in TELEMETRY_FIELDS if k not in rec}
        if isinstance(rec.get("frame"), int):
            out[rec["frame"]] = rec
    if missing:
        problems.append("contract fields missing: " + ", ".join(sorted(missing)))
    return out, problems


def read_csv_frames(path: str | None, columns: int) -> dict[int, list[str]]:
    if not path or not os.path.exists(path):
        return {}
    out = {}
    for line in read_lines(path, "ascii"):
        parts = line.strip().split(",", columns - 1)
        if len(parts) >= 2 and parts[0].isdigit():
            out[int(parts[0])] = parts
    return out


# --- image math ---------------------------------------------------------------------------------------------
def spectrum(a: np.ndarray) -> np.ndarray:
    return np.fft.fft2(a - a.mean())


def shift(a: np.ndarray, b: np.ndarray, fa=None, fb=None) -> tuple[int, int]:
    """Integer (dy, dx) that moves b onto a (phase correlation); fa/fb = precomputed spectra."""
    f = (spectrum(a) if fa is None else fa) * np.conj(spectrum(b) if fb is None else fb)
    r = np.fft.ifft2(f / (np.abs(f) + 1e-6)).real
    dy, dx = np.unravel_index(np.argmax(r), r.shape)
    if dy > r.shape[0] // 2: dy -= r.shape[0]
    if dx > r.shape[1] // 2: dx -= r.shape[1]
    return int(dy), int(dx)


def mc_error(a: np.ndarray, b: np.ndarray, block: int = 8, fa=None, fb=None) -> float:
    """Motion-compensated difference: shift b onto a, then the 98th percentile of 8x8-block mean |a - b| (0-255).
    A percentile of blocks catches both a whole-frame flash and a local pop."""
    dy, dx = shift(a, b, fa, fb)
    m = max(abs(dy), abs(dx)) + 2
    if a.shape[0] <= 2 * m or a.shape[1] <= 2 * m:   # the shift leaves no area to compare: use no shift (not NaN)
        dy, dx, m = 0, 0, 2
    b = np.roll(b, (dy, dx), axis=(0, 1))
    d = np.abs(a - b)[m:-m, m:-m]
    h, w = (d.shape[0] // block) * block, (d.shape[1] // block) * block
    if h == 0 or w == 0:
        return float(d.mean())
    blocks = d[:h, :w].reshape(h // block, block, w // block, block).mean(axis=(1, 3))
    return float(np.percentile(blocks, 98))


def dedupe(frames: np.ndarray) -> np.ndarray:
    """Drops captures identical to the previous one (the screen grabber saw the same game frame twice)."""
    if len(frames) < 2:
        return frames
    keep = [0] + [i for i in range(1, len(frames)) if not np.array_equal(frames[i], frames[i - 1])]
    return frames[keep]


def find_flashes(frames: np.ndarray, k: float = 3.0, floor: float = 12.0, max_len: int = 3) -> list[int]:
    """Start indices of runs of 1..max_len frames that differ from the frames on both sides of the run by more than
    max(k * (difference between those two bracketing frames), floor)."""
    frames = dedupe(frames)
    spectra, cache = [spectrum(f) for f in frames], {}

    def err(a, b):
        if (a, b) not in cache:
            cache[(a, b)] = mc_error(frames[a], frames[b], fa=spectra[a], fb=spectra[b])
        return cache[(a, b)]
    found, n, i = [], len(frames), 1
    while i < n - 1:
        hit = 0
        for length in range(1, max_len + 1):
            j = i + length
            if j >= n:
                break
            limit = max(k * err(i - 1, j), floor)
            if all(err(t, i - 1) > limit and err(t, j) > limit for t in range(i, j)):
                hit = length
                break
        if hit:
            found.append(i); i += hit
        else:
            i += 1
    return found


def still_flicker(frames: np.ndarray, block: int = 8) -> tuple[float, float]:
    """(block_jitter, luma_drift) across a still-camera capture (0-255 scale).
    block_jitter is the worst 8x8 block's median frame-to-frame |delta|, catching steady or alternating local flicker
    while ignoring a single-frame pop. luma_drift is max - min whole-frame mean luma after dropping the two highest
    and lowest frames, catching slow exposure hunting or brightness pulsing while ignoring a 1-2 frame flash."""
    frames = dedupe(frames)
    if len(frames) < 4:
        return 0.0, 0.0
    if frames.shape[1] > THUMB_H or frames.shape[2] > THUMB_W:
        frames = np.array([thumb(f, THUMB_W, THUMB_H) for f in frames], np.float32)
    h, w = (frames.shape[1] // block) * block, (frames.shape[2] // block) * block
    blocks = frames[:, :h, :w].reshape(len(frames), h // block, block, w // block, block).mean(axis=(2, 4))
    block_jitter = float(np.median(np.abs(np.diff(blocks, axis=0)), axis=0).max())
    means = np.sort(frames.mean(axis=(1, 2)))
    trimmed = means[2:-2] if len(means) >= 8 else means
    return block_jitter, float(trimmed[-1] - trimmed[0])


def thumb(img: np.ndarray, w: int = 64, h: int = 36) -> np.ndarray:
    ys = (np.arange(h + 1) * img.shape[0]) // h
    xs = (np.arange(w + 1) * img.shape[1]) // w
    return np.array([[img[ys[r]:ys[r + 1], xs[c]:xs[c + 1]].mean() for c in range(w)] for r in range(h)])


def neon_stats(img: np.ndarray) -> tuple[int, float]:
    """(non-finite pixel count, share of all pixels that are finite, clipped and strongly coloured)."""
    finite = np.isfinite(img).all(axis=2)
    safe = np.where(finite[..., None], img, 0.0)
    mx, mn = safe.max(axis=2), safe.min(axis=2)
    neon = finite & (mx >= 1.0) & ((mx - mn) >= 0.5 * np.maximum(mx, 1e-6))
    return int((~finite).sum()), float(neon.sum()) / finite.size



def luma_and_sharpness(img: np.ndarray) -> tuple[float, float, float]:
    """(mean linear luma, 10th-percentile linear luma, brightness-normalized |dx|+|dy| edge sharpness)."""
    finite = np.isfinite(img).all(axis=2)
    safe = np.where(finite[..., None], np.maximum(img, 0.0), 0.0)
    lum = 0.2126 * safe[..., 0] + 0.7152 * safe[..., 1] + 0.0722 * safe[..., 2]
    mean_l = float(lum.mean())
    gx = np.abs(np.diff(lum, axis=1)).mean() if lum.shape[1] > 1 else 0.0
    gy = np.abs(np.diff(lum, axis=0)).mean() if lum.shape[0] > 1 else 0.0
    return mean_l, float(np.percentile(lum, 10)), float((gx + gy) / max(mean_l, 1e-4))
def nearest_resize(a: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    ys = (np.arange(shape[0]) * a.shape[0]) // shape[0]
    xs = (np.arange(shape[1]) * a.shape[1]) // shape[1]
    return a[ys][:, xs]


def outline(depth: np.ndarray, near: float) -> np.ndarray:
    """Boundary pixels of the near field (depth < near): Sam's silhouette in a third-person view."""
    m = np.isfinite(depth) & (depth < near)
    e = np.zeros_like(m)
    dy, dx = m[1:, :] != m[:-1, :], m[:, 1:] != m[:, :-1]
    e[1:, :] |= dy; e[:-1, :] |= dy; e[:, 1:] |= dx; e[:, :-1] |= dx
    return e & m


def dilate(m: np.ndarray, r: int) -> np.ndarray:
    out = m.copy()
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            out |= np.roll(m, (dy, dx), axis=(0, 1))
    return out


def outline_overlap(game_depth: np.ndarray, rtx_depth: np.ndarray, near: float) -> tuple[float | None, int]:
    """(share of the game's outline matched by Remix's within 2 px, game outline pixels); None if too few pixels."""
    shape = (min(game_depth.shape[0], rtx_depth.shape[0]), min(game_depth.shape[1], rtx_depth.shape[1]))
    g = outline(nearest_resize(game_depth, shape), near)
    r = outline(nearest_resize(rtx_depth, shape), near)
    count = int(g.sum())
    if count < max(100, int(2e-4 * g.size)):
        return None, count
    return float((g & dilate(r, 2)).sum()) / count, count


def structure_missing(on: np.ndarray, off: np.ndarray, block: int = 8) -> float:
    """Share of blocks where the reference (off) has clear structure but the tested image (on) is nearly flat."""
    def grad(a):
        g = np.zeros_like(a)
        g[:, 1:] += np.abs(np.diff(a, axis=1)); g[1:, :] += np.abs(np.diff(a, axis=0))
        h, w = (a.shape[0] // block) * block, (a.shape[1] // block) * block
        return g[:h, :w].reshape(h // block, block, w // block, block).mean(axis=(1, 3))
    ga, gb = grad(on), grad(off)
    return float(((gb > 6) & (ga < 0.25 * gb)).sum()) / ga.size


# --- camera trace ---------------------------------------------------------------------------------------------
def camera_angles(row: list[str]) -> tuple[float, float] | None:
    """(yaw, pitch) degrees of the first unskipped published camera in a d3d9_proxy_frames.csv row
    (column 7: source:drawsBefore:skipped:x:y:z:fx:fy:fz:vpDifference, '|' between publications)."""
    if len(row) < 7 or not row[6]:
        return None
    for pub in row[6].split("|"):
        p = pub.split(":")
        if len(p) >= 9 and p[2] == "0":
            fx, fy, fz = float(p[6]), float(p[7]), float(p[8])
            return math.degrees(math.atan2(fy, fx)), math.degrees(math.asin(max(-1.0, min(1.0, fz))))
    return None


def wrap(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


# --- evaluation -----------------------------------------------------------------------------------------------
class Run:
    """A run folder: manifest.json lists the spots asked for; each spot's folder holds spot.json and its data."""

    def __init__(self, folder: str):
        self.dir = folder
        self.manifest = load_json(os.path.join(folder, "manifest.json"))
        self.metrics: list[dict] = []

    def add(self, name, status, value, threshold, detail, source):
        self.metrics.append({"check": name, "status": status, "value": value, "threshold": threshold,
                             "detail": detail, "source": source})


def spot_path(spot: dict, rel: str | None) -> str | None:
    return os.path.join(spot["_dir"], rel) if rel else None


def frames_in(window: dict, key: str) -> list[int]:
    a, b = window.get(key) or (0, 0)
    return list(range(a + 1, b + 1))


def coverage(pairs, key, data_key):
    """pairs = [(spot, window)] -> (frames expected, records present), each spot judged on its own data files."""
    want, have = 0, []
    for s, w in pairs:
        data = s["_data"][data_key]
        ids = frames_in(w, key)
        want += len(ids)
        have += [data[f] for f in ids if f in data]
    return want, have


def window_frames(spot: dict, w: dict) -> tuple[np.ndarray | None, str]:
    """Luma frames for one window: telemetry thumbnails when every frame has one, else the screen capture."""
    telemetry = spot["_data"]["telemetry"]
    ids = frames_in(w, "telemetry")
    thumbs = [telemetry.get(f, {}).get("lumaThumb") for f in ids]
    if ids and all(thumbs):
        arr = [np.frombuffer(base64.b64decode(t), np.uint8).reshape(THUMB_H, THUMB_W) for t in thumbs]
        return np.array(arr, np.float32), "telemetry lumaThumb"
    p = spot_path(spot, w.get("frames"))
    if p and os.path.exists(p):
        width, height = w["frameSize"]
        return read_gray(p, width, height), "screen capture (gdigrab)"
    return None, "none"


def load_spots(run: Run) -> list[dict]:
    spots = []
    for name in run.manifest.get("spots", []):
        folder = os.path.join(run.dir, name)
        path = os.path.join(folder, "spot.json")
        if not os.path.exists(path):
            run.add(f"{name}: setup", FAIL, None, "spot measured", "no spot.json (the spot never ran or crashed)", "drive.py")
            continue
        s = load_json(path)
        s["_dir"] = folder
        telemetry, problems = read_telemetry(spot_path(s, s.get("telemetry")))
        s["_data"] = {"telemetry": telemetry, "proxy": read_csv_frames(spot_path(s, s.get("proxyFrames")), 7),
                      "pgate": read_csv_frames(spot_path(s, s.get("proxyGate")), 6)}
        s["_tproblems"] = [f"{name}: {p}" for p in problems]
        if s.get("error"):
            run.add(f"{name}: setup", FAIL, None, "no error", s["error"], "drive.py")
        else:
            spots.append(s)
    return spots


def recorder_checks(run: Run, pick, good: list[dict], tsrc: str) -> None:
    """Checks on recorder fields no other check reads: per-step GPU time, skipped draws, the mod material share,
    texture budget demotion, and a clean quit (Remix's log shows no lost Vulkan device)."""
    scene = pick(SCENE)

    # Per-step GPU time (passMs: step -> [gpuMs, cpuMs, calls]). A step is untimed when its GPU time is negative,
    # and a nested step can't take longer than the step it sits in.
    want, recs = coverage(scene, "telemetry", "telemetry")
    steps = [r["passMs"] for r in recs if isinstance(r.get("passMs"), dict)]
    step_limit = (f"untimed <= {THRESHOLDS['step_untimed_share']:.0%}, "
                  f"nested <= parent + {THRESHOLDS['step_child_excess_ms']:g} ms")
    if want == 0 or len(steps) < want:
        run.add("step_timing", NOT_MEASURED, None, step_limit,
                f"telemetry has {len(steps)} of {want} scene frames with 'passMs'", tsrc)
    else:
        untimed = sum(1 for p in steps if any(v[0] < 0 for v in p.values()))
        excess = []
        for p in steps:
            for k in p:
                parent = k.rsplit("/", 1)[0]
                if "/" in k and parent in p and p[k][0] >= 0 and p[parent][0] >= 0:
                    excess.append(p[k][0] - p[parent][0])
        worst = max(excess, default=0.0)
        share = untimed / len(steps)
        ok = share <= THRESHOLDS["step_untimed_share"] and worst <= THRESHOLDS["step_child_excess_ms"]
        run.add("step_timing", PASS if ok else FAIL, round(share, 6), step_limit,
                f"{untimed} of {len(steps)} scene frames with an untimed step; worst nested step {worst:+.3f} ms "
                f"against its parent", tsrc)

    # Skipped draws per reason against the draws the proxy counted over the same frames (column 4 of the frame
    # trace). Each scene window is one sample, and the worst window decides.
    samples, missing = [], 0
    for s, w in scene:
        tel, prx = s["_data"]["telemetry"], s["_data"]["proxy"]
        tids, pids = frames_in(w, "telemetry"), frames_in(w, "proxy")
        if not (pids and all(f in prx for f in pids) and all(isinstance(tel.get(f, {}).get("skips"), dict) for f in tids)):
            missing += 1
            continue
        draws = sum(float(prx[f][4]) for f in pids)
        skips: dict[str, float] = {}
        for f in tids:
            for reason, n in tel[f]["skips"].items():
                skips[reason] = skips.get(reason, 0) + n
        if draws > 0:
            samples.append((draws, skips))
        else:
            missing += 1
    if not samples or missing:
        detail = (f"{missing} of {len(scene)} scene windows lack telemetry 'skips' or proxy draws" if scene
                  else "no scene windows were run")
        for name, limit in (("skip_occlusion_share", THRESHOLDS["skip_share_occlusion"]),
                            ("skip_other_share", THRESHOLDS["skip_share_other"])):
            run.add(name, NOT_MEASURED, None, f"<= {limit:.0%} of a window's draws", detail, tsrc)
    else:
        occ = max(sk.get("occlusionQuery", 0) / d for d, sk in samples)
        other = max((n / d for d, sk in samples for r, n in sk.items() if r != "occlusionQuery"), default=0.0)
        run.add("skip_occlusion_share", PASS if occ <= THRESHOLDS["skip_share_occlusion"] else FAIL, round(occ, 6),
                f"<= {THRESHOLDS['skip_share_occlusion']:.0%} of a window's draws",
                f"worst scene window: {occ:.1%} of draws skipped by occlusionQuery", tsrc)
        run.add("skip_other_share", PASS if other <= THRESHOLDS["skip_share_other"] else FAIL, round(other, 6),
                f"<= {THRESHOLDS['skip_share_other']:.0%} of a window's draws",
                f"worst scene window: {other:.1%} of draws skipped for any other reason", tsrc)

    # Mod materials: the share of scene draws that used a replacement material, judged in the worst spot.
    # A spot with no material draws at all fails: that is what the mod not loading looks like.
    shares, incomplete = [], 0
    for s in good:
        recs = [s["_data"]["telemetry"].get(f, {}) for w in s["windows"] if w["kind"] in SCENE
                for f in frames_in(w, "telemetry")]
        if recs and all(isinstance(r.get(k), (int, float)) for r in recs
                        for k in ("replacedMaterialDraws", "legacyMaterialDraws")):
            rep = sum(r["replacedMaterialDraws"] for r in recs)
            leg = sum(r["legacyMaterialDraws"] for r in recs)
            shares.append((os.path.basename(s["_dir"]), rep / (rep + leg) if rep + leg else 0.0))
        else:
            incomplete += 1
    if not shares or incomplete:
        run.add("material_share", NOT_MEASURED, None, f">= {THRESHOLDS['min_replaced_share']:.0%} in every spot",
                f"{incomplete} of {len(good)} spots lack 'replacedMaterialDraws' or 'legacyMaterialDraws' in scene frames",
                tsrc)
    else:
        spot, worst = min(shares, key=lambda x: x[1])
        run.add("material_share", PASS if worst >= THRESHOLDS["min_replaced_share"] else FAIL, round(worst, 6),
                f">= {THRESHOLDS['min_replaced_share']:.0%} in every spot",
                f"lowest spot {spot}: {worst:.1%} of scene draws used a mod material", tsrc)

    # Texture budget: scene frames where the budget shrank textures, as a share of all scene frames.
    recs = [s["_data"]["telemetry"].get(f, {}) for s, w in scene for f in frames_in(w, "telemetry")]
    have = sum(1 for r in recs if isinstance(r.get("texturesDemoted"), (int, float)))
    if recs and have == len(recs):
        share = sum(1 for r in recs if r["texturesDemoted"] > 0) / len(recs)
        run.add("textures_demoted", PASS if share <= THRESHOLDS["max_demoted_share"] else FAIL, round(share, 6),
                f"<= {THRESHOLDS['max_demoted_share']:.0%} of scene frames",
                f"{share:.1%} of {len(recs)} scene frames had textures shrunk by the budget", tsrc)
    else:
        run.add("textures_demoted", NOT_MEASURED, None, f"<= {THRESHOLDS['max_demoted_share']:.0%} of scene frames",
                f"telemetry has {have} of {len(recs)} scene frames with 'texturesDemoted'", tsrc)

    # A lost Vulkan device ends a run without a clean quit. Each spot's remix-dxvk.log must be present and free of it.
    logs = [os.path.join(s["_dir"], "remix-dxvk.log") for s in good]
    have_logs = [p for p in logs if os.path.exists(p)]
    if not good or len(have_logs) < len(logs):
        detail = (f"{len(logs) - len(have_logs)} of {len(logs)} spots have no remix-dxvk.log" if good
                  else "no spot ran far enough to judge")
        run.add("clean_quit", NOT_MEASURED, None, "no VK_ERROR_DEVICE_LOST in any spot log", detail, "remix-dxvk.log")
    else:
        lost = [os.path.basename(os.path.dirname(p)) for p in have_logs
                if any("VK_ERROR_DEVICE_LOST" in ln for ln in read_lines(p))]
        run.add("clean_quit", PASS if not lost else FAIL, len(lost), "no VK_ERROR_DEVICE_LOST in any spot log",
                f"VK_ERROR_DEVICE_LOST in: {', '.join(lost)}" if lost else f"no device loss in {len(have_logs)} spot logs",
                "remix-dxvk.log")


def load_other_run(folder: str | None) -> dict[str, dict] | None:
    """Spots of an earlier or reference run folder keyed by name: None when no folder was given, {} when the folder
    has no manifest (so every spot is NOT MEASURED)."""
    if folder is None:
        return None
    if not os.path.exists(os.path.join(folder, "manifest.json")):
        return {}
    return {s["name"]: s for s in load_spots(Run(folder))}


def scene_pairs(spot: dict) -> list[tuple[dict, dict]]:
    return [(spot, w) for w in spot["windows"] if w["kind"] in SCENE]


def spot_fps(spot: dict) -> float | None:
    """Median FPS over one spot's scene frames; None unless every frame has frameTimeMs."""
    want, recs = coverage(scene_pairs(spot), "telemetry", "telemetry")
    vals = [r["frameTimeMs"] for r in recs if isinstance(r.get("frameTimeMs"), (int, float))]
    if want == 0 or len(vals) < want:
        return None
    return 1000.0 / max(float(np.median(vals)), 1e-3)


def spot_input_latency(spot: dict) -> float | None:
    """Median mouse-to-screen estimate (frame time + CPU/bridge wait) over one spot's scene frames; None unless every
    frame has frameTimeMs and gpuTimeMs."""
    want, recs = coverage(scene_pairs(spot), "telemetry", "telemetry")
    lats = [ft + max(0.0, ft - gt) for ft, gt in ((r.get("frameTimeMs"), r.get("gpuTimeMs")) for r in recs)
            if isinstance(ft, (int, float)) and isinstance(gt, (int, float))]
    if want == 0 or len(lats) < want:
        return None
    return float(np.median(lats))


def fps_regression_check(run: Run, good: list[dict], previous: dict[str, dict] | None) -> None:
    """FAIL when a spot's median FPS is more than max_fps_drop below the same spot in the previous run. With no previous
    run it only WARNs, since there is nothing to compare against."""
    src = "previous run folder (--previous), same spot"
    threshold = f"no spot more than {THRESHOLDS['max_fps_drop']:.0%} slower than its previous run"
    if previous is None:
        run.add("fps_regression", WARN, None, threshold, "no previous run given (--previous): nothing to compare", src)
        return
    drops, missing = [], []
    for s in good:
        now = spot_fps(s)
        before = spot_fps(previous[s["name"]]) if s["name"] in previous else None
        if now is None or before is None:
            missing.append(s["name"])
        else:
            drops.append((1.0 - now / before, s["name"], before, now))
    if not drops or missing:
        run.add("fps_regression", NOT_MEASURED, None, threshold,
                "no comparable FPS for: " + (", ".join(missing) or "no spot measured"), src)
        return
    worst = max(d[0] for d in drops)
    detail = f"worst drop {worst:.1%}; " + "; ".join(f"{n} {b:.1f} -> {a:.1f} FPS" for _, n, b, a in drops)
    run.add("fps_regression", PASS if worst <= THRESHOLDS["max_fps_drop"] else FAIL, round(worst, 4), threshold,
            detail, src)


def input_lag_check(run: Run, good: list[dict], reference: dict[str, dict] | None) -> None:
    """Each spot's median mouse-to-screen time against the same spot in the no-Remix reference run (--reference), within
    input_lag_margin_ms (a GUESS). Without a reference the check is NOT MEASURED: there is no fixed limit."""
    src = "rtx_telemetry.jsonl (frameTimeMs, gpuTimeMs): this run and the reference run (--reference)"
    margin = THRESHOLDS["input_lag_margin_ms"]
    threshold = f"<= reference + {margin:g} ms per spot (GUESS margin)"
    ours = {s["name"]: spot_input_latency(s) for s in good}
    shown = "; ".join(f"{n} {v:.1f} ms" if v is not None else f"{n} not measured" for n, v in ours.items())
    if reference is None:
        run.add("input_latency_ms", NOT_MEASURED, None, threshold,
                f"no reference run given (--reference), so not compared; this run: {shown or 'no spot measured'}", src)
        return
    diffs, missing = [], []
    for name, now in ours.items():
        base = spot_input_latency(reference[name]) if name in reference else None
        if now is None or base is None:
            missing.append(name)
        else:
            diffs.append((now - base, name, now, base))
    if not diffs or missing:
        run.add("input_latency_ms", NOT_MEASURED, None, threshold,
                "no comparable input latency for: " + (", ".join(missing) or "no spot measured"), src)
        return
    worst = max(d[0] for d in diffs)
    detail = f"worst spot {worst:+.2f} ms over reference; " + "; ".join(f"{n} {a:.1f} vs {b:.1f} ms" for _, n, a, b in diffs)
    run.add("input_latency_ms", PASS if worst <= margin else FAIL, round(worst, 2), threshold, detail, src)


def evaluate(folder: str, previous: str | None = None, reference: str | None = None) -> dict:
    run = Run(folder)
    if not run.manifest.get("spots"):
        run.add("spots", FAIL, 0, ">= 1", "the manifest lists no spots", "manifest.json")
    good = load_spots(run)
    prev_spots, ref_spots = load_other_run(previous), load_other_run(reference)
    pick = lambda kinds: [(s, w) for s in good for w in s["windows"] if w["kind"] in kinds]
    tproblems = [p for s in good for p in s["_tproblems"]]
    tsrc = "rtx_telemetry.jsonl" + (f" ({'; '.join(tproblems[:3])})" if tproblems else "")

    def telemetry_check(name, pairs, field, judge, threshold, describe, bad=FAIL):
        if not pairs:
            run.add(name, NOT_MEASURED, None, threshold, "no windows of this kind were run", tsrc); return
        want, recs = coverage(pairs, "telemetry", "telemetry")
        vals = [r[field] for r in recs if isinstance(r.get(field), (int, float))]
        if want == 0 or len(vals) < want:
            run.add(name, NOT_MEASURED, None, threshold, f"telemetry has {len(vals)} of {want} frames with '{field}'", tsrc)
            return
        value, ok = judge(vals)
        run.add(name, PASS if ok else bad, value, threshold, describe(value, want), tsrc)

    over = lambda v: sum(1 for x in v if x > 50.0)
    telemetry_check("frames_over_50ms", pick(MEASURED), "frameTimeMs",
                    lambda v: (over(v), over(v) <= THRESHOLDS["frames_over_50ms"]),
                    "0", lambda v, n: f"{v} of {n} frames over 50 ms")
    telemetry_check("skinned_min", pick(SCENE), "skinnedInstances",
                    lambda v: (min(v), min(v) >= THRESHOLDS["skinned_min"]),
                    ">= 1 every frame", lambda v, n: f"fewest character meshes in {n} frames: {v}")
    telemetry_check("static_blas_rebuilds", pick(ROTATING), "staticBlasRebuilds",
                    lambda v: (sum(v), sum(v) <= THRESHOLDS["static_blas_rebuilds"]),
                    "0 while rotating", lambda v, n: f"{v} rebuilds in {n} rotating frames")
    # Counted on Remix's 128x72 thumbnail, so this sees a patch of bad pixels on any frame, not one lone pixel.
    telemetry_check("nonfinite_thumb_pixels", pick(SCENE), "thumbNonFinite",
                    lambda v: (sum(v), sum(v) == 0),
                    "0", lambda v, n: f"{v} NaN or infinite thumbnail pixels in {n} scene frames")
    telemetry_check("min_median_fps", pick(SCENE), "frameTimeMs",
                    lambda v: (round(1000.0 / max(float(np.median(v)), 1e-3), 2),
                               1000.0 / max(float(np.median(v)), 1e-3) >= THRESHOLDS["min_median_fps"]),
                    f">= {THRESHOLDS['min_median_fps']:g} FPS (goal, WARN below)",
                    lambda v, n: f"median {v:.1f} FPS, target is at or above {THRESHOLDS['min_median_fps']:g} FPS "
                                 f"({n} scene frames)", bad=WARN)
    fps_regression_check(run, good, prev_spots)

    # Latency checks & Top 10 Latency Contributors breakdown
    want_lat, lat_recs = coverage(pick(SCENE), "telemetry", "telemetry")
    lat_pairs = [(float(r["frameTimeMs"]), float(r["gpuTimeMs"])) for r in lat_recs
                 if isinstance(r.get("frameTimeMs"), (int, float)) and isinstance(r.get("gpuTimeMs"), (int, float))]
    if want_lat == 0 or len(lat_pairs) < want_lat:
        run.add("latency_overhead_ms", NOT_MEASURED, None, f"<= {THRESHOLDS['max_wait_overhead_ms']:g} ms",
                f"telemetry has {len(lat_pairs)} of {want_lat} frames with frameTimeMs and gpuTimeMs", tsrc)
    else:
        waits = [max(0.0, ft - gt) for ft, gt in lat_pairs]
        med_wait = round(float(np.median(waits)), 2)
        p99_wait = round(float(np.percentile(waits, 99)), 2)
        run.add("latency_overhead_ms", PASS if med_wait <= THRESHOLDS["max_wait_overhead_ms"] else FAIL, med_wait,
                f"<= {THRESHOLDS['max_wait_overhead_ms']:g} ms",
                f"median CPU/bridge wait {med_wait:.2f} ms (p99 {p99_wait:.2f} ms)", tsrc)
    input_lag_check(run, good, ref_spots)
    latency_contributors = build_latency_contributors(good, pick(SCENE))

    # inward-facing character triangles: two extra telemetry fields, judged per frame
    want, recs = coverage(pick(SCENE), "telemetry", "telemetry")
    tri = [(r["inwardSkinnedTriangles"], r["skinnedTriangles"]) for r in recs
           if isinstance(r.get("inwardSkinnedTriangles"), (int, float)) and isinstance(r.get("skinnedTriangles"), (int, float))]
    if want == 0 or len(tri) < want:
        run.add("inward_fraction", NOT_MEASURED, None, "< 0.5%", f"telemetry has {len(tri)} of {want} frames with "
                "inwardSkinnedTriangles and skinnedTriangles", tsrc)
    else:
        worst = max((i / t if t else (1.0 if i else 0.0)) for i, t in tri)
        run.add("inward_fraction", PASS if worst < THRESHOLDS["inward_fraction"] else FAIL, round(worst, 6), "< 0.5%",
                f"worst frame {worst:.4%} of character triangles inward", tsrc)

    recorder_checks(run, pick, good, tsrc)

    # raster fallback: proxy gate CSV frame,sceneDraws,skinnedSceneDraws,handoffs,goggles,rtActive
    want, rows = coverage(pick(SCENE), "proxy", "pgate")
    if want == 0 or len(rows) < want:
        run.add("raster_fallback_frames", NOT_MEASURED, None, "0", f"proxy gate CSV has {len(rows)} of {want} frames",
                "d3d9_proxy_gate.csv")
    else:
        bad = sum(1 for r in rows if len(r) >= 6 and int(r[1]) > 0 and int(r[4]) == 0 and (int(r[5]) == 0 or int(r[3]) == 0))
        run.add("raster_fallback_frames", PASS if bad == 0 else FAIL, bad, "0",
                f"{bad} of {want} scene frames shown without the ray-traced image", "d3d9_proxy_gate.csv")

    # flashes: every still stop (>= 15 frames) and the pan (frames for >= 90% of the game's frames)
    flash_total, flash_detail, flash_missing, sources = 0, [], [], set()
    for s, w in pick(("still", "pan")):
        frames, src = window_frames(s, w)
        sources.add(src)
        n_game = len(frames_in(w, "telemetry")) or len(frames_in(w, "proxy"))
        n = 0 if frames is None else len(dedupe(frames))
        if w["kind"] == "still" and n < THRESHOLDS["still_frames"]:
            flash_missing.append(f"{s['name']}/{w['name']}: {n} frames"); continue
        if w["kind"] == "pan" and (n_game == 0 or n / n_game < THRESHOLDS["pan_coverage"]):
            flash_missing.append(f"{s['name']}/{w['name']}: {n} frames for {n_game} game frames"); continue
        hits = find_flashes(frames)
        flash_total += len(hits)
        if hits:
            flash_detail.append(f"{s['name']}/{w['name']} at {hits[:5]}")
    if flash_missing or not pick(("pan",)):
        run.add("flashes", NOT_MEASURED, flash_total, "0",
                "incomplete frames: " + "; ".join(flash_missing[:6] or ["no pan window"]), ", ".join(sorted(sources)) or "none")
    else:
        run.add("flashes", PASS if flash_total == 0 else FAIL, flash_total, "0",
                "; ".join(flash_detail) or "no flashes or pops", ", ".join(sorted(sources)))

    # sustained flicker or slow brightness pulsing across still camera stops (>= 15 deduplicated frames)
    worst_jitter, worst_drift, flicker_bad, flicker_missing, flicker_sources = 0.0, 0.0, [], [], set()
    j_limit, d_limit = THRESHOLDS["still_block_jitter"], THRESHOLDS["still_luma_drift"]
    flicker_thresh = f"block jitter <= {j_limit:g}, drift <= {d_limit:g} (/255)"
    for s, w in pick(("still",)):
        frames, src = window_frames(s, w)
        flicker_sources.add(src)
        n = 0 if frames is None else len(dedupe(frames))
        if n < THRESHOLDS["still_frames"]:
            flicker_missing.append(f"{s['name']}/{w['name']}: {n} frames"); continue
        bj, ld = still_flicker(frames)
        worst_jitter, worst_drift = max(worst_jitter, bj), max(worst_drift, ld)
        if bj > j_limit or ld > d_limit:
            flicker_bad.append(f"{s['name']}/{w['name']} jitter {bj:.2f} drift {ld:.2f}")
    if flicker_missing or not pick(("still",)):
        run.add("still_flicker", NOT_MEASURED, None, flicker_thresh,
                "incomplete frames: " + "; ".join(flicker_missing[:6] or ["no still windows"]),
                ", ".join(sorted(flicker_sources)) or "none")
    else:
        run.add("still_flicker", FAIL if flicker_bad else PASS, round(max(worst_jitter, worst_drift), 2), flicker_thresh,
                f"worst block jitter {worst_jitter:.2f}, luma drift {worst_drift:.2f}"
                + ("; " + "; ".join(flicker_bad[:5]) if flicker_bad else ""),
                ", ".join(sorted(flicker_sources)))

    # image dumps at the stops: NaN, neon, brightness floor, angled sharpness, outline
    nan_total, worst_neon, n_final, missing_final, overlaps, outline_missing = 0, 0.0, 0, [], [], []
    worst_luma, worst_raster_ratio, worst_angled_sharp = float("inf"), float("inf"), float("inf")
    n_angled = 0
    for s, w in pick(("still",)):
        tag = w.get("dump")
        if not tag:
            continue
        final = spot_path(s, os.path.join("gate", f"{tag}_final.pfm"))
        if os.path.exists(final):
            img = read_pfm(final)
            nan, neon = neon_stats(img)
            mean_l, p10_l, sharp = luma_and_sharpness(img)
            nan_total += nan; worst_neon = max(worst_neon, neon); n_final += 1
            worst_luma = min(worst_luma, mean_l)
            raster = spot_path(s, os.path.join("gate", f"{tag}_raster.pfm"))
            if raster and os.path.exists(raster):
                rm, rp10, _ = luma_and_sharpness(read_pfm(raster))
                if rm > 1e-4:
                    worst_raster_ratio = min(worst_raster_ratio, mean_l / rm)
                if rp10 > 1e-4:
                    worst_raster_ratio = min(worst_raster_ratio, p10_l / rp10)
            if abs(float(w.get("pitch", 0.0))) > 1e-3:
                worst_angled_sharp = min(worst_angled_sharp, sharp)
                n_angled += 1
        else:
            missing_final.append(tag)
        if w.get("outline"):
            gd = spot_path(s, os.path.join("gate", f"{tag}_gamedepth.pfm"))
            rd = spot_path(s, os.path.join("gate", f"{tag}_depth.pfm"))
            if not (os.path.exists(gd) and os.path.exists(rd)):
                outline_missing.append(f"{s['name']}/{tag}: depth dumps missing"); continue
            ov, count = outline_overlap(read_pfm(gd), read_pfm(rd), float(s.get("nearField", 300)))
            if ov is None:
                outline_missing.append(f"{s['name']}/{tag}: Sam not in the near field ({count} outline px)"); continue
            overlaps.append((f"{s['name']} {w['outline']}", ov))
    if missing_final or n_final == 0:
        detail = f"{len(missing_final)} of {len(missing_final) + n_final} stops have no final image dump"
        run.add("nan_pixels", NOT_MEASURED, nan_total, "0", detail, "Remix <tag>_final.pfm")
        run.add("neon_fraction", NOT_MEASURED, round(worst_neon, 6), "< 0.1%", detail, "Remix <tag>_final.pfm")
        run.add("brightness_floor", NOT_MEASURED, None, f"luma >= {THRESHOLDS['min_scene_luma']:g}", detail, "Remix <tag>_final.pfm")
        run.add("angled_sharpness", NOT_MEASURED, None, f">= {THRESHOLDS['min_angled_sharpness']:g}", detail, "Remix <tag>_final.pfm")
    else:
        run.add("nan_pixels", PASS if nan_total <= THRESHOLDS["nan_pixels"] else FAIL, nan_total, "0",
                f"{nan_total} non-finite pixels in {n_final} dumps", "Remix <tag>_final.pfm")
        run.add("neon_fraction", PASS if worst_neon < THRESHOLDS["neon_fraction"] else FAIL, round(worst_neon, 6), "< 0.1%",
                f"worst dump {worst_neon:.4%} clipped neon pixels", "Remix <tag>_final.pfm")
        b_ok = worst_luma >= THRESHOLDS["min_scene_luma"] and (
            not math.isfinite(worst_raster_ratio) or worst_raster_ratio >= THRESHOLDS["min_raster_luma_ratio"]
        )
        b_detail = f"darkest stop mean luma {worst_luma:.4f}" + (
            f", raster match {worst_raster_ratio:.2f}x" if math.isfinite(worst_raster_ratio) else ""
        )
        run.add("brightness_floor", PASS if b_ok else FAIL, round(worst_luma, 5),
                f"luma >= {THRESHOLDS['min_scene_luma']:g} (>= 0.75x raster)", b_detail, "Remix <tag>_final.pfm")
        if n_angled == 0:
            run.add("angled_sharpness", NOT_MEASURED, None, f">= {THRESHOLDS['min_angled_sharpness']:g}",
                    "no pitched camera stops measured", "Remix <tag>_final.pfm")
        else:
            run.add("angled_sharpness", PASS if worst_angled_sharp >= THRESHOLDS["min_angled_sharpness"] else FAIL,
                    round(worst_angled_sharp, 5), f">= {THRESHOLDS['min_angled_sharpness']:g}",
                    f"worst angled stop gradient {worst_angled_sharp:.4f} across {n_angled} pitched views",
                    "Remix <tag>_final.pfm")
    expected_outlines = 2 * len(good)
    if outline_missing or not good or len(overlaps) < expected_outlines:
        run.add("outline_overlap", NOT_MEASURED, None, ">= 85%",
                f"{len(overlaps)} of {expected_outlines} outline pairs; " + "; ".join(outline_missing[:6]),
                "proxy <tag>_gamedepth.pfm + Remix <tag>_depth.pfm")
    else:
        worst = min(v for _, v in overlaps)
        run.add("outline_overlap", PASS if worst >= THRESHOLDS["outline_overlap"] else FAIL, round(worst, 4), ">= 85%",
                ", ".join(f"{k} {v:.1%}" for k, v in overlaps), "proxy <tag>_gamedepth.pfm + Remix <tag>_depth.pfm")

    pose_check(run, good)
    goggles_check(run, good)
    status = PASS if run.metrics and all(x["status"] in (PASS, WARN) for x in run.metrics) else FAIL
    result = {"status": status, "run": os.path.abspath(folder), "thresholds": THRESHOLDS, "checks": run.metrics,
              "latencyContributors": latency_contributors}
    save_json(os.path.join(folder, "result.json"), result)
    save_text(os.path.join(folder, "result.md"), markdown(result))
    return result

def pose_check(run: Run, spots: list[dict]) -> None:
    """The camera direction in the proxy's trace must follow the requested yaw/pitch (relative to the first stop;
    the sign convention is learnt from the first pure-yaw and first pitched stop), and the pan must turn >= 350 deg."""
    src = "d3d9_proxy_frames.csv camera trace"
    worst, problems, missing = 0.0, [], []
    for s in spots:
        proxy = s["_data"]["proxy"]
        stops = []
        for w in s["windows"]:
            if w["kind"] != "still":
                continue
            angles = [a for f in frames_in(w, "proxy") if f in proxy and (a := camera_angles(proxy[f]))]
            if not angles:
                missing.append(f"{s['name']}/{w['name']}: no camera in the trace"); continue
            y0 = angles[0][0]
            stops.append((w["yaw"], w["pitch"], y0 + float(np.median([wrap(a[0] - y0) for a in angles])),
                          float(np.median([a[1] for a in angles]))))
        if not stops or (stops[0][0], stops[0][1]) != (0, 0):
            missing.append(f"{s['name']}: no measured first stop at yaw 0 pitch 0"); continue
        _, _, by, bp = stops[0]
        sign_y = sign_p = 1
        y1 = next(((ry, my) for ry, rp, my, mp in stops if ry and not rp), None)
        if y1:
            d = wrap(y1[1] - by)
            sign_y = 1 if abs(wrap(d - y1[0])) <= abs(wrap(d + y1[0])) else -1
        p1 = next(((rp, mp) for ry, rp, my, mp in stops if rp), None)
        if p1:
            d = p1[1] - bp
            sign_p = 1 if abs(d - p1[0]) <= abs(d + p1[0]) else -1
        for ry, rp, my, mp in stops:
            worst = max(worst, abs(wrap(sign_y * wrap(my - by) - ry)), abs(sign_p * (mp - bp) - rp))
        for w in s["windows"]:
            if w["kind"] == "pan":
                yaws = [a[0] for f in frames_in(w, "proxy") if f in proxy and (a := camera_angles(proxy[f]))]
                turned = abs(sum(wrap(b - a) for a, b in zip(yaws, yaws[1:])))
                if turned < THRESHOLDS["pan_degrees"]:
                    problems.append(f"{s['name']}/{w['name']}: camera turned {turned:.0f} deg, expected 360")
    if missing or not spots:
        run.add("pose", NOT_MEASURED, None, "<= 3 deg", "; ".join((missing or ["no spot measured"])[:6]), src)
    else:
        ok = not problems and worst <= THRESHOLDS["pose_error_deg"]
        run.add("pose", PASS if ok else FAIL, round(worst, 2), "<= 3 deg",
                f"worst stop {worst:.2f} deg off" + ("; " + "; ".join(problems[:6]) if problems else ""), src)


def goggles_check(run: Run, spots: list[dict]) -> None:
    """Per goggle mode: toggled (proxy log / picture change), and the view with ray tracing on matches the game's own
    view with it off: thumbnail correlation, mean brightness, and no blocks that lost their structure."""
    src = "screen capture + d3d9_proxy.log"
    threshold = "toggled, corr >= 0.8, gap <= 0.06, missing <= 2%"
    details, missing, failed = [], [], False
    for s in spots:
        modes: dict[int, dict] = {}
        for w in s["windows"]:
            if w["kind"] == "goggles":
                modes.setdefault(w["goggles"]["mode"], {})[w["goggles"]["rt"]] = w["goggles"]
        if not modes:
            missing.append(f"{s['name']}: no goggle windows"); continue
        for mode, pair in sorted(modes.items()):
            if set(pair) != {"on", "off"} or not all(g.get("toggled") for g in pair.values()):
                missing.append(f"{s['name']} mode {mode}: not toggled or a capture missing"); continue
            imgs = {}
            for rt, g in pair.items():
                p = spot_path(s, g.get("shot"))
                if p and os.path.exists(p) and os.path.getsize(p) >= g["frameSize"][0] * g["frameSize"][1]:
                    imgs[rt] = read_gray(p, *g["frameSize"]).mean(axis=0) / 255.0   # average away goggle noise
            if len(imgs) != 2:
                missing.append(f"{s['name']} mode {mode}: shot missing"); continue
            ta, tb = thumb(imgs["on"]).ravel(), thumb(imgs["off"]).ravel()
            corr = float(np.corrcoef(ta, tb)[0, 1]) if ta.std() > 1e-6 and tb.std() > 1e-6 else 0.0
            gap = abs(float(imgs["on"].mean() - imgs["off"].mean()))
            miss = structure_missing(imgs["on"] * 255, imgs["off"] * 255)
            ok = corr >= THRESHOLDS["goggles_corr"] and gap <= THRESHOLDS["goggles_mean_gap"] \
                and miss <= THRESHOLDS["goggles_missing_blocks"]
            failed |= not ok
            details.append(f"{s['name']} mode {mode}: corr {corr:.2f}, brightness gap {gap:.3f}, missing blocks {miss:.1%}"
                           + ("" if ok else " FAIL"))
    if missing or not spots:
        run.add("goggles", NOT_MEASURED, None, threshold, "; ".join((missing or ["no spot measured"])[:6]), src)
    else:
        run.add("goggles", FAIL if failed else PASS, None, threshold, "; ".join(details), src)


def build_latency_contributors(spots: list[dict], scene_pairs: list[tuple[dict, dict]]) -> list[dict]:
    """Rank up to Top 10 latency contributors across GPU rendering, CPU/bridge wait, Present, call timings, and windows."""
    items: list[dict] = []
    _, recs = coverage(scene_pairs, "telemetry", "telemetry")
    gpu = [float(r["gpuTimeMs"]) for r in recs if isinstance(r.get("gpuTimeMs"), (int, float))]
    ft = [float(r["frameTimeMs"]) for r in recs if isinstance(r.get("frameTimeMs"), (int, float))]
    waits = [max(0.0, float(r["frameTimeMs"]) - float(r["gpuTimeMs"])) for r in recs
             if isinstance(r.get("frameTimeMs"), (int, float)) and isinstance(r.get("gpuTimeMs"), (int, float))]

    def add_series(name: str, vals: list[float], source: str) -> None:
        if not vals:
            return
        items.append({
            "contributor": name,
            "medianMs": round(float(np.median(vals)), 3),
            "p99Ms": round(float(np.percentile(vals, 99)), 3),
            "totalMs": round(float(sum(vals)), 1),
            "source": source,
        })

    add_series("GPU Ray-Tracing & Denoising (gpuTimeMs)", gpu, "rtx_telemetry.jsonl")
    add_series("CPU / Bridge Queue Wait Overhead (frameTime - gpuTime)", waits, "rtx_telemetry.jsonl")
    # d3d9_proxy_frames.csv columns (proxy_diagnostics.cpp FinishTracedFrame): 1 = Present entry clock (ms since
    # boot, not a duration), 2 = interval since the previous Present, 3 = time spent inside this Present.
    # The game's own CPU share is approximated as the interval minus this frame's Present.
    _, prows = coverage(scene_pairs, "proxy", "proxy")
    present_ms = []
    other_cpu_ms = []
    for r in prows:
        try:
            present_ms.append(float(r[3]))
            other_cpu_ms.append(max(0.0, float(r[2]) - float(r[3])))
        except (IndexError, ValueError):
            continue
    add_series("D3D9 Present Bridge Sync (presentMs)", present_ms, "d3d9_proxy_frames.csv")
    add_series("Game Engine CPU Simulation / Draw Submission", other_cpu_ms, "d3d9_proxy_frames.csv")

    for s, w in scene_pairs:
        tmap = s["_data"]["telemetry"]
        w_waits = [max(0.0, float(tmap[f]["frameTimeMs"]) - float(tmap[f]["gpuTimeMs"]))
                   for f in frames_in(w, "telemetry")
                   if f in tmap and isinstance(tmap[f].get("frameTimeMs"), (int, float))
                   and isinstance(tmap[f].get("gpuTimeMs"), (int, float))]
        if w_waits:
            add_series(f"Window Wait: {s['name']}/{w['name']} ({w['kind']})", w_waits, "window breakdown")
    items.sort(key=lambda x: (-x["p99Ms"], -x["medianMs"], -x["totalMs"]))
    return items[:10]


def markdown(result: dict) -> str:
    lines = [f"# Runtime gate: {result['status']}", "", f"Run: `{result['run']}`", "",
             "| Check | Status | Value | Threshold | Detail | Source |", "|---|---|---|---|---|---|"]
    for c in result["checks"]:
        lines.append(f"| {c['check']} | {c['status']} | {c['value']} | {c['threshold']} | {c['detail']} | {c['source']} |")
    contribs = result.get("latencyContributors") or []
    if contribs:
        lines += ["", "## Top 10 Latency Contributors", "",
                  "| Rank | Contributor | Median (ms) | P99 (ms) | Total (ms) | Source |",
                  "|---|---|---|---|---|---|"]
        for idx, item in enumerate(contribs, 1):
            lines.append(
                f"| {idx} | {item['contributor']} | {item['medianMs']:.3f} | {item['p99Ms']:.3f} | "
                f"{item['totalMs']:.1f} | {item['source']} |"
            )
    return "\n".join(lines) + "\n"

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Runtime gate: judge one verify_runtime run folder (see the module docstring).")
    ap.add_argument("folder")
    ap.add_argument("--previous", help="an earlier run folder: a spot more than 10% slower than it FAILs")
    ap.add_argument("--reference", help="the no-Remix reference run folder: input latency is compared with it per spot")
    a = ap.parse_args()
    r = evaluate(a.folder, previous=a.previous, reference=a.reference)
    for c in r["checks"]:
        print(f"[{c['status']}] {c['check']}: {c['detail']}")
    print("GATE", r["status"])
    sys.exit(0 if r["status"] == PASS else 1)
