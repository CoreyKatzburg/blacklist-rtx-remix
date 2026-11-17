"""Off / on / off reader for blown blocks owned by chosen draws (con_boiler y0_p0 suspects 2 and 3).

Per step (one Remix final.pfm, scored against one game raster), for each draw group:
  blown  = relBlown blocks (crushed_blown_vs_raster.py rule) owned by the group
  luma   = median absolute block luma of the Remix picture over the group's reference blocks (the blocks blown at
           the reference stop), so a shift of the frame median cannot pass for an effect.
Owners come from a block owner map (.npy, from the re-placed capture, see cap_replace.py), usually taken in another
launch. Two view checks, on the depth resampled to 320x180:
  - a step whose depth agrees with the first base's depth on under 98% of pixels is NOT MEASURED (the view moved
    within the launch; 194054 steps read 98.9 to 99.3%, a one-pixel shift reads 94.4% or less);
  - a group whose own pixels (blocks the owner map gives to its draws) agree with the owner map's reference depth on
    under 92% is NOT MEASURED in that step (its owners would be wrong; 194054 groups read 94.5 to 98.8%, a near
    surface no group owns, such as draw 143, does not count).

Verdict for a test step between bases: EFFECT when the group's luma moves by more than max(20%, 2x the spread
between the bases) against the mean of the bases and the blown count moves the same way; NO EFFECT when it moves
by less than that; otherwise UNCLEAR. A count change alone never counts.

usage: switch_owner_blocks.py <owner.npy> <ref_depth.pfm> <raster.pfm> <step>=<final.pfm>,<depth.pfm> ... \
           --group name=seq,seq ... --base step,step[,step] --test step
       switch_owner_blocks.py --self-test"""
import sys, os
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crushed_blown_vs_raster as M  # noqa: E402
from cap_replace import pfm  # noqa: E402

MIN_EFFECT = 0.20
MIN_STEP_AGREE = 0.98    # step depth against the first base's depth, all pixels
MIN_GROUP_AGREE = 0.92   # a group's pixels against the owner map's reference depth


def depth_agree(a, b):
    return np.abs(a - b) <= 0.02 * b + 2


def owner_pixels(owner, full, shape):
    """Block owner map (blocks of the HUD-cropped full-size picture) -> owner per pixel of the resampled depth."""
    y0, x0 = round(full[0] * M.pb.DEFAULT_HUD_MARGIN_Y), round(full[1] * M.pb.DEFAULT_HUD_MARGIN_X)
    ys = ((np.arange(shape[0]) + 0.5) * full[0] / shape[0]).astype(int)
    xs = ((np.arange(shape[1]) + 0.5) * full[1] / shape[1]).astype(int)
    bi, bj = (ys - y0) // M.BLOCK, (xs - x0) // M.BLOCK
    vi, vj = (bi >= 0) & (bi < owner.shape[0]), (bj >= 0) & (bj < owner.shape[1])
    out = np.full(shape, -1)
    out[np.ix_(vi, vj)] = owner[np.ix_(bi[vi], bj[vj])]
    return out


def view_check(depth, base_depth, ref_depth, owner_px, groups):
    """-> (step agreement with the base, {group: agreement with the owner map on its pixels}, step ok, {group: ok})"""
    step = float(depth_agree(depth, base_depth).mean())
    a = depth_agree(depth, ref_depth)
    grp = {g: float(a[np.isin(owner_px, s)].mean()) if np.isin(owner_px, s).any() else 0.0 for g, s in groups.items()}
    return step, grp, step >= MIN_STEP_AGREE, {g: v >= MIN_GROUP_AGREE for g, v in grp.items()}


def blown_blocks(f, r):
    fn, rn = f / max(np.median(f), 1e-4), r / max(np.median(r), 1e-4)
    return (rn <= M.REL_GAME_DARK) & (fn >= M.REL_REMIX_LIGHT)


def step_numbers(f, r, owner, groups, ref):
    """f, r: block luma (Remix, game). ref: reference blown mask. -> {group: (blown count, abs luma on ref blocks)}"""
    b = blown_blocks(f, r)
    out = {}
    for name, seqs in groups.items():
        own = np.isin(owner, seqs)
        out[name] = (int((b & own).sum()), float(np.median(f[ref & own])) if (ref & own).any() else float("nan"))
    return out


def verdict(bases, test):
    """bases: [(count, luma)], test: (count, luma)."""
    lb = np.array([x[1] for x in bases]); cb = np.array([x[0] for x in bases])
    mean = lb.mean()
    spread = (lb.max() - lb.min()) / max(mean, 1e-4)
    rel = (test[1] - mean) / max(mean, 1e-4)
    limit = max(MIN_EFFECT, 2 * spread)
    if abs(rel) <= limit:
        return "NO EFFECT", rel, limit
    count_same_way = (test[0] < cb.min()) if rel < 0 else (test[0] > cb.max())
    return ("EFFECT" if count_same_way else "UNCLEAR"), rel, limit


def self_test():
    rng = np.random.default_rng(3)
    H, W = 40, 60
    r = np.full((H, W), 0.2) + rng.normal(0, 0.005, (H, W)); r[0:10, :] = 0.02          # game: dark band on top
    owner = np.full((H, W), 1); owner[0:10, 0:30] = 133; owner[0:10, 30:60] = 183
    base = np.full((H, W), 0.2) + rng.normal(0, 0.005, (H, W)); base[0:10, :] = 0.40     # Remix: band blown
    G = {"shell": [133], "tanks": [183]}
    ref = blown_blocks(base, r)
    nb = step_numbers(base, r, owner, G, ref)
    assert nb["shell"][0] == 300 and nb["tanks"][0] == 300, nb
    # 1. The light lit only the shell: hiding it darkens the shell's blocks; it must read EFFECT for the shell only.
    lit_off = base.copy(); lit_off[0:10, 0:30] = 0.05
    n = step_numbers(lit_off, r, owner, G, ref)
    assert verdict([nb["shell"], nb["shell"]], n["shell"])[0] == "EFFECT", n
    assert verdict([nb["tanks"], nb["tanks"]], n["tanks"])[0] == "NO EFFECT", n
    # 2. Fake: the whole picture dims evenly (exposure). relBlown counts stay, absolute luma falls 50%, so the luma
    # rule alone would call it; the count must not move, so it must read UNCLEAR, not EFFECT.
    dim = base * 0.5
    n = step_numbers(dim, r, owner, G, ref)
    assert n["shell"][0] == nb["shell"][0], n
    assert verdict([nb["shell"], nb["shell"]], n["shell"])[0] == "UNCLEAR", n
    # 3. Fake: the same luma, but the frame median rises 10% elsewhere: the count drops, luma does not, NO EFFECT.
    brighter = base.copy(); brighter[10:, :] *= 1.4
    n = step_numbers(brighter, r, owner, G, ref)
    assert n["shell"][0] < nb["shell"][0], n
    assert verdict([nb["shell"], nb["shell"]], n["shell"])[0] == "NO EFFECT", n
    # 4. Noisy bases: a 25% luma change inside a 2x base spread of 30% must read NO EFFECT.
    noisy = [(300, 0.40), (300, 0.52)]
    assert verdict(noisy, (200, 0.36))[0] == "NO EFFECT"
    # View checks on a blocky depth scene (cells of 4x4 pixels at random depths), owner map: 133 on top, 183 a tank,
    # 143 a near slab owned by no group.
    Hd, Wd = 180, 320
    dep = np.kron(rng.uniform(100, 2000, (Hd // 4, Wd // 4)), np.ones((4, 4)))
    opx = np.full((Hd, Wd), 1); opx[0:40, :] = 133; opx[100:140, 40:80] = 183; opx[60:180, 220:320] = 143
    VG = {"shell": [133], "tanks": [183]}
    # 5. The same view as the owner map: measured.
    s, g, ok, gok = view_check(dep, dep, dep, opx, VG)
    assert ok and all(gok.values()), (s, g)
    # 6. Fake: the view moved by one pixel within the launch: the step must read NOT MEASURED.
    s, g, ok, gok = view_check(np.roll(dep, 1, 1), dep, dep, opx, VG)
    assert not ok, s
    # 7. The near slab no group owns differs from the owner map (Sam stands a little off): still measured.
    ref_off = dep.copy(); ref_off[opx == 143] *= 3
    s, g, ok, gok = view_check(dep, dep, ref_off, opx, VG)
    assert ok and all(gok.values()), (s, g)
    assert depth_agree(dep, ref_off).mean() < 0.95   # the old whole-frame 95% rule would have refused it
    # 8. Fake: the disagreement sits on a scored draw (the tank moved): that group must read NOT MEASURED.
    ref_tank = dep.copy(); ref_tank[opx == 183] *= 3
    s, g, ok, gok = view_check(dep, dep, ref_tank, opx, VG)
    assert ok and gok["shell"] and not gok["tanks"], (s, g)
    # 9. owner_pixels: block (0, 0) of the HUD-cropped picture lands at the crop's top-left pixel, not at (0, 0).
    own = np.zeros((75, 134), int); own[0, 0] = 7
    px = owner_pixels(own, (1440, 2560), (Hd, Wd))
    ys, xs = np.nonzero(px == 7)
    assert px[0, 0] == -1 and ys.min() * 8 >= round(1440 * M.pb.DEFAULT_HUD_MARGIN_Y) - 8 and xs.size == 4, (ys, xs)
    print("switch_owner_blocks self-test: PASS (9 cases)")


def main():
    if "--self-test" in sys.argv:
        return self_test()
    a = sys.argv[1:]
    owner = np.load(a[0]); ref_depth = pfm(a[1])
    r = M.block_luma(M.pb.gate.read_pfm(a[2]))
    groups = {}
    for i, x in enumerate(a):
        if x == "--group":
            name, seqs = a[i + 1].split("=")
            groups[name] = [int(s) for s in seqs.split(",")]
    bases = a[a.index("--base") + 1].split(","); test = a[a.index("--test") + 1]
    steps = {}
    for x in a[3:]:
        if "=" in x and not x.startswith("-") and "," in x.split("=", 1)[1] and x.split("=", 1)[1].lower().endswith(".pfm"):
            name, files = x.split("=", 1)
            fp, dp = files.split(",")
            img = M.pb.gate.read_pfm(fp)
            steps[name] = (M.block_luma(img), pfm(dp), img.shape[:2])
    owner_px = owner_pixels(owner, steps[bases[0]][2], ref_depth.shape)
    ref = blown_blocks(steps[bases[0]][0], r)
    res = {}
    for name, (f, dep, _) in steps.items():
        s, g, ok, gok = view_check(dep, steps[bases[0]][1], ref_depth, owner_px, groups)
        view = f"depth vs {bases[0]} {s * 100:.1f}%, " + ", ".join(f"{k} vs owner map {v * 100:.1f}%" for k, v in g.items())
        if not ok:
            print(f"{name}: NOT MEASURED ({view}; under {MIN_STEP_AGREE * 100:.0f}% against {bases[0]})")
            continue
        n = step_numbers(f, r, owner, groups, ref)
        res[name] = {k: v for k, v in n.items() if gok[k]}
        print(f"{name}: frame median {np.median(f):.3f}  ({view})  " + "  ".join(
            f"{k}: blown {c} luma {l:.3f}" if gok[k] else f"{k}: NOT MEASURED (under {MIN_GROUP_AGREE * 100:.0f}%)"
            for k, (c, l) in n.items()))
    for g in groups:
        if all(st in res and g in res[st] for st in bases + [test]):
            v, rel, lim = verdict([res[b][g] for b in bases], res[test][g])
            print(f"{test} vs {'/'.join(bases)}  {g}: {v}  (luma {rel * 100:+.0f}%, limit {lim * 100:.0f}%)")
        else:
            print(f"{test} vs {'/'.join(bases)}  {g}: NOT MEASURED (a base or the test step is not measured)")


if __name__ == "__main__":
    main()
