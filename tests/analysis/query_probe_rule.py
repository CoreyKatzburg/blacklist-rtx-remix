"""Query probe rule (worker B, written Nov 17 before the run): which draw puts the screen orbs on screen while the
flare stand-in balls stay on, and does Remix drop the balls because their instances live one frame?

One Remix launch at con_bright_hall on the build with X1/X2 (query_probe.h), [Lights] FlareStandIns=1 except where a
step says otherwise, [Proxy] QueryStatsFrames=10 in the ini before the game starts. Steps (window names):

  lampsA pose (bench.switchPoses, Sam at (-11300,-700,-493) yaw 170, view 0 / 30):
    switch_lampsA_off          FlareStandIns 0, rtx.numFramesToKeepInstances 1   (baseline for the ball measure)
    switch_lampsA_keep1        FlareStandIns 1, keep 1
    switch_lampsA_keep2        FlareStandIns 1, keep 2
    switch_lampsA_keep1_again  FlareStandIns 1, keep 1
  switch_base pose (bench.switches, the spot's y0_p0 at the spawn):
    switch_base                SkipBracketDraws 0, keep 1
    switch_skip_hdr            SkipBracketDraws 1
    switch_skip_other          SkipBracketDraws 2
    switch_base_again          SkipBracketDraws 0
    switch_keep2               keep 2
    switch_keep1_again         keep 1
    switch_balls_off           FlareStandIns 0 (X1 comparison only)

Every step's state is read back from proxy.log, never assumed from the request: the QueryProbe lines whose frame
range lies inside the window (+-1 frame) carry the window's counts, and the keys in force at each such line are the
last "SkipBracketDraws: now N" (else the load line), "SetConfigVariable rtx.numFramesToKeepInstances = V -> 0" and
"settings reloaded (... flare stand-ins N" written before it in the log. A step whose lines disagree, or that has no
QueryProbe line inside its window, is NOT MEASURED.

1. X2, which draw (verdict per skip step). Orbs = glow_blobs.py's unmatched count, Remix still against the no-Remix
   reference y0_p0 still. d = max(1, |orbs(base) - orbs(base_again)|).
     NOT MEASURED  the step's SkipBracketDraws is not the asked mask, base or base_again is not 0, the skip step's
                   QueryProbe lines skipped 0 draws of its class (nothing was tested), a base step skipped any,
                   stand-ins were off (settings line, or no standin=1 row in the step's light dump), or glow_blobs
                   did not compare one of the three stills (exit 2, pose).
     ORBS GONE     orbs(skip) is lower than both base and base_again by more than 3 d: the skipped class of draws
                   puts the orbs on screen.
     ORBS RISE     orbs(skip) is higher than both by more than 3 d.
     NO CHANGE     otherwise.
   Lost glows and puffs per step are printed beside it, report only: a skip that removes the game's own lamp glows
   too shows here as lost glows going up.

2. Keep instances (verdict at lampsA). For each standin=1 flare with moved=0 and samgear=0 that projects on screen
   with the step's recorded camera and is not hidden by Remix's depth (depth at the ball >= flare z - 10 cm), against
   switch_lampsA_off: nearer = depth pixels in the ball's box nearer than in the off step by more than 1 cm; rise =
   peak _final.pfm luma in the box minus the off step's. A ball SHOWS when nearer >= 40 and rise > 0.2; a ball
   HINTS when nearer >= 40 or rise > 0.2.
     NOT MEASURED  fewer than 3 such balls, a step's keep or stand-in state not as asked, or a dump missing.
     PROVEN        keep2 shows at least 3 balls and neither keep1 step hints any: Remix drops the balls because
                   rtx.numFramesToKeepInstances 1 removes instances re-created every frame before the TLAS is built.
     CONTRADICTED  a keep1 step hints a ball (the balls can be ray traced with keep 1).
     NOT PROVEN    otherwise.
   Screen orbs at switch_base under keep 1 / 2 / 1 (switch_base_again, switch_keep2, switch_keep1_again) are printed
   with the band 3 d, report only. Caveat, printed with every keep result: keep 2 keeps every game object one extra
   frame, not only the balls, so keep 2 is a test of the cause and not a fix.

3. X1, the game's occlusion queries (report only). Per step, per frame: queries issued, answers handed to the game,
   answers above 0 pixels, real polls above 0. "query answers follow the balls" is printed when answers above 0 per
   frame at switch_balls_off differ from the mean of switch_base and switch_base_again by more than
   3 max(1, |base - base_again|).

Usage: query_probe_rule.py <spot dir of the launch> [--reference <no-Remix con_bright_hall folder>]
       query_probe_rule.py --self-test
"""
import argparse, json, math, pathlib, re, subprocess, sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
GLOW = HERE / "glow_blobs.py"
REFERENCE = pathlib.Path(r"D:\BlacklistOverhaul\experiments\remix-gate\reference-20261116-191120\con_bright_hall")
QUERY_FRAMES = 10
R_CM = 2.5                     # largest stand-in radius (log: "max radius 2.5")
NEARER_PX, RISE, MIN_BALLS = 40, 0.2, 3
CAVEAT = ("caveat: rtx.numFramesToKeepInstances 2 keeps every game object one extra frame, not only the balls; "
          "it tests the cause and is not a fix")

X2_STEPS = {"switch_skip_hdr": (1, "skipped_hdr"), "switch_skip_other": (2, "skipped_other")}
BASES = ("switch_base", "switch_base_again")
LAMPS = {"off": "switch_lampsA_off", "keep1": "switch_lampsA_keep1", "keep2": "switch_lampsA_keep2",
         "keep1_again": "switch_lampsA_keep1_again"}
ASKED = {  # window: (stand-ins, keep, SkipBracketDraws)
    "switch_lampsA_off": (0, 1, 0), "switch_lampsA_keep1": (1, 1, 0), "switch_lampsA_keep2": (1, 2, 0),
    "switch_lampsA_keep1_again": (1, 1, 0), "switch_base": (1, 1, 0), "switch_skip_hdr": (1, 1, 1),
    "switch_skip_other": (1, 1, 2), "switch_base_again": (1, 1, 0), "switch_keep2": (1, 2, 0),
    "switch_keep1_again": (1, 1, 0), "switch_balls_off": (0, 1, 0)}

QP_RE = re.compile(r"QueryProbe: frames (\d+)-(\d+) issued (\d+) answered (\d+) \(pixels>0 (\d+)\) polled (\d+) "
                   r"\(pixels>0 (\d+)\) skipped scene-hdr (\d+) other (\d+)")
LOAD_RE = re.compile(r"Proxy keys: QueryStatsFrames (\d+), SkipBracketDraws (\d+)")
SKIP_RE = re.compile(r"SkipBracketDraws: now (\d+)")
KEEP_RE = re.compile(r"SetConfigVariable rtx\.numFramesToKeepInstances = (\d+) -> 0\b")
STANDIN_RE = re.compile(r"settings reloaded \(.*flare stand-ins (\d+),")
ORBS_RE = re.compile(r"glow_blobs: unmatched (\d+) of \d+ blobs")
LOST_RE = re.compile(r"glow_blobs: lost (\d+) of \d+ original glows")
PUFFS_RE = re.compile(r"glow_blobs: puffs (\d+)")
QP_FIELDS = ("issued", "answered", "answered_nz", "polled", "polled_nz", "skipped_hdr", "skipped_other")


# ---- reading proxy.log ---------------------------------------------------------------------------------------------

def parse_log(lines) -> list[dict]:
    """One dict per QueryProbe line: frames, counts and the keys in force when it was written (log order)."""
    skip, keep, standins, out = 0, 1, 1, []
    for line in lines:
        if m := LOAD_RE.search(line):
            skip = int(m[2])
        elif m := SKIP_RE.search(line):
            skip = int(m[1])
        elif m := KEEP_RE.search(line):
            keep = int(m[1])
        elif m := STANDIN_RE.search(line):
            standins = int(m[1])
        elif m := QP_RE.search(line):
            v = list(map(int, m.groups()))
            out.append({"first": v[0], "last": v[1], **dict(zip(QP_FIELDS, v[2:])),
                        "skip": skip, "keep": keep, "standins": standins})
    return out


def window_state(qp: list[dict], lo: int, hi: int):
    """(state, counts per frame, totals) of the QueryProbe lines inside [lo-1, hi+1]; (None, reason, None) if none
    or if the keys changed inside the window."""
    inside = [q for q in qp if q["first"] >= lo - 1 and q["last"] <= hi + 1]
    if not inside:
        return None, "no QueryProbe line inside the window", None
    states = {(q["standins"], q["keep"], q["skip"]) for q in inside}
    if len(states) > 1:
        return None, f"keys changed inside the window {sorted(states)}", None
    frames = sum(q["last"] - q["first"] + 1 for q in inside)
    totals = {k: sum(q[k] for q in inside) for k in QP_FIELDS}
    return states.pop(), {k: totals[k] / frames for k in QP_FIELDS}, totals


# ---- verdicts (pure, covered by the self-test) ---------------------------------------------------------------------

def x2_verdict(step: str, orbs: dict, state: dict, totals: dict, standin_rows: dict) -> tuple[str, str]:
    """orbs: {window: count or None}; state: {window: (standins, keep, skip) or None}; totals: {window: counts}."""
    mask, field = X2_STEPS[step]
    wins = (*BASES, step)
    for w in wins:
        if state.get(w) is None:
            return "NOT MEASURED", f"{w}: keys not read back"
        if state[w][0] != 1 or not standin_rows.get(w):
            return "NOT MEASURED", f"{w}: stand-ins off"
        if orbs.get(w) is None:
            return "NOT MEASURED", f"{w}: glow_blobs did not compare it"
    if state[step][2] != mask:
        return "NOT MEASURED", f"{step}: SkipBracketDraws {state[step][2]}, asked {mask}"
    for w in BASES:
        if state[w][2] != 0 or totals[w]["skipped_hdr"] or totals[w]["skipped_other"]:
            return "NOT MEASURED", f"{w}: SkipBracketDraws {state[w][2]}, skipped draws in it"
    if not totals[step][field]:
        return "NOT MEASURED", f"{step}: no draw of its class skipped (nothing tested)"
    d = max(1, abs(orbs[BASES[0]] - orbs[BASES[1]]))
    b, ba, s = orbs[BASES[0]], orbs[BASES[1]], orbs[step]
    detail = f"orbs base {b}, {step} {s}, base_again {ba}; band 3 d = {3 * d}; skipped {totals[step][field]} draws"
    if b - s > 3 * d and ba - s > 3 * d:
        return "ORBS GONE", detail
    if s - b > 3 * d and s - ba > 3 * d:
        return "ORBS RISE", detail
    return "NO CHANGE", detail


def ball_rows(flares, cam, proj, fin_on, fin_off, depth_on, depth_off, r_cm=R_CM) -> list[dict]:
    """Per static standin=1 flare on screen: in_view (not hidden by Remix depth), nearer depth px, peak luma rise.
    cam = (x, y, z, yaw deg, pitch deg); proj = (P00, P11); fin_* full-size luma, depth_* view z in cm."""
    y, p = math.radians(cam[3]), math.radians(cam[4])
    fwd = np.array([math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), math.sin(p)])
    right = np.cross([0.0, 0.0, 1.0], fwd)
    right /= np.linalg.norm(right)
    up = np.cross(fwd, right)
    H, W = fin_on.shape
    DH, DW = depth_on.shape
    rows, seen = [], set()
    for f in flares:
        if f.get("standin") != "1" or f.get("moved") != "0" or f.get("samgear") != "0":
            continue
        pos = np.array([float(v) for v in f["pos"].split(",")])
        key = tuple(pos.round(0))
        if key in seen:
            continue
        seen.add(key)
        d = pos - np.array(cam[:3])
        z = float(d @ fwd)
        if z <= 20:
            continue
        sx, sy = proj[0] * float(d @ right) / z, proj[1] * float(d @ up) / z
        if abs(sx) >= 1 or abs(sy) >= 1:
            continue
        u, v = W / 2 * (1 + sx), H / 2 * (1 - sy)
        du, dv = int(DW / 2 * (1 + sx)), int(DH / 2 * (1 - sy))
        rpx = r_cm / z * W / 2 * proj[0]
        rd = int(rpx * DW / W)
        rz = float(depth_off[max(0, dv - 1):dv + 2, max(0, du - 1):du + 2].max())
        b = int(math.ceil(rpx)) + 3
        box = (slice(max(0, int(v) - b), int(v) + b + 1), slice(max(0, int(u) - b), int(u) + b + 1))
        dbox = (slice(max(0, dv - rd - 2), dv + rd + 3), slice(max(0, du - rd - 2), du + rd + 3))
        rows.append({"u": u, "v": v, "z": z, "rpx": rpx, "in_view": rz >= z - 10,
                     "nearer": int((depth_on[dbox] < depth_off[dbox] - 1).sum()),
                     "rise": float(fin_on[box].max() - fin_off[box].max())})
    return rows


def shows(r) -> bool:
    return r["nearer"] >= NEARER_PX and r["rise"] > RISE


def hints(r) -> bool:
    return r["nearer"] >= NEARER_PX or r["rise"] > RISE


def keep_verdict(rows: dict, state: dict) -> tuple[str, str]:
    """rows: {'keep1'|'keep2'|'keep1_again': ball_rows or None}; state: {window: (standins, keep, skip) or None}."""
    for k, w in LAMPS.items():
        if state.get(w) is None:
            return "NOT MEASURED", f"{w}: keys not read back"
        if state[w][:2] != ASKED[w][:2]:
            return "NOT MEASURED", f"{w}: stand-ins {state[w][0]} keep {state[w][1]}, asked {ASKED[w][:2]}"
        if k != "off" and rows.get(k) is None:
            return "NOT MEASURED", f"{w}: dump missing"
    views = {k: [r for r in rows[k] if r["in_view"]] for k in ("keep1", "keep2", "keep1_again")}
    if len(views["keep2"]) < MIN_BALLS:
        return "NOT MEASURED", f"{len(views['keep2'])} static balls in view (need {MIN_BALLS})"
    n_show = sum(shows(r) for r in views["keep2"])
    n_hint = {k: sum(hints(r) for r in views[k]) for k in ("keep1", "keep1_again")}
    detail = (f"keep2 shows {n_show} of {len(views['keep2'])} balls; keep1 hints {n_hint['keep1']}, keep1_again "
              f"hints {n_hint['keep1_again']} (show: nearer >= {NEARER_PX} px and rise > {RISE})")
    if n_hint["keep1"] or n_hint["keep1_again"]:
        return "CONTRADICTED", detail
    if n_show >= MIN_BALLS:
        return "PROVEN", detail
    return "NOT PROVEN", detail


def x1_report(rates: dict) -> str:
    """rates: {window: per-frame counts or None}."""
    w = (*BASES, "switch_balls_off")
    if any(rates.get(x) is None for x in w):
        return "x1: NOT MEASURED (a step has no QueryProbe line)"
    b, ba, off = (rates[x]["answered_nz"] for x in w)
    band = 3 * max(1.0, abs(b - ba))
    follow = abs(off - (b + ba) / 2) > band
    return (f"x1: answers above 0 per frame base {b:.2f}, base_again {ba:.2f}, balls_off {off:.2f}, band {band:.2f}: "
            + ("query answers follow the balls" if follow else "no change with the balls") + " (report only)")


# ---- reading a launch ----------------------------------------------------------------------------------------------

def read_pfm(path: pathlib.Path):
    with open(path, "rb") as f:
        kind = f.readline().strip()
        w, h = map(int, f.readline().split())
        scale = float(f.readline())
        ch = 3 if kind == b"PF" else 1
        a = np.fromfile(f, "<f4" if scale < 0 else ">f4", w * h * ch).reshape(h, w, ch)[::-1]
    return (a @ np.array([0.2126, 0.7152, 0.0722], np.float32)) if ch == 3 else a[..., 0]


def flares_of(tsv: pathlib.Path) -> list[dict]:
    if not tsv.exists():
        return []
    return [dict(kv.split("=", 1) for kv in line.rstrip("\n").split("\t")[1:] if "=" in kv)
            for line in tsv.open(errors="replace") if line.startswith("flare\t")]


def camera(spot: pathlib.Path, lo: int, hi: int):
    cams = []
    for line in (spot / "proxy_frames.csv").open(errors="replace"):
        c = line.rstrip("\n").split(",")
        if len(c) < 7 or not c[0].isdigit() or not lo <= int(c[0]) <= hi or not c[6]:
            continue
        for pub in c[6].split("|"):
            p = pub.split(":")
            if len(p) >= 9 and p[2] == "0":
                cams.append([float(v) for v in p[3:9]])
                break
    if not cams:
        return None
    m = np.median(np.array(cams), axis=0)
    return (*m[:3], math.degrees(math.atan2(m[4], m[3])), math.degrees(math.asin(np.clip(m[5], -1, 1))))


def glow(spot: pathlib.Path, ref: pathlib.Path, still: str):
    p = subprocess.run([sys.executable, "-I", str(GLOW), "--remix-spot", str(spot), "--orig-spot", str(ref),
                        "--still", still, "--orig-still", "y0_p0"], capture_output=True, text=True, timeout=300)
    o, l, pf = ORBS_RE.search(p.stdout), LOST_RE.search(p.stdout), PUFFS_RE.search(p.stdout)
    if p.returncode not in (0, 1) or not o or not l:
        return None
    return {"orbs": int(o[1]), "lost": int(l[1]), "puffs": int(pf[1]) if pf else None}


def score(spot: pathlib.Path, ref: pathlib.Path, out=print):
    windows = {w["name"]: w for w in json.loads((spot / "spot.json").read_text())["windows"] if w.get("proxy")}
    qp = parse_log((spot / "proxy.log").open(errors="replace"))
    state, rates, totals, standin_rows = {}, {}, {}, {}
    for w in ASKED:
        if w not in windows:
            state[w] = rates[w] = totals[w] = None
            out(f"query_probe_rule: {w}: window missing")
            continue
        lo, hi = windows[w]["proxy"]
        state[w], rates[w], totals[w] = window_state(qp, lo, hi)
        if state[w] is None:
            out(f"query_probe_rule: {w}: {rates[w]}")
            rates[w] = None
        standin_rows[w] = sum(f.get("standin") == "1" for f in flares_of(spot / "switch" / f"{w[7:]}_lights.tsv"))
    for w in ASKED:
        if rates.get(w):
            r = rates[w]
            out(f"  {w:27s} state (stand-ins, keep, skip) {state[w]}  per frame: issued {r['issued']:.2f} answered "
                f"{r['answered']:.2f} (>0 {r['answered_nz']:.2f}) polled {r['polled']:.2f} (>0 {r['polled_nz']:.2f}) "
                f"skipped hdr {r['skipped_hdr']:.1f} other {r['skipped_other']:.1f}; standin rows {standin_rows[w]}")

    switch_wins = [w for w in ASKED if not w.startswith("switch_lampsA") and w in windows]
    counts = {w: glow(spot, ref, w) for w in switch_wins}
    orbs = {w: (c or {}).get("orbs") for w, c in counts.items()}
    for w in switch_wins:
        c = counts[w]
        out(f"  {w:27s} " + ("glow_blobs did not compare it" if c is None else
                             f"orbs {c['orbs']}, lost glows {c['lost']}, puffs {c['puffs']} (lost and puffs report only)"))
    for step in X2_STEPS:
        v, detail = x2_verdict(step, orbs, state, totals, standin_rows)
        out(f"query_probe_rule: x2 {step}: {v} ({detail})")

    rows = {}
    g = spot / "gate"
    try:
        off = LAMPS["off"]
        fin_off, depth_off = read_pfm(g / f"{off}_final.pfm"), read_pfm(g / f"{off}_depth.pfm")
        proj = np.median(np.array([tuple(map(float, m.groups())) for line in (spot / "proxy.log").open(errors="replace")
                                   if "camera decomposition accepted" in line and
                                   (m := re.search(r"projection ([-0-9.]+) ([-0-9.]+)", line))]), axis=0)
        for k in ("keep1", "keep2", "keep1_again"):
            w = LAMPS[k]
            cam = camera(spot, *windows[w]["proxy"]) if w in windows else None
            if cam is None or not (g / f"{w}_final.pfm").exists():
                rows[k] = None
                continue
            rows[k] = ball_rows(flares_of(spot / "switch" / f"{w[7:]}_lights.tsv"), cam, proj,
                                read_pfm(g / f"{w}_final.pfm"), fin_off, read_pfm(g / f"{w}_depth.pfm"), depth_off)
            for r in rows[k]:
                out(f"  {w:27s} ball at {r['u']:.0f},{r['v']:.0f} z {r['z']:.0f} cm radius {r['rpx']:.1f} px in view "
                    f"{int(r['in_view'])} nearer {r['nearer']} rise {r['rise']:+.3f}")
    except (FileNotFoundError, KeyError, ValueError) as exc:
        out(f"query_probe_rule: lampsA dumps unreadable: {exc}")
    v, detail = keep_verdict(rows, state)
    out(f"query_probe_rule: keep lampsA: {v} ({detail})")
    keep_orbs = [orbs.get(w) for w in ("switch_base_again", "switch_keep2", "switch_keep1_again")]
    if None in keep_orbs or orbs.get("switch_base") is None:
        out("query_probe_rule: keep switch_base orbs: NOT MEASURED (report only)")
    else:
        d = max(1, abs(orbs["switch_base"] - orbs["switch_base_again"]))
        out(f"query_probe_rule: keep switch_base orbs keep 1 / 2 / 1: {keep_orbs[0]} / {keep_orbs[1]} / {keep_orbs[2]}"
            f", band 3 d = {3 * d} (report only)")
    out(f"query_probe_rule: {CAVEAT}")
    out("query_probe_rule: " + x1_report(rates))


# ---- fake cases ----------------------------------------------------------------------------------------------------

def self_test() -> int:
    fails = []

    def check(name, got, want):
        good = got == want
        print(f"{'ok  ' if good else 'FAIL'} {name}: {got}")
        if not good:
            fails.append(name)

    # Log state: keys follow the log order, the load line sets the starting mask.
    log = ["Proxy keys: QueryStatsFrames 10, SkipBracketDraws 0",
           "lightpub: SetConfigVariable rtx.numFramesToKeepInstances = 1 -> 0",
           "QueryProbe: frames 100-109 issued 50 answered 40 (pixels>0 20) polled 10 (pixels>0 5) skipped scene-hdr 0 other 0",
           "QueryProbe: frames 110-119 issued 50 answered 40 (pixels>0 22) polled 10 (pixels>0 5) skipped scene-hdr 0 other 0",
           "SkipBracketDraws: now 1",
           "QueryProbe: frames 120-129 issued 50 answered 40 (pixels>0 2) polled 10 (pixels>0 1) skipped scene-hdr 30 other 0",
           "lightpub: SetConfigVariable rtx.numFramesToKeepInstances = 2 -> 0",
           "QueryProbe: frames 130-139 issued 50 answered 40 (pixels>0 2) polled 10 (pixels>0 1) skipped scene-hdr 30 other 0",
           "lightpub: SetConfigVariable rtx.numFramesToKeepInstances = 2 -> 3",
           "QueryProbe: frames 140-149 issued 0 answered 0 (pixels>0 0) polled 0 (pixels>0 0) skipped scene-hdr 0 other 0"]
    qp = parse_log(log)
    st, rate, tot = window_state(qp, 100, 119)
    check("window 100-119: state (1, 1, 0), 2.1 answers above 0 per frame", (st, round(rate["answered_nz"], 2)),
          ((1, 1, 0), 2.1))
    check("window 120-129 reads SkipBracketDraws 1 from the 'now' line", window_state(qp, 120, 129)[0], (1, 1, 1))
    check("a failed SetConfigVariable (-> 3) does not change keep", window_state(qp, 140, 149)[0], (1, 2, 1))
    check("keys changing inside a window (keep 1 then 2): NOT MEASURED", window_state(qp, 120, 139)[0], None)
    check("window with no QueryProbe line inside: NOT MEASURED", window_state(qp, 103, 108)[0], None)

    # X2 verdict.
    S = {"switch_base": (1, 1, 0), "switch_base_again": (1, 1, 0), "switch_skip_hdr": (1, 1, 1),
         "switch_skip_other": (1, 1, 2)}
    zero = {f: 0 for f in QP_FIELDS}
    T = {"switch_base": zero, "switch_base_again": zero, "switch_skip_hdr": {**zero, "skipped_hdr": 900},
         "switch_skip_other": {**zero, "skipped_other": 300}}
    R = {w: 94 for w in S}
    x2 = lambda o, s=S, t=T, r=R, step="switch_skip_hdr": x2_verdict(step, o, s, t, r)[0]
    check("13 / 0 / 10: ORBS GONE", x2({"switch_base": 13, "switch_skip_hdr": 0, "switch_base_again": 10}), "ORBS GONE")
    check("13 / 6 / 10 (6 is inside 3 d = 9 of base_again): NO CHANGE",
          x2({"switch_base": 13, "switch_skip_hdr": 6, "switch_base_again": 10}), "NO CHANGE")
    check("13 / 1 / 12 (falls by 11 and 11, band 3): ORBS GONE",
          x2({"switch_base": 13, "switch_skip_hdr": 1, "switch_base_again": 12}), "ORBS GONE")
    check("5 / 20 / 6: ORBS RISE", x2({"switch_base": 5, "switch_skip_hdr": 20, "switch_base_again": 6}), "ORBS RISE")
    gone = {"switch_base": 13, "switch_skip_hdr": 0, "switch_base_again": 10}
    check("mask did not load (skip step reads 0): NOT MEASURED", x2(gone, s={**S, "switch_skip_hdr": (1, 1, 0)}),
          "NOT MEASURED")
    check("no draw skipped in the skip step: NOT MEASURED", x2(gone, t={**T, "switch_skip_hdr": zero}), "NOT MEASURED")
    check("a base step skipped draws: NOT MEASURED",
          x2(gone, t={**T, "switch_base_again": {**zero, "skipped_hdr": 5}}), "NOT MEASURED")
    check("stand-ins off in the skip step: NOT MEASURED", x2(gone, s={**S, "switch_skip_hdr": (0, 1, 1)}),
          "NOT MEASURED")
    check("no standin=1 row in the skip step's dump: NOT MEASURED", x2(gone, r={**R, "switch_skip_hdr": 0}),
          "NOT MEASURED")
    check("glow_blobs refused base_again: NOT MEASURED",
          x2({**gone, "switch_base_again": None}), "NOT MEASURED")
    check("mask 2 step reads its own skipped-other count",
          x2({"switch_base": 13, "switch_skip_other": 0, "switch_base_again": 10}, step="switch_skip_other"),
          "ORBS GONE")

    # Ball measure on synthetic pictures: a wall at 300 cm, 4 static stand-ins 15 cm in front of it.
    W, H, DW, DH = 640, 360, 427, 240
    cam, proj = (0.0, 0.0, 0.0, 0.0, 0.0), (1.0, 1.777)
    flares = [{"standin": "1", "moved": "0", "samgear": "0", "pos": f"285,{y},{z}"}
              for y, z in ((-60, 20), (-20, -30), (30, 25), (70, -10))]
    flares += [{"standin": "1", "moved": "1", "samgear": "1", "pos": "285,0,0"},      # Sam gear: never counted
               {"standin": "0", "moved": "0", "samgear": "0", "pos": "285,10,0"}]     # no ball
    fin_off, depth_off = np.full((H, W), 0.1, np.float32), np.full((DH, DW), 300.0, np.float32)

    # The fake balls are drawn 10 cm wide so they cover as many pixels on this 640 px picture as a 2.5 cm ball does
    # at 2560 px; R10 passes that radius to the measure.
    R10 = 10.0

    def painted(which):
        fin, dep = fin_off.copy(), depth_off.copy()
        for i in which:
            r = ball_rows([flares[i]], cam, proj, fin_off, fin_off, depth_off, depth_off, R10)[0]
            yy, xx = np.ogrid[:H, :W]
            fin[(xx - r["u"]) ** 2 + (yy - r["v"]) ** 2 <= r["rpx"] ** 2] = 1.0
            du, dv, rd = r["u"] * DW / W, r["v"] * DH / H, r["rpx"] * DW / W
            yy, xx = np.ogrid[:DH, :DW]
            dep[(xx - du) ** 2 + (yy - dv) ** 2 <= rd * rd] = r["z"] - 2
        return fin, dep

    measure = lambda fl, fin, dep: ball_rows(fl, cam, proj, fin, fin_off, dep, depth_off, R10)
    rows_none = measure(flares, fin_off, depth_off)
    check("4 static balls on screen and in view, Sam gear and standin=0 left out",
          (len(rows_none), sum(r["in_view"] for r in rows_none)), (4, 4))
    check("unpainted: no ball shows or hints", sum(hints(r) for r in rows_none), 0)
    rows3 = measure(flares, *painted([0, 1, 2]))
    check("3 painted balls show, the 4th does not", [shows(r) for r in rows3], [True, True, True, False])
    check("brightness alone (no depth) hints but does not show",
          [(hints(r), shows(r)) for r in measure(flares[:1], painted([0])[0], depth_off)], [(True, False)])

    hidden = np.full((DH, DW), 200.0, np.float32)                       # a wall in front of every ball
    check("balls behind nearer geometry are not in view",
          sum(r["in_view"] for r in ball_rows(flares, cam, proj, fin_off, fin_off, hidden, hidden, R10)), 0)
    LS = {w: ASKED[w] for w in LAMPS.values()}
    check("keep2 shows 3, keep1 steps none: PROVEN",
          keep_verdict({"keep1": rows_none, "keep2": rows3, "keep1_again": rows_none}, LS)[0], "PROVEN")
    rows1 = measure(flares, *painted([0]))
    check("keep2 shows only 1: NOT PROVEN",
          keep_verdict({"keep1": rows_none, "keep2": rows1, "keep1_again": rows_none}, LS)[0], "NOT PROVEN")
    check("keep1_again hints a ball: CONTRADICTED",
          keep_verdict({"keep1": rows_none, "keep2": rows3, "keep1_again": rows1}, LS)[0], "CONTRADICTED")
    check("keep2 step read back keep 1: NOT MEASURED",
          keep_verdict({"keep1": rows_none, "keep2": rows3, "keep1_again": rows_none},
                       {**LS, "switch_lampsA_keep2": (1, 1, 0)})[0], "NOT MEASURED")
    check("only 2 balls in view: NOT MEASURED",
          keep_verdict({"keep1": rows_none[:2], "keep2": rows3[:2], "keep1_again": rows_none[:2]}, LS)[0],
          "NOT MEASURED")
    # X1 report.
    rr = lambda a: {"answered_nz": a}
    check("x1: balls_off 5.0 against 20.0 / 20.4 (band 3): follows the balls",
          "follow the balls" in x1_report({"switch_base": rr(20.0), "switch_base_again": rr(20.4),
                                            "switch_balls_off": rr(5.0)}), True)
    check("x1: balls_off 22.0 against 20.0 / 20.4 (1.8 off the mean, band 3): no change",
          "no change" in x1_report({"switch_base": rr(20.0), "switch_base_again": rr(20.4),
                                    "switch_balls_off": rr(22.0)}), True)
    check("x1: a step without QueryProbe lines: NOT MEASURED",
          "NOT MEASURED" in x1_report({"switch_base": rr(20.0), "switch_base_again": None,
                                       "switch_balls_off": rr(5.0)}), True)
    check("caveat names keep 2 as no fix", "not a fix" in CAVEAT, True)
    print("self-test:", "all passed" if not fails else f"FAILED {fails}")
    return 1 if fails else 0


def main():
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    ap = argparse.ArgumentParser()
    ap.add_argument("spot", type=pathlib.Path, help="the launch's con_bright_hall spot folder")
    ap.add_argument("--reference", type=pathlib.Path, default=REFERENCE, help="no-Remix con_bright_hall folder")
    a = ap.parse_args()
    score(a.spot, a.reference)


if __name__ == "__main__":
    main()
