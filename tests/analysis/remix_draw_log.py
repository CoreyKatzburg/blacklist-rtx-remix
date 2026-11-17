"""Checks a Remix per-draw scene log (gate\\<tag>_remix_draws.tsv, rtx.drawLog.tag; format in rtx_draw_log.h).

The offline C++ test (tests\\remix_source\\draw_log_test.cpp) proves each field is formatted under its own key. This
script proves, on a file the game wrote, that SceneManager::addDrawLogRow read each field from the right place,
using rules Remix's own code makes true for every draw (two witnesses for each field):
  rows      header draws= equals the row count, and order runs 0..n-1
  mat       for game draws (ext 0) mat == t0: LegacyMaterialData's hash is its stage-0 image hash
            (rtx_materials.h updateCachedHash)
  tc        tc 1 exactly when tcsrc is not none (DrawCallState::hasTextureCoordinates = texcoord buffer or texgen)
  tcbuf     tcsrc decl or vscapture exactly when tcstride > 0
  prims     prims == (idx or verts) // 3
  cap       cap 0 gives capmat -1 and capmeshmat -1; cap 1 gives capmat 0 or 1
  ignored   ignored 1 gives inst 0 and instalbedo -1
  rtype     ralbedo is -1 exactly when rtype is not 0 (opaque)
  t0empty   t0empty 1 gives t0 0 for game draws (TextureRef::getImageHash is 0 without an image view)
Report only, never a failure (these are what the log is for):
  FINDING instance albedo: on the first row of each instance (the draw whose material the instance binds this frame),
          drawtrack and instalbedo should agree; disagreements are counted and listed.
  counts of t0empty, tc 0, tcsrc kinds, drawtrack 0 with t0 non-empty, and capture binding.
Exit 0 = every rule holds, 1 = a rule fails (the log is mis-wired), 2 = bad input.

Usage: remix_draw_log.py FILE | remix_draw_log.py --self-test
"""
import collections
import sys

ZERO = "0" * 16


def parse(text):
    head, rows = None, []
    for line in text.splitlines():
        parts = line.split("\t")
        kv = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
        if parts[0] == "remixframe":
            head = kv
        elif parts[0] == "remixdraw":
            rows.append(kv)
    return head, rows


def check(head, rows):
    """Returns (failures, findings, counts)."""
    fails, findings = [], []
    if head is None:
        return ["no remixframe header"], [], {}
    if int(head.get("draws", -1)) != len(rows):
        fails.append(f"rows: header draws={head.get('draws')} but {len(rows)} rows")
    if [int(r["order"]) for r in rows] != list(range(len(rows))):
        fails.append("rows: order does not run 0..n-1")
    for r in rows:
        o = r["order"]
        game = r["ext"] == "0"
        if game and r["mat"] != r["t0"]:
            fails.append(f"mat: order {o} mat {r['mat']} != t0 {r['t0']}")
        if (r["tc"] == "1") != (r["tcsrc"] != "none"):
            fails.append(f"tc: order {o} tc {r['tc']} with tcsrc {r['tcsrc']}")
        if (r["tcsrc"] in ("decl", "vscapture")) != (int(r["tcstride"]) > 0):
            fails.append(f"tcbuf: order {o} tcsrc {r['tcsrc']} with tcstride {r['tcstride']}")
        if int(r["prims"]) != (int(r["idx"]) or int(r["verts"])) // 3:
            fails.append(f"prims: order {o} prims {r['prims']} idx {r['idx']} verts {r['verts']}")
        if r["cap"] == "0" and (r["capmat"] != "-1" or r["capmeshmat"] != "-1"):
            fails.append(f"cap: order {o} no capture but capmat {r['capmat']} capmeshmat {r['capmeshmat']}")
        if r["cap"] == "1" and r["capmat"] not in ("0", "1"):
            fails.append(f"cap: order {o} capture running but capmat {r['capmat']}")
        if r["ignored"] == "1" and (r["inst"] != "0" or r["instalbedo"] != "-1"):
            fails.append(f"ignored: order {o} ignored but inst {r['inst']} instalbedo {r['instalbedo']}")
        if (r["ralbedo"] == "-1") != (r["rtype"] != "0"):
            fails.append(f"rtype: order {o} rtype {r['rtype']} with ralbedo {r['ralbedo']}")
        if game and r["t0empty"] == "1" and r["t0"] != ZERO:
            fails.append(f"t0empty: order {o} t0empty 1 but t0 {r['t0']}")
    first = {}
    for r in rows:
        if r["inst"] != "0":
            first.setdefault(r["inst"], r)
    for r in first.values():
        if r["rtype"] == "0" and r["instalbedo"] in ("0", "1") and r["drawtrack"] != r["instalbedo"]:
            findings.append(f"instance albedo: order {r['order']} dcid {r['dcid']} inst {r['inst']} drawtrack "
                            f"{r['drawtrack']} instalbedo {r['instalbedo']} t0 {r['t0']} tc {r['tc']}")
    c = collections.Counter()
    for r in rows:
        c[f"tcsrc={r['tcsrc']}"] += 1
        c["t0empty=1"] += r["t0empty"] == "1"
        c["tc=0"] += r["tc"] == "0"
        c["drawtrack=0 with t0 set"] += r["drawtrack"] == "0" and r["t0"] != ZERO
        c["instalbedo=0"] += r["instalbedo"] == "0"
        c["capmeshmat=0"] += r["capmeshmat"] == "0"
    return fails, findings, c


def report(head, rows):
    fails, findings, counts = check(head, rows)
    lines = [f"{len(rows)} draws, frame {head.get('frame') if head else '?'}"]
    lines += [f"  {k}: {v}" for k, v in sorted(counts.items())]
    lines += [f"FINDING {f}" for f in findings]
    lines += [f"FAIL {f}" for f in fails]
    lines.append("remix_draw_log: " + ("PASS" if not fails else f"FAIL ({len(fails)})"))
    return (1 if fails else 0), lines


def row(order, **kw):
    r = {"order": str(order), "dcid": str(order + 3), "ext": "0", "cam": "0", "ignored": "0", "verts": "300",
         "idx": "600", "prims": "200", "geom": "A" * 16, "mat": "B" * 16, "rmat": "B" * 16, "rtype": "0",
         "t0": "B" * 16, "t0empty": "0", "t0valid": "1", "t1": ZERO, "tc": "1", "tcsrc": "vscapture",
         "tcfmt": "103", "tcstride": "36", "tcoff": "12", "ralbedo": "1", "drawtrack": "1", "inst": str(100 + order),
         "instmat": "B" * 16, "instalbedo": "1", "blasmat": "B" * 16, "capmesh": "C" * 16, "cap": "0",
         "capmat": "-1", "capmeshmat": "-1", "capmeshmathash": ZERO}
    r.update(kw)
    return r


def fake_file(rows, draws=None):
    lines = [f"remixframe\tframe=9\tqpcMs=1.000\ttag=t\tdraws={len(rows) if draws is None else draws}"]
    lines += ["remixdraw\t" + "\t".join(f"{k}={v}" for k, v in r.items()) for r in rows]
    return "\n".join(lines) + "\n"


def good_rows():
    return [
        row(0),                                                                   # like draw 305: textured, tracked
        row(1, mat="D" * 16, t0="D" * 16, instmat="D" * 16, tc="0", tcsrc="none", tcfmt="0", tcstride="0",
            tcoff="0", drawtrack="0", instalbedo="0"),                            # no texcoords: not tracked
        row(2, mat=ZERO, t0=ZERO, t0empty="1", t0valid="0", ralbedo="0", drawtrack="0", instalbedo="0",
            instmat=ZERO),                                                        # no texture bound
        row(3, ext="1", mat="E" * 16, t0=ZERO, t0empty="1", rmat="E" * 16),        # API draw with hash override
        row(4, ignored="1", inst="0", instalbedo="-1", instmat=ZERO),             # ignored material
        row(5, rtype="1", ralbedo="-1", drawtrack="0", instalbedo="-1", idx="0", verts="9", prims="3"),
        row(6, tcsrc="decl", tcfmt="114", tcstride="24", tcoff="16", cap="1", capmat="1", capmeshmat="0",
            capmeshmathash=ZERO),                                                 # capture running
        row(7, inst="100", instalbedo="1", drawtrack="0", tc="0", tcsrc="none", tcfmt="0", tcstride="0",
            tcoff="0"),                                                           # second draw of instance 100
    ]


def self_test():
    bad = 0

    def run(name, text, want_code, want_finding=None):
        nonlocal bad
        code, lines = report(*parse(text))
        ok = code == want_code and (want_finding is None or any(l.startswith("FINDING") and want_finding in l
                                                                 for l in lines))
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}: exit {code}")
        if not ok:
            print("     " + "\n     ".join(lines))

    run("good file", fake_file(good_rows()), 0)
    broken = [
        ("t0 logged from stage 1", 0, {"t0": "F" * 16}),
        ("tc logged as always 1", 1, {"tc": "1"}),
        ("tc logged as always 0", 0, {"tc": "0"}),
        ("texcoord stride not logged", 6, {"tcstride": "0"}),
        ("prims from verts on an indexed draw", 0, {"prims": "100"}),
        ("capmat logged while no capture", 0, {"capmat": "1"}),
        ("capmat missing while capturing", 6, {"capmat": "-1"}),
        ("instance logged for an ignored draw", 4, {"inst": "555"}),
        ("ralbedo logged for a translucent draw", 5, {"ralbedo": "1"}),
        ("ralbedo missing for an opaque draw", 0, {"ralbedo": "-1"}),
        ("t0empty logged inverted", 0, {"t0empty": "1"}),
    ]
    for name, i, change in broken:
        rows = good_rows()
        rows[i] = dict(rows[i], **change)
        run(name, fake_file(rows), 1)
    rows = good_rows()
    run("header count wrong", fake_file(rows, draws=7), 1)
    run("order gap", fake_file([rows[0], dict(rows[1], order="2")], draws=2), 1)
    rows = good_rows()
    rows[0] = dict(rows[0], instalbedo="0")
    run("albedo valid and texcoords but not tracked is a FINDING, not a failure", fake_file(rows), 0, "order 0 ")
    rows = good_rows()
    run("a later draw of an instance is not compared", fake_file(rows), 0)
    print("remix_draw_log self-test:", "PASS" if not bad else f"FAIL ({bad})")
    return 1 if bad else 0


def main(argv):
    if argv[1:] == ["--self-test"]:
        return self_test()
    if len(argv) != 2:
        print(__doc__)
        return 2
    with open(argv[1], errors="replace") as f:
        head, rows = parse(f.read())
    if head is None:
        print("no remixframe header")
        return 2
    code, lines = report(head, rows)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
