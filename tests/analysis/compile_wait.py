"""Find launches where Remix sat in its shader-compile wait instead of ray tracing.

While Remix shaders are still compiling and ray tracing is enabled, Remix does not ray trace and adds
rtx.shader.asyncCompilationThrottleMilliseconds (33 ms by default) to every frame. In telemetry that shows
as rayTraced=false with frameTimeMs - gpuTimeMs near 33 ms. A launch that never leaves that state never
ray traces (con_bright_hall, run 20261116-063147, pid 32932).

Usage: compile_wait.py [--root DIR] [--csv OUT] [--wait-min MS] [--wait-max MS] [files...]
With no files, scans every telemetry*.jsonl / rtx_telemetry*.jsonl under --root (default: experiments).
Identical copies (same size and same first 64 KB) are reported once, with a copy count.

Per launch:
  frames          telemetry lines
  firstRtS        seconds (sum of frameTimeMs) from the first frame to the first ray-traced frame; - if never
  waitS           seconds of frames in the compile-wait signature
  waitBeforeRtS   of those, seconds before the first ray-traced frame
  verdict         STUCK (never ray traced, >= --stuck-s of wait), NEVER_RT (never ray traced, little wait,
                  e.g. ray tracing was never switched on), OK, or NOT_MEASURED (no frames). "(inferred)" means the
                  file has no rayTraced field, so a frame counts as ray traced when gpuTimeMs >= 15.
Gate spot files start at the teleport, after ray tracing began, so their firstRtS is 0; only whole-launch
files (play-setup-logs\\before-*) measure the time to the first ray-traced frame.
"""
import argparse
import hashlib
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor

SKIP = ("hitman", "anim-keyframe", "movemgr", "autopilot", "dryrun-")   # dryrun-*: synthetic gate fixtures
INFERRED_RT_GPU_MS = 15.0
FT = re.compile(rb'"frameTimeMs":([-\d.]+),"gpuTimeMs":([-\d.]+)')
RT = re.compile(rb'"rayTraced":(true|false)')


def key(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read(65536))
    return os.path.getsize(path), h.hexdigest()


def scan(args):
    path, wait_min, wait_max, stuck_s = args
    frames = 0
    t = 0.0
    first_rt = None
    wait_s = 0.0
    wait_before = 0.0
    has_rt_field = False
    with open(path, "rb") as f:
        for line in f:
            m = FT.search(line)
            if not m:
                continue
            frames += 1
            ft = float(m.group(1))
            gpu = float(m.group(2))
            r = RT.search(line)
            has_rt_field |= r is not None
            # Recorders before rayTraced existed: a ray-traced frame keeps the GPU busy (>= 15 ms here), a
            # compile-wait frame does not.
            rt = r.group(1) == b"true" if r is not None else gpu >= INFERRED_RT_GPU_MS
            if rt and first_rt is None:
                first_rt = t
            if not rt and wait_min <= ft - gpu <= wait_max:
                wait_s += ft / 1000.0
                if first_rt is None:
                    wait_before += ft / 1000.0
            t += ft / 1000.0
    if frames == 0:
        verdict = "NOT_MEASURED"
    elif first_rt is not None:
        verdict = "OK"
    elif wait_s >= stuck_s:
        verdict = "STUCK"
    else:
        verdict = "NEVER_RT"
    if frames and not has_rt_field:
        verdict += "(inferred)"
    return dict(path=path, frames=frames, lengthS=t, firstRtS=first_rt, waitS=wait_s, waitBeforeRtS=wait_before,
                verdict=verdict)


def find(root):
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not any(s in x for s in SKIP)]
        for name in files:
            if name.endswith(".jsonl") and (name.startswith("telemetry") or name.startswith("rtx_telemetry")):
                yield os.path.join(d, name)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*")
    ap.add_argument("--root", default=os.path.normpath(os.path.join(here, "..", "..", "..", "..", "experiments")))
    ap.add_argument("--csv")
    ap.add_argument("--wait-min", type=float, default=28.0)
    ap.add_argument("--wait-max", type=float, default=40.0)
    ap.add_argument("--stuck-s", type=float, default=20.0)
    a = ap.parse_args()
    paths = a.files or sorted(find(a.root))
    groups = {}
    for p in paths:
        groups.setdefault(key(p), []).append(p)
    jobs = [(ps[0], a.wait_min, a.wait_max, a.stuck_s) for ps in groups.values()]
    with ProcessPoolExecutor() as ex:
        results = list(ex.map(scan, jobs))
    copies = {ps[0]: len(ps) for ps in groups.values()}
    results.sort(key=lambda r: r["path"])
    rows = []
    for r in results:
        rel = os.path.relpath(r["path"], a.root) if a.root and r["path"].startswith(a.root) else r["path"]
        rows.append((rel, copies[r["path"]], r["frames"], r["lengthS"],
                     "-" if r["firstRtS"] is None else f"{r['firstRtS']:.1f}", r["waitS"], r["waitBeforeRtS"],
                     r["verdict"]))
    print(f"{'launch (first copy)':90} {'copies':>6} {'frames':>7} {'lenS':>7} {'firstRtS':>8} {'waitS':>7} "
          f"{'waitPreRt':>9} verdict")
    for rel, n, fr, ln, frt, w, wb, v in rows:
        print(f"{rel[-90:]:90} {n:6d} {fr:7d} {ln:7.0f} {frt:>8} {w:7.1f} {wb:9.1f} {v}")
    counts = {}
    for row in rows:
        counts[row[-1]] = counts.get(row[-1], 0) + 1
    print("launches:", len(rows), " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if a.csv:
        with open(a.csv, "w", encoding="utf-8") as f:
            f.write("launch,copies,frames,lengthS,firstRtS,waitS,waitBeforeRtS,verdict\n")
            for row in rows:
                f.write(",".join(str(x) for x in row) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
