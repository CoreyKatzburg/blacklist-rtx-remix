#!/usr/bin/env python3
"""Offline flicker and published-light stability audit for Blacklist RTX Remix (Task 2).

Part 1 — Published light stability & calibration discontinuities (`*_lights.tsv` + `light_calibration.h`):
  - Re-evaluates `light_calibration.h` (`CalibrateSphereLight`, `IsWeaponSpotlight`,
    `CalibrateWeaponSpotlight`, `BuildRoomFill`) on every recorded `*_lights.tsv` dump group.
  - Separates stationary fixtures (`|dpos| <= 1 cm`) from moving lights (`|dpos| > 1 cm`), quantifying:
    (a) stationary light radiance/flux pulsing across snapshots (`max_flux / min_flux`), and
    (b) the `IsWeaponSpotlight` mode-flip discontinuity at `range == 100 cm` (`kWeaponSpotMaxGameRangeCm`),
        where a 0.1 cm engine `range` change flips `r^2 * radianceMultiplier` by 52x-100x and cone width by ~4x.

Part 2 — `gate.py` `still_flicker` / `find_flashes` blind-spot audit & extended local flicker metrics:
  - Evaluates all `still` stops in no-Remix reference runs + Remix gate runs, and stationary-camera RT stretches in playtests.
  - Compares `gate.py`'s current `(block_jitter, luma_drift)` + `find_flashes` against:
      * `block_pulse`: worst 8x8 block's two-sided excursion `min(upward_travel, downward_travel)` after a
        7-frame running median (0-255 scale). The 7-frame median annihilates single 1-3 frame flashes owned by
        `find_flashes(max_len=3)`, and `min(upward_travel, downward_travel)` ignores monotonic 1-way character
        idle breathing (`<= 4.72` across all 50 no-Remix stops at `con_bright_hall` + `con_custodial`), while
        catching >= 4-frame local flares/pulses that rise and return (`local_pulse_still`).
      * `block_step4`: worst 8x8 block's 4th-largest frame-to-frame `|delta|` (`sorted_diff[-4]`, 0-255 scale),
        catching >= 2-pulse local strobes below `find_flashes`'s 12/255 floor (`sparse_strobe_still`) while
        ignoring single pops (at most 2 transitions).

Usage:
  python -I tools/remix/tests/analysis/brightness/flicker_audit.py --self-test
  python -I tools/remix/tests/analysis/brightness/flicker_audit.py
"""
from __future__ import annotations

import argparse
import base64
import csv
import glob
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

import numpy as np

RUNTIME_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "runtime"))
if RUNTIME_DIR not in sys.path:
    sys.path.insert(0, RUNTIME_DIR)
import fakerun  # noqa: E402
import gate     # noqa: E402

DEFAULT_ROOT = Path(r"D:\BlacklistOverhaul")

# Constants mirrored from play-setup/unified-build/light_calibration.h
K_MIN_FIXTURE_RADIUS_CM = 20.0
K_MAX_FIXTURE_RADIUS_CM = 35.0
K_RADIUS_RANGE_FRACTION = 0.08
K_REF_RANGE_FRACTION = 0.50
K_MAX_REF_DISTANCE_CM = 180.0
K_CLOSE_CHECK_DISTANCE_CM = 50.0
K_MAX_CLOSE_RATIO_TARGET = 5.0

K_WEAPON_SPOT_MAX_GAME_RANGE_CM = 100.0
K_WEAPON_SPOT_RADIUS_CM = 3.0
K_WEAPON_SPOT_REACH_CM = 1600.0
K_WEAPON_SPOT_REF_DISTANCE_CM = 400.0
K_WEAPON_SPOT_VOLUMETRIC_SCALE = 2.0

# Proposed gate thresholds (0-255 scale) alongside gate.py's existing still_block_jitter=4.0, still_luma_drift=6.0.
# Calibrated against 50 no-Remix reference stops at con_bright_hall + con_custodial (reference-20261116-000454:
# block_pulse max=4.72, block_step4 max=2.20 -> >2.7x-3.3x headroom, 0/50 false positives).
PROPOSED_THRESHOLDS = {
    "still_block_jitter": 4.0,
    "still_luma_drift": 6.0,
    "still_block_pulse": 16.0,
    "still_block_step4": 6.0,
}


def game_light_falloff(range_cm: float, falloff_start_cm: float, distance_cm: float) -> float:
    span = range_cm - falloff_start_cm
    t = (distance_cm - falloff_start_cm) / span if span > 1e-3 else 1.0
    t = max(0.0, min(1.0, t))
    u = 1.0 - t * t
    return u * u


def calibrate_sphere_light(range_cm: float, falloff_start_cm: float, radiance_scale: float = 1.0) -> tuple[float, float]:
    radius = max(K_MIN_FIXTURE_RADIUS_CM, min(K_MAX_FIXTURE_RADIUS_CM, range_cm * K_RADIUS_RANGE_FRACTION))
    if not (0.0 < range_cm < 1.0e5):
        return radius, radiance_scale
    d_ref = max(radius, min(K_MAX_REF_DISTANCE_CM, range_cm * K_REF_RANGE_FRACTION))
    ref_ratio = d_ref / radius
    scale_mid = game_light_falloff(range_cm, falloff_start_cm, d_ref) * ref_ratio * ref_ratio

    d_close = max(radius, min(K_CLOSE_CHECK_DISTANCE_CM, 0.5 * range_cm))
    close_ratio = d_close / radius
    scale_cap = K_MAX_CLOSE_RATIO_TARGET * game_light_falloff(range_cm, falloff_start_cm, d_close) * close_ratio * close_ratio
    return radius, min(scale_mid, scale_cap) * radiance_scale


def is_weapon_spotlight(ltype: int, range_cm: float, near_size: tuple[float, float], near_dist: float) -> bool:
    return (
        ltype == 2
        and 0.0 < range_cm <= K_WEAPON_SPOT_MAX_GAME_RANGE_CM
        and near_dist > range_cm
        and near_size[0] > 0.0
        and near_size[1] > 0.0
    )


def calibrate_weapon_spotlight(radiance_scale: float = 1.0) -> tuple[float, float]:
    ratio = K_WEAPON_SPOT_REF_DISTANCE_CM / K_WEAPON_SPOT_RADIUS_CM
    falloff = game_light_falloff(K_WEAPON_SPOT_REACH_CM, 0.0, K_WEAPON_SPOT_REF_DISTANCE_CM)
    return K_WEAPON_SPOT_RADIUS_CM, falloff * ratio * ratio * radiance_scale


def evaluate_published_light(rec: dict[str, Any]) -> dict[str, Any]:
    ltype = int(rec["type"])
    color = rec["color"]
    intensity = float(rec["intensity"])
    range_cm = float(rec["range"])
    falloff_start = float(rec["falloffStart"])
    near_w, near_h, near_d = rec["near"]
    luma_in = (0.2126 * color[0] + 0.7152 * color[1] + 0.0722 * color[2]) * intensity
    qualifies = (1 <= ltype <= 3) and (max(color) * intensity > 1e-4)
    if not qualifies:
        return {
            "qualifies": False,
            "weapon_spot": False,
            "radius_cm": 0.0,
            "mult": 0.0,
            "cone_deg": 0.0,
            "vol_scale": 0.0,
            "flux_proxy": 0.0,
        }
    if ltype == 3:
        return {
            "qualifies": True,
            "weapon_spot": False,
            "radius_cm": 0.0,
            "mult": 1.0,
            "cone_deg": 0.0,
            "vol_scale": 1.0,
            "flux_proxy": luma_in,
        }
    wspot = is_weapon_spotlight(ltype, range_cm, (near_w, near_h), near_d)
    if wspot:
        radius, mult = calibrate_weapon_spotlight(1.0)
        vol = K_WEAPON_SPOT_VOLUMETRIC_SCALE
    else:
        radius, mult = calibrate_sphere_light(range_cm, falloff_start, 1.0)
        vol = 1.0
    cone_deg = 180.0
    if ltype == 2 and near_d > 1e-3 and max(near_w, near_h) > 0.0:
        cone_deg = math.degrees(math.atan2(0.5 * max(near_w, near_h), near_d))
    # Sphere radiant intensity scales with radius^2 * radiance (= radius^2 * luma_in * mult)
    flux_proxy = (radius * radius) * luma_in * mult
    return {
        "qualifies": True,
        "weapon_spot": wspot,
        "radius_cm": radius,
        "mult": mult,
        "cone_deg": cone_deg,
        "vol_scale": vol,
        "flux_proxy": flux_proxy,
    }


def parse_lights_tsv(path: Path) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line.startswith("light\t"):
                continue
            kv: dict[str, str] = {}
            for part in line.rstrip("\r\n").split("\t")[1:]:
                if "=" in part:
                    k, v = part.split("=", 1)
                    kv[k] = v
            node = kv.get("node")
            if not node:
                continue
            pos = tuple(float(x) for x in kv.get("pos", "0,0,0").split(","))
            near = tuple(float(x) for x in kv.get("near", "0,0,0").split(","))
            color = tuple(float(x) for x in kv.get("color", "0,0,0").split(","))
            rec: dict[str, Any] = {
                "node": node,
                "flags": int(kv.get("flags", "0"), 16),
                "type": int(kv.get("type", "0")),
                "wanted": int(kv.get("wanted", "0")),
                "tracked": int(kv.get("tracked", "0")),
                "pos": pos,
                "near": near,
                "range": float(kv.get("range", "0")),
                "falloffStart": float(kv.get("falloffStart", "0")),
                "color": color,
                "intensity": float(kv.get("intensity", "0")),
            }
            rec["pub"] = evaluate_published_light(rec)
            nodes[node] = rec
    return nodes


def audit_light_dump_group(label: str, paths: list[Path]) -> dict[str, Any]:
    existing = [p for p in paths if p.exists()]
    if len(existing) < 2:
        return {"label": label, "files": [p.name for p in existing], "status": "SKIP"}
    snaps = [parse_lights_tsv(p) for p in existing]
    all_nodes = sorted({n for s in snaps for n in s})

    static_pulsing: list[dict[str, Any]] = []
    moving_changed: list[dict[str, Any]] = []
    mode_flips: list[dict[str, Any]] = []
    weapon_spot_candidates: list[dict[str, Any]] = []
    static_count = 0

    for node in all_nodes:
        seq = [s[node] for s in snaps if node in s and s[node]["pub"]["qualifies"]]
        if len(seq) < 2:
            continue
        positions = np.array([r["pos"] for r in seq], dtype=np.float64)
        max_move_cm = float(np.linalg.norm(positions - positions[0], axis=1).max())
        fluxes = [float(r["pub"]["flux_proxy"]) for r in seq]
        ranges = [float(r["range"]) for r in seq]
        wmodes = [bool(r["pub"]["weapon_spot"]) for r in seq]
        f_min, f_max = min(fluxes), max(fluxes)
        ratio = f_max / max(f_min, 1e-6) if f_max > 0 else 1.0

        # Track guard rifle spotlight signature (type=2, near=(50,50,150)) even inside a single snapshot
        if seq[0]["type"] == 2 and seq[0]["near"] == (50.0, 50.0, 150.0):
            weapon_spot_candidates.append({
                "node": node,
                "flags": f"0x{seq[0]['flags']:04X}",
                "ranges_cm": [round(x, 1) for x in ranges],
                "weapon_modes": wmodes,
                "flux_ratio": round(ratio, 2),
                "max_move_cm": round(max_move_cm, 1),
            })

        if len(set(wmodes)) > 1:
            mode_flips.append({
                "node": node,
                "ranges_cm": [round(x, 1) for x in ranges],
                "flux_ratio": round(ratio, 2),
                "max_move_cm": round(max_move_cm, 1),
            })

        if max_move_cm <= 1.0:
            static_count += 1
            if ratio > 1.05:
                static_pulsing.append({
                    "node": node,
                    "type": seq[0]["type"],
                    "pos": [round(x, 1) for x in seq[0]["pos"]],
                    "color_min": [round(x, 4) for x in seq[int(np.argmin(fluxes))]["color"]],
                    "color_max": [round(x, 4) for x in seq[int(np.argmax(fluxes))]["color"]],
                    "ranges_cm": [round(min(ranges), 1), round(max(ranges), 1)],
                    "flux_ratio": round(ratio, 2),
                })
        elif ratio > 1.05:
            moving_changed.append({
                "node": node,
                "type": seq[0]["type"],
                "ranges_cm": [round(min(ranges), 1), round(max(ranges), 1)],
                "weapon_modes": sorted(set(wmodes)),
                "flux_ratio": round(ratio, 2),
                "max_move_cm": round(max_move_cm, 1),
            })

    static_pulsing.sort(key=lambda x: x["flux_ratio"], reverse=True)
    return {
        "label": label,
        "files": [p.name for p in existing],
        "static_qualifying_nodes": static_count,
        "static_pulsing_count": len(static_pulsing),
        "static_pulsing_max_ratio": max((x["flux_ratio"] for x in static_pulsing), default=1.0),
        "static_pulsing_top": static_pulsing[:8],
        "moving_flux_changed_count": len(moving_changed),
        "weapon_spot_mode_flips": mode_flips,
        "guard_flashlight_nodes": weapon_spot_candidates,
    }


def _fast_thumbs(work: np.ndarray) -> np.ndarray:
    n, fh, fw = work.shape
    th, tw = gate.THUMB_H, gate.THUMB_W
    if fh <= th and fw <= tw:
        return work
    if fh % th == 0 and fw % tw == 0:
        return work.reshape(n, th, fh // th, tw, fw // tw).mean(axis=(2, 4), dtype=np.float32)
    return np.array([gate.thumb(f, tw, th) for f in work], np.float32)


def still_flicker_extended(frames: np.ndarray, block: int = 8) -> dict[str, float]:
    """Computes gate.py's (block_jitter, luma_drift) plus blind-spot-closing (block_pulse, block_step4) on 0-255 scale."""
    deduped = gate.dedupe(frames)
    n = len(deduped)
    if n < 4:
        return {
            "n_deduped": n,
            "block_jitter": 0.0,
            "luma_drift": 0.0,
            "block_pulse": 0.0,
            "block_step4": 0.0,
        }
    work = _fast_thumbs(deduped.astype(np.float32, copy=False))
    h, w = (work.shape[1] // block) * block, (work.shape[2] // block) * block
    blocks = work[:, :h, :w].reshape(n, h // block, block, w // block, block).mean(axis=(2, 4))
    diffs = np.abs(np.diff(blocks, axis=0))
    block_jitter = float(np.median(diffs, axis=0).max())

    means = np.sort(work.mean(axis=(1, 2)))
    trimmed = means[2:-2] if len(means) >= 8 else means
    luma_drift = float(trimmed[-1] - trimmed[0])

    # 7-frame running median (edge-padded) suppresses any single <=3-frame flash (owned by find_flashes),
    # and min(upward_travel, downward_travel) suppresses 1-way character idle breathing while catching >=4-frame pulses.
    if n >= 8:
        pad = np.pad(blocks, ((3, 3), (0, 0), (0, 0)), mode="edge")
        smooth = np.median(np.stack([pad[k : k + n] for k in range(7)], axis=0), axis=0)
        sdiff = np.diff(smooth, axis=0)
        pos_travel = np.maximum(0.0, sdiff).sum(axis=0)
        neg_travel = np.maximum(0.0, -sdiff).sum(axis=0)
        block_pulse = float(np.minimum(pos_travel, neg_travel).max())
    else:
        block_pulse = 0.0

    # 4th-largest frame-to-frame |delta| in worst 8x8 block: ignores a single pop/step (at most 2 transitions)
    # while catching any >=2-pulse local strobe even below find_flashes's 12/255 floor.
    d_sorted = np.sort(diffs, axis=0)
    block_step4 = float(d_sorted[-4].max()) if len(diffs) >= 5 else 0.0

    return {
        "n_deduped": n,
        "block_jitter": block_jitter,
        "luma_drift": luma_drift,
        "block_pulse": block_pulse,
        "block_step4": block_step4,
    }


def _load_spot_telem_thumbs(spot_dir: Path) -> dict[int, np.ndarray]:
    telem_path = spot_dir / "telemetry.jsonl"
    if not telem_path.exists():
        telem_path = spot_dir / "rtx_telemetry.jsonl"
    thumbs: dict[int, np.ndarray] = {}
    if not telem_path.exists():
        return thumbs
    with telem_path.open("r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            if not ln.strip():
                continue
            rec = json.loads(ln)
            fr = int(rec.get("frame", -1))
            b64 = rec.get("lumaThumb")
            if fr >= 0 and b64:
                raw = base64.b64decode(b64)
                if len(raw) == gate.THUMB_W * gate.THUMB_H:
                    thumbs[fr] = np.frombuffer(raw, dtype=np.uint8).reshape(gate.THUMB_H, gate.THUMB_W)
    return thumbs


def _load_gate_still_frames(
    spot_dir: Path, win: dict[str, Any], telem_thumbs: dict[int, np.ndarray]
) -> tuple[np.ndarray | None, str]:
    # Prefer telemetry lumaThumb when complete, matching gate.py window_frames
    t_range = win.get("telemetry")
    if isinstance(t_range, list) and len(t_range) == 2 and telem_thumbs:
        f0, f1 = int(t_range[0]), int(t_range[1])
        thumbs = [telem_thumbs[fr] for fr in range(f0, f1) if fr in telem_thumbs]
        if len(thumbs) >= 15:
            return np.array(thumbs, dtype=np.float32), "telemetry.lumaThumb"
    rel = win.get("frames")
    fsize = win.get("frameSize")
    if rel and isinstance(fsize, list) and len(fsize) == 2:
        gp = spot_dir / rel
        if gp.exists():
            fw, fh = int(fsize[0]), int(fsize[1])
            raw = gp.read_bytes()
            nf = len(raw) // (fw * fh)
            if nf > 0:
                arr = np.frombuffer(raw[: nf * fw * fh], dtype=np.uint8).reshape(nf, fh, fw)
                return arr.astype(np.float32), "frames.gray"
    return None, "none"


def audit_gate_run_flicker(run_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not run_dir.exists():
        return rows
    for sp_json in sorted(run_dir.glob("*/spot.json")):
        spot_dir = sp_json.parent
        spot_name = spot_dir.name
        data = json.loads(sp_json.read_text(encoding="utf-8"))
        telem_thumbs = _load_spot_telem_thumbs(spot_dir)
        for w in data.get("windows", []):
            if w.get("kind") != "still":
                continue
            frames, src = _load_gate_still_frames(spot_dir, w, telem_thumbs)
            if frames is None:
                continue
            ext = still_flicker_extended(frames)
            if ext["n_deduped"] < 15:
                continue
            flashes = gate.find_flashes(_fast_thumbs(frames))
            old_fail = (
                ext["block_jitter"] > PROPOSED_THRESHOLDS["still_block_jitter"]
                or ext["luma_drift"] > PROPOSED_THRESHOLDS["still_luma_drift"]
                or len(flashes) > 0
            )
            new_fail = (
                old_fail
                or ext["block_pulse"] > PROPOSED_THRESHOLDS["still_block_pulse"]
                or ext["block_step4"] > PROPOSED_THRESHOLDS["still_block_step4"]
            )
            rows.append({
                "run": run_dir.name,
                "spot": spot_name,
                "window": w.get("name"),
                "source": src,
                "flashes": len(flashes),
                "old_fail": old_fail,
                "new_fail": new_fail,
                **{k: round(v, 3) if isinstance(v, float) else v for k, v in ext.items()},
            })
    return rows


def audit_session_still_stretches(session_dir: Path, label: str, min_len: int = 18) -> dict[str, Any]:
    frames_csv = session_dir / "d3d9_proxy_frames.csv"
    telem_jsonl = session_dir / "rtx_telemetry.jsonl"
    gate_csv = session_dir / "d3d9_proxy_gate.csv"
    if not (frames_csv.exists() and telem_jsonl.exists()):
        return {"label": label, "stretches": 0}

    poses: list[tuple[float, float, float, float, float, float] | None] = []
    with frames_csv.open("r", encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f):
            if len(row) < 7 or not row[6]:
                poses.append(None)
                continue
            fields = row[6].split("|")[0].split(":")
            if len(fields) >= 9:
                poses.append(tuple(float(fields[i]) for i in range(3, 9)))
            else:
                poses.append(None)

    n_total = len(poses)
    goggles = np.zeros(n_total, dtype=np.int8)
    if gate_csv.exists():
        with gate_csv.open("r", encoding="utf-8", errors="replace") as f:
            for row in csv.reader(f):
                if len(row) >= 5:
                    fr = int(row[0])
                    if 0 <= fr < n_total:
                        goggles[fr] = int(row[4])

    thumbs: dict[int, np.ndarray] = {}
    with telem_jsonl.open("r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            if not ln.strip():
                continue
            rec = json.loads(ln)
            if not rec.get("rayTraced", True):
                continue
            fr = int(rec.get("frame", -1))
            b64 = rec.get("lumaThumb")
            if 0 <= fr < n_total and b64 and goggles[fr] == 0:
                raw = base64.b64decode(b64)
                if len(raw) == gate.THUMB_W * gate.THUMB_H:
                    thumbs[fr] = np.frombuffer(raw, dtype=np.uint8).reshape(gate.THUMB_H, gate.THUMB_W)

    # Find consecutive stationary-camera RT frame runs (|dpos| <= 0.5 cm, |dfwd| <= 5e-4)
    stretches: list[list[int]] = []
    cur: list[int] = []
    for fr in range(n_total):
        p = poses[fr]
        if p is None or fr not in thumbs:
            if len(cur) >= min_len:
                stretches.append(cur)
            cur = []
            continue
        if not cur:
            cur = [fr]
            continue
        p0 = poses[cur[-1]]
        assert p0 is not None
        dp = math.sqrt(sum((p[k] - p0[k]) ** 2 for k in range(3)))
        df = math.sqrt(sum((p[3 + k] - p0[3 + k]) ** 2 for k in range(3)))
        if dp <= 0.5 and df <= 5e-4 and fr == cur[-1] + 1:
            cur.append(fr)
        else:
            if len(cur) >= min_len:
                stretches.append(cur)
            cur = [fr]
    if len(cur) >= min_len:
        stretches.append(cur)

    blind_spot_hits: list[dict[str, Any]] = []
    old_hits = 0
    new_hits = 0
    for st in stretches:
        # Score 20-frame chunks (matching gate still stops)
        for off in range(0, len(st) - min_len + 1, 20):
            sub = st[off : off + 20]
            arr = np.array([thumbs[i] for i in sub], dtype=np.float32)
            ext = still_flicker_extended(arr)
            if ext["n_deduped"] < 15:
                continue
            fl = gate.find_flashes(arr)
            old_f = (
                ext["block_jitter"] > PROPOSED_THRESHOLDS["still_block_jitter"]
                or ext["luma_drift"] > PROPOSED_THRESHOLDS["still_luma_drift"]
                or len(fl) > 0
            )
            new_f = (
                old_f
                or ext["block_pulse"] > PROPOSED_THRESHOLDS["still_block_pulse"]
                or ext["block_step4"] > PROPOSED_THRESHOLDS["still_block_step4"]
            )
            if old_f:
                old_hits += 1
            if new_f:
                new_hits += 1
            if new_f and not old_f:
                blind_spot_hits.append({
                    "f_range": f"{sub[0]}..{sub[-1]}",
                    "block_jitter": round(ext["block_jitter"], 2),
                    "luma_drift": round(ext["luma_drift"], 2),
                    "block_pulse": round(ext["block_pulse"], 2),
                    "block_step4": round(ext["block_step4"], 2),
                })
    blind_spot_hits.sort(key=lambda x: max(x["block_pulse"], x["block_step4"]), reverse=True)
    return {
        "label": label,
        "stationary_stretches": len(stretches),
        "old_gate_caught": old_hits,
        "extended_gate_caught": new_hits,
        "missed_by_old_caught_by_new": len(blind_spot_hits),
        "top_blind_spot_examples": blind_spot_hits[:8],
    }


def run_self_test() -> None:
    # 1. Verify weapon-spotlight mode-flip discontinuity at kWeaponSpotMaxGameRangeCm = 100.0 cm.
    guard_r50 = {
        "type": 2, "color": (23.2, 25.9, 27.6), "intensity": 1.0,
        "range": 50.0, "falloffStart": 0.0, "near": (50.0, 50.0, 150.0),
    }
    guard_r138 = {**guard_r50, "range": 138.0}
    p50 = evaluate_published_light(guard_r50)
    p138 = evaluate_published_light(guard_r138)
    assert p50["weapon_spot"] is True and p138["weapon_spot"] is False
    flux_jump = p50["flux_proxy"] / p138["flux_proxy"]
    assert 50.0 < flux_jump < 55.0, f"expected ~52x flux jump across range=100cm, got {flux_jump}"

    # 2. Verify flicker metrics preserve calm, 1-3 frame pop, AND 1-way idle breathing while closing both blind spots.
    rng = np.random.default_rng(3)
    calm = np.array([fakerun.view(0, 0, rng) for _ in range(18)], np.float32)
    c_ext = still_flicker_extended(calm)
    assert max(c_ext["block_jitter"], c_ext["luma_drift"], c_ext["block_pulse"], c_ext["block_step4"]) < 1.0, c_ext

    # 2a. Single 1-frame and 3-frame pops (caught by find_flashes, MUST NOT fail still_flicker / extended metrics).
    pop = calm.copy()
    pop[7, 20:40, 40:64] = 255.0 - pop[7, 20:40, 40:64]
    assert len(gate.find_flashes(pop)) == 1
    p_ext = still_flicker_extended(pop)
    assert p_ext["block_pulse"] < 2.0 and p_ext["block_step4"] < 2.0, p_ext

    pop3 = calm.copy()
    pop3[7:10, 20:40, 40:64] = 255.0 - pop3[7:10, 20:40, 40:64]
    assert len(gate.find_flashes(pop3)) == 1
    p3_ext = still_flicker_extended(pop3)
    assert p3_ext["block_pulse"] < 2.0 and p3_ext["block_step4"] < 2.0, p3_ext

    # 2b. Monotonic 1-way character idle breathing (+34/255 ramp on a 16x16 silhouette block, 2.0/frame):
    #     Matches no-Remix reference-20261116-000454 and MUST NOT fail either extended metric!
    breath = calm.copy()
    for t in range(len(breath)):
        breath[t, 24:40, 48:64] = np.clip(breath[t, 24:40, 48:64] + 2.0 * t, 0.0, 255.0)
    b_ext = still_flicker_extended(breath)
    assert b_ext["block_pulse"] <= PROPOSED_THRESHOLDS["still_block_pulse"], b_ext
    assert b_ext["block_step4"] <= PROPOSED_THRESHOLDS["still_block_step4"], b_ext
    assert b_ext["block_pulse"] < 1.0, f"1-way monotonic drift must have <1.0 return pulse, got {b_ext}"

    # 2c. Blind spot #1 (`local_pulse_still`): 5-frame local orb/flare (+32/255 on a 16x24 patch).
    #     Passes find_flashes (5 > max_len=3) AND passes old still_flicker (median |diff| == 0, whole-frame drift < 2),
    #     but FAILS proposed still_block_pulse (> 16.0)!
    local_pulse = calm.copy()
    local_pulse[6:11, 16:32, 40:64] = np.clip(local_pulse[6:11, 16:32, 40:64] + 32.0, 0.0, 255.0)
    assert gate.find_flashes(local_pulse) == [], "expected find_flashes blind spot on 5-frame pulse"
    old_bj, old_ld = gate.still_flicker(local_pulse)
    assert old_bj <= PROPOSED_THRESHOLDS["still_block_jitter"] and old_ld <= PROPOSED_THRESHOLDS["still_luma_drift"]
    lp_ext = still_flicker_extended(local_pulse)
    assert lp_ext["block_pulse"] > PROPOSED_THRESHOLDS["still_block_pulse"], lp_ext

    # 2d. Blind spot #2 (`sparse_strobe_still`): 1-frame local strobe every 4th frame (frames 2, 6, 10, 14) at +10/255.
    #     Passes find_flashes (10 < floor 12) AND passes old still_flicker (8/17 transitions -> median |diff| == 0),
    #     but FAILS proposed still_block_step4 (> 6.0)!
    sparse_strobe = calm.copy()
    sparse_strobe[2::4, 16:32, 40:56] = np.clip(sparse_strobe[2::4, 16:32, 40:56] + 10.0, 0.0, 255.0)
    assert gate.find_flashes(sparse_strobe) == [], "expected find_flashes blind spot below floor=12"
    sbj, sld = gate.still_flicker(sparse_strobe)
    assert sbj <= PROPOSED_THRESHOLDS["still_block_jitter"] and sld <= PROPOSED_THRESHOLDS["still_luma_drift"]
    ss_ext = still_flicker_extended(sparse_strobe)
    assert ss_ext["block_step4"] > PROPOSED_THRESHOLDS["still_block_step4"], ss_ext

    print("flicker_audit self-test: PASS")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Workspace root directory")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()

    if args.self_test:
        run_self_test()
        return 0

    root = args.root
    remix_dir = root / "tools" / "remix"
    gate_root = root / "experiments" / "remix-gate"
    ev_root = root / "experiments" / "playtest-evidence"

    dump_groups = [
        ("paladin_run1_rt_toggle", sorted((remix_dir / "fixtures" / "run1").glob("*_lights.tsv"))),
        ("paladin_run7_soak", [
            remix_dir / "fixtures" / "run7" / "soak_start_lights.tsv",
            remix_dir / "fixtures" / "run7" / "soak_end_lights.tsv",
        ]),
        ("consumption_run20_cam", sorted((remix_dir / "fixtures" / "run20").glob("*_lights.tsv"))),
        ("gate_con_custodial_base", [
            gate_root / "20261115-140515" / "con_custodial" / "switch" / "base_lights.tsv",
            gate_root / "20261115-140515" / "con_custodial" / "switch" / "base_again_lights.tsv",
        ]),
        ("gate_con_bright_hall_base", [
            gate_root / "20261115-142638" / "con_bright_hall" / "switch" / "base_lights.tsv",
            gate_root / "20261115-142638" / "con_bright_hall" / "switch" / "base_again_lights.tsv",
        ]),
        ("gate_con_green_hall_base", [
            gate_root / "20261115-142638" / "con_green_hall" / "switch" / "base_lights.tsv",
            gate_root / "20261115-142638" / "con_green_hall" / "switch" / "base_again_lights.tsv",
        ]),
    ]

    light_results = [audit_light_dump_group(lbl, paths) for lbl, paths in dump_groups]
    print("=== PART 1: PUBLISHED LIGHT STABILITY & CALIBRATION DISCONTINUITIES ===")
    for lr in light_results:
        if lr.get("status") == "SKIP":
            continue
        print(
            f"  [{lr['label']}] files={len(lr['files'])} static_nodes={lr['static_qualifying_nodes']} "
            f"static_pulsing={lr['static_pulsing_count']} (max_flux_ratio={lr['static_pulsing_max_ratio']:.2f}x) "
            f"moving_changed={lr['moving_flux_changed_count']} weapon_flips={len(lr['weapon_spot_mode_flips'])} "
            f"guard_flashlights={len(lr['guard_flashlight_nodes'])}"
        )
        for top in lr["static_pulsing_top"][:3]:
            print(f"    static pulse node={top['node']} pos={top['pos']} flux_ratio={top['flux_ratio']}x color={top['color_min']}->{top['color_max']}")
        for gf in lr["guard_flashlight_nodes"][:4]:
            print(f"    guard spot node={gf['node']} ranges={gf['ranges_cm']} weapon_modes={gf['weapon_modes']} flux_ratio={gf['flux_ratio']}x")

    ref_runs = ["reference-20261116-000454", "reference-20261116-002631"]
    ref_rows: list[dict[str, Any]] = []
    for rr in ref_runs:
        ref_rows.extend(audit_gate_run_flicker(gate_root / rr))

    print(f"\n=== PART 2A-0: NO-REMIX REFERENCE STILL WINDOWS ({len(ref_rows)} windows across {ref_runs}) ===")
    for rr in ref_runs:
        spots = sorted({r["spot"] for r in ref_rows if r["run"] == rr})
        for sp in spots:
            sr = [r for r in ref_rows if r["run"] == rr and r["spot"] == sp]
            print(
                f"  [{rr}/{sp}] n={len(sr)} "
                f"jitter_max={max(r['block_jitter'] for r in sr):.2f} "
                f"drift_max={max(r['luma_drift'] for r in sr):.2f} "
                f"pulse_med={float(np.median([r['block_pulse'] for r in sr])):.2f} "
                f"pulse_max={max(r['block_pulse'] for r in sr):.2f} "
                f"step4_max={max(r['block_step4'] for r in sr):.2f} "
                f"new_fail={sum(1 for r in sr if r['new_fail'])}/{len(sr)}"
            )

    gate_runs = ["20261114-153401", "20261115-140515", "20261115-142638"]
    gate_rows: list[dict[str, Any]] = []
    for gr in gate_runs:
        gate_rows.extend(audit_gate_run_flicker(gate_root / gr))

    print(f"\n=== PART 2A: REMIX GATE STILL WINDOWS ({len(gate_rows)} windows across {gate_runs}) ===")
    if gate_rows:
        for key in ("block_jitter", "luma_drift", "block_pulse", "block_step4"):
            vals = [float(r[key]) for r in gate_rows]
            print(f"  {key:14s}: min={min(vals):.2f} med={float(np.median(vals)):.2f} p95={float(np.percentile(vals, 95)):.2f} max={max(vals):.2f}")
        print(f"  old_fail={sum(1 for r in gate_rows if r['old_fail'])}/{len(gate_rows)}, new_fail={sum(1 for r in gate_rows if r['new_fail'])}/{len(gate_rows)}")
        for r in sorted(gate_rows, key=lambda x: max(x["block_pulse"], x["block_step4"]), reverse=True)[:10]:
            print(
                f"    {r['run']}/{r['spot']}/{r['window']} ({r['source']}): "
                f"jitter={r['block_jitter']:.2f} drift={r['luma_drift']:.2f} "
                f"pulse={r['block_pulse']:.2f} step4={r['block_step4']:.2f} flashes={r['flashes']} "
                f"old_fail={r['old_fail']}"
            )

    sess_dirs = [
        ("nov13", ev_root / "play-setup-logs" / "before-20261114-143424"),
        ("nov14_1531", ev_root / "play-setup-logs" / "before-20261114-153109"),
        ("nov14_1855", ev_root / "steam-session-20261114-1855"),
    ]
    sess_results = [audit_session_still_stretches(p, lbl) for lbl, p in sess_dirs]
    print("\n=== PART 2B: PLAYTEST STATIONARY-CAMERA RT WINDOWS (20-frame chunks, goggles=0) ===")
    for sr in sess_results:
        print(
            f"  [{sr['label']}] stretches={sr.get('stationary_stretches', 0)} "
            f"old_gate_caught={sr.get('old_gate_caught', 0)} "
            f"extended_caught={sr.get('extended_gate_caught', 0)} "
            f"blind_spot_only={sr.get('missed_by_old_caught_by_new', 0)}"
        )
        for ex in sr.get("top_blind_spot_examples", [])[:4]:
            print(f"    frames={ex['f_range']} jitter={ex['block_jitter']} drift={ex['luma_drift']} -> pulse={ex['block_pulse']} step4={ex['block_step4']}")

    if args.json_out:
        args.json_out.write_text(
            json.dumps({"lights": light_results, "reference_still": ref_rows, "gate_still": gate_rows, "sessions": sess_results}, indent=2),
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
