"""Frame gaps over 500 ms per spot, the step each falls in, and the windows taken across one.

  stall_windows.py <run folder> [--md <result.md>]   per spot: worst gap, gaps over 500 ms and their step; writes
                                                     <spot>\\stalls.json, prints Markdown, appends it to --md
  stall_windows.py --self-test

Why (coordinator, Nov 17): health run 20261117-125334 had a 1602.6 ms Present-to-Present gap at frame 15933 inside
the con_custodial y0_p30 still, and the gate only counted it among 25 gaps over 50 ms; nothing said that this still
was taken across a stall. A window (still, pan, bench, switch, lag) that contains a gap over 500 ms is now named in
result.md. Gaps between windows (dumps, teleports, loads) are listed with the step they fall in but mark nothing.

Source: <spot>\\proxy_frames.csv (column 0 frame number, column 2 Present-to-Present gap in ms) and the windows'
"proxy" frame ranges in <spot>\\spot.json. A gap at frame f is the time from frame f-1 to frame f, so it lies inside
a window [a, b] when a < f <= b. A spot without either file reads NOT MEASURED, never "no stalls".
"""
import csv
import json
import os
import sys

LIMIT_MS = 500.0


def read_gaps(path: str, limit: float = LIMIT_MS):
    """(frames read, worst (gap, frame), [(frame, gap) over limit])"""
    n, worst, over = 0, (0.0, None), []
    with open(path, newline="") as f:
        for row in csv.reader(f):
            try:
                fr, gap = int(row[0]), float(row[2])
            except (ValueError, IndexError):
                continue
            n += 1
            if gap > worst[0]:
                worst = (gap, fr)
            if gap > limit:
                over.append((fr, gap))
    return n, worst, over


def place(frame: int, windows: list) -> tuple[str, str | None]:
    """(step description, window name when the gap lies inside a window)"""
    for name, kind, (a, b) in windows:
        if a < frame <= b:
            return f"inside {name} ({kind}, frames {a}..{b})", name
    before = [w for w in windows if w[2][1] < frame]
    after = [w for w in windows if w[2][0] >= frame]
    if not windows:
        return "no windows recorded", None
    if not before:
        return f"before the first window {after[0][0]} (load, teleport, calibration)", None
    if not after:
        return f"after the last window {before[-1][0]} (end of spot)", None
    return f"between {before[-1][0]} and {after[0][0]} (the step between them, such as a dump or a settings change)", None


def spot_report(spot_dir: str, limit: float = LIMIT_MS) -> dict:
    name = os.path.basename(os.path.normpath(spot_dir))
    frames, sj = os.path.join(spot_dir, "proxy_frames.csv"), os.path.join(spot_dir, "spot.json")
    if not os.path.exists(frames) or not os.path.exists(sj):
        return {"spot": name, "verdict": "NOT MEASURED", "why": "proxy_frames.csv or spot.json missing"}
    with open(sj, encoding="utf-8") as f:
        spot = json.load(f)
    windows = sorted(((w.get("name"), w.get("kind"), tuple(w["proxy"])) for w in spot.get("windows", [])
                      if isinstance(w.get("proxy"), list) and len(w["proxy"]) == 2), key=lambda w: w[2][0])
    n, worst, over = read_gaps(frames, limit)
    if not n:
        return {"spot": name, "verdict": "NOT MEASURED", "why": "proxy_frames.csv has no frame rows"}
    gaps, marked = [], []
    for fr, gap in over:
        step, win = place(fr, windows)
        gaps.append({"frame": fr, "gapMs": round(gap, 1), "step": step, "window": win})
        if win and win not in marked:
            marked.append(win)
    return {"spot": name, "verdict": "STALL IN WINDOW" if marked else "NO STALL IN WINDOW", "limitMs": limit,
            "frames": n, "worstGapMs": round(worst[0], 1), "worstFrame": worst[1], "gapsOverLimit": len(gaps),
            "gaps": gaps, "windowsAcrossStall": marked}


def markdown(reports: list) -> str:
    out = [f"### Frame gaps over {LIMIT_MS:.0f} ms (stall_windows.py)", ""]
    for r in reports:
        if r["verdict"] == "NOT MEASURED":
            out.append(f"- {r['spot']}: NOT MEASURED ({r['why']})")
            continue
        head = (f"- {r['spot']}: worst gap {r['worstGapMs']} ms at frame {r['worstFrame']}; "
                f"{r['gapsOverLimit']} over {LIMIT_MS:.0f} ms")
        if r["windowsAcrossStall"]:
            head += f"; **taken across a stall: {', '.join(r['windowsAcrossStall'])}**"
        out.append(head)
        out += [f"  - frame {g['frame']}: {g['gapMs']} ms, {g['step']}" for g in r["gaps"]]
    return "\n".join(out) + "\n"


def run_reports(run_dir: str) -> list:
    spots = sorted(d for d in os.listdir(run_dir)
                   if os.path.exists(os.path.join(run_dir, d, "spot.json")))
    reports = []
    for d in spots:
        r = spot_report(os.path.join(run_dir, d))
        with open(os.path.join(run_dir, d, "stalls.json"), "w", encoding="utf-8") as f:
            json.dump(r, f, indent=1)
        reports.append(r)
    return reports


def self_test() -> int:
    import tempfile
    with tempfile.TemporaryDirectory(prefix="stalls-") as root:
        return _self_test(root)


def _self_test(root: str) -> int:
    import tempfile
    ok = 0
    cases = []

    def make(rows, windows, frames_file=True):
        d = tempfile.mkdtemp(dir=root)
        if frames_file:
            with open(os.path.join(d, "proxy_frames.csv"), "w", newline="") as f:
                csv.writer(f).writerows([[fr, fr * 30.0, gap, 1.0] for fr, gap in rows])
        with open(os.path.join(d, "spot.json"), "w") as f:
            json.dump({"windows": [{"name": n, "kind": k, "proxy": p} for n, k, p in windows]}, f)
        return d

    base = [(f, 30.0) for f in range(100, 400)]
    wins = [("y0_p0", "still", [150, 200]), ("y0_p30", "still", [250, 300])]

    def with_gap(frame, gap):
        return [(f, gap if f == frame else g) for f, g in base]

    # 1. A 1602 ms gap inside the y0_p30 still marks that still (the 125334 case).
    r = spot_report(make(with_gap(260, 1602.6), wins))
    cases.append(("gap inside a still marks it", r["windowsAcrossStall"] == ["y0_p30"] and r["verdict"] == "STALL IN WINDOW"))
    # 2. An 815 ms gap between two windows (a dump) is listed with its step and marks nothing.
    r = spot_report(make(with_gap(220, 815.0), wins))
    cases.append(("gap between windows marks nothing", r["windowsAcrossStall"] == [] and r["gapsOverLimit"] == 1
                  and "between y0_p0 and y0_p30" in r["gaps"][0]["step"]))
    # 3. A gap ending on a window's first frame began before it: not inside.
    r = spot_report(make(with_gap(250, 900.0), wins))
    cases.append(("gap ending on a window's first frame is outside it", r["windowsAcrossStall"] == []))
    # 4. 499 ms inside a still is under the limit; 501 ms is over (both sides of the boundary).
    r1, r2 = spot_report(make(with_gap(170, 499.0), wins)), spot_report(make(with_gap(170, 501.0), wins))
    cases.append(("499 ms not marked, 501 ms marked", r1["windowsAcrossStall"] == [] and r2["windowsAcrossStall"] == ["y0_p0"]))
    # 5. No proxy_frames.csv reads NOT MEASURED, not "no stall".
    r = spot_report(make(base, wins, frames_file=False))
    cases.append(("missing frames file reads NOT MEASURED", r["verdict"] == "NOT MEASURED"))
    # 6. The worst gap is reported even when it is under the limit.
    r = spot_report(make(with_gap(330, 123.3), wins))
    cases.append(("worst gap reported under the limit", r["worstGapMs"] == 123.3 and r["worstFrame"] == 330 and r["gapsOverLimit"] == 0))
    for name, good in cases:
        print(f"[{'PASS' if good else 'FAIL'}] {name}")
        ok += good
    print(f"stall_windows self-test: {ok} of {len(cases)}")
    return 0 if ok == len(cases) else 1


def main(argv: list) -> int:
    if argv[:1] == ["--self-test"]:
        return self_test()
    if not argv:
        print(__doc__)
        return 2
    reports = run_reports(argv[0])
    md = markdown(reports)
    print(md)
    if "--md" in argv:
        path = argv[argv.index("--md") + 1]
        if os.path.exists(path):
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n" + md)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
