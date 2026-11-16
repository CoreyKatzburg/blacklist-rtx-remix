"""Streams rtx_telemetry.jsonl into compact arrays (scratch analysis, not project code).

Usage: python -I parse.py <rtx_telemetry.jsonl> <out.pkl>
"""
import base64
import json
import pickle
import sys
from collections import defaultdict

import numpy as np

NUM = ("frame", "frameTimeMs", "gpuTimeMs", "instances", "skinnedInstances", "staticBlasRebuilds", "cameraCut",
       "textureMemoryMB", "vramUsedMB", "skinnedTriangles", "inwardSkinnedTriangles", "rayTraced", "telemetryCpuMs",
       "replacedMaterialDraws", "legacyMaterialDraws", "texturesDemoted", "textureBudgetMB", "thumbNonFinite",
       "thumbClipped", "staticBlasBuilds", "skinnedBlasUpdates", "unorderedInstances")

rows, bad = [], 0
passes = defaultdict(list)      # path -> [(row, gpuMs, cpuMs, count)]
skips = defaultdict(list)       # reason -> [(row, count)]
legacy = []                     # (row, hash)
luma_mean, luma_p99, luma_white, rgb_mean = [], [], [], []
keys_seen = set()

with open(sys.argv[1], "rb") as f:
    for line in f:
        try:
            o = json.loads(line)
        except ValueError:
            bad += 1
            continue
        i = len(rows)
        keys_seen |= o.keys()
        rows.append([float(o.get(k, -1)) for k in NUM])
        for path, (g, c, n) in o.get("passMs", {}).items():
            passes[path].append((i, g, c, n))
        for reason, n in o.get("skips", {}).items():
            skips[reason].append((i, n))
        for h in o.get("newLegacyMaterials", ()):
            legacy.append((i, h))
        t = o.get("lumaThumb")
        if t:
            a = np.frombuffer(base64.b64decode(t), np.uint8)
            luma_mean.append(a.mean() / 255.0)
            luma_p99.append(np.percentile(a, 99) / 255.0)
            luma_white.append(int((a >= 250).sum()))
        else:
            luma_mean.append(-1.0); luma_p99.append(-1.0); luma_white.append(-1)
        t = o.get("rgbThumb")
        if t:
            a = np.frombuffer(base64.b64decode(t), np.uint8).reshape(-1, 3)
            rgb_mean.append(a.mean(axis=0) / 255.0)
        else:
            rgb_mean.append(np.array([-1.0, -1.0, -1.0]))

out = {
    "cols": NUM, "A": np.array(rows), "bad": bad, "keys": sorted(keys_seen),
    "passes": {k: np.array(v) for k, v in passes.items()},
    "skips": {k: np.array(v) for k, v in skips.items()},
    "legacy": legacy,
    "luma_mean": np.array(luma_mean), "luma_p99": np.array(luma_p99), "luma_white": np.array(luma_white),
    "rgb_mean": np.array(rgb_mean),
}
with open(sys.argv[2], "wb") as f:
    pickle.dump(out, f)
print(f"rows {len(rows)}, bad lines {bad}, step names {len(passes)}, skip reasons {len(skips)}, fallback textures {len(legacy)}")
print("keys:", ", ".join(sorted(keys_seen)))
