"""Compare two replay run folders (WKR-7): pictures frame by frame and, when both have them, the proxy's per-frame
counts and Remix's per-frame instance counts.
compare_runs.py <run A> <run B> [<first frame>]
Pictures use apitrace's snapdiff.Comparer: precision in bits (8 = equal to the last bit) and pixels differing by more
than 5 percent. Prints one JSON object; decides nothing by itself.
"""
import glob
import json
import os
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, r"D:\BlacklistOverhaul\experiments\replay-rig\tools\apitrace-14.0-win32\lib\scripts")
from snapdiff import Comparer  # noqa: E402

a, b = sys.argv[1], sys.argv[2]
first = int(sys.argv[3]) if len(sys.argv) > 3 else 0


def frames(run):
    return {int(os.path.basename(p)[1:-4]): p for p in glob.glob(os.path.join(run, "frames", "f*.png"))}


fa, fb = frames(a), frames(b)
rows = []
for n in sorted(set(fa) & set(fb)):
    if n < first:
        continue
    c = Comparer(fa[n], fb[n])
    if c.size_mismatch():
        rows.append({"frame": n, "sizeMismatch": True})
        continue
    rows.append({"frame": n, "bits": round(c.precision(), 2), "over5pct": int(c.ae())})
bits = [r["bits"] for r in rows if "bits" in r]
out = {"a": a, "b": b, "firstFrame": first, "picturesCompared": len(rows), "onlyInA": len(set(fa) - set(fb)),
       "onlyInB": len(set(fb) - set(fa)), "sizeMismatches": sum(1 for r in rows if r.get("sizeMismatch")),
       "bitsMin": min(bits) if bits else None, "bitsMax": max(bits) if bits else None,
       "identicalPictures": sum(1 for r in rows if r.get("over5pct") == 0 and r.get("bits", 0) >= 8),
       "over5pctMax": max((r["over5pct"] for r in rows if "over5pct" in r), default=None), "perFrame": rows}


def proxy_frames(run):
    for name in ("proxy", "stage"):
        p = os.path.join(run, name, "d3d9_proxy_frames.csv")
        if os.path.exists(p):
            got = {}
            for line in open(p, encoding="utf-8", errors="replace"):
                f = line.rstrip("\r\n").split(",", 6)
                if f[0].isdigit():
                    got[int(f[0])] = (f[4], f[5], f[6] if len(f) > 6 else "")   # draws, camera count, camera list
            return got
    return None


pa, pb = proxy_frames(a), proxy_frames(b)
if pa is not None and pb is not None:
    common = sorted(n for n in set(pa) & set(pb) if n >= first)
    out["proxyFramesCompared"] = len(common)
    out["proxyFramesDiffering"] = sum(1 for n in common if pa[n] != pb[n])
    out["proxyFirstDiffering"] = [n for n in common if pa[n] != pb[n]][:5]


def telemetry(run):
    for name in ("proxy", "stage"):
        p = os.path.join(run, name, "rtx_telemetry.jsonl")
        if os.path.exists(p):
            got = {}
            for line in open(p, encoding="utf-8", errors="replace"):
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                got[r["frame"]] = (r["instances"], r["rayTraced"])
            return got
    return None


ta, tb = telemetry(a), telemetry(b)
if ta is not None and tb is not None:
    common = sorted(n for n in set(ta) & set(tb) if n >= first)
    diff = [n for n in common if ta[n] != tb[n]]
    out["remixFramesCompared"] = len(common)
    out["remixFramesDifferingInstances"] = len(diff)
    out["remixFirstDiffering"] = [(n, ta[n], tb[n]) for n in diff[:5]]
    out["remixLargestInstanceGap"] = max((abs(ta[n][0] - tb[n][0]) for n in common), default=None)
print(json.dumps(out))
