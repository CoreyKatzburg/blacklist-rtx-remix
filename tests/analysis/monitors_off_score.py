"""Monitors-off launch against the two monitors-on monitor launches (coordinator, Nov 16 23:50, Corey's rule 2).

The question: does a con_custodial launch with both monitors switched off at the plug (Windows reads both detached)
run and look the same as with both on? The monitors-off launch holds y0_p0 like the monitor launches, with live steps
"monitorStep": "both_off" of 45 s each, both monitors already off when the launch starts. It is compared with the two
monitors-on launches 20261116-222500 and 20261116-225845 (same build: Remix ee695ff, proxy FC8ECC61641E, both monitors
attached and the displays on), whose numbers and the pass limits are fixed below BEFORE the monitors-off launch:

  frame gap   median time between Presents (proxy_frames.csv column 3) over each 45 s held window. Monitors on:
              both_on 37.698 / 37.789 ms, both_on_again 37.163 / 37.253 ms, so 37.163 to 37.789 (spread 0.626 ms).
              Pass: every monitors-off window inside that range widened by NOISE_MS 0.4 (worker C's monitor_steps.py
              noise limit), 36.763 to 38.189 ms.
  brightness  gamma_mean of the Remix final picture at y0_p0 (gate\\y0_p0_final.pfm, pfm_brightness.py's statistic,
              HUD masked). Monitors on: 0.48892 / 0.49032 (they differ by 0.29 %). Pass: within 1.2 % of their mean
              (0.988 to 1.012, the wide end of worker A's original-against-original floor 0.996 to 1.012), so
              0.48375 to 0.49550.
  capture     mean of the gdigrab screen capture frames\\bench.gray (0..1). Monitors on: 0.4871 / 0.4895. Reported with
              the same 1.2 % band as its own verdict: it says whether screen captures taken with the monitors off
              count, not whether the game behaves the same.

Exit 0 = MATCH (frame gap and brightness both pass), 3 = DIFFER (either fails), 1 = NOT MEASURED (a both_off window
was skipped, too short, or did not read both monitors detached at its start and end; or a file is missing),
2 = bad input (the monitors-on folders no longer give the numbers above).
--plug-on <spot folder> (morning confirm, Nov 17): the same launch with Corey's monitors on again, live steps both_on.
Same limits; it must read plug on with both monitors attached at each window's start and end. Exit 0 = BACK (frame gap
and brightness inside), 3 = NOT BACK, 1 = NOT MEASURED, 2 = bad input.
Run: python -I monitors_off_score.py [--plug-on] <spot folder>   |   python -I monitors_off_score.py --self-test
"""
import json
import os
import statistics as st
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "brightness"))
import pfm_brightness  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runtime"))
import display_state  # noqa: E402

GATE = r"D:\BlacklistOverhaul\experiments\remix-gate"
ON_RUNS = [os.path.join(GATE, r, "01_monitors") for r in ("20261116-222500-settings", "20261116-225845-settings")]
ON_GAPS = {"20261116-222500-settings": {"both_on": 37.698, "both_on_again": 37.163},
           "20261116-225845-settings": {"both_on": 37.789, "both_on_again": 37.253}}
ON_BRIGHT = {"20261116-222500-settings": 0.48892, "20261116-225845-settings": 0.49032}
ON_GRAY = {"20261116-222500-settings": 0.4871, "20261116-225845-settings": 0.4895}
NOISE_MS = 0.4
GAP_LO = min(v for r in ON_GAPS.values() for v in r.values()) - NOISE_MS     # 36.763
GAP_HI = max(v for r in ON_GAPS.values() for v in r.values()) + NOISE_MS     # 38.189
BAND = (0.988, 1.012)
MIN_HELD_S = 40.0
CAP_W, CAP_H = 320, 180


def read_pfm(path: str) -> np.ndarray:
    with open(path, "rb") as f:
        kind = f.readline().strip()
        w, h = map(int, f.readline().split())
        scale = float(f.readline())
        data = np.frombuffer(f.read(), "<f4" if scale < 0 else ">f4")
    return np.flipud(data.reshape(h, w, 3 if kind == b"PF" else 1))


def frame_gaps(folder: str) -> dict[int, float]:
    out = {}
    for ln in open(os.path.join(folder, "proxy_frames.csv"), encoding="latin-1"):
        p = ln.split(",", 3)
        try:
            out[int(p[0])] = float(p[2])
        except (ValueError, IndexError):
            pass
    return out


def window_gap(gaps: dict[int, float], w: dict) -> tuple[float | None, int]:
    a, b = w["proxy"]
    v = [gaps[i] for i in range(a + 1, b + 1) if i in gaps]
    return (round(st.median(v), 3) if v else None), len(v)


def brightness(folder: str) -> float:
    return round(float(pfm_brightness.image_brightness_stats(read_pfm(os.path.join(folder, "gate", "y0_p0_final.pfm")))["gamma_mean"]), 5)


def capture_mean(folder: str) -> float | None:
    p = os.path.join(folder, "frames", "bench.gray")
    if not os.path.exists(p) or os.path.getsize(p) < CAP_W * CAP_H:
        return None
    return round(float(np.fromfile(p, np.uint8).mean()) / 255, 4)


# Corey's two monitors. With both off at the plug Windows keeps one more display active as primary (Nov 17 00:01:
# "ASUS MG278", 2560x1440 at 60 Hz, no PnP monitor present), so only these two must read detached; any other display
# is listed in the report.
MONITORS = ("HKC2731", "ACI27A8")


def detached(snap: list[dict] | None) -> bool:
    mine = [m for m in snap or [] if m.get("name") in MONITORS]
    return len(mine) == len(MONITORS) and all(not m.get("available") and not m.get("active") for m in mine)


def held_s(w: dict) -> float | None:
    """How long the window was held: drive.py's "heldS" (written only when it detaches monitors), else the window's own
    "clock" span [start, end] as HH:MM:SS.fff (a window with the monitors as found has only that). None if neither."""
    if w.get("heldS") is not None:
        return w["heldS"]
    try:
        a, b = (sum(float(x) * m for x, m in zip(c.split(":"), (3600, 60, 1))) for c in w["clock"])
    except (KeyError, TypeError, ValueError):
        return None
    return round((b - a) % 86400, 1)


def attached(snap: list[dict] | None) -> bool:
    mine = [m for m in snap or [] if m.get("name") in MONITORS]
    return len(mine) == len(MONITORS) and all(m.get("available") and m.get("active") for m in mine)


# --plug-on (coordinator, Nov 17 morning order (i)): the same con_custodial launch with Corey's monitors on again must
# read plug on and sit inside the same limits, confirming the plug-on state (and its frame time) is back.
MODES = {display_state.PLUG_OFF: ("both_off", detached, "detached", "MONITORS OFF", "MATCH", "DIFFER"),
         display_state.PLUG_ON: ("both_on", attached, "attached", "PLUG ON CONFIRM", "BACK", "NOT BACK")}


def others(snap: list[dict] | None) -> list[str]:
    return [m["name"] for m in snap or [] if m.get("name") not in MONITORS and (m.get("available") or m.get("active"))]


def check_on_runs(on_runs: list[str]) -> list[str]:
    bad = []
    for folder in on_runs:
        run = os.path.basename(os.path.dirname(folder))
        spot = json.load(open(os.path.join(folder, "spot.json")))
        gaps = frame_gaps(folder)
        for w in spot["windows"]:
            want = ON_GAPS[run].get(w.get("monitorStep"))
            if want is not None and abs(window_gap(gaps, w)[0] - want) > 0.0015:
                bad.append(f"{run} {w['monitorStep']} frame gap {window_gap(gaps, w)[0]} is not the fixed {want}")
        if abs(brightness(folder) - ON_BRIGHT[run]) > 0.000015:
            bad.append(f"{run} brightness {brightness(folder)} is not the fixed {ON_BRIGHT[run]}")
        ds = display_state.launch_state(folder)
        if ds["class"] != display_state.PLUG_ON:
            bad.append(f"{run} display state {ds['class']} ({ds['why']}), want plug on")
    return bad


def score(off: str, on_runs: list[str] = ON_RUNS, want: str = display_state.PLUG_OFF) -> tuple[int, list[str]]:
    step_want, state_ok, state_word, title, yes, no = MODES[want]
    bad = check_on_runs(on_runs)
    if bad:
        return 2, [f"BAD INPUT: {b}" for b in bad]
    lines = [f"monitors on (fixed before the launch): frame gap {GAP_LO + NOISE_MS:.3f} to {GAP_HI - NOISE_MS:.3f} ms, "
             f"pass {GAP_LO:.3f} to {GAP_HI:.3f}; brightness {min(ON_BRIGHT.values())} / {max(ON_BRIGHT.values())}, "
             f"pass {BAND[0]} to {BAND[1]} of their mean"]
    try:
        spot = json.load(open(os.path.join(off, "spot.json")))
        gaps = frame_gaps(off)
        bright = brightness(off)
    except (OSError, KeyError, ValueError) as e:
        return 1, lines + [f"NOT MEASURED: {e}"]
    wins = [w for w in spot["windows"] if w.get("monitorStep")]
    problems = []
    ds = display_state.launch_state(off)
    lines.append(f"display state before the launch: {ds['class']} ({ds['why']})")
    if ds["class"] != want:
        problems.append(f"display state {ds['class']} before the launch, want {want}")
    if not wins or any(w["monitorStep"] != step_want for w in wins):
        problems.append(f"steps {[w['monitorStep'] for w in wins]}, want only {step_want}")
    results = []
    for w in wins:
        if w.get("skipped"):
            problems.append(f"{w['name']} skipped: {w['skipped']}")
            continue
        held = held_s(w)
        if (held or 0) < MIN_HELD_S:
            problems.append(f"{w['name']} held {held} s, under {MIN_HELD_S}")
        mons = w.get("monitors", {})
        if not (state_ok(mons.get("start")) and state_ok(mons.get("end"))):
            problems.append(f"{w['name']} monitors not both {state_word} at start and end")
        g, n = window_gap(gaps, w)
        results.append((w["name"], g, n))
        extra = sorted(set(others(mons.get("start")) + others(mons.get("end"))))
        lines.append(f"{w['name']:12} frame gap {g} ms over {n} frames, held {held} s"
                     + (f"; other display(s) active: {', '.join(extra)}" if extra else ""))
    if problems:
        return 1, lines + [f"NOT MEASURED: {p}" for p in problems]
    mean_on = st.mean(ON_BRIGHT.values())
    ratio = bright / mean_on
    gap_ok = all(g is not None and GAP_LO <= g <= GAP_HI for _, g, _ in results)
    bright_ok = BAND[0] <= ratio <= BAND[1]
    lines.append(f"frame gap: {'PASS' if gap_ok else 'FAIL'} ({', '.join(str(g) for _, g, _ in results)} ms against "
                 f"{GAP_LO:.3f} to {GAP_HI:.3f})")
    lines.append(f"brightness: {'PASS' if bright_ok else 'FAIL'} ({bright}, ratio {ratio:.4f} to the monitors-on mean "
                 f"{mean_on:.5f}, against {BAND[0]} to {BAND[1]})")
    cap = capture_mean(off)
    cap_mean = st.mean(ON_GRAY.values())
    if cap is None:
        lines.append("capture: NOT MEASURED (no frames\\bench.gray)")
    else:
        cr = cap / cap_mean
        lines.append(f"capture: {'PASS' if BAND[0] <= cr <= BAND[1] else 'FAIL'} ({cap}, ratio {cr:.4f}; decides only "
                     f"whether gdigrab captures taken with the monitors off count)")
    verdict = gap_ok and bright_ok
    lines.append(f"{title}: {yes if verdict else no}")
    return (0 if verdict else 3), lines


def _fake(root: str, src: str, gap_scale: float = 1.0, bright_scale: float = 1.0, steps=("both_off", "both_off"),
          mon_avail: bool = False, held: float = 45.0, skip: bool = False, phantom: bool = False,
          machine: dict | None = display_state.OFF_002048, clock_span: float | None = None) -> str:
    """A monitors-off spot folder made from a monitors-on one: its both_on / both_on_again windows renamed to the given
    steps, frame gaps and the final picture scaled; machine_before.json is `machine` (002048's plug-off record)."""
    os.makedirs(os.path.join(root, "gate"), exist_ok=True)
    os.makedirs(os.path.join(root, "frames"), exist_ok=True)
    if machine is not None:
        json.dump(machine, open(os.path.join(root, "machine_before.json"), "w"))
    spot = json.load(open(os.path.join(src, "spot.json")))
    snap = [{"name": n, "available": mon_avail, "active": mon_avail} for n in ("HKC2731", "ACI27A8")]
    if phantom:
        snap.append({"name": "ASUS MG278", "available": True, "active": True, "primary": True})
    on = [w for w in spot["windows"] if w.get("monitorStep") in ("both_on", "both_on_again")]
    wins = []
    for w, step in zip(on, steps):
        w = dict(w, monitorStep=step, heldS=held, monitors={"start": snap, "end": snap})
        if clock_span is not None:   # a window with the monitors as found: no heldS, only its clock span
            del w["heldS"]
            end = 50.25 + clock_span   # starts 23:59:50.250, so a span over 9.75 s also crosses midnight
            w["clock"] = ["23:59:50.250", f"00:00:{end - 60:06.3f}" if end >= 60 else f"23:59:{end:06.3f}"]
        if skip:
            w = {"name": w["name"], "monitorStep": step, "skipped": "the both_off state was not reached within 45 s"}
        wins.append(w)
    json.dump({"windows": wins}, open(os.path.join(root, "spot.json"), "w"))
    with open(os.path.join(src, "proxy_frames.csv"), encoding="latin-1") as f, \
         open(os.path.join(root, "proxy_frames.csv"), "w", encoding="latin-1") as g:
        for ln in f:
            p = ln.split(",", 3)
            try:
                p[2] = f"{float(p[2]) * gap_scale:.3f}"
            except (ValueError, IndexError):
                pass
            g.write(",".join(p))
    img = read_pfm(os.path.join(src, "gate", "y0_p0_final.pfm")) * bright_scale
    h, w, c = img.shape
    with open(os.path.join(root, "gate", "y0_p0_final.pfm"), "wb") as f:
        f.write(b"PF\n" if c == 3 else b"Pf\n")
        f.write(f"{w} {h}\n-1.0\n".encode())
        f.write(np.ascontiguousarray(np.flipud(img)).astype("<f4").tobytes())
    gray = np.fromfile(os.path.join(src, "frames", "bench.gray"), np.uint8)
    gray.tofile(os.path.join(root, "frames", "bench.gray"))
    return root


def self_test() -> int:
    import shutil
    import tempfile
    base = tempfile.mkdtemp(prefix="monoff-", dir=os.environ.get("TEMP"))
    src = ON_RUNS[1]
    cases = [  # name, fake args, want exit, a line that must appear
        ("same as monitors on", {}, 0, "MONITORS OFF: MATCH"),
        ("frame gap 6 % faster (the morning's 35 ms)", {"gap_scale": 0.94}, 3, "frame gap: FAIL"),
        ("frame gap 2 % slower", {"gap_scale": 1.02}, 3, "frame gap: FAIL"),
        ("picture 2 % darker", {"bright_scale": 0.97}, 3, "brightness: FAIL"),
        ("picture 2 % brighter", {"bright_scale": 1.03}, 3, "brightness: FAIL"),
        ("phantom primary display active, both monitors off", {"phantom": True}, 0, "other display(s) active: ASUS MG278"),
        ("monitors still attached", {"mon_avail": True}, 1, "monitors not both detached"),
        ("monitors still attached beside the phantom", {"mon_avail": True, "phantom": True}, 1, "monitors not both detached"),
        ("both_off held only 20 s", {"held": 20.0}, 1, "held 20.0 s"),
        ("both_off skipped", {"skip": True}, 1, "skipped"),
        ("a both_on step in the launch", {"steps": ("both_on", "both_off")}, 1, "want only both_off"),
        ("machine_before reads plug on", {"machine": display_state.ON_212703}, 1, "want plug off"),
        ("no machine_before", {"machine": None}, 1, "display state unknown before the launch"),
        ]
    fails = 0
    try:
        for i, (name, kw, want, line) in enumerate(cases):
            code, lines = score(_fake(os.path.join(base, str(i)), src, **kw))
            ok = code == want and any(line in ln for ln in lines)
            fails += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {name}: exit {code}" + ("" if ok else f" (want {want}); " + " | ".join(lines)))
        on_cases = [  # --plug-on: the morning confirm run (both_on windows, monitors attached, machine reads plug on)
            ("plug on, same as the monitors-on runs", {}, 0, "PLUG ON CONFIRM: BACK"),
            ("plug on, frame gap at the plug-off 33.6 ms", {"gap_scale": 0.892}, 3, "PLUG ON CONFIRM: NOT BACK"),
            ("plug on asked, machine_before reads plug off", {"machine": display_state.OFF_002048}, 1, "want plug on"),
            ("plug on asked, monitors read detached", {"mon_avail": False}, 1, "monitors not both attached"),
            ("plug on asked, both_off steps", {"steps": ("both_off", "both_off")}, 1, "want only both_on"),
            ("plug on, no heldS, clock spans 45 s", {"clock_span": 45.0}, 0, "held 45.0 s"),
            ("plug on, no heldS, clock spans 30 s", {"clock_span": 30.0}, 1, "held 30.0 s, under"),
            ]
        for i, (name, kw, want, line) in enumerate(on_cases):
            args = dict({"steps": ("both_on", "both_on"), "mon_avail": True, "machine": display_state.ON_212703}, **kw)
            code, lines = score(_fake(os.path.join(base, f"on{i}"), src, **args), want=display_state.PLUG_ON)
            ok = code == want and any(line in ln for ln in lines)
            fails += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {name}: exit {code}" + ("" if ok else f" (want {want}); " + " | ".join(lines)))
        # The monitors-on folders are checked against the fixed numbers: a changed one is bad input, not a verdict.
        moved = os.path.join(base, "moved", "20261116-225845-settings", "01_monitors")
        _fake(moved, src, steps=("both_on", "both_on_again"), mon_avail=True, gap_scale=0.95)
        code, lines = score(_fake(os.path.join(base, "x"), src), on_runs=[ON_RUNS[0], moved])
        ok = code == 2
        fails += not ok
        print(f"{'ok  ' if ok else 'FAIL'} a monitors-on folder no longer gives the fixed numbers: exit {code}")
    finally:
        shutil.rmtree(base, ignore_errors=True)
    print(f"self-test: {'PASS' if not fails else f'{fails} FAILED'}")
    return 1 if fails else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    a = sys.argv[1:]
    plug_on = a[:1] == ["--plug-on"]
    if len(a) != 1 + plug_on:
        print(__doc__)
        sys.exit(2)
    code, out = score(a[-1], want=display_state.PLUG_ON if plug_on else display_state.PLUG_OFF)
    print("\n".join(out))
    sys.exit(code)
