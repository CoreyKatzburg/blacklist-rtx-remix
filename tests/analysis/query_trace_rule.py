"""Reading rule for worker B's X3 launch (per-query trace and frame grabs), written Nov 17 before the run.

Launch: con_bright_hall, the switch_base pose (spawn), three steps in this order:
    switch_base (FlareStandIns 1), switch_balls_off (FlareStandIns 0), switch_base_again (FlareStandIns 1)
  SkipBracketDraws 0 and rtx.numFramesToKeepInstances 1 throughout. [Proxy] QueryStatsFrames=10, QueryTraceEvery=15,
  PresentGrabFrames=8 for the whole launch (read at load). One GateDump per step plus the light dump; whole proxy.log.

Keys (else every part reads NOT MEASURED): proxy.log has "Proxy keys: QueryTraceEvery K, QueryTraceActive 1,
PresentGrabFrames N" with K >= 1 and N >= 4; switch_base and switch_base_again light dumps have standin=1 rows and the
balls_off dump has none.

Part 1. The orbs at hidden flares (kind 1; 5 of them in 210750). Per balls-on step:
  orbs: glow_blobs' unmatched orbs of the step's still against the reference (as query_probe_rule.glow).
  A kind-1 orb: the nearest flare of the step's light dump projected on screen is within 2 grid px (320x180), and that
  flare is behind the game's depth (gamedepth 3x3 max < flare distance - 10 units).
  Tie a traced query to a flare by the position box of the first draw inside the query (QueryTrace "pos" fields), in
  one of three spaces, chosen once for the launch:
    world   the flare lies inside the box grown by 30 units, or within 100 units of its centre
    camrel  the same with the box moved by the camera position
    ndc     the box lies within |x|,|y| <= 1.5 and its centre maps (320x180) within 3 px of the projected flare
  For each space, n = traced queries of both balls-on windows that tie any flare, and c = the same with every flare
  moved by 1000 units (ndc: 40 px). A space is usable when n >= 3 and n > 2c; the usable space with the largest n is
  taken. None usable: Part 1 reads NOT MEASURED (no query can be tied to a flare).
  Per kind-1 orb, its tied records (traced queries of the step's window tying its flare), then the first that applies:
    NOT MEASURED          no tied record.
    (a) GAME IGNORES      >= 80% of tied records were handed nothing (handed 0) or were handed 0 pixels: the game drew
                          the sprite without a visible answer for its query.
    MIXED                 fewer than 80% were handed pixels > 0 ("told visible"); counts printed.
    STALE QUERYMODE CACHE of the told-visible records with a fresh read, >= 50% read 0 pixels fresh from the bridge.
    NOT MEASURED          fewer than half the told-visible records have a QueryTraceFrame line with a scene-ds.
    (b) OTHER DEPTH       more than half the told-visible records tested against another depth surface than the
                          frame's scene-ds, or had ZENABLE 0, or ZFUNC 8 (ALWAYS).
    (c) WALL DROPPED      more than half are in frames whose dropped draws before the first query wrote depth into the
                          scene depth surface (QueryTraceFrame "z-write on scene ds" > 0).
    QUAD IN FRONT         (world or camrel only) the query quad's median depth is more than 10 units nearer than the
                          game depth at the flare: a correct test answers visible; the cause is where the game places
                          the query geometry.
    UNEXPLAINED           same depth surface, Z test on, no depth-writing drop, quad behind the wall: the D3D9 depth
                          the query tests against lacks the wall although the proxy dropped none of its writers. This
                          supports none of (a), (b), (c) and points at the bridge or Remix side (guess).
  Part 1 verdict: the reading most tied kind-1 orbs share across both balls-on steps (a tie between readings reads
  MIXED); NOT MEASURED when no kind-1 orb is tied. Report only: the QueryTraceFrame lines of each window.

Part 2. The 6 orbs on no flare (kind 2; 210750 sites 191,97; 186,128; 129,96; 173,130; 185,92; 207,127).
  Pictures per step: the screen still (frames\\<step>.gray, mean of its frames), the composite grabs
  (gate\\<step>_composite_f*.pfm, Remix's picture right after the proxy hands it to the game's composite) and the
  present grabs (gate\\<step>_present_f*.pfm, the back buffer before the real Present). Grabs: Rec. 709 luma, 320x180.
  Contrast at a site: (3x3 max + e) / (median of the ring 4..7 px + e), e = 0.02 x the picture's 99th percentile.
  d = mean contrast over the balls-on pictures (both steps) - mean over the balls_off pictures.
  Band per source: max(0.05, |median| + 3 x 1.4826 x MAD) of d over a 40 px grid of control points (32 points, those
  within 12 px of a site left out). A site is "in" a source when d > band.
  NOT MEASURED for every site when a step has fewer than 4 present grabs, or the mean present grab of any step
  correlates below 0.9 with its still (the grab is not the picture the still measured). Composite NOT MEASURED when a
  step has fewer than 4 composite grabs (a failed grab logs "PresentGrab: ... failed at step N").
  Per site:
    NOT REPRODUCED   not in the stills (the orb does not come and go with the balls in this launch).
    REMIX PICTURE    in the composite grabs: Remix's own output has the orb on grabbed frames (the one-frame
                     _final.pfm dump lacked it, so the orb is in some frames only).
    AFTER COMPOSITE  in the present grabs only: drawn onto the picture after the hand-off to the composite.
    NOT IN GRABS     in the stills but in neither grab.
  Part 2 verdict: the reading most reproduced sites share; NOT MEASURED when no site reproduces.

Usage: query_trace_rule.py <spot dir of the launch> [--reference <no-Remix con_bright_hall folder>]
       query_trace_rule.py --self-test
"""
import argparse, importlib.util, json, math, pathlib, re, sys
from collections import Counter

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("query_probe_rule", HERE / "query_probe_rule.py")
qp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qp)

GW, GH = 320, 180
ON, OFF = ("switch_base", "switch_base_again"), "switch_balls_off"
STEPS = ("switch_base", "switch_balls_off", "switch_base_again")
KIND2 = ((191, 97), (186, 128), (129, 96), (173, 130), (185, 92), (207, 127))
ORB_FLARE_PX = 2.0
MODES = ("world", "camrel", "ndc")
SHIFT_UNITS, SHIFT_PX = 1000.0, 40.0

FLOAT = r"(-?[0-9.]+(?:e[-+]?\d+)?|-?nan|-?inf)"
LOAD_RE = re.compile(r"Proxy keys: QueryTraceEvery (\d+), QueryTraceActive (\d+), PresentGrabFrames (\d+)")
TRACE_RE = re.compile(
    r"QueryTrace: frame (\d+) q (\S+) draws (\d+) sent (\d+) kind (\S+) ds ([0-9A-Fa-f]+) (\d+)x(\d+) f(\d+) "
    r"zen (-?\d+) zfn (-?\d+) zwr (-?\d+) cw (-?\d+) rt ([0-9A-Fa-f]+) pos (\d+) " + " ".join([FLOAT] * 6) +
    r" handed (\d+) first (\d+) last (\d+) fresh (\d+) first (\d+) end (\S+)")
FRAME_RE = re.compile(r"QueryTraceFrame: frame (\d+) queries (\d+) scene-ds ([0-9A-Fa-f]+) dropped-before-first-query "
                      r"(\d+) z-write on scene ds (\d+) other ds (\d+)")
GRAB_FAIL_RE = re.compile(r"PresentGrab: (\S+) failed at step (\d+)")


# ---- the query trace ------------------------------------------------------------------------------------------------

def parse_trace(lines):
    recs, frames = [], {}
    for line in lines:
        if m := TRACE_RE.search(line):
            g = m.groups()
            recs.append({"frame": int(g[0]), "q": g[1], "draws": int(g[2]), "sent": int(g[3]), "kind": g[4],
                         "ds": int(g[5], 16), "zen": int(g[9]), "zfn": int(g[10]), "zwr": int(g[11]),
                         "posn": int(g[14]), "mn": np.array([float(v) for v in g[15:18]]),
                         "mx": np.array([float(v) for v in g[18:21]]), "handed": int(g[21]), "last": int(g[23]),
                         "fresh": int(g[24]), "fresh_first": int(g[25]), "end": g[26]})
        elif m := FRAME_RE.search(line):
            g = m.groups()
            frames[int(g[0])] = {"queries": int(g[1]), "scene_ds": int(g[2], 16), "dropped": int(g[3]),
                                 "zw_scene": int(g[4]), "zw_other": int(g[5])}
    return recs, frames


def basis(cam):
    y, p = math.radians(cam[3]), math.radians(cam[4])
    fwd = np.array([math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), math.sin(p)])
    right = np.cross([0.0, 0.0, 1.0], fwd)
    right /= np.linalg.norm(right)
    return fwd, right, np.cross(fwd, right)


def project(cam, proj, pos):
    """(u, v, z, sx, sy) on the 320x180 grid, or None when behind the camera or off screen."""
    fwd, right, up = basis(cam)
    d = np.asarray(pos, float) - np.array(cam[:3], float)
    z = float(d @ fwd)
    if z <= 20:
        return None
    sx, sy = proj[0] * float(d @ right) / z, proj[1] * float(d @ up) / z
    if abs(sx) >= 1.1 or abs(sy) >= 1.1:
        return None
    return GW / 2 * (1 + sx), GH / 2 * (1 - sy), z, sx, sy


def scene_depth(gd, sx, sy):
    dh, dw = gd.shape
    du, dv = min(dw - 1, max(0, int(dw / 2 * (1 + sx)))), min(dh - 1, max(0, int(dh / 2 * (1 - sy))))
    return float(gd[max(0, dv - 1):dv + 2, max(0, du - 1):du + 2].max())


def make_flares(rows, cam, proj, gd):
    out = []
    for f in rows:
        pos = np.array([float(v) for v in f["pos"].split(",")])
        p = project(cam, proj, pos)
        fl = {"pos": pos, "uv": None, "z": None, "hidden": False, "scene": None, "standin": f.get("standin")}
        if p:
            fl["uv"], fl["z"] = (p[0], p[1]), p[2]
            if gd is not None:
                fl["scene"] = scene_depth(gd, p[3], p[4])
                fl["hidden"] = fl["scene"] < p[2] - 10
        out.append(fl)
    return out


def shifted(flares, mode, cam, proj):
    out = []
    for f in flares:
        g = dict(f)
        if mode == "ndc":
            g["uv"] = None if f["uv"] is None else (f["uv"][0] + SHIFT_PX, f["uv"][1])
        else:
            g["pos"] = f["pos"] + np.array([SHIFT_UNITS, SHIFT_UNITS, 0.0])
            p = project(cam, proj, g["pos"])
            g["uv"] = (p[0], p[1]) if p else None
        out.append(g)
    return out


def ties(r, f, mode, cam, proj):
    if not r["posn"] or not (np.all(np.isfinite(r["mn"])) and np.all(np.isfinite(r["mx"]))):
        return False
    c = (r["mn"] + r["mx"]) / 2
    if mode == "ndc":
        if f["uv"] is None or max(abs(r["mn"][0]), abs(r["mx"][0]), abs(r["mn"][1]), abs(r["mx"][1])) > 1.5:
            return False
        return math.hypot(GW / 2 * (1 + c[0]) - f["uv"][0], GH / 2 * (1 - c[1]) - f["uv"][1]) <= 3
    off = np.array(cam[:3], float) if mode == "camrel" else np.zeros(3)
    p = f["pos"]
    inside = bool(np.all(p >= r["mn"] + off - 30) and np.all(p <= r["mx"] + off + 30))
    if inside or float(np.linalg.norm(p - (c + off))) <= 100:
        return True
    q = project(cam, proj, c + off) if f["uv"] is not None else None
    return bool(q and math.hypot(q[0] - f["uv"][0], q[1] - f["uv"][1]) <= 3)


def choose_mode(steps):
    """steps: list of (records, flares, cam, proj). Returns (mode or None, {mode: (n, c)})."""
    table = {}
    for mode in MODES:
        n = sum(any(ties(r, f, mode, cam, proj) for f in fl) for recs, fl, cam, proj in steps for r in recs)
        c = sum(any(ties(r, f, mode, cam, proj) for f in shifted(fl, mode, cam, proj))
                for recs, fl, cam, proj in steps for r in recs)
        table[mode] = (n, c)
    usable = [m for m in MODES if table[m][0] >= 3 and table[m][0] > 2 * table[m][1]]
    return (max(usable, key=lambda m: table[m][0]) if usable else None), table


def quad_depth(recs, mode, cam):
    if mode not in ("world", "camrel") or not recs:
        return None
    fwd = basis(cam)[0]
    off = np.array(cam[:3], float) if mode == "camrel" else np.zeros(3)
    return float(np.median([((r["mn"] + r["mx"]) / 2 + off - np.array(cam[:3], float)) @ fwd for r in recs]))


def read_orb(tied, frames, quad_z=None, scene_z=None):
    if not tied:
        return "NOT MEASURED", "no tied record"
    n = len(tied)
    vis = [r for r in tied if r["handed"] > 0 and r["last"] > 0]
    blind = n - len(vis)
    counts = f"{n} tied: {len(vis)} told visible, {blind} handed nothing or 0 pixels"
    if blind >= 0.8 * n:
        return "(a) GAME IGNORES", counts
    if len(vis) < 0.8 * n:
        return "MIXED", counts
    fresh = [r for r in vis if r["fresh"] > 0]
    if fresh and sum(r["fresh_first"] == 0 for r in fresh) >= 0.5 * len(fresh):
        return "STALE QUERYMODE CACHE", counts + f"; {sum(r['fresh_first'] == 0 for r in fresh)} of {len(fresh)} " \
                                                 "fresh reads 0"
    known = [r for r in vis if frames.get(r["frame"], {}).get("scene_ds")]
    if len(known) < 0.5 * len(vis):
        return "NOT MEASURED", counts + "; no scene-ds logged for the traced frames"
    other = [r for r in known if r["ds"] != frames[r["frame"]]["scene_ds"] or r["zen"] == 0 or r["zfn"] == 8]
    drops = [r for r in known if frames[r["frame"]]["zw_scene"] > 0]
    detail = counts + f"; {len(other)} of {len(known)} on another depth or Z off/ALWAYS; {len(drops)} in frames " \
                      "with depth-writing drops on the scene ds"
    if len(other) > 0.5 * len(known):
        return "(b) OTHER DEPTH", detail
    if len(drops) > 0.5 * len(known):
        return "(c) WALL DROPPED", detail
    if quad_z is not None and scene_z is not None:
        detail += f"; quad depth {quad_z:.0f}, game depth {scene_z:.0f}"
        if quad_z < scene_z - 10:
            return "QUAD IN FRONT", detail
    return "UNEXPLAINED", detail


def majority(readings):
    c = Counter(readings)
    if not c:
        return "NOT MEASURED"
    top = c.most_common()
    if len(top) > 1 and top[0][1] == top[1][1]:
        return "MIXED"
    return top[0][0]


def part1(steps, out=print):
    """steps: {name: {"recs", "frames", "flares", "cam", "orbs"}} for the balls-on steps. Returns the verdict."""
    mode, table = choose_mode([(s["recs"], s["flares"], s["cam"], s["proj"]) for s in steps.values()])
    out("part 1 tie spaces (queries tying a flare / with flares moved): " +
        ", ".join(f"{m} {table[m][0]}/{table[m][1]}" for m in MODES) + f"; chosen {mode or 'none'}")
    readings = []
    for name, s in steps.items():
        kind1 = []
        for x, y in s["orbs"]:
            on = [f for f in s["flares"] if f["uv"] is not None]
            best = min(on, key=lambda f: math.hypot(f["uv"][0] - x, f["uv"][1] - y), default=None)
            if best and math.hypot(best["uv"][0] - x, best["uv"][1] - y) <= ORB_FLARE_PX and best["hidden"]:
                kind1.append((x, y, best))
        out(f"  {name}: {len(s['orbs'])} orbs, {len(kind1)} at a hidden flare")
        for x, y, f in kind1:
            if mode is None:
                out(f"    orb {x},{y}: NOT MEASURED (no tie space)")
                continue
            tied = [r for r in s["recs"] if ties(r, f, mode, s["cam"], s["proj"])]
            vis = [r for r in tied if r["handed"] > 0 and r["last"] > 0]
            reading, detail = read_orb(tied, s["frames"], quad_depth(vis, mode, s["cam"]), f["scene"])
            if reading != "NOT MEASURED":
                readings.append(reading)
            out(f"    orb {x},{y} flare {f['pos'].round(0).tolist()} standin {f['standin']}: {reading} ({detail})")
        for fr, info in sorted(s["frames"].items()):
            out(f"    report only: frame {fr} queries {info['queries']} scene-ds {info['scene_ds']:08X} dropped "
                f"{info['dropped']} z-write on scene ds {info['zw_scene']} other {info['zw_other']}")
    verdict = majority(readings) if mode else "NOT MEASURED"
    out(f"part 1: {verdict}")
    return verdict


# ---- the frame grabs ------------------------------------------------------------------------------------------------

def contrast_at(img, x, y):
    e = 0.02 * float(np.percentile(img, 99)) + 1e-6
    peak = float(img[max(0, y - 1):y + 2, max(0, x - 1):x + 2].max())
    yy, xx = np.ogrid[:img.shape[0], :img.shape[1]]
    d2 = (yy - y) ** 2 + (xx - x) ** 2
    ring = img[(d2 >= 16) & (d2 <= 49)]
    return (peak + e) / (float(np.median(ring)) + e)


def delta(on_imgs, off_imgs, x, y):
    return (float(np.mean([contrast_at(i, x, y) for i in on_imgs])) -
            float(np.mean([contrast_at(i, x, y) for i in off_imgs])))


def controls(sites):
    return [(x, y) for x in range(20, GW, 40) for y in range(20, GH, 40)
            if all(math.hypot(x - sx, y - sy) > 12 for sx, sy in sites)]


def band(on_imgs, off_imgs, sites):
    d = np.array([delta(on_imgs, off_imgs, x, y) for x, y in controls(sites)])
    med = float(np.median(d))
    return max(0.05, abs(med) + 3 * 1.4826 * float(np.median(np.abs(d - med))))


def site_reading(still_in, comp_in, pres_in):
    if not still_in:
        return "NOT REPRODUCED"
    if comp_in is True:
        return "REMIX PICTURE"
    if comp_in is None:
        return "NOT MEASURED"
    return "AFTER COMPOSITE" if pres_in else "NOT IN GRABS"


def part2(stills, comp, pres, sites=KIND2, out=print):
    """stills: {step: 2D array}; comp, pres: {step: [2D arrays]}. Returns (verdict, {site: reading})."""
    short = [s for s in STEPS if len(pres.get(s, [])) < 4]
    if short:
        out(f"part 2: NOT MEASURED (fewer than 4 present grabs in {', '.join(short)})")
        return "NOT MEASURED", {}
    for s in STEPS:
        r = float(np.corrcoef(np.mean(pres[s], axis=0).ravel(), stills[s].ravel())[0, 1])
        out(f"  {s}: mean present grab against the still r = {r:.3f}")
        if not r >= 0.9:
            out("part 2: NOT MEASURED (a present grab does not match its still)")
            return "NOT MEASURED", {}
    comp_ok = all(len(comp.get(s, [])) >= 4 for s in STEPS)
    src = {"still": ([stills[s] for s in ON], [stills[OFF]]),
           "present": ([i for s in ON for i in pres[s]], pres[OFF])}
    if comp_ok:
        src["composite"] = ([i for s in ON for i in comp[s]], comp[OFF])
    else:
        out("  composite grabs: NOT MEASURED (fewer than 4 in a step)")
    bands = {k: band(on, off, sites) for k, (on, off) in src.items()}
    out("  bands: " + ", ".join(f"{k} {v:.3f}" for k, v in bands.items()))
    result = {}
    for x, y in sites:
        d = {k: delta(on, off, x, y) for k, (on, off) in src.items()}
        isin = {k: d[k] > bands[k] for k in d}
        reading = site_reading(isin["still"], isin.get("composite") if comp_ok else None, isin["present"])
        result[(x, y)] = reading
        out(f"  site {x},{y}: " + ", ".join(f"{k} d {v:.3f}" for k, v in d.items()) + f": {reading}")
    verdict = majority([r for r in result.values() if r != "NOT REPRODUCED"])
    out(f"part 2: {verdict}")
    return verdict, result


# ---- a launch -------------------------------------------------------------------------------------------------------

def load_grabs(gate, step, what):
    from PIL import Image
    imgs = []
    for p in sorted(gate.glob(f"{step}_{what}_f*.pfm")):
        a = np.nan_to_num(qp.read_pfm(p)).astype(np.float32)
        if a.shape != (GH, GW):
            a = np.asarray(Image.fromarray(a).resize((GW, GH), Image.BOX))
        imgs.append(a)
    return imgs


def still_of(spot, step):
    raw = np.fromfile(spot / "frames" / f"{step}.gray", dtype=np.uint8)
    return raw.reshape(-1, GH, GW).mean(axis=0) / 255.0


def score(spot: pathlib.Path, ref: pathlib.Path, out=print):
    log = (spot / "proxy.log").read_text(errors="replace").splitlines()
    windows = {w["name"]: w for w in json.loads((spot / "spot.json").read_text())["windows"] if w.get("proxy")}
    load = next((m for line in log if (m := LOAD_RE.search(line))), None)
    out(f"load line: {load.group(0) if load else 'missing'}")
    dumps = {s: qp.flares_of(spot / "switch" / f"{s[len('switch_'):]}_lights.tsv") for s in STEPS}
    stand = {s: sum(f.get("standin") == "1" for f in dumps[s]) for s in STEPS}
    out("standin=1 rows: " + ", ".join(f"{s} {stand[s]}" for s in STEPS))
    problems = []
    if not load or int(load[1]) < 1 or load[2] != "1" or int(load[3]) < 4:
        problems.append("trace or grab keys not loaded as asked")
    if any(s not in windows for s in STEPS):
        problems.append("a step window is missing")
    if not all(stand[s] for s in ON) or stand[OFF]:
        problems.append("stand-in rows not as asked")
    for line in log:
        if m := GRAB_FAIL_RE.search(line):
            out(f"report only: grab {m[1]} failed at step {m[2]}")
    if problems:
        out("part 1: NOT MEASURED; part 2: NOT MEASURED (" + "; ".join(problems) + ")")
        return
    recs, frames = parse_trace(log)
    proj = np.median(np.array([tuple(map(float, m.groups())) for line in log if "camera decomposition accepted" in line
                               and (m := re.search(r"projection ([-0-9.]+) ([-0-9.]+)", line))]), axis=0)
    steps = {}
    for s in ON:
        lo, hi = windows[s]["proxy"]
        cam = qp.camera(spot, lo, hi)
        g = qp.glow(spot, ref, s)
        gd = qp.read_pfm(spot / "gate" / f"{s}_gamedepth.pfm")
        if cam is None or g is None:
            out(f"part 1: NOT MEASURED ({s}: no camera or no orb count)")
            steps = None
            break
        steps[s] = {"recs": [r for r in recs if lo <= r["frame"] <= hi],
                    "frames": {f: v for f, v in frames.items() if lo <= f <= hi},
                    "flares": make_flares(dumps[s], cam, proj, gd), "cam": cam, "proj": proj, "orbs": g["at"]}
    if steps:
        part1(steps, out)
    gate = spot / "gate"
    part2({s: still_of(spot, s) for s in STEPS}, {s: load_grabs(gate, s, "composite") for s in STEPS},
          {s: load_grabs(gate, s, "present") for s in STEPS}, out=out)


# ---- fake cases -----------------------------------------------------------------------------------------------------

def trace_line(frame, q, ds, zen, zfn, mn, mx, handed, last, fresh=0, fresh_first=0, posn=4):
    """One line in the proxy's exact format (query_trace.h Close, x86: %p and %08IX print 8 hex digits)."""
    return ("2026-11-17 21:00:00.000 [pid 1] QueryTrace: frame %u q %08X draws %u sent %u kind %s ds %08X %ux%u f%u "
            "zen %d zfn %d zwr %d cw %d rt %08X pos %u %.1f %.1f %.1f %.1f %.1f %.1f handed %u first %u last %u "
            "fresh %u first %u end %s") % (frame, q, 1, 1, "indexed-up", ds, 2560, 1440, 75, zen, zfn, 0, 0,
                                           0x1C5A9B70, posn, *mn, *mx, handed, last, last, fresh, fresh_first,
                                           "reissue")


def frame_line(frame, scene_ds, zw_scene):
    return ("2026-11-17 21:00:00.000 [pid 1] QueryTraceFrame: frame %u queries %u scene-ds %08X "
            "dropped-before-first-query %u z-write on scene ds %u other ds %u") % (frame, 60, scene_ds, 10, zw_scene, 0)


def self_test() -> int:
    fails = []

    def expect(cond, what):
        print(("ok   " if cond else "FAIL ") + what)
        if not cond:
            fails.append(what)

    quiet = lambda *_: None
    # A camera at the origin looking down +x; a wall 279 units away; a flare 400 units away behind it.
    cam, proj = (0.0, 0.0, 0.0, 0.0, 0.0), (1.0, 1.778)
    gd = np.full((90, 160), 279.0, np.float32)
    flare_pos = np.array([400.0, -40.0, 20.0])
    rows = [{"pos": "400,-40,20", "standin": "1"}, {"pos": "900,300,-100", "standin": "1"}]
    fl = make_flares(rows, cam, proj, gd)
    p = project(cam, proj, flare_pos)
    expect(p is not None and fl[0]["hidden"] and abs(fl[0]["uv"][0] - p[0]) < 1e-6,
           "a flare 400 units out behind a wall at 279 projects on screen and reads hidden")
    orb = (round(p[0]), round(p[1]))
    SCENE = 0x1A2011B0
    box = lambda c, h=8: ([c[0] - h, c[1] - h, c[2] - h], [c[0] + h, c[1] + h, c[2] + h])

    def case(lines, frames_zw=0, scene_ds=SCENE, cam_=cam, orbs=(orb,)):
        log = list(lines) + [frame_line(f, scene_ds, frames_zw) for f in (100, 115, 130)]
        recs, frames = parse_trace(log)
        steps = {s: {"recs": recs, "frames": frames, "flares": fl, "cam": cam_, "proj": proj, "orbs": list(orbs)}
                 for s in ON}
        return part1(steps, quiet), recs

    def recs_for(ds=SCENE, zen=1, zfn=4, handed=1, last=40, fresh=0, fresh_first=0, centre=flare_pos):
        mn, mx = box(centre)
        return [trace_line(f, 0x0B00 + f, ds, zen, zfn, mn, mx, handed, last, fresh, fresh_first)
                for f in (100, 115, 130)] + \
               [trace_line(f, 0x0C00 + f, ds, zen, zfn, *box(np.array(fl[1]["pos"])), handed, last)
                for f in (100, 115, 130)]

    v, recs = case(recs_for())
    expect(len(recs) == 6 and recs[0]["ds"] == SCENE and recs[0]["last"] == 40 and recs[0]["posn"] == 4,
           "the proxy's line format parses (6 records, ds, pixels, positions)")
    expect(v == "UNEXPLAINED", f"told visible, same ds, Z on, no drops, quad behind the wall: UNEXPLAINED ({v})")
    v, _ = case(recs_for(handed=0, last=0))
    expect(v == "(a) GAME IGNORES", f"never handed an answer: (a) ({v})")
    v, _ = case(recs_for(last=0))
    expect(v == "(a) GAME IGNORES", f"handed 0 pixels yet the orb shows: (a) ({v})")
    v, _ = case(recs_for(ds=0x22220000))
    expect(v == "(b) OTHER DEPTH", f"query draw on another depth surface: (b) ({v})")
    v, _ = case(recs_for(zfn=8))
    expect(v == "(b) OTHER DEPTH", f"query draw with ZFUNC ALWAYS: (b) ({v})")
    v, _ = case(recs_for(zen=0))
    expect(v == "(b) OTHER DEPTH", f"query draw with ZENABLE 0: (b) ({v})")
    v, _ = case(recs_for(), frames_zw=3)
    expect(v == "(c) WALL DROPPED", f"depth-writing drops on the scene ds: (c) ({v})")
    v, _ = case(recs_for(centre=np.array([200.0, 60.0, 10.0])))
    expect(v == "NOT MEASURED", f"a quad off the flare's line of sight, 200 units out, ties no flare ({v})")
    near = flare_pos * 0.6   # on the line of sight, 240 units out: in front of the wall, 160 units from the flare
    lines = [trace_line(f, 0x0B00 + f, SCENE, 1, 4, *box(near, 4), 1, 40) for f in (100, 115, 130)]
    lines += [trace_line(f, 0x0C00 + f, SCENE, 1, 4, *box(np.array(fl[1]["pos"])), 1, 40) for f in (100, 115, 130)]
    v, _ = case(lines)
    expect(v == "QUAD IN FRONT", f"query quad nearer than the wall: QUAD IN FRONT ({v})")
    v, _ = case(recs_for(fresh=1, fresh_first=0))
    expect(v == "STALE QUERYMODE CACHE", f"handed pixels but read 0 fresh: STALE QUERYMODE CACHE ({v})")
    mixed = recs_for()[:2] + recs_for(handed=0, last=0)[2:]
    v, _ = case(mixed)
    expect(v == "MIXED", f"2 of 3 told visible: MIXED ({v})")
    v, _ = case(recs_for(), scene_ds=0)
    expect(v == "NOT MEASURED", f"no scene-ds in the frame lines: NOT MEASURED ({v})")
    moved = np.array([0.0, 700.0, 0.0])
    lines = [trace_line(f, 0x0B00 + f, SCENE, 1, 4, *box(c + moved), 1, 40) for f in (100, 115, 130)
             for c in (flare_pos, np.array(fl[1]["pos"]))]
    v, _ = case(lines)
    expect(v == "NOT MEASURED", f"every quad moved 700 units: no tie space, NOT MEASURED ({v})")
    v, _ = case([])
    expect(v == "NOT MEASURED", f"no traced query: NOT MEASURED ({v})")
    v, _ = case(recs_for(), orbs=((orb[0] + 30, orb[1]),))
    expect(v == "NOT MEASURED", f"an orb 30 px from any flare is no kind-1 orb: NOT MEASURED ({v})")
    # camera-relative positions with the camera away from the origin
    cam2 = (1000.0, 500.0, 50.0, 0.0, 0.0)
    fl2 = make_flares([{"pos": "1400,460,70", "standin": "1"}, {"pos": "1900,800,-50", "standin": "1"}], cam2, proj,
                      gd)
    rel = [trace_line(f, 0x0D00 + f, SCENE, 1, 4, *box(np.array([400.0, -40.0, 20.0])), 1, 40) for f in (1, 2, 3)]
    rel += [trace_line(f, 0x0E00 + f, SCENE, 1, 4, *box(np.array([900.0, 300.0, -100.0])), 1, 40) for f in (1, 2, 3)]
    mode, _ = choose_mode([(parse_trace(rel)[0], fl2, cam2, proj)])
    expect(mode == "camrel", f"camera-relative quads choose camrel ({mode})")
    ndc = [trace_line(f, 0x0F00 + f, SCENE, 1, 4, [p[3] - 0.01, p[4] - 0.01, 0.5], [p[3] + 0.01, p[4] + 0.01, 0.5],
                      1, 40) for f in (1, 2, 3)]
    mode, _ = choose_mode([(parse_trace(ndc)[0], fl, cam, proj)])
    expect(mode == "ndc", f"screen-space quads choose ndc ({mode})")

    # Part 2 on synthetic pictures: a dim gradient, noise, and a 3 px blob at site (191,97) where asked.
    rng = np.random.default_rng(7)
    base = np.tile(np.linspace(0.15, 0.35, GW), (GH, 1))

    def pic(blob, scale=1.0, flip=False):
        a = base + rng.normal(0, 0.01, base.shape)
        if blob:
            a[96:99, 190:193] += 0.5
        a = a * scale
        return a[:, ::-1] if flip else a

    def grabs(blob_on, scale=1.0, flip=False, n=8):
        return {s: [pic(blob_on and s != OFF, scale, flip) for _ in range(n)] for s in STEPS}

    stills_on = {s: pic(s != OFF) for s in STEPS}
    stills_none = {s: pic(False) for s in STEPS}
    site = ((191, 97),)
    _, r = part2(stills_on, grabs(True, 4.0), grabs(True), site, quiet)
    expect(r.get(site[0]) == "REMIX PICTURE", f"blob in the HDR composite and present grabs: REMIX PICTURE ({r})")
    _, r = part2(stills_on, grabs(False, 4.0), grabs(True), site, quiet)
    expect(r.get(site[0]) == "AFTER COMPOSITE", f"blob in the present grabs only: AFTER COMPOSITE ({r})")
    _, r = part2(stills_on, grabs(False), grabs(False), site, quiet)
    expect(r.get(site[0]) == "NOT IN GRABS", f"blob in the stills only: NOT IN GRABS ({r})")
    _, r = part2(stills_none, grabs(True), grabs(True), site, quiet)
    expect(r.get(site[0]) == "NOT REPRODUCED", f"no blob in the stills: NOT REPRODUCED ({r})")
    both = {s: pic(True) for s in STEPS}
    _, r = part2(both, grabs(True), grabs(True), site, quiet)
    expect(r.get(site[0]) == "NOT REPRODUCED", f"blob also with the balls off: NOT REPRODUCED ({r})")
    v, r = part2(stills_on, grabs(True), grabs(True, flip=True), site, quiet)
    expect(v == "NOT MEASURED" and not r, f"present grabs mirrored: NOT MEASURED ({v})")
    v, r = part2(stills_on, grabs(True), grabs(True, n=3), site, quiet)
    expect(v == "NOT MEASURED" and not r, f"3 present grabs per step: NOT MEASURED ({v})")
    v, r = part2(stills_on, grabs(True, n=2), grabs(True), site, quiet)
    expect(r.get(site[0]) == "NOT MEASURED", f"composite grabs missing: the site reads NOT MEASURED ({r})")
    _, r = part2(stills_on, grabs(False), grabs(True), ((100, 140),), quiet)
    expect(r.get((100, 140)) == "NOT REPRODUCED", f"a site away from the blob: NOT REPRODUCED ({r})")

    print("query_trace_rule self-test:", "FAIL" if fails else "PASS", f"({len(fails)} failed)")
    return 1 if fails else 0


def main():
    if sys.argv[1:] == ["--self-test"]:
        sys.exit(self_test())
    ap = argparse.ArgumentParser()
    ap.add_argument("spot", type=pathlib.Path, help="the launch's con_bright_hall spot folder")
    ap.add_argument("--reference", type=pathlib.Path, default=qp.REFERENCE, help="no-Remix con_bright_hall folder")
    a = ap.parse_args()
    score(a.spot, a.reference)


if __name__ == "__main__":
    main()
