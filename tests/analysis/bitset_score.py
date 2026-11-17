"""Worker G: which of Sam's bits keep a teleport from being undone (coordinator's bit-set test, Nov 16).

Each set is one run of worker G's scratch probe (.amp/in/workerG/probe_live.py, slot 3) with one phase ("off:150" for
the set "none", "on:150" for the others) that teleports Sam to the spot's target at the phase start. Per set:
  HOLDS    no player change (relink) and no row without a player for the whole hold, at least MIN_SECONDS watched,
           and every row within drive.py's teleport limits of the target.
  RELOADS  the player objects were replaced (the game reloaded the checkpoint), with the time into the hold.
  MOVED    no reload, but Sam left the target (pushed, fell, or walked).
  VOID     a reload within CARRY_SECONDS of the start (left over from the set before), too short a watch, a teleport
           that never landed, or bits that did not read back as asked.
Per spot: VOID unless the control "none" RELOADS and the control "all" HOLDS (the behaviour seen with Remix in E1/E4).
The smallest set that HOLDS is reported, and whether one without "invisible" holds.

python bitset_score.py --run <run dir> --spot green_hall --spot mall      (reads <run>/<spot>-<set>.json)
python bitset_score.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys

SETS = {"none": [], "all": ["invisible", "godmode", "blind"], "godmode_blind": ["godmode", "blind"], "blind": ["blind"]}
HOLD_SECONDS = 150.0
MIN_SECONDS = 145.0
CARRY_SECONDS = 10.0
# Floor height under each target, from slot 2 (20261116-195001): green hall landed at z 91.5 in all three steps, mall at
# 85.6 and 92.5. Slot 3 (20261116-233254) showed why this is needed: with the same target Sam fell out of the world
# (z -988, -2393, -4008 at 1, 2, 3 s) in green_hall-none and was pushed up to z 835.7 in green_hall-blind, so a reload
# or a hold there says nothing about guards. A set whose Sam is not on the floor 3 s after the teleport is VOID.
FLOORS = {"green_hall": 91.5, "mall": 89.0}
FLOOR_TOLERANCE = 15.0
FLOOR_ROW = 3          # rows are 1 s apart


def score_set(probe: dict, name: str, floor: float | None = None) -> dict:
    want = SETS[name]
    if probe.get("error"):
        return {"verdict": "VOID", "why": f"probe error: {probe['error']}"}
    phases = probe.get("phases") or []
    if len(phases) != 1:
        return {"verdict": "VOID", "why": f"{len(phases)} phases, expected 1"}
    ph = phases[0]
    rows, s = ph["rows"], ph["summary"]
    out = {"rows": len(rows), "relinks": s["relinks"], "landed": None}
    if not rows:
        return dict(out, verdict="VOID", why="no rows")
    # Times count from the first watch row, written right after the teleport. The phase clock (qpcStartMs) starts
    # before the blind writes, which scan every object (4.3 to 4.8 s in slot 2's E1).
    start = rows[0]["t"]
    out["setupSeconds"] = round(start - ph["qpcStartMs"] / 1000.0, 1)
    span = rows[-1]["t"] - start
    out["seconds"] = round(span, 1)
    # Bits: Sam's on every row before the first relink must be as asked; blind must have been written on some guards.
    first_change = None
    for r in rows:
        if "player" in r and r["player"] is None:
            first_change = r["t"]
            break
    for rl in s.get("relinkList", []):
        first_change = rl["t"] if first_change is None else min(first_change, rl["t"])
    before = [r for r in rows if "location" in r and (first_change is None or r["t"] < first_change)]
    if not before:
        return dict(out, verdict="VOID", why="no row with Sam before the first player change")
    out["landed"] = before[min(len(before) - 1, 8)]["location"]   # about 8 s in at 1 row/s
    if floor is not None:
        z = rows[min(len(rows) - 1, FLOOR_ROW)]["location"][2] if "location" in rows[min(len(rows) - 1, FLOOR_ROW)] else None
        out["zAt3s"] = z
        if z is None or abs(z - floor) > FLOOR_TOLERANCE:
            return dict(out, verdict="VOID", why=f"Sam not on the floor 3 s after the teleport: z {z}, floor {floor}")
    for bit in ("invisible", "godmode"):
        bad = [r["t"] for r in before if r[bit] != (bit in want)]
        if bad:
            return dict(out, verdict="VOID", why=f"{bit} read {not (bit in want)} at {len(bad)} rows")
    blind = ph.get("blind") or []
    blind_ok = [b for b in blind if "error" not in b]
    out["blindWrites"] = len(blind_ok)
    if "blind" in want and not blind_ok:
        return dict(out, verdict="VOID", why="blind asked but no guard was written")
    if "blind" not in want and any(b.get("on") for b in blind_ok):
        return dict(out, verdict="VOID", why="blind written on in a set without blind")
    left = [r["t"] for r in before if not r.get("held")]
    if left:
        out["leftTargetAt"] = round(left[0] - start, 1)
    if first_change is not None:
        t = round(first_change - start, 1)
        out["reloadAt"] = t
        if t < CARRY_SECONDS:
            return dict(out, verdict="VOID", why=f"player changed {t} s into the hold: a reload carried over from before")
        return dict(out, verdict="RELOADS", why=f"player objects replaced {t} s into the hold")
    if span < MIN_SECONDS:
        return dict(out, verdict="VOID", why=f"watched {span:.1f} s, under {MIN_SECONDS}")
    if not before[0].get("held", False) and not any(r.get("held") for r in before[:10]):
        return dict(out, verdict="VOID", why="the teleport never landed within the limits")
    if left:
        return dict(out, verdict="MOVED", why=f"Sam outside the limits at {len(left)} rows, first {round(left[0] - start, 1)} s in")
    return dict(out, verdict="HOLDS", why=f"no reload in {span:.1f} s, Sam within the limits on all {len(before)} rows")


def score_spot(sets: dict[str, dict], floor: float | None = None) -> dict:
    res = {n: score_set(p, n, floor) if p is not None else {"verdict": "VOID", "why": "missing"} for n, p in sets.items()}
    out = {"sets": res}
    ctl = []
    if res.get("none", {}).get("verdict") != "RELOADS":
        ctl.append(f"control none is {res.get('none', {}).get('verdict')}, not RELOADS")
    if res.get("all", {}).get("verdict") != "HOLDS":
        ctl.append(f"control all is {res.get('all', {}).get('verdict')}, not HOLDS")
    if ctl:
        out.update(verdict="VOID", why="; ".join(ctl))
        return out
    holding = sorted((n for n, r in res.items() if r["verdict"] == "HOLDS"), key=lambda n: len(SETS[n]))
    out["holding"] = holding
    out["smallest"] = holding[0]
    out["withoutInvisibleHolds"] = any("invisible" not in SETS[n] for n in holding)
    out["verdict"] = "SCORED"
    out["why"] = f"smallest holding set: {holding[0]} ({'+'.join(SETS[holding[0]]) or 'nothing'})"
    return out


# --- self-test -------------------------------------------------------------------------------------------------
def fake(name: str, reload_at=None, seconds=150.0, moved_at=None, bits_wrong=False, no_blind=False, never_landed=False) -> dict:
    want = SETS[name]
    start = 1000.0
    rows = []
    t = 0.0
    while t <= seconds:
        r = {"t": round(start + t, 2)}
        if reload_at is not None and t >= reload_at:
            r.update(location=[-6285.7, -2420.7, 1188.0], held=False, invisible=False, godmode=False)
        else:
            held = not (moved_at is not None and t >= moved_at) and not never_landed
            r.update(location=[-8240.0, 1995.0, 91.5] if held else [-8000.0, 2500.0, 91.5], held=held,
                     invisible=("invisible" in want) != bits_wrong, godmode="godmode" in want)
        rows.append(r)
        t += 1.0
    relinks = [{"t": round(start + reload_at, 2)}] if reload_at is not None else []
    blind = [] if no_blind else [{"name": f"EAIBARegular_{i}", "on": "blind" in want} for i in range(3)]
    return {"phases": [{"phase": "on" if want else "off", "qpcStartMs": start * 1000, "rows": rows, "blind": blind,
                        "summary": {"relinks": len(relinks), "relinkList": relinks}}]}


def selftest() -> int:
    results = []

    def check(label, got, want):
        ok = got == want
        results.append(ok)
        print(("PASS  " if ok else "FAIL  ") + f"{label}: {got}" + ("" if ok else f" (expected {want})"))

    check("hold", score_set(fake("all"), "all")["verdict"], "HOLDS")
    check("reload at 60 s", score_set(fake("none", reload_at=60), "none")["verdict"], "RELOADS")
    check("reload time", score_set(fake("none", reload_at=60), "none")["reloadAt"], 60.0)
    check("reload carried over (3 s)", score_set(fake("none", reload_at=3), "none")["verdict"], "VOID")
    check("short watch", score_set(fake("all", seconds=100), "all")["verdict"], "VOID")
    check("moved", score_set(fake("blind", moved_at=40), "blind")["verdict"], "MOVED")
    check("bit read wrong", score_set(fake("godmode_blind", bits_wrong=True), "godmode_blind")["verdict"], "VOID")
    check("blind asked, none written", score_set(fake("blind", no_blind=True), "blind")["verdict"], "VOID")
    check("never landed", score_set(fake("all", never_landed=True), "all")["verdict"], "VOID")
    check("probe error", score_set({"error": "x"}, "all")["verdict"], "VOID")
    good = {"none": fake("none", reload_at=40), "all": fake("all"), "godmode_blind": fake("godmode_blind"),
            "blind": fake("blind", reload_at=50)}
    s = score_spot(good)
    check("spot smallest", s.get("smallest"), "godmode_blind")
    check("spot without invisible", s.get("withoutInvisibleHolds"), True)
    s = score_spot(dict(good, blind=fake("blind")))
    check("spot smallest is blind", s.get("smallest"), "blind")
    s = score_spot(dict(good, godmode_blind=fake("godmode_blind", reload_at=30)))
    check("only all holds", (s.get("smallest"), s.get("withoutInvisibleHolds")), ("all", False))
    check("control none holds -> VOID", score_spot(dict(good, none=fake("none")))["verdict"], "VOID")
    check("control all reloads -> VOID", score_spot(dict(good, all=fake("all", reload_at=50)))["verdict"], "VOID")
    check("missing set -> VOID", score_spot(dict(good, all=None))["verdict"], "VOID")
    # floor check, with slot 3's two real failures as the fake cases
    check("on the floor -> HOLDS", score_set(fake("all"), "all", 91.5)["verdict"], "HOLDS")
    up = fake("blind")
    for r in up["phases"][0]["rows"][1:]:
        r["location"] = [-8240.9, 2010.2, 835.7]
    check("pushed up to 835.7 -> VOID", score_set(up, "blind", 91.5)["verdict"], "VOID")
    down = fake("none", reload_at=40)
    for i, r in enumerate(down["phases"][0]["rows"][1:6], 1):
        r["location"] = [-8240.0, 1995.0, 136.0 - 1300.0 * i]
    check("fell out of the world -> VOID", score_set(down, "none", 91.5)["verdict"], "VOID")
    check("same fall without a floor given -> RELOADS", score_set(down, "none")["verdict"], "RELOADS")
    check("spot with a pushed-up set", score_spot(dict(good, all=up), 91.5)["verdict"], "VOID")
    print(f"{sum(results)} of {len(results)} pass")
    return 0 if all(results) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--run")
    ap.add_argument("--spot", action="append", default=[])
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    report = {}
    for spot in a.spot:
        sets = {}
        for n in SETS:
            p = os.path.join(a.run, f"{spot}-{n}.json")
            sets[n] = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else None
        report[spot] = score_spot(sets, FLOORS.get(spot))
        print(f"{spot}: {report[spot]['verdict']}  {report[spot]['why']}")
        for n, r in report[spot]["sets"].items():
            print(f"   {n:14s} {r['verdict']:8s} {r['why']}  landed {r.get('landed')}")
    with open(os.path.join(a.run, "bitset_score.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
