"""No-game proof that the second_off method comes back by itself (worker C's monitor spec, Nov 16).

Method: DDC/CI VCP 0xD6 = 5 (power off, as the button) on the second monitor through a handle opened while it was
attached, then VCP 0xD6 = 1 through the same handle. Per trial, recorded: the layout before, the monitors when off
(detached = targetAvailable false), the monitors and layout after, and whether the gate's display check reads "on".
A trial passes when the monitor detached within OFF_WAIT s and came back attached, with the same layout and the
display state "on", within ON_WAIT s. Stops at the first failure (never leaves a second trial running on a monitor
that did not come back).

Usage: monitor_proof.py OUTDIR [--trials 3] [--name ACI27A8]   exit 0 = every trial passed
Refuses to run while a game process is up.
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import display  # noqa: E402
import monitors  # noqa: E402

OFF_WAIT, ON_WAIT, HOLD = 30.0, 90.0, 10.0
GAMES = ("Blacklist_game.exe", "Blacklist_DX11_game.exe", "Blacklist_Launcher.exe", "NvRemixBridge.exe")


def game_running() -> bool:
    out = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True, text=True).stdout
    return any(g.lower() in out.lower() for g in GAMES)


def by_name(name):
    return next(m for m in monitors.monitors(pnp=False)[0] if m["name"] == name)


def poll(name, want_available, seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        m = by_name(name)
        if m["available"] == want_available and (not want_available or m["active"]):
            return round(time.time() - t0, 1), m
        time.sleep(0.5)
    return None, by_name(name)


def trial(name):
    rec = {"before": monitors.snapshot(), "start": time.strftime("%H:%M:%S")}
    rec["offAck"] = monitors.ddc_power(name, monitors.DDC_OFF)
    rec["offAfterS"], rec["offMonitor"] = poll(name, False, OFF_WAIT)
    rec["off"] = monitors.snapshot()
    time.sleep(HOLD)
    rec["onAck"] = monitors.ddc_power(name, monitors.DDC_ON)
    rec["onAfterS"], rec["onMonitor"] = poll(name, True, ON_WAIT)
    if rec["onAfterS"] is None:   # one retry of the write, then report
        rec["onAck2"] = monitors.ddc_power(name, monitors.DDC_ON)
        rec["onAfterS2"], rec["onMonitor"] = poll(name, True, ON_WAIT)
    time.sleep(3)                 # let Windows put the layout back before comparing
    rec["after"] = monitors.snapshot()
    rec["gateDisplay"] = display.state()
    back = rec["onMonitor"]["available"] and rec["onMonitor"]["active"]
    same = rec["after"]["layout"] == rec["before"]["layout"]
    rec["detached"] = rec["offAfterS"] is not None
    rec["back"], rec["sameLayout"] = back, same
    rec["pass"] = bool(rec["detached"] and back and same and rec["gateDisplay"] == "on")
    return rec


def main(argv):
    out = argv[0]
    trials = int(argv[argv.index("--trials") + 1]) if "--trials" in argv else 3
    name = argv[argv.index("--name") + 1] if "--name" in argv else "ACI27A8"
    if game_running():
        print("refused: a game process is running")
        return 2
    os.makedirs(out, exist_ok=True)
    display.wake()
    monitors.ddc_open()   # handles opened while attached, kept for the whole proof
    res = {"method": f"DDC/CI VCP 0xD6=5 then 0xD6=1 on {name} through a handle opened while attached",
           "trials": []}
    for i in range(trials):
        r = trial(name)
        res["trials"].append(r)
        print(f"trial {i + 1}: detached {r['detached']} after {r['offAfterS']} s, back {r['back']} after "
              f"{r.get('onAfterS')} s{'' if r.get('onAfterS2') is None else ' (second write ' + str(r['onAfterS2']) + ' s)'}, "
              f"same layout {r['sameLayout']}, gate display {r['gateDisplay']}: {'PASS' if r['pass'] else 'FAIL'}",
              flush=True)
        with open(os.path.join(out, "monitor_proof.json"), "w") as f:
            json.dump(res, f, indent=1)
        if not r["pass"]:
            break
    res["passed"] = len(res["trials"]) == trials and all(r["pass"] for r in res["trials"])
    with open(os.path.join(out, "monitor_proof.json"), "w") as f:
        json.dump(res, f, indent=1)
    print("PROOF", "PASS" if res["passed"] else "FAIL")
    return 0 if res["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
