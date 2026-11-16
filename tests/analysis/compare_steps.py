"""Compare per-step GPU/CPU times and scene metrics across parsed .pkl or raw rtx_telemetry.jsonl files.

Usage:
  python -I compare_steps.py <label=path[:start:end]> [<label=path[:start:end]> ...]
"""
import json
import pickle
import sys
from collections import defaultdict

import numpy as np

NUM = (
    "frame", "frameTimeMs", "gpuTimeMs", "instances", "skinnedInstances", "staticBlasRebuilds", "cameraCut",
    "textureMemoryMB", "vramUsedMB", "skinnedTriangles", "inwardSkinnedTriangles", "rayTraced", "telemetryCpuMs",
    "replacedMaterialDraws", "legacyMaterialDraws", "texturesDemoted", "textureBudgetMB", "thumbNonFinite",
    "thumbClipped", "staticBlasBuilds", "skinnedBlasUpdates", "unorderedInstances",
)


def load_source(spec):
    if "=" in spec:
        label, rest = spec.split("=", 1)
    else:
        label, rest = spec, spec
    parts = rest.rsplit(":", 2)
    f_lo, f_hi = None, None
    path = rest
    if len(parts) == 3 and (parts[1].isdigit() or parts[1] == "") and (parts[2].isdigit() or parts[2] == ""):
        path = parts[0]
        f_lo = int(parts[1]) if parts[1] else None
        f_hi = int(parts[2]) if parts[2] else None

    if path.endswith(".pkl"):
        with open(path, "rb") as f:
            d = pickle.load(f)
        cols = list(d["cols"])
        A = d["A"]
        passes = d["passes"]
        skips = d["skips"]
    else:
        rows = []
        passes_list = defaultdict(list)
        skips_list = defaultdict(list)
        with open(path, "rb") as f:
            for line in f:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                i = len(rows)
                rows.append([float(o.get(k, -1)) for k in NUM])
                for p_name, (g, c, cnt) in o.get("passMs", {}).items():
                    passes_list[p_name].append((i, g, c, cnt))
                for reason, cnt in o.get("skips", {}).items():
                    skips_list[reason].append((i, cnt))
        cols = list(NUM)
        A = np.array(rows, dtype=np.float64)
        passes = {k: np.array(v, dtype=np.float64) for k, v in passes_list.items()}
        skips = {k: np.array(v, dtype=np.float64) for k, v in skips_list.items()}

    col_idx = {k: i for i, k in enumerate(cols)}
    rt = A[:, col_idx["rayTraced"]] > 0.5
    frames = A[:, col_idx["frame"]]
    if f_lo is not None:
        rt = rt & (frames >= f_lo)
    if f_hi is not None:
        rt = rt & (frames <= f_hi)
    return label, cols, col_idx, A, passes, skips, rt


def summarize_pass(v, rt_mask):
    n_rt = int(rt_mask.sum())
    if n_rt == 0 or len(v) == 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    idx = v[:, 0].astype(np.int64)
    in_bounds = (idx >= 0) & (idx < len(rt_mask))
    idx = idx[in_bounds]
    v = v[in_bounds]
    keep = rt_mask[idx]
    if not keep.any():
        return 0.0, 0.0, 0.0, 0.0, 0.0
    g = v[keep, 1]
    c = v[keep, 2]
    ok = (g >= 0.0) & (g < 1.0e4)
    if not ok.any():
        return 0.0, 0.0, 0.0, float(np.median(c)), len(g) / n_rt
    g_ok = g[ok]
    mean_per_rt_frame = float(g_ok.sum() / n_rt)
    med_when_present = float(np.median(g_ok))
    p90_when_present = float(np.percentile(g_ok, 90))
    cpu_med = float(np.median(c))
    share = len(g) / n_rt
    return mean_per_rt_frame, med_when_present, p90_when_present, cpu_med, share


def main(argv):
    sources = [load_source(s) for s in argv]
    labels = [s[0] for s in sources]

    print("== Summary metrics on ray-traced frames (median [p90]) ==")
    header = f"{'metric':26s}" + "".join(f"  {lb:>22s}" for lb in labels)
    print(header)
    print("-" * len(header))

    n_line = f"{'rt_frames':26s}" + "".join(f"  {int(s[6].sum()):22d}" for s in sources)
    print(n_line)
    fps_vals = []
    for _, _, ci, A, _, _, rt in sources:
        med_ft = float(np.median(A[rt, ci["frameTimeMs"]])) if rt.any() else float("nan")
        fps_vals.append(1000.0 / med_ft if med_ft > 0 else 0.0)
    print(f"{'median_fps':26s}" + "".join(f"  {v:22.2f}" for v in fps_vals))

    metrics = [
        "frameTimeMs", "gpuTimeMs", "telemetryCpuMs", "instances", "skinnedInstances",
        "skinnedTriangles", "replacedMaterialDraws", "legacyMaterialDraws",
        "staticBlasBuilds", "staticBlasRebuilds", "skinnedBlasUpdates",
        "unorderedInstances", "textureMemoryMB", "textureBudgetMB", "vramUsedMB", "texturesDemoted",
    ]
    for m in metrics:
        cells = []
        for _, _, ci, A, _, _, rt in sources:
            if m not in ci or not rt.any():
                cells.append(f"{'n/a':>22s}")
                continue
            vals = A[rt, ci[m]]
            med = float(np.median(vals))
            p90 = float(np.percentile(vals, 90))
            cells.append(f"{med:9.2f} [{p90:8.2f}]  ")
        print(f"{m:26s}" + "".join(f"  {c:>22s}" for c in cells))

    all_passes = set()
    for _, _, _, _, passes, _, _ in sources:
        all_passes |= set(passes.keys())

    pass_rows = []
    for p_name in all_passes:
        per_src = [summarize_pass(s[4].get(p_name, np.empty((0, 4))), s[6]) for s in sources]
        max_mean = max((x[0] for x in per_src), default=0.0)
        if max_mean >= 0.03 or "Opacity" in p_name or "NRC" in p_name or "Volum" in p_name:
            pass_rows.append((max_mean, p_name, per_src))
    pass_rows.sort(reverse=True)

    print("\n== GPU pass breakdown on ray-traced frames: mean_ms/frame (median_ms, share%) ==")
    p_header = f"{'pass':58s}" + "".join(f"  {lb:>22s}" for lb in labels)
    print(p_header)
    print("-" * len(p_header))
    for _, p_name, per_src in pass_rows:
        cells = []
        for mean_g, med_g, _, _, share in per_src:
            if share == 0.0:
                cells.append(f"{'-':>22s}")
            else:
                cells.append(f"{mean_g:5.2f} ({med_g:5.2f},{share * 100:5.1f}%)")
        print(f"{p_name[:58]:58s}" + "".join(f"  {c:>22s}" for c in cells))

    print("\n== Non-InjectRTX gap (median frameTimeMs - median InjectRTX gpuMs) ==")
    for lb, _, ci, A, passes, _, rt in sources:
        if not rt.any():
            continue
        med_ft = float(np.median(A[rt, ci["frameTimeMs"]]))
        med_gpu = float(np.median(A[rt, ci["gpuTimeMs"]]))
        _, med_inj, _, cpu_inj, _ = summarize_pass(passes.get("InjectRTX", np.empty((0, 4))), rt)
        print(
            f"  {lb:22s}: frameTime={med_ft:6.2f} ms, gpuTime={med_gpu:6.2f} ms, "
            f"InjectRTX_gpu={med_inj:6.2f} ms, InjectRTX_cpu={cpu_inj:5.2f} ms, "
            f"frame_minus_InjectRTX={med_ft - med_inj:5.2f} ms, gpu_minus_InjectRTX={med_gpu - med_inj:5.2f} ms"
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sources", nargs="+", metavar="label=path[:start:end]",
                        help="a parsed .pkl (parse.py) or raw rtx_telemetry.jsonl, optionally limited to frames start..end")
    main(parser.parse_args().sources)
