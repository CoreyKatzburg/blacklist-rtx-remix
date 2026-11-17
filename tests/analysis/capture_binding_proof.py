"""Scores the live proof of rtx.capture.bindInstanceMaterials (USD capture material binding, worker C, Nov 16).

Three USD captures at one held pose, each with the Remix draw log of the captured frame (rtx.drawLog.waitForCapture,
header capture=1): off, on, off. Usage (pxr comes from the RTX Remix Kit python):
  "C:\\Program Files\\NVIDIA Corporation\\RTX Remix\\kit\\python\\python.exe" -I capture_binding_proof.py \\
      off1.usd off1_remix_draws.tsv on.usd on_remix_draws.tsv off2.usd off2_remix_draws.tsv
  python -I capture_binding_proof.py --self-test          (fakes only, no pxr)

Per capture, per mesh drawn in the logged frame (ext=0 rows, keyed by capmesh):
  unbound  no capture instance of the mesh binds a material
  clean    every drawn row has instmat == mat and mat is a Look of the capture
Rules (exit 0 only when every question is answered and passes):
  VOID     a draw log header lacks capture=1 (the rows are not the captured frame), or a USD has no Looks.
  Q1 cause, each off capture: every unbound drawn mesh reads capmeshmat=0 with capmeshmathash != mat on its rows, and
           every drawn mesh reading capmeshmat=1 is bound. Reports how many first-instance hashes are 0.
  Q2 fix:  NOT ANSWERED when neither off capture has a clean unbound drawn mesh (bug not reproduced).
           on: 0 clean drawn meshes unbound, and 0 meshes bound in an off capture are unbound in on.
           off2: at least 1 clean unbound drawn mesh again (the option really went off).
"""
import collections
import os
import sys


def read_log(path):
    head, rows = {}, []
    for line in open(path, encoding="utf-8"):
        p = line.rstrip("\n").split("\t")
        kv = dict(x.split("=", 1) for x in p[1:] if "=" in x)
        if p[0] == "remixframe":
            head = kv
        elif p[0] == "remixdraw" and kv.get("ext") == "0":
            rows.append(kv)
    return head, rows


def read_usd(path):
    usd_libs = r"C:\Program Files\NVIDIA Corporation\RTX Remix\extscache\omni.usd.libs-1.0.1+d02c707b.wx64.r.cp310"
    os.add_dll_directory(usd_libs + r"\bin")
    sys.path.insert(0, usd_libs)
    from pxr import Usd
    looks, inst = set(), collections.defaultdict(list)
    stage = Usd.Stage.Open(path)  # keep the stage alive while traversing
    for prim in stage.Traverse():
        p = str(prim.GetPath())
        if p.startswith("/RootNode/Looks/mat_"):
            looks.add(prim.GetName()[4:])
        elif p.startswith("/RootNode/instances/inst_") and p.count("/") == 3:
            rel = prim.GetRelationship("material:binding")
            tg = rel.GetTargets() if rel else []
            inst[prim.GetName()[5:].rsplit("_", 1)[0]].append(tg[0].name[4:] if tg else None)
    return looks, dict(inst)


def summarize(looks, inst, head, rows):
    """Per drawn mesh: unbound, clean, and the draw log's capture fields."""
    by = collections.defaultdict(list)
    for r in rows:
        by[r["capmesh"]].append(r)
    meshes = {}
    for mesh, rs in by.items():
        if mesh not in inst:
            continue
        meshes[mesh] = {
            "unbound": all(b is None for b in inst[mesh]),
            "clean": all(r["instmat"] == r["mat"] and r["mat"] in looks for r in rs),
            "capmeshmat": {r["capmeshmat"] for r in rs},
            "firstDiffers": all(r["capmeshmathash"] != r["mat"] for r in rs),
            "firstZero": all(int(r["capmeshmathash"], 16) == 0 for r in rs),
        }
    return {"captureHeader": head.get("capture") == "1", "looks": len(looks), "meshes": meshes}


def score(off1, on, off2):
    out, void = [], []
    for name, s in (("off1", off1), ("on", on), ("off2", off2)):
        if not s["captureHeader"]:
            void.append(f"{name}: draw log header lacks capture=1")
        if s["looks"] == 0:
            void.append(f"{name}: USD has no Looks")
    if void:
        return "VOID", ["VOID " + v for v in void]
    ok = True
    for name, s in (("off1", off1), ("off2", off2)):
        unb = [m for m, d in s["meshes"].items() if d["unbound"]]
        wrong = [m for m in unb if s["meshes"][m]["capmeshmat"] != {"0"} or not s["meshes"][m]["firstDiffers"]]
        bound_but_0 = [m for m, d in s["meshes"].items() if d["capmeshmat"] == {"1"} and d["unbound"]]
        zero = sum(1 for m in unb if s["meshes"][m]["firstZero"])
        q1 = not wrong and not bound_but_0
        ok &= q1
        out.append(f"Q1 {name}: {'PASS' if q1 else 'FAIL'} unbound drawn {len(unb)}, first-instance hash 0 on {zero}, "
                   f"unknown non-zero on {len(unb) - zero}; disagreeing {len(wrong) + len(bound_but_0)}"
                   + (f" {sorted(wrong + bound_but_0)[:6]}" if wrong or bound_but_0 else ""))
    clean_unb = {n: {m for m, d in s["meshes"].items() if d["unbound"] and d["clean"]} for n, s in
                 (("off1", off1), ("on", on), ("off2", off2))}
    if not clean_unb["off1"] and not clean_unb["off2"]:
        out.append("Q2 NOT ANSWERED: no clean unbound drawn mesh in either off capture (bug not reproduced)")
        return "NOT ANSWERED", out
    bound_off = {m for s in (off1, off2) for m, d in s["meshes"].items() if not d["unbound"]}
    lost = sorted(m for m in bound_off if m in on["meshes"] and on["meshes"][m]["unbound"])
    still = sorted(clean_unb["on"])
    off_unb = clean_unb["off1"] | clean_unb["off2"]
    fixed = sum(1 for m in off_unb if m in on["meshes"] and not on["meshes"][m]["unbound"])
    q2 = not still and not lost and bool(clean_unb["off2"])
    ok &= q2
    out.append(f"Q2 fix: {'PASS' if q2 else 'FAIL'} off1 clean unbound {len(clean_unb['off1'])}, "
               f"on clean unbound {len(still)}, off2 clean unbound {len(clean_unb['off2'])}; "
               f"off-unbound meshes bound in on {fixed}; bound in off but unbound in on {len(lost)}"
               + (f" {lost[:6]}" if lost else "") + (f" still unbound {still[:6]}" if still else ""))
    return ("PASS" if ok else "FAIL"), out


def _fake(unbound_clean=2, fix_on=False, bound=3, capture="1", first_zero=0):
    meshes = {}
    for i in range(bound):
        meshes[f"B{i}"] = {"unbound": False, "clean": True, "capmeshmat": {"1"}, "firstDiffers": False, "firstZero": False}
    for i in range(unbound_clean):
        meshes[f"U{i}"] = {"unbound": not fix_on, "clean": True, "capmeshmat": {"0"}, "firstDiffers": True,
                           "firstZero": i < first_zero}
    return {"captureHeader": capture == "1", "looks": 10, "meshes": meshes}


def self_test():
    cases = []
    def case(name, got, want):
        cases.append(got == want)
        print(f"{'ok  ' if got == want else 'FAIL'} {name}: {got}")
    case("fix works", score(_fake(), _fake(fix_on=True), _fake())[0], "PASS")
    case("option had no effect", score(_fake(), _fake(), _fake())[0], "FAIL")
    on_lost = _fake(fix_on=True); on_lost["meshes"]["B0"]["unbound"] = True
    case("on loses a bound mesh", score(_fake(), on_lost, _fake())[0], "FAIL")
    case("option stuck on in off2", score(_fake(), _fake(fix_on=True), _fake(fix_on=True))[0], "FAIL")
    case("bug not reproduced", score(_fake(0), _fake(0, fix_on=True), _fake(0))[0], "NOT ANSWERED")
    case("draw log not the captured frame", score(_fake(capture="0"), _fake(fix_on=True), _fake())[0], "VOID")
    off_wrong = _fake(); off_wrong["meshes"]["U0"]["capmeshmat"] = {"1"}
    case("Q1: unbound mesh whose first hash is captured", score(off_wrong, _fake(fix_on=True), _fake())[0], "FAIL")
    s, lines = score(_fake(first_zero=1), _fake(fix_on=True), _fake())
    case("Q1 reports hash-0 count", "first-instance hash 0 on 1" in lines[0], True)
    # summarize() on raw inputs: an unbound mesh with an undrawn first instance, and a bound one.
    head = {"capture": "1"}
    rows = [{"capmesh": "M1", "instmat": "A", "mat": "A", "capmeshmat": "0", "capmeshmathash": "00000000000000FF"},
            {"capmesh": "M2", "instmat": "B", "mat": "B", "capmeshmat": "1", "capmeshmathash": "B"}]
    sm = summarize({"A", "B"}, {"M1": [None, None], "M2": ["B"]}, head, rows)["meshes"]
    case("summarize unbound clean", (sm["M1"]["unbound"], sm["M1"]["clean"], sm["M1"]["firstDiffers"]), (True, True, True))
    case("summarize bound", (sm["M2"]["unbound"], sm["M2"]["capmeshmat"]), (False, {"1"}))
    sm2 = summarize({"B"}, {"M1": [None]}, head, rows[:1])["meshes"]
    case("summarize: mat not in Looks is not clean", sm2["M1"]["clean"], False)
    print("capture_binding_proof self-test:", "PASS" if all(cases) else "FAIL")
    return 0 if all(cases) else 1


def main(argv):
    if argv[:1] == ["--self-test"]:
        return self_test()
    if len(argv) != 6:
        print(__doc__)
        return 2
    caps = []
    for usd, log in zip(argv[0::2], argv[1::2]):
        looks, inst = read_usd(usd)
        head, rows = read_log(log)
        caps.append(summarize(looks, inst, head, rows))
    verdict, lines = score(*caps)
    for line in lines:
        print(line)
    print("capture_binding_proof:", verdict)
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
