"""Verdict per variant of a frame-time settings sweep (coordinator, Nov 17: the cut sweep, variants_con_custodial_sweep_cut.json).

  sweep_score.py <sweep run> --reference <no-Remix run> [--spot con_custodial] [--orig-still y0_p0]
  sweep_score.py --self-test

Writes sweep_score.json and sweep_score.md in the run and prints the table. Every number is one launch.

Frame time: settings_report.py's median interval between Presents over the bench window (proxy frame log, present also
with Remix telemetry off). The base launches are the variants named *_base. The floor is the spread between the bases,
at least FLOOR_MIN_MS (0.626 ms, the spread of the four plug-on windows of 20261117-105336). A variant is FASTER only
when its frame time is below every base by more than the floor, SLOWER when above every base by more than the floor,
otherwise NOT FASTER.

Picture, against the original game (the reference run's <spot> folder):
  - brightness: pfm_brightness.compare_final_to_raster on the variant's y0_p0_final.pfm and the reference's
    <orig-still>_raster.pfm; distances from the original: |ln mean ratio|, |near-black share difference|,
    |near-white share difference|.
  - flicker: gate.still_flicker on the bench window's screen frames (frames/bench.gray, overlay box masked as the gate
    does), the same source in every variant (telemetry thumbnails are absent with telemetry off).
  - blobs: glow_blobs.py on the bench window against the reference's <orig-still> still: orbs unmatched and lost glows
    (puffs printed, report only, as in glow_blobs).
A picture number is WORSE when it exceeds the worse of the two bases by more than the two bases' own spread in that
number (the picture noise measured in this run; one pair, so it is a floor from one launch pair, not a measured range).
A FASTER variant with any WORSE number reads "FASTER, PICTURE WORSE" and names it; one whose picture number could not
be measured reads "FASTER, PICTURE NOT MEASURED".

Refused: a variant in another display state or Amp-window state than the bases (display_state.py), or either base not
in a known display state with the Amp window minimized, reads NOT COMPARABLE; a variant with an error or rtx keys not
loaded reads NOT MEASURED. Fewer than two measured bases: every variant reads NOT MEASURED.
mouse_lag is reported beside it (median game share, turns shorter than the variant's median frame; LAG_SHORT_MAX or
more makes it NOT MEASURED, as gate.input_lag_check) and does not enter the verdict.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(TESTS, "analysis", "brightness"))
import display_state  # noqa: E402
import gate  # noqa: E402
import settings_report  # noqa: E402

FLOOR_MIN_MS = 0.626
GLOW_BLOBS = os.path.join(TESTS, "analysis", "glow_blobs.py")
PIC_KEYS = ("brightLogRatio", "nearBlackDiff", "nearWhiteDiff", "flickerJitter", "flickerDrift", "flickerPulse",
            "flickerStep4", "orbsUnmatched", "lostGlows")
COMPARE_OK = (display_state.AMP_MIN,)


def is_base(name: str) -> bool:
    return name.endswith("_base") or "_base_" in name


def frame_verdict(v_ms: float, base_ms: list[float]) -> tuple[str, float]:
    """(FASTER / SLOWER / NOT FASTER, floor) for one variant against the measured bases."""
    floor = max(FLOOR_MIN_MS, max(base_ms) - min(base_ms))
    if v_ms < min(base_ms) - floor:
        return "FASTER", floor
    if v_ms > max(base_ms) + floor:
        return "SLOWER", floor
    return "NOT FASTER", floor


def picture_worse(v: dict, bases: list[dict]) -> tuple[list[str], list[str]]:
    """(worse numbers, unmeasured numbers) of one variant's picture distances against the bases'."""
    worse, missing = [], []
    for k in PIC_KEYS:
        b = [x["pic"].get(k) for x in bases]
        val = v["pic"].get(k)
        if val is None or any(x is None for x in b):
            missing.append(k)
            continue
        spread = max(b) - min(b)
        if val > max(b) + spread:
            worse.append(f"{k} {val:.4g} (bases {', '.join(f'{x:.4g}' for x in b)}, spread {spread:.4g})")
    return worse, missing


def judge(rows: list[dict]) -> list[dict]:
    """Pure verdicts. Each row: variant, frameMs, display, amp, error, pic {PIC_KEYS: distance or None}."""
    bases = [r for r in rows if is_base(r["variant"]) and r.get("frameMs") is not None and not r.get("error")]
    out = []
    if len(bases) < 2:
        for r in rows:
            out.append(dict(r, verdict="NOT MEASURED", why=f"{len(bases)} measured base launches, 2 needed"))
        return out
    b0 = bases[0]
    base_bad = [b["variant"] for b in bases if b["display"] == display_state.UNKNOWN or b["amp"] not in COMPARE_OK
                or (b["display"], b["amp"]) != (b0["display"], b0["amp"])]
    base_ms = [b["frameMs"] for b in bases]
    for r in rows:
        if r.get("error") or r.get("frameMs") is None:
            out.append(dict(r, verdict="NOT MEASURED", why=r.get("error") or "no frame time"))
            continue
        if base_bad:
            out.append(dict(r, verdict="NOT COMPARABLE", why="bases not in one known display state with the Amp "
                            f"window minimized: {', '.join(base_bad)}"))
            continue
        if (r["display"], r["amp"]) != (b0["display"], b0["amp"]):
            out.append(dict(r, verdict="NOT COMPARABLE", why=f"display {r['display']}, Amp window {r['amp']} against "
                            f"the bases' {b0['display']}, {b0['amp']}"))
            continue
        fv, floor = frame_verdict(r["frameMs"], base_ms)
        worse, missing = picture_worse(r, bases)
        if is_base(r["variant"]):
            verdict = "BASE"
        elif fv != "FASTER":
            verdict = fv
        elif worse:
            verdict = "FASTER, PICTURE WORSE"
        elif missing:
            verdict = "FASTER, PICTURE NOT MEASURED"
        else:
            verdict = "FASTER"
        why = (f"{r['frameMs']} ms against bases {', '.join(str(m) for m in base_ms)}, floor {floor:.3f} ms"
               + (f"; worse: {'; '.join(worse)}" if worse else "") + (f"; not measured: {', '.join(missing)}" if missing else ""))
        out.append(dict(r, verdict=verdict, why=why, floorMs=round(floor, 3)))
    return out


def brightness(folder: str, raster: str, still: str) -> dict:
    final = os.path.join(folder, "gate", f"{still}_final.pfm")
    if not (os.path.exists(final) and os.path.exists(raster)):
        return {}
    import pfm_brightness
    c = pfm_brightness.compare_final_to_raster(gate.read_pfm(final), gate.read_pfm(raster))
    ratio = c.get("gamma_mean_ratio")
    return {"brightRatio": ratio, "brightLogRatio": abs(math.log(ratio)) if ratio else None,
            "nearBlackDiff": abs(c["near_black_delta"]), "nearWhiteDiff": abs(c["near_white_delta"]),
            "finalNearBlack": c["final_near_black"], "finalNearWhite": c["final_near_white"]}


def flicker(folder: str, spot: dict) -> dict:
    w = next((w for w in spot.get("windows", []) if w.get("name") == settings_report.WINDOW), None)
    p = os.path.join(folder, w["frames"]) if w and w.get("frames") else None
    if not p or not os.path.exists(p):
        return {}
    frames = gate.read_gray(p, *w["frameSize"])
    x0, y0, x1, y1 = gate.overlay_mask.box_at(*w["frameSize"])
    frames[:, y0:y1 + 1, x0:x1 + 1] = 0
    if len(gate.dedupe(frames)) < gate.THRESHOLDS["still_frames"]:
        return {}
    bj, ld, bp, bs4 = gate.still_flicker(frames)
    return {"flickerJitter": bj, "flickerDrift": ld, "flickerPulse": bp, "flickerStep4": bs4}


def blobs(folder: str, orig_spot: str, orig_still: str) -> dict:
    r = subprocess.run([sys.executable, "-I", GLOW_BLOBS, "--remix-spot", folder, "--orig-spot", orig_spot,
                        "--still", settings_report.WINDOW, "--orig-still", orig_still],
                       capture_output=True, text=True, timeout=600)
    m = re.search(r"glow_blobs: (?:PASS|FAIL) \(orbs unmatched (\d+), lost glows (\d+); puffs (\d+)", r.stdout)
    if not m:
        last = (r.stdout.strip().splitlines() or r.stderr.strip().splitlines() or ["no output"])[-1]
        return {"blobsNote": last[:200]}
    return {"orbsUnmatched": int(m.group(1)), "lostGlows": int(m.group(2)), "puffs": int(m.group(3))}


def lag(spot: dict, frame_ms: float | None) -> dict:
    ml = spot.get("mouse_lag")
    trials = ml.get("trials") if isinstance(ml, dict) else None
    if not trials:
        return {"lag": "NOT RECORDED"}
    g = [t["gameMs"] for t in trials if isinstance(t, dict) and isinstance(t.get("gameMs"), (int, float))]
    short = gate.short_lag_trials(spot, frame_ms) or []
    med = round(float(np.median(g)), 1) if g else None
    state = "NOT MEASURED" if len(short) >= gate.LAG_SHORT_MAX or med is None else f"{med} ms"
    return {"lag": state, "lagMedianMs": med, "lagTurns": len(g), "lagShort": len(short)}


def collect(run: str, reference: str, spot_name: str, orig_still: str) -> list[dict]:
    srows, _ = settings_report.report(run)
    orig_spot = os.path.join(reference, spot_name)
    raster = os.path.join(orig_spot, "gate", f"{orig_still}_raster.pfm")
    rows = []
    for s in srows:
        folder = os.path.join(run, s["variant"])
        sp = os.path.join(folder, "spot.json")
        spot = gate.load_json(sp) if os.path.exists(sp) else {}
        err = s.get("error") or ("rtx keys not loaded as asked" if s.get("rtxLoaded") is False else None) \
            or ("proxy keys not loaded as asked" if s.get("proxyLoaded") is False else None)
        pic: dict = {}
        if not err:
            pic.update(brightness(folder, raster, "y0_p0"))
            pic.update(flicker(folder, spot))
            pic.update(blobs(folder, orig_spot, orig_still))
        rows.append({"variant": s["variant"], "change": s.get("change"), "frameMs": s.get("frameMs"),
                     "frameMsP95": s.get("frameMsP95"), "gpuTimeMs": s.get("gpuTimeMs"),
                     "display": s.get("displayState"), "amp": display_state.amp_launch_state(folder)[0],
                     "error": err, "pic": pic, **lag(spot, s.get("frameMs"))})
    return rows


def table(out: list[dict], run: str, reference: str) -> str:
    f = lambda v, d=3: "" if v is None else (f"{v:.{d}f}" if isinstance(v, float) else str(v))
    lines = [f"# Sweep score {os.path.basename(os.path.normpath(run))}", "",
             f"One launch per variant. Original: {reference}. Distances are from the original; flicker is the Remix "
             "bench still's own (0-255 luma steps).", "",
             "| variant | change | verdict | frame ms | p95 ms | GPU ms | mean ratio | near-black | near-white | "
             "jitter | drift | pulse | step4 | orbs | lost | puffs | mouse_lag | short turns | display | Amp window |",
             "|" + "---|" * 20]
    for r in out:
        p = r["pic"]
        lines.append("| " + " | ".join([r["variant"], r.get("change") or "", r["verdict"], f(r.get("frameMs"), 2),
                     f(r.get("frameMsP95"), 2), f(r.get("gpuTimeMs"), 2), f(p.get("brightRatio")),
                     f(p.get("finalNearBlack")), f(p.get("finalNearWhite")), f(p.get("flickerJitter"), 2),
                     f(p.get("flickerDrift"), 2), f(p.get("flickerPulse"), 2), f(p.get("flickerStep4"), 2),
                     f(p.get("orbsUnmatched")), f(p.get("lostGlows")), f(p.get("puffs")), r.get("lag", ""),
                     f(r.get("lagShort")), r.get("display") or "", r.get("amp") or ""]) + " |")
    lines += ["", "Why:"] + [f"- {r['variant']}: {r['verdict']}, {r['why']}" for r in out]
    notes = [f"- {r['variant']}: glow_blobs {r['pic']['blobsNote']}" for r in out if r["pic"].get("blobsNote")]
    return "\n".join(lines + (["", "Notes:"] + notes if notes else [])) + "\n"


def self_test() -> int:
    good = {k: 1.0 for k in PIC_KEYS}
    row = lambda name, ms, **kw: {"variant": name, "frameMs": ms, "display": "plug on", "amp": display_state.AMP_MIN,
                                  "error": None, "pic": dict(good, **kw.pop("pic", {})), **kw}
    cases = []

    def check(name, got, want):
        cases.append((name, got == want, got, want))

    v = lambda rows, name: next(r for r in judge(rows) if r["variant"] == name)["verdict"]
    b = [row("01_base", 40.0), row("08_base", 40.3)]
    check("0.7 ms below both bases with a 0.3 ms base spread is FASTER (floor 0.626)", v(b + [row("02_x", 39.3)], "02_x"), "FASTER")
    check("0.5 ms below the faster base is NOT FASTER", v(b + [row("02_x", 39.5)], "02_x"), "NOT FASTER")
    check("1.0 ms above the slower base is SLOWER", v(b + [row("02_x", 41.3)], "02_x"), "SLOWER")
    wide = [row("01_base", 40.0), row("08_base", 41.0)]
    check("0.8 ms below both bases is NOT FASTER when the bases differ by 1.0 ms", v(wide + [row("02_x", 39.2)], "02_x"), "NOT FASTER")
    check("a faster variant with one more unmatched orb than both equal bases is PICTURE WORSE",
          v(b + [row("02_x", 38.0, pic={"orbsUnmatched": 2.0})], "02_x"), "FASTER, PICTURE WORSE")
    pb = [row("01_base", 40.0, pic={"flickerJitter": 1.0}), row("08_base", 40.3, pic={"flickerJitter": 1.4})]
    check("flicker within the worse base plus the bases' spread is not worse",
          v(pb + [row("02_x", 38.0, pic={"flickerJitter": 1.75})], "02_x"), "FASTER")
    check("flicker beyond the worse base plus the bases' spread is worse",
          v(pb + [row("02_x", 38.0, pic={"flickerJitter": 1.85})], "02_x"), "FASTER, PICTURE WORSE")
    check("a faster variant with no blob count reads PICTURE NOT MEASURED",
          v(b + [row("02_x", 38.0, pic={"orbsUnmatched": None})], "02_x"), "FASTER, PICTURE NOT MEASURED")
    check("a variant with the Amp window drawing is NOT COMPARABLE",
          v(b + [row("02_x", 30.0, amp=display_state.AMP_DRAWING)], "02_x"), "NOT COMPARABLE")
    check("a variant in another display state is NOT COMPARABLE",
          v(b + [row("02_x", 30.0, display="plug off")], "02_x"), "NOT COMPARABLE")
    check("bases with the Amp window not recorded refuse every variant",
          v([dict(x, amp=display_state.AMP_NOT_RECORDED) for x in b] + [row("02_x", 30.0, amp=display_state.AMP_NOT_RECORDED)], "02_x"),
          "NOT COMPARABLE")
    check("one measured base reads NOT MEASURED", v([b[0], row("08_base", None), row("02_x", 30.0)], "02_x"), "NOT MEASURED")
    check("a variant with an error reads NOT MEASURED", v(b + [row("02_x", 30.0, error="died")], "02_x"), "NOT MEASURED")
    # the frame rule can fail: with the floor removed the 0.5 ms case would read FASTER
    global FLOOR_MIN_MS
    keep, FLOOR_MIN_MS = FLOOR_MIN_MS, 0.0
    check("without the 0.626 ms floor the 0.5 ms case would read FASTER (the floor is what refuses it)",
          v(b + [row("02_x", 39.5)], "02_x"), "FASTER")
    FLOOR_MIN_MS = keep
    for name, ok, got, want in cases:
        print(f"{'ok  ' if ok else 'FAIL'} {name}: {got}" + ("" if ok else f" (wanted {want})"))
    bad = sum(not ok for _, ok, _, _ in cases)
    print(f"sweep_score self-test: {len(cases) - bad} of {len(cases)}")
    return 1 if bad else 0


def main() -> int:
    if sys.argv[1:] == ["--self-test"]:
        return self_test()
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--reference", required=True)
    ap.add_argument("--spot", default="con_custodial")
    ap.add_argument("--orig-still", default="y0_p0")
    a = ap.parse_args()
    out = judge(collect(a.run, a.reference, a.spot, a.orig_still))
    md = table(out, a.run, a.reference)
    gate.save_json(os.path.join(a.run, "sweep_score.json"), out)
    gate.save_text(os.path.join(a.run, "sweep_score.md"), md)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
