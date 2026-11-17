"""Numbers for the switch test of one spot (drive.py "switch" windows): per variant, against "base".

  switch_report.py <spot folder>     writes switch.json and switch.md next to spot.json, prints switch.md

Per variant: mean brightness, share of near-black (< 8 of 255) and full-white (>= 250) pixels, from the
telemetry thumbnails when every frame has one, else the screen capture; median frame time and share of ray-traced
frames from telemetry; and from the proxy's light dump the lights in the game's list, how many the proxy wants and
how many it sent to Remix, and the flares. For the light switch window: seconds until the screen stays within 10% of
its final brightness (fadeSeconds).
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate  # noqa: E402

NEAR_BLACK, FULL_WHITE = 8, 250


def picture_stats(frames: np.ndarray | None, masked: bool = False) -> dict:
    """masked: gate.window_frames set the NVIDIA overlay box to 0 in these screen-capture frames; the box is left out
    of the shares and the mean instead of counting as near black."""
    if frames is None or not len(frames):
        return {}
    if masked:
        keep = np.ones(frames.shape[1:], bool)
        x0, y0, x1, y1 = gate.overlay_mask.box_at(frames.shape[2], frames.shape[1])
        keep[y0:y1 + 1, x0:x1 + 1] = False
        frames = frames[:, keep]
    return {"brightness": round(float(frames.mean()) / 255.0, 4),
            "nearBlack": round(float((frames < NEAR_BLACK).mean()), 4),
            "fullWhite": round(float((frames >= FULL_WHITE).mean()), 4)}


def light_stats(path: str | None) -> dict:
    if not path or not os.path.exists(path):
        return {}
    lights = wanted = sent = flares = 0
    with open(path, encoding="latin-1") as f:
        for line in f:
            kind = line.split("\t", 1)[0]
            if kind == "light":
                lights += 1
                wanted += "\twanted=1" in line
                sent += "\ttracked=1" in line
            elif kind.startswith("flare"):
                flares += 1
    return {"lights": lights, "lightsWanted": wanted, "lightsSent": sent, "flares": flares}


def fade_seconds(means: np.ndarray, fps: float) -> float | None:
    """Seconds from the first frame until the brightness stays within 10% of the start-to-end change of its final
    value. None when the picture did not change by at least 2 of 255 (the switch did nothing measurable)."""
    if len(means) < 2:
        return None
    start, end = float(np.median(means[:3])), float(np.median(means[-int(fps):]))
    if abs(start - end) < 2.0:
        return None
    outside = np.nonzero(np.abs(means - end) > 0.1 * abs(start - end))[0]
    return round(float(outside[-1] + 1) / fps, 2) if len(outside) else 0.0


def report(folder: str) -> dict:
    spot = gate.load_json(os.path.join(folder, "spot.json"))
    spot["_dir"] = folder
    telemetry, _ = gate.read_telemetry(gate.spot_path(spot, spot.get("telemetry")))
    spot["_data"] = {"telemetry": telemetry}
    rows = []
    for w in spot["windows"]:
        if w["kind"] != "switch":
            continue
        frames, source = gate.window_frames(spot, w)
        tele = [telemetry[f] for f in gate.frames_in(w, "telemetry") if f in telemetry]
        row = {"variant": w["switch"]["name"], "change": w["switch"], "pictureSource": source,
               **picture_stats(frames, "overlay box masked" in (source or ""))}
        if tele:
            row["frameMs"] = round(float(np.median([t.get("frameTimeMs", np.nan) for t in tele])), 2)
            row["rayTraced"] = round(float(np.mean([bool(t.get("rayTraced")) for t in tele])), 3)
        row.update(light_stats(os.path.join(folder, "switch", w["lights"]) if w.get("lights") else None))
        if row["variant"] == "light_switch" and w.get("frames"):
            shot = gate.read_gray(os.path.join(folder, w["frames"]), *w["frameSize"])
            row["fadeSeconds"] = fade_seconds(shot.reshape(len(shot), -1).mean(axis=1), 30.0)
        rows.append(row)
    base = next((r for r in rows if r["variant"] == "base"), None)
    for r in rows:
        if base and r is not base:
            r["vsBase"] = {k: round(r[k] - base[k], 4) for k in ("brightness", "nearBlack", "fullWhite", "frameMs",
                                                                  "lights", "lightsSent", "flares") if k in r and k in base}
    out = {"spot": spot.get("name"), "variants": rows}
    gate.save_json(os.path.join(folder, "switch.json"), out)
    gate.save_text(os.path.join(folder, "switch.md"), markdown(out))
    return out


def markdown(out: dict) -> str:
    cols = ["variant", "brightness", "nearBlack", "fullWhite", "frameMs", "rayTraced", "lights", "lightsSent", "flares", "fadeSeconds"]
    lines = [f"# Switch test: {out['spot']}", "", "Each row changes one thing against `base`. Brightness is 0 to 1.", "",
             "| " + " | ".join(cols) + " | change in brightness vs base |", "|" + " --- |" * (len(cols) + 1)]
    for r in out["variants"]:
        cells = ["" if r.get(c) is None else str(r.get(c)) for c in cols]
        lines.append("| " + " | ".join(cells) + f" | {r.get('vsBase', {}).get('brightness', '')} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(markdown(report(sys.argv[1])))
