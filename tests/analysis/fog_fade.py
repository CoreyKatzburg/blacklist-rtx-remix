"""Custodial light-switch fade against Remix's fog history length (rtx.volumetrics.maxAccumulationFrames), worker B.

Remix's volume integrator (src/dxvk-remix/submodules/rtxdi/rtxdi-sdk/include/volumetrics/rtx/algorithm/
volume_integrator.slangh:126-139) blends each froxel's history with weight 1 / (age + 5), age capped at
maxAccumulationFrames N. Once the age sits at N, fog lit by a light that went out decays by (1 - 1/(N+5)) per game
frame: time constant tauFrames(N) = -1 / ln(1 - 1/(N+5)) game frames (132.5 at 128, 68.5 at 64, 36.5 at 32, 20.5 at
16). In seconds that is divided by the frame rate, so the same N fades 3.3x faster at 90 FPS than at 27.

Per variant folder of a verify_runtime.ps1 -Variants run (tests/runtime/variants_con_custodial_fog.json):
  N:       variant.json rtx["rtx.volumetrics.maxAccumulationFrames"] (rtxLoaded false -> not measured).
  control: variant.json rtx["rtx.volumetrics.enable"] == "False": fog off at the same switch.
  fade:    frames/light_switch.gray (320x180 u8 luma, 30 per second), mean luma per frame, split into
             press   the first frame 10% of the way from the first frame's luma to the last second's,
             pre     mean luma before the press,
             step    pre minus the luma 0.5 s after the press (our lights stop 0.3 to 0.7 s after it),
             slow    an exponential floor + A exp(-t / tau) fitted from 0.5 s after the press to the end, at 0.5 s,
             floor   that fit's floor; total = pre - floor,
             unexplained = total - step - slow, and the fit's RMS error, both as shares of total,
             t90     seconds from the press until the luma stays within 10% of total above the floor.
           Game fps over the light_switch window from proxy_frames.csv (column 2 = ms), so tau is also in game frames.
  noise:   frames/bench.gray, the lit room at a still camera: per-pixel standard deviation over time, mean over pixels,
           in 0..255 luma steps. Fewer accumulation frames should make the fog noisier: this is the cost.

Two verdicts, kept apart (coordinator, Nov 16):
  CAUSE (does the slow part come from fog history?), in game frames, base = mean of the first and last N=128 variants:
    NOT MEASURED  a variant has no fade or its key did not load, there is no base and base_again, base_again's tau
                  differs from base's by more than 10%, or there is no fog-off control.
    FAIL          the fog-off control's t90 is above CONTROL_T90_S: the slow fade is there without fog.
    FAIL          tau at the smallest N is above half the base tau: the fade does not follow fog history.
    FAIL          any step is off its prediction base * tauFrames(N) / tauFrames(128) by more than 15%.
    PASS          otherwise.
  OUTCOME (--original <no-Remix spot folder>: is the fade as short as the original game's at the same switch?):
    each variant's t90 against the original's t90 + OUTCOME_MARGIN_S: FIXED or NOT FIXED; NOT MEASURED without it.
Usage: fog_fade.py <run folder> [--original <spot folder>]   |   fog_fade.py --self-test
"""
import json, math, pathlib, sys, tempfile
import numpy as np

W, H, CAP_FPS = 320, 180, 30.0
KEY, FOG_KEY = "rtx.volumetrics.maxAccumulationFrames", "rtx.volumetrics.enable"
BASE_N, BASE_AGAIN_TOL, STEP_TOL, HALF = 128, 0.10, 0.15, 0.5
# The control: with fog off only our own lights are left, and they stop publishing 0.3 to 0.7 s after the press
# (lightpub_trace.csv, run 072242), so their fade is about 0.5 s; twice that is the limit.
CONTROL_T90_S = 1.0
# The outcome: the original game's t90 plus our lights' stop delay (0.5 s).
OUTCOME_MARGIN_S = 0.5
STEP_AFTER_S = 0.5


def tau_frames(n: int) -> float:
    return -1.0 / math.log(1.0 - 1.0 / (n + 5))


def gray(path: pathlib.Path) -> np.ndarray:
    raw = np.fromfile(path, dtype=np.uint8)
    n = raw.size // (W * H)
    return raw[: n * W * H].reshape(n, H, W).astype(np.float64)


def fade_parts(luma: np.ndarray) -> dict | None:
    """Split a luma curve (0..1, CAP_FPS per second) into press, step, slow part and unexplained rest."""
    end = luma[-int(CAP_FPS):].mean()
    drop0 = luma[0] - end
    if drop0 <= 0.01:
        return None
    below = np.nonzero(luma < luma[0] - 0.10 * drop0)[0]
    if len(below) == 0 or below[0] == 0:
        return None
    i0 = int(below[0])
    pre = float(luma[:i0].mean())
    t = (np.arange(len(luma)) - i0) / CAP_FPS
    sel = t >= STEP_AFTER_S
    if sel.sum() < 30:
        return None
    # luma = floor + A exp(-t / tau), floor free: a slow fade has not reached its floor after 20 s, and taking the
    # last second as the floor shortens tau. For each tau on a log grid, floor and A are a linear least-squares fit.
    ts, ys = t[sel], luma[sel]
    best = None
    for tau in np.exp(np.linspace(np.log(0.02), np.log(60.0), 2000)):
        X = np.stack([np.ones_like(ts), np.exp(-ts / tau)], axis=1)
        coef, *_ = np.linalg.lstsq(X, ys, rcond=None)
        err = float(((X @ coef - ys) ** 2).sum())
        if coef[1] >= 0 and (best is None or err < best[0]):
            best = (err, tau, coef)
    if best is None:   # flat after the step (an instant cut): no slow part
        best = (float(((ys - ys.mean()) ** 2).sum()), 0.02, (float(ys.mean()), 0.0))
    err, tau, (floor, amp) = best
    total = pre - floor
    k = int(np.argmin(np.abs(t - STEP_AFTER_S)))
    at_step = float(luma[max(k - 1, 0): k + 2].mean())
    step = pre - at_step
    slow = float(amp * math.exp(-STEP_AFTER_S / tau))
    above = np.nonzero(luma[i0:] > floor + 0.10 * total)[0]
    t90 = (above[-1] + 1) / CAP_FPS if len(above) else 0.0
    return {"pressS": i0 / CAP_FPS, "pre": pre, "floor": float(floor), "total": total, "step": step, "slow": slow,
            "unexplained": total - step - slow, "fitRms": math.sqrt(err / len(ys)), "tauS": float(tau), "t90S": t90,
            "settled": bool(len(above) == 0 or above[-1] + 1 < len(luma) - i0)}


def fade(spot: pathlib.Path) -> dict | None:
    p = spot / "frames" / "light_switch.gray"
    if not p.exists():
        return None
    out = fade_parts(gray(p).mean(axis=(1, 2)) / 255.0)
    if out is None:
        return None
    sj = json.loads((spot / "spot.json").read_text())
    w = next((w for w in sj.get("windows", []) if w.get("name") == "light_switch"), None)
    csv = spot / "proxy_frames.csv"
    if w and w.get("proxy") and csv.exists():
        a, b = w["proxy"]
        ms = {}
        for line in csv.open():
            c = line.split(",", 2)
            if c[0].isdigit() and a <= int(c[0]) <= b:
                ms[int(c[0])] = float(c[1])
        if len(ms) > 10:
            lo, hi = min(ms), max(ms)
            out["fps"] = (hi - lo) / ((ms[hi] - ms[lo]) / 1000.0)
            out["tauFrames"] = out["tauS"] * out["fps"]
    return out


def noise(spot: pathlib.Path, still: str = "bench") -> float | None:
    p = spot / "frames" / f"{still}.gray"
    return float(gray(p).std(axis=0).mean()) if p.exists() else None


def variant(spot: pathlib.Path) -> dict:
    vj = spot / "variant.json"
    v = json.loads(vj.read_text()) if vj.exists() else {}
    rtx = v.get("rtx") or {}
    n = rtx.get(KEY)
    row = {"name": spot.name, "N": int(n) if n is not None else None, "loaded": v.get("rtxLoaded", False),
           "control": str(rtx.get(FOG_KEY, "")).lower() == "false", "noise": noise(spot)}
    row.update(fade(spot) or {})
    return row


def verdict(rows: list[dict]) -> tuple[str, str]:
    controls = [r for r in rows if r["control"]]
    sweep = [r for r in rows if not r["control"]]
    missing = [r["name"] for r in sweep if r["N"] is None or not r["loaded"] or "tauFrames" not in r]
    missing += [r["name"] for r in controls if not r["loaded"] or "t90S" not in r]
    if missing:
        return "NOT MEASURED", f"no fade, no frame rate or key not loaded in {', '.join(missing)}"
    if not controls:
        return "NOT MEASURED", "no fog-off control (rtx.volumetrics.enable=False)"
    bases = [r for r in sweep if r["N"] == BASE_N]
    if len(bases) < 2:
        return "NOT MEASURED", f"needs base and base_again at N={BASE_N}"
    b0, b1 = bases[0]["tauFrames"], bases[-1]["tauFrames"]
    if abs(b1 - b0) / b0 > BASE_AGAIN_TOL:
        return "NOT MEASURED", f"base_again tau {b1:.1f} frames is {abs(b1 - b0) / b0:.0%} off base {b0:.1f} (limit {BASE_AGAIN_TOL:.0%})"
    base = (b0 + b1) / 2
    slow_control = [r for r in controls if r["t90S"] > CONTROL_T90_S]
    if slow_control:
        r = slow_control[0]
        return "FAIL", (f"fog-off control {r['name']} still takes {r['t90S']:.2f} s to fade (limit {CONTROL_T90_S} s): "
                        "the slow fade is there without fog")
    steps = [r for r in sweep if r["N"] != BASE_N]
    if not steps:
        return "NOT MEASURED", "no step below the base"
    low = min(steps, key=lambda r: r["N"])
    if low["tauFrames"] > HALF * base:
        return "FAIL", (f"at N={low['N']} tau {low['tauFrames']:.1f} frames is above half the base {base:.1f}: "
                        "the fade does not follow fog history")
    off = []
    for r in steps:
        want = base * tau_frames(r["N"]) / tau_frames(BASE_N)
        if abs(r["tauFrames"] - want) / want > STEP_TOL:
            off.append(f"N={r['N']} tau {r['tauFrames']:.1f} vs {want:.1f}")
    if off:
        return "FAIL", f"off prediction by more than {STEP_TOL:.0%}: " + "; ".join(off)
    return "PASS", (f"fog-off control fades in {max(r['t90S'] for r in controls):.2f} s and every step is within "
                    f"{STEP_TOL:.0%} of base {base:.1f} frames x tauFrames(N)/tauFrames({BASE_N})")


def outcome(rows: list[dict], original: dict | None) -> list[tuple[str, str]]:
    if not original:
        return [(r["name"], "NOT MEASURED") for r in rows]
    limit = original["t90S"] + OUTCOME_MARGIN_S
    return [(r["name"], "NOT MEASURED" if "t90S" not in r else
             f"{'FIXED' if r['t90S'] <= limit else 'NOT FIXED'} ({r['t90S']:.2f} s against {limit:.2f} s)") for r in rows]


def describe(r: dict) -> str:
    if "t90S" not in r:
        return "no fade measured"
    tot = r["total"]
    s = (f"press at {r['pressS']:.2f} s; drop {255 * tot:.1f} luma steps = step {r['step'] / tot:.0%} + slow "
         f"{r['slow'] / tot:.0%} + unexplained {r['unexplained'] / tot:+.0%} (fit RMS {r['fitRms'] / tot:.1%}); "
         f"tau {r['tauS']:.2f} s; t90 {r['t90S']:.2f} s{'' if r['settled'] else ' (not settled in the capture)'}")
    if r["slow"] < 0.02 * tot:
        s += "; no slow part, so tau means nothing here"
    elif "tauFrames" in r:
        s += (f"; at {r['fps']:.1f} fps tau = {r['tauFrames']:.1f} game frames, which is {r['tauFrames'] / 27:.2f} s at "
              f"27 FPS and {r['tauFrames'] / 90:.2f} s at 90 FPS")
    return s


def report(run: pathlib.Path, original: pathlib.Path | None = None) -> tuple[tuple[str, str], list]:
    rows = [variant(d) for d in sorted(run.iterdir()) if (d / "spot.json").exists()]
    print("prediction, tau = -1/ln(1 - 1/(N+5)) game frames: "
          + ", ".join(f"N={n}: {tau_frames(n):.1f}" for n in (128, 64, 32, 16)))
    for r in rows:
        tag = "fog off (control)" if r["control"] else f"N={r['N']}"
        line = f"{r['name']}: {tag} loaded={r['loaded']}: {describe(r)}"
        if r["noise"] is not None:
            line += f"; still noise {r['noise']:.2f} luma steps"
        print(line)
    orig = fade(original) if original else None
    if original:
        print(f"original game {original}: {describe(orig) if orig else 'no fade measured'}")
    v = verdict(rows)
    print(f"CAUSE {v[0]}: {v[1]}")
    out = outcome(rows, orig)
    for name, o in out:
        print(f"OUTCOME {name}: {o}")
    return v, out


# ---- self-test: fake variant folders --------------------------------------------------------------------------

def fake_variant(root: pathlib.Path, name: str, n: int | None, tau_game_frames: float, fps: float = 26.0,
                 noise_steps: float = 2.0, loaded: bool = True, step_share: float = 0.0, fog_off: bool = False) -> None:
    d = root / name
    (d / "frames").mkdir(parents=True)
    t = np.arange(600) / CAP_FPS
    # 0.5 s lit at 128, then the light goes out: step_share of the 120-step drop at once, the rest decays to 8 with
    # the given time constant (in game frames)
    after = 8.0 + 120.0 * ((1 - step_share) * np.exp(-(t - 0.5) * fps / tau_game_frames))
    level = np.where(t < 0.5, 128.0, after)
    dither = (np.arange(W * H).reshape(H, W) % 97) / 97.0 - 0.5   # spread over pixels so the mean is not quantised
    frames = np.clip(np.round(level[:, None, None] + dither[None]), 0, 255).astype(np.uint8)
    frames.tofile(d / "frames" / "light_switch.gray")
    # still camera: every pixel alternates +-noise_steps around 100, so its standard deviation over time is exactly that
    bench = np.where((np.arange(150) % 2 == 0)[:, None, None], 100 + noise_steps, 100 - noise_steps)
    np.broadcast_to(bench, (150, H, W)).astype(np.uint8).tofile(d / "frames" / "bench.gray")
    a, b = 1000, 1000 + int(20 * fps)
    (d / "proxy_frames.csv").write_text("".join(f"{f},{(f - a) * 1000.0 / fps:.3f},0,0,0,0,\n" for f in range(a, b + 1)))
    (d / "spot.json").write_text(json.dumps({"windows": [{"name": "light_switch", "proxy": [a, b]}]}))
    rtx = {FOG_KEY: "False"} if fog_off else {KEY: str(n)}
    (d / "variant.json").write_text(json.dumps({"name": name, "rtx": rtx, "rtxLoaded": loaded}))


def self_test() -> int:
    failures = []

    def check(label, ok, detail=""):
        print(f"{'ok' if ok else 'WRONG'}: {label}{': ' + detail if detail else ''}")
        if not ok:
            failures.append(label)

    def run(label, variants, want, control=(3.0,), original=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "run"
            for i, (n, tau, *rest) in enumerate(variants):
                fake_variant(root, f"{i + 1:02d}_{'base' if n == BASE_N else f'acc{n}'}", n, tau, *rest)
            if control is not None:
                fake_variant(root, "06_fog_off", None, *control, fog_off=True)
            orig = None
            if original is not None:
                orig = pathlib.Path(tmp) / "orig"
                fake_variant(orig.parent, "orig", 128, *original)
            (got, why), out = report(root, orig)
        check(f"{label}: {got} (want {want})", got == want)
        return out

    follows = [(n, tau_frames(n)) for n in (128, 64, 32, 16, 128)]
    run("fade follows N+5, control fast", follows, "PASS")
    run("fade stays at the base tau for every N", [(n, tau_frames(128)) for n in (128, 64, 32, 16, 128)], "FAIL")
    run("base_again 15% off base", follows[:-1] + [(128, tau_frames(128) * 1.15)], "NOT MEASURED")
    run("key not loaded in one variant", follows[:2] + [(32, tau_frames(32), 26.0, 2.0, False)] + follows[3:],
        "NOT MEASURED")
    run("one step 50% slow", follows[:2] + [(32, tau_frames(32) * 1.5)] + follows[3:], "FAIL")
    # the band is 15% now: a step 22% slow passed the old 30% band and must fail
    run("one step 22% slow", follows[:2] + [(32, tau_frames(32) * 1.22)] + follows[3:], "FAIL")
    run("one step 8% slow", follows[:2] + [(32, tau_frames(32) * 1.08)] + follows[3:], "PASS")
    # the coincidence Corey asked about: the steps follow N+5, but with fog off the fade is still slow
    run("fade follows N+5 but the fog-off control is slow too", follows, "FAIL", control=(tau_frames(128),))
    run("no fog-off control", follows, "NOT MEASURED", control=None)
    # the fade is in game frames: the same N at a higher frame rate is shorter in seconds, not a different verdict
    run("same fades at 60 fps", [(n, tau_frames(n), 60.0) for n in (128, 64, 32, 16, 128)], "PASS")
    # a 20% step at the press with the same slow part: the split must find the step and still pass
    run("20% step at the press", [(n, tau_frames(n), 26.0, 2.0, True, 0.2) for n in (128, 64, 32, 16, 128)], "PASS")
    # outcome against an original that cuts at once (tau 1 game frame at 128 fps): N=16 at 26 fps is still too slow
    out = run("outcome", follows, "PASS", original=(1.0, 128.0))
    check("outcome: N=128 not fixed against an instant original", out[0][1].startswith("NOT FIXED"), out[0][1])
    check("outcome: fog-off control fixed against an instant original", out[-1][1].startswith("FIXED"), out[-1][1])
    with tempfile.TemporaryDirectory() as tmp:
        fake_variant(pathlib.Path(tmp), "x", 128, 100.0, noise_steps=3.0)
        got = noise(pathlib.Path(tmp) / "x")
        check("known noise 3.0 luma steps", abs(got - 3.0) < 1e-9, f"got {got}")
        f = fade(pathlib.Path(tmp) / "x")
        check("known tau 100 game frames at 26 fps", abs(f["tauFrames"] - 100.0) / 100.0 < 0.03, f"got {f['tauFrames']:.1f}")
        fake_variant(pathlib.Path(tmp), "y", 128, 100.0, step_share=0.3)
        f = fade(pathlib.Path(tmp) / "y")
        # 30% of the drop at once; the 0.5 s point already holds 0.5 s of the slow decay, so step = 30% + 70% x
        # (1 - exp(-0.5 x 26 / 100)) = 38.5%
        want_step = 0.3 + 0.7 * (1 - math.exp(-0.5 * 26 / 100))
        check("known step share", abs(f["step"] / f["total"] - want_step) < 0.02 and abs(f["unexplained"] / f["total"]) < 0.02,
              f"step {f['step'] / f['total']:.3f} (want {want_step:.3f}), unexplained {f['unexplained'] / f['total']:+.3f}")
    print("fog_fade self-test: " + ("all passed" if not failures else f"FAILED: {', '.join(failures)}"))
    return 1 if failures else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--self-test"]:
        sys.exit(self_test())
    if len(args) == 1:
        report(pathlib.Path(args[0]))
    elif len(args) == 3 and args[1] == "--original":
        report(pathlib.Path(args[0]), pathlib.Path(args[2]))
    else:
        sys.exit(__doc__)
