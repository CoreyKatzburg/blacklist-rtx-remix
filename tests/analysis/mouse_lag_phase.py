"""What splits the mouse_lag game share into two groups: every turn placed in its frame (worker C).

Measurement only. drive.py mouse_lag sends a 5 degree turn with SendInput at t0 (perf_counter ms) and records
gameMs = Present entry (proxy_frames.csv column 2, QPC ms, the same clock) of the first proxy frame whose slot-0 camera
forward vector points more than 0.5 degrees away from the last frame before t0 (drive.first_turned). spot.json keeps
gameMs but not t0.

proxy_frames.csv logs the forward vector with 4 decimals, so its length is not exactly 1, and drive.first_turned takes
acos of the plain dot product. For an unchanged vector v that reads acos(|v|^2), which is above 0.5 degrees when
|v|^2 < cos(0.5 deg) = 0.999962. This script calls that the still vector's self-angle.

Per spot it finds the real turns (frames more than 0.5 degrees from the frame before after normalising both vectors;
one per trial, in order) and, per trial, every frame D that drive.first_turned would have returned for gameMs, that is
every D where first_turned_raw(entry(D) - gameMs) == D. When D can only be the real turn, t0 = entry(real) - gameMs.
When several D fit, t0 is taken from the gate's send log (gate_sends_*.csv, GetTickCount ms, 15.6 ms steps) with the
tick-to-QPC offset measured on the unique turns, and the D whose t0 lies nearest is used; its residual is reported.

Per turn: selfAngle of the still vector, detected (D) == real turn, trueMs = entry(real) - t0, framesLate (frames
Presented after t0 before the real turn), sinceExit = t0 - Present exit of the last frame entered before t0
(< 0: inside Present), the frame gaps around t0.

Rule tested: a turn is in the low group (gameMs < 1 frame) exactly when selfAngle > 0.5 degrees.
Run: python -I mouse_lag_phase.py [--json out.json]   |   python -I mouse_lag_phase.py --self-test
Exit 0 = every turn matched and the rule held on every turn with both fakes failing it; 1 = otherwise; 2 = bad input.
"""
import glob
import json
import math
import os
import statistics as st
import sys

GATE = r"D:\BlacklistOverhaul\experiments\remix-gate"
RUNS = ["20261117-152548", "20261117-125334", "reference-20261117-133918"]
TURN_DEG = 0.5        # drive.first_turned's threshold
MARGIN = 60           # proxy frames read before and after the mouse_lag window
SETTLE_MS = 300.0     # one real turn per 300 ms at most (turns are about 1.4 s apart)
TICK_STEP_MS = 15.625  # GetTickCount resolution (the send log's clock)


def cam_fwd(rest):
    """Slot-0 camera forward vector of a proxy_frames.csv publish column, as drive.proxy_camera_dirs reads it."""
    for pub in rest.split("|"):
        p = pub.split(":")
        if len(p) >= 9 and p[2] == "0":
            return tuple(map(float, p[6:9]))
    return None


def load_frames(path, lo, hi):
    """[(frame, entry ms, gap ms, inPresent ms, fwd or None)] for frames lo..hi."""
    out = []
    with open(path, encoding="latin-1") as f:
        for ln in f:
            row = ln.rstrip("\r\n").split(",", 6)
            try:
                n = int(row[0])
            except (ValueError, IndexError):
                continue
            if n < lo:
                continue
            if n > hi:
                break
            out.append((n, float(row[1]), float(row[2]), float(row[3]), cam_fwd(row[6]) if len(row) > 6 else None))
    return out


def raw_angle(a, b):
    """drive.first_turned's angle: acos of the plain dot product."""
    return math.degrees(math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))))


def angle(a, b):
    """Angle between the directions of a and b (both normalised)."""
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(x * x for x in b))
    return raw_angle([x / na for x in a], [x / nb for x in b])


def first_turned_raw(frames, t0):
    """drive.first_turned on frame tuples: index of the first frame at or after t0 whose raw angle from the last
    frame before t0 exceeds TURN_DEG."""
    ref = None
    for f in frames:
        if f[1] < t0 and f[4]:
            ref = f[4]
    if ref is None:
        return None
    for i, f in enumerate(frames):
        if f[1] >= t0 and f[4] and raw_angle(ref, f[4]) > TURN_DEG:
            return i
    return None


def real_turns(frames, window=None):
    """Indices of frames whose direction is more than TURN_DEG from the frame before (normalised), at most one per
    SETTLE_MS, inside window (first, last proxy frame number; default all). The window's first frame is left out: it
    carries the move to the spot's pose (con_bright_hall 152548: 30.9 degrees at frame 6137)."""
    out, last = [], -1e18
    for i in range(1, len(frames)):
        if window and not window[0] < frames[i][0] <= window[1]:
            continue
        a, b = frames[i - 1][4], frames[i][4]
        if a and b and angle(a, b) > TURN_DEG:
            if frames[i][1] - last > SETTLE_MS:
                out.append(i)
            last = frames[i][1]
    return out


def place(frames, t0, r):
    """Where t0 falls: k = last frame entered at or before t0; framesLate = frames Presented after t0 before r."""
    k = max(i for i, f in enumerate(frames) if f[1] <= t0)
    n, entry, gap, inp, _ = frames[k]
    return {"t0": round(t0, 3), "trueMs": round(frames[r][1] - t0, 3), "framesLate": r - (k + 1),
            "sinceEntry": round(t0 - entry, 3), "sinceExit": round(t0 - (entry + inp), 3),
            "inPresentK": inp, "gapK1": frames[k + 1][2]}


def turns(frames, game_ms, ticks=(), window=None):
    """One record per trial. ticks: the gate's send log (GetTickCount ms) for turns whose t0 is ambiguous; window:
    the mouse_lag window's proxy frame range, where the turns are looked for."""
    reals = real_turns(frames, window)
    if len(reals) != len(game_ms) or any(g is None for g in game_ms):
        raise ValueError(f"{len(reals)} real turns in the window for {len(game_ms)} trials "
                         f"({sum(g is None for g in game_ms)} without gameMs)")
    out = []
    for g, r in zip(game_ms, reals):
        lo = r - 1
        while lo > 0 and frames[lo - 1][1] > frames[r][1] - 1000.0 and frames[lo][4] == frames[r - 1][4]:
            lo -= 1
        cands = [d for d in range(lo, r + 1) if first_turned_raw(frames, frames[d][1] - g) == d]
        rec = {"gameMs": g, "realFrame": frames[r][0], "selfAngle": round(raw_angle(frames[r - 1][4], frames[r - 1][4]), 4),
               "candidates": [frames[d][0] for d in cands]}
        if cands == [r]:
            rec.update(detectedIsReal=True, t0From="unique", t0Settled=True, **place(frames, frames[r][1] - g, r))
        else:
            rec.update(detectedIsReal=(r in cands) and len(cands) == 1, t0From="ambiguous" if cands else "none")
        rec["_c"], rec["_r"] = cands, r
        out.append(rec)
    # Ambiguous turns: t0 from the send log, offset measured on the unique turns.
    offs = []
    for rec in out:
        if rec["t0From"] == "unique" and ticks:
            near = min(ticks, key=lambda t: abs(t - rec["t0"] - (st.median(offs) if offs else 0)) if offs else
                       abs(t - rec["t0"]))
            offs.append(near - rec["t0"])
    off = st.median(offs) if offs else None
    for rec in out:
        if rec["t0From"] == "ambiguous" and off is not None:
            est = min(ticks, key=lambda t: abs(t - off - (frames[rec["_r"]][1] - 50.0))) - off
            dist = sorted((abs(frames[d][1] - rec["gameMs"] - est), d) for d in rec["_c"])
            d = dist[0][1]
            # GetTickCount steps 15.6 ms: the t0 is settled only when no other candidate lies within one step.
            rec.update(detectedFrame=frames[d][0], detectedIsReal=d == rec["_r"], t0From="sendlog",
                       sendLogResidualMs=round(frames[d][1] - rec["gameMs"] - est, 1),
                       t0Settled=len(dist) == 1 or dist[1][0] > TICK_STEP_MS,
                       **place(frames, frames[d][1] - rec["gameMs"], rec["_r"]))
        del rec["_c"], rec["_r"]
    return out, {"tickOffsetMs": off, "tickOffsetSpreadMs": (round(max(offs) - min(offs), 1) if offs else None)}


def read_ticks(folder):
    t = []
    for p in glob.glob(os.path.join(folder, "gate_sends_*.csv")):
        t += [int(x) for x in open(p, encoding="ascii", errors="replace").read().split() if x.isdigit()]
    return sorted(t)


def spot_turns(run, spot):
    folder = os.path.join(GATE, run, spot)
    s = json.load(open(os.path.join(folder, "spot.json"), encoding="utf-8"))
    w = [x for x in s.get("windows", []) if x.get("name") == "mouse_lag"]
    trials = (s.get("mouse_lag") or {}).get("trials") or []
    if not w or not trials:
        return None
    a, b = w[0]["proxy"]
    frames = load_frames(os.path.join(folder, "proxy_frames.csv"), a - MARGIN, b + MARGIN)
    return turns(frames, [t.get("gameMs") for t in trials], read_ticks(folder), (a, b))


def rule_low(rec):
    return rec["selfAngle"] > TURN_DEG


def rule_hits(recs, frame_ms):
    """(turns where 'gameMs below one frame' agrees with the self-angle rule, turns)."""
    return sum((r["gameMs"] < frame_ms) == rule_low(r) for r in recs), len(recs)


def main(json_out=None):
    res, ok = {}, True
    for run in RUNS:
        for spot in sorted(os.listdir(os.path.join(GATE, run))):
            if not os.path.exists(os.path.join(GATE, run, spot, "spot.json")):
                continue
            try:
                got = spot_turns(run, spot)
            except ValueError as e:
                print(f"\n== {run} {spot}: NOT MATCHED: {e}")
                ok = False
                continue
            if got is None:
                continue
            recs, cal = got
            frame_ms = st.median(r["gapK1"] for r in recs if "gapK1" in r)
            hits, n = rule_hits(recs, frame_ms)
            # Fake: the same gameMs list moved by one turn must break the rule somewhere.
            fake = [dict(r, gameMs=recs[(i + 1) % len(recs)]["gameMs"]) for i, r in enumerate(recs)]
            fhits, _ = rule_hits(fake, frame_ms)
            # Remix runs must hold on every turn; the reference is reported (its frames are 5 ms, hitches reach 160).
            ok &= fhits < n and (hits == n or run.startswith("reference"))
            settled = [r for r in recs if r.get("t0Settled")]
            late = {k: sum(r["framesLate"] == k for r in settled) for k in sorted({r["framesLate"] for r in settled})}
            two = [r["sinceExit"] for r in settled if r["framesLate"] == 2]
            three = [r["sinceExit"] for r in settled if r["framesLate"] == 3]
            summ = {"frameMs": round(frame_ms, 2), "ruleHits": hits, "fakeShiftHits": fhits, "low": sum(map(rule_low, recs)),
                    "gameMsMedian": st.median(r["gameMs"] for r in recs),
                    "trueMsMedian": st.median(r["trueMs"] for r in settled) if settled else None,
                    "trueMsRange": [min(r["trueMs"] for r in settled), max(r["trueMs"] for r in settled)] if settled else None,
                    "settled": len(settled), "framesLate": late,
                    "sinceExitLate2Max": max(two) if two else None, "sinceExitLate3Min": min(three) if three else None}
            res[f"{run}/{spot}"] = {"summary": summ, "turns": recs, **cal}
            print(f"\n== {run} {spot}: median frame {frame_ms:.2f} ms; rule holds on {hits} of {n}; "
                  f"fake (gameMs moved by one turn) {fhits} of {n}; tick offset spread {cal['tickOffsetSpreadMs']} ms")
            print(f"   summary {json.dumps(summ)}")
            print(f"{'gameMs':>7} {'self°':>6} {'real?':>5} {'t0 from':>9} {'trueMs':>7} {'late':>4} {'sinceExit':>9} "
                  f"{'gap(k+1)':>8} cands")
            for r in recs:
                print(f"{r['gameMs']:7.1f} {r['selfAngle']:6.3f} {str(r['detectedIsReal']):>5} {r['t0From']:>9} "
                      f"{r.get('trueMs', float('nan')):7.1f} {r.get('framesLate', -1):4d} "
                      f"{r.get('sinceExit', float('nan')):9.2f} {r.get('gapK1', float('nan')):8.2f} "
                      f"{len(r['candidates'])}{'' if 'sendLogResidualMs' not in r else ' res ' + str(r['sendLogResidualMs'])}")
    if json_out:
        json.dump(res, open(json_out, "w"), indent=1)
    return 0 if ok else 1


def self_test():
    cases = []

    def case(name, got, want):
        cases.append(got == want)
        print(f"{'ok  ' if got == want else 'FAIL'} {name}: {got}")

    # A still forward vector rounded to 4 decimals whose length^2 is 0.99996 or less reads > 0.5 degrees from itself.
    v_bad, v_good = (0.9997, -0.0196, 0.0), (0.9998, -0.0196, 0.0)
    case("self-angle of a short 4-decimal vector is above 0.5 (0.9997,-0.0196,0)", raw_angle(v_bad, v_bad) > 0.5, True)
    case("self-angle of a near-unit vector is below 0.5", raw_angle(v_good, v_good) < 0.5, True)
    case("normalised angle of a vector with itself is 0", round(angle(v_bad, v_bad), 6), 0.0)

    def fr(vec_still, turn_at=6, n=12, dt=33.0, inp=28.0):
        turned = (math.cos(math.radians(1.5)) * vec_still[0], math.sin(math.radians(1.5)), 0.0)
        return [(i, 1000.0 + i * dt, dt, inp, vec_still if i < turn_at else turned) for i in range(n)]

    # Still vector near unit: only the real turn (frame 6, entry 1198) fits gameMs 70 -> t0 1128, inside frame 3's
    # CPU time (exit of frame 3 = 1099 + 28 = 1127), turn 2 frames after the next Present.
    f = fr(v_good)
    recs, _ = turns(f, [70.0])
    r = recs[0]
    case("near-unit still vector: detected frame is the real turn", (r["detectedIsReal"], r["t0From"]), (True, "unique"))
    case("t0 = entry(real) - gameMs", r["t0"], 1198.0 - 70.0)
    case("framesLate counts frames Presented after t0 before the turn (4, 5)", r["framesLate"], 2)
    case("sinceExit = t0 - (entry(k) + inPresent(k))", r["sinceExit"], 1128.0 - (1099.0 + 28.0))
    # Short still vector: gameMs 10 fits every still frame from the turn back, so gameMs does not identify the turn.
    f = fr(v_bad)
    recs, _ = turns(f, [10.0])
    case("short still vector: several frames fit gameMs 10 (false detection possible)",
         len(recs[0]["candidates"]) > 1 and recs[0]["t0From"] == "ambiguous", True)
    case("rule: low group predicted for the short vector", rule_low(recs[0]), True)
    # Rule check and its fake: two turns, low and high, labelled right and swapped.
    recs = [{"gameMs": 10.0, "selfAngle": 0.7}, {"gameMs": 80.0, "selfAngle": 0.3}]
    case("rule holds on correct labels", rule_hits(recs, 33.0), (2, 2))
    case("fake: swapped gameMs breaks the rule", rule_hits([dict(recs[0], gameMs=80.0), dict(recs[1], gameMs=10.0)],
                                                           33.0), (0, 2))
    # Send-log t0: one unique turn calibrates the tick offset, an ambiguous one picks the candidate nearest its tick.
    # Frames 0-5 near-unit, 6-39 a 2 degree turn whose rounded vector is short (self-angle about 1.1), 40- a real turn.
    v_short2 = (0.9993, 0.0349, 0.0)
    f2 = [(i, 1000.0 + i * 33.0, 33.0, 28.0, v_good if i < 6 else (v_short2 if i < 40 else (0.99, 0.13, 0.0)))
          for i in range(60)]
    recs, cal = turns(f2, [70.0, 10.0], ticks=[1128 + 5000, 1000.0 + 40 * 33.0 - 10.0 + 5000 + 3])
    case("send log: offset from the unique turn", cal["tickOffsetMs"], 5000.0)
    case("send log: ambiguous turn takes the candidate nearest its tick (real frame 40)",
         (recs[1]["t0From"], recs[1]["detectedIsReal"], recs[1]["sendLogResidualMs"]), ("sendlog", True, -3.0))
    print("mouse_lag_phase self-test:", "PASS" if all(cases) else "FAIL")
    return 0 if all(cases) else 1


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--self-test"]:
        sys.exit(self_test())
    try:
        sys.exit(main(a[1] if a[:1] == ["--json"] and len(a) > 1 else None))
    except (OSError, KeyError) as e:
        print("bad input:", e)
        sys.exit(2)
