"""Find launches where Remix sat in its shader-compile wait instead of ray tracing.

While Remix shaders are still compiling and ray tracing is enabled, Remix does not ray trace and adds
rtx.shader.asyncCompilationThrottleMilliseconds (33 ms by default) to every frame. In telemetry that shows
as rayTraced=false with frameTimeMs - gpuTimeMs near 33 ms. A launch that never leaves that state never
ray traces (con_bright_hall, run 20261116-063147, pid 32932).

Usage:
  compile_wait.py [--root DIR] [--csv OUT] [files...]   table, one row per launch
  compile_wait.py --runs DIR [DIR...]                    wait runs after ray tracing started, joined to the proxy
  compile_wait.py --self-test                            fake launches; exit 1 if any verdict is wrong

Table (no files: every telemetry*.jsonl / rtx_telemetry*.jsonl under --root, identical copies once):
  frames          telemetry lines
  firstRtS        seconds (sum of frameTimeMs) from the first frame to the first ray-traced frame; - if never
  waitS           seconds of frames in the compile-wait signature
  waitPreRtS      of those, seconds before the first ray-traced frame
  midWaitS        of those, seconds after ray tracing had started (shaders compiled mid-session)
  preRtComp       most Remix pipelines in flight (remixShaderCompiles) before the first ray-traced frame, or over
                  the whole launch if it never ray traced; - when the recorder predates the field. Above 0 proves
                  the wait was Remix compiling; 0 with wait time means the signature caught something else, such
                  as a loading screen paced near 30 FPS.
  verdict         STUCK       never ray traced, >= --stuck-s of wait
                  NEVER_RT    never ray traced, little wait (ray tracing never switched on)
                  MID_WAIT    ray traced, but has wait frames after ray tracing started
                  OK          ray traced, wait only before the first ray-traced frame
                  NOT_MEASURED no frames
                  "(inferred)": no rayTraced field, so a frame counts as ray traced when gpuTimeMs >= 15.
Gate spot files start at the teleport, after ray tracing began, so their firstRtS is 0; only whole-launch
files (play-setup-logs\\before-*) measure the time to the first ray-traced frame.

--runs DIR: DIR holds rtx_telemetry.jsonl (or telemetry.jsonl), d3d9_proxy_frames.csv and d3d9_proxy_gate.csv.
Telemetry and proxy rows are joined on the frame number (same counter; the offset is checked and printed).
For each run of consecutive wait frames after the first ray-traced frame it prints the session time, length,
the worst frameTimeMs, and how many of its frames the proxy marks as loading (gate sceneDraws == 0 or an
empty/all-zero camera, the rule gate.py uses to excuse hitches) or goggles.
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor

SKIP = ("hitman", "anim-keyframe", "movemgr", "autopilot", "dryrun-")   # dryrun-*: synthetic gate fixtures
INFERRED_RT_GPU_MS = 15.0
FRAME = re.compile(rb'"frame":(\d+)')
FT = re.compile(rb'"frameTimeMs":([-\d.]+),"gpuTimeMs":([-\d.]+)')
RT = re.compile(rb'"rayTraced":(true|false)')
COMPILES = re.compile(rb'"remixShaderCompiles":(\d+)')


def frames_of(path):
    """Yields (frame, frameTimeMs, gpuTimeMs, rayTraced or None, remixShaderCompiles or None) per telemetry line."""
    with open(path, "rb") as f:
        for line in f:
            m = FT.search(line)
            if not m:
                continue
            fr = FRAME.search(line)
            r = RT.search(line)
            c = COMPILES.search(line)
            yield (int(fr.group(1)) if fr else -1, float(m.group(1)), float(m.group(2)),
                   None if r is None else r.group(1) == b"true", None if c is None else int(c.group(1)))


def is_wait(ft, gpu, rt, wait_min, wait_max):
    return not rt and wait_min <= ft - gpu <= wait_max


def scan(args):
    path, wait_min, wait_max, stuck_s = args
    frames, t, first_rt, wait_s, wait_before, has_rt_field = 0, 0.0, None, 0.0, 0.0, False
    pre_compiles = None   # most Remix compiles in flight before the first ray-traced frame; None: recorder lacks the field
    for _, ft, gpu, r, c in frames_of(path):
        frames += 1
        has_rt_field |= r is not None
        # Recorders before rayTraced existed: a ray-traced frame keeps the GPU busy, a compile-wait frame does not.
        rt = r if r is not None else gpu >= INFERRED_RT_GPU_MS
        if first_rt is None and c is not None:
            pre_compiles = max(pre_compiles or 0, c)
        if rt and first_rt is None:
            first_rt = t
        if is_wait(ft, gpu, rt, wait_min, wait_max):
            wait_s += ft / 1000.0
            if first_rt is None:
                wait_before += ft / 1000.0
        t += ft / 1000.0
    mid = wait_s - wait_before
    if frames == 0:
        verdict = "NOT_MEASURED"
    elif first_rt is None:
        verdict = "STUCK" if wait_s >= stuck_s else "NEVER_RT"
    else:
        verdict = "MID_WAIT" if mid > 0 else "OK"
    if frames and not has_rt_field:
        verdict += "(inferred)"
    return dict(path=path, frames=frames, lengthS=t, firstRtS=first_rt, waitS=wait_s, waitBeforeRtS=wait_before,
                midWaitS=mid, preRtCompiles=pre_compiles, verdict=verdict)


def key(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read(65536))
    return os.path.getsize(path), h.hexdigest()


def find(root):
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not any(s in x for s in SKIP)]
        for name in files:
            if name.endswith(".jsonl") and (name.startswith("telemetry") or name.startswith("rtx_telemetry")):
                yield os.path.join(d, name)


def table(paths, root, wait_min, wait_max, stuck_s, csv_out=None, out=sys.stdout):
    groups = {}
    for p in paths:
        groups.setdefault(key(p), []).append(p)
    jobs = [(ps[0], wait_min, wait_max, stuck_s) for ps in groups.values()]
    with ProcessPoolExecutor() as ex:
        results = sorted(ex.map(scan, jobs), key=lambda r: r["path"])
    copies = {ps[0]: len(ps) for ps in groups.values()}
    print(f"{'launch (first copy)':80} {'copies':>6} {'frames':>7} {'lenS':>6} {'firstRtS':>8} {'waitS':>6} "
          f"{'waitPreRtS':>10} {'midWaitS':>8} {'preRtComp':>9} verdict", file=out)
    counts = {}
    for r in results:
        rel = os.path.relpath(r["path"], root) if root and r["path"].startswith(root) else r["path"]
        frt = "-" if r["firstRtS"] is None else f"{r['firstRtS']:.1f}"
        r["launch"], r["copies"] = rel, copies[r["path"]]
        print(f"{rel[-80:]:80} {r['copies']:6d} {r['frames']:7d} {r['lengthS']:6.0f} {frt:>8} {r['waitS']:6.1f} "
              f"{r['waitBeforeRtS']:10.1f} {r['midWaitS']:8.1f} {'-' if r['preRtCompiles'] is None else r['preRtCompiles']:>9} "
              f"{r['verdict']}", file=out)
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("launches:", len(results), " ".join(f"{k}={v}" for k, v in sorted(counts.items())), file=out)
    if csv_out:
        with open(csv_out, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["launch", "copies", "frames", "lengthS", "firstRtS", "waitS", "waitBeforeRtS", "midWaitS",
                        "preRtCompiles", "verdict"])
            for r in results:
                w.writerow([r["launch"], r["copies"], r["frames"], round(r["lengthS"], 1), r["firstRtS"],
                            round(r["waitS"], 1), round(r["waitBeforeRtS"], 1), round(r["midWaitS"], 1),
                            r["preRtCompiles"], r["verdict"]])
    return results


def read_proxy(d):
    """{frame: (frameDeltaMs, presentMs, emptyOrZeroCamera)} and {frame: (sceneDraws, goggles, rtActive)}."""
    frames, gate = {}, {}
    pick = lambda name: next(p for p in (os.path.join(d, "d3d9_" + name), os.path.join(d, name)) if os.path.exists(p))
    with open(pick("proxy_frames.csv"), newline="") as f:
        for row in csv.reader(f):
            try:
                cam = row[6] if len(row) >= 7 else None
                zero = cam == "" or (cam is not None and all(
                    len(p) >= 9 and p[2] == "0" and all(abs(float(v)) < 1e-6 for v in p[3:6])
                    for p in (x.split(":") for x in cam.split("|"))))
                frames[int(row[0])] = (float(row[2]), float(row[3]), zero)
            except (ValueError, IndexError):
                continue
    with open(pick("proxy_gate.csv"), newline="") as f:
        for row in csv.reader(f):
            try:
                gate[int(row[0])] = (int(row[1]), int(row[4]), int(row[5]))
            except (ValueError, IndexError):
                continue
    return frames, gate


def best_offset(tel, proxy, span=5, sample=20000):
    """The proxy frame offset whose frame deltas best match the telemetry frame times (sum of |difference|)."""
    tf = [(fr, ft) for fr, ft, _, _, _ in tel[1:sample]]
    scores = {}
    for off in range(-span, span + 1):
        diffs = [abs(proxy[fr + off][0] - ft) for fr, ft in tf if fr + off in proxy]
        scores[off] = sum(diffs) / len(diffs) if diffs else float("inf")
    off = min(scores, key=scores.get)
    return off, scores


def runs(d, wait_min, wait_max, out=sys.stdout):
    tel_path = next((os.path.join(d, n) for n in ("rtx_telemetry.jsonl", "telemetry.jsonl")
                     if os.path.exists(os.path.join(d, n))), None)
    tel = list(frames_of(tel_path))
    proxy, gate = read_proxy(d)
    off, scores = best_offset(tel, proxy)
    print(f"== {d}\n   {len(tel)} telemetry frames, {len(proxy)} proxy frames; join offset {off} "
          f"(mean |delta| {scores[off]:.2f} ms; offset 0: {scores.get(0, float('inf')):.2f} ms)", file=out)
    t, first_rt, cur, found = 0.0, None, None, []
    # Wait frames before the first ray-traced frame: count, seconds, loading count, seconds, count with the proxy's
    # ray tracing off. Remix only throttles while ray tracing is on, so those last frames are not compile waits.
    pre = [0, 0.0, 0, 0.0, 0]
    for i, (fr, ft, gpu, r, _) in enumerate(tel):
        rt = r if r is not None else gpu >= INFERRED_RT_GPU_MS
        if rt and first_rt is None:
            first_rt = i
        if first_rt is None and is_wait(ft, gpu, rt, wait_min, wait_max):
            p = fr + off
            pre[0] += 1
            pre[1] += ft / 1000.0
            if (p in gate and gate[p][0] == 0) or (p in proxy and proxy[p][2]):
                pre[2] += 1
                pre[3] += ft / 1000.0
            pre[4] += p in gate and gate[p][2] == 0
        w = first_rt is not None and is_wait(ft, gpu, rt, wait_min, wait_max)
        if w:
            if cur is None:
                cur = dict(start=i, t=t, frames=[])
            cur["frames"].append((fr, ft))
        elif cur is not None:
            cur["next_rt"] = rt
            found.append(cur)
            cur = None
        t += ft / 1000.0
    if cur is not None:
        cur["next_rt"] = None
        found.append(cur)
    print(f"   before the first ray-traced frame: {pre[0]} wait frames ({pre[1]:.1f} s), "
          f"{pre[2]} of them loading ({pre[3]:.1f} s), {pre[4]} with the proxy's ray tracing off", file=out)
    print(f"   first ray-traced frame at line {first_rt}; {len(found)} wait runs after it, "
          f"{sum(len(c['frames']) for c in found)} frames", file=out)
    print(f"   {'sessionS':>8} {'frame':>7} {'frames':>6} {'seconds':>7} {'worstMs':>7} {'>50ms':>5} {'loading':>7} "
          f"{'goggles':>7} {'rtActive':>8} {'prevRT':>6} {'nextRT':>6}", file=out)
    for c in found:
        frs = [fr + off for fr, _ in c["frames"]]
        load = sum(1 for p in frs if (p in gate and gate[p][0] == 0) or (p in proxy and proxy[p][2]))
        gog = sum(1 for p in frs if p in gate and gate[p][1] != 0)
        rta = sum(1 for p in frs if p in gate and gate[p][2] != 0)
        prev = tel[c["start"] - 1]
        prev_rt = prev[3] if prev[3] is not None else prev[2] >= INFERRED_RT_GPU_MS
        sec = sum(ft for _, ft in c["frames"]) / 1000.0
        worst = max(ft for _, ft in c["frames"])
        over = sum(1 for _, ft in c["frames"] if ft > 50.0)
        print(f"   {c['t']:8.1f} {c['frames'][0][0]:7d} {len(frs):6d} {sec:7.2f} {worst:7.1f} {over:5d} {load:7d} "
              f"{gog:7d} {rta:8d} {str(prev_rt):>6} {str(c['next_rt']):>6}", file=out)
    return found


def self_test():
    """Fake launches. Each must get its verdict; the mid-session run must be found by --runs."""
    def line(fr, ft, gpu, rt, compiles=None):
        o = {"frame": fr, "frameTimeMs": ft, "gpuTimeMs": gpu, "instances": 1}
        if rt is not None:
            o["rayTraced"] = rt
        if compiles is not None:
            o["remixShaderCompiles"] = compiles
        return json.dumps(o, separators=(",", ":")) + "\n"

    menu = [(10.0, 5.0, False)] * 300                 # menus: ray tracing off, no throttle
    wait = [(35.0, 2.0, False)] * 600                 # 21 s of compile wait
    rt = [(33.0, 31.0, True)] * 900                   # ray tracing
    cases = {
        "stuck": (menu + wait * 10, "STUCK"),
        # remixShaderCompiles present: in flight during the wait proves a compile wait; 0 during the same signature
        # (a loading screen paced near 30 FPS) must report 0, not a compile.
        "stuck_compiling": ([r + (0,) for r in menu] + [r + (3,) for r in wait * 10], "STUCK", 3),
        "loading_paced": ([r + (0,) for r in menu + wait + rt], "OK", 0),
        "normal": (menu + wait + rt, "OK"),
        "mid_session": (menu + rt + wait[:40] + rt, "MID_WAIT"),
        "old_recorder": ([(f, g, None) for f, g, _ in menu + wait + rt], "OK(inferred)"),
        "never_on": (menu * 20, "NEVER_RT"),
    }
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        paths = {}
        for name, (rows, *_) in cases.items():
            os.makedirs(os.path.join(tmp, name))
            p = paths[name] = os.path.join(tmp, name, "rtx_telemetry.jsonl")
            with open(p, "w", encoding="utf-8") as f:
                f.writelines(line(i, *r) for i, r in enumerate(rows))
        for name, (_, want, *comp) in cases.items():
            r = scan((paths[name], 28.0, 40.0, 20.0))
            want_comp = comp[0] if comp else None   # None: the fake recorder has no remixShaderCompiles
            ok = r["verdict"] == want and r["preRtCompiles"] == want_comp
            bad += not ok
            print(f"{'ok ' if ok else 'BAD'} {name}: {r['verdict']}, preRtCompiles {r['preRtCompiles']} "
                  f"(want {want}, {want_comp})")
        # --runs on the mid-session launch: one run of 40 frames, ray traced before and after, 10 of them loading.
        d = os.path.dirname(paths["mid_session"])
        n = len(cases["mid_session"][0])
        with open(os.path.join(d, "d3d9_proxy_frames.csv"), "w", newline="") as f:
            w = csv.writer(f)
            for i, (ft, _, _) in enumerate(cases["mid_session"][0]):
                w.writerow([i, i * 30.0, ft, 1.0, 3, 1, "" if 1200 <= i < 1210 else "C:0:0:1:2:3:0:0:1:0"])
        with open(os.path.join(d, "d3d9_proxy_gate.csv"), "w", newline="") as f:
            csv.writer(f).writerows([i, 5, 0, 1, 0, 1] for i in range(n))
        sink = open(os.devnull, "w")
        found = runs(d, 28.0, 40.0, out=sink)
        sink.close()
        c = found[0] if len(found) == 1 else None
        ok = c is not None and len(c["frames"]) == 40 and c["frames"][0][0] == 1200 and c["next_rt"] is True
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} runs: {len(found)} run(s)" + (f", {len(c['frames'])} frames from frame "
                                                                        f"{c['frames'][0][0]}" if c else ""))
    print("compile_wait self-test:", "PASS" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*")
    ap.add_argument("--root", default=os.path.normpath(os.path.join(here, "..", "..", "..", "..", "experiments")))
    ap.add_argument("--csv")
    ap.add_argument("--runs", nargs="+", metavar="DIR")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--wait-min", type=float, default=28.0)
    ap.add_argument("--wait-max", type=float, default=40.0)
    ap.add_argument("--stuck-s", type=float, default=20.0)
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.runs:
        for d in a.runs:
            runs(d, a.wait_min, a.wait_max)
        return 0
    table(a.files or sorted(find(a.root)), a.root, a.wait_min, a.wait_max, a.stuck_s, a.csv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
