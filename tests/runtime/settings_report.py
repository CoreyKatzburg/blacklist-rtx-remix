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
import display_state  # noqa: E402
import gate  # noqa: E402

WINDOW = "bench_y0_p0"


def med(vals, q=50):
    vals = [float(v) for v in vals if isinstance(v, (int, float))]
    return round(float(np.percentile(vals, q)), 2) if vals else None


def nvidia_smi(path: str) -> dict:
    """Median and worst used/free MiB from `nvidia-smi --format=csv` rows ("6012 MiB")."""
    if not os.path.isfile(path):
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


def window_numbers(folder: str, spot: dict, w: dict, smi: str, prefix: str) -> dict:
    """Numbers for one bench window; keys get `prefix` (for the end-of-run revisit, "endFrameMs" and so on)."""
    out: dict = {"gameFrames": w.get("gameFrames")}
    pframes = gate.read_csv_frames(os.path.join(folder, spot.get("proxyFrames") or "proxy_frames.csv"), 7)
    rows = [pframes[f] for f in gate.frames_in(w, "proxy") if f in pframes]
    interval = [float(r[2]) for r in rows if len(r) > 3]
    out.update(proxyFrames=len(interval), frameMs=med(interval), frameMsP95=med(interval, 95),
               fps=round(1000.0 / med(interval), 1) if interval else None)
    tel, _ = gate.read_telemetry(os.path.join(folder, spot.get("telemetry") or "telemetry.jsonl"))
    recs = [tel[f] for f in gate.frames_in(w, "telemetry") if f in tel]
    out["telemetryFrames"] = len(recs)
    for key in ("frameTimeMs", "gpuTimeMs", "vramUsedMB", "vramFreeMB", "textureMemoryMB", "textureBudgetMB"):
        out[key] = med([r.get(key) for r in recs])
    # Ray Reconstruction's own GPU time: ~11.1 or ~13.3 ms per launch at the same settings (worker C, Nov 16)
    out["rrMs"] = med([(r.get("passMs") or {}).get("InjectRTX/Ray Reconstruction") for r in recs])
    out["texturesDemotedMax"] = max((r["texturesDemoted"] for r in recs if isinstance(r.get("texturesDemoted"), (int, float))), default=None)
    out.update(nvidia_smi(os.path.join(folder, smi)))
    return {(prefix + k[0].upper() + k[1:]) if prefix else k: v for k, v in out.items()}


def gpu_run(path: str) -> dict:
    """Whole-launch nvidia-smi log (gpu_run.csv, once a second, started by verify_runtime.ps1 before the game): median
    graphics clock, highest temperature, median power and every non-idle clock event (throttle) reason seen."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        rows = list(csv.reader(f, skipinitialspace=True))
    if not rows:
        return {}
    head = [h.strip() for h in rows[0]]
    col = lambda name: next((i for i, h in enumerate(head) if h.startswith(name)), None)
    num = lambda r, i: float(r[i].split()[0]) if i is not None and i < len(r) and r[i].strip()[:1].isdigit() else None
    gr, temp, power, reasons = col("clocks.current.graphics"), col("temperature.gpu"), col("power.draw"), col("clocks_event_reasons.active")
    body = [r for r in rows[1:] if len(r) == len(head)]
    seen = sorted({r[reasons].strip() for r in body if reasons is not None and r[reasons].strip() not in ("0x0000000000000000", "0x0000000000000001")})
    return {"runSmiSamples": len(body), "runGpuMHzMed": med([num(r, gr) for r in body]),
            "runTempMax": max((v for v in (num(r, temp) for r in body) if v is not None), default=None),
            "runPowerWMed": med([num(r, power) for r in body]), "runThrottle": " ".join(seen) or "none"}


def variant_row(folder: str) -> dict:
    row: dict = {"variant": os.path.basename(folder), "displayState": display_state.launch_state(folder)["class"]}
    vpath = os.path.join(folder, "variant.json")
    if os.path.exists(vpath):
        v = gate.load_json(vpath)
        row["change"] = ", ".join(f"{k}={val}" for part in ("rtx", "proxy") for k, val in (v.get(part) or {}).items()) or "(none)"
        row["rtxLoaded"] = bool(v.get("rtxLoaded"))
        row["proxyLoaded"] = v.get("proxyLoaded", True)   # True, False, or "not proven" (no log line for a key)
        rr = [line.split("created:", 1)[1].strip() for line in v.get("rrCreated") or [] if "created:" in line]
        row["rr"] = " / ".join(dict.fromkeys(rr)) or None
    row.update(gpu_run(os.path.join(folder, "gpu_run.csv")))
    spath = os.path.join(folder, "spot.json")
    spot = gate.load_json(spath) if os.path.exists(spath) else {}
    if spot.get("error"):
        row["error"] = spot["error"]
    # Live steps (one launch, a texture budget change per step): their own rows in the live table.
    row["live"] = [dict(window_numbers(folder, spot, w, "", ""), step=w["name"], change=w.get("liveRtx") or {})
                   for w in spot.get("windows", []) if w.get("name", "").startswith("live_step")]
    for prefix, name, smi in (("", WINDOW, "gpu_mem.csv"), ("end", WINDOW + "_end", "gpu_mem_end.csv")):
        w = next((w for w in spot.get("windows", []) if w.get("name") == name), None)
        if not w:
            if not prefix:
                row.setdefault("error", f"no {WINDOW} window")
                return row
            continue
        row.update(window_numbers(folder, spot, w, smi, prefix))
    return row


def report(run: str) -> tuple[list[dict], str]:
    manifest = gate.load_json(os.path.join(run, "manifest.json"))
    names = manifest.get("variants") or sorted(d for d in os.listdir(run) if os.path.isdir(os.path.join(run, d)))
    rows = [variant_row(os.path.join(run, n)) for n in names if os.path.isdir(os.path.join(run, n))]
    # Every variant with no change of its own is a base launch; the first is the reference, and the spread between
    # them (worker C runs the base first and last) is the run-to-run noise a setting's change has to beat.
    bases = [r for r in rows if r["variant"].endswith("_base") or "_base_" in r["variant"]]
    base = next((r for r in bases if r.get("frameMs")), next((r for r in rows if r.get("frameMs")), None))
    # Each variant is its own launch: a frame time is compared with the base only in the same known display state
    # (display_state.py; Nov 17: plug off ran about 12 % faster per frame than plug on).
    same = (lambda r: base is not None and r["displayState"] == base["displayState"] != display_state.UNKNOWN)
    for r in rows:
        if base and r.get("frameMs") and r is not base:
            r["frameMsVsBase"] = round(r["frameMs"] - base["frameMs"], 2) if same(r) else "NOT COMPARABLE"
        if r.get("frameMs") and r.get("endFrameMs"):
            r["endVsStartMs"] = round(r["endFrameMs"] - r["frameMs"], 2)
    cols = [("variant", "variant"), ("change", "change"), ("rtxLoaded", "loaded"), ("proxyLoaded", "proxy keys"),
            ("displayState", "display state"),
            ("frameMs", "frame ms"), ("frameMsP95", "p95 ms"), ("frameMsVsBase", "vs base"), ("fps", "FPS"),
            ("gpuTimeMs", "GPU ms"), ("rrMs", "RR ms"), ("endRrMs", "end RR ms"), ("rr", "RR created"),
            ("runGpuMHzMed", "GPU MHz"), ("runTempMax", "temp max"), ("runPowerWMed", "power W"), ("runThrottle", "clock events"),
            ("endFrameMs", "end frame ms"), ("endVsStartMs", "end - start ms"), ("endGpuTimeMs", "end GPU ms"),
            ("vramUsedMB", "Remix VRAM MB"), ("vramFreeMB", "Remix VRAM free MB"), ("endVramFreeMB", "end VRAM free MB"),
            ("textureMemoryMB", "tex MB"), ("textureBudgetMB", "tex budget MB"),
            ("texturesDemotedMax", "demoted"), ("smiUsedMedMiB", "smi used MiB"), ("smiFreeMinMiB", "smi free min MiB"),
            ("endSmiFreeMinMiB", "end smi free min MiB"), ("proxyFrames", "frames"), ("telemetryFrames", "telemetry frames")]
    lines = [f"# Settings sweep {os.path.basename(run)}", "",
             "Frame ms = median time between Presents in the proxy frame log over the bench window (every variant); "
             "GPU ms and the Remix memory columns come from Remix telemetry (empty when it is off). smi = nvidia-smi, "
             "whole GPU. 'loaded' = remix-dxvk.log lists each changed rtx key with the new value. 'end' columns = the "
             "same pose measured again after one look round the room, at the end of the launch. 'vs base' and the "
             "run-to-run noise use only variants in the base's display state (plug on / plug off, display_state.py); "
             "any other reads NOT COMPARABLE.", "",
             "| " + " | ".join(c[1] for c in cols) + " |", "|" + "---|" * len(cols)]
    for r in rows:
        lines.append("| " + " | ".join("" if r.get(k) is None else str(r.get(k)) for k, _ in cols) + " |")
    timed = [r for r in bases if r.get("frameMs") and same(r)]
    if len(timed) >= 2:
        ms = [r["frameMs"] for r in timed]
        noise = round(max(ms) - min(ms), 2)
        for r in rows:
            r["baseNoiseMs"] = noise
        lines += ["", f"Run-to-run noise: the {len(timed)} base launches ({', '.join(r['variant'] for r in timed)}) "
                      f"differ by {noise} ms in median frame time ({', '.join(str(m) for m in ms)})."]
    live = [(r["variant"], s) for r in rows for s in r.get("live") or []]
    if live:
        lines += ["", "Live steps (one launch, the change applied while holding the pose; it took effect only if "
                      "'tex budget MB' moves):", "",
                  "| variant | step | change | frame ms | GPU ms | RR ms | tex budget MB | tex MB | Remix VRAM free MB | frames |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for name, s in live:
            change = ", ".join(f"{k}={v}" for k, v in s["change"].items()) or "(as launched)"
            lines.append(f"| {name} | {s['step']} | {change} | " + " | ".join(
                "" if s.get(k) is None else str(s.get(k)) for k in
                ("frameMs", "gpuTimeMs", "rrMs", "textureBudgetMB", "textureMemoryMB", "vramFreeMB", "proxyFrames")) + " |")
    problems = [(r["variant"], r.get("error") or "rtx keys not loaded as asked") for r in rows if r.get("error") or r.get("rtxLoaded") is False]
    problems += [(r["variant"], f"proxy keys {'not loaded as asked' if r['proxyLoaded'] is False else 'not proven (no proxy.log line)'}")
                 for r in rows if r.get("proxyLoaded") not in (True, None)]
    if problems:
        lines += ["", "Problems:"] + [f"- {v}: {why}" for v, why in problems]
    return rows, "\n".join(lines) + "\n"


def main() -> int:
    run = sys.argv[1]
    rows, md = report(run)
    gate.save_json(os.path.join(run, "settings.json"), rows)
    gate.save_text(os.path.join(run, "settings.md"), md)
    print(md)
    return 1 if any(r.get("error") or r.get("rtxLoaded") is False or r.get("proxyLoaded") is False for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
