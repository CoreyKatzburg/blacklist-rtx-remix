"""Hole analysis at one stop: capture depth vs Remix depth.pfm vs the game's depth, and a test of the
"same-mesh copies are written once" reading: copies whose captured points are identical across draws with
different gLocalToWorld (vs c5..c7) are re-placed at each draw's own transform, and the hole is measured again.

usage: cap_replace.py <draws.tsv> <export.json> <remix_depth.pfm> <game_depth.pfm> [x0 x1]
       cap_replace.py --self-test"""
import sys, os, hashlib
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cap_join_material as J  # noqa: E402

W, H = 320, 180


def pfm(p):
    with open(p, "rb") as f:
        k = f.readline().strip(); w, h = map(int, f.readline().split()); s = float(f.readline())
        a = np.fromfile(f, "<f4" if s < 0 else ">f4").reshape(h, w, 3 if k == b"PF" else 1)
    a = np.flipud(a)[..., 0]
    ys = ((np.arange(H) + 0.5) * a.shape[0] / H).astype(int); xs = ((np.arange(W) + 0.5) * a.shape[1] / W).astype(int)
    return a[np.ix_(ys, xs)]


def tris(idx, cnt):
    idx = np.asarray(idx); cnt = np.asarray(cnt)
    if (cnt == 3).all():
        return idx.reshape(-1, 3)
    out, o = [], 0
    for n in cnt:
        out += [(idx[o], idx[o + j], idx[o + j + 1]) for j in range(1, n - 1)]
        o += n
    return np.array(out).reshape(-1, 3)


def raster(P, T, cam, depth, ids, tag):
    view, proj = cam
    C = np.c_[P, np.ones(len(P))] @ view @ proj
    w = C[:, 3]
    NEAR = 1.0
    for t in T:
        V = C[t]
        if (V[:, 3] < NEAR).all():
            continue
        if (V[:, 3] < NEAR).any():  # clip the triangle to w >= NEAR, fan the polygon
            poly = []
            for a, b in ((0, 1), (1, 2), (2, 0)):
                pa, pb = V[a], V[b]
                if pa[3] >= NEAR:
                    poly.append(pa)
                if (pa[3] >= NEAR) != (pb[3] >= NEAR):
                    s = (NEAR - pa[3]) / (pb[3] - pa[3])
                    poly.append(pa + s * (pb - pa))
            subs = [np.array([poly[0], poly[i], poly[i + 1]]) for i in range(1, len(poly) - 1)]
        else:
            subs = [V]
        for S in subs:
            raster_tri(S, depth, ids, tag)


def raster_tri(S, depth, ids, tag):
    if True:
        zz = S[:, 3]
        x = (S[:, 0] / zz * 0.5 + 0.5) * W
        y = (0.5 - S[:, 1] / zz * 0.5) * H
        x0, x1 = max(int(np.floor(x.min())), 0), min(int(np.ceil(x.max())), W - 1)
        y0, y1 = max(int(np.floor(y.min())), 0), min(int(np.ceil(y.max())), H - 1)
        if x1 < x0 or y1 < y0:
            return
        den = (y[1] - y[2]) * (x[0] - x[2]) + (x[2] - x[1]) * (y[0] - y[2])
        if abs(den) < 1e-12:
            return
        gx, gy = np.meshgrid(np.arange(x0, x1 + 1) + 0.5, np.arange(y0, y1 + 1) + 0.5)
        a = ((y[1] - y[2]) * (gx - x[2]) + (x[2] - x[1]) * (gy - y[2])) / den
        b = ((y[2] - y[0]) * (gx - x[2]) + (x[0] - x[2]) * (gy - y[2])) / den
        c = 1 - a - b
        inside = (a >= 0) & (b >= 0) & (c >= 0)
        if not inside.any():
            return
        zi = 1.0 / (a / zz[0] + b / zz[1] + c / zz[2])
        sd = depth[y0:y1 + 1, x0:x1 + 1]; si = ids[y0:y1 + 1, x0:x1 + 1]
        win = inside & (zi < sd)
        sd[win] = zi[win]; si[win] = tag


def l2w(kv):
    v = np.array(kv["vsc0-15"].split(","), float).reshape(16, 4)
    M = np.eye(4); M[:3, :] = v[5:8, :]
    return M


def hole(C, R, x0, x1):
    m = np.zeros((H, W), bool)
    m[:, x0:x1] = True
    return m & (C > R * 1.15 + 30)


def render(draws, insts, match, cam, place):
    """place[j] = 4x4 applied to the copy's points for draw j (identity = as captured)."""
    depth = np.full((H, W), np.inf); ids = np.full((H, W), -1, np.int32)
    for j, k in match.items():
        P = np.asarray(insts[k]["points"], float)
        P = (np.c_[P, np.ones(len(P))] @ place.get(j, np.eye(4)).T)[:, :3]
        raster(P, insts[k]["tris"], cam, depth, ids, draws[j]["seq"])
    return depth, ids


def self_test():
    # One mesh (a 200x200 quad facing the camera) drawn twice at x = -300 and x = +300, but the capture holds both
    # copies at -300. Re-placing must fill the hole at +300; leaving it must not.
    view = np.eye(4); proj = np.diag([1.0, 1.7778, 1.0, 0.0]); proj[2, 3] = 1.0; proj[3, 2] = -2.0
    cam = (view, proj)
    quad = np.array([[-100, -100, 1000], [100, -100, 1000], [100, 100, 1000], [-100, 100, 1000.]])
    T = np.array([[0, 1, 2], [0, 2, 3]])
    L = lambda x: np.array([[1, 0, 0, x], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1.]])
    piled = quad + [-300, 0, 0]
    insts = [{"points": piled, "tris": T}, {"points": piled, "tris": T}]
    draws = [{"seq": 1, "L": L(-300)}, {"seq": 2, "L": L(300)}]
    truth, _ = render(draws, [{"points": quad + [-300, 0, 0], "tris": T}, {"points": quad + [300, 0, 0], "tris": T}],
                      {0: 0, 1: 1}, cam, {})
    cap, _ = render(draws, insts, {0: 0, 1: 1}, cam, {})
    h0 = hole(cap, truth, 0, W).sum()
    fixed, _ = render(draws, insts, {0: 0, 1: 1}, cam, {1: draws[1]["L"] @ np.linalg.inv(draws[0]["L"])})
    h1 = hole(fixed, truth, 0, W).sum()
    assert h0 > 50 and h1 == 0, (h0, h1)
    # A wrong source choice (re-place draw 0 from draw 1) must leave a hole.
    bad, _ = render(draws, insts, {0: 0, 1: 1}, cam, {0: draws[0]["L"] @ np.linalg.inv(draws[1]["L"])})
    assert hole(bad, truth, 0, W).sum() > 50
    print(f"cap_replace self-test: PASS (hole {h0} px piled, {h1} px re-placed)")


def main():
    if "--self-test" in sys.argv:
        return self_test()
    draws_raw, cam = J.parse_draws(sys.argv[1])
    import json
    exp = json.load(open(sys.argv[2]))
    R = pfm(sys.argv[3]); G = pfm(sys.argv[4])
    x0, x1 = (int(sys.argv[5]), int(sys.argv[6])) if len(sys.argv) > 6 else (80, 185)
    draws, insts = J.prep(draws_raw, exp)
    for d, kv in zip(draws, draws_raw):
        d["L"] = l2w(kv)
    for i, e in zip(insts, exp["instances"]):
        i["tris"] = tris(e["idx"], e["cnt"])
    match, miss = J.join(draws, insts)
    C, ids = render(draws, insts, match, cam, {})
    hm = hole(C, R, x0, x1)
    band = np.zeros((H, W), bool); band[:, x0:x1] = True
    agree = np.abs(R - G) <= 0.02 * G + 2
    print(f"hole (capture > 1.15 x Remix + 30) in x {x0}..{x1}: {hm.sum()} px = {hm.sum() / band.sum() * 100:.1f}% of the band, "
          f"{hm.sum() / (W * H) * 100:.2f}% of the frame (whole frame: {hole(C, R, 0, W).sum() / (W * H) * 100:.2f}%)")
    print(f"Remix depth agrees with game depth (2% + 2): {agree[hm].mean() * 100:.1f}% of hole px, {agree[band].mean() * 100:.1f}% of band")
    # Which copies the capture shows behind the hole.
    u, c = np.unique(ids[hm], return_counts=True)
    print("capture shows behind the hole (draw seq: px):", ", ".join(f"{s}:{n}" for s, n in sorted(zip(u, c), key=lambda t: -t[1])[:10]))
    # Groups of draws whose copies hold identical points while their transforms differ.
    groups = {}
    for j, k in match.items():
        h = hashlib.sha1(np.asarray(insts[k]["points"], np.float32).tobytes()).hexdigest()
        groups.setdefault(h, []).append(j)
    piled = {h: js for h, js in groups.items() if len(js) > 1 and
             max(np.abs(draws[a]["L"][:3, 3] - draws[js[0]]["L"][:3, 3]).max() for a in js) > 1.0}
    print(f"copy groups with identical points across draws at different places: {len(piled)}, "
          f"draws in them: {sum(len(v) for v in piled.values())}")
    # Choose each group's source draw by depth fit against Remix, then re-place the others.
    place = {}
    for h, js in piled.items():
        best = None
        for s in js:
            pl = {j: draws[j]["L"] @ np.linalg.inv(draws[s]["L"]) for j in js if j != s}
            dd = np.full((H, W), np.inf); ii = np.full((H, W), -1, np.int32)
            for j in js:
                P = np.asarray(insts[match[j]]["points"], float)
                P = (np.c_[P, np.ones(len(P))] @ pl.get(j, np.eye(4)).T)[:, :3]
                raster(P, insts[match[j]]["tris"], cam, dd, ii, 1)
            vis = np.isfinite(dd)
            fit = (np.abs(dd - R) <= 0.03 * R + 3)[vis].sum() - 5 * (dd < R * 0.9 - 20)[vis].sum()
            if best is None or fit > best[0]:
                best = (fit, s, pl)
        place.update(best[2])
        seqs = [draws[j]["seq"] for j in js]
        print(f"  group {seqs[:8]}{'...' if len(seqs) > 8 else ''} n={len(js)} prims {draws[js[0]]['prims']} source {draws[best[1]]['seq']}")
    C2, ids2 = render(draws, insts, match, cam, place)
    hm2 = hole(C2, R, x0, x1)
    print(f"after re-placing: hole {hm2.sum()} px = {hm2.sum() / band.sum() * 100:.1f}% of the band, "
          f"whole frame {hole(C2, R, 0, W).sum() / (W * H) * 100:.2f}%")
    fill = hm & ~hm2
    u, c = np.unique(ids2[fill], return_counts=True)
    print("re-placed draws that fill the hole (seq: px):", ", ".join(f"{s}:{n}" for s, n in sorted(zip(u, c), key=lambda t: -t[1])[:12]))
    u, c = np.unique(ids2[hm2], return_counts=True)
    print("still behind the remaining hole (seq: px):", ", ".join(f"{s}:{n}" for s, n in sorted(zip(u, c), key=lambda t: -t[1])[:8]))
    unb = {draws[j]["seq"] for j in match if draws[j].get("unbound")}
    inpile = {draws[j]["seq"] for js in piled.values() for j in js}
    print("unbound-copy draws:", sorted(unb), " of them in piled groups:", sorted(unb & inpile))
    base = os.path.splitext(sys.argv[2])[0]
    np.save(base + "_replaced_ids.npy", ids2)
    np.save(base + "_replaced_depth.npy", C2)
    import json as _j
    _j.dump({str(draws[j]["seq"]): {"unbound": bool(draws[j].get("unbound")), "t0": draws[j]["sig"] is not None,
                                     "albedo": exp["instances"][k]["albedo"], "ps": draws[j]["ps"], "vs": draws[j]["vs"],
                                     "prims": draws[j]["prims"]} for j, k in match.items()},
            open(base + "_drawinfo.json", "w"))


if __name__ == "__main__":
    main()
