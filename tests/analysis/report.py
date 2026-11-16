"""Prints summary tables from session.pkl (scratch analysis)."""
import pickle
import sys

import numpy as np

d = pickle.load(open(sys.argv[1], "rb"))
A, cols = d["A"], list(d["cols"])
col = lambda k: A[:, cols.index(k)]
n = len(A)
rt = col("rayTraced") > 0.5
ft = col("frameTimeMs")
print(f"frames {n}, ray traced {rt.sum()} ({rt.mean():.1%}), not ray traced {(~rt).sum()}")
for name, m in (("ray traced", rt), ("not ray traced", ~rt)):
    v = ft[m]
    if not len(v):
        print(f"  {name}: no frames")
        continue
    print(f"  {name}: frame ms median {np.median(v):.1f}, mean {v.mean():.1f}, p90 {np.percentile(v, 90):.1f}, p99 {np.percentile(v, 99):.1f}, "
          f"FPS at median {1000 / np.median(v):.1f}; over 50 ms: {(v > 50).sum()}")
print(f"  total minutes {ft.sum() / 60000:.1f}")

print("\n== per-minute medians (ray-traced frames) ==")
tmin = np.cumsum(ft) / 60000.0
for m in range(int(tmin[-1]) + 1):
    s = (tmin >= m) & (tmin < m + 1) & rt
    if s.sum() < 30:
        print(f"  min {m:2d}: {s.sum()} ray-traced frames"); continue
    print(f"  min {m:2d}: frame {np.median(ft[s]):5.1f} ms  gpuTime {np.median(col('gpuTimeMs')[s]):5.1f}  instances {np.median(col('instances')[s]):5.0f}  "
          f"skinned {np.median(col('skinnedInstances')[s]):3.0f}  luma {np.median(d['luma_mean'][s]):.3f}  white px {np.median(d['luma_white'][s]):5.0f}  "
          f"clipped {np.median(col('thumbClipped')[s]):5.0f}  inward {np.median(col('inwardSkinnedTriangles')[s]):5.0f}/{np.median(col('skinnedTriangles')[s]):6.0f}")

print("\n== steps on ray-traced frames: median GPU ms when present, share of frames present, mean per frame ==")
table = []
for path, v in d["passes"].items():
    idx = v[:, 0].astype(int)
    keep = rt[idx]
    g, c, cnt = v[keep, 1], v[keep, 2], v[keep, 3]
    if not len(g):
        continue
    ok = g >= 0
    table.append((g[ok].sum() / rt.sum() if ok.any() else 0.0, path, np.median(g[ok]) if ok.any() else -1, np.percentile(g[ok], 99) if ok.any() else -1,
                  np.median(c), c.sum() / rt.sum(), len(g) / rt.sum(), np.median(cnt), (~ok).sum()))
table.sort(reverse=True)
print(f"{'mean gpu/frame':>14} {'median':>7} {'p99':>7} {'cpu med':>7} {'cpu/frame':>9} {'present':>7} {'runs':>5} {'untimed':>7}  step")
for mean_g, path, med, p99, cmed, cmean, share, cnt, untimed in table:
    print(f"{mean_g:14.3f} {med:7.3f} {p99:7.3f} {cmed:7.3f} {cmean:9.3f} {share:7.1%} {cnt:5.0f} {untimed:7d}  {path}")

print("\n== steps on frames that were not ray traced ==")
table = []
for path, v in d["passes"].items():
    idx = v[:, 0].astype(int)
    keep = ~rt[idx]
    g = v[keep, 1]
    if len(g):
        ok = g >= 0
        table.append((g[ok].sum() / max((~rt).sum(), 1), path, np.median(g[ok]) if ok.any() else -1, len(g) / max((~rt).sum(), 1)))
table.sort(reverse=True)
for mean_g, path, med, share in table[:12]:
    print(f"{mean_g:14.3f} {med:7.3f} {share:7.1%}  {path}")

print("\n== recorder cost ==")
tc = col("telemetryCpuMs")
print(f"  telemetryCpuMs ray traced: median {np.median(tc[rt]):.3f}, mean {tc[rt].mean():.3f}, p99 {np.percentile(tc[rt], 99):.3f}, max {tc[rt].max():.2f}")
if (~rt).any():
    print(f"  telemetryCpuMs not ray traced: median {np.median(tc[~rt]):.3f}, mean {tc[~rt].mean():.3f}, p99 {np.percentile(tc[~rt], 99):.3f}")

print("\n== skips (draws kept out of ray tracing) ==")
for reason, v in sorted(d["skips"].items(), key=lambda kv: -kv[1][:, 1].sum()):
    print(f"  {reason:22s} total {int(v[:, 1].sum()):9d}  frames {len(v):6d} ({len(v) / n:.1%})  median per frame {np.median(v[:, 1]):.0f}  max {int(v[:, 1].max())}")

print("\n== materials and textures ==")
rep, leg = col("replacedMaterialDraws"), col("legacyMaterialDraws")
print(f"  draws per ray-traced frame: replaced median {np.median(rep[rt]):.0f}, plain game texture median {np.median(leg[rt]):.0f} "
      f"(fallback share {leg[rt].sum() / max(rep[rt].sum() + leg[rt].sum(), 1):.1%})")
print(f"  distinct fallback textures listed: {len(set(h for _, h in d['legacy']))}")
print(f"  texturesDemoted: total {int(col('texturesDemoted').sum())}, frames with any {(col('texturesDemoted') > 0).sum()}")
print(f"  textureBudgetMB median {np.median(col('textureBudgetMB')):.0f}, textureMemoryMB median {np.median(col('textureMemoryMB')):.0f} max {col('textureMemoryMB').max():.0f}, "
      f"vramUsedMB median {np.median(col('vramUsedMB')):.0f} max {col('vramUsedMB').max():.0f}")

print("\n== picture ==")
has = d["luma_mean"] >= 0
print(f"  frames with gray thumbnail {has.sum()} of {n}; ray traced with {(has & rt).sum()} of {rt.sum()}; not ray traced with {(has & ~rt).sum()} of {(~rt).sum()}")
hr = d["rgb_mean"][:, 0] >= 0
print(f"  frames with color thumbnail {hr.sum()}")
for name, m in (("ray traced", rt & has), ("not ray traced", ~rt & has)):
    if m.any():
        print(f"  {name}: mean brightness median {np.median(d['luma_mean'][m]):.3f} (p10 {np.percentile(d['luma_mean'][m], 10):.3f}, p90 {np.percentile(d['luma_mean'][m], 90):.3f}); "
              f"near-white pixels of 9216: median {np.median(d['luma_white'][m]):.0f}, p90 {np.percentile(d['luma_white'][m], 90):.0f}, max {d['luma_white'][m].max()}")
        rgb = d["rgb_mean"][m & hr]
        if len(rgb):
            print(f"    mean color r {rgb[:, 0].mean():.3f} g {rgb[:, 1].mean():.3f} b {rgb[:, 2].mean():.3f}")
nf, cl = col("thumbNonFinite"), col("thumbClipped")
print(f"  thumbNonFinite: frames with any {(nf > 0).sum()}, max {int(nf.max())}")
print(f"  thumbClipped (ray traced): median {np.median(cl[rt]):.0f}, p90 {np.percentile(cl[rt], 90):.0f}, max {int(cl[rt].max())}, frames over 92 px (1%) {(cl[rt] > 92).sum()}")

print("\n== characters ==")
sk, inw, tri = col("skinnedInstances"), col("inwardSkinnedTriangles"), col("skinnedTriangles")
print(f"  ray-traced frames with 0 skinned instances: {(sk[rt] == 0).sum()} of {rt.sum()}")
print(f"  inward triangles median {np.median(inw[rt]):.0f}, share of skinned triangles median {np.median(inw[rt] / np.maximum(tri[rt], 1)):.2%}, p90 {np.percentile(inw[rt] / np.maximum(tri[rt], 1), 90):.2%}")
print(f"  unorderedInstances median {np.median(col('unorderedInstances')[rt]):.0f}, cameraCuts {int(col('cameraCut').sum())}, staticBlasRebuilds total {int(col('staticBlasRebuilds').sum())}")
