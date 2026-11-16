"""Off / on / off reader for blown blocks owned by chosen draws (con_boiler y0_p0 suspects 2 and 3).

Per step (one Remix final.pfm, scored against one game raster), for each draw group:
  blown  = relBlown blocks (crushed_blown_vs_raster.py rule) owned by the group
  luma   = median absolute block luma of the Remix picture over the group's reference blocks (the blocks blown at
           the reference stop), so a shift of the frame median cannot pass for an effect.
Owners come from a block owner map (.npy, from the re-placed capture, see cap_replace.py). A step whose Remix depth
agrees with the owner map's reference depth on under 95% of pixels is NOT MEASURED (a different view).

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
MIN_VIEW_AGREE = 0.95


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
    print("switch_owner_blocks self-test: PASS (4 cases)")


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
            agree = (np.abs(pfm(dp) - ref_depth) <= 0.02 * ref_depth + 2).mean()
            steps[name] = (M.block_luma(M.pb.gate.read_pfm(fp)), agree)
    ref_name = bases[0]
    ref = blown_blocks(steps[ref_name][0], r)
    res = {}
    for name, (f, agree) in steps.items():
        if agree < MIN_VIEW_AGREE:
            print(f"{name}: NOT MEASURED (depth agrees with the owner map on {agree * 100:.1f}% < 95%)")
            continue
        res[name] = step_numbers(f, r, owner, groups, ref)
        print(f"{name}: frame median {np.median(f):.3f}  " + "  ".join(
            f"{g}: blown {c} luma {l:.3f}" for g, (c, l) in res[name].items()))
    if test in res and all(b in res for b in bases):
        for g in groups:
            v, rel, lim = verdict([res[b][g] for b in bases], res[test][g])
            print(f"{test} vs {'/'.join(bases)}  {g}: {v}  (luma {rel * 100:+.0f}%, limit {lim * 100:.0f}%)")
    else:
        print(f"{test}: NOT MEASURED (a base or the test step is missing)")


if __name__ == "__main__":
    main()
