"""Read gpu_engines.csv (runtime\\gpu_engines_sampler.ps1): GPU use per program group, per sample and per window.

Groups: game (Blacklist_game, Blacklist_DX11_game), bridge (NvRemixBridge), dwm, nvidia overlay (NVIDIA Overlay,
NVIDIA Share), other (every other process). Per sample, each group's 3d-engine percent and its all-engine percent
(summed over the group's processes and engine types; can pass 100). A sample counts only when its "_sample" row has
at least one counter instance; a group absent from a counted sample used 0 % in it. No counted sample in a window
reads NOT RECORDED, never 0.

  gpu_engines.py summary CSV [--from HH:MM:SS[.fff] --to HH:MM:SS[.fff]]   medians per group
  gpu_engines.py --self-test
"""
import os
import statistics as st
import sys
from datetime import datetime

GROUPS = {"game": ("blacklist_game", "blacklist_dx11_game"), "bridge": ("nvremixbridge",), "dwm": ("dwm",),
          "nvidia overlay": ("nvidia overlay", "nvidia share")}
ORDER = list(GROUPS) + ["other"]
NOT_RECORDED = "NOT RECORDED"


def group_of(process):
    p = process.lower()
    return next((g for g, names in GROUPS.items() if p in names), "other")


def _time(text):
    for fmt in ("%H:%M:%S.%f", "%H:%M:%S"):
        try:
            return datetime.strptime(text.strip(), fmt).time()
        except ValueError:
            pass
    return None


def samples(path, t0=None, t1=None):
    """[(time, {stage: percent})] for every counted sample in [t0, t1]; [] when the file is missing or empty."""
    if not path or not os.path.exists(path):
        return []
    rows = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        next(f, None)
        for ln in f:
            p = ln.rstrip("\r\n").split(",")
            if len(p) < 6 or (t := _time(p[0])) is None:
                continue
            if (t0 and t < t0) or (t1 and t > t1):
                continue
            s = rows.setdefault(p[0], {"t": t, "n": 0, "rows": []})
            if p[2] == "_sample":
                s["n"] = int(p[5] or 0)
            elif p[2] != "_error":
                try:
                    s["rows"].append((group_of(p[2]), p[3], float(p[4])))
                except ValueError:
                    pass
    out = []
    for s in rows.values():
        if s["n"] <= 0:
            continue
        v = {f"{g} {e}": 0.0 for g in ORDER for e in ("3d", "all")}
        for g, eng, pct in s["rows"]:
            v[f"{g} all"] += pct
            if eng == "3d":
                v[f"{g} 3d"] += pct
        out.append((s["t"], v))
    return sorted(out, key=lambda x: x[0])


def window_stages(path, clock):
    """{"program gpu% <group> <3d|all>": [per-sample percent]} inside a window's clock range ([HH:MM:SS.fff] * 2);
    {} when nothing was recorded there, so a missing recording is never read as 0 %."""
    if not clock:
        return {}
    got = samples(path, _time(clock[0]), _time(clock[1]))
    if not got:
        return {}
    return {f"program gpu% {k}": [v[k] for _, v in got] for k in got[0][1]}


def summary(path, t0=None, t1=None):
    got = samples(path, _time(t0) if t0 else None, _time(t1) if t1 else None)
    if not got:
        return {k: NOT_RECORDED for g in ORDER for k in (f"{g} 3d", f"{g} all")}, 0
    return {k: round(st.median(v[k] for _, v in got), 3) for k in got[0][1]}, len(got)


def self_test():
    import tempfile
    ok = []

    def case(name, got, want):
        ok.append(got == want)
        print(f"{'ok  ' if got == want else 'FAIL'} {name}: {got}")

    head = "time,pid,process,engtype,percent,instances\n"

    def sample(t, rows, n=500):
        return "".join(f"{t},{pid},{proc},{eng},{pct},\n" for pid, proc, eng, pct in rows) + f"{t},-1,_sample,all,0,{n}\n"

    with tempfile.TemporaryDirectory() as d:
        def put(name, text):
            path = os.path.join(d, name)
            open(path, "w", encoding="utf-8").write(text)
            return path
        # dwm at 10 % in 3d (split over two engines of one pid in one sample), game 80 % 3d + 5 % copy, an unknown
        # process 2 % in other; the third sample has no dwm row (a measured 0 %), the fourth lies outside the window.
        base = [(1000, "Blacklist_game", "3d", 80), (1000, "Blacklist_game", "copy", 5), (77, "Foo", "3d", 2)]
        p = put("good.csv", head + sample("01:00:00.000", base + [(636, "dwm", "3d", 6), (636, "dwm", "3d", 4)])
                + sample("01:00:01.000", base + [(636, "dwm", "3d", 10)])
                + sample("01:00:02.000", base + [(636, "dwm", "3d", 10)])
                + sample("01:00:03.000", base)
                + sample("01:00:09.000", base + [(636, "dwm", "3d", 90)]))
        m, n = summary(p, "01:00:00", "01:00:05")
        case("dwm at 10 % reads 10 % (median of 10, 10, 10, 0)", m["dwm 3d"], 10.0)
        case("game 3d 80, all 85", (m["game 3d"], m["game all"]), (80.0, 85.0))
        case("unknown process goes to other", m["other 3d"], 2.0)
        case("sample outside the window is left out", n, 4)
        case("bridge not running reads 0 in counted samples", m["bridge all"], 0.0)
        st_ = window_stages(p, ["01:00:00.000", "01:00:05.000"])
        case("window stage per sample", st_["program gpu% dwm 3d"], [10.0, 10.0, 10.0, 0.0])
        case("missing file reads NOT RECORDED", summary(os.path.join(d, "none.csv"))[0]["dwm 3d"], NOT_RECORDED)
        case("header-only file reads NOT RECORDED", summary(put("empty.csv", head))[0]["dwm 3d"], NOT_RECORDED)
        case("samples with 0 counter instances read NOT RECORDED",
             summary(put("zero.csv", head + sample("01:00:00.000", [], n=0)))[0]["dwm 3d"], NOT_RECORDED)
        case("only error rows read NOT RECORDED",
             summary(put("err.csv", head + "01:00:00.000,-1,_error,all,,0\n"))[0]["dwm 3d"], NOT_RECORDED)
        case("no window stages when nothing recorded", window_stages(os.path.join(d, "none.csv"), ["01:00:00", "01:00:05"]), {})
    print(f"gpu_engines self-test: {sum(ok)} of {len(ok)} pass")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--self-test"]:
        sys.exit(self_test())
    if a[:1] == ["summary"] and len(a) >= 2:
        t0 = a[a.index("--from") + 1] if "--from" in a else None
        t1 = a[a.index("--to") + 1] if "--to" in a else None
        m, n = summary(a[1], t0, t1)
        print(f"{n} counted samples")
        for k, v in m.items():
            print(f"  {k:22} {v}")
        sys.exit(0 if n else 3)
    print(__doc__)
    sys.exit(2)
