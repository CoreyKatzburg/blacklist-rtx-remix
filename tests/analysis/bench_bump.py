"""Which bench-window load costs GPU time: the gdigrab screen capture or the nvidia-smi sampler (worker C, Nov 16).

Every bench window reads about 1.7 to 1.9 ms of GPU time above the same pose outside it (171502 and 101111). Two
things run only inside bench_window in drive.py: an ffmpeg gdigrab capture at 30 pictures a second, and
gpu_sampler.ps1 calling nvidia-smi every 200 ms. This scores a launch that holds one pose through live steps
whose spot.json windows carry "benchLoad": none, capture, none, sampler, none (in that order, displays awake).

Usage:
  bench_bump.py --self-test
  bench_bump.py SPOTDIR

Per step: median gpuTimeMs over ray-traced telemetry frames, skipping the first SKIP_MS of the step.
Verdict per suspect step S with neighbours L and R (the none steps either side), dL = S - L, dR = S - R:
  CAUSE        dL >= CAUSE_MS and dR >= CAUSE_MS
  NOT A CAUSE  |dL| <= NOISE_MS and |dR| <= NOISE_MS
  UNSETTLED    anything else
The launch is VOID (no verdicts) when: the three none steps spread more than NOISE_MS; a step has fewer than
MIN_FRAMES scored frames; a step's display was not awake at its start and end; or a load step has no proof that
its load ran (benchLoadProof captureFrames / samplerRows below LOAD_MIN of what its length asks for).
NOISE_MS is today's repeat-step spread (171502: awake steps 38.88, 38.47, 38.72, so 0.41 ms).
Report only: capture + sampler (mean of each step's dL, dR) against the bench bump the same launch measured.
Exit 0 = scored (any verdicts), 1 = VOID, 2 = bad input.
"""
import json
import os
import statistics as st
import sys

SKIP_MS = 2000.0
NOISE_MS = 0.4
CAUSE_MS = 0.6
MIN_FRAMES = 150
LOAD_MIN = 0.8
ORDER = ["none", "capture", "none", "sampler", "none"]


def step_medians(windows, frames):
    """windows: spot.json windows in order; frames: {frame: (qpcMs, gpuMs, rayTraced)}. Returns the load steps."""
    steps = []
    for w in windows:
        if "benchLoad" not in w or not w.get("telemetry"):
            continue
        a, b = w["telemetry"]
        rows = [frames[f] for f in range(a + 1, b + 1) if f in frames]
        if not rows:
            steps.append({"name": w["name"], "load": w["benchLoad"], "gpu": None, "n": 0, "w": w})
            continue
        t0 = rows[0][0]
        gpu = [g for q, g, rt in rows if rt and q - t0 >= SKIP_MS]
        steps.append({"name": w["name"], "load": w["benchLoad"], "gpu": st.median(gpu) if gpu else None,
                      "n": len(gpu), "w": w, "seconds": (rows[-1][0] - t0) / 1000.0})
    return steps


def void_reasons(steps):
    reasons = []
    if [s["load"] for s in steps] != ORDER:
        return [f"steps {[s['load'] for s in steps]} are not {ORDER}"]
    for s in steps:
        if s["n"] < MIN_FRAMES:
            reasons.append(f"{s['name']}: {s['n']} scored frames, below {MIN_FRAMES}")
        d = s["w"].get("display") or {}
        if d.get("display") != "on" or d.get("after") != "on":
            reasons.append(f"{s['name']}: display {d.get('display')} at start, {d.get('after')} at end, not awake")
        proof = s["w"].get("benchLoadProof") or {}
        secs = s.get("seconds", 0.0)
        if s["load"] == "capture" and proof.get("captureFrames", 0) < LOAD_MIN * 30 * secs:
            reasons.append(f"{s['name']}: capture wrote {proof.get('captureFrames', 0)} pictures in {secs:.1f} s")
        if s["load"] == "sampler" and proof.get("samplerRows", 0) < LOAD_MIN * 5 * secs:
            reasons.append(f"{s['name']}: sampler wrote {proof.get('samplerRows', 0)} rows in {secs:.1f} s")
    nones = [s["gpu"] for s in steps if s["load"] == "none" and s["gpu"] is not None]
    if len(nones) == 3 and max(nones) - min(nones) > NOISE_MS:
        reasons.append(f"none steps spread {max(nones) - min(nones):.2f} ms, above {NOISE_MS}")
    return reasons


def verdicts(steps):
    out = {}
    for i in (1, 3):
        s, left, right = steps[i]["gpu"], steps[i - 1]["gpu"], steps[i + 1]["gpu"]
        dl, dr = s - left, s - right
        if dl >= CAUSE_MS and dr >= CAUSE_MS:
            v = "CAUSE"
        elif abs(dl) <= NOISE_MS and abs(dr) <= NOISE_MS:
            v = "NOT A CAUSE"
        else:
            v = "UNSETTLED"
        out[steps[i]["load"]] = (v, dl, dr)
    return out


def score(steps):
    """Returns (exit code, lines)."""
    lines = [f"{s['name']:12s} {s['load']:8s} gpu {s['gpu'] if s['gpu'] is None else round(s['gpu'], 2)!s:>7} "
             f"({s['n']} frames)" for s in steps]
    reasons = void_reasons(steps)
    if reasons:
        return 1, lines + [f"VOID: {r}" for r in reasons]
    v = verdicts(steps)
    for load, (verdict, dl, dr) in v.items():
        lines.append(f"{load:8s} {verdict:12s} {dl:+.2f} ms against the step before, {dr:+.2f} ms against the step after")
    total = sum((dl + dr) / 2 for _, dl, dr in v.values())
    lines.append(f"capture + sampler {total:+.2f} ms (report only; compare with this launch's bench bump)")
    return 0, lines


def load_spot(spot):
    with open(os.path.join(spot, "spot.json")) as f:
        windows = json.load(f)["windows"]
    frames = {}
    with open(os.path.join(spot, "telemetry.jsonl"), errors="replace") as f:
        for line in f:
            r = json.loads(line)
            frames[r["frame"]] = (r["qpcMs"], r["gpuTimeMs"], bool(r.get("rayTraced")))
    return windows, frames


def bench_bump(windows, frames):
    """The launch's own bench bump: bench_y0_p0 against the y0_p0 window right after it (report only)."""
    def med(name):
        w = next((w for w in windows if w["name"] == name and w.get("telemetry")), None)
        if not w:
            return None
        g = [frames[f][1] for f in range(w["telemetry"][0] + 1, w["telemetry"][1] + 1) if f in frames and frames[f][2]]
        return st.median(g) if g else None
    b, y = med("bench_y0_p0"), med("y0_p0")
    return None if b is None or y is None else b - y


def fake_launch(gpus, display=("on", "on"), proof=None, frames_per_step=500, start=1000):
    """Five steps of 20 s at 40 ms a frame, GPU ms flat per step with +-0.05 ripple."""
    windows, frames = [], {}
    f, q = start, 0.0
    for i, (load, g) in enumerate(zip(ORDER, gpus)):
        a = f
        for k in range(frames_per_step):
            f += 1
            q += 40.0
            frames[f] = (q, g + (0.05 if k % 2 else -0.05), True)
        p = {"capture": {"captureFrames": 600}, "sampler": {"samplerRows": 100}, **(proof or {})}.get(load, {})
        windows.append({"name": f"live_step{6 + i}", "telemetry": [a, f], "benchLoad": load,
                        "display": {"display": display[0], "after": display[1]}, "benchLoadProof": p})
    return windows, frames


def self_test():
    cases = [
        ("capture costs 1.8 ms", [38.5, 40.3, 38.6, 38.5, 38.6], {"capture": "CAUSE", "sampler": "NOT A CAUSE"}, 0, {}),
        ("sampler costs 1.8 ms", [38.5, 38.6, 38.5, 40.3, 38.6], {"capture": "NOT A CAUSE", "sampler": "CAUSE"}, 0, {}),
        ("neither", [38.5, 38.7, 38.5, 38.4, 38.6], {"capture": "NOT A CAUSE", "sampler": "NOT A CAUSE"}, 0, {}),
        ("half each, 0.5 ms", [38.5, 39.0, 38.5, 39.0, 38.5], {"capture": "UNSETTLED", "sampler": "UNSETTLED"}, 0, {}),
        ("above one neighbour only", [38.4, 39.05, 38.8, 38.8, 38.5], {"capture": "UNSETTLED", "sampler": "NOT A CAUSE"}, 0, {}),
        ("none steps drift 1 ms", [38.0, 40.0, 38.5, 39.0, 39.0], None, 1, {}),
        ("display asleep", [38.5, 40.3, 38.6, 38.5, 38.6], None, 1, {"display": ("off", "off")}),
        ("display fell asleep in the step", [38.5, 40.3, 38.6, 38.5, 38.6], None, 1, {"display": ("on", "off")}),
        ("capture never ran", [38.5, 40.3, 38.6, 38.5, 38.6], None, 1, {"proof": {"capture": {}}}),
        ("sampler wrote a third", [38.5, 38.6, 38.5, 40.3, 38.6], None, 1, {"proof": {"sampler": {"samplerRows": 33}}}),
        ("too few frames", [38.5, 40.3, 38.6, 38.5, 38.6], None, 1, {"frames_per_step": 100}),
    ]
    bad = 0
    for name, gpus, want, want_code, kw in cases:
        steps = step_medians(*fake_launch(gpus, **kw))
        code, lines = score(steps)
        got = None if code else {k: v[0] for k, v in verdicts(steps).items()}
        ok = code == want_code and got == want
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}: exit {code}, {got}")
        if not ok:
            print("     " + "\n     ".join(lines))
    # The skip: a step whose first 2 s are 5 ms slow (the dump before it) must not read slow.
    w, fr = fake_launch([38.5, 38.6, 38.5, 38.5, 38.6])
    a = w[1]["telemetry"][0]
    for k in range(1, 40):
        q, g, rt = fr[a + k]
        fr[a + k] = (q, g + 5.0, rt)
    steps = step_medians(w, fr)
    code, _ = score(steps)
    ok = code == 0 and verdicts(steps)["capture"][0] == "NOT A CAUSE" and steps[1]["n"] == 450
    bad += not ok
    print(f"{'ok  ' if ok else 'FAIL'} slow first 1.6 s of a step is skipped")
    print("bench_bump self-test:", "PASS" if not bad else f"FAIL ({bad})")
    return 1 if bad else 0


def main(argv):
    if argv[1:] == ["--self-test"]:
        return self_test()
    if len(argv) != 2:
        print(__doc__)
        return 2
    windows, frames = load_spot(argv[1])
    code, lines = score(step_medians(windows, frames))
    print("\n".join(lines))
    bump = bench_bump(windows, frames)
    if bump is not None:
        print(f"this launch's bench bump: bench_y0_p0 minus y0_p0 = {bump:+.2f} ms (report only)")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
