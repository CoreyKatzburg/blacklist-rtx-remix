"""Numbers for a settings sweep (verify_runtime.ps1 -Variants): one row per variant, from its "bench_y0_p0" window.

  settings_report.py <sweep run folder>     writes settings.json and settings.md in it, prints settings.md

Per variant: whether remix-dxvk.log lists the changed rtx keys with the new values (variant.json); frame time from the
proxy frame log (interval between Presents, present in every variant, also with Remix telemetry off) and from Remix
telemetry (frameTimeMs, gpuTimeMs, video memory and texture budget fields) when it is on; video memory from nvidia-smi
(gpu_mem.csv, sampled every 200 ms during the window). Frame-time changes are against 01_base (or the first variant).
"""
from __future__ import annotations

import csv
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gate  # noqa: E402

WINDOW = "bench_y0_p0"


def med(vals, q=50):
    vals = [float(v) for v in vals if isinstance(v, (int, float))]
    return round(float(np.percentile(vals, q)), 2) if vals else None


def nvidia_smi(path: str) -> dict:
    """Median and worst used/free MiB from `nvidia-smi --format=csv` rows ("6012 MiB")."""
    if not os.path.exists(path):
        return {}
    used, free, total = [], [], []
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        for row in csv.reader(f, skipinitialspace=True):
            if len(row) < 4 or not row[1].strip()[:1].isdigit():
                continue   # header, or a cut-off last line
            try:
                total.append(float(row[1].split()[0])); used.append(float(row[2].split()[0])); free.append(float(row[3].split()[0]))
            except (ValueError, IndexError):
                continue
    if not used:
        return {"smiSamples": 0}
    return {"smiSamples": len(used), "smiTotalMiB": total[0], "smiUsedMedMiB": med(used), "smiUsedMaxMiB": max(used),
            "smiFreeMedMiB": med(free), "smiFreeMinMiB": min(free)}


def variant_row(folder: str) -> dict:
    row: dict = {"variant": os.path.basename(folder)}
    vpath = os.path.join(folder, "variant.json")
    if os.path.exists(vpath):
        v = gate.load_json(vpath)
        row["change"] = ", ".join(f"{k}={val}" for part in ("rtx", "proxy") for k, val in (v.get(part) or {}).items()) or "(none)"
        row["rtxLoaded"] = bool(v.get("rtxLoaded"))
    spath = os.path.join(folder, "spot.json")
    spot = gate.load_json(spath) if os.path.exists(spath) else {}
    if spot.get("error"):
        row["error"] = spot["error"]
    w = next((w for w in spot.get("windows", []) if w.get("name") == WINDOW), None)
    if not w:
        row.setdefault("error", f"no {WINDOW} window")
        return row
    row["gameFrames"] = w.get("gameFrames")
    pframes = gate.read_csv_frames(os.path.join(folder, spot.get("proxyFrames") or "proxy_frames.csv"), 7)
    rows = [pframes[f] for f in gate.frames_in(w, "proxy") if f in pframes]
    interval = [float(r[2]) for r in rows if len(r) > 3]
    row.update(proxyFrames=len(interval), frameMs=med(interval), frameMsP95=med(interval, 95),
               fps=round(1000.0 / med(interval), 1) if interval else None)
    tel, _ = gate.read_telemetry(os.path.join(folder, spot.get("telemetry") or "telemetry.jsonl"))
    recs = [tel[f] for f in gate.frames_in(w, "telemetry") if f in tel]
    row["telemetryFrames"] = len(recs)
    for key in ("frameTimeMs", "gpuTimeMs", "vramUsedMB", "textureMemoryMB", "textureBudgetMB"):
        row[key] = med([r.get(key) for r in recs])
    row["texturesDemotedMax"] = max((r["texturesDemoted"] for r in recs if isinstance(r.get("texturesDemoted"), (int, float))), default=None)
    row.update(nvidia_smi(os.path.join(folder, "gpu_mem.csv")))
    return row


def report(run: str) -> tuple[list[dict], str]:
    manifest = gate.load_json(os.path.join(run, "manifest.json"))
    names = manifest.get("variants") or sorted(d for d in os.listdir(run) if os.path.isdir(os.path.join(run, d)))
    rows = [variant_row(os.path.join(run, n)) for n in names if os.path.isdir(os.path.join(run, n))]
    base = next((r for r in rows if r["variant"].endswith("_base") and r.get("frameMs")), next((r for r in rows if r.get("frameMs")), None))
    for r in rows:
        if base and r.get("frameMs") and r is not base:
            r["frameMsVsBase"] = round(r["frameMs"] - base["frameMs"], 2)
    cols = [("variant", "variant"), ("change", "change"), ("rtxLoaded", "loaded"), ("frameMs", "frame ms"),
            ("frameMsP95", "p95 ms"), ("frameMsVsBase", "vs base"), ("fps", "FPS"), ("gpuTimeMs", "GPU ms"),
            ("vramUsedMB", "Remix VRAM MB"), ("textureMemoryMB", "tex MB"), ("textureBudgetMB", "tex budget MB"),
            ("texturesDemotedMax", "demoted"), ("smiUsedMedMiB", "smi used MiB"), ("smiFreeMinMiB", "smi free min MiB"),
            ("proxyFrames", "frames"), ("telemetryFrames", "telemetry frames")]
    lines = [f"# Settings sweep {os.path.basename(run)}", "",
             "Frame ms = median time between Presents in the proxy frame log over the bench window (every variant); "
             "GPU ms and the Remix memory columns come from Remix telemetry (empty when it is off). smi = nvidia-smi, "
             "whole GPU. 'loaded' = remix-dxvk.log lists each changed rtx key with the new value.", "",
             "| " + " | ".join(c[1] for c in cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        lines.append("| " + " | ".join("" if r.get(k) is None else str(r.get(k)) for k, _ in cols) + " |")
    bad = [r for r in rows if r.get("error") or r.get("rtxLoaded") is False]
    if bad:
        lines += ["", "Problems:"] + [f"- {r['variant']}: {r.get('error') or 'rtx keys not loaded as asked'}" for r in bad]
    return rows, "\n".join(lines) + "\n"


def main() -> int:
    run = sys.argv[1]
    rows, md = report(run)
    gate.save_json(os.path.join(run, "settings.json"), rows)
    gate.save_text(os.path.join(run, "settings.md"), md)
    print(md)
    return 1 if any(r.get("error") or r.get("rtxLoaded") is False for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
