"""Balls-off rule (worker B, written Nov 17 before the run): does a Remix launch with [Lights] FlareStandIns=0 keep the
orb fix at every spot without losing lamp glows or adding flicker, against the balls-on launch 20261117-152548 on the
same build?

Both launches are scored the same way, stop by stop (every "still" window the two launches and the reference share):
  orbs   glow_blobs.py's unmatched count, the Remix still against the no-Remix reference still (symptom check)
  lost   glow_blobs.py's lost-glow count, the same pair
  pulse  gate.py's still_flicker block_pulse on the stop's frames (telemetry lumaThumb, as the gate's still_flicker)
A stop counts only when glow_blobs compares it in both launches (exit 0 or 1; exit 2 means the poses are apart) and
gate.py measures its pulse in both (every frame ray traced, enough frames).

Noise per spot and count, from the balls-on launch against itself (on against on_again): d = the largest
|count(a) - count(b)| over the pairs the launch repeats at one pose, y0_p0 / y0_p0_after and switch_base /
switch_base_again where the spot has them, with a floor of 1 for orbs and lost glows and 2 (/255) for pulse.
Per stop the band is 3 d; for a spot total over S stops it is 3 d sqrt(S).

Per spot, decided in this order:
  NOT MEASURED  fewer than MIN_STOPS stops compared in both launches, or no repeated pair to take the noise from.
  WORSE         any stop loses more than 3 d_lost glows more than with the balls on (a lamp glow the original shows is
                gone), or any stop's pulse rises by more than 3 d_pulse, or the spot's orb total rises by more than
                3 d_orbs sqrt(S).
  FIX HOLDS     the spot's orb total falls by more than 3 d_orbs sqrt(S).
  NO CHANGE     otherwise.
Overall: WORSE when any spot is WORSE; else FIX HOLDS when at least one spot is FIX HOLDS; else NO CHANGE; NOT
MEASURED when no spot is measured. Exit 0 FIX HOLDS, 1 WORSE, 3 NO CHANGE, 2 NOT MEASURED.

Expected (a guess from 152548 and probe 171517, where the balls-off switch steps read 0 orbs at con_bright_hall):
con_bright_hall FIX HOLDS; the other spots are not predicted. A spot whose orbs come from other causes (paladin_1,
where 44 of 71 orbs sit on a black original) may read NO CHANGE; that is a result, not a failure of the rule.

Usage: balls_off_rule.py <balls-off run> [--base <balls-on run>] [--reference <folder with one subfolder per spot>]
       balls_off_rule.py --self-test
"""
import argparse, concurrent.futures, importlib.util, json, math, pathlib, re, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
GLOW = HERE / "glow_blobs.py"
GATE = HERE.parent / "runtime" / "gate.py"
BASE = pathlib.Path(r"D:\BlacklistOverhaul\experiments\remix-gate\20261117-152548")
MIN_STOPS = 3
FLOOR = {"orbs": 1.0, "lost": 1.0, "pulse": 2.0}
PAIRS = (("y0_p0", "y0_p0_after"), ("switch_base", "switch_base_again"))
ORBS_RE = re.compile(r"glow_blobs: unmatched (\d+) of \d+ blobs")
LOST_RE = re.compile(r"glow_blobs: lost (\d+) of \d+ original glows")


def noise(stops: dict) -> dict | None:
    """d per count from the repeated pairs in one launch's {stop: {orbs, lost, pulse}}; None without a pair."""
    pairs = [(stops[a], stops[b]) for a, b in PAIRS if stops.get(a) and stops.get(b)]
    if not pairs:
        return None
    return {k: max(FLOOR[k], max(abs(p[k] - q[k]) for p, q in pairs)) for k in FLOOR}


def spot_verdict(base: dict, new: dict, d: dict | None) -> tuple[str, str]:
    """(verdict, detail) for one spot. base and new: {stop: {orbs, lost, pulse} or None}; d from noise(base)."""
    common = sorted(s for s in base if base[s] and new.get(s))
    if d is None:
        return "NOT MEASURED", "no repeated pair (y0_p0/y0_p0_after or switch_base/switch_base_again) for the noise"
    if len(common) < MIN_STOPS:
        return "NOT MEASURED", f"{len(common)} stops compared in both launches (need {MIN_STOPS})"
    band = 3 * d["orbs"] * math.sqrt(len(common))
    ob, on = sum(base[s]["orbs"] for s in common), sum(new[s]["orbs"] for s in common)
    lost_up = [f"{s} lost {base[s]['lost']} -> {new[s]['lost']}" for s in common
               if new[s]["lost"] - base[s]["lost"] > 3 * d["lost"]]
    pulse_up = [f"{s} pulse {base[s]['pulse']:.1f} -> {new[s]['pulse']:.1f}" for s in common
                if new[s]["pulse"] - base[s]["pulse"] > 3 * d["pulse"]]
    detail = (f"{len(common)} stops; orbs {ob} -> {on} (band {band:.1f}); lost {sum(base[s]['lost'] for s in common)}"
              f" -> {sum(new[s]['lost'] for s in common)} (per stop band {3 * d['lost']:g}); worst pulse rise "
              f"{max(new[s]['pulse'] - base[s]['pulse'] for s in common):.1f} (band {3 * d['pulse']:g})")
    if lost_up or pulse_up or on - ob > band:
        why = lost_up + pulse_up + ([f"orb total up by {on - ob}"] if on - ob > band else [])
        return "WORSE", detail + "; " + "; ".join(why)
    if ob - on > band:
        return "FIX HOLDS", detail
    return "NO CHANGE", detail


def overall(verdicts: list[str]) -> str:
    measured = [v for v in verdicts if v != "NOT MEASURED"]
    if not measured:
        return "NOT MEASURED"
    if "WORSE" in measured:
        return "WORSE"
    return "FIX HOLDS" if "FIX HOLDS" in measured else "NO CHANGE"


# ---- reading a launch --------------------------------------------------------------------------------------------

def load_gate():
    spec = importlib.util.spec_from_file_location("gate", GATE)
    gate = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("gate", gate)
    spec.loader.exec_module(gate)
    return gate


def glow(remix_spot: pathlib.Path, ref_spot: pathlib.Path, still: str, orig: str):
    """(orbs, lost) for one stop, or None when glow_blobs does not compare it."""
    p = subprocess.run([sys.executable, "-I", str(GLOW), "--remix-spot", str(remix_spot), "--orig-spot", str(ref_spot),
                        "--still", still, "--orig-still", orig], capture_output=True, text=True, timeout=300)
    o, l = ORBS_RE.search(p.stdout), LOST_RE.search(p.stdout)
    return (int(o[1]), int(l[1])) if p.returncode in (0, 1) and o and l else None


def score_launch(gate, run_dir: pathlib.Path, ref_dir: pathlib.Path) -> dict:
    """{spot: {stop: {orbs, lost, pulse} or None}} for the still and repeated switch windows of one launch."""
    run = gate.Run(str(run_dir))
    out = {}
    for s in gate.load_spots(run):
        ref_spot = ref_dir / s["name"]
        if not (ref_spot / "spot.json").exists():
            continue
        ref_stills = {w["name"] for w in json.loads((ref_spot / "spot.json").read_text())["windows"] if w.get("frames")}
        wins = [w for w in s["windows"] if w.get("frames") and (w["kind"] == "still" or w["name"] in
                                                                {n for p in PAIRS for n in p})]
        jobs = []
        for w in wins:
            orig = w["name"] if w["name"] in ref_stills else ("y0_p0" if w["kind"] == "switch" else None)
            if orig in ref_stills:
                jobs.append((w, orig))
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            counts = list(pool.map(lambda j: glow(pathlib.Path(s["_dir"]), ref_spot, j[0]["name"], j[1]), jobs))
        stops = {}
        for (w, _), c in zip(jobs, counts):
            ok, _ = gate.stop_all_ray_traced(s, w)
            frames, _ = gate.window_frames(s, w)
            n = 0 if frames is None else len(gate.dedupe(frames))
            if c is None or not ok or n < gate.THRESHOLDS["still_frames"]:
                stops[w["name"]] = None
                continue
            stops[w["name"]] = {"orbs": c[0], "lost": c[1], "pulse": gate.still_flicker(frames)[2]}
        out[s["name"]] = stops
    return out


def judge_launches(base: dict, new: dict, out=print) -> str:
    verdicts = []
    for spot in sorted(set(base) | set(new)):
        b, n = base.get(spot, {}), new.get(spot, {})
        # Switch windows only give the noise; the stops judged are the still windows both launches share.
        judged = lambda t: {k: v for k, v in t.items() if re.fullmatch(r"y-?\d+_p-?\d+(_after)?", k)}
        v, detail = spot_verdict(judged(b), judged(n), noise(b))
        verdicts.append(v)
        out(f"balls_off_rule: {spot}: {v} ({detail})")
    v = overall(verdicts)
    out(f"balls_off_rule: {v}")
    return v


# ---- fake cases ---------------------------------------------------------------------------------------------------

def self_test() -> int:
    fails = []

    def stops(orbs, lost=None, pulse=None, pair=(0, 0)):
        n = len(orbs)
        lost = lost or [2] * n
        pulse = pulse or [10.0] * n
        t = {f"y{45 * i}_p0": {"orbs": orbs[i], "lost": lost[i], "pulse": pulse[i]} for i in range(n)}
        t["y0_p0_after"] = {"orbs": orbs[0] + pair[0], "lost": lost[0] + pair[1], "pulse": pulse[0]}
        return t

    def case(name, base, new, want):
        v, detail = spot_verdict(base, new, noise(base))
        good = v == want
        print(f"{'ok  ' if good else 'FAIL'} {name}: {v} ({detail})")
        if not good:
            fails.append(name)

    on = stops([6, 5, 7, 4, 8, 6])
    case("balls off removes most orbs, lost and pulse unchanged: FIX HOLDS", on, stops([1, 0, 2, 1, 1, 0]), "FIX HOLDS")
    case("the same launch again (orbs move by 1 at two stops): NO CHANGE", on, stops([6, 6, 7, 4, 7, 6]), "NO CHANGE")
    case("orbs fall but one stop loses 4 more glows (band 3): WORSE", on,
         stops([1, 0, 2, 1, 1, 0], lost=[2, 2, 6, 2, 2, 2]), "WORSE")
    case("lost glows up by exactly the band (3) at one stop: not WORSE, FIX HOLDS", on,
         stops([1, 0, 2, 1, 1, 0], lost=[2, 2, 5, 2, 2, 2]), "FIX HOLDS")
    case("orbs fall but one stop's pulse rises by 10 (band 6): WORSE", on,
         stops([1, 0, 2, 1, 1, 0], pulse=[10, 10, 10, 20, 10, 10]), "WORSE")
    case("orb total rises beyond the band: WORSE", on, stops([12, 10, 13, 9, 14, 11]), "WORSE")
    noisy = stops([6, 5, 7, 4, 8, 6], pair=(3, 0))            # y0_p0 against y0_p0_after differs by 3 orbs
    case("a drop of 17 inside a noisy spot's band (3 x 3 x sqrt 7 = 23.8): NO CHANGE", noisy,
         stops([3, 3, 5, 2, 6, 3], pair=(3, 0)), "NO CHANGE")
    case("2 stops compared: NOT MEASURED", {k: v for k, v in list(on.items())[:1]} | {"y0_p0_after": on["y0_p0_after"]},
         stops([0]), "NOT MEASURED")
    no_pair = {k: v for k, v in on.items() if k != "y0_p0_after"}
    case("no repeated pair for the noise: NOT MEASURED", no_pair, stops([1, 0, 2, 1, 1, 0]), "NOT MEASURED")
    gone = stops([1, 0, 2, 1, 1, 0])
    gone["y90_p0"] = None                                     # a stop not compared in the new launch drops out
    case("a stop missing in one launch is left out of both totals", on, gone, "FIX HOLDS")
    for want, vs in (("WORSE", ["FIX HOLDS", "WORSE", "NO CHANGE"]), ("FIX HOLDS", ["FIX HOLDS", "NO CHANGE"]),
                     ("NO CHANGE", ["NO CHANGE", "NOT MEASURED"]), ("NOT MEASURED", ["NOT MEASURED"])):
        good = overall(vs) == want
        print(f"{'ok  ' if good else 'FAIL'} overall of {vs}: {overall(vs)}")
        if not good:
            fails.append(f"overall {vs}")
    print("self-test:", "all passed" if not fails else f"FAILED {fails}")
    return 1 if fails else 0


def main():
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    ap = argparse.ArgumentParser()
    ap.add_argument("new", type=pathlib.Path, help="the Remix launch with FlareStandIns=0")
    ap.add_argument("--base", type=pathlib.Path, default=BASE, help="the balls-on launch on the same build")
    ap.add_argument("--reference", type=pathlib.Path, help="no-Remix reference folder with one subfolder per spot "
                                                           "(default: <base>/reference-chosen)")
    a = ap.parse_args()
    ref = a.reference or a.base / "reference-chosen"
    gate = load_gate()
    v = judge_launches(score_launch(gate, a.base, ref), score_launch(gate, a.new, ref))
    sys.exit({"FIX HOLDS": 0, "WORSE": 1, "NOT MEASURED": 2, "NO CHANGE": 3}[v])


if __name__ == "__main__":
    main()
