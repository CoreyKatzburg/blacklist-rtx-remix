"""Hard numeric pass/fail for one verify_runtime run.  usage: gate.py <run folder> [--previous <run folder>] [--reference <run folder>]  (exit 0 = PASS, 1 = FAIL)

Reads <run>/manifest.json (the spots asked for) and each <run>/<spot>/spot.json (written by drive.py: timed windows
with frame ranges, plus the data files copied from the game), and writes <run>/result.json and <run>/result.md.
Every check is PASS, WARN, FAIL or NOT MEASURED. WARN is shown and does not fail the gate. NOT MEASURED does: a check whose data is missing or
incomplete never passes. Frame numbers restart at every game launch, so each spot is judged on its own files.

Checks (thresholds in THRESHOLDS):
  nan_pixels               non-finite pixels in Remix's final image dumps (<tag>_final.pfm), all sweep stops
  scene_brightness         two-sided original-game brightness + near-white check vs the --reference (no Remix) run's
                           <tag>_raster.pfm, only at stops reference_position accepts (central 84% ROI, clipped to
                           [0, 1], on-pose <= 1 deg): per-stop [0.50x, 1.85x], spot median [0.60x, 1.50x],
                           near-white delta <= +0.10, floor >= 0.03; NOT MEASURED without an accepted reference
                           stop. This run's own <tag>_raster.pfm (the in-launch picture, ray tracing off) is NOT the
                           original game (1.41-1.69x brighter at accepted stops, Nov 16): it is printed report only,
                           "against the in-launch picture, not the original game", and is fit only for change
                           detection between steps of one launch, never for a verdict.
  neon_fraction            worst dump's share of clipped, strongly coloured pixels: max channel >= 1.0 and
                           (max - min) / max >= 0.5 (white highlights are allowed, neon colours are not)
  skinned_min              fewest active character meshes in any frame where proxy skinnedSceneDraws > 0
  remix_dropped_character_frames  frames outside goggles (proxy_gate goggles==0 on frame +/-3 and rayTraced!=false)
                           where Remix has 0 skinned instances while proxy sent skinnedSceneDraws > 0 on +/-3 (must be 0)
  frame_join_clock         for telemetry frames with qpcMs > 0, qpcMs must lie in (entry+presentMs of row k-1,
                           entry+presentMs of row k] for matching proxy frame k
  zero_normal_skinned_triangles   per-spot median/max of telemetry zeroNormalSkinnedTriangles (max 0, measured in every Nov 16 run)
  zero_normal_instances           per-spot median/max of telemetry zeroNormalInstances (max 0, measured in every Nov 16 run)
  zero_normal_static_triangles    per-spot median/max of telemetry zeroNormalStaticTriangles (max 0, measured in every Nov 16 run)
  zero_normal_static_instances    per-spot median/max of telemetry zeroNormalStaticInstances (max 0, measured in every Nov 16 run)
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
  frames_over_50ms         telemetry frameTimeMs > 50 in any measured window, excluding loading-screen frames
                           (proxy sceneDraws == 0 or all-zero/empty camera)
  min_median_fps           median FPS over the scene windows: WARN below 90 (Corey's target: a goal, not a limit)
  fps_regression           each spot's median FPS against the same spot in --previous: FAIL when more than 10% slower;
                           WARN when no previous run is given
  input_latency_ms         each spot's median game share of the mouse lag (spot.json mouse_lag.trials gameMs, >= 20
                           turns: mouse turn to the first proxy frame with the turned camera) against the same spot in
                           --reference (the no-Remix run): FAIL when more than the GUESS margin above it; NOT MEASURED when
                           missing or without --reference. It leaves out everything after the game hands the frame on, so
                           for Remix it is a lower bound.
  input_latency_screen_ms  the screen share (samples_ms, from a desktop grab): always NOT MEASURED, numbers in the detail,
                           until the grab is shown to work in Remix runs (coordinator, Nov 16)
  frame_interval_vs_reference  report only, never FAIL: per spot, median FPS and frames over 50 ms from the proxy frame
                           interval (proxy_frames.csv index 2) in scene windows, this run next to --reference
  step_timing              per-step GPU times (telemetry passMs): untimed steps, and nested steps longer than their parent
  skip_occlusion_share     worst scene window's share of draws skipped by occlusionQuery (telemetry skips / proxy draws)
  skip_other_share         worst scene window's share of draws skipped for any other reason
  material_share           share of scene draws that used a mod replacement material, in the worst spot
  textures_demoted         share of scene frames where the texture budget shrank textures (telemetry texturesDemoted)
  clean_quit               no VK_ERROR_DEVICE_LOST in remix-dxvk.log and no non-zero exit code in drive.log's QUIT
                           record (either fails); then bridge32.log has "Sending Terminate command to server" (else
                           NOT MEASURED), and no proxy Present > 1000 ms in the last 10 s
  present_stalls           d3d9_proxy_frames.csv 4th column (row index 3 after the frame number: time inside Present)
                           > 50 ms in any measured window, excluding loading-screen frames via is_proxy_loading
  frame_gap_over_50ms      d3d9_proxy_frames.csv 3rd column (row index 2: ms since the previous Present entry, the gap
                           the player sees) > 50 ms in any measured window, excluding loading frames (worker C, Nov 16)
  goggles_toggle_rebuilds  on the first rayTraced=true frame after rayTraced=false in any goggles window, telemetry
                           staticBlasBuilds <= 40 (GUESS)
  goggles_dark_frame       on that same first rayTraced=true frame, mean luma / mean luma 10 ray-traced frames later
                           >= 0.4 (GUESS)
  flare_standins_off       when proxy.log says flare stand-ins 0, stray line stand-ins S must be 0
  flare_unplaced_balls     stray line max unplaced stand-ins UM: 0 when skip unplaced SU=1, <= 15 when SU=0 (measured
                           live by worker B: 15 at con_custodial and con_bright_hall with SU=0, 0 with SU=1)
  flare_moving_balls       stray line max moving stand-ins MM: 0 when skip moving SM=1
  flare_sam_gear_balls     stray line "on sam gear N (max M)": M must be 0 when the settings say skip sam gear 1; no
                           limit with 0. Proxies before c78eb25 lack the field: NOT MEASURED
  lights_weapon_flips      stray line "weapon flips W": 0 on every line when weapon by shape is 1 or 2; report only
                           with 0. Proxies before fbe011f lack the field: NOT MEASURED
  lights_recreated         stray line "recreated R": <= 2 per line when keep tracked 1 (GUESS); report only with 0.
                           Proxies before fbe011f lack the field: NOT MEASURED
  lamps_without_flare      spot max of lamps without flare XM <= --reference spot max + 10 (GUESS)
  lights_orbs_unmatched    symptom checks, ledger rows 1 and 2 (worker B's glow_blobs.py, pictures only): per still and
  lights_puffs             switch stop, the Remix still against the --reference (no Remix) still at the same pose.
  lights_glows_lost        Orbs: bright blobs the original does not show. Puffs: large bright areas the original leaves
                           dark (also surfaces Remix lights, so puffs are REPORT ONLY: WARN above 0, never FAIL).
                           Glows lost: lamp glows the original shows that Remix lost (what must stay). Orbs and lost
                           glows: limit 0 per stop. Poses apart: that stop is listed, not compared. No no-Remix
                           reference: NOT MEASURED
  lights_lamps_dimmed      must-stay check for a change (glow_blobs.py --dimmed, 32a905d): each switch step against
                           the on / on_again pair (switch_base / switch_base_again, or the see-through probe's
                           switch_on / switch_on_again) at the lamps of the reference still nearest the on pose that
                           passes glow_blobs.pose_ok (none: NOT MEASURED); a lamp is dimmed when its glow drops by more
                           than max(12, 3 x |on - on_again|). Limit 0. Goggles switches left out.
                           Runs without that triplet: NOT MEASURED, which does not fail the gate (SOFT_NM)
  lights_dimmed_linger     report only (WARN above 600 frames, never FAIL): stray line max longest dim TM. It measures
                           the game: guard rifle flashlights dim on their own (2060 frames at con_bright_hall)
  light_switchoff_lag      report only (WARN above 1.0 s, never FAIL): longest downward brightness run before stopped
                           in lightpub_trace.csv. It measures the game: a guard flashlight ran down 18.6 s
  pose                     the camera really went where it was told (proxy camera trace vs requested yaw/pitch)
  goggles                  each goggle mode toggled (proxy log) and looks the same with ray tracing on and off
                           (no missing geometry, no different picture)
Every per-frame check also requires the data to cover every frame of its windows.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import math
import os
import re
import subprocess
import sys

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "analysis")))
import overlay_mask  # noqa: E402  the NVIDIA performance overlay box, blanked in every screen-capture compare

THRESHOLDS = {
    "nan_pixels": 0, "neon_fraction": 0.001, "skinned_min": 1, "remix_dropped_character_frames": 0,
    "outline_overlap": 0.85, "inward_fraction": 0.005,
    "raster_fallback_frames": 0, "flashes": 0, "still_block_jitter": 4.0, "still_luma_drift": 6.0,
    "still_block_pulse": 16.0, "still_block_step4": 6.0,
    "static_blas_rebuilds": 0,
    # Zero hitches outside loading screens (Corey, Nov 15). Loading-screen frames are excluded using the proxy trace
    # (sceneDraws == 0 in d3d9_proxy_gate.csv or empty/all-zero camera in d3d9_proxy_frames.csv).
    "frames_over_50ms": 0,
    "min_median_fps": 90.0,  # Corey's target, Nov 15: WARN below it, not a fail. Median FPS over the SCENE windows (goggles excluded).
    "max_wait_overhead_ms": 5.0,
    # Input lag (input_latency_ms) is judged per spot against the no-Remix reference run, not a fixed limit. The margin
    # is a GUESS: reference game share is 12.7 to 27.5 ms and Remix 93.7 to 101.5 ms, so no healthy pair sets it yet.
    "input_lag_margin_ms": 5.0,
    # Speed FAIL (fps_regression): a spot whose median FPS is more than this below the same spot in the previous run.
    "max_fps_drop": 0.10,  # Corey, Nov 15
    "min_scene_luma": 0.03, "min_raster_luma_ratio": 0.50, "max_raster_luma_ratio": 1.85,
    "min_spot_median_ratio": 0.60, "max_spot_median_ratio": 1.50, "max_near_white_delta": 0.10,
    "min_angled_sharpness": 0.02,
    "pose_error_deg": 3.0, "pan_degrees": 350.0, "goggles_corr": 0.8, "goggles_mean_gap": 0.06,
    "goggles_missing_blocks": 0.02, "still_frames": 15, "pan_coverage": 0.9,
    # Recorder checks (recorder_checks). MEASURED = set from live runs: the healthy Remix gate runs 20261115-140515 and
    # -142638 and 20261116-063147 to -123628 (11 runs with scene windows, about 31,000 scene frames). GUESS = no live
    # number decides it yet; the comment says what was measured.
    "step_untimed_share": 0.0,      # MEASURED: 0 untimed steps in every run (was a GUESS of 2%).
    "step_child_excess_ms": 0.5,    # GUESS, timer rounding tolerance. Measured +0.000 ms worst in every run.
    "skip_share_occlusion": 0.6,    # MEASURED: worst window 18.3% to 58.0% (paladin_1, 140515) in healthy runs.
    "skip_share_other": 0.05,       # GUESS. Measured at most 0.42% (140515); nothing says what share is harmful.
    "min_replaced_share": 0.6,      # MEASURED: lowest spot 73.6% (paladin_1) to 88.6% with the mod, 0% without it.
    "max_demoted_share": 0.0,       # MEASURED: 0 frames with a texture shrunk by the budget in every run.
    "goggles_toggle_max_builds": 40,   # GUESS. No healthy goggles-off since the proxy fix (RT stays on: NOT MEASURED).
    "goggles_dark_min_ratio": 0.4,     # GUESS. Same: no healthy goggles-off since the proxy fix.
    "present_stalls_max": 0,           # proxy row index 3 (time inside Present) > 50 ms in measured windows (excl. loads).
    "frame_gap_max": 0,                # proxy row index 2 (Present-to-Present gap) > 50 ms, same rule as frames_over_50ms.
    "freeze_gap_ms": 1000.0,           # --session: a gap over this in play is a freeze (worker C's spec; Corey's 1 s).
    "freezes_in_play_max": 0,          # Measured: 0 in sessions 135449 and 212037 (worker C's freeze_scan.py).
    "quit_present_max_ms": 1000.0,     # No proxy Present > 1000 ms in the last 10 s before Terminate.
    "flare_unplaced_max_noskip": 15,   # Measured live (worker B): 15 at both spots with SU=0, 0 with SU=1.
    "lamps_without_flare_margin": 10,  # GUESS. Max jump over --reference spot max XM.
    "lights_dimmed_max_frames": 600,   # Report only: WARN above this. Game guard flashlights reach 2060.
    "light_switchoff_max_s": 1.0,      # Report only: WARN above this. Game guard flashlights reach 18.6 s.
    "lights_recreated_max_kept": 2,    # GUESS (worker B). Re-creates per stray line with keep tracked 1.
    "zero_normal_skinned_tris_max": 0,     # MEASURED: max 0 in every Nov 16 run with the field (063147 to 123628).
    "zero_normal_instances_max": 0,        # MEASURED: same.
    "zero_normal_static_tris_max": 0,      # MEASURED: same.
    "zero_normal_static_instances_max": 0, # MEASURED: same.
    # Compile hang: 063147 con_bright_hall (stuck) waited frameTimeMs - gpuTimeMs median 29.5 ms, p99 31.7 ms on its
    # non-loading frames; healthy runs have longest such run 0 to 4 frames and 0.0 s of compile wait.
    "compile_stall_min_frames": 300,       # MEASURED range: healthy max 4 frames, stuck 14,314; 300 is a choice in between.
    "compile_stall_wait_min_ms": 25.0,     # MEASURED band around the stuck 29.5 ms median; the edges are GUESSES.
    "compile_stall_wait_max_ms": 40.0,     # Same.
    "compile_wait_stuck_s": 20.0,          # MEASURED range: healthy 0.0 s, stuck 300.2 s; 20 s is a choice in between.
    "compile_wait_min_ms": 28.0,           # Worker C's signature around the stuck 29.5 ms median; edges are GUESSES.
    "compile_wait_max_ms": 40.0,           # Same.
    "min_scene_rt_share": 0.99,            # MEASURED: 100.0% in every healthy run, 0% stuck; 99% is a choice.
    }
# Checks whose NOT MEASURED does not fail the gate (a field not yet in a build that ran live). Empty since Remix
# 0c13881 (staged in 81c0328) ran live on Nov 16 with qpcMs and instancedSharedSlotDraws (run 20261116-100605).
# input_latency_screen_ms is always NOT MEASURED until the desktop grab works in Remix runs (coordinator, Nov 16);
# input_latency_ms (the game share) is the judged lag check. lights_puffs is report only (worker B's 9ddc95d), as
# are the session lines freeze_gaps_excluded and quit_gap_ms. lights_lamps_dimmed applies only to runs with switch_base,
# a change and switch_base_again; its NOT MEASURED says why in the detail.
SOFT_NM: frozenset[str] = frozenset({"input_latency_screen_ms", "lights_puffs", "freeze_gaps_excluded", "quit_gap_ms",
                                     "lights_lamps_dimmed", "reference_position"})
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


def still_flicker(frames: np.ndarray, block: int = 8) -> tuple[float, float, float, float]:
    """(block_jitter, luma_drift, block_pulse, block_step4) across a still-camera capture (0-255 scale).
    - block_jitter: worst 8x8 block's median frame-to-frame |delta| (steady/alternating flicker).
    - luma_drift: trimmed max - min whole-frame mean luma (slow exposure hunting).
    - block_pulse: worst 8x8 block's min(pos_travel, neg_travel) on a 7-frame running median (>=4-frame local pulses).
    - block_step4: worst 8x8 block's 4th-largest frame-to-frame |delta| (sparse local strobes below flashes floor)."""
    frames = dedupe(frames)
    n = len(frames)
    if n < 4:
        return 0.0, 0.0, 0.0, 0.0
    if frames.shape[1] > THUMB_H or frames.shape[2] > THUMB_W:
        frames = np.array([thumb(f, THUMB_W, THUMB_H) for f in frames], np.float32)
    h, w = (frames.shape[1] // block) * block, (frames.shape[2] // block) * block
    blocks = frames[:, :h, :w].reshape(n, h // block, block, w // block, block).mean(axis=(2, 4))
    diffs = np.abs(np.diff(blocks, axis=0))
    block_jitter = float(np.median(diffs, axis=0).max())
    means = np.sort(frames.mean(axis=(1, 2)))
    trimmed = means[2:-2] if len(means) >= 8 else means
    luma_drift = float(trimmed[-1] - trimmed[0])
    if n >= 8:
        pad = np.pad(blocks, ((3, 3), (0, 0), (0, 0)), mode="edge")
        smooth = np.median(np.stack([pad[k : k + n] for k in range(7)], axis=0), axis=0)
        sdiff = np.diff(smooth, axis=0)
        pos_travel = np.maximum(0.0, sdiff).sum(axis=0)
        neg_travel = np.maximum(0.0, -sdiff).sum(axis=0)
        block_pulse = float(np.minimum(pos_travel, neg_travel).max())
    else:
        block_pulse = 0.0
    block_step4 = float(np.sort(diffs, axis=0)[-4].max()) if len(diffs) >= 5 else 0.0
    return block_jitter, luma_drift, block_pulse, block_step4


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


def is_zero_or_empty_camera(row: list[str]) -> bool:
    """True when a 7-column d3d9_proxy_frames.csv row has a truly empty camera column or a complete all-zero
    (0, 0, 0) unskipped camera. A short row (< 7 columns), cut-short camera text, or bad float is unknown and
    returns False (never excuses a hitch)."""
    if len(row) < 7:
        return False
    if not row[6]:
        return True
    unskipped = [p for p in (pub.split(":") for pub in row[6].split("|")) if len(p) >= 9 and p[2] == "0"]
    if not unskipped:
        return False
    try:
        return all(abs(float(p[3])) < 1e-6 and abs(float(p[4])) < 1e-6 and abs(float(p[5])) < 1e-6 for p in unskipped)
    except ValueError:
        return False


def is_proxy_loading(spot: dict, pf: int, no_remix: bool = False) -> bool:
    """True when proxy frame `pf` explicitly shows a loading screen. In no-Remix runs, proxy sceneDraws is always 0,
    so only the camera rule is used. Fails closed (False) if data for `pf` is missing or short."""
    if not no_remix:
        pg = spot["_data"]["pgate"]
        if pf in pg and len(pg[pf]) >= 2 and int(pg[pf][1]) == 0:
            return True
    prx = spot["_data"]["proxy"]
    if pf in prx and is_zero_or_empty_camera(prx[pf]):
        return True
    return False


def is_loading_frame(spot: dict, tf: int, no_remix: bool = False) -> bool:
    """True when the proxy row with the same frame number `tf` explicitly shows a loading screen.
    When that row is absent, returns False (unknown -> never excuses a hitch)."""
    return is_proxy_loading(spot, tf, no_remix=no_remix)


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
        frames = read_gray(p, width, height)
        x0, y0, x1, y1 = overlay_mask.box_at(width, height)
        frames[:, y0:y1 + 1, x0:x1 + 1] = 0            # every frame the same way, like overlay_mask.mask_pair
        return frames, "screen capture (gdigrab, overlay box masked)"
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
        if not (pids and all(f in prx and len(prx[f]) > 4 for f in pids)
                and all(isinstance(tel.get(f, {}).get("skips"), dict) for f in tids)):
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

    st, val, detail = check_clean_quit_dirs(
        [s["_dir"] for s in good],
        run_dir=run.dir,
        proxy_by_dir={s["_dir"]: s["_data"]["proxy"] for s in good},
    )
    run.add("clean_quit", st, val, "no VK_ERROR_DEVICE_LOST, exit code 0, Terminate logged, no Present > 1000 ms in last 10 s",
            detail, "remix-dxvk.log + drive.log + bridge32.log + d3d9_proxy_frames.csv")


def find_session_log(folder: str, stem: str) -> str | None:
    """Locate a Remix/bridge log by stem in a spot or saved session folder."""
    for rel in (f"{stem}.log", f"rtx-remix_logs_{stem}.log", os.path.join("rtx-remix", "logs", f"{stem}.log")):
        p = os.path.join(folder, rel)
        if os.path.exists(p):
            return p
    return None


def check_clean_quit_dirs(dirs: list[str], run_dir: str | None = None,
                          proxy_by_dir: dict[str, dict[int, list[str]]] | None = None) -> tuple[str, int | None, str]:
    """Extended clean_quit check across spot/session dirs (and optional run_dir):
    1) remix-dxvk.log must exist for every dir and have no VK_ERROR_DEVICE_LOST, and a drive.log QUIT record must
       not show a non-zero exit code; either fails at once, even without Terminate (a crash never logs it).
    2) bridge32.log must contain 'Sending Terminate command to server' (else NOT_MEASURED).
    3) No proxy Present > 1000 ms in the last 10 s before Terminate.
    Counts (without judging) 'were not disposed of' and bridge64 'objects discovered in'."""
    if not dirs:
        return NOT_MEASURED, None, "no spot ran far enough to judge"
    dxvk_logs = [find_session_log(d, "remix-dxvk") for d in dirs]
    if any(p is None for p in dxvk_logs):
        missing_n = sum(1 for p in dxvk_logs if p is None)
        return NOT_MEASURED, None, f"{missing_n} of {len(dirs)} spots have no remix-dxvk.log"

    search_dirs = list(dirs) + ([run_dir] if run_dir and run_dir not in dirs else [])
    lost = [os.path.basename(d) for d, p in zip(dirs, dxvk_logs)
            if p and any("VK_ERROR_DEVICE_LOST" in ln for ln in read_lines(p))]
    # A quit spot's drive.log ends with 'QUIT {json}' holding the game's exit code once it exited.
    bad_exit = []
    for d in dirs:
        dp = os.path.join(d, "drive.log")
        for ln in (read_lines(dp) if os.path.exists(dp) else []):
            if ln.startswith("QUIT "):
                try:
                    q = json.loads(ln[5:])
                except ValueError:
                    continue
                if q.get("exited") and q.get("exitCode") not in (None, 0):
                    bad_exit.append(f"{os.path.basename(d)} exit 0x{int(q['exitCode']) & 0xFFFFFFFF:08X}")
    # Device loss or a crash exit fails the spot even when the crash kept bridge32 from logging Terminate.
    if lost or bad_exit:
        parts = ([f"VK_ERROR_DEVICE_LOST in: {', '.join(lost)}"] if lost else []) + \
                ([f"non-zero exit: {', '.join(bad_exit)}"] if bad_exit else [])
        return FAIL, len(lost) + len(bad_exit), "; ".join(parts)

    b32_logs = [p for d in search_dirs if (p := find_session_log(d, "bridge32")) is not None]
    term_dirs = [d for d in search_dirs
                 if (p := find_session_log(d, "bridge32")) and any("Sending Terminate command to server" in ln for ln in read_lines(p))]
    if not b32_logs or not term_dirs:
        return NOT_MEASURED, None, "bridge32.log has no 'Sending Terminate command to server' (game did not quit cleanly)"

    not_disposed = sum(1 for p in dxvk_logs if p for ln in read_lines(p) if "were not disposed of" in ln)
    b64_leaks = sum(1 for d in search_dirs if (p := find_session_log(d, "bridge64"))
                    for ln in read_lines(p) if "objects discovered in" in ln)

    quit_stalls: list[tuple[str, int, float]] = []
    for d in term_dirs:
        prx = (proxy_by_dir or {}).get(d)
        if prx is None:
            for cand in ("proxy_frames.csv", "d3d9_proxy_frames.csv"):
                cp = os.path.join(d, cand)
                if os.path.exists(cp):
                    prx = read_csv_frames(cp, 7)
                    break
        if not prx:
            continue
        timed = []
        for f, r in prx.items():
            if len(r) >= 4:
                try:
                    timed.append((f, float(r[1]), float(r[3])))
                except ValueError:
                    continue
        if not timed:
            continue
        max_clk = max(c for _, c, _ in timed)
        for f, c, pms in timed:
            if c >= max_clk - 10000.0 and pms > THRESHOLDS["quit_present_max_ms"]:
                quit_stalls.append((os.path.basename(d), f, pms))

    problems = len(lost) + len(quit_stalls)
    parts = []
    if lost:
        parts.append(f"VK_ERROR_DEVICE_LOST in: {', '.join(lost)}")
    if quit_stalls:
        worst = max(quit_stalls, key=lambda x: x[2])
        parts.append(f"{len(quit_stalls)} proxy Present > {THRESHOLDS['quit_present_max_ms']:g} ms in last 10 s "
                     f"(worst {worst[2]:.1f} ms at frame {worst[1]})")
    if not parts:
        parts.append(f"clean Terminate in {len(term_dirs)} log(s), no device loss, no quit Present > 1000 ms")
    parts.append(f"info: {not_disposed} not-disposed, {b64_leaks} bridge64-map lines")
    return (PASS if problems == 0 else FAIL), problems, "; ".join(parts)


_LP_CFG_RE = re.compile(
    r"lightpub:.*?\(.*?\bflare stand-ins\s+(\d+)\b.*?\bstand-in skip unplaced\s+(\d+)\b.*?\bskip moving\s+(\d+)\b"
)
_LP_STRAY_RE = re.compile(
    r"lightpub:\s+stray frame\s+(\d+)\s+lamps\s+\d+\s+without flare\s+\d+\s+\(max\s+(\d+)\)\s+"
    r"flares\s+\d+\s+stand-ins\s+(\d+)\s+on unplaced\s+\d+\s+\(max\s+(\d+)\)\s+"
    r"on moving\s+\d+\s+\(max\s+(\d+)\)\s+dimmed\s+\d+\s+\(max\s+\d+\)\s+"
    r"longest dim\s+\d+\s+frames\s+\(max\s+(\d+)\)"
)


def _find_proxy_log(spot: dict) -> str | None:
    p = spot_path(spot, spot.get("proxyLog"))
    if p and os.path.exists(p):
        return p
    return find_session_log(spot["_dir"], "proxy") or find_session_log(spot["_dir"], "d3d9_proxy")


def _lp_settings(ln: str) -> str:
    """The settings of a lightpub settings line (the text in its parentheses), to tell whether a reload changed them."""
    return ln[ln.index("(", ln.index("lightpub:")):].strip()


def _parse_lightpub_log(path: str) -> list[tuple[tuple[int, int, int], int, int, int, int, int, int, bool]]:
    """Return [( (E, SU, SM), frame, XM, S, UM, MM, TM, reloaded ), ...] from proxy.log. reloaded: a settings line
    between the previous stray line (or the first settings line) and this one changed the settings, so this line's
    counts span two settings and are not held to the limit of the new one."""
    cfg: tuple[int, int, int] | None = None
    cur = start = None
    rows = []
    for ln in read_lines(path):
        m = _LP_CFG_RE.search(ln)
        if m:
            cfg = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            cur = _lp_settings(ln)
            start = cur if start is None else start
            continue
        m = _LP_STRAY_RE.search(ln)
        if m and cfg is not None:
            rows.append((cfg, int(m.group(1)), int(m.group(2)), int(m.group(3)),
                         int(m.group(4)), int(m.group(5)), int(m.group(6)), cur != start))
            start = cur
    return rows


_LP_OPT_CFG = {"weapon_shape": re.compile(r"\bweapon by shape\s+(\d+)"),
               "keep_tracked": re.compile(r"\bkeep tracked\s+(\d+)"),
               "skip_sam_gear": re.compile(r"\bskip sam gear\s+(\d+)")}
_LP_OPT_STRAY = {"sam_gear_max": re.compile(r"\bon sam gear\s+\d+\s+\(max\s+(\d+)\)"),
                 "weapon_flips": re.compile(r"\bweapon flips\s+(\d+)"),
                 "recreated": re.compile(r"\brecreated\s+(\d+)")}
_LP_DIMMED_RE = re.compile(r"\bdimmed\s+\d+\s+\(max\s+(\d+)\)")
S5_WHY = ("S5 report-only (game behaviour: guard rifle flashlights dim and brighten on their own; e.g. light 72977280 "
          "at con_bright_hall stayed dimmed 2060 frames)")
S5_THRESH = f"report only, WARN above {THRESHOLDS['lights_dimmed_max_frames']} frames"
T1_WHY = ("T1 report-only (game behaviour: a guard flashlight fades over many frames before it stops; e.g. light "
          "72976980 at con_bright_hall ran down 534 frames, 18.6 s, before it stopped)")
T1_THRESH = f"report only, WARN above {THRESHOLDS['light_switchoff_max_s']:g} s"


def _parse_lightpub_extras(path: str) -> list[dict]:
    """Per stray line: frame plus the optional settings and stray fields of newer proxies (None when absent).
    Settings come from the last settings line before the stray line ("enabled (", "settings reloaded (", "settings
    reloaded after a missed notice (" all match _LP_CFG_RE). reloaded: as in _parse_lightpub_log."""
    cfg: dict | None = None
    cur = start = None
    rows = []
    for ln in read_lines(path):
        if _LP_CFG_RE.search(ln):
            cfg = {k: (int(m.group(1)) if (m := rx.search(ln)) else None) for k, rx in _LP_OPT_CFG.items()}
            cur = _lp_settings(ln)
            start = cur if start is None else start
            continue
        m = _LP_STRAY_RE.search(ln)
        if m and cfg is not None:
            tail = ln[m.end():]
            row = dict(cfg, frame=int(m.group(1)), dimmed_max=int(_LP_DIMMED_RE.search(ln).group(1)),
                       reloaded=cur != start)
            start = cur
            row.update({k: (int(mm.group(1)) if (mm := rx.search(tail)) else None) for k, rx in _LP_OPT_STRAY.items()})
            rows.append(row)
    return rows


def _lightpub_field_checks(run: Run, extras: dict[str, list[dict]], log_src: str) -> None:
    """S6 flare_sam_gear_balls, lights_weapon_flips and lights_recreated. A stray line or its settings line without
    the field (an older proxy) makes the check NOT MEASURED. A stray line whose 600-frame window contains a reload
    that changed the settings counts under two settings, so it is reported, not held to the new setting's limit."""
    rows = [(n, r) for n, rs in extras.items() for r in rs]
    specs = (
        ("flare_sam_gear_balls", "skip_sam_gear", "sam_gear_max", "0 when skip sam gear 1, no limit with 0",
         "c78eb25", lambda key, val: 0 if key == 1 else None),
        ("lights_weapon_flips", "weapon_shape", "weapon_flips",
         "0 per stray line when weapon by shape 1 or 2, report only with 0", "fbe011f",
         lambda key, val: 0 if key >= 1 else None),
        ("lights_recreated", "keep_tracked", "recreated",
         f"<= {THRESHOLDS['lights_recreated_max_kept']} per stray line when keep tracked 1 (GUESS), report only with 0",
         "fbe011f", lambda key, val: THRESHOLDS["lights_recreated_max_kept"] if key == 1 else None),
    )
    for check, key, field, thresh, since, limit_of in specs:
        if not rows:
            run.add(check, NOT_MEASURED, None, thresh, "no lightpub stray lines", log_src)
            continue
        old = sorted({n for n, r in rows if r[key] is None or r[field] is None})
        if old:
            run.add(check, NOT_MEASURED, None, thresh,
                    f"proxy older than {since}: no '{field}' or '{key}' field on lightpub lines for: " + ", ".join(old),
                    log_src)
            continue
        bad, limited, report = [], [], []
        for n, r in rows:
            lim = None if r["reloaded"] else limit_of(r[key], r[field])
            (limited if lim is not None else report).append(r[field])
            if lim is not None and r[field] > lim:
                bad.append(f"{n} frame {r['frame']} {field}={r[field]} ({key} {r[key]})")
        worst = max(r[field] for _, r in rows)
        detail = (f"limited lines: {len(limited)} (max {max(limited, default=0)}); "
                  f"report-only lines: {len(report)} (max {max(report, default=0)}, total {sum(report)}; "
                  f"{sum(1 for _, r in rows if r['reloaded'])} spanning a settings change)")
        if bad:
            detail = f"{len(bad)} violation(s), first {bad[0]}; " + detail
        run.add(check, FAIL if bad else PASS, worst, thresh, detail, log_src)


def stop_all_ray_traced(spot: dict, w: dict) -> tuple[bool, str | None]:
    """(True, None) only when the stop has >= 1 telemetry frame and every frame has rayTraced=True;
    otherwise (False, reason) so picture checks fail closed as NOT MEASURED."""
    tel = spot["_data"]["telemetry"]
    tids = frames_in(w, "telemetry")
    if not tids or any(f not in tel for f in tids):
        return False, "no telemetry for the stop"
    if any(not isinstance(tel[f].get("rayTraced"), bool) for f in tids):
        return False, "rayTraced missing"
    if any(not tel[f]["rayTraced"] for f in tids):
        return False, "not all frames ray traced"
    return True, None


def _time_to_first_rt_text(ltel: dict[int, dict]) -> str:
    """'timeToFirstRayTracedS=<x>s preRtCompiles=<n>' for one launch's telemetry: the sum of frameTimeMs from its
    first line to the first rayTraced=true line, and the highest remixShaderCompiles before it ('-' without it)."""
    t_ms, first_rt_s, pre = 0.0, None, None
    for fr in sorted(ltel):
        r = ltel[fr]
        cval = r.get("remixShaderCompiles")
        if isinstance(cval, (int, float)):
            pre = max(pre or 0, int(cval))
        if r.get("rayTraced") is True:
            first_rt_s = t_ms / 1000.0
            break
        if isinstance(r.get("frameTimeMs"), (int, float)):
            t_ms += float(r["frameTimeMs"])
    tt = f"{first_rt_s:.1f}s" if first_rt_s is not None else "never RT"
    return f"timeToFirstRayTracedS={tt} preRtCompiles={pre if pre is not None else '-'}"


def _launch_compile_state(ltel: dict[int, dict], loading) -> tuple[str, float]:
    """One launch's telemetry -> ('missing' | 'rt' | 'never' | 'wait', seconds in the shader compile wait).
    'rt': some frame ray traced. 'never': remixShaderCompiles present on every frame and always 0. 'wait': never ray
    traced; the wait sums frameTimeMs over frames with remixShaderCompiles > 0, or without that field over frames
    with compile_wait_min_ms <= frameTimeMs - gpuTimeMs <= compile_wait_max_ms that loading(frame) does not excuse."""
    frames = sorted(ltel)
    if not frames or not all(isinstance(ltel[f].get("rayTraced"), bool)
                             and isinstance(ltel[f].get("frameTimeMs"), (int, float))
                             and isinstance(ltel[f].get("gpuTimeMs"), (int, float)) for f in frames):
        return "missing", 0.0
    if any(ltel[f]["rayTraced"] is True for f in frames):
        return "rt", 0.0
    comp = [ltel[f].get("remixShaderCompiles") for f in frames]
    has_comp = all(isinstance(c, (int, float)) for c in comp)
    if has_comp and max(comp) == 0:
        return "never", 0.0
    lo, hi = THRESHOLDS["compile_wait_min_ms"], THRESHOLDS["compile_wait_max_ms"]
    wait_s = 0.0
    for f, c in zip(frames, comp):
        ft, gt = float(ltel[f]["frameTimeMs"]), float(ltel[f]["gpuTimeMs"])
        if (c > 0) if has_comp else (lo <= ft - gt <= hi and not loading(f)):
            wait_s += ft / 1000.0
    return "wait", wait_s


def ray_tracing_checks(run: Run, good: list[dict], tsrc: str) -> None:
    """remix_ray_tracing (non-goggles scene RT share >= 99% GUESS + every still stop 100% ray traced) and
    remix_compile_stall (>= 300 consecutive rayTraced=false frames with 25-40 ms CPU wait, GUESS)."""
    min_rt = THRESHOLDS["min_scene_rt_share"]
    rt_thresh = f"all still stops 100% RT, scene RT >= {min_rt:.0%} (GUESS)"
    cs_lim = THRESHOLDS["compile_stall_min_frames"]
    w_lo, w_hi = THRESHOLDS["compile_stall_wait_min_ms"], THRESHOLDS["compile_stall_wait_max_ms"]
    cs_thresh = f"< {cs_lim} consecutive rayTraced=false frames with {w_lo:g}..{w_hi:g} ms wait (GUESS)"
    if not good:
        run.add("remix_ray_tracing", NOT_MEASURED, None, rt_thresh, "no spot ran far enough to judge", tsrc)
        run.add("remix_compile_stall", NOT_MEASURED, None, cs_thresh, "no spot ran far enough to judge", tsrc)
        run.add("compile_wait_stuck", NOT_MEASURED, None,
                f"< {THRESHOLDS['compile_wait_stuck_s']:g} s in compile wait without rayTraced=true (GUESS)",
                "no spot ran far enough to judge", tsrc)
        run.add("never_ray_traced", NOT_MEASURED, None, "0 spots with rayTraced=false and remixShaderCompiles==0",
                "no spot ran far enough to judge", tsrc)
        return

    # 1. remix_ray_tracing
    shares, bad_stops, launch_times, missing_rt = [], [], [], []
    for s in good:
        tel, pg = s["_data"]["telemetry"], s["_data"]["pgate"]
        spairs = scene_pairs(s)
        want_s, recs_s = coverage(spairs, "telemetry", "telemetry")
        have_rt = sum(1 for r in recs_s if isinstance(r.get("rayTraced"), bool))
        want_pg, pg_rows = coverage(spairs, "proxy", "pgate")
        have_pg = sum(1 for r in pg_rows if len(r) >= 5)
        if want_s == 0 or have_rt < want_s or want_pg == 0 or have_pg < want_pg:
            missing_rt.append(s["name"])
            continue
        non_goggles_total, non_goggles_rt = 0, 0
        for _, w in spairs:
            for tf in frames_in(w, "telemetry"):
                in_goggles = any(f in pg and len(pg[f]) >= 5 and int(pg[f][4]) == 1 for f in range(tf - 3, tf + 4))
                if not in_goggles:
                    non_goggles_total += 1
                    if tel[tf]["rayTraced"]:
                        non_goggles_rt += 1
        shares.append((s["name"], non_goggles_rt / non_goggles_total if non_goggles_total else 1.0))
        for w in s["windows"]:
            if w["kind"] == "still":
                ok_rt, _ = stop_all_ray_traced(s, w)
                if not ok_rt:
                    bad_stops.append(f"{s['name']}/{w['name']}")
        # timeToFirstRayTracedS & preRtCompiles from whole-launch telemetry (telemetry_launch.jsonl);
        # never report 0 when only spot telemetry exists ("not measured").
        launch_path = spot_path(s, s.get("telemetryLaunch")) or os.path.join(s["_dir"], "telemetry_launch.jsonl")
        if os.path.exists(launch_path):
            launch_times.append(f"{s['name']} {_time_to_first_rt_text(read_telemetry(launch_path)[0])}")
        else:
            launch_times.append(f"{s['name']} timeToFirstRayTracedS=not measured preRtCompiles=-")

    if missing_rt or not shares:
        run.add("remix_ray_tracing", NOT_MEASURED, None, rt_thresh,
                "missing rayTraced or proxy_gate for: " + (", ".join(missing_rt) or "no spot"), tsrc)
    else:
        worst_share = min(sh for _, sh in shares)
        share_str = ", ".join(f"{n} {sh:.1%}" for n, sh in shares)
        lt_str = "; ".join(launch_times)
        detail = (f"{len(bad_stops)} non-RT still stop(s) ({', '.join(bad_stops[:5])}); "
                  if bad_stops else "") + f"scene RT: {share_str} ({lt_str})"
        ok_rt_all = (not bad_stops) and (worst_share >= min_rt)
        run.add("remix_ray_tracing", PASS if ok_rt_all else FAIL, round(worst_share, 4), rt_thresh, detail, tsrc)

    # 2. remix_compile_stall
    worst_run, stall_runs, missing_cs = 0, [], []
    for s in good:
        tel = s["_data"]["telemetry"]
        frames = sorted(tel)
        valid = [fr for fr in frames if isinstance(tel[fr].get("rayTraced"), bool)
                 and isinstance(tel[fr].get("frameTimeMs"), (int, float))
                 and isinstance(tel[fr].get("gpuTimeMs"), (int, float))]
        if not frames or len(valid) < len(frames):
            missing_cs.append(s["name"])
            continue
        cur_start = cur_len = 0
        for fr in frames:
            r = tel[fr]
            wait = float(r["frameTimeMs"]) - float(r["gpuTimeMs"])
            if r["rayTraced"] is False and w_lo <= wait <= w_hi:
                if cur_len == 0:
                    cur_start = fr
                cur_len += 1
                if cur_len > worst_run:
                    worst_run = cur_len
            else:
                if cur_len >= cs_lim:
                    stall_runs.append((s["name"], cur_start, fr - 1, cur_len))
                cur_len = 0
        if cur_len >= cs_lim:
            stall_runs.append((s["name"], cur_start, frames[-1], cur_len))

    if missing_cs:
        run.add("remix_compile_stall", NOT_MEASURED, None, cs_thresh,
                "missing rayTraced/frameTimeMs/gpuTimeMs for: " + ", ".join(missing_cs), tsrc)
    elif stall_runs:
        s0, a0, b0, n0 = max(stall_runs, key=lambda x: x[3])
        run.add("remix_compile_stall", FAIL, n0, cs_thresh,
                f"{s0} frames {a0}..{b0} ({n0} frames) rayTraced=false with {w_lo:g}..{w_hi:g} ms wait", tsrc)
    else:
        run.add("remix_compile_stall", PASS, worst_run, cs_thresh,
                f"longest rayTraced=false 25..40ms-wait run: {worst_run} frames", tsrc)

    # 3. compile_wait_stuck & never_ray_traced
    stuck_lim_s = THRESHOLDS["compile_wait_stuck_s"]
    cw_thresh = f"< {stuck_lim_s:g} s in compile wait without rayTraced=true (GUESS)"
    stuck_spots, never_rt_spots, missing_cw = [], [], []
    worst_wait_s = 0.0
    for s in good:
        launch_path = spot_path(s, s.get("telemetryLaunch")) or os.path.join(s["_dir"], "telemetry_launch.jsonl")
        ltel = read_telemetry(launch_path)[0] if os.path.exists(launch_path) else s["_data"]["telemetry"]
        state, wait_s = _launch_compile_state(ltel, lambda fr, s=s: is_proxy_loading(s, fr))
        if state == "missing":
            missing_cw.append(s["name"])
            continue
        if state == "rt":
            continue
        if state == "never":
            never_rt_spots.append(f"{s['name']} (0 compiles)")
            continue
        worst_wait_s = max(worst_wait_s, wait_s)
        if wait_s >= stuck_lim_s:
            stuck_spots.append(f"{s['name']} ({wait_s:.1f}s)")
    if missing_cw:
        run.add("compile_wait_stuck", NOT_MEASURED, None, cw_thresh,
                "missing rayTraced/frameTimeMs/gpuTimeMs for: " + ", ".join(missing_cw), tsrc)
        run.add("never_ray_traced", NOT_MEASURED, None, "rayTraced=true when remixShaderCompiles==0",
                "missing rayTraced/frameTimeMs/gpuTimeMs for: " + ", ".join(missing_cw), tsrc)
    else:
        run.add("compile_wait_stuck", PASS if not stuck_spots else FAIL, round(worst_wait_s, 1), cw_thresh,
                f"compile-wait hang: {', '.join(stuck_spots)}" if stuck_spots
                else f"no compile-wait hang (max {worst_wait_s:.1f}s)", tsrc)
        run.add("never_ray_traced", PASS if not never_rt_spots else FAIL, len(never_rt_spots),
                "0 spots with rayTraced=false and remixShaderCompiles==0",
                f"never ray traced with 0 compiles: {', '.join(never_rt_spots)}" if never_rt_spots
                else "no 0-compile non-RT launch", tsrc)


def lightpub_checks(run: Run, good: list[dict], reference: dict[str, dict] | None) -> None:
    """Worker B's lightpub checks: S1..S5 from proxy.log and T1 from lightpub_trace.csv."""
    log_src = "proxy.log (lightpub)"
    trace_src = "lightpub_trace.csv"
    parsed: dict[str, list[tuple[tuple[int, int, int], int, int, int, int, int, int]]] = {}
    extras: dict[str, list[dict]] = {}
    missing_log = []
    for s in good:
        lp = _find_proxy_log(s)
        rows = _parse_lightpub_log(lp) if lp else []
        if not rows:
            missing_log.append(s["name"])
        else:
            parsed[s["name"]] = rows
            extras[s["name"]] = _parse_lightpub_extras(lp)
    _lightpub_field_checks(run, {} if (not good or missing_log) else extras, log_src)

    if not good or missing_log:
        msg = ("no spot ran far enough to judge" if not good
               else "missing lightpub stray/settings lines in proxy.log for: " + ", ".join(missing_log))
        run.add("flare_standins_off", NOT_MEASURED, None, "0 when flare stand-ins 0", msg, log_src)
        run.add("flare_unplaced_balls", NOT_MEASURED, None,
                f"0 when SU=1, <= {THRESHOLDS['flare_unplaced_max_noskip']} when SU=0 (measured live)", msg, log_src)
        run.add("flare_moving_balls", NOT_MEASURED, None, "0 when SM=1", msg, log_src)
        run.add("lamps_without_flare", NOT_MEASURED, None,
                f"<= reference + {THRESHOLDS['lamps_without_flare_margin']} (GUESS)", msg, log_src)
        run.add("lights_dimmed_linger", NOT_MEASURED, None, S5_THRESH, msg, log_src)
    else:
        all_rows = [(name, *row) for name, rows in parsed.items() for row in rows]
        # S1: flare_standins_off
        # A line whose window spans a settings change (reloaded) is not held to the new settings' limits.
        off_rows = [(n, fr, S) for n, (E, _, _), fr, _, S, _, _, _, rl in all_rows if E == 0 and not rl]
        bad_s1 = [r for r in off_rows if r[2] > 0]
        worst_s1 = max((r[2] for r in off_rows), default=0)
        run.add("flare_standins_off", PASS if not bad_s1 else FAIL, worst_s1, "0 when flare stand-ins 0",
                f"max stand-ins S={worst_s1} with flare stand-ins 0 ({len(bad_s1)} bad line(s))", log_src)

        # S2: flare_unplaced_balls
        noskip_lim = THRESHOLDS["flare_unplaced_max_noskip"]
        bad_s2 = [(n, fr, SU, UM) for n, (_, SU, _), fr, _, _, UM, _, _, rl in all_rows
                  if not rl and UM > (0 if SU == 1 else noskip_lim)]
        worst_um = max(r[5] for r in all_rows)
        run.add("flare_unplaced_balls", PASS if not bad_s2 else FAIL, worst_um,
                f"0 when SU=1, <= {noskip_lim} when SU=0 (measured live)",
                f"worst max-unplaced UM={worst_um} ({len(bad_s2)} violation(s))", log_src)

        # S3: flare_moving_balls
        sm1_rows = [(n, fr, MM) for n, (_, _, SM), fr, _, _, _, MM, _, rl in all_rows if SM == 1 and not rl]
        bad_s3 = [r for r in sm1_rows if r[2] > 0]
        worst_mm = max((r[2] for r in sm1_rows), default=0)
        run.add("flare_moving_balls", PASS if not bad_s3 else FAIL, worst_mm, "0 when SM=1",
                f"worst max-moving MM={worst_mm} with SM=1 ({len(bad_s3)} violation(s))", log_src)

        # S4: lamps_without_flare
        margin = THRESHOLDS["lamps_without_flare_margin"]
        thresh_s4 = f"<= reference + {margin} (GUESS)"
        if reference is None:
            run.add("lamps_without_flare", NOT_MEASURED, None, thresh_s4,
                    "no reference run given (--reference), so not compared", log_src)
        else:
            diffs, missing_ref = [], []
            for s in good:
                name = s["name"]
                now_xm = max(r[2] for r in parsed[name])
                ref_s = reference.get(name)
                ref_lp = _find_proxy_log(ref_s) if ref_s else None
                ref_rows = _parse_lightpub_log(ref_lp) if ref_lp else []
                if not ref_rows:
                    missing_ref.append(name)
                else:
                    ref_xm = max(r[2] for r in ref_rows)
                    diffs.append((now_xm - ref_xm, name, now_xm, ref_xm))
            if not diffs or missing_ref:
                run.add("lamps_without_flare", NOT_MEASURED, None, thresh_s4,
                        "no reference proxy.log lightpub baseline for: " + (", ".join(missing_ref) or "no spot"),
                        log_src)
            else:
                worst_jump = max(d[0] for d in diffs)
                detail = f"worst jump {worst_jump:+d} over reference; " + "; ".join(
                    f"{n} {a} vs {b}" for _, n, a, b in diffs)
                run.add("lamps_without_flare", PASS if worst_jump <= margin else FAIL, worst_jump, thresh_s4,
                        detail, log_src)

        # S5: lights_dimmed_linger, report only (worker B, approved by the coordinator)
        worst_tm = max(r[7] for r in all_rows)
        worst_dm = max(r["dimmed_max"] for rs in extras.values() for r in rs)
        run.add("lights_dimmed_linger", PASS if worst_tm <= THRESHOLDS["lights_dimmed_max_frames"] else WARN,
                worst_tm, S5_THRESH, f"{S5_WHY}: longest dim {worst_tm} frames, dimmed {worst_dm}", log_src)

    # T1: light_switchoff_lag from lightpub_trace.csv, report only (worker B, approved by the coordinator)
    t1_thresh = T1_THRESH
    if not good:
        run.add("light_switchoff_lag", NOT_MEASURED, None, t1_thresh, "no spot ran far enough to judge", trace_src)
        return
    traces: list[tuple[dict, str]] = []
    missing_trace = []
    for s in good:
        lp = _find_proxy_log(s)
        cand = os.path.join(os.path.dirname(lp), "lightpub_trace.csv") if lp else os.path.join(s["_dir"], "lightpub_trace.csv")
        if not os.path.exists(cand):
            cand = os.path.join(s["_dir"], "lightpub_trace.csv")
        if os.path.exists(cand):
            traces.append((s, cand))
        else:
            missing_trace.append(s["name"])
    if missing_trace:
        run.add("light_switchoff_lag", NOT_MEASURED, None, t1_thresh,
                "missing lightpub_trace.csv for: " + ", ".join(missing_trace), trace_src)
        return

    lags: list[tuple[float, int, str, str]] = []
    missing_fps = []
    for s, tp in traces:
        want_f, recs_f = coverage(scene_pairs(s), "telemetry", "telemetry")
        vals_f = [r["frameTimeMs"] for r in recs_f if isinstance(r.get("frameTimeMs"), (int, float))]
        mean_fps = (1000.0 / max(float(np.mean(vals_f)), 1e-3)) if (want_f > 0 and len(vals_f) == want_f) else None
        run_start: dict[str, int | None] = {}
        for ln in read_lines(tp):
            parts = [x.strip() for x in ln.split(",")]
            if len(parts) < 6 or not parts[0].isdigit():
                continue
            fr, _, lid, chg, old_v, new_v = int(parts[0]), parts[1], parts[2], parts[3], parts[4], parts[5]
            if chg == "started":
                run_start[lid] = None
            elif chg == "brightness":
                try:
                    if float(new_v) < float(old_v):
                        if run_start.get(lid) is None:
                            run_start[lid] = fr
                    else:
                        run_start[lid] = None
                except ValueError:
                    run_start[lid] = None
            elif chg == "stopped":
                st_fr = run_start.get(lid)
                lag_fr = (fr - st_fr) if st_fr is not None else 0
                run_start[lid] = None
                if lag_fr == 0:
                    lags.append((0.0, 0, s["name"], lid))
                elif mean_fps is None:
                    missing_fps.append(s["name"])
                else:
                    lags.append((lag_fr / mean_fps, lag_fr, s["name"], lid))
    if missing_fps:
        run.add("light_switchoff_lag", NOT_MEASURED, None, t1_thresh,
                "no scene mean FPS for: " + ", ".join(sorted(set(missing_fps))), trace_src)
    elif not lags:
        run.add("light_switchoff_lag", PASS, 0.0, t1_thresh, "0 stopped events in lightpub_trace.csv", trace_src)
    else:
        worst_s, worst_fr, w_spot, w_id = max(lags, key=lambda x: x[0])
        run.add("light_switchoff_lag", PASS if worst_s <= THRESHOLDS["light_switchoff_max_s"] else WARN,
                round(worst_s, 3), t1_thresh,
                f"{T1_WHY}: longest downward run {worst_fr} frames ({worst_s:.2f} s) at {w_spot} id={w_id} "
                f"({len(lags)} stopped event(s))",
                trace_src)


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


def _lag_stats(raw) -> dict | None:
    vals = [float(x) for x in raw if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)]
    if len(vals) < 20:
        return None
    return {"median": float(np.median(vals)), "spread": float(np.percentile(vals, 75) - np.percentile(vals, 25)),
            "n": len(vals)}


def spot_mouse_lag(spot: dict) -> dict | None:
    """Game share of the mouse lag from spot.json['mouse_lag'].trials[].gameMs (mouse turn to the first proxy frame
    with the turned camera): median, IQR spread and count; None unless at least 20 trials have a finite gameMs."""
    ml = spot.get("mouse_lag")
    trials = ml.get("trials") if isinstance(ml, dict) else None
    if not isinstance(trials, list):
        return None
    return _lag_stats([t.get("gameMs") for t in trials if isinstance(t, dict)])


def spot_screen_lag(spot: dict) -> dict | None:
    """Mouse turn to a changed desktop grab (spot.json mouse_lag.samples_ms); None unless >= 20 finite samples."""
    ml = spot.get("mouse_lag")
    raw = ml.get("samples_ms") if isinstance(ml, dict) else None
    return _lag_stats(raw) if isinstance(raw, list) else None


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


def _fmt_lag(s: dict | None) -> str:
    if s is None:
        return "not measured"
    return f"{s['median']:.1f} ms (spread {s['spread']:.1f}, {s['n']} turns)"


def input_lag_check(run: Run, good: list[dict], reference: dict[str, dict] | None) -> None:
    """input_latency_ms: each spot's median game share of the mouse lag (spot_mouse_lag, >= 20 turns) against the same
    spot in the no-Remix reference run (--reference), within input_lag_margin_ms (a GUESS). It is the mouse turn to the
    first game frame with the turned camera and leaves out everything after the game hands the frame on, so for Remix
    it is a lower bound. Without --reference or when any spot lacks it: NOT MEASURED, there is no fixed limit.
    input_latency_screen_ms: the screen share (samples_ms) is always NOT MEASURED until the desktop grab is shown to
    work in Remix runs; its numbers go in the detail. It is in SOFT_NM, so it does not fail the gate."""
    sources = "spot.json mouse_lag.samples_ms (desktop grab): this run and --reference"
    screen = "; ".join(f"{s['name']} {_fmt_lag(spot_screen_lag(s))}" + (
        f" vs {_fmt_lag(spot_screen_lag(reference[s['name']]))}" if reference and s["name"] in reference else "")
        for s in good)
    run.add("input_latency_screen_ms", NOT_MEASURED, None, "not judged until the screen grab works",
            f"screen share not trusted yet (grab repeats pictures in Remix runs); numbers: {screen or 'no spot'}",
            sources)
    src = "spot.json mouse_lag.trials gameMs (mouse turn to the first game frame with the turned camera; leaves out " \
          "everything after the game hands the frame on, a lower bound for Remix): this run and --reference"
    margin = THRESHOLDS["input_lag_margin_ms"]
    threshold = f"<= reference + {margin:g} ms per spot (GUESS margin)"
    ours = {s["name"]: spot_mouse_lag(s) for s in good}
    shown = "; ".join(f"{n} {_fmt_lag(v)}" for n, v in ours.items())
    if reference is None:
        run.add("input_latency_ms", NOT_MEASURED, None, threshold,
                f"no reference run given (--reference), so not compared; this run: {shown or 'no spot measured'}", src)
        return
    diffs, missing = [], []
    for name, now in ours.items():
        base = spot_mouse_lag(reference[name]) if name in reference else None
        if now is None or base is None:
            missing.append(name)
        else:
            diffs.append((now["median"] - base["median"], name, now, base))
    if not diffs or missing:
        run.add("input_latency_ms", NOT_MEASURED, None, threshold,
                "no comparable mouse_lag for: " + (", ".join(missing) or "no spot measured"), src)
        return
    worst = max(d[0] for d in diffs)
    detail = f"worst spot {worst:+.2f} ms over reference; " + "; ".join(
        f"{n} {_fmt_lag(a)} vs {_fmt_lag(b)}" for _, n, a, b in diffs)
    run.add("input_latency_ms", PASS if worst <= margin else FAIL, round(worst, 2), threshold, detail, src)


def proxy_over_50ms(run: Run, name: str, col: int, what: str, limit_key: str, pairs, no_remix: bool) -> None:
    """Count proxy rows in the windows `pairs` whose row index `col` is above 50 ms, loading frames excluded
    (is_proxy_loading). Index 3 is the time inside Present (present_stalls), index 2 the gap since the previous Present
    entry (frame_gap_over_50ms). NOT MEASURED when any frame in the windows lacks that column."""
    src = "d3d9_proxy_frames.csv"
    if not pairs:
        run.add(name, NOT_MEASURED, None, "0", "no windows of this kind were run", src)
        return
    want, rows = coverage(pairs, "proxy", "proxy")
    have = sum(1 for r in rows if len(r) > col)
    if want == 0 or have < want:
        run.add(name, NOT_MEASURED, None, "0", f"proxy trace has {have} of {want} frames with column {col}", src)
        return
    over, excluded, worst_ms, worst_f = 0, 0, -1.0, 0
    starts = []   # rows over 50 ms at a window's first frame a, which frames_in leaves out (it counts a+1..b)
    for s, w in pairs:
        prx = s["_data"]["proxy"]
        a = (w.get("proxy") or (0, 0))[0]
        try:
            if float(prx[a][col]) > 50.0:
                starts.append(f"{float(prx[a][col]):.1f} ms at frame {a}")
        except (KeyError, IndexError, ValueError):
            pass
        for pf in frames_in(w, "proxy"):
            try:
                ms = float(prx[pf][col])
            except (IndexError, ValueError):
                continue
            if ms > 50.0 and is_proxy_loading(s, pf, no_remix=no_remix):
                excluded += 1
                continue
            if ms > worst_ms:
                worst_ms, worst_f = ms, pf
            if ms > 50.0:
                over += 1
    detail = f"{over} of {want} frames with {what} > 50 ms (worst {worst_ms:.1f} ms at frame {worst_f})" + (
        f" ({excluded} loading-screen frames excluded)" if excluded else "") + (
        f"; {len(starts)} over 50 ms at window starts, not counted: {', '.join(starts)}" if starts else "")
    run.add(name, PASS if over <= THRESHOLDS[limit_key] else FAIL, over, "0", detail, src)


def spot_proxy_interval(spot: dict, no_remix: bool) -> tuple[float, int, int] | None:
    """(median FPS, frames over 50 ms, frames) from proxy row index 2 (Present-to-Present gap) over one spot's scene
    windows, loading frames excluded; None when any frame lacks the column."""
    vals = []
    for s, w in scene_pairs(spot):
        prx = s["_data"]["proxy"]
        for pf in frames_in(w, "proxy"):
            try:
                ms = float(prx[pf][2])
            except KeyError:
                continue
            except (IndexError, ValueError):
                return None
            if not is_proxy_loading(s, pf, no_remix=no_remix):
                vals.append(ms)
    if not vals:
        return None
    return 1000.0 / max(float(np.median(vals)), 1e-3), sum(1 for v in vals if v > 50.0), len(vals)


def frame_interval_check(run: Run, good: list[dict], reference: dict[str, dict] | None, no_remix: bool) -> None:
    """frame_interval_vs_reference: report only, never FAIL (coordinator, Nov 16: a ray-traced picture will not match
    the original's 150-200 FPS; the FPS rules are min_median_fps and fps_regression). Prints each spot's median FPS
    and frames over 50 ms from the proxy frame gap, this run next to the reference. WARN when anything is missing."""
    src = "d3d9_proxy_frames.csv index 2 (Present-to-Present gap), scene windows: this run and --reference (no Remix)"
    parts, missing = [], []
    for s in good:
        now = spot_proxy_interval(s, no_remix)
        ref = spot_proxy_interval(reference[s["name"]], True) if reference and s["name"] in reference else None
        fmt = lambda v: "not measured" if v is None else f"{v[0]:.1f} FPS, {v[1]} of {v[2]} frames over 50 ms"
        parts.append(f"{s['name']} {fmt(now)} vs original {fmt(ref)}")
        if now is None or ref is None:
            missing.append(s["name"])
    if reference is None:
        parts.insert(0, "no reference run given (--reference)")
    run.add("frame_interval_vs_reference", WARN if missing or reference is None or not good else PASS, None,
            "report only", "; ".join(parts) or "no spot measured", src)


GLOW_BLOBS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "analysis", "glow_blobs.py")


def glow_blobs_stop(remix_spot: str, orig_spot: str, still: str, orig_still: str) -> tuple[int, str]:
    """Run worker B's glow_blobs.py on one stop: (exit code, output). It reads only the two stills and the proxy camera
    (pose check), never light, flare or ball lists. 0 PASS, 1 FAIL, 2 NOT MEASURED (poses apart)."""
    try:
        p = subprocess.run([sys.executable, "-I", GLOW_BLOBS, "--remix-spot", remix_spot, "--orig-spot", orig_spot,
                            "--still", still, "--orig-still", orig_still], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, f"glow_blobs.py did not run: {e}"
    return p.returncode, (p.stdout + p.stderr).strip()


_GLOW_COUNTS = {  # check -> the glow_blobs.py line that carries its count (worker B, 9ddc95d); limit 0, puffs none
    "lights_orbs_unmatched": re.compile(r"glow_blobs: unmatched (\d+) of (\d+) blobs"),
    "lights_puffs": re.compile(r"glow_blobs: puffs (\d+)()"),
    "lights_glows_lost": re.compile(r"glow_blobs: lost (\d+) of (\d+) original glows"),
}
_GLOW_REPORT_ONLY = {  # check -> why it never decides the verdict (WARN above 0; NOT MEASURED is in SOFT_NM)
    "lights_puffs": "a puff is also a surface Remix lights that the original leaves dark, so it cannot tell a "
                    "floating light from correct extra lighting; coordinator decision with worker B's 9ddc95d",
}


def glow_checks(run: Run, good: list[dict], reference: dict[str, dict] | None, ref_no_remix: bool) -> None:
    """Symptom checks for ledger rows 1 and 2 from worker B's glow_blobs.py (pictures only): per still and switch
    stop, the Remix still against the original game's still at the same pose. Three counts:
    lights_orbs_unmatched (bright blobs the original does not show; limit 0 per stop), lights_puffs (large bright
    areas the original leaves dark; also counts surfaces Remix lights that the original leaves dark, so it is report
    only: WARN above 0, its NOT MEASURED in SOFT_NM) and lights_glows_lost (the reverse: a lamp glow the original
    shows that Remix lost; limit 0 per stop).
    Switch stops are compared with the original's y0_p0; the pose check decides whether that is the same pose.
    Each check: FAIL when any compared stop is over 0; otherwise NOT MEASURED when a spot has no no-Remix reference,
    glow_blobs.py broke on a stop, or no stop could be compared; PASS otherwise. Stops whose poses are too far apart
    (exit 2) are listed and not compared."""
    src = "glow_blobs.py: the stop's Remix still against the same spot's still in --reference (no Remix)"
    threshold = "0 per stop"
    if reference is None or not ref_no_remix:
        why = "no reference run given (--reference)" if reference is None else "the reference run is not a no-Remix run"
        for name in _GLOW_COUNTS:
            run.add(name, NOT_MEASURED, None, threshold, why, src)
        return
    jobs, missing, apart = [], [], []
    for s in good:
        ref = reference.get(s["name"])
        if ref is None:
            missing.append(s["name"])
            continue
        ref_stills = {w["name"] for w in ref["windows"] if w.get("frames")}
        for w in s["windows"]:
            if not w.get("frames") or w["kind"] not in ("still", "switch"):
                continue
            orig = w["name"] if w["name"] in ref_stills else ("y0_p0" if w["kind"] == "switch" else None)
            if orig not in ref_stills:
                continue
            placed, why = reference_position(s, w["name"], ref, orig)
            if placed:
                jobs.append((s["name"], s["_dir"], ref["_dir"], w["name"], orig))
            else:
                apart.append(f"{s['name']} {why}")      # the reference position rule: not compared
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        outs = list(pool.map(lambda j: glow_blobs_stop(j[1], j[2], j[3], j[4]), jobs))
    counts = {name: [] for name in _GLOW_COUNTS}    # name -> [(label, n, of)] per compared stop
    errors = []
    for (spot, _, _, stop, orig), (code, out) in zip(jobs, outs):
        first = (out.splitlines()[0] if out else f"exit {code}").replace("glow_blobs: ", "")
        found = {name: rx.search(out) for name, rx in _GLOW_COUNTS.items()}
        if code == 2 and "NOT MEASURED" in out:
            apart.append(f"{spot} {stop}: {first}")       # poses too far apart: a known gap, listed
        elif code in (0, 1) and all(found.values()):
            label = f"{spot} {stop}" + (f" (vs original {orig})" if orig != stop else "")
            for name, m in found.items():
                counts[name].append((label, int(m[1]), m[2]))
        else:
            errors.append(f"{spot} {stop}: exit {code}, {first}")   # the script broke: not measured
    tail = ((f"; poses apart, not compared: {'; '.join(apart)}" if apart else "")
            + (f"; glow_blobs.py errors: {'; '.join(errors)}" if errors else "")
            + (f"; no reference for {', '.join(missing)}" if missing else ""))
    for name, rows in counts.items():
        bad = [(lab, n, of) for lab, n, of in rows if n > 0]
        detail = (f"{len(bad)} of {len(rows)} compared stops over 0"
                  + (": " + "; ".join(f"{lab} {n}" + (f" of {of}" if of else "") for lab, n, of in bad) if bad else "")
                  + tail)
        if name in _GLOW_REPORT_ONLY:
            detail = f"report only ({_GLOW_REPORT_ONLY[name]}): {detail}"
        if bad:
            run.add(name, WARN if name in _GLOW_REPORT_ONLY else FAIL, sum(n for _, n, _ in bad),
                    "none (report only)" if name in _GLOW_REPORT_ONLY else threshold, detail, src)
        elif missing or errors or not rows:
            run.add(name, NOT_MEASURED, None, threshold, detail, src)
        else:
            run.add(name, PASS, 0, threshold, detail, src)


def glow_dimmed_stop(remix_spot: str, orig_spot: str, on: str, orig_still: str, changed: str,
                     again: str) -> tuple[int, str]:
    """Run glow_blobs.py's dimmed-lamp count (worker B, 32a905d) on one change: (exit code, output).
    0 PASS, 1 FAIL, 2 NOT MEASURED (a pose differs from the on step)."""
    try:
        p = subprocess.run([sys.executable, "-I", GLOW_BLOBS, "--remix-spot", remix_spot, "--orig-spot", orig_spot,
                            "--still", on, "--orig-still", orig_still, "--dimmed", changed, "--again", again],
                           capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, f"glow_blobs.py did not run: {e}"
    return p.returncode, (p.stdout + p.stderr).strip()


_DIMMED_RE = re.compile(r"glow_blobs: dimmed (\d+) of (\d+) lamps the original shows")
# (on, on_again) window names of a must-stay triplet: the gate's switch steps (142638) and worker B's see-through
# probe (variants_con_bright_hall_seethrough.json: on / off / on_again). Every other non-goggles switch window in the
# spot is a change.
_DIMMED_TRIPLETS = (("switch_base", "switch_base_again"), ("switch_on", "switch_on_again"))
_glow_blobs = None


def glow_blobs_module():
    """Worker B's tests\\analysis\\glow_blobs.py, imported once (its pose_ok is the pose rule; not copied)."""
    global _glow_blobs
    if _glow_blobs is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("glow_blobs", GLOW_BLOBS)
        _glow_blobs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_glow_blobs)
    return _glow_blobs


def window_poses(spot: dict) -> dict[str, np.ndarray]:
    """name -> camera (x, y, z, yaw, pitch) of each still or switch window with frames, as glow_blobs.spot_still
    computes it (median render camera, publish slot 0, over the window's proxy frames in proxy_frames.csv), read in
    one pass over the file instead of one per window."""
    wins = [(w["name"], *w["proxy"]) for w in spot["windows"]
            if w["kind"] in ("still", "switch") and w.get("frames") and w.get("proxy")]
    cams: dict[str, list] = {n: [] for n, _, _ in wins}
    path = os.path.join(spot["_dir"], "proxy_frames.csv")
    if not wins or not os.path.exists(path):
        return {}
    with open(path, errors="replace") as f:
        for line in f:
            c = line.rstrip("\n").split(",")
            if len(c) < 7 or not c[0].isdigit() or not c[6]:
                continue
            fr = int(c[0])
            hit = [n for n, lo, hi in wins if lo <= fr <= hi]
            if not hit:
                continue
            for pub in c[6].split("|"):
                p = pub.split(":")
                if len(p) >= 9 and p[2] == "0":
                    for n in hit:
                        cams[n].append([float(v) for v in p[3:9]])
                    break
    out = {}
    for n, rows in cams.items():
        if rows:
            m = np.median(np.array(rows), axis=0)
            out[n] = np.array([*m[:3], math.degrees(math.atan2(m[4], m[3])),
                               math.degrees(math.asin(float(np.clip(m[5], -1, 1))))])
    return out


# Reference position rule (coordinator, Nov 16 evening). Worker E2 found that reference 175705's con_boiler y0_p0 passed
# the angle checks at 0.00 deg but was taken from another place: Sam 7.9 units off and the camera 65 units forward of
# launch 085935. A reference stop now counts only when Sam's location (the stop's <window>_raster.json "location") and
# the camera position (median render camera in proxy_frames.csv) both match the Remix stop, as well as the angles
# (glow_blobs.pose_ok's yaw and pitch limits). Limits measured over every Nov 15-16 gate run: between Remix launches
# that agree, Sam differs by at most 1.9 units at one stop (the next value, 4.0 to 5.4, is con_custodial's second
# landing spot: 063147/123628/165842 against 085935/101111/130630), and where Sam agrees the camera differs by at
# most 3.7 units (con_custodial y270_p0; 2.3 at every other stop).
REF_SAM_UNITS, REF_CAM_UNITS = 2.0, 4.0


def stop_positions(spot: dict) -> dict[str, tuple[list | None, np.ndarray | None]]:
    """name -> (Sam's location, camera (x, y, z, yaw, pitch)) for each still or switch window with frames. Sam's
    location is the window's own raster json "location"; a window without one takes its stretch's location when every
    location in the stretch agrees within REF_SAM_UNITS (a stretch ends at each teleport window, whose landed point
    counts in the next stretch); otherwise None. Cached in spot["_positions"]."""
    if "_positions" in spot:
        return spot["_positions"]
    stretches: list[list] = [[]]
    for w in spot["windows"]:
        if w["kind"] == "teleport":
            landed = (w.get("teleport") or {}).get("landed")
            stretches.append([(None, landed)] if landed else [])
        elif w["kind"] in ("still", "switch") and w.get("frames"):
            rj = os.path.join(spot["_dir"], "gate", f"{w['name']}_raster.json")
            loc = (load_json(rj) or {}).get("location") if os.path.exists(rj) else None
            stretches[-1].append((w["name"], loc))
    sam: dict[str, list | None] = {}
    for st in stretches:
        locs = [np.array(l, float) for _, l in st if l]
        same = bool(locs) and max(float(np.linalg.norm(a - locs[0])) for a in locs) <= REF_SAM_UNITS
        for n, l in st:
            if n:
                sam[n] = l or ([float(v) for v in np.median(locs, axis=0)] if same else None)
    cams = window_poses(spot)
    spot["_positions"] = {n: (loc, cams.get(n)) for n, loc in sam.items()}
    return spot["_positions"]


def reference_position(remix: dict, stop: str, ref: dict, ref_stop: str) -> tuple[bool | None, str]:
    """Whether the reference stop was taken where the Remix stop was: (True, distances), (False, distances) when Sam,
    the camera or the angles are apart, (None, why) when either side has no camera in the trace. A side with no
    recorded Sam location (no raster json) is placed by the camera alone, and the line says "Sam location not
    recorded". Only True may be compared; False and None both make the stop NOT MEASURED, never FAIL."""
    gb = glow_blobs_module()
    a, b = stop_positions(remix).get(stop, (None, None)), stop_positions(ref).get(ref_stop, (None, None))
    lab = f"{stop}" + (f" vs {ref_stop}" if ref_stop != stop else "")
    # Coordinator, Nov 16: the camera fixes the view and Sam is the second guard, so a side with no raster json
    # (runs before it existed, such as 140515 and 142638) is placed by the camera alone, and the line says so.
    unplaced = []
    for side, (loc, cam), name in (("Remix", a, stop), ("reference", b, ref_stop)):
        if cam is None:
            return None, f"{lab}: no camera in the {side} trace for {name}"
        if loc is None:
            unplaced.append(side)
    ds = None if unplaced else float(np.linalg.norm(np.subtract(a[0], b[0])))
    dc = float(np.linalg.norm(a[1][:3] - b[1][:3]))
    dyaw, dpitch = abs(wrap(float(a[1][3] - b[1][3]))), abs(float(a[1][4] - b[1][4]))
    ok = ((ds is None or ds <= REF_SAM_UNITS) and dc <= REF_CAM_UNITS and dyaw <= gb.POSE_DEG
          and dpitch <= gb.POSE_PITCH_DEG)
    sam = (f"Sam location not recorded ({' and '.join(unplaced)}, no raster json)" if unplaced
           else f"Sam {ds:.1f} apart")
    return ok, (f"{lab}: {sam}, camera {dc:.1f} apart, yaw {dyaw:.2f}, pitch {dpitch:.2f} deg"
                + ("" if ok else f" (limits {REF_SAM_UNITS:g} and {REF_CAM_UNITS:g} units, {gb.POSE_DEG:g} and "
                                 f"{gb.POSE_PITCH_DEG:g} deg)"))


def reference_position_check(run: Run, good: list[dict], reference: dict[str, dict] | None) -> None:
    """reference_position: every Remix still and switch stop against the same spot's reference stop (switch steps
    against y0_p0, as glow_checks pairs them). PASS when all match; NOT MEASURED listing each stop that does not, with
    its distances. It is a guard for the checks that compare with the reference (scene_brightness, lights_*), which
    leave those stops out themselves, so its NOT MEASURED is in SOFT_NM."""
    name, src = "reference_position", "<window>_raster.json location + proxy_frames.csv camera, both runs"
    thr = f"Sam <= {REF_SAM_UNITS:g}, camera <= {REF_CAM_UNITS:g} units"
    if reference is None:
        run.add(name, NOT_MEASURED, None, thr, "no reference run given (--reference)", src)
        return
    ok_n, bad, missing = 0, [], []
    for s in good:
        ref = reference.get(s["name"])
        if ref is None:
            missing.append(f"{s['name']}: no reference spot")
            continue
        ref_stills = {w["name"] for w in ref["windows"] if w.get("frames")}
        for w in s["windows"]:
            if not w.get("frames") or w["kind"] not in ("still", "switch"):
                continue
            orig = w["name"] if w["name"] in ref_stills else ("y0_p0" if w["kind"] == "switch" else None)
            if orig not in ref_stills:
                continue
            ok, why = reference_position(s, w["name"], ref, orig)
            if ok:
                ok_n += 1
            else:
                bad.append(f"{s['name']} {why}")
    detail = (f"{ok_n} of {ok_n + len(bad)} stops match" + (f"; not comparable: {'; '.join(bad)}" if bad else "")
              + (f"; {'; '.join(missing)}" if missing else ""))
    run.add(name, PASS if ok_n and not bad and not missing else NOT_MEASURED, len(bad), thr, detail, src)


def _reference_stop_ok(good: list[dict], reference: dict[str, dict] | None):
    """pfm_brightness's stop_ok hook: a raster taken from the reference run counts only when reference_position
    accepts that stop; spots it cannot find are refused with the reason."""
    by_dir = {os.path.normcase(os.path.abspath(s["_dir"])): s for s in good}
    refs = {os.path.normcase(os.path.abspath(r["_dir"])): r for r in (reference or {}).values()}

    def ok(spot_dir: str, ref_spot_dir: str, tag: str) -> tuple[bool, str]:
        s = by_dir.get(os.path.normcase(os.path.abspath(spot_dir)))
        r = refs.get(os.path.normcase(os.path.abspath(ref_spot_dir)))
        if s is None or r is None:
            return False, f"{tag}: spot not loaded for the reference position rule"
        placed, why = reference_position(s, tag, r, tag)
        return bool(placed), why
    return ok


def nearest_reference_still(on_pose, ref: dict, remix: dict | None = None,
                            on: str | None = None) -> tuple[str | None, str]:
    """(name, note): the reference still nearest the on step's camera among those glow_blobs.pose_ok accepts (8 cm,
    1.5 and 1.0 deg since 33dce35) and, given the Remix spot and on step, reference_position accepts; or (None, why)
    when none does (worker B, Nov 16: never fixed to y0_p0)."""
    gb = glow_blobs_module()
    best, closest, misplaced = None, None, []
    for n, p in window_poses(ref).items():
        if not n.startswith("y"):
            continue                       # stills only (y<yaw>_p<pitch>...), not the reference's own switch steps
        ok, dist, dyaw, dpitch = gb.pose_ok((on_pose, p))
        key = (dist, dyaw + dpitch)
        if closest is None or key < closest[0]:
            closest = (key, n, dist, dyaw, dpitch)
        if ok and remix is not None:
            placed, why = reference_position(remix, on, ref, n)
            if not placed:
                misplaced.append(why)      # the camera is near, but Sam or the camera is not where Remix had them
                continue
        if ok and (best is None or key < best[0]):
            best = (key, n)
    if best:
        return best[1], ""
    if misplaced:
        return None, "reference position rule: " + "; ".join(misplaced)
    if closest is None:
        return None, "the reference has no still with a camera"
    _, n, dist, dyaw, dpitch = closest
    return None, (f"no reference still within {gb.POSE_CM:g} cm and {gb.POSE_DEG:g} deg of the on step (nearest {n}: "
                  f"{dist:.0f} cm, yaw {dyaw:.2f}, pitch {dpitch:.2f} deg)")


def lamps_dimmed_check(run: Run, good: list[dict], reference: dict[str, dict] | None, ref_no_remix: bool) -> None:
    """lights_lamps_dimmed (worker B's spec, glow_blobs.py 32a905d): the must-stay check for a change that removes
    something. A spot with an on / on_again pair (_DIMMED_TRIPLETS) has every other non-goggles switch window scored
    as a change: a lamp the original shows is DIMMED when its Remix glow drops by more than max(12, 3 x |on -
    on_again|). The original's still is the reference still nearest the on step's camera that passes
    glow_blobs.pose_ok; with none, that spot is NOT MEASURED. Limit 0 per change. Goggles switches are left out: the
    goggles replace the picture. Only runs with a triplet can be measured, so NOT MEASURED is in SOFT_NM; the detail
    says why (no triplet, no no-Remix reference, no reference still at the pose, poses apart or a script error)."""
    name, src, threshold = "lights_lamps_dimmed", "glow_blobs.py --dimmed: changes against the on and on_again " \
        "switch steps and the --reference (no Remix) still nearest the on pose", "0 per change"
    if reference is None or not ref_no_remix:
        why = "no reference run given (--reference)" if reference is None else "the reference run is not a no-Remix run"
        run.add(name, NOT_MEASURED, None, threshold, why, src)
        return
    jobs, missing = [], []
    for s in good:
        sw = {w["name"]: w for w in s["windows"] if w["kind"] == "switch" and w.get("frames")}
        pair = next(((on, ag) for on, ag in _DIMMED_TRIPLETS if on in sw and ag in sw), None)
        if pair is None:
            continue
        changes = [n for n, w in sw.items() if not any(n in t for t in _DIMMED_TRIPLETS)
                   and not (w.get("switch") or {}).get("goggles")]
        if not changes:
            continue
        ref = reference.get(s["name"])
        if ref is None:
            missing.append(f"{s['name']}: no reference spot")
            continue
        on_pose = window_poses(s).get(pair[0])
        orig, why = (nearest_reference_still(on_pose, ref, s, pair[0]) if on_pose is not None
                     else (None, "no camera for the on step"))
        if orig is None:
            missing.append(f"{s['name']}: {why}")
            continue
        jobs += [(s["name"], s["_dir"], ref["_dir"], ch, pair, orig) for ch in changes]
    if not jobs:
        run.add(name, NOT_MEASURED, None, threshold,
                "no spot with an on step, a change and an on_again step plus a reference still at that pose"
                + (f" ({'; '.join(missing)})" if missing else ""), src)
        return
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        outs = list(pool.map(lambda j: glow_dimmed_stop(j[1], j[2], j[4][0], j[5], j[3], j[4][1]), jobs))
    rows, apart, errors = [], [], []
    for (spot, _, _, ch, _, orig), (code, out) in zip(jobs, outs):
        ch = f"{ch} (vs original {orig})"
        m = _DIMMED_RE.search(out)
        first = (out.splitlines()[0] if out else f"exit {code}").replace("glow_blobs: ", "")
        if code == 2 and "NOT MEASURED" in out:
            apart.append(f"{spot} {ch}: {first}")
        elif code in (0, 1) and m:
            rows.append((f"{spot} {ch}", int(m[1]), int(m[2])))
        else:
            errors.append(f"{spot} {ch}: exit {code}, {first}")
    bad = [r for r in rows if r[1] > 0]
    detail = (f"{len(bad)} of {len(rows)} changes dimmed a lamp the original shows: "
              + "; ".join(f"{lab} {n} of {of}" for lab, n, of in rows)
              + (f"; poses apart, not compared: {'; '.join(apart)}" if apart else "")
              + (f"; glow_blobs.py errors: {'; '.join(errors)}" if errors else "")
              + (f"; not measured: {'; '.join(missing)}" if missing else ""))
    if bad:
        run.add(name, FAIL, sum(r[1] for r in bad), threshold, detail, src)
    elif errors or apart or missing or not rows:
        run.add(name, NOT_MEASURED, None, threshold, detail, src)
    else:
        run.add(name, PASS, 0, threshold, detail, src)


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

    no_remix = bool(run.manifest.get("noRemix"))
    measured_pairs = pick(MEASURED)
    if not measured_pairs:
        run.add("frames_over_50ms", NOT_MEASURED, None, "0", "no windows of this kind were run", tsrc)
    else:
        want_50, recs_50 = coverage(measured_pairs, "telemetry", "telemetry")
        have_50 = sum(1 for r in recs_50 if isinstance(r.get("frameTimeMs"), (int, float)))
        if want_50 == 0 or have_50 < want_50:
            run.add("frames_over_50ms", NOT_MEASURED, None, "0",
                    f"telemetry has {have_50} of {want_50} frames with 'frameTimeMs'", tsrc)
        else:
            over_nonload, excluded_load = 0, 0
            for s, w in measured_pairs:
                tel = s["_data"]["telemetry"]
                for tf in frames_in(w, "telemetry"):
                    if tel[tf]["frameTimeMs"] > 50.0:
                        if is_loading_frame(s, tf, no_remix=no_remix):
                            excluded_load += 1
                        else:
                            over_nonload += 1
            detail = f"{over_nonload} of {want_50} frames over 50 ms" + (
                f" ({excluded_load} loading-screen frames excluded)" if excluded_load else "")
            run.add("frames_over_50ms", PASS if over_nonload <= THRESHOLDS["frames_over_50ms"] else FAIL,
                    over_nonload, "0", detail, tsrc)

    # present_stalls: proxy row index 3 (time inside Present); frame_gap_over_50ms: index 2 (gap since the previous
    # Present entry). Both > 50 ms in measured windows, loading frames excluded.
    proxy_over_50ms(run, "present_stalls", 3, "Present", "present_stalls_max", measured_pairs, no_remix)
    proxy_over_50ms(run, "frame_gap_over_50ms", 2, "a Present-to-Present gap", "frame_gap_max", measured_pairs, no_remix)
    # skinned_min & remix_dropped_character_frames: join telemetry skinnedInstances with proxy_gate skinnedSceneDraws
    scene_pairs_list = pick(SCENE)
    if not scene_pairs_list:
        run.add("skinned_min", NOT_MEASURED, None, ">= 1 every frame", "no windows of this kind were run", tsrc)
        run.add("remix_dropped_character_frames", NOT_MEASURED, None, "0", "no windows of this kind were run", tsrc)
    else:
        want_sk, recs_sk = coverage(scene_pairs_list, "telemetry", "telemetry")
        have_sk = sum(1 for r in recs_sk if isinstance(r.get("skinnedInstances"), (int, float)))
        want_pg, pg_rows = coverage(scene_pairs_list, "proxy", "pgate")
        have_pg = sum(1 for r in pg_rows if len(r) >= 3)
        if want_sk == 0 or have_sk < want_sk or want_pg == 0 or have_pg < want_pg:
            msg = (f"telemetry has {have_sk} of {want_sk} frames with 'skinnedInstances', "
                   f"proxy_gate has {have_pg} of {want_pg}")
            run.add("skinned_min", NOT_MEASURED, None, ">= 1 every frame", msg, tsrc)
            run.add("remix_dropped_character_frames", NOT_MEASURED, None, "0", msg, tsrc)
        else:
            sk_vals, dropped_chars, n_char_frames = [], 0, 0
            for s, w in scene_pairs_list:
                tel, pg = s["_data"]["telemetry"], s["_data"]["pgate"]
                for tf in frames_in(w, "telemetry"):
                    if tf in pg and len(pg[tf]) >= 5 and int(pg[tf][2]) > 0:
                        n_char_frames += 1
                        sk_inst = int(tel[tf]["skinnedInstances"])
                        sk_vals.append(sk_inst)
                        in_goggles = (tel[tf].get("rayTraced") is False) or any(
                            f in pg and len(pg[f]) >= 5 and int(pg[f][4]) == 1 for f in range(tf - 3, tf + 4)
                        )
                        if not in_goggles and sk_inst == 0 and all(
                            f in pg and len(pg[f]) >= 5 and int(pg[f][2]) > 0 for f in range(tf - 3, tf + 4)
                        ):
                            dropped_chars += 1
            if not sk_vals:
                run.add("skinned_min", NOT_MEASURED, None, ">= 1 every frame",
                        f"0 of {want_sk} scene frames had proxy skinnedSceneDraws > 0", tsrc)
            else:
                min_sk = min(sk_vals)
                run.add("skinned_min", PASS if min_sk >= THRESHOLDS["skinned_min"] else FAIL,
                        min_sk, ">= 1 every frame",
                        f"fewest character meshes in {n_char_frames} proxy-drawn frames: {min_sk}", tsrc)
            run.add("remix_dropped_character_frames",
                    PASS if dropped_chars <= THRESHOLDS["remix_dropped_character_frames"] else FAIL,
                    dropped_chars, "0",
                    f"{dropped_chars} of {want_sk} scene frames had 0 Remix skinnedInstances while proxy drew +/-3",
                    f"{tsrc} + d3d9_proxy_gate.csv")
    telemetry_check("static_blas_rebuilds", pick(ROTATING), "staticBlasRebuilds",
                    lambda v: (sum(v), sum(v) <= THRESHOLDS["static_blas_rebuilds"]),
                    "0 while rotating", lambda v, n: f"{v} rebuilds in {n} rotating frames")
    # Counted on Remix's 128x72 thumbnail, so this sees a patch of bad pixels on any frame, not one lone pixel.
    telemetry_check("nonfinite_thumb_pixels", pick(SCENE), "thumbNonFinite",
                    lambda v: (sum(v), sum(v) == 0),
                    "0", lambda v, n: f"{v} NaN or infinite thumbnail pixels in {n} scene frames")
    def fmt_fps(raw: float, target: float) -> str:
        if abs(raw - target) >= 0.1:
            return f"{raw:.1f}"
        for d in (2, 3, 4):
            s = f"{raw:.{d}f}"
            if (raw < target and float(s) < target) or raw >= target:
                return s
        return f"{raw:.4f}"

    fps_pairs = pick(SCENE)
    fps_target = THRESHOLDS["min_median_fps"]
    fps_thresh = f">= {fps_target:g} FPS (goal, WARN below)"
    if not fps_pairs:
        run.add("min_median_fps", NOT_MEASURED, None, fps_thresh, "no windows of this kind were run", tsrc)
    else:
        want_fps, recs_fps = coverage(fps_pairs, "telemetry", "telemetry")
        vals_fps = [r["frameTimeMs"] for r in recs_fps if isinstance(r.get("frameTimeMs"), (int, float))]
        if want_fps == 0 or len(vals_fps) < want_fps:
            run.add("min_median_fps", NOT_MEASURED, None, fps_thresh,
                    f"telemetry has {len(vals_fps)} of {want_fps} frames with 'frameTimeMs'", tsrc)
        else:
            raw_fps = 1000.0 / max(float(np.median(vals_fps)), 1e-3)
            ok_fps = raw_fps >= fps_target
            val_fps = float(fmt_fps(raw_fps, fps_target))
            run.add("min_median_fps", PASS if ok_fps else WARN, val_fps, fps_thresh,
                    f"median {fmt_fps(raw_fps, fps_target)} FPS, target is at or above {fps_target:g} FPS "
                    f"({want_fps} scene frames)", tsrc)
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
    frame_interval_check(run, good, ref_spots, no_remix)
    ref_manifest = os.path.join(reference, "manifest.json") if reference else None
    ref_no_remix = bool(ref_manifest and os.path.exists(ref_manifest) and load_json(ref_manifest).get("noRemix"))
    reference_position_check(run, good, ref_spots)
    glow_checks(run, good, ref_spots, ref_no_remix)
    lamps_dimmed_check(run, good, ref_spots, ref_no_remix)
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

    # frame_join_clock: qpcMs cross-check against proxy_frames.csv (entry + presentMs of k-1, k]
    if not measured_pairs:
        run.add("frame_join_clock", NOT_MEASURED, None, "0 frames outside proxy window",
                "no windows of this kind were run",
                "rtx_telemetry.jsonl qpcMs + d3d9_proxy_frames.csv")
    else:
        qpc_checked, qpc_bad, first_bad = 0, 0, None
        for s, w in measured_pairs:
            tel, prx = s["_data"]["telemetry"], s["_data"]["proxy"]
            for tf in frames_in(w, "telemetry"):
                qpc = tel.get(tf, {}).get("qpcMs")
                if not isinstance(qpc, (int, float)) or qpc <= 0:
                    continue
                if tf not in prx or (tf - 1) not in prx or len(prx[tf]) < 4 or len(prx[tf - 1]) < 4:
                    continue
                try:
                    lo = float(prx[tf - 1][1]) + float(prx[tf - 1][3])
                    hi = float(prx[tf][1]) + float(prx[tf][3])
                except ValueError:
                    continue
                qpc_checked += 1
                if not (lo < float(qpc) <= hi):
                    qpc_bad += 1
                    if first_bad is None:
                        first_bad = (s["name"], tf, float(qpc), lo, hi)
        if qpc_checked == 0:
            run.add("frame_join_clock", NOT_MEASURED, None, "0 frames outside proxy window",
                    "qpcMs absent or 0 on every frame (a build before dxvk-remix 7f8c727)",
                    "rtx_telemetry.jsonl qpcMs + d3d9_proxy_frames.csv")
        else:
            bdetail = f"{qpc_bad} of {qpc_checked} qpcMs frames outside (prev_end, cur_end]" + (
                f" (first: {first_bad[0]} f{first_bad[1]} qpc={first_bad[2]:.3f} not in ({first_bad[3]:.3f}, {first_bad[4]:.3f}])"
                if first_bad else ""
            )
            run.add("frame_join_clock", PASS if qpc_bad == 0 else FAIL, qpc_bad, "0 frames outside proxy window",
                    bdetail, "rtx_telemetry.jsonl qpcMs + d3d9_proxy_frames.csv")

    # Zero-normal counts per spot (median and max; limit 0 measured in every Nov 16 run)
    for cname, field, lim_key in (
        ("zero_normal_skinned_triangles", "zeroNormalSkinnedTriangles", "zero_normal_skinned_tris_max"),
        ("zero_normal_instances", "zeroNormalInstances", "zero_normal_instances_max"),
        ("zero_normal_static_triangles", "zeroNormalStaticTriangles", "zero_normal_static_tris_max"),
        ("zero_normal_static_instances", "zeroNormalStaticInstances", "zero_normal_static_instances_max"),
    ):
        lim = THRESHOLDS[lim_key]
        thresh_zn = f"<= {lim} max per spot"
        if not good:
            run.add(cname, NOT_MEASURED, None, thresh_zn, "no spot ran far enough to judge", tsrc)
            continue
        spot_stats, missing_zn = [], []
        for s in good:
            want_z, recs_z = coverage(scene_pairs(s), "telemetry", "telemetry")
            vals_z = [int(r[field]) for r in recs_z if isinstance(r.get(field), (int, float))]
            if want_z == 0 or len(vals_z) < want_z:
                missing_zn.append(s["name"])
            else:
                spot_stats.append((s["name"], float(np.median(vals_z)), max(vals_z)))
        if missing_zn or not spot_stats:
            run.add(cname, NOT_MEASURED, None, thresh_zn,
                    f"missing '{field}' in scene telemetry for: " + (", ".join(missing_zn) or "no spot"), tsrc)
        else:
            worst_max = max(x[2] for x in spot_stats)
            detail_zn = "; ".join(f"{n} med {med:.1f} max {mx}" for n, med, mx in spot_stats)
            run.add(cname, PASS if worst_max <= lim else FAIL, worst_max, thresh_zn, detail_zn, tsrc)

    recorder_checks(run, pick, good, tsrc)
    ray_tracing_checks(run, good, tsrc)
    lightpub_checks(run, good, ref_spots)

    # goggles_toggle_rebuilds & goggles_dark_frame: first rayTraced=true frame after rayTraced=false in goggles windows.
    goggles_pairs = pick(("goggles",))
    if not goggles_pairs:
        run.add("goggles_toggle_rebuilds", NOT_MEASURED, None,
                f"<= {THRESHOLDS['goggles_toggle_max_builds']} (GUESS)", "no goggles windows were run", tsrc)
        run.add("goggles_dark_frame", NOT_MEASURED, None,
                f">= {THRESHOLDS['goggles_dark_min_ratio']:g} (GUESS)", "no goggles windows were run", tsrc)
    else:
        want_g, recs_g = coverage(goggles_pairs, "telemetry", "telemetry")
        have_g = sum(1 for r in recs_g if isinstance(r.get("rayTraced"), bool)
                     and isinstance(r.get("staticBlasBuilds"), (int, float)))
        if want_g == 0 or have_g < want_g:
            msg = f"telemetry has {have_g} of {want_g} goggles frames with 'rayTraced' and 'staticBlasBuilds'"
            run.add("goggles_toggle_rebuilds", NOT_MEASURED, None,
                    f"<= {THRESHOLDS['goggles_toggle_max_builds']} (GUESS)", msg, tsrc)
            run.add("goggles_dark_frame", NOT_MEASURED, None,
                    f">= {THRESHOLDS['goggles_dark_min_ratio']:g} (GUESS)", msg, tsrc)
        else:
            builds_list, ratios_list = [], []
            for s, w in goggles_pairs:
                tel = s["_data"]["telemetry"]
                tids = frames_in(w, "telemetry")
                cap_frames = None
                seen_off = False
                for idx, tf in enumerate(tids):
                    rt = tel[tf]["rayTraced"]
                    if not rt:
                        seen_off = True
                        continue
                    if seen_off:
                        seen_off = False
                        builds_list.append((int(tel[tf]["staticBlasBuilds"]), s["name"], w["name"], tf))
                        # Find the frame 10 ray-traced frames later (within window or continuing in telemetry).
                        rt_after = [tids[j] for j in range(idx, len(tids)) if tel[tids[j]].get("rayTraced")]
                        tf10 = rt_after[10] if len(rt_after) > 10 else None
                        l0 = l10 = None
                        th0 = tel[tf].get("lumaThumb")
                        th10 = tel.get(tf10, {}).get("lumaThumb") if tf10 is not None else None
                        if th0 and th10:
                            l0 = float(np.frombuffer(base64.b64decode(th0), np.uint8).mean())
                            l10 = float(np.frombuffer(base64.b64decode(th10), np.uint8).mean())
                        else:
                            if cap_frames is None:
                                p = spot_path(s, w.get("frames") or w.get("goggles", {}).get("shot"))
                                if p and os.path.exists(p):
                                    cw, ch = w.get("frameSize") or w.get("goggles", {}).get("frameSize", (0, 0))
                                    if cw and ch:
                                        cap_frames = read_gray(p, cw, ch)
                            if cap_frames is not None and idx + 10 < len(cap_frames):
                                l0 = float(cap_frames[idx].mean())
                                l10 = float(cap_frames[idx + 10].mean())
                        if l0 is not None and l10 is not None and l10 > 1e-6:
                            ratios_list.append((l0 / l10, s["name"], w["name"], tf))
            if not builds_list:
                run.add("goggles_toggle_rebuilds", NOT_MEASURED, None,
                        f"<= {THRESHOLDS['goggles_toggle_max_builds']} (GUESS)",
                        "no rayTraced=false -> rayTraced=true transition in goggles windows", tsrc)
            else:
                worst_b = max(builds_list, key=lambda x: x[0])
                run.add("goggles_toggle_rebuilds",
                        PASS if worst_b[0] <= THRESHOLDS["goggles_toggle_max_builds"] else FAIL,
                        worst_b[0], f"<= {THRESHOLDS['goggles_toggle_max_builds']} (GUESS)",
                        f"worst {worst_b[0]} staticBlasBuilds at {worst_b[1]}/{worst_b[2]} frame {worst_b[3]} "
                        f"({len(builds_list)} toggle(s))", tsrc)
            if not ratios_list:
                run.add("goggles_dark_frame", NOT_MEASURED, None,
                        f">= {THRESHOLDS['goggles_dark_min_ratio']:g} (GUESS)",
                        "no toggle with 10 subsequent ray-traced frames and luma data", tsrc)
            else:
                worst_r = min(ratios_list, key=lambda x: x[0])
                run.add("goggles_dark_frame",
                        PASS if worst_r[0] >= THRESHOLDS["goggles_dark_min_ratio"] else FAIL,
                        round(worst_r[0], 3), f">= {THRESHOLDS['goggles_dark_min_ratio']:g} (GUESS)",
                        f"worst luma ratio {worst_r[0]:.3f} at {worst_r[1]}/{worst_r[2]} frame {worst_r[3]} "
                        f"({len(ratios_list)} toggle(s))", tsrc)

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
        if w["kind"] == "still":
            ok_rt, rt_why = stop_all_ray_traced(s, w)
            if not ok_rt:
                flash_missing.append(f"{s['name']}/{w['name']}: {rt_why}"); continue
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

    # sustained flicker, slow pulsing, >=4-frame local pulses, or sparse local strobes across still camera stops
    worst_jitter, worst_drift, worst_pulse, worst_step4 = 0.0, 0.0, 0.0, 0.0
    flicker_bad, flicker_missing, flicker_sources = [], [], set()
    j_limit, d_limit = THRESHOLDS["still_block_jitter"], THRESHOLDS["still_luma_drift"]
    p_limit, s4_limit = THRESHOLDS["still_block_pulse"], THRESHOLDS["still_block_step4"]
    flicker_thresh = (f"block jitter <= {j_limit:g}, drift <= {d_limit:g}, "
                      f"pulse <= {p_limit:g}, step4 <= {s4_limit:g} (/255)")
    for s, w in pick(("still",)):
        ok_rt, rt_why = stop_all_ray_traced(s, w)
        if not ok_rt:
            flicker_missing.append(f"{s['name']}/{w['name']}: {rt_why}"); continue
        frames, src = window_frames(s, w)
        flicker_sources.add(src)
        n = 0 if frames is None else len(dedupe(frames))
        if n < THRESHOLDS["still_frames"]:
            flicker_missing.append(f"{s['name']}/{w['name']}: {n} frames"); continue
        bj, ld, bp, bs4 = still_flicker(frames)
        worst_jitter, worst_drift = max(worst_jitter, bj), max(worst_drift, ld)
        worst_pulse, worst_step4 = max(worst_pulse, bp), max(worst_step4, bs4)
        if bj > j_limit or ld > d_limit or bp > p_limit or bs4 > s4_limit:
            flicker_bad.append(f"{s['name']}/{w['name']} jitter {bj:.2f} drift {ld:.2f} pulse {bp:.2f} step4 {bs4:.2f}")
    if flicker_missing or not pick(("still",)):
        run.add("still_flicker", NOT_MEASURED, None, flicker_thresh,
                "incomplete frames: " + "; ".join(flicker_missing[:6] or ["no still windows"]),
                ", ".join(sorted(flicker_sources)) or "none")
    else:
        run.add("still_flicker", FAIL if flicker_bad else PASS,
                round(max(worst_jitter, worst_drift, worst_pulse, worst_step4), 2), flicker_thresh,
                f"worst block jitter {worst_jitter:.2f}, luma drift {worst_drift:.2f}, "
                f"pulse {worst_pulse:.2f}, step4 {worst_step4:.2f}"
                + ("; " + "; ".join(flicker_bad[:5]) if flicker_bad else ""),
                ", ".join(sorted(flicker_sources)))

    # image dumps at the stops: NaN, neon, two-sided scene_brightness vs raster, angled sharpness, outline
    nan_total, worst_neon, n_final, missing_final, not_rt_stops, overlaps, outline_missing = 0, 0.0, 0, [], [], [], []
    worst_angled_sharp = float("inf")
    n_angled = 0
    for s, w in pick(("still",)):
        tag = w.get("dump")
        if not tag:
            continue
        ok_rt, rt_why = stop_all_ray_traced(s, w)
        if not ok_rt:
            not_rt_stops.append(f"{s['name']}/{tag}: {rt_why}")
            if w.get("outline"):
                outline_missing.append(f"{s['name']}/{tag}: {rt_why}")
            continue
        final = spot_path(s, os.path.join("gate", f"{tag}_final.pfm"))
        if os.path.exists(final):
            img = read_pfm(final)
            nan, neon = neon_stats(img)
            _, _, sharp = luma_and_sharpness(img)
            nan_total += nan; worst_neon = max(worst_neon, neon); n_final += 1
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
    b_thresh = (f"luma >= {THRESHOLDS['min_scene_luma']:g}, stop [{THRESHOLDS['min_raster_luma_ratio']:.2f}x, "
                f"{THRESHOLDS['max_raster_luma_ratio']:.2f}x], median [{THRESHOLDS['min_spot_median_ratio']:.2f}x, "
                f"{THRESHOLDS['max_spot_median_ratio']:.2f}x], near-white <= +{THRESHOLDS['max_near_white_delta']:.2f}")
    b_src = "Remix <tag>_final.pfm + --reference (no Remix) <tag>_raster.pfm at stops reference_position accepts"
    if missing_final or not_rt_stops or n_final == 0 or not good:
        if not_rt_stops:
            detail = f"{len(not_rt_stops)} stop(s) unverified for RT ({'; '.join(not_rt_stops[:4])})"
        elif missing_final or n_final == 0:
            detail = f"{len(missing_final)} of {len(missing_final) + n_final} stops have no final image dump"
        else:
            detail = "no spot ran far enough to judge"
        run.add("nan_pixels", NOT_MEASURED, nan_total, "0", detail, "Remix <tag>_final.pfm")
        run.add("neon_fraction", NOT_MEASURED, round(worst_neon, 6), "< 0.1%", detail, "Remix <tag>_final.pfm")
        run.add("scene_brightness", NOT_MEASURED, None, b_thresh, detail, b_src)
        run.add("angled_sharpness", NOT_MEASURED, None, f">= {THRESHOLDS['min_angled_sharpness']:g}", detail, "Remix <tag>_final.pfm")
    else:
        run.add("nan_pixels", PASS if nan_total <= THRESHOLDS["nan_pixels"] else FAIL, nan_total, "0",
                f"{nan_total} non-finite pixels in {n_final} dumps", "Remix <tag>_final.pfm")
        run.add("neon_fraction", PASS if worst_neon < THRESHOLDS["neon_fraction"] else FAIL, round(worst_neon, 6), "< 0.1%",
                f"worst dump {worst_neon:.4%} clipped neon pixels", "Remix <tag>_final.pfm")

        sys.modules.setdefault("gate", sys.modules[__name__])
        b_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "analysis", "brightness"))
        if b_dir not in sys.path:
            sys.path.insert(0, b_dir)
        import pfm_brightness  # noqa: E402

        b_th = {k: THRESHOLDS[k] for k in (
            "min_scene_luma", "min_raster_luma_ratio", "max_raster_luma_ratio",
            "min_spot_median_ratio", "max_spot_median_ratio", "max_near_white_delta",
        )}
        b_sum = pfm_brightness.summarize_gate_run(
            folder, reference_dirs=[reference] if reference else None, max_pose_error_deg=1.0, thresholds=b_th,
            stop_ok=_reference_stop_ok(good, ref_spots)
        )
        b_spots = [b_sum["spots"][s["name"]] for s in good if s["name"] in b_sum.get("spots", {})]
        inl = [f"{s['name']} {b_sum['spots'][s['name']]['inlaunch_gamma_ratio_median']:.2f}x" for s in good
               if b_sum.get("spots", {}).get(s["name"], {}).get("inlaunch_gamma_ratio_median") is not None]
        inl_note = (f"; report only, against the in-launch picture, not the original game: median {', '.join(inl)}"
                    if inl else "")
        if len(b_spots) < len(good) or any(sp["status"] == pfm_brightness.NOT_MEASURED for sp in b_spots):
            reasons = [f"{s['name']}: {', '.join(b_sum['spots'][s['name']]['reasons'] or ['not measured'])}"
                       if s["name"] in b_sum.get("spots", {}) else f"{s['name']}: no brightness data"
                       for s in good if s["name"] not in b_sum.get("spots", {})
                       or b_sum["spots"][s["name"]]["status"] == pfm_brightness.NOT_MEASURED]
            run.add("scene_brightness", NOT_MEASURED, None, b_thresh, "; ".join(reasons[:6]) + inl_note, b_src)
        else:
            meds = [sp["raster_gamma_ratio_median"] for sp in b_spots if sp["raster_gamma_ratio_median"] is not None]
            nw_max = max((sp["near_white_delta_max"] for sp in b_spots if sp["near_white_delta_max"] is not None),
                         default=0.0)
            failed_sp = [f"{s['name']} ({'; '.join(b_sum['spots'][s['name']]['reasons'])})"
                         for s in good if b_sum["spots"][s["name"]]["status"] == pfm_brightness.FAIL]
            val = round(float(meds[0]) if len(meds) == 1 else max(meds), 3) if meds else None
            b_detail = ("; ".join(failed_sp[:4]) if failed_sp
                        else f"median {min(meds):.2f}x..{max(meds):.2f}x the original game's raster, "
                             f"max near-white delta {nw_max:+.3f}")
            run.add("scene_brightness", FAIL if failed_sp else PASS, val, b_thresh, b_detail + inl_note, b_src)

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
    status = PASS if run.metrics and all(
        x["status"] in (PASS, WARN) or (x["status"] == NOT_MEASURED and x["check"] in SOFT_NM)
        for x in run.metrics
    ) else FAIL
    result = {"status": status, "run": os.path.abspath(folder), "thresholds": THRESHOLDS, "checks": run.metrics,
              "latencyContributors": latency_contributors}
    save_json(os.path.join(folder, "result.json"), result)
    save_text(os.path.join(folder, "result.md"), markdown(result))
    return result


def _first_file(folder: str, names: tuple[str, ...]) -> str | None:
    return next((os.path.join(folder, n) for n in names if os.path.exists(os.path.join(folder, n))), None)


FREEZE_SCAN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "analysis", "freeze_scan.py")
_freeze_scan = None


def freeze_scan_module():
    """Worker C's tests\\analysis\\freeze_scan.py, imported once (its classify() is the freeze rule; not copied)."""
    global _freeze_scan
    if _freeze_scan is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("freeze_scan", FREEZE_SCAN)
        _freeze_scan = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_freeze_scan)
    return _freeze_scan


def freeze_checks(run: Run, path: str | None) -> None:
    """Session freezes from d3d9_proxy_frames.csv (worker C's spec, Nov 16). freezes_in_play_over_1s: gaps over
    1000 ms between Present entries where the frame and the one before both carry a gameplay-camera publish ('G:')
    and draws > 0, not in a launch's first 10 frames and not on its last frame (freeze_scan.classify). Limit 0.
    Report only, never FAIL: freeze_gaps_excluded (startup, menu and end gaps with sizes) and quit_gap_ms (each
    launch's last gapMs; no reference number agreed: the original game's launches ended on 4-53 ms, once 172 ms)."""
    src = "d3d9_proxy_frames.csv (freeze_scan.classify)"
    thr = f"0 gaps over {THRESHOLDS['freeze_gap_ms']:g} ms in play"
    if not path:
        for name, t in (("freezes_in_play_over_1s", thr), ("freeze_gaps_excluded", "none (report only)"),
                        ("quit_gap_ms", "none (report only)")):
            run.add(name, NOT_MEASURED, None, t, "session has no d3d9_proxy_frames.csv", src)
        return
    fs = freeze_scan_module()
    launches = [fr for fr in fs.read_launches(path) if fr]
    if not launches:
        for name, t in (("freezes_in_play_over_1s", thr), ("freeze_gaps_excluded", "none (report only)"),
                        ("quit_gap_ms", "none (report only)")):
            run.add(name, NOT_MEASURED, None, t, "d3d9_proxy_frames.csv has no readable frame rows", src)
        return
    found = [(n, x) for n, frames in enumerate(launches) for x in fs.classify(frames, THRESHOLDS["freeze_gap_ms"])]
    play = [(n, x) for n, x in found if x[3] == "play"]
    tag = lambda n: f"launch {n} " if len(launches) > 1 else ""     # noqa: E731
    run.add("freezes_in_play_over_1s", FAIL if len(play) > THRESHOLDS["freezes_in_play_max"] else PASS, len(play), thr,
            f"{len(play)} gap(s) over {THRESHOLDS['freeze_gap_ms']:g} ms in play over "
            f"{sum(len(f) for f in launches)} frames"
            + "".join(f"; {tag(n)}frame {fr}: gap {gap:.0f} ms, inside Present {pr:.0f} ms"
                      for n, (fr, gap, pr, _) in play), src)
    excl = [(n, x) for n, x in found if x[3] != "play"]
    run.add("freeze_gaps_excluded", PASS, len(excl), "none (report only)",
            "; ".join(f"{tag(n)}{k} frame {fr}: {gap:.0f} ms" for n, (fr, gap, _, k) in excl) or "none", src)
    quits = [(n, frames[-1][0], frames[-1][1]) for n, frames in enumerate(launches)]
    run.add("quit_gap_ms", PASS, round(max(q[2] for q in quits)), "none (report only; no reference agreed)",
            "; ".join(f"{tag(n)}{gap:.0f} ms on last frame {fr}" for n, fr, gap in quits), src)


def evaluate_session(folder: str) -> dict:
    """Score the logs of a free-play session (a play-setup logs folder: rtx_telemetry.jsonl, d3d9_proxy_gate.csv and
    rtx-remix_logs_remix-dxvk.log) with the gate rules that need no scripted camera: goggles_toggle_rebuilds and
    goggles_dark_frame on every goggles-off (proxy goggles column 1 -> 0), remix_dropped_character_frames on every
    frame, frames_over_50ms (only loading screens excused; result["slowFrames"] lists every frame over 50 ms),
    remix_ray_tracing (scene share outside loading and goggles, time to first ray-traced frame from launch start),
    compile_wait_stuck and never_ray_traced on the whole launch,
    freezes_in_play_over_1s from d3d9_proxy_frames.csv (freeze_checks), clean_quit on device loss. Telemetry is
    streamed (sessions reach 1.6 GB). Writes nothing to the folder."""
    run = Run.__new__(Run)
    run.dir, run.manifest, run.metrics = folder, {}, []
    tel_path = _first_file(folder, ("rtx_telemetry.jsonl", "telemetry.jsonl"))
    pg_path = _first_file(folder, ("d3d9_proxy_gate.csv", "proxy_gate.csv"))
    tsrc = f"{os.path.basename(tel_path or 'rtx_telemetry.jsonl')} + {os.path.basename(pg_path or 'd3d9_proxy_gate.csv')}"
    builds_lim, dark_lim = THRESHOLDS["goggles_toggle_max_builds"], THRESHOLDS["goggles_dark_min_ratio"]
    goggle_checks = (("goggles_toggle_rebuilds", f"<= {builds_lim} (GUESS)"),
                     ("goggles_dark_frame", f">= {dark_lim:g} (GUESS)"),
                     ("remix_dropped_character_frames", "0"))
    if not tel_path or not pg_path:
        for name, thr in goggle_checks + (("frames_over_50ms", "0"), ("remix_ray_tracing", "scene RT"),
                                          ("compile_wait_stuck", "no compile-wait hang"),
                                          ("never_ray_traced", "rayTraced=true when remixShaderCompiles==0")):
            run.add(name, NOT_MEASURED, None, thr, "session has no telemetry or no proxy gate CSV", tsrc)
        slow = []
    else:
        pg = read_csv_frames(pg_path, 6)
        pf_path = _first_file(folder, ("d3d9_proxy_frames.csv", "proxy_frames.csv"))
        spot = {"_data": {"pgate": pg, "proxy": read_csv_frames(pf_path, 7) if pf_path else {}}}
        keep = ("rayTraced", "frameTimeMs", "gpuTimeMs", "remixShaderCompiles")
        tel: dict[int, tuple] = {}   # frame -> (rayTraced, staticBlasBuilds, skinnedInstances, instances, luma)
        ltel: dict[int, dict] = {}   # frame -> the fields the launch and frame-time rules read
        with open(tel_path, "rb") as f:
            for ln in f:
                try:
                    o = json.loads(ln)
                except ValueError:
                    continue
                th = o.get("lumaThumb")
                luma = float(np.frombuffer(base64.b64decode(th), np.uint8).mean()) if th else None
                if isinstance(o.get("frame"), int):
                    tel[o["frame"]] = (o.get("rayTraced"), o.get("staticBlasBuilds"), o.get("skinnedInstances"),
                                       o.get("instances"), luma)
                    ltel[o["frame"]] = {k: o[k] for k in keep if k in o}

        def goggles(fr: int) -> bool | None:
            row = pg.get(fr)
            return None if row is None or len(row) < 5 else int(row[4]) == 1

        offs = [k for k in sorted(pg) if goggles(k) is False and goggles(k - 1) is True]
        builds, ratios, unknown = [], [], []
        for k in offs:
            first = next((f for f in range(k, k + 31) if f in tel and tel[f][0] is True), None)
            if first is None or not isinstance(tel[first][1], (int, float)):
                unknown.append(k)
                continue
            if not tel[first][3]:            # no level loaded: a menu, not a goggles toggle in play
                continue
            builds.append((int(tel[first][1]), k, first))
            later = [f for f in range(first, first + 200) if f in tel and tel[f][0] is True][10:11]
            if later and tel[first][4] is not None and tel[later[0]][4]:
                ratios.append((tel[first][4] / tel[later[0]][4], k, first))
        nm = f"{len(offs)} goggles-off(s), {len(unknown)} without a ray-traced frame with staticBlasBuilds in 30 frames"
        if not builds:
            run.add("goggles_toggle_rebuilds", NOT_MEASURED, None, goggle_checks[0][1], nm, tsrc)
        else:
            bad = [b for b in builds if b[0] > builds_lim]
            w = max(builds)
            run.add("goggles_toggle_rebuilds", FAIL if bad else PASS, w[0], goggle_checks[0][1],
                    f"{len(bad)} of {len(builds)} goggles-off(s) built > {builds_lim} static meshes (scene cleared); "
                    f"worst {w[0]} at goggles-off frame {w[1]} (first ray-traced frame {w[2]})"
                    + "".join(f"; {b[0]} at {b[1]}" for b in sorted(bad, key=lambda b: b[1])[:50]), tsrc)
        if not ratios:
            run.add("goggles_dark_frame", NOT_MEASURED, None, goggle_checks[1][1], nm + ", none with 10 later "
                    "ray-traced frames and lumaThumb", tsrc)
        else:
            w = min(ratios)
            run.add("goggles_dark_frame", PASS if w[0] >= dark_lim else FAIL, round(w[0], 3), goggle_checks[1][1],
                    f"worst luma ratio {w[0]:.3f} at goggles-off frame {w[1]} (first ray-traced frame {w[2]}), "
                    f"{len(ratios)} goggles-off(s)", tsrc)

        # remix_dropped_character_frames: the gate run rule, on every frame of the session
        def pg_skinned(fr: int) -> int | None:
            row = pg.get(fr)
            return None if row is None or len(row) < 5 else int(row[2])

        dropped, char_frames = [], 0
        for tf, (rt, _, sk, _, _) in tel.items():
            if not isinstance(sk, (int, float)) or not (pg_skinned(tf) or 0) > 0:
                continue
            char_frames += 1
            if rt is not True or any(goggles(f) is not False for f in range(tf - 3, tf + 4)):
                continue
            if sk == 0 and all((pg_skinned(f) or 0) > 0 for f in range(tf - 3, tf + 4)):
                dropped.append(tf)
        if not char_frames:
            run.add("remix_dropped_character_frames", NOT_MEASURED, None, "0",
                    "no frame where the proxy drew characters and telemetry has skinnedInstances", tsrc)
        else:
            run.add("remix_dropped_character_frames", PASS if not dropped else FAIL, len(dropped), "0",
                    f"{len(dropped)} of {char_frames} proxy character frames had 0 Remix skinnedInstances while the "
                    f"proxy drew characters +/-3 frames outside goggles"
                    + (f"; frames {', '.join(map(str, dropped[:10]))}" if dropped else ""), tsrc)

        # frames_over_50ms: the gate rule (only loading screens excused, by the proxy row with the same frame number;
        # goggles frames count, as goggles windows do in a gate run). result["slowFrames"] lists every frame > 50 ms.
        loading = lambda fr: is_proxy_loading(spot, fr)     # noqa: E731
        timed = [(fr, float(r["frameTimeMs"])) for fr, r in sorted(ltel.items())
                 if isinstance(r.get("frameTimeMs"), (int, float))]
        slow = [(fr, ms, "loading" if loading(fr) else None) for fr, ms in timed if ms > 50.0]
        counted = [x for x in slow if x[2] is None]
        if not timed:
            run.add("frames_over_50ms", NOT_MEASURED, None, "0", "telemetry has no frameTimeMs", tsrc)
        else:
            worst10 = sorted(counted, key=lambda x: -x[1])[:10]
            run.add("frames_over_50ms", PASS if len(counted) <= THRESHOLDS["frames_over_50ms"] else FAIL, len(counted),
                    "0", f"{len(counted)} of {len(timed)} frames over 50 ms ({len(slow) - len(counted)} loading-screen "
                    f"frames excused); ten worst: " + ", ".join(f"{fr} {ms:.1f} ms" for fr, ms, _ in worst10), tsrc)

        # remix_ray_tracing: ray-traced share of scene frames (not loading, not goggles +/-3), time to first RT frame.
        scene = [fr for fr in sorted(ltel) if isinstance(ltel[fr].get("rayTraced"), bool) and not loading(fr)
                 and not any(goggles(f) for f in range(fr - 3, fr + 4))]
        min_rt = THRESHOLDS["min_scene_rt_share"]
        rt_thresh = f"scene RT >= {min_rt:.0%} (GUESS)"
        if not scene:
            run.add("remix_ray_tracing", NOT_MEASURED, None, rt_thresh, "no scene frame with rayTraced", tsrc)
        else:
            share = sum(1 for fr in scene if ltel[fr]["rayTraced"]) / len(scene)
            run.add("remix_ray_tracing", PASS if share >= min_rt else FAIL, round(share, 4), rt_thresh,
                    f"scene RT {share:.2%} of {len(scene)} frames outside loading and goggles; from launch start "
                    f"{_time_to_first_rt_text(ltel)}", tsrc)

        # compile_wait_stuck & never_ray_traced: the gate rule on the whole launch.
        state, wait_s = _launch_compile_state(ltel, loading)
        cw_thresh = f"< {THRESHOLDS['compile_wait_stuck_s']:g} s in compile wait without rayTraced=true (GUESS)"
        nr_thresh = "rayTraced=true when remixShaderCompiles==0"
        if state == "missing":
            for name, thr in (("compile_wait_stuck", cw_thresh), ("never_ray_traced", nr_thresh)):
                run.add(name, NOT_MEASURED, None, thr, "telemetry lacks rayTraced/frameTimeMs/gpuTimeMs", tsrc)
        else:
            stuck = state == "wait" and wait_s >= THRESHOLDS["compile_wait_stuck_s"]
            run.add("compile_wait_stuck", FAIL if stuck else PASS, round(wait_s, 1), cw_thresh,
                    "ray traced" if state == "rt" else f"never ray traced, {wait_s:.1f} s in the compile wait", tsrc)
            run.add("never_ray_traced", FAIL if state == "never" else PASS, int(state == "never"), nr_thresh,
                    "never ray traced with 0 compiles" if state == "never" else "no 0-compile non-RT launch", tsrc)


    freeze_checks(run, _first_file(folder, ("d3d9_proxy_frames.csv",)))
    dxvk = _first_file(folder, ("rtx-remix_logs_remix-dxvk.log", "remix-dxvk.log"))
    if not dxvk:
        run.add("clean_quit", NOT_MEASURED, None, "no VK_ERROR_DEVICE_LOST", "no remix-dxvk.log in the session",
                "remix-dxvk.log")
    else:
        lost = [i + 1 for i, ln in enumerate(read_lines(dxvk)) if "VK_ERROR_DEVICE_LOST" in ln]
        run.add("clean_quit", FAIL if lost else PASS, len(lost), "no VK_ERROR_DEVICE_LOST",
                f"VK_ERROR_DEVICE_LOST on line(s) {', '.join(map(str, lost[:5]))}" if lost else "no device loss",
                os.path.basename(dxvk))
    status = PASS if all(x["status"] in (PASS, WARN) or (x["status"] == NOT_MEASURED and x["check"] in SOFT_NM)
                         for x in run.metrics) else FAIL
    return {"status": status, "session": os.path.abspath(folder), "thresholds": THRESHOLDS, "checks": run.metrics,
            "slowFrames": [[fr, round(ms, 1), why] for fr, ms, why in slow]}


def pose_check(run: Run, spots: list[dict]) -> None:
    """The camera direction in the proxy's trace must follow the requested yaw/pitch (relative to the first stop of
    its stretch; the sign convention is learnt from the first pure-yaw and first pitched stop), and the pan must turn
    >= 350 deg. drive.py asks each still's yaw relative to the last teleport's yaw, so a stretch ends at each teleport
    window and wherever Sam's raster-json location moves more than REF_SAM_UNITS from the stretch's (the return
    teleport after referenceStills/switchPoses records no window). Scoring a stretch against the spot's first stop gave
    96.94 deg on 191120 con_bright_hall lampsA (teleport yaw 170)."""
    src = "d3d9_proxy_frames.csv camera trace"
    worst, problems, missing = 0.0, [], []
    for s in spots:
        proxy = s["_data"]["proxy"]
        stops, stretch, here = [], 0, None
        for w in s["windows"]:
            if w["kind"] == "teleport":
                stretch, here = stretch + 1, None
                continue
            if w["kind"] != "still":
                continue
            rj = os.path.join(s["_dir"], "gate", f"{w['name']}_raster.json") if s.get("_dir") else ""
            loc = (load_json(rj) or {}).get("location") if rj and os.path.exists(rj) else None
            if loc:
                if here is not None and float(np.linalg.norm(np.subtract(loc, here))) > REF_SAM_UNITS:
                    stretch, here = stretch + 1, None
                here = loc if here is None else here
            angles = [a for f in frames_in(w, "proxy") if f in proxy and (a := camera_angles(proxy[f]))]
            if not angles:
                missing.append(f"{s['name']}/{w['name']}: no camera in the trace"); continue
            y0 = angles[0][0]
            stops.append((w["yaw"], w["pitch"], y0 + float(np.median([wrap(a[0] - y0) for a in angles])),
                          float(np.median([a[1] for a in angles])), stretch))
        if not stops or (stops[0][0], stops[0][1]) != (0, 0):
            missing.append(f"{s['name']}: no measured first stop at yaw 0 pitch 0"); continue
        _, _, by, bp, main = stops[0]
        sign_y = sign_p = 1
        y1 = next(((ry, my) for ry, rp, my, mp, k in stops if k == main and ry and not rp), None)
        if y1:
            d = wrap(y1[1] - by)
            sign_y = 1 if abs(wrap(d - y1[0])) <= abs(wrap(d + y1[0])) else -1
        p1 = next(((rp, mp) for ry, rp, my, mp, k in stops if k == main and rp), None)
        if p1:
            d = p1[1] - bp
            sign_p = 1 if abs(d - p1[0]) <= abs(d + p1[0]) else -1
        first: dict[int, tuple] = {}
        for ry, rp, my, mp, k in stops:
            ry0, rp0, my0, mp0, _ = first.setdefault(k, (ry, rp, my, mp, k))
            worst = max(worst, abs(wrap(sign_y * wrap(my - my0) - (ry - ry0))), abs(sign_p * (mp - mp0) - (rp - rp0)))
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
            imgs["on"], imgs["off"] = overlay_mask.mask_pair(imgs["on"], imgs["off"])
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
    ap.add_argument("--session", action="store_true",
                    help="folder is a free-play session's logs (play-setup logs), not a gate run: see evaluate_session")
    a = ap.parse_args()
    r = evaluate_session(a.folder) if a.session else evaluate(a.folder, previous=a.previous, reference=a.reference)
    for c in r["checks"]:
        print(f"[{c['status']}] {c['check']}: {c['detail']}")
    print("GATE", r["status"])
    sys.exit(0 if r["status"] == PASS else 1)
