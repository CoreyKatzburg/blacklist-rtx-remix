"""Custodial light-switch fade against Remix's fog history length (rtx.volumetrics.maxAccumulationFrames), worker B.

Remix's volume integrator (src/dxvk-remix/submodules/rtxdi/rtxdi-sdk/include/volumetrics/rtx/algorithm/
volume_integrator.slangh:126-139) blends each froxel's history with weight 1 / (age + 5), age capped at
maxAccumulationFrames N. Once the age sits at N, fog lit by a light that went out decays by (1 - 1/(N+5)) per game
frame: time constant tauFrames(N) = -1 / ln(1 - 1/(N+5)) game frames (132.5 at 128, 68.5 at 64, 36.5 at 32, 20.5 at
16). In seconds that is divided by the frame rate, so the same N fades 3.3x faster at 90 FPS than at 27.

Per variant folder of a verify_runtime.ps1 -Variants run (tests/runtime/variants_con_custodial_fog.json):
  N:     variant.json rtx["rtx.volumetrics.maxAccumulationFrames"] (rtxLoaded false -> not measured).
  fade:  frames/light_switch.gray (320x180 u8 luma, 30 per second): exp fit of mean luma minus its last second,
         1 to 15 s after the capture starts; game fps over the light_switch window from proxy_frames.csv (column 2 =
         ms), so tau is also in game frames.
  noise: frames/bench.gray, the lit room at a still camera: per-pixel standard deviation over time, mean over pixels,
         in 0..255 luma steps. Fewer accumulation frames should make the fog noisier: this is the cost.
Verdict (pass numbers named before the run, coordinator Nov 16), in game frames, base = mean of the first and last
N=128 variants:
  NOT MEASURED  a variant has no fade or its key did not load, there is no base and base_again, or base_again's tau
                differs from base's by more than 10%.
  FAIL          tau at the smallest N is above half the base tau: the fade does not follow fog history.
  FAIL          any step is off its prediction base * tauFrames(N) / tauFrames(128) by more than 30%.
  PASS          otherwise.
Usage: fog_fade.py <run folder>   |   fog_fade.py --self-test
"""
import json, math, pathlib, sys, tempfile
import numpy as np

W, H, CAP_FPS = 320, 180, 30.0
KEY = "rtx.volumetrics.maxAccumulationFrames"
BASE_N, BASE_AGAIN_TOL, STEP_TOL, HALF = 128, 0.10, 0.30, 0.5


def tau_frames(n: int) -> float:
    return -1.0 / math.log(1.0 - 1.0 / (n + 5))


def gray(path: pathlib.Path) -> np.ndarray:
    raw = np.fromfile(path, dtype=np.uint8)
    n = raw.size // (W * H)
    return raw[: n * W * H].reshape(n, H, W).astype(np.float64)


def fade(spot: pathlib.Path) -> dict | None:
    p = spot / "frames" / "light_switch.gray"
    if not p.exists():
        return None
    luma = gray(p).mean(axis=(1, 2)) / 255.0
    t = np.arange(len(luma)) / CAP_FPS
    sel = t >= 1.0
    if sel.sum() < 30:
        return None
    # luma = final + A exp(-t / tau), with final free: a slow fade has not reached its floor after 20 s, and taking
    # the last second as the floor shortens tau (an ideal 132.5-frame fade at 26 fps read as 115 frames that way).
    # For each tau on a log grid, final and A are a linear least-squares fit; keep the tau with the least residual.
    ts, ys = t[sel], luma[sel]
    best = None
    for tau in np.exp(np.linspace(np.log(0.1), np.log(60.0), 2000)):
        X = np.stack([np.ones_like(ts), np.exp(-ts / tau)], axis=1)
        coef, res, *_ = np.linalg.lstsq(X, ys, rcond=None)
        err = float(((X @ coef - ys) ** 2).sum())
        if coef[1] > 0 and (best is None or err < best[0]):
            best = (err, tau, coef)
    if best is None:
        return None
    out = {"tauS": float(best[1]), "lumaStart": float(luma[:10].mean()), "lumaFinal": float(best[2][0])}
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
    n = (v.get("rtx") or {}).get(KEY)
    row = {"name": spot.name, "N": int(n) if n is not None else None, "loaded": v.get("rtxLoaded", False),
           "noise": noise(spot)}
    row.update(fade(spot) or {})
    return row


def verdict(rows: list[dict]) -> tuple[str, str]:
    missing = [r["name"] for r in rows if r["N"] is None or not r["loaded"] or "tauFrames" not in r]
    if missing:
        return "NOT MEASURED", f"no fade, no frame rate or key not loaded in {', '.join(missing)}"
    bases = [r for r in rows if r["N"] == BASE_N]
    if len(bases) < 2:
        return "NOT MEASURED", f"needs base and base_again at N={BASE_N}"
    b0, b1 = bases[0]["tauFrames"], bases[-1]["tauFrames"]
    if abs(b1 - b0) / b0 > BASE_AGAIN_TOL:
        return "NOT MEASURED", f"base_again tau {b1:.1f} frames is {abs(b1 - b0) / b0:.0%} off base {b0:.1f} (limit {BASE_AGAIN_TOL:.0%})"
    base = (b0 + b1) / 2
    steps = [r for r in rows if r["N"] != BASE_N]
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
        return "FAIL", "off prediction by more than 30%: " + "; ".join(off)
    return "PASS", f"every step within 30% of base {base:.1f} frames x tauFrames(N)/tauFrames({BASE_N})"


def report(run: pathlib.Path) -> tuple[str, str]:
    rows = [variant(d) for d in sorted(run.iterdir()) if (d / "spot.json").exists()]
    print("prediction, tau = -1/ln(1 - 1/(N+5)) game frames: "
          + ", ".join(f"N={n}: {tau_frames(n):.1f}" for n in (128, 64, 32, 16)))
    for r in rows:
        line = f"{r['name']}: N={r['N']} loaded={r['loaded']}"
        if "tauS" in r:
            line += f" tau {r['tauS']:.2f} s"
        if "tauFrames" in r:
            line += (f" at {r['fps']:.1f} fps = {r['tauFrames']:.1f} game frames; fade to 5% (3 tau) would take "
                     f"{3 * r['tauFrames'] / 27:.1f} s at 27 FPS, {3 * r['tauFrames'] / 90:.1f} s at 90 FPS")
        if r["noise"] is not None:
            line += f"; still noise {r['noise']:.2f} luma steps"
        print(line)
    v = verdict(rows)
    print(f"VERDICT {v[0]}: {v[1]}")
    return v


# ---- self-test: fake variant folders --------------------------------------------------------------------------

def fake_variant(root: pathlib.Path, name: str, n: int, tau_game_frames: float, fps: float = 26.0,
                 noise_steps: float = 2.0, loaded: bool = True) -> None:
    d = root / name
    (d / "frames").mkdir(parents=True)
    t = np.arange(600) / CAP_FPS
    # 0.5 s lit, then the light goes out and the luma decays to 8 with the given time constant
    level = np.where(t < 0.5, 128.0, 8.0 + 120.0 * np.exp(-(t - 0.5) * fps / tau_game_frames))
    dither = (np.arange(W * H).reshape(H, W) % 97) / 97.0 - 0.5   # spread over pixels so the mean is not quantised
    frames = np.clip(np.round(level[:, None, None] + dither[None]), 0, 255).astype(np.uint8)
    frames.tofile(d / "frames" / "light_switch.gray")
    # still camera: every pixel alternates +-noise_steps around 100, so its standard deviation over time is exactly that
    bench = np.where((np.arange(150) % 2 == 0)[:, None, None], 100 + noise_steps, 100 - noise_steps)
    np.broadcast_to(bench, (150, H, W)).astype(np.uint8).tofile(d / "frames" / "bench.gray")
    a, b = 1000, 1000 + int(20 * fps)
    (d / "proxy_frames.csv").write_text("".join(f"{f},{(f - a) * 1000.0 / fps:.3f},0,0,0,0,\n" for f in range(a, b + 1)))
    (d / "spot.json").write_text(json.dumps({"windows": [{"name": "light_switch", "proxy": [a, b]}]}))
    (d / "variant.json").write_text(json.dumps({"name": name, "rtx": {KEY: str(n)}, "rtxLoaded": loaded}))


def self_test() -> int:
    failures = []

    def run(label, variants, want):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for i, (n, tau, *rest) in enumerate(variants):
                fake_variant(root, f"{i + 1:02d}_{'base' if n == BASE_N else f'acc{n}'}", n, tau, *rest)
            got, why = report(root)
        ok = got == want
        print(f"{'ok' if ok else 'WRONG'}: {label}: {got} (want {want})")
        if not ok:
            failures.append(label)

    follows = [(n, tau_frames(n)) for n in (128, 64, 32, 16, 128)]
    run("fade follows N+5", follows, "PASS")
    run("fade stays at the base tau for every N", [(n, tau_frames(128)) for n in (128, 64, 32, 16, 128)], "FAIL")
    run("base_again 15% off base", follows[:-1] + [(128, tau_frames(128) * 1.15)], "NOT MEASURED")
    run("key not loaded in one variant", follows[:2] + [(32, tau_frames(32), 26.0, 2.0, False)] + follows[3:],
        "NOT MEASURED")
    run("one step 50% slow", follows[:2] + [(32, tau_frames(32) * 1.5)] + follows[3:], "FAIL")
    # the fade is in game frames: the same N at a higher frame rate is shorter in seconds, not a different verdict
    run("same fades at 60 fps", [(n, tau_frames(n), 60.0) for n in (128, 64, 32, 16, 128)], "PASS")
    with tempfile.TemporaryDirectory() as tmp:
        fake_variant(pathlib.Path(tmp), "x", 128, 100.0, noise_steps=3.0)
        got = noise(pathlib.Path(tmp) / "x")
        ok = abs(got - 3.0) < 1e-9
        print(f"{'ok' if ok else 'WRONG'}: known noise 3.0 luma steps: got {got}")
        if not ok:
            failures.append("noise")
        # and the fit recovers a known time constant in game frames
        f = fade(pathlib.Path(tmp) / "x")
        ok = abs(f["tauFrames"] - 100.0) / 100.0 < 0.03
        print(f"{'ok' if ok else 'WRONG'}: known tau 100 game frames at 26 fps: got {f['tauFrames']:.1f}")
        if not ok:
            failures.append("tau")
    print("fog_fade self-test: " + ("all passed" if not failures else f"FAILED: {', '.join(failures)}"))
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    report(pathlib.Path(sys.argv[1]))
