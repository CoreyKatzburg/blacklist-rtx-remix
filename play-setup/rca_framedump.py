"""Summarise a proxy FrameDump (rca_capture.ps1): the game's render passes and what each one is.
usage: rca_framedump.py <label>_draws.tsv [shader dump root = the game's SYSTEM folder] [--pass N] [--shaders]
A pass = a run of consecutive draws with the same render targets and depth buffer.
Shader constant/sampler names come from the CTAB block in the dumped bytecode (vsdump\\vs_<hash>.bin, psdump\\ps_<hash>.bin)."""
import collections
import os
import struct
import sys

SYSTEM = r"D:\Steam\steamapps\common\Tom Clancy's Splinter Cell Blacklist\src\SYSTEM"
FMT = {21: "A8R8G8B8", 22: "X8R8G8B8", 113: "A16B16G16R16F", 114: "R32F", 111: "R16F", 112: "G16R16F", 116: "A32B32G32R32F", 115: "G32R32F",
       75: "D24S8", 77: "D24X8", 80: "D16", 71: "D32", 36: "A16B16G16R16", 50: "L8", 28: "A8", 827611204: "DXT1", 861165636: "DXT3",
       894720068: "DXT5", 1515474505: "INTZ", 875710020: "DF24", 909201988: "DF16", 843666497: "ATI2", 827606349: "ATI1", 1280331841: "NULL", 23: "R5G6B5", 35: "A2R10G10B10", 31: "A2B10G10R10", 34: "G16R16"}
fmt = lambda f: FMT.get(int(f), f"f{f}")


def ctab(path):
    """-> list of (set, index, count, name); set: b bool, i int, c float, s sampler"""
    try:
        data = open(path, "rb").read()
    except OSError:
        return None
    n = len(data) // 4
    tok = struct.unpack(f"<{n}I", data[:n * 4])
    i = 1
    while i < n:
        t = tok[i]
        if (t & 0xFFFF) == 0xFFFE:
            length = (t >> 16) & 0x7FFF
            if length and tok[i + 1] == 0x42415443:
                base = (i + 2) * 4
                size, creator, version, count, info, flags, target = struct.unpack_from("<7I", data, base)
                out = []
                for k in range(count):
                    name, rset, rindex, rcount, _res, _type, _default = struct.unpack_from("<IHHHHII", data, base + info + k * 20)
                    end = data.index(b"\0", base + name)
                    out.append(("bics"[rset] if rset < 4 else "?", rindex, rcount, data[base + name:end].decode("ascii", "replace")))
                return sorted(out)
            i += length + 1
        elif t == 0x0000FFFF:
            break
        else:
            i += 1
    return []


def names(kind, h, root):
    t = ctab(os.path.join(root, f"{kind}dump", f"{kind}_{h}.bin"))
    if t is None:
        return "(no dump)"
    if not t:
        return "(no CTAB)"
    return " ".join(f"{s}{i}{'x' + str(c) if c > 1 else ''}:{nm}" for s, i, c, nm in t)


def parse(path):
    draws, pubs, head = [], [], ""
    for line in open(path, "r", errors="replace"):
        p = line.rstrip("\n").split("\t")
        d = dict(x.split("=", 1) for x in p[1:] if "=" in x)
        if p[0] == "draw":
            draws.append(d)
        elif p[0] == "campub":
            pubs.append(d)
        elif p[0] == "frame":
            head = line.strip()
    return head, draws, pubs


def surf(s):
    """'ptr,WxH,fmt,usage,ms,texPTR' -> dict or None"""
    if not s or s == "0":
        return None
    a = s.split(",")
    return {"ptr": a[0], "size": a[1], "fmt": fmt(a[2][1:]), "usage": a[3][1:], "ms": a[4][2:], "tex": a[5][3:]}


def main():
    only = int(sys.argv[sys.argv.index("--pass") + 1]) if "--pass" in sys.argv else None
    argv = list(sys.argv[1:])
    if "--pass" in argv:
        del argv[argv.index("--pass"):argv.index("--pass") + 2]
    args = [a for a in argv if not a.startswith("--")]
    root = args[1] if len(args) > 1 else SYSTEM
    head, draws, pubs = parse(args[0])
    print(head, f"| draws {len(draws)} | camera publications {len(pubs)}: " + ", ".join(f"after draw {p['afterdraw']} caller {p['caller']} applied {p['applied']}" for p in pubs))
    # every texture that is (or was) a render target or depth buffer in this frame
    rt_tex = {}
    for d in draws:
        for k in ("rt0", "rt1", "rt2", "rt3", "ds"):
            s = surf(d.get(k))
            if s and s["tex"] not in ("00000000", "0", ""):
                rt_tex.setdefault(s["tex"].upper().lstrip("0"), f"{k}:{s['size']}:{s['fmt']}")
    def stage(d, i):
        v = d.get(f"t{i}")
        if not v:
            return None
        a = v.split(",")
        key = a[0].upper().lstrip("0")
        return {"ptr": a[0], "type": a[1], "size": a[2], "fmt": fmt(a[3][1:]), "usage": a[4][1:], "rt": rt_tex.get(key), "rtusage": int(a[4][1:], 16) & 3}
    passes, cur, key = [], None, None
    for d in draws:
        k = tuple(d.get(x, "0").split(",")[0] for x in ("rt0", "rt1", "rt2", "rt3", "ds"))
        if k != key:
            cur = []; passes.append(cur); key = k
        cur.append(d)
    print(f"{len(passes)} passes (runs of draws with the same targets)\n")
    for n, ps in enumerate(passes):
        if only is not None and n != only:
            continue
        d0 = ps[0]
        rts = [surf(d0.get(k)) for k in ("rt0", "rt1", "rt2", "rt3")]
        ds = surf(d0.get("ds"))
        tgt = " + ".join(f"{s['size']} {s['fmt']}" for s in rts if s) + (f" | depth {ds['size']} {ds['fmt']}" if ds else " | no depth")
        c = collections.Counter
        st = lambda k: ",".join(f"{v}x{cnt}" for v, cnt in c(d.get(k, "?") for d in ps).most_common(3))
        samp_rt = sum(1 for d in ps if any((s := stage(d, i)) and (s["rt"] or s["rtusage"]) for i in range(8)))
        prims = sum(int(d["prims"]) for d in ps)
        small = sum(1 for d in ps if int(d["prims"]) <= 2)
        print(f"PASS {n}: draws {ps[0]['seq']}-{ps[-1]['seq']} ({len(ps)})  back={d0['rtback']}  target {tgt}  vp {st('vp')}")
        print(f"   prims {prims} (draws of <=2 triangles: {small}) | zen {st('zen')} zwr {st('zwr')} zfn {st('zfn')} cw {st('cw')} blend {st('bl')} src {st('src')} dst {st('dst')} cull {st('cull')} atest {st('at')} stencil {st('st')}")
        print(f"   draws sampling a render-target/depth texture: {samp_rt} | proxy action: {st('action')} | alphahint {st('alphahint')}")
        pairs = c((d["vs"], d["ps"]) for d in ps)
        limit = 40 if only is not None else 6
        for (vs, psh), cnt in pairs.most_common(limit):
            ex = next(d for d in ps if d["vs"] == vs and d["ps"] == psh)
            tex = []
            for i in range(8):
                s = stage(ex, i)
                if s:
                    tex.append(f"t{i}={'RT[' + s['rt'] + ']' if s['rt'] else ('rtUsage' if s['rtusage'] else '')}{s['size']} {s['fmt']}")
            print(f"     {cnt:4d} x vs {vs} ps {psh} | prims e.g. {ex['prims']} | {'; '.join(tex)}")
            if "--shaders" in sys.argv or only is not None:
                print(f"            vs: {names('vs', vs, root)}")
                print(f"            ps: {names('ps', psh, root)}")
                print(f"            decl: {ex.get('decl', '')}")
        if len(pairs) > limit:
            print(f"     ... {len(pairs) - limit} more shader pairs")
        print()


main()
