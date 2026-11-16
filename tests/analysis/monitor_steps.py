"""Does detaching the monitors explain the morning's fast frames? (worker C, Nov 16)

Morning launches on Nov 16 ran with both DisplayPort monitors (HKC2731, ACI27A8) switched off at their buttons, which
normally drops them off the PC. At con_custodial the morning held pose read 32.73 ms GPU (0c13881, 10:24). Today the
displays asleep read about 2.9 ms less than awake (171502 held steps: ASLEEP_COST_MS), and about 3 ms stays unexplained.
This scores one held-pose launch whose live steps carry "monitorStep": both_on, second_off, both_off, both_on_again
(in that order), each with the monitor state Windows reported at the step's start and end.
second_off is a software step done by the test driver. both_off is done by Corey (both monitors off together,
remotely): the step starts when both monitors read detached and ends at the last poll that still reads both detached.
The driver records it with "skipped" (and no frames) when the monitors did not change within 10 minutes.

Usage:
  monitor_steps.py --self-test
  monitor_steps.py SPOTDIR

Monitor state per step, from w["monitors"]["start"] and ["end"]: a list of {"name", "primary", "available", "active",
"power"}. available = Windows' QueryDisplayConfig(QDC_ALL_PATHS) targetAvailable for that monitor (false or missing
means the card does not see it: detached); active = it has an active path in QDC_ONLY_ACTIVE_PATHS; power = the
console display state ("on" / "off").
  attached     available and active and power on
  detached     not available
  deactivated  available but not active (a software disconnect; NOT the morning state)
  asleep       available and active but power off (NOT the morning state)

VOID (no verdicts) for the driver's own steps (both_on, second_off, both_on_again) when: a step's monitors are not
what it asks for at start and end (both attached; second_off the primary attached and the other detached); the game
window is minimized or not foreground at start or end; a step has fewer than MIN_FRAMES scored frames; or both_on and
both_on_again differ by more than NOISE_MS.
both_off never voids the launch. Anything wrong with it makes Q1 "NOT ANSWERED (<why>)" and prints a FINDING line:
skipped; monitors not both detached at start and end (deactivated or asleep or still attached); the game window
minimized (a minimized game recreates its swap chain in a loop, so its GPU ms mean nothing); or fewer than MIN_FRAMES
scored frames (the game stopped or slowed presenting with no monitor). Foreground is not required with no monitor;
it is printed.

Per step: median gpuTimeMs over ray-traced telemetry frames, skipping the first SKIP_MS (a monitor change can recreate
the swap chain and hitch).
Q1 both_off, d1 = both_on - both_off, d2 = both_on_again - both_off:
  DETACHED REACHES THE MORNING  min(d1, d2) >= ASLEEP_COST_MS + 2.0 (asleep's 2.9 plus most of the unexplained 3)
  DETACHED SAME AS ASLEEP       both within ASLEEP_COST_MS +- CAUSE_MS
  NO DISPLAY COST               both within +-NOISE_MS
  PARTIAL                       anything else (numbers printed)
Q2 second_off, e1 = both_on - second_off, e2 = both_on_again - second_off:
  SECOND MONITOR COSTS          both >= CAUSE_MS
  SECOND MONITOR FREE           both within +-NOISE_MS
  UNSETTLED                     anything else
Report only: both_off against the morning's held 32.73 ms (another launch, another scene state).
Exit 0 = scored, 1 = VOID, 2 = bad input.
"""
import json
import os
import statistics as st
import sys

SKIP_MS = 5000.0
NOISE_MS = 0.4
CAUSE_MS = 0.6
ASLEEP_COST_MS = 2.9
MORNING_HELD_MS = 32.73
MIN_FRAMES = 300
ORDER = ["both_on", "second_off", "both_off", "both_on_again"]


def classify(m):
    if not m.get("available"):
        return "detached"
    if not m.get("active"):
        return "deactivated"
    return "attached" if m.get("power") == "on" else "asleep"


def states(snapshot, primary_name):
    """{'primary': state, 'second': state} from one start or end snapshot, by monitor name (monitors.py marks primary
    only on an active monitor, so a detached primary reads primary false); None unless it lists two monitors."""
    prim = [m for m in snapshot if m.get("name") == primary_name]
    other = [m for m in snapshot if m.get("name") != primary_name]
    if len(prim) != 1 or len(other) != 1:
        return None
    return {"primary": classify(prim[0]), "second": classify(other[0])}


WANT = {"both_on": ("attached", "attached"), "second_off": ("attached", "detached"),
        "both_off": ("detached", "detached"), "both_on_again": ("attached", "attached")}


def steps_from(windows, frames):
    out = []
    for w in windows:
        if "monitorStep" not in w:
            continue
        gpu, presented = [], 0
        if w.get("telemetry") and not w.get("skipped"):
            a, b = w["telemetry"]
            rows = [frames[f] for f in range(a + 1, b + 1) if f in frames]
            presented = len(rows)
            if rows:
                t0 = rows[0][0]
                gpu = [g for q, g, rt in rows if rt and q - t0 >= SKIP_MS]
        out.append({"name": w["name"], "step": w["monitorStep"], "gpu": st.median(gpu) if gpu else None,
                    "n": len(gpu), "presented": presented, "w": w})
    return out


def check(steps):
    """Returns (void reasons, why Q1 is not answered or None, findings)."""
    names = [s["step"] for s in steps]
    if [n for n in names if n != "both_off"] != [n for n in ORDER if n != "both_off"] or \
            names not in (ORDER, [n for n in ORDER if n != "both_off"]):
        return [f"steps {names} are not {ORDER}"], None, []
    reasons, q1_not, findings = [], None, []
    first = (steps[0]["w"].get("monitors") or {}).get("start") or []
    primary_name = next((m["name"] for m in first if m.get("primary")), None)
    if primary_name is None:
        return [f"both_on start lists no primary monitor: {first}"], None, []
    for s in steps:
        mon = s["w"].get("monitors") or {}
        game = s["w"].get("gameWindow") or {}
        want = dict(zip(("primary", "second"), WANT[s["step"]]))
        if s["step"] == "both_off":
            if s["w"].get("skipped"):
                q1_not = f"skipped: {s['w']['skipped']}"
                findings.append(f"both_off skipped: {s['w']['skipped']}")
                continue
            got = [states(mon.get(w) or [], primary_name) for w in ("start", "end")]
            gws = [game.get(w) or {} for w in ("start", "end")]
            if any(g != want for g in got):
                q1_not = f"state {got[0]} at start, {got[1]} at end"
                findings.append(f"both_off monitors {got[0]} at start, {got[1]} at end, not both detached")
            if any(g.get("minimized") is not False for g in gws):
                q1_not = "game minimized or window not recorded"
                findings.append(f"with no monitor the game window was {gws[0] or 'not recorded'} at start, "
                                f"{gws[1] or 'not recorded'} at end (minimized or not recorded)")
            if s["n"] < MIN_FRAMES:
                q1_not = q1_not or f"{s['n']} scored frames"
                findings.append(f"with no monitor the game presented {s['presented']} frames, {s['n']} ray-traced after "
                                f"the first {SKIP_MS / 1000:.0f} s (below {MIN_FRAMES})")
            if not q1_not:
                findings.append(f"with no monitor: {s['presented']} frames presented, game window {gws[0]} at start, "
                                f"{gws[1]} at end")
            continue
        if s["n"] < MIN_FRAMES:
            reasons.append(f"{s['name']}: {s['n']} scored frames, below {MIN_FRAMES}")
        for when in ("start", "end"):
            got = states(mon.get(when) or [], primary_name)
            if got != want:
                reasons.append(f"{s['name']} {when}: monitors {got}, want {want}")
            gw = game.get(when) or {}
            if gw.get("minimized") is not False or gw.get("foreground") is not True:
                reasons.append(f"{s['name']} {when}: game window {gw or 'not recorded'}, want not minimized and foreground")
    if "both_off" not in names:
        q1_not = "both_off not recorded"
    on = [s["gpu"] for s in steps if s["step"] in ("both_on", "both_on_again") and s["gpu"] is not None]
    if len(on) == 2 and abs(on[0] - on[1]) > NOISE_MS:
        reasons.append(f"both_on and both_on_again differ {abs(on[0] - on[1]):.2f} ms, above {NOISE_MS}")
    return reasons, q1_not, findings


def verdicts(steps, q1_not=None):
    g = {s["step"]: s["gpu"] for s in steps}
    d1 = d2 = None
    if q1_not:
        q1 = f"NOT ANSWERED ({q1_not})"
    else:
        d1, d2 = g["both_on"] - g["both_off"], g["both_on_again"] - g["both_off"]
        if min(d1, d2) >= ASLEEP_COST_MS + 2.0:
            q1 = "DETACHED REACHES THE MORNING"
        elif abs(d1 - ASLEEP_COST_MS) <= CAUSE_MS and abs(d2 - ASLEEP_COST_MS) <= CAUSE_MS:
            q1 = "DETACHED SAME AS ASLEEP"
        elif abs(d1) <= NOISE_MS and abs(d2) <= NOISE_MS:
            q1 = "NO DISPLAY COST"
        else:
            q1 = "PARTIAL"
    e1, e2 = g["both_on"] - g["second_off"], g["both_on_again"] - g["second_off"]
    if e1 >= CAUSE_MS and e2 >= CAUSE_MS:
        q2 = "SECOND MONITOR COSTS"
    elif abs(e1) <= NOISE_MS and abs(e2) <= NOISE_MS:
        q2 = "SECOND MONITOR FREE"
    else:
        q2 = "UNSETTLED"
    return {"q1": (q1, d1, d2), "q2": (q2, e1, e2)}


def score(steps):
    lines = [f"{s['name']:12s} {s['step']:14s} gpu {s['gpu'] if s['gpu'] is None else round(s['gpu'], 2)!s:>7} "
             f"({s['n']} frames)" for s in steps]
    reasons, q1_not, findings = check(steps)
    lines += [f"FINDING: {f}" for f in findings]
    if reasons:
        return 1, lines + [f"VOID: {r}" for r in reasons], None
    v = verdicts(steps, q1_not)
    q1, d1, d2 = v["q1"]
    q2, e1, e2 = v["q2"]
    if d1 is None:
        lines.append(f"Q1 both monitors detached: {q1}")
    else:
        lines.append(f"Q1 both monitors detached: {q1}, {d1:+.2f} / {d2:+.2f} ms faster than both on "
                     f"(asleep was {ASLEEP_COST_MS}; the morning needs about {ASLEEP_COST_MS + 3.0:.1f})")
        both_off = next(s["gpu"] for s in steps if s["step"] == "both_off")
        lines.append(f"both_off {both_off:.2f} ms against the morning's held {MORNING_HELD_MS} ms (report only)")
    lines.append(f"Q2 second monitor detached: {q2}, {e1:+.2f} / {e2:+.2f} ms faster than both on")
    return 0, lines, v


def load_spot(spot):
    with open(os.path.join(spot, "spot.json")) as f:
        windows = json.load(f)["windows"]
    frames = {}
    with open(os.path.join(spot, "telemetry.jsonl"), errors="replace") as f:
        for line in f:
            r = json.loads(line)
            frames[r["frame"]] = (r["qpcMs"], r["gpuTimeMs"], bool(r.get("rayTraced")))
    return windows, frames


def mon(name, primary, state):
    available, active, power = {"attached": (True, True, "on"), "detached": (False, False, "off"),
                                "deactivated": (True, False, "on"), "asleep": (True, True, "off")}[state]
    # As tests\runtime\monitors.py: only an active monitor can read primary.
    return {"name": name, "primary": primary and active, "available": available, "active": active, "power": power}


def fake_launch(gpus, override=None, frames_per_step=1100, game=None, skip=False, drop_off=False, off_frames=None):
    """Four steps of 44 s at 40 ms a frame, flat GPU ms per step with +-0.05 ripple. override: {step: (prim, second)};
    game: {step: gameWindow snapshot}; skip: both_off recorded as skipped; drop_off: no both_off window at all."""
    windows, frames, f, q = [], {}, 1000, 0.0
    for i, (step, g) in enumerate(zip(ORDER, gpus)):
        if step == "both_off" and drop_off:
            continue
        if step == "both_off" and skip:
            windows.append({"name": f"live_step{i + 1}", "monitorStep": step, "skipped": "no change in 600 s"})
            continue
        a = f
        for k in range(off_frames if step == "both_off" and off_frames is not None else frames_per_step):
            f += 1
            q += 40.0
            frames[f] = (q, g + (0.05 if k % 2 else -0.05), True)
        p, s = (override or {}).get(step, WANT[step])
        snap = [mon("ACI27A8", False, s), mon("HKC2731", True, p)]  # as monitors.py read at 18:14: HKC2731 primary
        gw = (game or {}).get(step, {"minimized": False, "foreground": True})
        windows.append({"name": f"live_step{i + 1}", "telemetry": [a, f], "monitorStep": step,
                        "monitors": {"start": snap, "end": snap}, "gameWindow": {"start": gw, "end": gw}})
    return windows, frames


def self_test():
    R, S, N, P = "DETACHED REACHES THE MORNING", "DETACHED SAME AS ASLEEP", "NO DISPLAY COST", "PARTIAL"
    C, F, U = "SECOND MONITOR COSTS", "SECOND MONITOR FREE", "UNSETTLED"
    base = [38.7, 38.6, 35.8, 38.6]
    cases = [
        ("detached reaches the morning", [38.7, 37.5, 32.8, 38.6], {}, 0, (R, C), None),
        ("detached same as asleep", base, {}, 0, (S, F), None),
        ("no display cost at all", [38.7, 38.6, 38.5, 38.6], {}, 0, (N, F), None),
        ("partial, 4.2 ms", [38.7, 38.2, 34.5, 38.6], {}, 0, (P, U), None),
        ("second faster than one neighbour only", [38.7, 38.0, 35.6, 38.4], {}, 0, (S, U), None),
        ("both_off only deactivated", [38.7, 38.6, 32.8, 38.6], {"override": {"both_off": ("deactivated", "deactivated")}},
         0, ("NOT ANSWERED (state", F), "not both detached"),
        ("both_off only asleep", base, {"override": {"both_off": ("asleep", "asleep")}}, 0, ("NOT ANSWERED (state", F),
         "not both detached"),
        ("both_off skipped (no change in 10 min)", base, {"skip": True}, 0, ("NOT ANSWERED (skipped", F), "skipped"),
        ("both_off not recorded", base, {"drop_off": True}, 0, ("NOT ANSWERED (both_off not recorded", F), None),
        ("no monitor: game minimized is a finding, not a void", base,
         {"game": {"both_off": {"minimized": True, "foreground": False}}}, 0, ("NOT ANSWERED (game minimized", F), "minimized"),
        ("no monitor: game in background is fine", base, {"game": {"both_off": {"minimized": False, "foreground": False}}},
         0, (S, F), "with no monitor: "),
        ("no monitor: game stopped presenting", base, {"off_frames": 40}, 0, ("NOT ANSWERED (0 scored frames", F),
         "presented 40 frames"),
        ("second_off left the second attached", [38.7, 38.6, 32.8, 38.6],
         {"override": {"second_off": ("attached", "attached")}}, 1, None, None),
        ("both_on_again came back with one monitor", [38.7, 38.6, 32.8, 38.6],
         {"override": {"both_on_again": ("attached", "detached")}}, 1, None, None),
        ("both_on and both_on_again drift 1 ms", [38.7, 38.6, 32.8, 37.7], {}, 1, None, None),
        ("game minimized in a driver step", base, {"game": {"second_off": {"minimized": True, "foreground": False}}},
         1, None, None),
        ("too few frames in a driver step", base, {"frames_per_step": 300}, 1, None, None),
    ]
    bad = 0
    for name, gpus, kw, want_code, want, finding in cases:
        code, lines, v = score(steps_from(*fake_launch(gpus, **kw)))
        got = (v["q1"][0], v["q2"][0]) if v else None
        ok = code == want_code and (got == want if want is None or got is None else
                                    got[0].startswith(want[0]) and got[1] == want[1])
        if finding:
            ok = ok and any(l.startswith("FINDING") and finding in l for l in lines)
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}: exit {code}, {got}")
        if not ok:
            print("     " + "\n     ".join(lines))
    # The skip: a swap chain hitch of 20 ms over the first 4 s of both_off must not move its median or its count.
    w, fr = fake_launch(base)
    a = w[2]["telemetry"][0]
    for k in range(1, 100):
        q, g, rt = fr[a + k]
        fr[a + k] = (q, g + 20.0, rt)
    steps = steps_from(w, fr)
    code, _, v = score(steps)
    ok = code == 0 and v["q1"][0] == S and steps[2]["n"] == 975
    bad += not ok
    print(f"{'ok  ' if ok else 'FAIL'} first 5 s of a step is skipped")
    print("monitor_steps self-test:", "PASS" if not bad else f"FAIL ({bad})")
    return 1 if bad else 0


def main(argv):
    if argv[1:] == ["--self-test"]:
        return self_test()
    if len(argv) != 2:
        print(__doc__)
        return 2
    code, lines, _ = score(steps_from(*load_spot(argv[1])))
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
