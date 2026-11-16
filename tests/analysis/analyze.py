"""Scratch: joins the proxy CSVs with the Remix recorder for one free-play session.

Usage: python -I analyze.py <session.pkl> <SYSTEM folder or log folder with d3d9_proxy_*.csv>
"""
import os
import pickle
import sys

import numpy as np

d = pickle.load(open(sys.argv[1], "rb"))
folder = sys.argv[2]
A, cols = d["A"], list(d["cols"])
col = lambda k: A[:, cols.index(k)]
n = len(A)
rt = col("rayTraced") > 0.5
ft, gpu = col("frameTimeMs"), col("gpuTimeMs")
luma, rgb = d["luma_mean"], d["rgb_mean"]
tmin = np.cumsum(ft) / 60000.0

gate = np.loadtxt(os.path.join(folder, "d3d9_proxy_gate.csv"), delimiter=",", dtype=np.int64)
pf = []
with open(os.path.join(folder, "d3d9_proxy_frames.csv"), "r", errors="replace") as f:
    for line in f:
        p = line.split(",", 6)
        try:
            pf.append([float(x) for x in p[:6]])
        except ValueError:
            pf.append([-1.0] * 6)
pf = np.array(pf)
print(f"telemetry frames {n}, proxy gate rows {len(gate)}, proxy frame rows {len(pf)}")

# proxy says: this frame should show the ray-traced picture
def expect_rt(g):
    return (g[:, 1] > 0) & (g[:, 4] == 0) & (g[:, 5] == 1) & (g[:, 3] > 0)

best = None
for off in range(-3, 4):
    lo, hi = max(0, -off), min(n, len(gate) - off)
    e = expect_rt(gate[lo + off:hi + off])
    agree = (e == rt[lo:hi]).mean()
    print(f"  offset {off:+d}: proxy 'expects ray tracing' agrees with Remix 'ray traced' on {agree:.4%} of frames")
    if best is None or agree > best[0]:
        best = (agree, off)
off = best[1]
print(f"using offset {off:+d}")
m = min(n, len(gate) - max(off, 0))
idx = np.arange(m)
g = gate[np.clip(idx + off, 0, len(gate) - 1)]
p = pf[np.clip(idx + off, 0, len(pf) - 1)]
rt, ft, gpu, luma, rgb, tmin = rt[:m], ft[:m], gpu[:m], luma[:m], rgb[:m], tmin[:m]
scene, skinned_draws, handoffs, goggles, rt_active = g[:, 1], g[:, 2], g[:, 3], g[:, 4], g[:, 5]
exp = expect_rt(g)

def runs(mask):
    out, i = [], 0
    while i < len(mask):
        if mask[i]:
            j = i
            while j + 1 < len(mask) and mask[j + 1]:
                j += 1
            out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out

print("\n== 1. what the frames that were not ray traced were ==")
nrt = ~rt
kinds = {
    "goggles on (game's own picture, intended)": nrt & (goggles == 1),
    "no 3D scene drawn (menu, loading, video)": nrt & (goggles == 0) & (scene == 0),
    "scene drawn, proxy had ray tracing off": nrt & (goggles == 0) & (scene > 0) & (rt_active == 0),
    "scene drawn, proxy wanted ray tracing, Remix drew none": nrt & exp,
    "scene drawn, ray tracing on, no handoff": nrt & (goggles == 0) & (scene > 0) & (rt_active == 1) & (handoffs == 0),
}
for k, v in kinds.items():
    print(f"  {v.sum():6d} frames ({ft[v].sum() / 60000:5.2f} min)  {k}")
print(f"  {nrt.sum():6d} total not ray traced; ray traced while proxy did not expect it: {(rt & ~exp).sum()}")
for name, v in list(kinds.items())[2:]:
    rr = runs(v)
    if rr:
        print(f"  runs of '{name}': {len(rr)}, longest {max(b - a + 1 for a, b in rr)} frames; first 8: " +
              ", ".join(f"f{a}-{b} (min {tmin[a]:.1f})" for a, b in rr[:8]))

print("\n== 2. goggles turning off (ray tracing coming back) ==")
gon = runs(goggles == 1)
print(f"  goggle periods: {len(gon)}, total {sum(ft[a:b + 1].sum() for a, b in gon) / 60000:.2f} min, longest {max((ft[a:b + 1].sum() for a, b in gon), default=0) / 1000:.1f} s")
dark, rebuild, stall, gap = 0, 0, 0, 0
rows = []
sb = col("staticBlasBuilds")[:m]
for a, b in gon:
    s = b + 1
    if s + 40 >= m:
        continue
    after = np.arange(s, s + 40)
    rta = after[rt[after]]
    if len(rta) < 20:
        continue
    ref = np.median(luma[rta[10:]])
    first = luma[s:s + 4]
    worst = first.min()
    not_rt_after = int((~rt[s:s + 4]).sum())
    builds = int(sb[s:s + 6].sum())
    worst_ms = ft[s - 2:s + 8].max()
    is_dark = ref > 0.02 and worst < 0.4 * ref
    dark += is_dark; rebuild += builds > 20; stall += worst_ms > 50; gap += not_rt_after > 0
    rows.append((s, tmin[s], ref, worst, not_rt_after, builds, worst_ms))
print(f"  toggles measured {len(rows)}: with a dark frame (under 40% of settled brightness) {dark}; with over 20 static mesh builds in 6 frames {rebuild}; "
      f"with a frame over 50 ms {stall}; with a frame Remix did not ray trace after goggles off {gap}")
for s, t, ref, worst, nr, builds, ms in rows[:12]:
    print(f"    frame {s} (min {t:4.1f}): settled brightness {ref:.3f}, darkest of first 4 frames {worst:.3f}, non-ray-traced frames {nr}, mesh builds {builds}, worst frame {ms:.0f} ms")

print("\n== 3. ray-traced frames with no character meshes in Remix ==")
sk = col("skinnedInstances")[:m]
z = rt & (sk == 0)
zz = runs(z)
print(f"  {z.sum()} frames in {len(zz)} runs; the game drew characters in {(z & (skinned_draws > 0)).sum()} of them, drew none in {(z & (skinned_draws == 0)).sum()}")
for a, b in zz[:15]:
    print(f"    f{a}-{b} ({b - a + 1} frames, min {tmin[a]:.1f}): game character draws {int(np.median(skinned_draws[a:b + 1]))}, scene draws {int(np.median(scene[a:b + 1]))}, "
          f"brightness {np.median(luma[a:b + 1]):.3f}, frames since goggles/ray tracing change {a - max([e for _, e in gon if e < a] + [0])}")
# the reverse: game drew plenty of characters, Remix has very few
few = rt & (skinned_draws >= 10) & (sk < 0.25 * skinned_draws)
print(f"  frames where the game drew 10+ character meshes and Remix kept under a quarter of them: {few.sum()} in {len(runs(few))} runs")
ratio = sk[rt & (skinned_draws > 0)] / skinned_draws[rt & (skinned_draws > 0)]
print(f"  Remix character meshes per game character draw: median {np.median(ratio):.2f}, p1 {np.percentile(ratio, 1):.2f}, min {ratio.min():.2f}")

print("\n== 4. stalls: ray-traced frames over 50 ms ==")
slow = rt & (ft > 50)
sr = runs(slow)
gpu_ok = slow & (gpu < 45)
present = p[:, 3]
print(f"  {slow.sum()} frames in {len(sr)} runs; over 100 ms: {(rt & (ft > 100)).sum()}; over 200 ms: {(rt & (ft > 200)).sum()}; worst {ft[rt].max():.0f} ms")
print(f"  GPU time was normal (under 45 ms) in {gpu_ok.sum()} of them, so the wait was elsewhere; GPU itself slow in {(slow & (gpu >= 45)).sum()}")
near_toggle = np.zeros(m, bool)
for a, b in gon:
    near_toggle[max(0, a - 5):a + 10] = True
    near_toggle[b - 5:b + 15] = True
cut = col("cameraCut")[:m] > 0
near_cut = np.zeros(m, bool)
for i in np.flatnonzero(cut):
    near_cut[max(0, i - 5):i + 30] = True
builds_heavy = sb > 20
print(f"  stall frames within 15 frames of a goggle toggle: {(slow & near_toggle).sum()}; within 30 frames after a camera cut: {(slow & near_cut & ~near_toggle).sum()}; "
      f"with over 20 static mesh builds that frame: {(slow & builds_heavy).sum()}; none of these: {(slow & ~near_toggle & ~near_cut & ~builds_heavy).sum()}")
per_min = [int((slow & (tmin >= k) & (tmin < k + 1)).sum()) for k in range(int(tmin[-1]) + 1)]
print("  per minute: " + " ".join(str(x) for x in per_min))

print("\n== 5. dark ray-traced frames (mean brightness under 0.03) ==")
dk = rt & (luma >= 0) & (luma < 0.03)
dr = [r for r in runs(dk)]
long_dr = [r for r in dr if r[1] - r[0] >= 30]
print(f"  {dk.sum()} frames in {len(dr)} runs; runs of 30+ frames: {len(long_dr)}; single or double frames: {sum(1 for a, b in dr if b - a < 2)}")
for a, b in sorted(long_dr, key=lambda r: r[0] - r[1])[:8]:
    print(f"    f{a}-{b} ({ft[a:b + 1].sum() / 1000:.1f} s at min {tmin[a]:.1f}): brightness {np.median(luma[a:b + 1]):.4f}, scene draws {int(np.median(scene[a:b + 1]))}, instances {int(np.median(col('instances')[a:b + 1]))}")

print("\n== 6. bright and white ==")
cl = col("thumbClipped")[:m]
print(f"  ray-traced frames with over 1% pure white pixels: {(rt & (cl > 92)).sum()}; over 0.25%: {(rt & (cl > 23)).sum()}; brightness over 0.6: {(rt & (luma > 0.6)).sum()} ({(rt & (luma > 0.6)).sum() / rt.sum():.1%})")
gm = nrt & (goggles == 1) & (luma >= 0)
print(f"  for scale, goggle frames (the game's own picture): brightness median {np.median(luma[gm]):.3f}; ray-traced frames: {np.median(luma[rt]):.3f}")

print("\n== 7. inside-out character triangles ==")
inw, tri = col("inwardSkinnedTriangles")[:m], col("skinnedTriangles")[:m]
frac = inw / np.maximum(tri, 1)
ok = rt & (tri > 0)
print(f"  ray-traced frames over the 0.5% limit: {(ok & (frac >= 0.005)).sum()} of {ok.sum()} ({(ok & (frac >= 0.005)).sum() / ok.sum():.1%}); over 1%: {(ok & (frac >= 0.01)).sum()}; worst {frac[ok].max():.2%}")
for lo, hi in ((0, 40), (40, 80), (80, 120), (120, 400)):
    s = ok & (sk >= lo) & (sk < hi)
    if s.sum():
        print(f"    character meshes {lo}-{hi}: {s.sum()} frames, median share {np.median(frac[s]):.2%}, over limit {(s & (frac >= 0.005)).sum() / s.sum():.0%}")

print("\n== 8. scene rebuilds while playing ==")
sreb = col("staticBlasRebuilds")[:m]
print(f"  static mesh rebuilds on ray-traced frames: total {int(sreb[rt].sum())}, frames with any {(rt & (sreb > 0)).sum()} ({(rt & (sreb > 0)).sum() / rt.sum():.1%}); static mesh builds total {int(sb[rt].sum())}, frames with over 20: {(rt & builds_heavy).sum()}")
print(f"  camera cuts {int(cut.sum())}; 'scene cleared' is logged by the proxy, see proxy log")

print("\n== 9. memory ==")
print(f"  video memory used MB: median {np.median(col('vramUsedMB')[:m][rt]):.0f}, max {col('vramUsedMB')[:m].max():.0f}; texture memory max {col('textureMemoryMB')[:m].max():.0f} of budget {np.median(col('textureBudgetMB')[:m]):.0f}")
