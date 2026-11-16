"""Per draw: live albedo debug view (median, spread) over the pixels where the draw is visible, next to whether its
capture copy has a material and that texture's mean luma.

Inputs come from cap_replace.py (<base>_replaced_ids.npy, _replaced_depth.npy, _drawinfo.json). A draw's pixel counts
only where its re-placed capture depth agrees with Remix depth (3% + 3 units), so a copy hidden behind something
else in the live picture does not borrow that thing's albedo. This is a cause check: the original game has no
albedo view, so it cannot show that the player sees anything wrong.

usage: cap_albedo_per_draw.py <export base (no .json)> <remix_depth.pfm> <albedo.pfm> [<albedo.pfm> ...]
       cap_albedo_per_draw.py --self-test"""
import sys, os, json
import numpy as np
from PIL import Image
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cap_replace import pfm  # noqa: E402

LUMA = np.array([0.2126, 0.7152, 0.0722])


def per_draw(ids, dep, R, info, alb, tmean=lambda p: None):
    """Rows (seq, px, state, texMean, [albedo median per grab], p90-p10, ps) and a summary per state."""
    vis = np.abs(dep - R) <= 0.03 * R + 3
    rows = []
    for seq in np.unique(ids[ids >= 0]):
        m = (ids == seq) & vis
        if m.sum() < 25:
            continue
        d = info[str(seq)]
        vals = [a[m] for a in alb]
        state = "UNBOUND" if d["unbound"] else ("bound" if d["albedo"] else "no t0")
        rows.append((int(seq), int(m.sum()), state, tmean(d["albedo"]), [float(np.median(v)) for v in vals],
                     float(np.percentile(vals[0], 90) - np.percentile(vals[0], 10)), d["ps"]))
    summary = {}
    for st in ("UNBOUND", "bound", "no t0"):
        sel = [r for r in rows if r[2] == st]
        if sel:
            px = sum(r[1] for r in sel)
            summary[st] = (len(sel), px, sum(r[1] * r[4][0] for r in sel) / px,
                           sum(r[1] for r in sel if r[4][0] > 0.8) / px)
    return rows, summary


def self_test():
    H, W = 180, 320
    R = np.full((H, W), 500.0)
    ids = np.full((H, W), -1, np.int32); dep = R.copy(); alb = np.full((H, W), 0.2)
    ids[:, 0:100] = 1; alb[:, 0:100] = 1.0                  # draw 1: copy has no material, albedo view white
    ids[:, 100:200] = 2; alb[:, 100:200] = 0.3              # draw 2: bound, albedo view reads its texture
    ids[:, 200:320] = 3; dep[:, 200:320] = 900; alb[:, 200:320] = 1.0   # draw 3: bound copy hidden behind the live wall
    info = {"1": {"unbound": True, "albedo": None, "ps": "a"}, "2": {"unbound": False, "albedo": "t.dds", "ps": "b"},
            "3": {"unbound": False, "albedo": "u.dds", "ps": "c"}}
    rows, s = per_draw(ids, dep, R, info, [alb])
    assert abs(s["UNBOUND"][2] - 1.0) < 1e-6 and s["UNBOUND"][3] == 1.0, s
    # Draw 3's white pixels are not its own (its copy is behind Remix depth), so they must not reach the bound set.
    assert [r[0] for r in rows] == [1, 2] and abs(s["bound"][2] - 0.3) < 1e-6 and s["bound"][3] == 0.0, (rows, s)
    # Must fail without the visibility filter: counting draw 3 would put white pixels into the bound set.
    _, s_bad = per_draw(ids, R.copy(), R, info, [alb])
    assert s_bad["bound"][3] > 0.5, s_bad
    print("cap_albedo_per_draw self-test: PASS (3 cases)")


def main():
    if "--self-test" in sys.argv:
        return self_test()
    base = sys.argv[1]
    ids = np.load(base + "_replaced_ids.npy"); dep = np.load(base + "_replaced_depth.npy")
    info = json.load(open(base + "_drawinfo.json"))
    R = pfm(sys.argv[2])
    alb = []
    for p in sys.argv[3:]:
        with open(p, "rb") as f:
            f.readline(); w, h = map(int, f.readline().split()); s = float(f.readline())
            a = np.flipud(np.fromfile(f, "<f4" if s < 0 else ">f4").reshape(h, w, 3))
        alb.append(np.asarray(Image.fromarray((a @ LUMA).astype(np.float32)).resize((320, 180), Image.BOX)))
    tex = {}

    def tmean(p):
        if p and p not in tex:
            im = Image.open(p); im.load()
            tex[p] = float((np.asarray(im.convert("RGB"), np.float32) / 255 @ LUMA).mean())
        return tex.get(p)

    rows, summary = per_draw(ids, dep, R, info, alb, tmean)
    print("seq   px  copy     texMean  albedoView median per grab        p90-p10  ps")
    for r in sorted(rows, key=lambda r: -r[1]):
        print(f"{r[0]:>4} {r[1]:>5} {r[2]:<8} {('%.3f' % r[3]) if r[3] is not None else '  -  '}  "
              f"{' '.join('%.3f' % v for v in r[4])}   {r[5]:.3f}  {r[6]}")
    for st, (n, px, med, hi) in summary.items():
        print(f"{st}: {n} draws, {px} px, px-weighted albedo median {med:.3f}, share of their px above 0.8: {hi * 100:.0f}%")


if __name__ == "__main__":
    main()
