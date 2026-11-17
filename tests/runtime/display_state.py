"""Display state of a launch, from what was recorded before it started (machine_before.json), never from frame times.

  display_state.py launch <machine_before.json>     prints {"class", "why"} for one launch
  display_state.py run <run folder>                  prints {"class", "why", "launches"} for a run
  display_state.py compare <run A> <run B>           exit 0 when their frame times may be compared, 4 NOT COMPARABLE
  display_state.py classify <results root> [--out <tsv>]   one line per run (and launch), written to --out
  display_state.py --self-test

Why (coordinator, Nov 17 00:30): launch 20261117-002048 with Corey's two monitors switched off at the plug ran
33.6 / 32.8 ms per frame where the same launch with them on (20261116-222500, -225845) ran 37.2 to 37.8 ms, about
12 % apart, so a frame time means nothing without the display state it was measured in.

Classes:
  plug on    both of Corey's monitors (HKC2731, ACI27A8) listed Present and the display state "on"; when the active
             layout was recorded (from Nov 17 00:21), HKC2731 must also be the primary display at 240 Hz.
  plug off   no monitor Present; when the layout was recorded, the only active display is "ASUS MG278" as primary at
             60 Hz (Corey: his left monitor, the same screen as ACI27A8, still seen by Windows with its power off).
  unknown    anything else: no machine_before.json (recorded only from Nov 16 16:58), one monitor present, a layout
             that fits neither class, or a run whose launches were in different states.
The idle GPU power in machine_before.json is listed but not used: the plug-off reading (76.3 W) sits next to the
"displays asleep, plug on" reading of Nov 16 16:41 (77.4 W), so it does not separate the states.
"""
from __future__ import annotations

import glob
import json
import os
import sys

PLUG_ON, PLUG_OFF, UNKNOWN = "plug on", "plug off", "unknown"
MONITORS = ("HKC2731", "ACI27A8")
PLUG_ON_PRIMARY, PLUG_ON_HZ = "HKC2731", 240
PLUG_OFF_DISPLAY, PLUG_OFF_HZ = "ASUS MG278", 60


def _load(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _primary(displays: list[dict]) -> str:
    p = [d for d in displays if d.get("primary")]
    return ", ".join(f"{d.get('name')} {d.get('refreshHz')} Hz" for d in p) or "none"


def classify(m: dict | None) -> tuple[str, str]:
    """(class, why) for one launch's machine_before record."""
    if not m:
        return UNKNOWN, "no machine_before.json (recorded only from Nov 16 16:58)"
    present = sorted({p.split("\\")[1] for p in m.get("monitors") or [] if p.count("\\") >= 2})
    n = m.get("monitorsOn")
    displays = m.get("displays")
    if isinstance(displays, dict):   # ConvertTo-Json writes a one-item list as an object
        displays = [displays]
    if n == 2 and present == sorted(MONITORS) and m.get("display") == "on":
        if displays is not None:
            p = [d for d in displays if d.get("primary")]
            if not (len(p) == 1 and p[0].get("name") == PLUG_ON_PRIMARY
                    and round(float(p[0].get("refreshHz") or 0)) == PLUG_ON_HZ):
                return UNKNOWN, f"both monitors present but the primary display is {_primary(displays)}"
        return PLUG_ON, "both monitors present, display on" + (
            f", primary {_primary(displays)}" if displays is not None else ", layout not recorded")
    if n == 0 and not present:
        if displays is not None:
            if not (len(displays) == 1 and displays[0].get("name") == PLUG_OFF_DISPLAY and displays[0].get("primary")
                    and round(float(displays[0].get("refreshHz") or 0)) == PLUG_OFF_HZ):
                names = ", ".join(f"{d.get('name')} {d.get('refreshHz')} Hz" for d in displays) or "none"
                return UNKNOWN, f"no monitor present but the active displays are: {names}"
            return PLUG_OFF, f"no monitor present, only {PLUG_OFF_DISPLAY} active, primary at {PLUG_OFF_HZ} Hz"
        return PLUG_OFF, "no monitor present, layout not recorded"
    return UNKNOWN, f"{n} monitor(s) present ({', '.join(present) or 'none named'}), display {m.get('display')}"


def launch_state(launch: str) -> dict:
    """Class of one launch folder (holding machine_before.json)."""
    m = _load(os.path.join(launch, "machine_before.json"))
    # Monitor launches recorded the active layout separately before 282daa1 put it in machine_before.json.
    lay = _load(os.path.join(launch, "monitor_layout.json"))
    if m is not None and m.get("displays") is None and lay and lay.get("layoutBefore") is not None:
        m = dict(m, displays=lay["layoutBefore"])
    cls, why = classify(m)
    return {"launch": os.path.basename(os.path.normpath(launch)), "class": cls, "why": why,
            "time": (m or {}).get("time"), "idleGpuPowerW": (m or {}).get("idleGpuPowerW")}


def run_state(folder: str) -> dict:
    """Class of a run: every launch's machine_before.json (one per launch subfolder) must agree."""
    files = sorted(glob.glob(os.path.join(folder, "*", "machine_before.json")))
    launches = [launch_state(os.path.dirname(f)) for f in files]
    if not launches:
        return {"class": UNKNOWN, "why": "no machine_before.json (recorded only from Nov 16 16:58)", "launches": []}
    classes = sorted({x["class"] for x in launches})
    if len(classes) > 1:
        return {"class": UNKNOWN, "why": "launches in different states: " + ", ".join(
            f"{x['launch']} {x['class']}" for x in launches), "launches": launches}
    return {"class": classes[0], "why": launches[0]["why"] if len(launches) == 1 else
            f"all {len(launches)} launches {classes[0]}", "launches": launches}


def comparable(a: str, b: str) -> tuple[bool, str]:
    """Whether frame times of runs a and b may be compared: same class, and not unknown."""
    sa, sb = run_state(a), run_state(b)
    na, nb = os.path.basename(os.path.normpath(a)), os.path.basename(os.path.normpath(b))
    if sa["class"] == UNKNOWN or sb["class"] == UNKNOWN or sa["class"] != sb["class"]:
        return False, (f"NOT COMPARABLE: display state {sa['class']} in {na} ({sa['why']}), "
                       f"{sb['class']} in {nb} ({sb['why']})")
    return True, f"display state {sa['class']} in both {na} and {nb}"


def classify_root(root: str) -> list[str]:
    lines = ["run\tclass\twhy\tlaunch\tlaunch class\tmachine_before time\tidle GPU W"]
    for d in sorted(os.listdir(root)):
        folder = os.path.join(root, d)
        if not os.path.isdir(folder) or not os.path.exists(os.path.join(folder, "manifest.json")):
            continue
        s = run_state(folder)
        lines.append(f"{d}\t{s['class']}\t{s['why']}\t\t\t\t")
        if len(s["launches"]) > 1:
            lines += [f"{d}\t\t\t{x['launch']}\t{x['class']}\t{x['time']}\t{x['idleGpuPowerW']}" for x in s["launches"]]
        elif s["launches"]:
            x = s["launches"][0]
            lines[-1] = f"{d}\t{s['class']}\t{s['why']}\t{x['launch']}\t{x['class']}\t{x['time']}\t{x['idleGpuPowerW']}"
    return lines


# Fake machine_before records, copied field for field from the real ones they are named after.
ON_212703 = {"time": "21:27:15", "display": "on", "monitorsOn": 2, "idleGpuPowerW": 81.78,
             "monitors": ["DISPLAY\\ACI27A8\\5&D0E7D33&2&UID4357", "DISPLAY\\HKC2731\\5&D0E7D33&2&UID4353"]}
OFF_002048 = {"time": "00:21:10", "display": "on", "monitorsOn": 0, "idleGpuPowerW": 76.31, "monitors": [],
              "monitorsOffAsked": True,
              "displays": {"name": "ASUS MG278", "primary": True, "width": 2560, "height": 1440, "x": 0, "y": 0,
                           "refreshHz": 60.0}}
ON_LAYOUT = dict(ON_212703, displays=[
    {"name": "ACI27A8", "primary": False, "x": -2560, "refreshHz": 143.86},
    {"name": "HKC2731", "primary": True, "x": 0, "refreshHz": 240.0}])


def self_test() -> int:
    import tempfile
    fails = 0

    def check(name, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" (want {want!r})"))

    one = dict(ON_212703, monitorsOn=1, monitors=ON_212703["monitors"][:1])
    asleep = dict(ON_212703, display="off")
    check("212703 record", classify(ON_212703)[0], PLUG_ON)
    check("002048 record", classify(OFF_002048)[0], PLUG_OFF)
    check("plug on with the 240 Hz layout", classify(ON_LAYOUT)[0], PLUG_ON)
    check("one monitor present", classify(one)[0], UNKNOWN)
    check("both present, display off", classify(asleep)[0], UNKNOWN)
    check("no record", classify(None)[0], UNKNOWN)
    check("both present but MG278 primary", classify(dict(ON_212703, displays=OFF_002048["displays"]))[0], UNKNOWN)
    check("none present but HKC2731 active",
          classify(dict(OFF_002048, displays=[{"name": "HKC2731", "primary": True, "refreshHz": 240.0}]))[0], UNKNOWN)
    check("none present, MG278 at 144 Hz", classify(dict(OFF_002048, displays=dict(OFF_002048["displays"],
                                                                                 refreshHz=143.86)))[0], UNKNOWN)
    with tempfile.TemporaryDirectory() as root:
        def run(name, *records, layout=None):
            os.makedirs(os.path.join(root, name), exist_ok=True)
            for i, r in enumerate(records):
                os.makedirs(os.path.join(root, name, f"l{i}"), exist_ok=True)
                with open(os.path.join(root, name, f"l{i}", "machine_before.json"), "w") as f:
                    json.dump(r, f)
                if layout is not None:
                    with open(os.path.join(root, name, f"l{i}", "monitor_layout.json"), "w") as f:
                        json.dump({"layoutBefore": layout}, f)
            open(os.path.join(root, name, "manifest.json"), "w").write("{}")
            return os.path.join(root, name)
        on_a, on_b = run("on_a", ON_212703), run("on_b", ON_LAYOUT, ON_212703)
        off, mixed = run("off", OFF_002048), run("mixed", ON_212703, OFF_002048)
        old = run("old")
        check("plug off against plug on", comparable(off, on_a)[0], False)
        check("plug on against plug off", comparable(on_a, off)[0], False)
        check("two plug-on runs", comparable(on_a, on_b)[0], True)
        check("two plug-off runs", comparable(off, run("off2", OFF_002048))[0], True)
        check("run without a record against plug on", comparable(old, on_a)[0], False)
        check("two runs without a record", comparable(old, run("old2"))[0], False)
        check("mixed run class", run_state(mixed)["class"], UNKNOWN)
        check("mixed run against plug on", comparable(mixed, on_a)[0], False)
        check("monitor_layout.json with HKC2731 240 Hz primary",
              run_state(run("lay_on", ON_212703, layout=ON_LAYOUT["displays"]))["why"],
              "both monitors present, display on, primary HKC2731 240.0 Hz")
        check("monitor_layout.json with MG278 primary while both present",
              run_state(run("lay_bad", ON_212703, layout=[OFF_002048["displays"]]))["class"], UNKNOWN)
        check("refusal names both states", comparable(off, on_a)[1].startswith("NOT COMPARABLE: display state plug off"),
              True)
    print("self-test:", "PASS" if not fails else f"FAIL ({fails})")
    return 1 if fails else 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--self-test"]:
        return self_test()
    if len(argv) >= 2 and argv[0] == "launch":
        cls, why = classify(_load(argv[1]))
        print(json.dumps({"class": cls, "why": why}))
        return 0
    if len(argv) >= 2 and argv[0] == "run":
        print(json.dumps(run_state(argv[1])))
        return 0
    if len(argv) >= 3 and argv[0] == "compare":
        ok, why = comparable(argv[1], argv[2])
        print(why)
        return 0 if ok else 4
    if len(argv) >= 2 and argv[0] == "classify":
        lines = classify_root(argv[1])
        if "--out" in argv:
            with open(argv[argv.index("--out") + 1], "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(lines) + "\n")
        print("\n".join(lines))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
