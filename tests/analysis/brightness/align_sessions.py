#!/usr/bin/env python3
"""Offline session alignment for Blacklist RTX Remix brightness and cinematics (Task 1).

Joins per-PID d3d9_proxy.log wall-clock anchors (lightpub + PassFilter),
d3d9_proxy_frames.csv QPC timestamps + camera pose/source ('G' gameplay vs 'P' pure/cinematic),
d3d9_proxy_gate.csv scene/goggle/RT flags, and rtx_telemetry.jsonl 128x72 lumaThumb
(cropped to the 8% HUD-free interior ROI) across the three recorded playtest sessions:
  - nov13      : before-20261114-143424 (pid 44264, video 21:22:48..21:33:13)
  - nov14_1531 : before-20261114-153109 (pid 23260, session 14:35:04..15:02:34, video 14:49:59..15:02:18)
  - nov14_1855 : steam-session-20261114-1855 (pid 25832, session 18:55:42..19:19:22, materials mod active)
"""

from __future__ import annotations

import argparse
import base64
import csv
import datetime as dt
import json
import math
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

THUMB_W = 128
THUMB_H = 72
THUMB_PX = THUMB_W * THUMB_H
HUD_MARGIN = 0.08
NEAR_BLACK_GAMMA = 0.02
NEAR_WHITE_GAMMA = 0.98
MAX_ANCHOR_RESIDUAL_MS = 500.0

DEFAULT_ROOT = Path(r"D:\BlacklistOverhaul")


@dataclass(frozen=True)
class SessionSpec:
    key: str
    folder: Path
    pid: int
    video_start: str | None  # "YYYY-MM-DD HH:MM:SS"
    video_end: str | None


def build_default_sessions(root: Path = DEFAULT_ROOT) -> dict[str, SessionSpec]:
    return {
        "nov13": SessionSpec(
            key="nov13",
            folder=root / r"experiments\playtest-evidence\play-setup-logs\before-20261114-143424",
            pid=44264,
            video_start="2026-11-13 21:22:48",
            video_end="2026-11-13 21:33:13",
        ),
        "nov14_1531": SessionSpec(
            key="nov14_1531",
            folder=root / r"experiments\playtest-evidence\play-setup-logs\before-20261114-153109",
            pid=23260,
            video_start="2026-11-14 14:49:59",
            video_end="2026-11-14 15:02:18",
        ),
        "nov14_1855": SessionSpec(
            key="nov14_1855",
            folder=root / r"experiments\playtest-evidence\steam-session-20261114-1855",
            pid=25832,
            video_start=None,
            video_end=None,
        ),
    }

RE_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \[pid (\d+)\] (.*)$")
RE_LIGHTPUB = re.compile(
    r"lightpub: frame (\d+) qualifying (\d+) drawn (\d+) "
    r"\(room fill (\d+), sky dome (\d+)\) created (\d+) updated (\d+) tracked (\d+)"
)
RE_PASSFILTER = re.compile(
    r"PassFilter: last (\d+) frames (\d+) with a 3D scene, (\d+) scene draws sent to ray tracing, "
    r"(\d+) hand-offs \((\d+) before post-processing\)"
)
RE_DOME = re.compile(r"lightpub: sky dome radiance ([0-9.eE+-]+) ([0-9.eE+-]+) ([0-9.eE+-]+)")
RE_AUTO_RT = re.compile(r"lightpub: AutoRaytracing -> (on|off), (.*)$")


def parse_wall_ts(ts_str: str) -> float:
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in ts_str else "%Y-%m-%d %H:%M:%S"
    return dt.datetime.strptime(ts_str, fmt).timestamp()


def fmt_wall_hms(epoch_s: float) -> str:
    return dt.datetime.fromtimestamp(epoch_s).strftime("%H:%M:%S")


def fmt_mmss(delta_s: float) -> str:
    sign = "-" if delta_s < 0 else ""
    total = int(round(abs(delta_s)))
    return f"{sign}{total // 60:02d}:{total % 60:02d}"


def thumb_roi_slices(height: int = THUMB_H, width: int = THUMB_W, margin: float = HUD_MARGIN) -> tuple[slice, slice]:
    my = int(round(height * margin))
    mx = int(round(width * margin))
    return slice(my, height - my), slice(mx, width - mx)


def decode_luma_thumb_stats(b64_str: str | None) -> tuple[float, float, float, float, float, float]:
    """Returns (mean, p10, p50, p90, near_black, near_white) in gamma [0,1] on the 8% interior ROI."""
    if not b64_str:
        nan = float("nan")
        return (nan, nan, nan, nan, nan, nan)
    raw = base64.b64decode(b64_str)
    if len(raw) != THUMB_PX:
        nan = float("nan")
        return (nan, nan, nan, nan, nan, nan)
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(THUMB_H, THUMB_W).astype(np.float32) * (1.0 / 255.0)
    ys, xs = thumb_roi_slices()
    roi = arr[ys, xs]
    p10, p50, p90 = np.percentile(roi, [10.0, 50.0, 90.0])
    return (
        float(roi.mean()),
        float(p10),
        float(p50),
        float(p90),
        float(np.mean(roi < NEAR_BLACK_GAMMA)),
        float(np.mean(roi >= NEAR_WHITE_GAMMA)),
    )


def parse_proxy_log(log_path: Path, pid: int) -> dict[str, Any]:
    lightpub_rows: list[dict[str, Any]] = []
    dome_events: list[dict[str, Any]] = []
    autort_events: list[dict[str, Any]] = []
    first_ts: float | None = None
    last_ts: float | None = None

    pending_lp: dict[str, Any] | None = None
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if f"[pid {pid}]" not in line:
                continue
            m = RE_LOG_TS.match(line.rstrip())
            if not m:
                continue
            ts = parse_wall_ts(m.group(1))
            msg = m.group(3)
            if first_ts is None:
                first_ts = ts
            last_ts = ts

            m_lp = RE_LIGHTPUB.search(msg)
            if m_lp:
                pending_lp = {
                    "wall_ts": ts,
                    "frame": int(m_lp.group(1)),
                    "qualifying": int(m_lp.group(2)),
                    "drawn": int(m_lp.group(3)),
                    "room_fill": int(m_lp.group(4)),
                    "sky_dome": int(m_lp.group(5)),
                    "created": int(m_lp.group(6)),
                    "updated": int(m_lp.group(7)),
                    "tracked": int(m_lp.group(8)),
                    "scene_frames_600": 0,
                    "scene_draws_600": 0,
                    "handoffs_600": 0,
                    "before_post_600": 0,
                }
                lightpub_rows.append(pending_lp)
                continue

            m_pf = RE_PASSFILTER.search(msg)
            if m_pf and pending_lp is not None and abs(ts - pending_lp["wall_ts"]) < 0.5:
                pending_lp["scene_frames_600"] = int(m_pf.group(2))
                pending_lp["scene_draws_600"] = int(m_pf.group(3))
                pending_lp["handoffs_600"] = int(m_pf.group(4))
                pending_lp["before_post_600"] = int(m_pf.group(5))
                continue

            m_dm = RE_DOME.search(msg)
            if m_dm:
                dome_events.append({
                    "wall_ts": ts,
                    "r": float(m_dm.group(1)),
                    "g": float(m_dm.group(2)),
                    "b": float(m_dm.group(3)),
                })
                continue

            m_ar = RE_AUTO_RT.search(msg)
            if m_ar:
                autort_events.append({
                    "wall_ts": ts,
                    "state": m_ar.group(1),
                    "detail": m_ar.group(2),
                })

    return {
        "first_ts": first_ts,
        "last_ts": last_ts,
        "lightpub": lightpub_rows,
        "dome_events": dome_events,
        "autort_events": autort_events,
    }


def load_aligned_session(spec: SessionSpec) -> dict[str, Any]:
    log_info = parse_proxy_log(spec.folder / "d3d9_proxy.log", spec.pid)

    # Load d3d9_proxy_frames.csv
    frames_csv = spec.folder / "d3d9_proxy_frames.csv"
    qpc_ms_list: list[float] = []
    cam_src_list: list[str] = []
    cam_pos_list: list[tuple[float, float, float]] = []
    total_draws_list: list[int] = []

    with frames_csv.open("r", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) < 6:
                continue
            qpc_ms = float(row[1])
            draws = int(row[4])
            pubs = row[6] if len(row) > 6 else ""
            src = "-"
            px = py = pz = float("nan")
            if pubs:
                # Choose first non-skipped publish if present, else first publish
                chosen = None
                for part in pubs.split("|"):
                    fields = part.split(":")
                    if len(fields) >= 9:
                        if chosen is None or fields[2] == "0":
                            chosen = fields
                            if fields[2] == "0":
                                break
                if chosen is not None:
                    src = chosen[0]
                    px, py, pz = float(chosen[3]), float(chosen[4]), float(chosen[5])
            qpc_ms_list.append(qpc_ms)
            cam_src_list.append(src)
            cam_pos_list.append((px, py, pz))
            total_draws_list.append(draws)

    n_proxy = len(qpc_ms_list)
    qpc_s = np.array(qpc_ms_list, dtype=np.float64) * 1e-3

    # Anchor QPC seconds to wall-clock epoch seconds using lightpub: frame K lines.
    # Note: lightpub's g_frame increments once per Present alongside hook->tracedFrames.
    offsets: list[float] = []
    for lp in log_info["lightpub"]:
        fr = lp["frame"]
        if 0 <= fr < n_proxy:
            offsets.append(lp["wall_ts"] - qpc_s[fr])
    wall_offset = float(np.median(offsets)) if offsets else (log_info["first_ts"] or 0.0) - qpc_s[0]
    wall_s = qpc_s + wall_offset
    anchor_residuals_ms = [
        (lp["wall_ts"] - (qpc_s[lp["frame"]] + wall_offset)) * 1000.0
        for lp in log_info["lightpub"]
        if 0 <= lp["frame"] < n_proxy
    ]

    # Load d3d9_proxy_gate.csv if present
    gate_csv = spec.folder / "d3d9_proxy_gate.csv"
    scene_draws = np.zeros(n_proxy, dtype=np.int32)
    handoffs = np.zeros(n_proxy, dtype=np.int32)
    goggles = np.zeros(n_proxy, dtype=np.int8)
    rt_active = np.zeros(n_proxy, dtype=np.int8)
    if gate_csv.exists():
        with gate_csv.open("r", encoding="utf-8", errors="replace") as f:
            for row in csv.reader(f):
                if len(row) < 6:
                    continue
                fr = int(row[0])
                if 0 <= fr < n_proxy:
                    scene_draws[fr] = int(row[1])
                    handoffs[fr] = int(row[3])
                    goggles[fr] = int(row[4])
                    rt_active[fr] = int(row[5])

    # Load rtx_telemetry.jsonl
    telem_path = spec.folder / "rtx_telemetry.jsonl"
    luma_mean = np.full(n_proxy, np.nan, dtype=np.float32)
    luma_p10 = np.full(n_proxy, np.nan, dtype=np.float32)
    luma_p50 = np.full(n_proxy, np.nan, dtype=np.float32)
    luma_p90 = np.full(n_proxy, np.nan, dtype=np.float32)
    near_black = np.full(n_proxy, np.nan, dtype=np.float32)
    near_white = np.full(n_proxy, np.nan, dtype=np.float32)
    thumb_clipped_frac = np.full(n_proxy, np.nan, dtype=np.float32)
    ray_traced = np.full(n_proxy, -1, dtype=np.int8)
    replaced_draws = np.zeros(n_proxy, dtype=np.int32)
    legacy_draws = np.zeros(n_proxy, dtype=np.int32)
    instances = np.zeros(n_proxy, dtype=np.int32)

    telem_count = 0
    with telem_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            fr = int(rec.get("frame", telem_count))
            telem_count += 1
            if fr >= n_proxy:
                continue
            m_val, p10_v, p50_v, p90_v, nb_v, nw_v = decode_luma_thumb_stats(rec.get("lumaThumb"))
            luma_mean[fr] = m_val
            luma_p10[fr] = p10_v
            luma_p50[fr] = p50_v
            luma_p90[fr] = p90_v
            near_black[fr] = nb_v
            near_white[fr] = nw_v
            if "thumbClipped" in rec:
                thumb_clipped_frac[fr] = float(rec["thumbClipped"]) / float(THUMB_PX)
            if "rayTraced" in rec:
                ray_traced[fr] = 1 if rec["rayTraced"] else 0
            replaced_draws[fr] = int(rec.get("replacedMaterialDraws", 0))
            legacy_draws[fr] = int(rec.get("legacyMaterialDraws", 0))
            instances[fr] = int(rec.get("instances", 0))

    alignment = validate_session_alignment(
        spec=spec,
        log_info=log_info,
        n_frames=n_proxy,
        wall_s=wall_s,
        anchor_residuals_ms=anchor_residuals_ms,
    )

    return {
        "spec": spec,
        "log": log_info,
        "alignment": alignment,
        "n_frames": n_proxy,
        "telem_frames": telem_count,
        "wall_s": wall_s,
        "anchor_residuals_ms": anchor_residuals_ms,
        "cam_src": np.array(cam_src_list, dtype="U1"),
        "cam_pos": np.array(cam_pos_list, dtype=np.float64),
        "total_draws": np.array(total_draws_list, dtype=np.int32),
        "scene_draws": scene_draws,
        "handoffs": handoffs,
        "goggles": goggles,
        "rt_active": rt_active,
        "ray_traced": ray_traced,
        "luma_mean": luma_mean,
        "luma_p10": luma_p10,
        "luma_p50": luma_p50,
        "luma_p90": luma_p90,
        "near_black": near_black,
        "near_white": near_white,
        "thumb_clipped_frac": thumb_clipped_frac,
        "replaced_draws": replaced_draws,
        "legacy_draws": legacy_draws,
        "instances": instances,
    }


def validate_session_alignment(
    spec: SessionSpec,
    log_info: dict[str, Any],
    n_frames: int,
    wall_s: np.ndarray,
    anchor_residuals_ms: list[float],
    max_anchor_residual_ms: float = MAX_ANCHOR_RESIDUAL_MS,
) -> dict[str, Any]:
    reasons: list[str] = []
    if n_frames == 0:
        reasons.append("no_proxy_frames")
    if log_info["first_ts"] is None:
        reasons.append(f"pid_{spec.pid}_missing_in_proxy_log")
    max_res = max((abs(x) for x in anchor_residuals_ms), default=0.0)
    if max_res > max_anchor_residual_ms:
        reasons.append(f"anchor_residual_too_high ({max_res:.1f}ms > {max_anchor_residual_ms:.1f}ms)")
    if spec.video_start and n_frames > 0:
        vs = parse_wall_ts(spec.video_start)
        ve = parse_wall_ts(spec.video_end) if spec.video_end else vs
        overlap_s = min(float(wall_s[-1]), ve) - max(float(wall_s[0]), vs)
        if overlap_s <= 0.0:
            reasons.append(
                f"video_window_no_overlap (session={fmt_wall_hms(float(wall_s[0]))}..{fmt_wall_hms(float(wall_s[-1]))}, "
                f"video={spec.video_start}..{spec.video_end})"
            )
    return {
        "aligned": len(reasons) == 0,
        "max_anchor_residual_ms": max_res,
        "reasons": reasons,
    }


def classify_mission_by_pos(pos_cm: np.ndarray) -> str:
    """Identify level from world coordinates (cm) verified from gate & proxy dumps."""
    if not np.all(np.isfinite(pos_cm)):
        return "no-3d-cam"
    x, y, z = float(pos_cm[0]), float(pos_cm[1]), float(pos_cm[2])
    # Paladin hub sits near x in [-1500..1500], y in [-2500..2500], z in [-500..700]
    if -2500 <= x <= 2500 and -3500 <= y <= 3500 and -800 <= z <= 900:
        return "Paladin?"
    # American Consumption sits near x in [-28000..-13000], y in [-30000..-12000]
    if -32000 <= x <= -10000 and -35000 <= y <= -8000:
        return "Consumption"
    return f"pos({x/100:.0f},{y/100:.0f},{z/100:.0f}m)"


def summarize_slice(data: dict[str, Any], mask: np.ndarray, label: str) -> dict[str, Any]:
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return {"label": label, "frames": 0}
    f0, f1 = int(idx[0]), int(idx[-1])
    t0, t1 = float(data["wall_s"][f0]), float(data["wall_s"][f1])
    rt_mask = mask & ((data["ray_traced"] == 1) | ((data["ray_traced"] < 0) & (data["instances"] > 0)))
    use_mask = rt_mask if np.any(rt_mask & np.isfinite(data["luma_mean"])) else (mask & np.isfinite(data["luma_mean"]))

    lm = data["luma_mean"][use_mask]
    p10 = data["luma_p10"][use_mask]
    p90 = data["luma_p90"][use_mask]
    nb = data["near_black"][use_mask]
    nw = data["near_white"][use_mask]
    cl = data["thumb_clipped_frac"][use_mask]

    src = data["cam_src"][mask]
    g_share = float(np.mean(src == "G")) if src.size else 0.0
    p_share = float(np.mean(src == "P")) if src.size else 0.0

    pos_valid = data["cam_pos"][mask]
    pos_valid = pos_valid[np.all(np.isfinite(pos_valid), axis=1)]
    med_pos = np.median(pos_valid, axis=0) if pos_valid.shape[0] > 0 else np.array([np.nan, np.nan, np.nan])

    rep = int(data["replaced_draws"][use_mask].sum())
    leg = int(data["legacy_draws"][use_mask].sum())
    mat_pct = (100.0 * rep / (rep + leg)) if (rep + leg) > 0 else float("nan")

    # Find lightpub 600-frame rows overlapping [f0-300, f1+300]
    lp_rows = [lp for lp in data["log"]["lightpub"] if (f0 - 300) <= lp["frame"] <= (f1 + 599)]
    qual_str = "-"
    bp_str = "-"
    if lp_rows:
        quals = [lp["qualifying"] for lp in lp_rows]
        fills = [lp["room_fill"] for lp in lp_rows]
        domes = [lp["sky_dome"] for lp in lp_rows]
        upds = [lp["updated"] for lp in lp_rows]
        qual_str = (
            f"q={min(quals)}..{max(quals)} fill={min(fills)}..{max(fills)} "
            f"dome={min(domes)}..{max(domes)} upd={max(upds)}"
        )
        ho = sum(lp["handoffs_600"] for lp in lp_rows)
        bp = sum(lp["before_post_600"] for lp in lp_rows)
        bp_str = f"{bp}/{ho} ({100.0*bp/ho:.0f}%)" if ho > 0 else "0/0"

    return {
        "label": label,
        "frames": int(idx.size),
        "f_range": f"{f0}..{f1}",
        "wall_range": f"{fmt_wall_hms(t0)}..{fmt_wall_hms(t1)}",
        "rt_frames": int(np.sum(rt_mask)),
        "mean_avg": float(np.nanmean(lm)) if lm.size else float("nan"),
        "mean_med": float(np.nanmedian(lm)) if lm.size else float("nan"),
        "mean_min": float(np.nanmin(lm)) if lm.size else float("nan"),
        "mean_max": float(np.nanmax(lm)) if lm.size else float("nan"),
        "p10_med": float(np.nanmedian(p10)) if p10.size else float("nan"),
        "p90_med": float(np.nanmedian(p90)) if p90.size else float("nan"),
        "nb_med": float(np.nanmedian(nb)) if nb.size else float("nan"),
        "nb_max": float(np.nanmax(nb)) if nb.size else float("nan"),
        "nw_med": float(np.nanmedian(nw)) if nw.size else float("nan"),
        "nw_max": float(np.nanmax(nw)) if nw.size else float("nan"),
        "clip_gt1pct_frames": int(np.sum(cl > 0.01)) if np.any(np.isfinite(cl)) else -1,
        "clip_max": float(np.nanmax(cl)) if np.any(np.isfinite(cl)) else float("nan"),
        "nw_gt5pct_frames": int(np.sum(nw > 0.05)) if nw.size else 0,
        "nb_gt80pct_frames": int(np.sum(nb > 0.80)) if nb.size else 0,
        "cam_G_pct": 100.0 * g_share,
        "cam_P_pct": 100.0 * p_share,
        "med_pos_cm": med_pos,
        "area_guess": classify_mission_by_pos(med_pos),
        "mat_pct": mat_pct,
        "lightpub": qual_str,
        "before_post": bp_str,
    }


def print_slice_row(s: dict[str, Any]) -> None:
    if s["frames"] == 0:
        print(f"  {s['label']}: NO FRAMES")
        return
    pos = s["med_pos_cm"]
    pos_s = f"({pos[0]/100:.1f},{pos[1]/100:.1f},{pos[2]/100:.1f}m)" if np.all(np.isfinite(pos)) else "(nan)"
    mat_s = f"{s['mat_pct']:.1f}%" if math.isfinite(s["mat_pct"]) else "n/a"
    clip_s = f"{s['clip_max']*100:.1f}%({s['clip_gt1pct_frames']}f>1%)" if s["clip_gt1pct_frames"] >= 0 else "n/a"
    print(
        f"  [{s['label']}] {s['wall_range']} f={s['f_range']} (n={s['frames']}, rt={s['rt_frames']}) "
        f"pos={pos_s} cam(G={s['cam_G_pct']:.0f}%/P={s['cam_P_pct']:.0f}%) mat={mat_s}\n"
        f"      ROI gamma: mean={s['mean_avg']:.4f} med={s['mean_med']:.4f} [{s['mean_min']:.4f}..{s['mean_max']:.4f}] "
        f"p10_med={s['p10_med']:.4f} p90_med={s['p90_med']:.4f} | "
        f"nearBlack(med/max)={s['nb_med']*100:.1f}%/{s['nb_max']*100:.1f}% ({s['nb_gt80pct_frames']}f>80%) | "
        f"nearWhite(med/max)={s['nw_med']*100:.1f}%/{s['nw_max']*100:.1f}% ({s['nw_gt5pct_frames']}f>5%) | "
        f"thumbClipMax={clip_s}\n"
        f"      lights: {s['lightpub']} | handoffBeforePost: {s['before_post']}"
    )


def print_session_report(data: dict[str, Any], window_sec: float = 30.0) -> None:
    spec: SessionSpec = data["spec"]
    al = data["alignment"]
    al_str = "PASS" if al["aligned"] else f"FAIL ({'; '.join(al['reasons'])})"
    print("=" * 110)
    print(
        f"SESSION {spec.key} (pid {spec.pid}) folder={spec.folder.name} ALIGNMENT={al_str}\n"
        f"  proxy_frames={data['n_frames']} telem_frames={data['telem_frames']} "
        f"wall={fmt_wall_hms(data['wall_s'][0])}..{fmt_wall_hms(data['wall_s'][-1])} "
        f"({(data['wall_s'][-1]-data['wall_s'][0])/60.0:.2f} min) "
        f"QPC<->wall anchor max residual={al['max_anchor_residual_ms']:.1f} ms"
    )
    print("  AutoRaytracing & sky dome events:")
    events = [
        (ev["wall_ts"], f"AutoRT -> {ev['state']} ({ev['detail']})")
        for ev in data["log"]["autort_events"]
    ] + [
        (ev["wall_ts"], f"SkyDome radiance=({ev['r']:.5f}, {ev['g']:.5f}, {ev['b']:.5f})")
        for ev in data["log"]["dome_events"]
    ]
    events.sort(key=lambda x: x[0])
    v_start = parse_wall_ts(spec.video_start) if spec.video_start else None
    for ts, desc in events:
        vid_tag = f" [video {fmt_mmss(ts - v_start)}]" if v_start is not None else ""
        print(f"    {fmt_wall_hms(ts)}{vid_tag}: {desc}")

    print(f"\n  Coarse {int(window_sec)}s timeline:")
    t0 = float(data["wall_s"][0])
    t_end = float(data["wall_s"][-1])
    cur = t0
    while cur < t_end:
        nxt = min(cur + window_sec, t_end + 0.001)
        mask = (data["wall_s"] >= cur) & (data["wall_s"] < nxt)
        vid_lbl = f"vid {fmt_mmss(cur - v_start)}..{fmt_mmss(nxt - v_start)}" if v_start is not None else f"m{(cur-t0)/60.0:04.1f}"
        s = summarize_slice(data, mask, vid_lbl)
        if s["frames"] > 0:
            print_slice_row(s)
        cur = nxt


def print_named_spots(sessions: dict[str, dict[str, Any]]) -> None:
    print("\n" + "=" * 110)
    print("COREY NAMED SPOTS & EXTREME STRETCHES (EXACT VIDEO/SESSION ALIGNMENT)")
    print("=" * 110)

    if "nov13" in sessions:
        d13 = sessions["nov13"]
        vs13 = parse_wall_ts(d13["spec"].video_start) if d13["spec"].video_start else float(d13["wall_s"][0])
        print("\n--- Nov 13 Video (21:22:48..21:33:13, pid 44264 in before-20261114-143424, pre-materials-fix) ---")
        nov13_spots = [
            ("Nov13 pre-video Paladin load/walk", -80, 0),
            ("Nov13 00:00..00:55 Paladin hub & cutscene", 0, 55),
            ("Nov13 01:36..02:10 Consumption spawn/fence", 96, 130),
            ("Nov13 02:10..02:42 Consumption BRIGHT hallway", 130, 162),
            ("Nov13 02:42..04:30 Consumption DARK mall entry & rooms", 162, 270),
            ("Nov13 04:30..06:13 Consumption DARK mall combat/flashlight", 270, 373),
            ("Nov13 06:13..08:30 Consumption later rooms", 373, 510),
            ("Nov13 08:30..10:25 Consumption end of video", 510, 625),
        ]
        for title, s0, s1 in nov13_spots:
            m = (d13["wall_s"] >= vs13 + s0) & (d13["wall_s"] < vs13 + s1)
            print_slice_row(summarize_slice(d13, m, title))

    if "nov14_1531" in sessions:
        d14 = sessions["nov14_1531"]
        vs14 = parse_wall_ts(d14["spec"].video_start) if d14["spec"].video_start else float(d14["wall_s"][0])
        print("\n--- Nov 14 Afternoon (14:35:04..15:02:34, pid 23260 in before-20261114-153109, 0% materials mod) ---")
        nov14_spots = [
            ("Nov14 pre-vid 14:36:00..14:38:30 Paladin initial", -839, -689),
            ("Nov14 pre-vid 14:38:30..14:41:30 Benghazi load + OPENING CINEMATIC check", -689, -509),
            ("Nov14 pre-vid 14:41:30..14:45:00 Benghazi street/approach", -509, -299),
            ("Nov14 pre-vid 14:45:00..14:49:59 Benghazi courtyard/entry", -299, 0),
            ("Nov14 vid 00:00..02:00 Benghazi outdoor/alley", 0, 120),
            ("Nov14 vid 06:38..07:41 Benghazi UPSTAIRS (dark, pre-Kobin)", 398, 461),
            ("Nov14 vid 07:41..08:10 Benghazi Kobin cutscene start", 461, 490),
            ("Nov14 vid 08:10..08:58 Benghazi INTERROGATION ROOM (pure white blowout)", 490, 538),
            ("Nov14 vid 09:00..09:40 Benghazi UPSTAIRS post-interrogation combat (dark)", 540, 580),
            ("Nov14 vid 10:02..11:15 Paladin post-mission cutscene", 602, 675),
            ("Nov14 vid 11:15..12:18 Paladin hub walkaround (washed out)", 675, 738),
        ]
        for title, s0, s1 in nov14_spots:
            m = (d14["wall_s"] >= vs14 + s0) & (d14["wall_s"] < vs14 + s1)
            print_slice_row(summarize_slice(d14, m, title))

        rt = d14["ray_traced"] == 1
        white_frames = np.flatnonzero(rt & ((d14["near_white"] > 0.10) | (d14["thumb_clipped_frac"] > 0.01)))
        print(f"\n  [nov14_1531 whiteout/clipped RT frame clusters: total={white_frames.size} frames]")
        print_frame_clusters(d14, white_frames, vs14)

        dark_frames = np.flatnonzero(rt & (d14["luma_mean"] < 0.04))
        print(f"\n  [nov14_1531 ultra-dark RT frame clusters (ROI mean < 0.04): total={dark_frames.size} frames]")
        print_frame_clusters(d14, dark_frames, vs14)

    if "nov14_1855" in sessions:
        d18 = sessions["nov14_1855"]
        print("\n--- Nov 14 Evening (18:55:42..19:19:22, pid 25832 in steam-session-20261114-1855, 78.6% materials mod ACTIVE) ---")
        rt = d18["ray_traced"] == 1
        white_frames = np.flatnonzero(rt & ((d18["near_white"] > 0.05) | (d18["thumb_clipped_frac"] > 0.01) | (d18["luma_mean"] > 0.45)))
        print(f"  [nov14_1855 bright/whiteout RT clusters (ROI mean > 0.45 or nearWhite > 5% or clip > 1%): total={white_frames.size} frames]")
        print_frame_clusters(d18, white_frames, None)

        dark_frames = np.flatnonzero(rt & (d18["luma_mean"] < 0.04))
        print(f"\n  [nov14_1855 ultra-dark RT clusters (ROI mean < 0.04): total={dark_frames.size} frames]")
        print_frame_clusters(d18, dark_frames, None)


def print_frame_clusters(data: dict[str, Any], frames: np.ndarray, v_start: float | None, gap: int = 45) -> None:
    if frames.size == 0:
        print("    (none)")
        return
    splits = np.where(np.diff(frames) > gap)[0] + 1
    groups = np.split(frames, splits)
    for g in groups:
        if g.size < 5:
            continue
        m = np.zeros(data["n_frames"], dtype=bool)
        m[int(g[0]) : int(g[-1]) + 1] = True
        t0 = float(data["wall_s"][int(g[0])])
        lbl = f"vid {fmt_mmss(t0 - v_start)}" if v_start is not None else f"t+{fmt_mmss(t0 - float(data['wall_s'][0]))}"
        print_slice_row(summarize_slice(data, m, f"cluster {lbl}"))


def _read_cutscene_rows(csv_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not csv_path.exists():
        return rows
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            in_cut = r.get("inCutscene", "True") in ("", "True", "true", "1")
            b_str = r.get("activeBrightness") or r.get("brightness") or ""
            if b_str == "":
                continue
            gf = int(r["gameFrame"]) if r.get("gameFrame") else None
            cx = float(r["camX"]) if r.get("camX") else float("nan")
            cy = float(r["camY"]) if r.get("camY") else float("nan")
            cz = float(r["camZ"]) if r.get("camZ") else float("nan")
            rows.append({
                "shot": int(r["shot"]) if r.get("shot") else len(rows),
                "game_frame": gf,
                "in_cutscene": in_cut,
                "cam": (cx, cy, cz),
                "brightness": float(b_str),
                "near_black": float(r.get("activeNearBlack") or r.get("nearBlack") or 0.0),
                "near_white": float(r.get("activeNearWhite") or r.get("nearWhite") or 0.0),
                "full_white": float(r.get("activeFullWhite") or r.get("fullWhite") or 0.0),
            })
    return rows


def analyze_cutscene_dir(
    spot_dir: Path,
    ref_spot_dir: Path | None = None,
    cross_launch_spot_dir: Path | None = None,
    whiteout_mean_limit: float = 0.35,
    whiteout_near_white_limit: float = 0.12,
) -> dict[str, Any] | None:
    """Analyzes <spot>/cinematic/cinematic.csv, checks same-launch telemetry overlap, and flags whiteout."""
    cin_csv = spot_dir / "cinematic" / "cinematic.csv"
    all_rows = _read_cutscene_rows(cin_csv)
    if not all_rows:
        return None
    cut_rows = [r for r in all_rows if r["in_cutscene"]]
    if not cut_rows:
        cut_rows = all_rows

    b = np.array([r["brightness"] for r in cut_rows], dtype=np.float64)
    nw = np.array([r["near_white"] for r in cut_rows], dtype=np.float64)
    fw = np.array([r["full_white"] for r in cut_rows], dtype=np.float64)
    nb = np.array([r["near_black"] for r in cut_rows], dtype=np.float64)
    gfs = [r["game_frame"] for r in cut_rows if r["game_frame"] is not None]
    gf_min = min(gfs) if gfs else None
    gf_max = max(gfs) if gfs else None

    # Check same-launch telemetry.jsonl overlap with cutscene game frames!
    same_launch_telem = spot_dir / "telemetry.jsonl"
    telem_frames_all: list[int] = []
    same_launch_overlap: list[float] = []
    if same_launch_telem.exists():
        with same_launch_telem.open("r", encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                rec = json.loads(ln)
                fr = int(rec.get("frame", -1))
                telem_frames_all.append(fr)
                if gf_min is not None and gf_max is not None and gf_min <= fr <= gf_max:
                    m_v, _, _, _, _, _ = decode_luma_thumb_stats(rec.get("lumaThumb"))
                    if math.isfinite(m_v):
                        same_launch_overlap.append(m_v)

    same_launch_status = "MEASURED" if same_launch_overlap else "NOT_MEASURED"
    ref_summary = None
    if ref_spot_dir is not None:
        ref_rows = [r for r in _read_cutscene_rows(ref_spot_dir / "cinematic" / "cinematic.csv") if r["in_cutscene"]]
        if ref_rows:
            rb = np.array([r["brightness"] for r in ref_rows], dtype=np.float64)
            rnw = np.array([r["near_white"] for r in ref_rows], dtype=np.float64)
            rfw = np.array([r["full_white"] for r in ref_rows], dtype=np.float64)
            ref_summary = {
                "shots": len(ref_rows),
                "mean_brightness": float(rb.mean()),
                "median_brightness": float(np.median(rb)),
                "near_white_mean": float(rnw.mean()),
                "full_white_mean": float(rfw.mean()),
            }

    whiteout = bool(float(b.mean()) > whiteout_mean_limit or float(nw.mean()) > whiteout_near_white_limit)
    if ref_summary is not None and ref_summary["mean_brightness"] >= 0.02:
        ratio = float(b.mean()) / ref_summary["mean_brightness"]
        nw_delta = float(nw.mean()) - ref_summary["near_white_mean"]
        whiteout = bool(ratio > 1.50 or nw_delta > 0.10)
    else:
        ratio = None
        nw_delta = None

    return {
        "spot_dir": str(spot_dir),
        "shots_in_cutscene": len(cut_rows),
        "gf_range": [gf_min, gf_max],
        "mean_brightness": float(b.mean()),
        "median_brightness": float(np.median(b)),
        "max_brightness": float(b.max()),
        "near_black_mean": float(nb.mean()),
        "near_white_mean": float(nw.mean()),
        "near_white_median": float(np.median(nw)),
        "near_white_max": float(nw.max()),
        "full_white_mean": float(fw.mean()),
        "full_white_max": float(fw.max()),
        "same_launch_telemetry_frame_range": (
            [min(telem_frames_all), max(telem_frames_all)] if telem_frames_all else None
        ),
        "same_launch_overlap_frames": len(same_launch_overlap),
        "same_launch_remix_output": same_launch_status,
        "same_launch_luma_mean": float(np.mean(same_launch_overlap)) if same_launch_overlap else None,
        "reference": ref_summary,
        "mean_ratio_vs_reference": ratio,
        "near_white_delta_vs_reference": nw_delta,
        "whiteout": whiteout,
        "all_rows": all_rows,
        "cross_launch_spot_dir": str(cross_launch_spot_dir) if cross_launch_spot_dir else None,
    }


def print_benghazi_cinematic_comparison(
    spot_dir: Path,
    ref_spot_dir: Path | None = None,
    cross_launch_spot_dir: Path | None = None,
) -> dict[str, Any] | None:
    res = analyze_cutscene_dir(spot_dir, ref_spot_dir=ref_spot_dir, cross_launch_spot_dir=cross_launch_spot_dir)
    if res is None:
        return None
    print("\n" + "=" * 110)
    print("BENGHAZI OPENING CINEMATIC: D3D9 SCREEN (cinematic.csv) vs REFERENCE & TELEMETRY AUDIT")
    print("=" * 110)
    print(
        f"  Cutscene (gf={res['gf_range'][0]}..{res['gf_range'][1]}, n={res['shots_in_cutscene']}, "
        f"whiteout={res['whiteout']}): "
        f"screen mean={res['mean_brightness']:.4f} med={res['median_brightness']:.4f} max={res['max_brightness']:.4f} | "
        f"nearWhite(>=230) mean={res['near_white_mean']*100:.1f}% med={res['near_white_median']*100:.1f}% "
        f"max={res['near_white_max']*100:.1f}% | "
        f"fullWhite(>=250) mean={res['full_white_mean']*100:.1f}% max={res['full_white_max']*100:.1f}%"
    )
    if res["reference"] is not None:
        rf = res["reference"]
        print(
            f"  No-Remix reference (n={rf['shots']}, not frame-aligned): "
            f"mean={rf['mean_brightness']:.4f} med={rf['median_brightness']:.4f} | "
            f"nearWhite mean={rf['near_white_mean']*100:.1f}% | fullWhite mean={rf['full_white_mean']*100:.1f}% "
            f"=> ratio={res['mean_ratio_vs_reference']:.2f}x, nearWhite_delta=+{res['near_white_delta_vs_reference']:.3f}"
        )
    print(
        f"  Same-launch Remix telemetry during cutscene: {res['same_launch_remix_output']} "
        f"(telemetry frame range={res['same_launch_telemetry_frame_range']}, "
        f"overlap={res['same_launch_overlap_frames']} frames)"
    )

    rows = res["all_rows"]
    for label, lo, hi in [
        ("Shot 1 fade-in (gf 4780..4800)", 4780, 4800),
        ("Shot 1 alley guards running (gf 4808..4848)", 4808, 4848),
        ("Shot 2 Sam approach (gf 4857..4986)", 4857, 4986),
        ("Post-cutscene gameplay (gf 4987..5018)", 4987, 5018),
    ]:
        sub = [r for r in rows if r["game_frame"] is not None and lo <= r["game_frame"] <= hi]
        if not sub:
            continue
        b = np.array([r["brightness"] for r in sub], dtype=np.float64)
        nw = np.array([r["near_white"] for r in sub], dtype=np.float64)
        fw = np.array([r["full_white"] for r in sub], dtype=np.float64)
        print(
            f"  [{label}] shots={len(sub)} screen mean={b.mean():.4f} med={np.median(b):.4f} "
            f"[{b.min():.4f}..{b.max():.4f}] "
            f"nearWhite mean={nw.mean()*100:.1f}% med={np.median(nw)*100:.1f}% (max {nw.max()*100:.1f}%) "
            f"fullWhite mean={fw.mean()*100:.1f}% med={np.median(fw)*100:.1f}% (max {fw.max()*100:.1f}%)"
        )

    print(
        "  NOTE (caveat & hypothesis): When same-launch cutscene telemetry overlap is 0 frames, any comparison "
        "against ben_upstairs telemetry is cross-launch and position-matched for only the last ~37 frames of shot 2 "
        "(0 frames for shot 1). Unskipped post-handoff D3D9 draws in play-setup\\unified-build\\draw_filter.cpp:454-455 "
        "remain an UNPROVEN HYPOTHESIS until same-launch cutscene telemetry and draw captures are recorded."
    )
    return res


def _write_synthetic_session(
    folder: Path,
    pid: int,
    base_wall_str: str,
    n_frames: int = 20,
    anchor_jitter_s: float = 0.0,
) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    base_ts = parse_wall_ts(base_wall_str)
    # Write proxy log with two lightpub anchors (frame 0 and frame 10)
    t0_dt = dt.datetime.fromtimestamp(base_ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    t10_dt = dt.datetime.fromtimestamp(base_ts + 1.0 + anchor_jitter_s).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    (folder / "d3d9_proxy.log").write_text(
        f"{t0_dt} [pid {pid}] lightpub: frame 0 qualifying 80 drawn 80 (room fill 3, sky dome 1) created 80 updated 0 tracked 80\n"
        f"{t10_dt} [pid {pid}] lightpub: frame 10 qualifying 80 drawn 80 (room fill 3, sky dome 1) created 0 updated 0 tracked 80\n",
        encoding="utf-8",
    )
    # Write proxy frames (100 ms per frame -> 10 frames = 1.0 s)
    with (folder / "d3d9_proxy_frames.csv").open("w", encoding="utf-8", newline="") as f:
        for fr in range(n_frames):
            f.write(f"{fr},{fr * 100.0:.1f},16.0,1.0,1200,1,G:22:0:100.0:200.0:50.0:1.0:0.0:0.0:0.0\n")
    img = np.full((THUMB_H, THUMB_W), 64, dtype=np.uint8)
    b64 = base64.b64encode(img.tobytes()).decode("ascii")
    with (folder / "rtx_telemetry.jsonl").open("w", encoding="utf-8", newline="") as f:
        for fr in range(n_frames):
            f.write(json.dumps({"frame": fr, "rayTraced": True, "lumaThumb": b64, "thumbClipped": 0}) + "\n")
    return folder


def self_test() -> None:
    # 1. Synthetic 128x72 lumaThumb test: border = 255, interior = 51 (0.2000)
    img = np.full((THUMB_H, THUMB_W), 255, dtype=np.uint8)
    ys, xs = thumb_roi_slices()
    img[ys, xs] = 51
    b64 = base64.b64encode(img.tobytes()).decode("ascii")
    mean_v, p10_v, p50_v, p90_v, nb_v, nw_v = decode_luma_thumb_stats(b64)
    assert abs(mean_v - 0.2) < 1e-5, f"expected 0.2, got {mean_v}"
    assert nw_v == 0.0, f"border 255 leaked into ROI: nw={nw_v}"
    assert nb_v == 0.0
    assert abs(p10_v - p90_v) < 1e-6

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # 2. Valid aligned session -> alignment['aligned'] is True
        ok_folder = _write_synthetic_session(tmp_path / "ok_sess", pid=1111, base_wall_str="2026-11-14 14:50:00")
        ok_data = load_aligned_session(
            SessionSpec("ok", ok_folder, 1111, "2026-11-14 14:49:59", "2026-11-14 14:50:05")
        )
        assert ok_data["alignment"]["aligned"] is True, ok_data["alignment"]

        # 3. Fake misaligned sessions MUST fail alignment validation:
        # 3a. Video window from wrong day/session (like mapping Nov 13 21:22 video onto Nov 12 23:30 logs)
        wrong_vid_data = load_aligned_session(
            SessionSpec("wrong_vid", ok_folder, 1111, "2026-11-13 21:22:48", "2026-11-13 21:33:13")
        )
        assert wrong_vid_data["alignment"]["aligned"] is False, wrong_vid_data["alignment"]
        assert any("video_window_no_overlap" in r for r in wrong_vid_data["alignment"]["reasons"])

        # 3b. Wrong PID missing from d3d9_proxy.log
        wrong_pid_data = load_aligned_session(
            SessionSpec("wrong_pid", ok_folder, 9999, "2026-11-14 14:49:59", "2026-11-14 14:50:05")
        )
        assert wrong_pid_data["alignment"]["aligned"] is False, wrong_pid_data["alignment"]
        assert any("missing_in_proxy_log" in r for r in wrong_pid_data["alignment"]["reasons"])

        # 3c. Broken QPC <-> wall-clock anchor residual (> 500 ms drift)
        bad_qpc_folder = _write_synthetic_session(
            tmp_path / "bad_qpc", pid=2222, base_wall_str="2026-11-14 14:50:00", anchor_jitter_s=4.0
        )
        bad_qpc_data = load_aligned_session(SessionSpec("bad_qpc", bad_qpc_folder, 2222, None, None))
        assert bad_qpc_data["alignment"]["aligned"] is False, bad_qpc_data["alignment"]
        assert any("anchor_residual_too_high" in r for r in bad_qpc_data["alignment"]["reasons"])

        # 4. Fake normal-brightness cutscene vs whiteout cutscene (plus NOT_MEASURED same-launch telemetry check)
        ref_spot = tmp_path / "ref_ben"
        (ref_spot / "cinematic").mkdir(parents=True)
        (ref_spot / "cinematic" / "cinematic.csv").write_text(
            "shot,gameFrame,camX,camY,camZ,brightness,nearBlack,nearWhite,fullWhite,inCutscene\n"
            + "".join(f"{i},{3340+i},3000,4000,-200,0.150,0.215,0.012,0.007,True\n" for i in range(10)),
            encoding="utf-8",
        )
        normal_spot = tmp_path / "normal_ben"
        (normal_spot / "cinematic").mkdir(parents=True)
        (normal_spot / "cinematic" / "cinematic.csv").write_text(
            "shot,gameFrame,camX,camY,camZ,brightness,nearBlack,nearWhite,fullWhite,inCutscene\n"
            + "".join(f"{i},{4800+i},3000,4000,-200,0.175,0.180,0.022,0.009,True\n" for i in range(10)),
            encoding="utf-8",
        )
        normal_cut = analyze_cutscene_dir(normal_spot, ref_spot_dir=ref_spot)
        assert normal_cut is not None and normal_cut["whiteout"] is False, normal_cut

        white_spot = tmp_path / "white_ben"
        (white_spot / "cinematic").mkdir(parents=True)
        (white_spot / "cinematic" / "cinematic.csv").write_text(
            "shot,gameFrame,camX,camY,camZ,brightness,nearBlack,nearWhite,fullWhite,inCutscene\n"
            + "".join(f"{i},{4800+i},3000,4000,-200,0.666,0.097,0.370,0.270,True\n" for i in range(10)),
            encoding="utf-8",
        )
        # Telemetry starting AFTER cutscene (frames 5033..5040) -> same_launch_remix_output must be NOT_MEASURED
        (white_spot / "telemetry.jsonl").write_text(
            "".join(json.dumps({"frame": 5033 + i, "rayTraced": True, "lumaThumb": b64}) + "\n" for i in range(5)),
            encoding="utf-8",
        )
        white_cut = analyze_cutscene_dir(white_spot, ref_spot_dir=ref_spot)
        assert white_cut is not None and white_cut["whiteout"] is True, white_cut
        assert white_cut["same_launch_remix_output"] == "NOT_MEASURED", white_cut

    print("SELF-TEST PASSED: align_sessions.py")


def _parse_session_arg(raw: str) -> SessionSpec:
    """Parses key=folder:pid[|video_start|video_end] from CLI."""
    if "=" not in raw or ":" not in raw:
        raise argparse.ArgumentTypeError("expected key=folder:pid[|video_start|video_end]")
    key, rest = raw.split("=", 1)
    parts = rest.split("|")
    folder_pid = parts[0]
    folder_str, pid_str = folder_pid.rsplit(":", 1)
    v_start = parts[1] if len(parts) > 1 and parts[1] else None
    v_end = parts[2] if len(parts) > 2 and parts[2] else None
    return SessionSpec(key=key, folder=Path(folder_str), pid=int(pid_str), video_start=v_start, video_end=v_end)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Workspace root directory")
    ap.add_argument("--session", default="all",
                    help="Session key to run (nov13, nov14_1531, nov14_1855, all, cinematic, or custom key)")
    ap.add_argument("--session-spec", action="append", default=[], type=_parse_session_arg,
                    help="Override or add session: key=folder:pid[|video_start|video_end]")
    ap.add_argument("--cinematic-spot-dir", type=Path, default=None,
                    help="Spot directory containing cinematic/cinematic.csv")
    ap.add_argument("--reference-cinematic-dir", type=Path, default=None,
                    help="No-Remix reference spot directory containing cinematic/cinematic.csv")
    ap.add_argument("--window-sec", type=float, default=30.0)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return 0

    sessions_map = build_default_sessions(args.root)
    for custom in args.session_spec:
        sessions_map[custom.key] = custom

    cin_spot = args.cinematic_spot_dir or (
        args.root / r"experiments\remix-gate\20261115-214203\ben_interrogation"
    )
    ref_cin_spot = args.reference_cinematic_dir or (
        args.root / r"experiments\remix-gate\reference-20261116-000454\ben_interrogation"
    )

    if args.session == "cinematic":
        print_benghazi_cinematic_comparison(cin_spot, ref_spot_dir=ref_cin_spot)
        return 0

    keys = list(sessions_map.keys()) if args.session == "all" else [args.session]
    loaded: dict[str, dict[str, Any]] = {}
    for k in keys:
        loaded[k] = load_aligned_session(sessions_map[k])
        print_session_report(loaded[k], window_sec=args.window_sec)

    print_named_spots(loaded)
    if args.session == "all":
        print_benghazi_cinematic_comparison(cin_spot, ref_spot_dir=ref_cin_spot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
