"""Amp-window test (coordinator, Nov 17 after launch (i)): does the Amp app's window drawing cost the game frame time?

One con_custodial launch at y0_p0, same build (Remix ee695ff, proxy FC8ECC61641E), five 45 s plug-on windows in the
order as is, minimized, as is, minimized, as is. The only change between them is ShowWindow minimize / restore of the
Amp.exe main window (drive.py live step "ampWindow"). Rules fixed before the launch:

  frame gap  median time between Presents in each window (monitors_off_score.window_gap, proxy_frames.csv column 3).
  floor      0.626 ms, the spread of last night's four plug-on windows (37.163 to 37.789).
  EFFECT     both minimized windows are faster than every as-is window by more than the floor.
  NO EFFECT  every minimized window is within the floor of every as-is window.
  NOT SETTLED anything else.
  VOID       display state not plug on at the start or end of the launch, a display not on (asleep) inside a window,
             outside input inside a window, a window's Amp state not read back, or the three as-is windows differing
             among themselves by more than the floor.
  NOT MEASURED  the windows are not the five asked in that order, a window was skipped, held under 40 s, or has no
             frame gap.

Reported per window next to the frame gap (the verdict rests on the frame gap only): the Amp WebView GPU process's 3d
share (gpu_engines.csv rows of ampWindowTest.before.webviewGpuPid; NOT RECORDED when no counted sample), and worker C's
stages from monitor_stage_split.window_medians: "remix gpu outside timed passes" and the four ray-tracing passes.
This test shows only whether the Amp window drawing matters. It does not separate refresh rate from the game window
being covered by the Amp window.

Outside input: display.csv writes a row every ~2 s; a row at time T with input "outside" covers input in (T-2, T], so it
counts inside a window [a, b] when a < T < b + 2.

Exit 0 = EFFECT, NO EFFECT or NOT SETTLED (a verdict), 3 = VOID, 1 = NOT MEASURED.
Run: python -I amp_window_score.py <spot folder>   |   python -I amp_window_score.py --self-test
"""
import csv
import os
import statistics as st
import sys
import json
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "runtime"))
import display_state  # noqa: E402
import monitors_off_score as mos  # noqa: E402

FLOOR_MS = 0.626
ORDER = ("as_is", "minimized", "as_is", "minimized", "as_is")
PASSES = ("remix gpu outside timed passes", "gpu InjectRTX/Integrate Raytracing", "gpu InjectRTX/Ray Reconstruction",
          "gpu InjectRTX/RTXDI", "gpu InjectRTX/NRC: Training and Resolve")
ROW_SPAN_S = 2.0
NOT_RECORDED = "NOT RECORDED"


def _secs(text: str) -> float | None:
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S"):
        try:
            t = datetime.strptime(text.strip(), fmt)
            return t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6
        except ValueError:
            pass
    return None


def outside_inputs(rows: list[dict], clock: list[str]) -> tuple[list[str], list[str]]:
    """(times of "outside" input rows, times of display-not-on rows) whose 2 s span overlaps the window clock."""
    a, b = _secs(clock[0]), _secs(clock[1])
    ins, dark = [], []
    for r in rows:
        t = _secs(r.get("time", ""))
        if t is None or not (a < t < b + ROW_SPAN_S):
            continue
        if r.get("input") == "outside":
            ins.append(r["time"])
        if r.get("display") != "on":
            dark.append(r["time"])
    return ins, dark


def webview_3d(path: str, pid: int | None, clock: list[str]) -> float | str:
    """Median per counted sample of the 3d percent of one process id inside the window; a counted sample without the
    pid is 0 %; no counted sample (or no pid) reads NOT RECORDED."""
    if not pid or not os.path.exists(path):
        return NOT_RECORDED
    a, b = _secs(clock[0]), _secs(clock[1])
    samples: dict[str, dict] = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        next(f, None)
        for ln in f:
            p = ln.rstrip("\r\n").split(",")
            if len(p) < 6 or (t := _secs(p[0])) is None or not (a <= t <= b):
                continue
            s = samples.setdefault(p[0], {"n": 0, "pct": 0.0})
            if p[2] == "_sample":
                s["n"] = int(p[5] or 0)
            elif p[1] == str(pid) and p[3] == "3d":
                try:
                    s["pct"] += float(p[4])
                except ValueError:
                    pass
    got = [s["pct"] for s in samples.values() if s["n"] > 0]
    return round(st.median(got), 2) if got else NOT_RECORDED


def verdict(wins: list[dict], start_on: bool, end_on: bool, floor: float = FLOOR_MS,
            breaks: frozenset = frozenset()) -> tuple[str, list[str]]:
    """wins: [{name, asked, gap, held, readBack, outside: [times], dark: [times], skipped}] in launch order.
    breaks: rules switched off, used only by the self-test to show the fake cases catch them."""
    why = []
    if not start_on:
        why.append("display state not plug on at the launch start")
    if not end_on:
        why.append("display state not plug on at the launch end")
    for w in wins:
        if w.get("outside"):
            why.append(f"{w['name']}: outside input at {', '.join(w['outside'])}")
        if w.get("dark"):
            why.append(f"{w['name']}: display not on at {', '.join(w['dark'])}")
        if not w.get("readBack") and "readback" not in breaks:
            why.append(f"{w['name']}: Amp window state {w.get('asked')} not read back")
    asis = [w["gap"] for w in wins if w.get("asked") == "as_is" and w.get("gap") is not None]
    if len(asis) > 1 and round(max(asis) - min(asis), 3) > floor and "spread" not in breaks:
        why.append(f"as-is windows differ by {max(asis) - min(asis):.3f} ms, over the floor {floor}")
    if why:
        return "VOID", why
    if tuple(w.get("asked") for w in wins) != ORDER:
        return "NOT MEASURED", [f"windows asked {[w.get('asked') for w in wins]}, want {list(ORDER)}"]
    for w in wins:
        if w.get("skipped"):
            why.append(f"{w['name']} skipped: {w['skipped']}")
        elif (w.get("held") or 0) < mos.MIN_HELD_S:
            why.append(f"{w['name']} held {w.get('held')} s, under {mos.MIN_HELD_S}")
        elif w.get("gap") is None:
            why.append(f"{w['name']} has no frame gap")
    if why:
        return "NOT MEASURED", why
    mins = [w["gap"] for w in wins if w["asked"] == "minimized"]
    diffs = [round(a - m, 3) for a in asis for m in mins]
    faster = (lambda d: d >= floor) if "strict" in breaks else (lambda d: d > floor)
    if all(faster(d) for d in diffs):
        return "EFFECT", [f"every minimized window faster than every as-is window by {min(diffs):.3f} ms or more"]
    if all(abs(d) <= floor for d in diffs):
        return "NO EFFECT", [f"every minimized window within {max(abs(d) for d in diffs):.3f} ms of every as-is window"]
    return "NOT SETTLED", [f"as-is minus minimized ranges {min(diffs):.3f} to {max(diffs):.3f} ms against the floor "
                           f"{floor}"]


def plug_on_snap(snap: list[dict] | None) -> bool:
    """Both of Corey's monitors attached and HKC2731 primary (the plug-on class)."""
    prim = [m["name"] for m in snap or [] if m.get("primary")]
    return mos.attached(snap) and prim == ["HKC2731"]


def measure(folder: str) -> tuple[list[dict], bool, bool, list[str], int | None]:
    import monitor_stage_split as mss
    spot = json.load(open(os.path.join(folder, "spot.json"), encoding="utf-8"))
    raw = [w for w in spot.get("windows", []) if w.get("monitorStep")]
    test = spot.get("ampWindowTest") or {}
    pid = (test.get("before") or {}).get("webviewGpuPid")
    rows = list(csv.DictReader(open(os.path.join(folder, "display.csv"), encoding="utf-8")))
    gaps = mos.frame_gaps(folder)
    ds = display_state.launch_state(folder)
    notes = [f"display state before the launch: {ds['class']} ({ds['why']})",
             f"Amp window at launch start / end: {(spot.get('ampWindow') or {}).get('start')} / "
             f"{(spot.get('ampWindow') or {}).get('end')}",
             f"Amp window after the live steps: {test.get('after')}"]
    wins = []
    for w in raw:
        aw = w.get("ampWindow") or {}
        mons = w.get("monitors") or {}
        rec = {"name": w["name"], "asked": aw.get("asked"), "readBack": aw.get("readBack") is True,
               "skipped": w.get("skipped"), "held": mos.held_s(w), "gap": None, "outside": [], "dark": [],
               "attached": plug_on_snap(mons.get("start")) and plug_on_snap(mons.get("end")),
               "ampStart": {k: (aw.get("start") or {}).get(k) for k in ("minimized", "maximized", "rect", "monitor")},
               "ampEnd": {k: (aw.get("end") or {}).get(k) for k in ("minimized", "maximized", "rect", "monitor")}}
        if not w.get("skipped") and w.get("clock"):
            rec["gap"], rec["frames"] = mos.window_gap(gaps, w)
            rec["outside"], rec["dark"] = outside_inputs(rows, w["clock"])
            rec["webview3d"] = webview_3d(os.path.join(folder, "gpu_engines.csv"), pid, w["clock"])
            med = mss.window_medians(folder, w) if w.get("proxy") and w.get("telemetry") else {}
            rec["stages"] = {k: (med.get(k) or (None,))[0] for k in PASSES}
            prog = {k: v[0] for k, v in med.items() if k.startswith("program gpu% ")}
            rec["programs"] = prog or NOT_RECORDED
        wins.append(rec)
    start_on = ds["class"] == display_state.PLUG_ON and bool(wins) and wins[0]["attached"]
    end_on = bool(wins) and all(w["attached"] for w in wins)
    return wins, start_on, end_on, notes, pid


def report(folder: str) -> int:
    try:
        wins, start_on, end_on, notes, pid = measure(folder)
    except (OSError, KeyError, ValueError) as e:
        print(f"AMP WINDOW TEST: NOT MEASURED ({type(e).__name__}: {e})")
        return 1
    for n in notes:
        print(n)
    print(f"floor {FLOOR_MS} ms; WebView GPU pid {pid}")
    print(f"{'window':12} {'asked':10} {'gap ms':>8} {'held s':>7} {'readBack':>8} {'webview3d%':>10}  "
          + "  ".join(p.replace("gpu InjectRTX/", "") for p in PASSES))
    for w in wins:
        st_ = w.get("stages") or {}
        print(f"{w['name']:12} {str(w['asked']):10} {str(w['gap']):>8} {str(w['held']):>7} {str(w['readBack']):>8} "
              f"{str(w.get('webview3d')):>10}  " + "  ".join(str(st_.get(p)) for p in PASSES))
        print(f"{'':12} Amp window start {w['ampStart']} end {w['ampEnd']}; plug on start and end {w['attached']}")
        print(f"{'':12} program gpu% medians {w.get('programs')}")
    v, why = verdict(wins, start_on, end_on)
    print(f"AMP WINDOW TEST: {v}")
    for y in why:
        print(f"  {y}")
    print("one launch until a second launch repeats it; it does not separate refresh rate from the game window being "
          "covered by the Amp window")
    return {"VOID": 3, "NOT MEASURED": 1}.get(v, 0)


def _wins(gaps, asked=ORDER, **over):
    out = [{"name": f"live_step{i + 1}", "asked": a, "gap": g, "held": 45.0, "readBack": True, "outside": [],
            "dark": []} for i, (a, g) in enumerate(zip(asked, gaps))]
    for i, kv in over.items():
        out[int(i[1:])].update(kv)
    return out


def self_test(breaks: frozenset = frozenset()) -> int:
    cases = [
        ("EFFECT: both minimized ~0.8 ms faster", _wins([38.5, 37.6, 38.6, 37.5, 38.4]), True, True, "EFFECT"),
        ("NO EFFECT: all within 0.3 ms", _wins([38.5, 38.3, 38.6, 38.55, 38.4]), True, True, "NO EFFECT"),
        ("NOT SETTLED: one minimized faster, one not", _wins([38.5, 37.6, 38.6, 38.5, 38.4]), True, True,
         "NOT SETTLED"),
        ("boundary: exactly the floor faster is not an effect", _wins([38.5, 37.874, 38.5, 37.874, 38.5]), True, True,
         "NO EFFECT"),
        ("boundary: 0.627 ms faster is an effect", _wins([38.5, 37.873, 38.5, 37.873, 38.5]), True, True, "EFFECT"),
        ("VOID: plug off at the start", _wins([38.5, 37.6, 38.6, 37.5, 38.4]), False, True, "VOID"),
        ("VOID: plug off at the end", _wins([38.5, 37.6, 38.6, 37.5, 38.4]), True, False, "VOID"),
        ("VOID: outside input in window 3", _wins([38.5, 37.6, 38.6, 37.5, 38.4], w2={"outside": ["11:00:01"]}),
         True, True, "VOID"),
        ("VOID: display asleep in window 5", _wins([38.5, 37.6, 38.6, 37.5, 38.4], w4={"dark": ["11:03:00"]}),
         True, True, "VOID"),
        ("VOID: window 2 state not read back", _wins([38.5, 37.6, 38.6, 37.5, 38.4], w1={"readBack": False}),
         True, True, "VOID"),
        ("VOID: as-is windows differ by 0.7 ms", _wins([38.0, 37.0, 38.7, 37.1, 38.3]), True, True, "VOID"),
        ("NOT MEASURED: wrong order", _wins([38.5, 37.6, 38.6, 37.5, 38.4], asked=("minimized",) + ORDER[1:]),
         True, True, "NOT MEASURED"),
        ("NOT MEASURED: window 4 held 30 s", _wins([38.5, 37.6, 38.6, 37.5, 38.4], w3={"held": 30.0}), True, True,
         "NOT MEASURED"),
    ]
    bad = 0
    for name, wins, s, e, want in cases:
        got = verdict(wins, s, e, breaks=breaks)[0]
        ok = got == want
        bad += not ok
        if not breaks:
            print(f"{'ok  ' if ok else 'FAIL'} {name}: {got}")
    rows = [{"time": "10:59:59", "input": "outside", "display": "on"},   # covers 10:59:57..59, before the window
            {"time": "11:00:01", "input": "outside", "display": "on"},   # covers 10:59:59..11:00:01, overlaps the start
            {"time": "11:00:46", "input": "outside", "display": "on"},   # 0.1 s under b + 2: counts
            {"time": "11:00:47", "input": "outside", "display": "on"},   # 1.0 s past b + 2: does not
            {"time": "11:00:20", "input": "gate", "display": "off"}]
    ins, dark = outside_inputs(rows, ["11:00:00.000", "11:00:44.100"])
    for name, ok in [("outside rows 11:00:01 and 11:00:46 counted, 10:59:59 and 11:00:47 not",
                      ins == ["11:00:01", "11:00:46"]), ("display off row counted", dark == ["11:00:20"])]:
        bad += not ok
        if not breaks:
            print(f"{'ok  ' if ok else 'FAIL'} {name}")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "gpu_engines.csv")
        with open(p, "w", encoding="utf-8") as f:
            f.write("time,pid,process,engtype,percent,instances\n")
            for t, pct in (("11:00:01.000", 10.0), ("11:00:02.000", 10.0), ("11:00:03.000", None)):
                f.write(f"{t},,_sample,,,40\n")
                f.write(f"{t},96,dwm,3d,3.0,\n")
                if pct is not None:
                    f.write(f"{t},40044,msedgewebview2,3d,{pct},\n")
            f.write("11:01:00.000,,_sample,,,0\n")
        for name, got, want in [("webview 10 % in two samples, absent in one: median 10", 
                                 webview_3d(p, 40044, ["11:00:00", "11:00:10"]), 10.0),
                                ("empty counter reads NOT RECORDED", webview_3d(p, 40044, ["11:00:30", "11:01:10"]),
                                 NOT_RECORDED),
                                ("no pid reads NOT RECORDED", webview_3d(p, None, ["11:00:00", "11:00:10"]),
                                 NOT_RECORDED)]:
            ok = got == want
            bad += not ok
            if not breaks:
                print(f"{'ok  ' if ok else 'FAIL'} {name}: {got}")
    if not breaks:
        print(f"self-test {'PASS' if not bad else 'FAIL'}")
        for b in ("spread", "readback", "strict"):
            n = self_test(frozenset([b]))
            print(f"break {b!r}: {n} case(s) fail ({'ok, the fake cases catch it' if n else 'NOT CAUGHT'})")
            bad += not n
        print(f"self-test with breaks {'PASS' if not bad else 'FAIL'}")
        return 0 if not bad else 1
    return bad


def main(argv: list[str]) -> int:
    if argv[:1] == ["--self-test"]:
        return self_test()
    if len(argv) != 1:
        print(__doc__)
        return 2
    return report(argv[0])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
