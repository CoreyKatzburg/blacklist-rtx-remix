"""Join proxy-sent draws to Remix capture instances by mesh size and albedo texture signature, then list the
draws whose screen position falls in a region and that have no capture instance.

The proxy dump has texture pointers, not Remix's content hashes, so the texture key is the DDS signature
(width, height, format fourcc, mip levels) of the draw's t0 against the capture's albedo .dds. The mesh key is
(prims, verts) against (triangles, points). Ties among candidates are broken by distance from the draw's
gLocalToWorld origin (vs constants c5..c7, column 3) to the instance's bounding box. Capture points are game
world positions (the instance xform only mirrors x).

usage: cap_join_material.py <draws.tsv> <export.json> [x0 x1] [--drop K]   (screen x in 320x180)
       cap_join_material.py --self-test"""
import json, os, struct, sys
import numpy as np

W, H = 320, 180


def parse_draws(path):
    draws, cam = [], None
    for line in open(path, encoding="utf-8", errors="replace"):
        f = line.rstrip("\r\n").split("\t")
        kv = dict(x.split("=", 1) for x in f[1:] if "=" in x)
        if f[0] == "campub" and cam is None:
            cam = (np.array(kv["view"].split(","), float).reshape(4, 4), np.array(kv["proj"].split(","), float).reshape(4, 4))
        elif f[0] == "draw" and kv.get("action") == "sent" and kv.get("scene") == "1":
            draws.append(kv)
    return draws, cam


def tex_sig(field):
    """t0=ptr,tyN,WxH,fFOURCC,uN,lN,srgbN -> (W, H, fourcc, levels)."""
    if not field or field == "0":
        return None
    p = field.split(",")
    w, h = (int(v) for v in p[2].split("x"))
    lv = next((int(x[1:]) for x in p if x.startswith("l") and x[1:].isdigit()), None)
    return (w, h, int(p[3][1:]), lv)


def dds_sig(path, cache={}):
    if not path:
        return None
    if path not in cache:
        try:
            with open(path, "rb") as fh:
                hd = fh.read(128)
            h, w, mips = struct.unpack_from("<I", hd, 12)[0], struct.unpack_from("<I", hd, 16)[0], struct.unpack_from("<I", hd, 28)[0]
            cache[path] = (w, h, struct.unpack_from("<I", hd, 84)[0], max(mips, 1))
        except OSError:
            cache[path] = None
    return cache[path]


def draw_origin(kv):
    v = np.array(kv["vsc0-15"].split(","), float).reshape(16, 4)
    return v[5:8, 3].copy()


def inst_bbox(inst):
    P = np.array(inst["points"], float)
    return P.min(0), P.max(0)


def join(draws, insts):
    """Return {draw index: instance index} and the list of unmatched draw indices."""
    pool = {}
    for k, i in enumerate(insts):
        key = (len(i["idx"]) // 3, len(i["points"]), i["sig"])
        pool.setdefault(key, []).append(k)
    # Nearest pairs first across all draws of one key, so draw order cannot steal another draw's copy.
    pairs = []
    for j, d in enumerate(draws):
        o = d["origin"]
        for k in pool.get((d["prims"], d["verts"], d["sig"]), []):
            bb = insts[k]["bb"]
            pairs.append((float(np.linalg.norm(np.maximum(0, np.maximum(bb[0] - o, o - bb[1])))), j, k))
    match, used = {}, set()
    for dist, j, k in sorted(pairs):
        if j in match or k in used:
            continue
        match[j] = k
        used.add(k)
        draws[j]["bbdist"] = dist
    # Second pass: a draw with a texture whose capture copy has no material binding (sig None) still has an instance.
    pairs = []
    for j, d in enumerate(draws):
        if j in match:
            continue
        o = d["origin"]
        for k in pool.get((d["prims"], d["verts"], None), []):
            if k in used:
                continue
            bb = insts[k]["bb"]
            pairs.append((float(np.linalg.norm(np.maximum(0, np.maximum(bb[0] - o, o - bb[1])))), j, k))
    for dist, j, k in sorted(pairs):
        if j in match or k in used:
            continue
        match[j] = k
        used.add(k)
        draws[j]["bbdist"] = dist
        draws[j]["unbound"] = d_sig = draws[j]["sig"] is not None
    missing = [j for j in range(len(draws)) if j not in match]
    return match, missing


def project(p, cam):
    view, proj = cam
    c = np.r_[p, 1.0] @ view @ proj
    if c[3] <= 1e-3:
        return None
    return ((c[0] / c[3]) * 0.5 + 0.5) * W, (0.5 - (c[1] / c[3]) * 0.5) * H, c[3]


def prep(draws_raw, exp):
    draws = [{"seq": int(k["seq"]), "prims": int(k["prims"]), "verts": int(k.get("verts", 0)), "vs": k.get("vs"),
              "ps": k.get("ps"), "sig": tex_sig(k.get("t0")), "t0": k.get("t0", "-"), "origin": draw_origin(k)} for k in draws_raw]
    insts = []
    for i in exp["instances"]:
        bb = inst_bbox(i)
        insts.append({"idx": i["idx"], "points": i["points"], "sig": dds_sig(i["albedo"]), "bb": bb,
                      "mat": (i["material"] or "-").split("/")[-1][:12], "path": i["path"]})
    return draws, insts


def self_test():
    # Two identical meshes with the same texture at different places, plus one untextured mesh.
    sq = [[0, 0, 0], [100, 0, 0], [0, 100, 0]]
    mk = lambda off, sig: {"idx": [0, 1, 2], "points": [[a + off, b, c] for a, b, c in sq], "sig": sig,
                           "bb": (np.array([off, 0, 0.]), np.array([off + 100, 100, 0.])), "mat": "m", "path": "p"}
    S = (1024, 1024, 827611204, 9)
    insts = [mk(0, S), mk(5000, S), mk(9000, None)]
    dr = lambda seq, off, sig: {"seq": seq, "prims": 1, "verts": 3, "sig": sig, "origin": np.array([off, 0, 0.])}
    draws = [dr(1, 5000, S), dr(2, 0, S), dr(3, 9000, None)]
    m, miss = join([dict(d) for d in draws], insts)
    assert miss == [] and m == {0: 1, 1: 0, 2: 2}, (m, miss)  # each draw gets its own copy, not the first found
    # Fake missing instance: drop the copy at x=5000; exactly draw seq 1 must show up as missing.
    m, miss = join([dict(d) for d in draws], [insts[0], insts[2]])
    assert [draws[j]["seq"] for j in miss] == [1], miss
    # A texture mismatch must not join.
    m, miss = join([dict(draws[0], sig=(512, 512, 827611204, 9))], insts[:2])
    assert miss == [0], miss
    # A textured draw whose only copy lost its binding joins in the second pass and is marked unbound.
    dd = [dict(draws[0], origin=np.array([9000, 0, 0.]))]
    m, miss = join(dd, [insts[2]])
    assert m == {0: 0} and dd[0].get("unbound"), (m, dd)
    print("cap_join_material self-test: PASS")


def main():
    if "--self-test" in sys.argv:
        return self_test()
    args = [a for a in sys.argv[1:]]
    drop = None
    if "--drop" in args:
        i = args.index("--drop"); drop = int(args[i + 1]); del args[i:i + 2]
    draws_raw, cam = parse_draws(args[0])
    exp = json.load(open(args[1]))
    x0, x1 = (int(args[2]), int(args[3])) if len(args) > 3 else (80, 185)
    draws, insts = prep(draws_raw, exp)
    if drop is not None:
        print("fake: dropped capture instance", drop, insts[drop]["path"])
        insts = insts[:drop] + insts[drop + 1:]
    m, miss = join(draws, insts)
    print(f"sent scene draws {len(draws)}  capture instances {len(insts)}  joined {len(m)}  no instance {len(miss)}")
    near = sum(1 for j in m if draws[j]["bbdist"] < 1.0)
    print(f"joined draws whose origin lies inside their instance's bbox: {near} of {len(m)}")
    for j in sorted(m, key=lambda j: draws[j]["seq"]):
        if draws[j]["seq"] in (132, 133, 134):
            d, i = draws[j], insts[m[j]]
            print(f"  check seq {d['seq']}: -> {i['mat']} {i['path'].split('/')[3]} bbdist {d['bbdist']:.0f} "
                  f"bb {i['bb'][0].round(0)}..{i['bb'][1].round(0)}")
    print(f"joined in pass 2 (draw has t0, capture copy has no material): {sum(1 for j in m if draws[j].get('unbound'))}")
    print(f"== every draw whose footprint touches screen x {x0}..{x1} (instance points projected; origin if no instance):")
    view, proj = cam
    for j, d in enumerate(draws):
        if j in m:
            P = np.array(insts[m[j]]["points"], float)
            C = np.c_[P, np.ones(len(P))] @ view @ proj
            ok = C[:, 3] > 1.0
            if not ok.any():
                continue
            sx = (C[ok, 0] / C[ok, 3] * 0.5 + 0.5) * W
            sy = (0.5 - C[ok, 1] / C[ok, 3] * 0.5) * H
            if sx.max() < x0 or sx.min() >= x1 or sy.max() < 0 or sy.min() >= H:
                continue
            fp = f"x {max(sx.min(), -99):.0f}..{min(sx.max(), 419):.0f} y {max(sy.min(), -99):.0f}..{min(sy.max(), 279):.0f} " \
                 f"depth {C[ok, 3].min():.0f}..{C[ok, 3].max():.0f}"
            state = "UNBOUND copy" if d.get("unbound") else ("copy, no texture either side" if d["sig"] is None else "textured copy")
            print(f"  seq {d['seq']:>4} prims {d['prims']:>5} {state:<28} {insts[m[j]]['mat']:<12} {fp}")
        else:
            s = project(d["origin"], cam)
            if s and x0 <= s[0] < x1:
                print(f"  seq {d['seq']:>4} prims {d['prims']:>5} NO INSTANCE origin at ({s[0]:.0f},{s[1]:.0f}) depth {s[2]:.0f}")
    print(f"== draws with no capture instance, origin in screen x {x0}..{x1}:")
    for j in miss:
        d = draws[j]
        s = project(d["origin"], cam)
        if s is None or not (x0 <= s[0] < x1 and -40 <= s[1] < H + 40):
            continue
        print(f"  seq {d['seq']:>5} prims {d['prims']:>5} verts {d['verts']:>5} vs {d['vs']} ps {d['ps']} "
              f"screen ({s[0]:.0f},{s[1]:.0f}) depth {s[2]:.0f} origin {d['origin'].round(0)} t0 {d['t0'].split(',')[2] if d['sig'] else '-'} "
              f"f{d['sig'][2] if d['sig'] else '-'}")
    print("== all draws with no capture instance (seq prims):", " ".join(f"{draws[j]['seq']}:{draws[j]['prims']}" for j in miss))
    keys = {}
    for k in range(len(insts)):
        keys.setdefault((len(insts[k]["idx"]) // 3, len(insts[k]["points"]), insts[k]["sig"]), []).append(k)
    uniq = sum(1 for j in m if len(keys.get((draws[j]["prims"], draws[j]["verts"], None if draws[j].get("unbound") else draws[j]["sig"]), [])) == 1)
    print(f"joined draws whose key matches exactly one instance: {uniq} of {len(m)}")
    used = set(m.values())
    print("== near misses: unused instances with the same mesh size as a missing draw but another texture:")
    for j in miss:
        d = draws[j]
        alt = [k for k in range(len(insts)) if k not in used and len(insts[k]["idx"]) // 3 == d["prims"] and len(insts[k]["points"]) == d["verts"]]
        if alt:
            print(f"  seq {d['seq']} draw t0 {d['sig']} -> " + "; ".join(f"inst {k} {insts[k]['mat']} sig {insts[k]['sig']}" for k in alt[:3]))
    print("unused capture instances:", len(insts) - len(used))


if __name__ == "__main__":
    main()
