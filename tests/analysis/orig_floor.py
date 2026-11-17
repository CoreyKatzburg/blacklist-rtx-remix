"""Control 2e (coordinator, Nov 17): does a newly taken original-game picture still match the old reference?

Two no-Remix launches at the same stop must agree within worker A's original-against-original floor: brightness ratio
0.996 to 1.012 (ledger row 3). The statistic is the gate's own scene_brightness one: gamma_mean of
<stop>_raster.pfm with the HUD border masked (pfm_brightness.compare_final_to_raster, outer 8 % cut on every edge,
which also cuts the NVIDIA overlay box in the top 64 rows). If a retaken original falls outside the floor, the no-Remix
pictures of that launch do not count.

The two pictures must show the same view: Sam's recorded location (raster.json "location") within 8 units and the
camera within 1.0 degree of yaw and pitch ("yawPitch"), else NOT MEASURED.

  orig_floor.py <new no-Remix spot folder> <reference spot folder> [--stop y0_p0]
  orig_floor.py --self-test

Exit 0 = INSIDE the floor, 3 = OUTSIDE, 1 = NOT MEASURED (a file missing, a black picture, or the views differ).
"""
import json
import math
import os
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "brightness"))
import pfm_brightness  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "runtime"))
import gate  # noqa: E402

FLOOR = (0.996, 1.012)
MAX_LOCATION = 8.0      # units (cm), worker B's same-camera limit
MAX_DEG = 1.0           # pfm_brightness.DEFAULT_MAX_POSE_ERROR_DEG


def load(spot: str, stop: str):
    pfm, js = (os.path.join(spot, "gate", f"{stop}_raster.{ext}") for ext in ("pfm", "json"))
    if not (os.path.exists(pfm) and os.path.exists(js)):
        return None, None, f"{spot}: no {stop}_raster.pfm/json"
    with open(js, encoding="utf-8") as f:
        meta = json.load(f)
    return gate.read_pfm(pfm), meta, None


def score(new_spot: str, ref_spot: str, stop: str = "y0_p0") -> tuple[int, dict]:
    out: dict = {"new": new_spot, "reference": ref_spot, "stop": stop, "floor": list(FLOOR)}
    a, ma, why_a = load(new_spot, stop)
    b, mb, why_b = load(ref_spot, stop)
    if why_a or why_b:
        out.update(verdict="NOT MEASURED", why=why_a or why_b)
        return 1, out
    la, lb = ma.get("location"), mb.get("location")
    out["locationApart"] = round(math.dist(la, lb), 2) if la and lb else None
    ya, yb = ma.get("yawPitch"), mb.get("yawPitch")
    out["degApart"] = round(max(abs(((ya[0] - yb[0]) + 180) % 360 - 180), abs(ya[1] - yb[1])), 3) if ya and yb else None
    if out["locationApart"] is None or out["degApart"] is None:
        out.update(verdict="NOT MEASURED", why="location or yawPitch not recorded")
        return 1, out
    if out["locationApart"] > MAX_LOCATION or out["degApart"] > MAX_DEG:
        out.update(verdict="NOT MEASURED", why=f"views differ ({out['locationApart']} units, {out['degApart']} deg; "
                                               f"limits {MAX_LOCATION:g} units, {MAX_DEG:g} deg)")
        return 1, out
    cmp = pfm_brightness.compare_final_to_raster(a, b, mask_hud=True)
    ratio = cmp["gamma_mean_ratio"]
    out.update(newGammaMean=round(cmp["final_gamma_mean"], 5), refGammaMean=round(cmp["raster_gamma_mean"], 5))
    if ratio is None or pfm_brightness.image_brightness_stats(a)["gamma_mean"] < pfm_brightness.MIN_RASTER_LUMA:
        out.update(verdict="NOT MEASURED", why="a black picture")
        return 1, out
    out["ratio"] = round(ratio, 4)
    inside = FLOOR[0] <= ratio <= FLOOR[1]
    out["verdict"] = "INSIDE" if inside else "OUTSIDE"
    return (0 if inside else 3), out


def self_test() -> int:
    rng = np.random.default_rng(7)
    base = rng.uniform(0.1, 0.6, (90, 160, 3)).astype(np.float32)
    tmp = tempfile.mkdtemp()

    def spot(name, img, location=(0.0, 0.0, 0.0), yaw_pitch=(-66.9, 0.0)):
        d = os.path.join(tmp, name)
        os.makedirs(os.path.join(d, "gate"))
        gate.write_pfm(os.path.join(d, "gate", "y0_p0_raster.pfm"), img)
        with open(os.path.join(d, "gate", "y0_p0_raster.json"), "w") as f:
            json.dump({"location": list(location), "yawPitch": list(yaw_pitch)}, f)
        return d

    hud = base.copy()
    hud[:5] = 1.0                                                   # top band only (HUD and overlay box)
    cases = [("same picture", spot("a", base), 0),
             ("0.4 % brighter", spot("b", base * 1.004), 0),
             ("3 % brighter", spot("c", base * 1.03), 3),
             ("2 % darker", spot("d", base * 0.98), 3),
             ("change in the top band only", spot("e", hud), 0),
             ("Sam 20 units away", spot("f", base, location=(20.0, 0.0, 0.0)), 1),
             ("camera 2 deg off", spot("g", base, yaw_pitch=(-64.9, 0.0)), 1),
             ("no picture", os.path.join(tmp, "missing"), 1)]
    ref = spot("ref", base)
    ok = True
    for name, d, want in cases:
        code, out = score(d, ref)
        good = code == want
        ok &= good
        print(f"{name}: exit {code} {out.get('verdict')} ratio {out.get('ratio')} -> {'PASS' if good else 'FAIL'}")
    print("self-test", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    if argv[:1] == ["--self-test"]:
        return self_test()
    stop = "y0_p0"
    if "--stop" in argv:
        i = argv.index("--stop")
        stop = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    if len(argv) != 2:
        print(__doc__)
        return 2
    code, out = score(argv[0], argv[1], stop)
    print(json.dumps(out, indent=1))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
