"""Control 2e (coordinator, Nov 17): does a newly taken original-game spot still match the old reference?

The statistic is the gate's own scene_brightness one: gamma_mean of <stop>_raster.pfm with the HUD border masked
(pfm_brightness.compare_final_to_raster, outer 8 % cut on every edge, which also cuts the NVIDIA overlay box in the top
64 rows), as the ratio new / reference at each stop.

A stop is scored only when both pictures show the same view: Sam's recorded location (raster.json "location") within
8 units and the camera within 1.0 degree of yaw and pitch ("yawPitch"), and neither picture black.

The whole spot is judged, not one stop (coordinator, Nov 17 10:4x; one stop of the original against itself already
reads 0.9877):
  - fewer than MIN_STOPS (10) scored stops: NOT MEASURED;
  - INSIDE needs both the spot median inside MEDIAN_BAND (0.996 to 1.012) and every scored stop inside STOP_BAND (0.987
    to 1.030); anything else with enough stops is OUTSIDE and names the stops.
The bands are the measured range of the original against itself, not a chosen tolerance: --bands recomputes them from
the three lit original-against-original pairs in PAIRS (55 same-view stops). If a retake lands outside them, that is a
finding, not a reason to widen them.

  orig_floor.py <new no-Remix spot folder> <reference spot folder>
  orig_floor.py --bands      recompute the ranges from PAIRS, check the bands hold them, score each pair as a spot
  orig_floor.py --self-test

Exit 0 = INSIDE, 3 = OUTSIDE, 1 = NOT MEASURED, 2 = bad input. --bands: 0 when the bands match the measured ranges and
every pair reads INSIDE, else 3.
"""
import glob
import json
import math
import os
import statistics
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "brightness"))
import pfm_brightness  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "runtime"))
import gate  # noqa: E402

MEDIAN_BAND = (0.996, 1.012)   # worker A's original-against-original floor; the three measured medians lie inside
STOP_BAND = (0.987, 1.030)     # min and max over the 55 same-view stops of PAIRS, rounded outward to 0.001
MIN_STOPS = 10
MAX_LOCATION = 8.0             # units (cm), worker B's same-camera limit
MAX_DEG = 1.0                  # pfm_brightness.DEFAULT_MAX_POSE_ERROR_DEG
GATE_RUNS = r"D:\BlacklistOverhaul\experiments\remix-gate"
PAIRS = [("reference-20261116-000454", "reference-20261116-175705", "con_custodial"),
         ("reference-20261116-000454", "reference-20261116-191120", "con_bright_hall"),
         ("reference-20261116-174116", "reference-20261116-175705", "con_boiler")]


def load(spot: str, stop: str):
    pfm, js = (os.path.join(spot, "gate", f"{stop}_raster.{ext}") for ext in ("pfm", "json"))
    if not (os.path.exists(pfm) and os.path.exists(js)):
        return None, None, f"no {stop}_raster.pfm/json in {spot}"
    with open(js, encoding="utf-8") as f:
        meta = json.load(f)
    return gate.read_pfm(pfm), meta, None


def stop_ratio(new_spot: str, ref_spot: str, stop: str) -> dict:
    """{"stop", "ratio" (None when not scored), "why", "locationApart", "degApart"} for one stop."""
    out: dict = {"stop": stop, "ratio": None}
    a, ma, why_a = load(new_spot, stop)
    b, mb, why_b = load(ref_spot, stop)
    if why_a or why_b:
        out["why"] = why_a or why_b
        return out
    la, lb, ya, yb = ma.get("location"), mb.get("location"), ma.get("yawPitch"), mb.get("yawPitch")
    if not (la and lb and ya and yb):
        out["why"] = "location or yawPitch not recorded"
        return out
    out["locationApart"] = round(math.dist(la, lb), 2)
    out["degApart"] = round(max(abs(((ya[0] - yb[0]) + 180) % 360 - 180), abs(ya[1] - yb[1])), 3)
    if out["locationApart"] > MAX_LOCATION or out["degApart"] > MAX_DEG:
        out["why"] = f"views differ ({out['locationApart']} units, {out['degApart']} deg)"
        return out
    cmp = pfm_brightness.compare_final_to_raster(a, b, mask_hud=True)
    if cmp["gamma_mean_ratio"] is None or cmp["final_gamma_mean"] < pfm_brightness.MIN_RASTER_LUMA:
        out["why"] = "a black picture"
        return out
    out["ratio"] = round(float(cmp["gamma_mean_ratio"]), 4)
    return out


def stops_of(spot: str) -> list[str]:
    return sorted(os.path.basename(p)[:-len("_raster.pfm")] for p in glob.glob(os.path.join(spot, "gate", "*_raster.pfm")))


def judge(ratios: dict[str, float]) -> tuple[int, dict]:
    """The spot verdict from the scored stops' ratios."""
    out: dict = {"scored": len(ratios), "minStops": MIN_STOPS, "medianBand": list(MEDIAN_BAND),
                 "stopBand": list(STOP_BAND)}
    if len(ratios) < MIN_STOPS:
        out.update(verdict="NOT MEASURED", why=f"{len(ratios)} scored stops, fewer than {MIN_STOPS}")
        return 1, out
    med = statistics.median(ratios.values())
    out.update(median=round(med, 4), min=min(ratios.values()), max=max(ratios.values()))
    outside = {s: r for s, r in ratios.items() if not STOP_BAND[0] <= r <= STOP_BAND[1]}
    why = ([f"median {med:.4f} outside {MEDIAN_BAND[0]} to {MEDIAN_BAND[1]}"]
           if not MEDIAN_BAND[0] <= med <= MEDIAN_BAND[1] else []) + \
          [f"{s} {r:.4f} outside {STOP_BAND[0]} to {STOP_BAND[1]}" for s, r in outside.items()]
    if why:
        out.update(verdict="OUTSIDE", why="; ".join(why))
        return 3, out
    out["verdict"] = "INSIDE"
    return 0, out


def score_spot(new_spot: str, ref_spot: str) -> tuple[int, dict]:
    stops = [stop_ratio(new_spot, ref_spot, s) for s in stops_of(new_spot)]
    code, out = judge({s["stop"]: s["ratio"] for s in stops if s["ratio"] is not None})
    out.update(new=new_spot, reference=ref_spot,
               notScored={s["stop"]: s["why"] for s in stops if s["ratio"] is None}, stops=stops)
    return code, out


def bands() -> int:
    every, medians, ok = [], [], True
    for new, ref, spot in PAIRS:
        code, out = score_spot(os.path.join(GATE_RUNS, new, spot), os.path.join(GATE_RUNS, ref, spot))
        ratios = [s["ratio"] for s in out["stops"] if s["ratio"] is not None]
        every += ratios
        if ratios:
            medians.append(statistics.median(ratios))
        inside = sum(MEDIAN_BAND[0] <= r <= MEDIAN_BAND[1] for r in ratios)
        print(f"{spot} {new[-6:]} against {ref[-6:]}: {len(ratios)} of {len(out['stops'])} scored, "
              f"{min(ratios, default=float('nan')):.4f} to {max(ratios, default=float('nan')):.4f}, median "
              f"{statistics.median(ratios) if ratios else float('nan'):.4f}, {inside} inside {MEDIAN_BAND} -> "
              f"{out['verdict']}")
        ok &= code == 0
    lo, hi = min(every), max(every)
    stop_band = (math.floor(lo * 1000) / 1000, math.ceil(hi * 1000) / 1000)
    print(f"all scored stops: {len(every)}, range {lo:.4f} to {hi:.4f} -> stop band {stop_band} "
          f"(in the code: {STOP_BAND})")
    print(f"spot medians: {min(medians):.4f} to {max(medians):.4f} (median band in the code: {MEDIAN_BAND})")
    ok &= stop_band == STOP_BAND and all(MEDIAN_BAND[0] <= m <= MEDIAN_BAND[1] for m in medians)
    print("bands", "MATCH the measured ranges" if ok else "DO NOT MATCH")
    return 0 if ok else 3


def self_test() -> int:
    rng = np.random.default_rng(7)
    base = rng.uniform(0.1, 0.6, (90, 160, 3)).astype(np.float32)
    tmp = tempfile.mkdtemp()

    def spot(name, gains, location=(0.0, 0.0, 0.0), yaw_pitch=(-66.9, 0.0), img=None):
        d = os.path.join(tmp, name)
        os.makedirs(os.path.join(d, "gate"))
        for i, g in enumerate(gains):
            gate.write_pfm(os.path.join(d, "gate", f"y{i * 15}_p0_raster.pfm"), (base if img is None else img) * g)
            with open(os.path.join(d, "gate", f"y{i * 15}_p0_raster.json"), "w") as f:
                json.dump({"location": list(location), "yawPitch": [yaw_pitch[0] + i * 15, yaw_pitch[1]]}, f)
        return d

    hud = base.copy()
    hud[:5] = 1.0                                                   # top band only (HUD and overlay box)
    ref = spot("ref", [1.0] * 12)
    cases = [("12 stops, small noise", spot("a", [1.0, 1.004, 0.998, 1.002] * 3), 0),
             ("every stop 2 % brighter: inside the stop band, median outside", spot("b", [1.02] * 12), 3),
             ("every stop 1 % dimmer: inside the stop band, median outside", spot("c1", [0.99] * 12), 3),
             ("every stop 3 % dimmer (outside both bands: 0.97 < 0.987)", spot("c", [0.97] * 12), 3),
             ("one stop at 1.05, good median", spot("d", [1.0] * 11 + [1.05]), 3),
             ("9 scored stops (3 views differ)", spot("e", [1.0] * 12), None),
             ("9 stops only", spot("f", [1.0] * 9), 1),
             ("a change in the top band only", spot("g", [1.0] * 12, img=hud), 0),
             ("Sam 20 units away everywhere", spot("h", [1.0] * 12, location=(20.0, 0.0, 0.0)), 1)]
    # case e: move three of its stops' cameras 2 degrees, so only 9 pass the view rule
    k = next(i for i, c in enumerate(cases) if c[2] is None)
    e = cases[k][1]
    for i in (0, 1, 2):
        p = os.path.join(e, "gate", f"y{i * 15}_p0_raster.json")
        with open(p) as f:
            meta = json.load(f)
        meta["yawPitch"][0] += 2.0
        with open(p, "w") as f:
            json.dump(meta, f)
    cases[k] = (cases[k][0], e, 1)
    ok = True
    for name, d, want in cases:
        code, out = score_spot(d, ref)
        good = code == want
        ok &= good
        print(f"{name}: exit {code} {out['verdict']} scored {out['scored']} median {out.get('median')} "
              f"{out.get('why', '')} -> {'PASS' if good else 'FAIL'}")
        if want == 3 and "1.05" in name:
            ok &= "y165_p0" in out["why"]                            # the bad stop is named
    print("self-test", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    if argv[:1] == ["--self-test"]:
        return self_test()
    if argv[:1] == ["--bands"]:
        return bands()
    if len(argv) != 2:
        print(__doc__)
        return 2
    code, out = score_spot(argv[0], argv[1])
    print(json.dumps({k: v for k, v in out.items() if k != "stops"}, indent=1))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
