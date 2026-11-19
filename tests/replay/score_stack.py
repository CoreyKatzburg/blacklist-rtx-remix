"""Score a finished stack replay's Remix telemetry (WKR-7).
score_stack.py <run folder> <first level frame> [<last frame>]
Prints one JSON object: ray traced frames, frame time, GPU time and free video memory over the level frames,
and the bridge log's wait counts. Decides nothing by itself.
"""
import json
import os
import statistics
import sys

run = sys.argv[1]
first = int(sys.argv[2])
last = int(sys.argv[3]) if len(sys.argv) > 3 else 10 ** 9
rows = []
total = 0
with open(os.path.join(run, "proxy", "rtx_telemetry.jsonl"), encoding="utf-8", errors="replace") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        total += 1
        r = json.loads(line)
        if first <= r["frame"] <= last:
            rows.append((r["frame"], r["rayTraced"], r["frameTimeMs"], r["gpuTimeMs"], r["vramFreeMB"],
                         r["vramUsedMB"], r["instances"]))


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]


ft = [r[2] for r in rows]
out = {"run": run, "telemetryRows": total, "levelRows": len(rows), "firstFrame": rows[0][0], "lastFrame": rows[-1][0],
       "rayTraced": sum(1 for r in rows if r[1]),
       "notRayTracedFrames": [r[0] for r in rows if not r[1]][:10],
       "frameMsMedian": round(statistics.median(ft), 1), "frameMsP95": round(pct(ft, 0.95), 1),
       "frameMsP99": round(pct(ft, 0.99), 1), "frameMsMax": round(max(ft), 1),
       "framesOver250ms": [(r[0], round(r[2], 1)) for r in rows if r[2] > 250][:20],
       "framesOver250msCount": sum(1 for r in rows if r[2] > 250),
       "gpuMsMedian": round(statistics.median(r[3] for r in rows), 1),
       "vramFreeMinMB": min(r[4] for r in rows), "vramUsedMaxMB": max(r[5] for r in rows),
       "instancesMin": min(r[6] for r in rows), "instancesMax": max(r[6] for r in rows)}
logs = os.path.join(run, "stage", "rtx-remix", "logs")
for name, needles in (("bridge32.log", ("Still waiting on the Present", "no response from server")),
                      ("remix-dxvk.log", ("Vidmem dropped", "err:"))):
    p = os.path.join(logs, name)
    if os.path.exists(p):
        text = open(p, encoding="utf-8", errors="replace").read()
        for n in needles:
            out[name + " '" + n + "'"] = text.count(n)
print(json.dumps(out))
