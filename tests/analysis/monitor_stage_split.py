"""Where the monitors-off frame time goes: every recorded stage, plug-on windows against plug-off windows (worker C).

Measurement only. Same build (Remix ee695ff, proxy FC8ECC61641E), con_custodial y0_p0 held pose, 45 s live windows:
  plug on   20261116-222500-settings\\01_monitors   both_on, both_on_again
            20261116-225845-settings\\01_monitors   both_on, both_on_again
  plug off  20261117-002048-settings\\01_monitors_off  live_step1, live_step2 (both both_off)

Stages, each as a per-window median over the window's own frames or samples:
  proxy_frames.csv  gap        Present entry to Present entry (column 3; the coordinator's frame gap)
                    inPresent  time inside the proxy's Present call (column 4)
                    outside    gap minus the previous frame's inPresent: time from leaving Present to the next Present
                    draws, publishes (columns 5, 6)
  proxy_gate.csv    sceneDraws, skinnedDraws, handoffs
  telemetry.jsonl   frameTimeMs, gpuTimeMs, gpuIdleMs (= frameTimeMs - gpuTimeMs), remixQpcGap (qpcMs step between
                    consecutive Remix frames), telemetryCpuMs, instances, every passMs step's GPU and CPU ms, and
                    gpu outside timed passes (= gpuTimeMs - sum of the top-level passMs GPU times)
  gpu_run.csv       graphics and memory clock, power, temperature (nvidia-smi samples inside the window's clock range)
  memory.csv        hardFaultsPerSec, freeRamMB (samples inside the window's clock range)
  proxy_hitches.txt hitches starting inside the window, per minute
  gpu_engines.csv   GPU use per program group, 3d engine and all engines, % (runtime\\gpu_engines.py; launches from
                    Nov 17 on; absent, so left out of the compare, when a run did not record it)

Rule. floor = max - min of the 4 plug-on window medians (within- and between-run spread), at least MIN_FLOOR for ms
stages. A stage CARRIES the difference when |off - on| > floor (off, on = mean of the window medians) and each off window
median lies outside the plug-on range. Fake case (--fake-on-vs-on, also run by default): the windows of run 222500 as
reference against those of run 225845 with floor = the reference's own spread; no stage may CARRY. Second fake: the
same with 2 ms added to every 225845 frame gap; gap must CARRY.

Run: python -I monitor_stage_split.py [--json out.json] [--on <spot folder>]... [--off <spot folder>]...
     |   python -I monitor_stage_split.py --self-test
--on adds a plug-on run (its both_on and both_on_again windows) to the two above, --off a plug-off run (its both_off
windows) to the one above; every window of every run counts. The on-vs-on fakes stay 222500 against 225845.
Exit 0 = report written and the on-vs-on fake shows no stage outside its floor; 1 = the fake failed; 2 = bad input.
"""
import json
import os
import re
import statistics as st
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "runtime"))
import gpu_engines  # noqa: E402  program gpu% <game|bridge|dwm|nvidia overlay|other> <3d|all>, absent when not recorded

GATE = r"D:\BlacklistOverhaul\experiments\remix-gate"
ON = [os.path.join(GATE, "20261116-222500-settings", "01_monitors"),
      os.path.join(GATE, "20261116-225845-settings", "01_monitors")]
OFF = os.path.join(GATE, "20261117-002048-settings", "01_monitors_off")
ON_STEPS = ("both_on", "both_on_again")
OFF_STEPS = ("both_off",)
MIN_FLOOR_MS = 0.05


def windows(folder, steps):
    s = json.load(open(os.path.join(folder, "spot.json"), encoding="utf-8"))
    out = [w for w in s["windows"] if w.get("monitorStep") in steps and w.get("proxy") and w.get("telemetry")]
    if not out:
        raise ValueError(f"{folder}: no window for {steps}")
    return out


def med(v):
    return round(st.median(v), 4) if v else None


def proxy_stages(folder, w):
    rows = {}
    for ln in open(os.path.join(folder, "proxy_frames.csv"), encoding="latin-1"):
        p = ln.split(",", 6)
        try:
            rows[int(p[0])] = (float(p[1]), float(p[2]), float(p[3]), int(p[4]), int(p[5]))
        except (ValueError, IndexError):
            pass
    a, b = w["proxy"]
    fr = [i for i in range(a + 1, b + 1) if i in rows and i - 1 in rows]
    out = {"proxy gap": [rows[i][1] for i in fr],
           "proxy inPresent": [rows[i][2] for i in fr],
           "proxy outside Present": [rows[i][1] - rows[i - 1][2] for i in fr],
           "proxy draws": [rows[i][3] for i in fr],
           "proxy publishes": [rows[i][4] for i in fr]}
    gate = {}
    for ln in open(os.path.join(folder, "proxy_gate.csv"), encoding="latin-1"):
        p = ln.strip().split(",")
        try:
            gate[int(p[0])] = [int(x) for x in p[1:4]]
        except (ValueError, IndexError):
            pass
    g = [gate[i] for i in fr if i in gate]
    out["gate sceneDraws"] = [x[0] for x in g]
    out["gate skinnedDraws"] = [x[1] for x in g]
    out["gate handoffs"] = [x[2] for x in g]
    qa, qb = (rows[fr[0]][0], rows[fr[-1]][0]) if fr else (0, 0)
    return out, (qa, qb)


def telemetry_stages(folder, w):
    a, b = w["telemetry"]
    pat = re.compile(r'\{"frame":(\d+),')
    lines = []
    with open(os.path.join(folder, "telemetry.jsonl"), encoding="utf-8") as f:
        for ln in f:
            m = pat.match(ln)
            if not m:
                continue
            n = int(m.group(1))
            if n > b:
                break
            if n >= a:
                lines.append(json.loads(ln))
    out = {}
    prev = None
    for t in lines:
        if not t.get("rayTraced", True):
            continue
        add = out.setdefault
        add("remix frameTimeMs", []).append(t["frameTimeMs"])
        add("remix gpuTimeMs", []).append(t["gpuTimeMs"])
        add("remix gpuIdleMs", []).append(t["frameTimeMs"] - t["gpuTimeMs"])
        add("remix telemetryCpuMs", []).append(t.get("telemetryCpuMs", 0.0))
        add("remix instances", []).append(t.get("instances", 0))
        if prev is not None and t["frame"] == prev["frame"] + 1:
            add("remix qpc gap", []).append(t["qpcMs"] - prev["qpcMs"])
        passes = t.get("passMs", {})
        add("remix gpu outside timed passes", []).append(
            t["gpuTimeMs"] - sum(v[0] for k, v in passes.items() if "/" not in k))
        for k, v in passes.items():
            add(f"gpu {k}", []).append(v[0])
            add(f"cpu {k}", []).append(v[1])
        prev = t
    return out


def clock_rows(folder, name, w, cols):
    path = os.path.join(folder, name)
    if not os.path.exists(path) or not w.get("clock"):
        return {}
    t0, t1 = (datetime.strptime(c, "%H:%M:%S.%f").time() for c in w["clock"])
    lines = open(path, encoding="utf-8").read().splitlines()
    head = [h.strip() for h in lines[0].split(",")]
    out = {label: [] for label in cols.values()}
    for ln in lines[1:]:
        p = [x.strip() for x in ln.split(",")]
        ts = p[0].split(" ")[-1]
        try:
            t = datetime.strptime(ts, "%H:%M:%S.%f").time()
        except ValueError:
            continue
        if not (t0 <= t <= t1):
            continue
        for col, label in cols.items():
            try:
                out[label].append(float(p[head.index(col)].split(" ")[0]))
            except (ValueError, IndexError):
                pass
    return out


def hitches_per_min(folder, qa, qb):
    path = os.path.join(folder, "proxy_hitches.txt")
    n = 0
    for ln in open(path, encoding="latin-1"):
        m = re.match(r"HITCH frame \d+ start ([\d.]+) ms", ln)
        if m and qa <= float(m.group(1)) <= qb:
            n += 1
    return [n / max((qb - qa) / 60000.0, 1e-9)]


GPU_COLS = {"clocks.current.graphics [MHz]": "nvsmi graphics MHz", "clocks.current.memory [MHz]": "nvsmi memory MHz",
            "power.draw [W]": "nvsmi power W", "temperature.gpu": "nvsmi temperature C"}
MEM_COLS = {"hardFaultsPerSec": "memory hardFaultsPerSec", "freeRamMB": "memory freeRamMB"}


def window_medians(folder, w):
    stages, (qa, qb) = proxy_stages(folder, w)
    stages.update(telemetry_stages(folder, w))
    stages.update(clock_rows(folder, "gpu_run.csv", w, GPU_COLS))
    stages.update(clock_rows(folder, "memory.csv", w, MEM_COLS))
    stages.update(gpu_engines.window_stages(os.path.join(folder, "gpu_engines.csv"), w.get("clock")))
    stages["proxy hitches per min"] = hitches_per_min(folder, qa, qb)
    return {k: (med(v), len(v)) for k, v in stages.items()}


def is_ms(stage):
    return stage.startswith(("proxy gap", "proxy inPresent", "proxy outside", "remix", "gpu ", "cpu ")) and \
        "instances" not in stage


def judge(ref, test, floor_from=None):
    """ref, test: lists of {stage: median}. floor_from: windows the floor comes from (default ref).
    Returns {stage: dict(on, off, diff, floor, carries)} for stages present in every window."""
    floor_from = floor_from or ref
    out = {}
    stages = set.intersection(*(set(k for k, v in w.items() if v is not None) for w in ref + test + floor_from))
    for s in sorted(stages):
        r, t, fl = [w[s] for w in ref], [w[s] for w in test], [w[s] for w in floor_from]
        floor = max(fl) - min(fl)
        if is_ms(s):
            floor = max(floor, MIN_FLOOR_MS)
        on, off = st.mean(r), st.mean(t)
        outside = all(x < min(r) or x > max(r) for x in t)
        out[s] = {"on": round(on, 3), "off": round(off, 3), "diff": round(off - on, 3), "floor": round(floor, 3),
                  "onWindows": r, "offWindows": t, "carries": abs(off - on) > floor and outside}
    return out


def table(res, keys=None, title="", fake=None):
    lines = [title, f"{'stage':58} {'on':>9} {'off':>9} {'off-on':>8} {'floor':>7} {'on-on':>7}  verdict"]
    for s in keys or res:
        if s not in res:
            continue
        r = res[s]
        f = (fake or {}).get(s)
        note = " (on runs differ beyond within-run spread)" if f and f["carries"] else ""
        lines.append(f"{s[:58]:58} {r['on']:9.3f} {r['off']:9.3f} {r['diff']:8.3f} {r['floor']:7.3f} "
                     f"{(f['diff'] if f else float('nan')):7.3f}  {'CARRIES' if r['carries'] else 'inside floor'}{note}")
    return "\n".join(lines)


def last_line(path, pattern):
    hit = None
    for ln in open(path, encoding="latin-1"):
        if pattern in ln:
            hit = ln.strip()
    return hit


def recorded_config(folder):
    """Recorded settings, not stage times: the last swapchain present mode Remix logged and the refresh rates
    monitor_layout.json lists."""
    lay = open(os.path.join(folder, "monitor_layout.json"), encoding="utf-8").read()
    hz = sorted(set(re.findall(r'"refreshHz":\s*([0-9.]+)', lay)))
    return {"presentMode": last_line(os.path.join(folder, "remix-dxvk.log"), "Present mode"), "refreshHz": hz}


def run_args(args):
    """(plug-on folders, plug-off folders, json path) from the command line: ON and OFF above, then each extra
    --on <spot folder> (both_on, both_on_again windows) and --off <spot folder> (both_off windows), in order."""
    on, off, json_out = list(ON), [OFF], None
    it = iter(args)
    for a in it:
        value = next(it, None)
        if a not in ("--on", "--off", "--json") or value is None:
            raise ValueError(f"unknown or incomplete argument {a!r}")
        if a == "--json":
            json_out = value
        else:
            (on if a == "--on" else off).append(value)
    return on, off, json_out


def run_windows(folders, steps):
    """Window medians per run folder: [[medians of each window of steps] per folder]."""
    return [[window_medians(f, w) for w in windows(f, steps)] for f in folders]


def pool(runs):
    """Every window of every run, as {stage: median}; extra runs count like the fixed ones."""
    return [{k: v[0] for k, v in w.items()} for ws in runs for w in ws]


def main_report(json_out=None, on_folders=ON, off_folders=(OFF,)):
    on_runs = run_windows(on_folders, ON_STEPS)
    off_runs = run_windows(off_folders, OFF_STEPS)
    off = [w for ws in off_runs for w in ws]
    flat = lambda ws: [{k: v[0] for k, v in w.items()} for w in ws]  # noqa: E731
    on_all = pool(on_runs)
    real = judge(on_all, pool(off_runs))
    fake = judge(flat(on_runs[0]), flat(on_runs[1]))
    shifted = [dict(w, **{"proxy gap": w["proxy gap"] + 2.0}) for w in flat(on_runs[1])]
    fake2 = judge(flat(on_runs[0]), shifted)
    main_keys = [k for k in real if not k.startswith(("gpu ", "cpu "))] + \
        [k for k in real if k.startswith(("gpu ", "cpu ")) and k.count("/") <= 1]
    print(table(real, main_keys, f"PLUG ON ({len(on_all)} windows) vs PLUG OFF ({len(off)} windows); floor = spread "
                                 f"of the {len(on_all)} on windows; on-on = 225845 minus 222500", fake))
    for f in list(on_folders) + list(off_folders):
        print(f"recorded config {os.path.basename(os.path.dirname(f))}: {recorded_config(f)}")
    carried = [k for k, r in real.items() if r["carries"]]
    print(f"\nstages that CARRY ({len(carried)} of {len(real)}):")
    for k in carried:
        print(f"  {k}: on {real[k]['on']} off {real[k]['off']} diff {real[k]['diff']} floor {real[k]['floor']}")
    fake_out = [k for k, r in fake.items() if r["carries"]]
    print(f"\nFAKE on-vs-on (222500 ref, 225845 test, floor = 222500's own spread): "
          f"{len(fake_out)} of {len(fake)} stages outside" + (f": {fake_out}" if fake_out else ""))
    print(f"FAKE +2 ms gap on 225845: proxy gap {'CARRIES (ok)' if fake2['proxy gap']['carries'] else 'MISSED'}")
    counts = {"on": [[w["proxy gap"][1] for w in r] for r in on_runs], "off": [w["proxy gap"][1] for w in off]}
    print(f"frames per window (proxy): {counts}")
    if json_out:
        json.dump({"real": real, "fakeOnVsOn": fake, "frames": counts}, open(json_out, "w"), indent=1)
    ok = not fake_out and fake2["proxy gap"]["carries"]
    return 0 if ok else 1


def repeat_compare(ref_runs, test_runs, plug_off=None):
    """Same display state, two launches: ref_runs (last night's plug-on runs) against test_runs (a repeat). Floor =
    spread of the ref windows, same rule as judge(). Stages the test run has but the ref runs lack (program gpu%) are
    returned separately as test-only medians. plug_off: the plug-on vs plug-off judge() result, to mark the stages
    that carried that difference too."""
    ref, test = pool(ref_runs), pool(test_runs)
    res = judge(ref, test)
    only = sorted(set.intersection(*(set(k for k, v in w.items() if v is not None) for w in test)) - set(res))
    for k, r in res.items():
        r["carriedPlugOff"] = bool(plug_off and plug_off.get(k, {}).get("carries"))
    return res, {k: [w[k] for w in test] for k in only}


def repeat_report(folder, json_out=None):
    ref_runs = run_windows(ON, ON_STEPS)
    plug_off = judge(pool(ref_runs), pool(run_windows([OFF], OFF_STEPS)))
    res, only = repeat_compare(ref_runs, run_windows([folder], ON_STEPS), plug_off)
    keys = [k for k in res if not k.startswith(("gpu ", "cpu "))] + \
        [k for k in res if k.startswith(("gpu ", "cpu ")) and k.count("/") <= 1]
    print(f"{'stage':58} {'night':>9} {'repeat':>9} {'diff':>8} {'floor':>7}  verdict          plug-off")
    for k in keys:
        r = res[k]
        print(f"{k[:58]:58} {r['on']:9.3f} {r['off']:9.3f} {r['diff']:8.3f} {r['floor']:7.3f}  "
              f"{'CARRIES     ' if r['carries'] else 'inside floor'}     {'carried' if r['carriedPlugOff'] else '-'}")
    carried = [k for k, r in res.items() if r["carries"]]
    print(f"\nstages that CARRY the repeat difference ({len(carried)} of {len(res)}):")
    for k in carried:
        r = res[k]
        print(f"  {k}: night {r['on']} repeat {r['off']} diff {r['diff']} floor {r['floor']} windows {r['offWindows']}"
              f"{' (carried plug-off too)' if r['carriedPlugOff'] else ''}")
    po = [k for k, r in plug_off.items() if r["carries"]]
    print(f"\nplug-off carriers that do NOT carry the repeat: {[k for k in po if k in res and not res[k]['carries']]}")
    print(f"\nrepeat-only stages (not recorded last night), repeat window medians:")
    for k, v in only.items():
        print(f"  {k}: {v}")
    for f in ON + [folder]:
        print(f"recorded config {os.path.basename(os.path.dirname(f))}: {recorded_config(f)}")
    # Fake: 225845's own windows with 1.1 ms added to the gap must CARRY; unshifted they must not.
    base = pool(run_windows([ON[1]], ON_STEPS))
    fake_hit = judge(pool(ref_runs), [dict(w, **{"proxy gap": w["proxy gap"] + 1.1}) for w in base])["proxy gap"]
    fake_none = judge(pool(ref_runs), base)["proxy gap"]
    ok = fake_hit["carries"] and not fake_none["carries"]
    print(f"\nFAKE 225845 +1.1 ms gap vs night: {'CARRIES (ok)' if fake_hit['carries'] else 'MISSED'}; "
          f"unshifted: {'inside floor (ok)' if not fake_none['carries'] else 'CARRIES (wrong)'}")
    if json_out:
        json.dump({"repeat": res, "repeatOnly": only}, open(json_out, "w"), indent=1)
    return 0 if ok else 1


def self_test():
    cases = []

    def case(name, got, want):
        cases.append(got == want)
        print(f"{'ok  ' if got == want else 'FAIL'} {name}: {got}")

    ref = [{"proxy gap": 37.7, "nvsmi power W": 200.0}, {"proxy gap": 37.2, "nvsmi power W": 205.0}]
    case("off well outside carries", judge(ref, [{"proxy gap": 33.6, "nvsmi power W": 202.0},
                                                {"proxy gap": 32.8, "nvsmi power W": 201.0}])["proxy gap"]["carries"], True)
    case("off inside the on range does not carry",
         judge(ref, [{"proxy gap": 37.3, "nvsmi power W": 0}, {"proxy gap": 37.5, "nvsmi power W": 0}])["proxy gap"]["carries"], False)
    case("diff below floor does not carry",
         judge(ref, [{"proxy gap": 37.9, "nvsmi power W": 0}, {"proxy gap": 38.0, "nvsmi power W": 0}])["proxy gap"]["carries"], False)
    case("one off window inside the range does not carry",
         judge(ref, [{"proxy gap": 30.0, "nvsmi power W": 0}, {"proxy gap": 37.5, "nvsmi power W": 0}])["proxy gap"]["carries"], False)
    case("ms floor has a minimum",
         judge([{"proxy gap": 1.0}, {"proxy gap": 1.0}], [{"proxy gap": 1.04}, {"proxy gap": 1.04}])["proxy gap"]["carries"], False)
    case("count stage has no minimum floor",
         judge([{"proxy draws": 10}, {"proxy draws": 10}], [{"proxy draws": 11}, {"proxy draws": 11}])["proxy draws"]["carries"], True)
    case("power not carried in the first case", judge(ref, [{"proxy gap": 33.6, "nvsmi power W": 202.0},
                                                            {"proxy gap": 32.8, "nvsmi power W": 201.0}])["nvsmi power W"]["carries"], False)
    # Fake run folder, values chosen so each likely parsing mistake gives a different median.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        def put(name, text):
            open(os.path.join(d, name), "w", encoding="utf-8").write(text)
        put("spot.json", json.dumps({"windows": [{"monitorStep": "both_on", "proxy": [10, 13], "telemetry": [21, 22],
                                                  "clock": ["01:00:00.000", "01:00:02.500"]}]}))
        # dwm 10 % in two samples and 0 % (no row) in one inside the clock range, 90 % in one sample after it
        put("gpu_engines.csv", "time,pid,process,engtype,percent,instances\n" + "".join(
            (f"{t},636,dwm,3d,{v},\n" if v else "") + f"{t},-1,_sample,all,0,500\n"
            for t, v in (("01:00:00.000", 10), ("01:00:01.000", 0), ("01:00:02.000", 10), ("01:00:03.000", 90))))
        # frame, entry, gap, inPresent, draws, publishes; window frames are 11..13 (frame 10 only gives inPresent)
        put("proxy_frames.csv", "frame,qpc,gap,inPresent,draws,publishes\n9,970,30,20,100,16\n"
                                "10,1000,30,25,100,16\n11,1040,40,35,100,16\n"
                                "12,1082,42,30,100,16\n13,1126,44,34,100,16\n14,1206,80,70,100,16\n")
        put("proxy_gate.csv", "frame,sceneDraws,skinned,handoffs,goggles,rt\n11,5,1,1,0,1\n12,6,1,1,0,1\n13,7,1,1,0,1\n")
        tl = lambda n, q, g: json.dumps({"frame": n, "qpcMs": q, "frameTimeMs": 33.0, "gpuTimeMs": g,  # noqa: E731
                                         "passMs": {"InjectRTX": [20.0, 1.0, 1], "InjectRTX/RR": [10.0, 0.1, 1],
                                                    "Queue Present": [0.5, 0.0, 1]}}, separators=(",", ":")) + "\n"
        put("telemetry.jsonl", tl(20, 0, 100.0) + tl(21, 33, 30.0) + tl(22, 66, 32.0) + tl(23, 99, 100.0))
        put("proxy_hitches.txt", "HITCH frame 12 start 1082.0 ms dt 60\nHITCH frame 14 start 1206.0 ms dt 80\n")
        w = windows(d, ON_STEPS)[0]
        m = {k: v[0] for k, v in window_medians(d, w).items()}
        case("window takes proxy frames a+1..b (gap median 42)", m["proxy gap"], 42)
        case("inPresent median", m["proxy inPresent"], 34)
        case("outside Present uses the previous frame's Present time (median 14)", m["proxy outside Present"], 14)
        case("telemetry window inclusive a..b (gpuTimeMs 31)", m["remix gpuTimeMs"], 31)
        case("GPU outside timed passes sums only top-level passes (10.5)", m["remix gpu outside timed passes"], 10.5)
        case("hitches counted inside the window's QPC range only (1 per 86 ms)",
             round(m["proxy hitches per min"], 1), round(60000 / 86, 1))
        case("dwm GPU use inside the window's clock range (median of 10, 0, 10)", m["program gpu% dwm 3d"], 10)
        # Extra run folders (--on, --off): an extra plug-on folder is counted in the plug-on windows.
        on_f, off_f, js = run_args(["--on", d, "--json", "x.json", "--off", "o1", "--on", "e2"])
        case("--on folders follow the fixed plug-on runs", on_f, ON + [d, "e2"])
        case("--off folders follow the fixed plug-off run, --json still read", (off_f, js), ([OFF, "o1"], "x.json"))
        case("no arguments: the fixed runs only", run_args([]), (ON, [OFF], None))
        case("three plug-on runs give three plug-on windows", len(pool(run_windows([d, d, d], ON_STEPS))), 3)
        case("an extra run's window is in the pool (proxy gap 42)",
             [w["proxy gap"] for w in pool(run_windows([d], ON_STEPS))], [42])
        try:
            run_args(["--on"])
            case("--on without a folder is refused", False, True)
        except ValueError:
            case("--on without a folder is refused", True, True)
        os.remove(os.path.join(d, "gpu_engines.csv"))
        case("no gpu_engines.csv: no program stage, not 0",
             any(k.startswith("program gpu%") for k in window_medians(d, w)), False)
    # --repeat: ref windows 37.2..37.8, repeat windows 38.55 / 38.56 carry; a stage only the repeat has is listed apart.
    W = lambda g, rr, **kw: {"proxy gap": (g, 1), "gpu InjectRTX/RR": (rr, 1), **{k: (v, 1) for k, v in kw.items()}}  # noqa: E731
    ref_runs = [[W(37.7, 13.3), W(37.2, 13.4)], [W(37.8, 13.4), W(37.25, 13.5)]]
    rep, only = repeat_compare(ref_runs, [[W(38.55, 13.4, **{"program gpu% dwm all": 3.0}),
                                           W(38.56, 13.45, **{"program gpu% dwm all": 5.0})]],
                               {"proxy gap": {"carries": False}, "gpu InjectRTX/RR": {"carries": True}})
    case("--repeat: gap 1.1 ms above last night carries", rep["proxy gap"]["carries"], True)
    case("--repeat: a stage inside last night's range does not carry", rep["gpu InjectRTX/RR"]["carries"], False)
    case("--repeat: plug-off carriers are marked", (rep["proxy gap"]["carriedPlugOff"],
                                                     rep["gpu InjectRTX/RR"]["carriedPlugOff"]), (False, True))
    case("--repeat: a stage last night lacks is listed as repeat-only, not compared",
         (only, "program gpu% dwm all" in rep), ({"program gpu% dwm all": [3.0, 5.0]}, False))
    print("monitor_stage_split self-test:", "PASS" if all(cases) else "FAIL")
    return 0 if all(cases) else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--self-test"]:
        sys.exit(self_test())
    try:
        if args[:1] == ["--repeat"] and len(args) in (2, 4):
            sys.exit(repeat_report(args[1], args[3] if len(args) == 4 and args[2] == "--json" else None))
        on_folders, off_folders, json_path = run_args(args)
        sys.exit(main_report(json_path, on_folders, off_folders))
    except (OSError, ValueError, KeyError) as e:
        print("bad input:", e)
        sys.exit(2)
