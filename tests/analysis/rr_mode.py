"""Why Ray Reconstruction runs in an 11.1 ms or a 13.3 ms mode: GPU clock, or something specific to RR?

Usage: python -I rr_mode.py [--rebuild] [--self-test]
Reads every gate spot's telemetry.jsonl under experiments\\remix-gate (one launch per spot) once, keeps the
GPU time of every render step on ray-traced frames, and caches it in .amp\\in\\c_rr_mode.pkl.

The test: steps whose cost depends only on the render size, not the scene (Demodulate, Composition, Bloom,
Local Tone Mapping, Auto Exposure, PostFx, Blit to Game), are a clock probe. If they slow down by the same
ratio as RR between a fast and a slow launch, the whole GPU ran slower (clock, power or thermal state). If
they do not move while RR does, the extra cost is inside RR (model, preset or render size).
Second test, within one launch: frame by frame, does the probe sum follow RR when RR flips between modes?
"""
import glob
import json
import os
import pickle
import sys

import numpy as np

GATE = r"D:\BlacklistOverhaul\experiments\remix-gate"
CACHE = r"D:\BlacklistOverhaul\tools\remix\.amp\in\c_rr_mode.pkl"
RR = "InjectRTX/Ray Reconstruction"
PROBES = ("InjectRTX/Demodulate", "InjectRTX/Composite/Composition", "InjectRTX/Bloom", "InjectRTX/Local Tone Mapping",
          "InjectRTX/Auto Exposure", "InjectRTX/PostFx", "InjectRTX/Blit to Game")
SCENE = ("InjectRTX/Integrate Raytracing", "InjectRTX/Gbuffer Raytracing", "InjectRTX/RTXDI")
KEYS = (RR,) + PROBES + SCENE


def load_spot(path):
    """Per ray-traced frame: frame number, textureBudgetMB, and the GPU ms of each step in KEYS (nan if absent)."""
    rows = []
    with open(path, "rb") as f:
        for line in f:
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not o.get("rayTraced"):
                continue
            p = o.get("passMs", {})
            g = [p[k][0] if k in p and p[k][0] >= 0 else np.nan for k in KEYS]
            rows.append([o["frame"], o.get("textureBudgetMB", np.nan)] + g)
    return np.array(rows, dtype=np.float64).reshape(-1, 2 + len(KEYS))


def build_cache():
    data = {}
    for path in sorted(glob.glob(os.path.join(GATE, "2026111[4-9]-*", "*", "telemetry.jsonl"))):
        spot = os.path.relpath(os.path.dirname(path), GATE)
        a = load_spot(path)
        if len(a) >= 50:
            data[spot] = a
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "wb") as f:
        pickle.dump({"keys": KEYS, "spots": data}, f)
    return data


def probe_sum(a):
    return np.nansum(a[:, 2 + 1:2 + 1 + len(PROBES)], axis=1)


def report(data):
    print(f"{'spot (one launch each)':38} {'n':>6} {'RR':>6} {'probes':>7} {'integ':>6}  per-probe ms")
    summary = []
    for spot, a in data.items():
        rr = np.nanmedian(a[:, 2])
        pr = np.median(probe_sum(a))
        integ = np.nanmedian(a[:, 2 + KEYS.index(SCENE[0])])
        per = [np.nanmedian(a[:, 2 + KEYS.index(k)]) for k in PROBES]
        summary.append((spot, rr, pr, per))
        print(f"{spot:38} {len(a):6} {rr:6.2f} {pr:7.3f} {integ:6.2f}  " + " ".join(f"{v:.3f}" for v in per))
    fast = [s for s in summary if s[1] < 12.0]
    slow = [s for s in summary if s[1] >= 12.5]
    if fast and slow:
        rr_ratio = np.median([s[1] for s in slow]) / np.median([s[1] for s in fast])
        pr_ratio = np.median([s[2] for s in slow]) / np.median([s[2] for s in fast])
        print(f"\nlaunch level: {len(fast)} fast-RR launches, {len(slow)} slow-RR launches")
        print(f"  RR slow/fast {rr_ratio:.3f}; probe sum slow/fast {pr_ratio:.3f}")
        for i, k in enumerate(PROBES):
            r = np.median([s[3][i] for s in slow]) / np.median([s[3][i] for s in fast])
            print(f"    {k:40} slow/fast {r:.3f}")
    # Within one launch where RR flips: frame-level correlation of RR with the probe sum.
    print("\nwithin a launch, frames split at RR 12 ms (only spots with at least 100 frames on each side):")
    for spot, a in data.items():
        rr, pr = a[:, 2], probe_sum(a)
        lo, hi = rr < 12.0, rr >= 12.5
        if lo.sum() >= 100 and hi.sum() >= 100:
            print(f"  {spot:38} RR {np.median(rr[lo]):.2f} -> {np.median(rr[hi]):.2f} "
                  f"(x{np.median(rr[hi]) / np.median(rr[lo]):.3f}); probes {np.median(pr[lo]):.3f} -> "
                  f"{np.median(pr[hi]):.3f} (x{np.median(pr[hi]) / np.median(pr[lo]):.3f}); "
                  f"tbud {np.nanmedian(a[lo, 1]):.0f} vs {np.nanmedian(a[hi, 1]):.0f}")


def self_test():
    """Fake data: a clock-limited slow launch must show probe ratio ~ RR ratio, an RR-only one must not."""
    def fake(rr, probe_scale, n=200):
        a = np.full((n, 2 + len(KEYS)), np.nan)
        a[:, 0], a[:, 1], a[:, 2] = np.arange(n), 1200, rr
        a[:, 3:3 + len(PROBES)] = 0.2 * probe_scale
        return a
    clock = {"fast": fake(11.1, 1.0), "slow": fake(13.3, 1.2)}
    rronly = {"fast": fake(11.1, 1.0), "slow": fake(13.3, 1.0)}
    r1 = np.median(probe_sum(clock["slow"])) / np.median(probe_sum(clock["fast"]))
    r2 = np.median(probe_sum(rronly["slow"])) / np.median(probe_sum(rronly["fast"]))
    assert abs(r1 - 1.2) < 1e-9 and abs(r2 - 1.0) < 1e-9, (r1, r2)
    print("rr_mode self-test: PASS (clock case probes x1.20, RR-only case probes x1.00)")


if __name__ == "__main__":
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
    elif "--self-test" in sys.argv:
        self_test()
    else:
        if "--rebuild" in sys.argv or not os.path.exists(CACHE):
            spots = build_cache()
        else:
            with open(CACHE, "rb") as f:
                spots = pickle.load(f)["spots"]
        report(spots)
