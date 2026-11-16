"""Does detaching the monitors explain the morning's fast frames? (worker C, Nov 16)

Morning launches on Nov 16 ran with both DisplayPort monitors (HKC2731, ACI27A8) switched off at their buttons, which
normally drops them off the PC. At con_custodial the morning held pose read 32.73 ms GPU (0c13881, 10:24). Today the
displays asleep read about 2.9 ms less than awake (171502 held steps: ASLEEP_COST_MS), and about 3 ms stays unexplained.
This scores one held-pose launch whose live steps carry "monitorStep": both_on, second_off, both_off, both_on_again
(in that order), each with the monitor state Windows reported at the step's start and end.

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
Each step must show, at start and end: both_on and both_on_again both attached; second_off the primary attached and
the other detached; both_off both detached. Also at start and end: w["gameWindow"] not minimized and foreground.
A step whose state is wrong voids the launch (no verdicts), except both_off reading deactivated or asleep, which
scores Q2 but answers Q1 "NOT ANSWERED (state)".

Per step: median gpuTimeMs over ray-traced telemetry frames, skipping the first SKIP_MS (a monitor change can recreate
the swap chain and hitch). VOID also when: both_on and both_on_again differ by more than NOISE_MS, or a step has fewer
than MIN_FRAMES scored frames.

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


def states(snapshot):
    """{'primary': state, 'second': state} from one start or end snapshot; None when it does not list two monitors."""
    prim = [m for m in snapshot if m.get("primary")]
    other = [m for m in snapshot if not m.get("primary")]
    if len(prim) != 1 or len(other) != 1:
        return None
    return {"primary": classify(prim[0]), "second": classify(other[0])}


WANT = {"both_on": ("attached", "attached"), "second_off": ("attached", "detached"),
        "both_off": ("detached", "detached"), "both_on_again": ("attached", "attached")}


def steps_from(windows, frames):
    out = []
    for w in windows:
        if "monitorStep" not in w or not w.get("telemetry"):
            continue
        a, b = w["telemetry"]
        rows = [frames[f] for f in range(a + 1, b + 1) if f in frames]
        gpu = []
        if rows:
            t0 = rows[0][0]
            gpu = [g for q, g, rt in rows if rt and q - t0 >= SKIP_MS]
        out.append({"name": w["name"], "step": w["monitorStep"], "gpu": st.median(gpu) if gpu else None,
                    "n": len(gpu), "w": w})
    return out


def check(steps):
    """Returns (void reasons, both_off answers Q1)."""
    if [s["step"] for s in steps] != ORDER:
        return [f"steps {[s['step'] for s in steps]} are not {ORDER}"], False
    reasons, q1 = [], True
    for s in steps:
        if s["n"] < MIN_FRAMES:
            reasons.append(f"{s['name']}: {s['n']} scored frames, below {MIN_FRAMES}")
        mon = s["w"].get("monitors") or {}
        for when in ("start", "end"):
            got = states(mon.get(when) or [])
            want = dict(zip(("primary", "second"), WANT[s["step"]]))
            if got != want:
                if s["step"] == "both_off" and got and all(v in ("deactivated", "asleep", "detached") for v in got.values()):
                    q1 = False
                else:
                    reasons.append(f"{s['name']} {when}: monitors {got}, want {want}")
            gw = (s["w"].get("gameWindow") or {}).get(when) or {}
            if gw.get("minimized") is not False or gw.get("foreground") is not True:
                reasons.append(f"{s['name']} {when}: game window {gw or 'not recorded'}, want not minimized and foreground")
    on = [s["gpu"] for s in steps if s["step"] in ("both_on", "both_on_again") and s["gpu"] is not None]
    if len(on) == 2 and abs(on[0] - on[1]) > NOISE_MS:
        reasons.append(f"both_on and both_on_again differ {abs(on[0] - on[1]):.2f} ms, above {NOISE_MS}")
    return reasons, q1


def verdicts(steps, q1_answered=True):
    g = {s["step"]: s["gpu"] for s in steps}
    d1, d2 = g["both_on"] - g["both_off"], g["both_on_again"] - g["both_off"]
    if not q1_answered:
        q1 = "NOT ANSWERED (state)"
    elif min(d1, d2) >= ASLEEP_COST_MS + 2.0:
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
    reasons, q1_answered = check(steps)
    if reasons:
        return 1, lines + [f"VOID: {r}" for r in reasons], None
    v = verdicts(steps, q1_answered)
    q1, d1, d2 = v["q1"]
    q2, e1, e2 = v["q2"]
    lines.append(f"Q1 both monitors detached: {q1}, {d1:+.2f} / {d2:+.2f} ms faster than both on "
                 f"(asleep was {ASLEEP_COST_MS}; the morning needs about {ASLEEP_COST_MS + 3.0:.1f})")
    lines.append(f"Q2 second monitor detached: {q2}, {e1:+.2f} / {e2:+.2f} ms faster than both on")
    both_off = next(s["gpu"] for s in steps if s["step"] == "both_off")
    lines.append(f"both_off {both_off:.2f} ms against the morning's held {MORNING_HELD_MS} ms (report only)")
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
    return {"name": name, "primary": primary, "available": available, "active": active, "power": power}


def fake_launch(gpus, override=None, frames_per_step=1100, game=None):
    """Four steps of 44 s at 40 ms a frame, flat GPU ms per step with +-0.05 ripple. override: {step: (prim, second)}."""
    windows, frames, f, q = [], {}, 1000, 0.0
    for i, (step, g) in enumerate(zip(ORDER, gpus)):
        a = f
        for k in range(frames_per_step):
            f += 1
            q += 40.0
            frames[f] = (q, g + (0.05 if k % 2 else -0.05), True)
        p, s = (override or {}).get(step, WANT[step])
        snap = [mon("ACI27A8", True, p), mon("HKC2731", False, s)]
        gw = game or {"minimized": False, "foreground": True}
        windows.append({"name": f"live_step{i + 1}", "telemetry": [a, f], "monitorStep": step,
                        "monitors": {"start": snap, "end": snap}, "gameWindow": {"start": gw, "end": gw}})
    return windows, frames


def self_test():
    R, S, N, P = "DETACHED REACHES THE MORNING", "DETACHED SAME AS ASLEEP", "NO DISPLAY COST", "PARTIAL"
    cases = [
        ("detached reaches the morning", [38.7, 37.5, 32.8, 38.6], {}, 0, (R, "SECOND MONITOR COSTS")),
        ("detached same as asleep", [38.7, 38.6, 35.8, 38.6], {}, 0, (S, "SECOND MONITOR FREE")),
        ("no display cost at all", [38.7, 38.6, 38.5, 38.6], {}, 0, (N, "SECOND MONITOR FREE")),
        ("partial, 4.2 ms", [38.7, 38.2, 34.5, 38.6], {}, 0, (P, "UNSETTLED")),
        ("second faster than one neighbour only", [38.7, 38.0, 35.6, 38.4], {}, 0, (S, "UNSETTLED")),
        ("both_off only deactivated", [38.7, 38.6, 32.8, 38.6], {"override": {"both_off": ("deactivated", "deactivated")}},
         0, ("NOT ANSWERED (state)", "SECOND MONITOR FREE")),
        ("both_off only asleep", [38.7, 38.6, 35.8, 38.6], {"override": {"both_off": ("asleep", "asleep")}},
         0, ("NOT ANSWERED (state)", "SECOND MONITOR FREE")),
        ("second_off left the second attached", [38.7, 38.6, 32.8, 38.6],
         {"override": {"second_off": ("attached", "attached")}}, 1, None),
        ("both_on_again came back with one monitor", [38.7, 38.6, 32.8, 38.6],
         {"override": {"both_on_again": ("attached", "detached")}}, 1, None),
        ("both_on and both_on_again drift 1 ms", [38.7, 38.6, 32.8, 37.7], {}, 1, None),
        ("game minimized", [38.7, 38.6, 32.8, 38.6], {"game": {"minimized": True, "foreground": False}}, 1, None),
        ("too few frames", [38.7, 38.6, 32.8, 38.6], {"frames_per_step": 300}, 1, None),
    ]
    bad = 0
    for name, gpus, kw, want_code, want in cases:
        code, lines, v = score(steps_from(*fake_launch(gpus, **kw)))
        got = (v["q1"][0], v["q2"][0]) if v else None
        ok = code == want_code and got == want
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}: exit {code}, {got}")
        if not ok:
            print("     " + "\n     ".join(lines))
    # The skip: a swap chain hitch of 20 ms over the first 4 s of both_off must not move its median or its count.
    w, fr = fake_launch([38.7, 38.6, 35.8, 38.6])
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
