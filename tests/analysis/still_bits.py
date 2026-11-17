"""Worker G: does each of Sam's test bits change the picture? (coordinator's bit picture test, Nov 16)

Live (--pid): Sam is teleported once to --teleport and left there; nothing moves him again until the fake case.
For each bit (invisible, godmode, blind) in turn: still "off1", bit on, still "on", bit off, still "off2", with the same
wait before each still. A still is the mean of GRABS full-size grabs of the game window's client area (ffmpeg gdigrab,
the way drive.py's raster() grabs), saved as <tag>-<bit>-<step>.png. Sam's place and the controller's yaw/pitch are
read at every still, so a pose change shows up. Last, the FAKE CASE: Sam is moved FAKE_SHIFT units sideways and one
more still is taken; it must score ABOVE FLOOR against the last off still, or the whole test is VOID.

Score (offline, also --score <dir>): luma of each still, a pixel "changed" when its luma differs by more than
THRESH (0..255 scale). Per bit: floor = changed share off1 vs off2; on = the larger of on vs off1 and on vs off2.
ABOVE FLOOR when on > FLOOR_FACTOR * floor and on > floor + FLOOR_MARGIN; else WITHIN NOISE. The same numbers are
given for the centre box (where the camera behind Sam puts him), and a difference picture <bit>-diff.png marks the
changed pixels of on vs off1 in red. VOID for a bit whose pose moved between its stills (Sam more than 1 unit, yaw or
pitch more than 0.2 degrees) or whose bit did not read back as written.

  still_bits.py --pid N --exe-hash H --out DIR --tag pic --teleport x,y,z
  still_bits.py --score DIR --tag pic
  still_bits.py --selftest
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME = os.path.normpath(os.path.join(HERE, "..", "runtime"))
sys.path.insert(0, RUNTIME)

GRABS = 10
THRESH = 6.0
FLOOR_FACTOR, FLOOR_MARGIN = 2.0, 0.001
BOX = (0.35, 0.65, 0.30, 0.95)   # x0, x1, y0, y1 as shares of the picture
WAIT = 3.0
SETTLE = 15.0
FAKE_SHIFT = 40.0
BITS = ("invisible", "godmode", "blind")
POSE_MOVE, POSE_TURN = 1.0, 0.2


# --- scoring ---------------------------------------------------------------------------------------------------
def luma(img: np.ndarray) -> np.ndarray:
    return img[..., :3].astype(np.float32) @ np.float32([0.2126, 0.7152, 0.0722])


def changed(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.abs(luma(a) - luma(b)) > THRESH


def shares(a: np.ndarray, b: np.ndarray) -> dict:
    m = changed(a, b)
    h, w = m.shape
    x0, x1, y0, y1 = int(BOX[0] * w), int(BOX[1] * w), int(BOX[2] * h), int(BOX[3] * h)
    return {"all": round(float(m.mean()), 5), "box": round(float(m[y0:y1, x0:x1].mean()), 5)}


def above(on: float, floor: float) -> bool:
    return on > FLOOR_FACTOR * floor and on > floor + FLOOR_MARGIN


def pose_moved(poses: list[dict]) -> str | None:
    p0 = poses[0]
    for p in poses[1:]:
        d = math.dist(p0["sam"], p["sam"])
        dyaw = abs((p["yaw"] - p0["yaw"] + 180.0) % 360.0 - 180.0)
        dpitch = abs(p["pitch"] - p0["pitch"])
        if d > POSE_MOVE or dyaw > POSE_TURN or dpitch > POSE_TURN:
            return f"pose moved: Sam {d:.2f} units, yaw {dyaw:.2f}, pitch {dpitch:.2f} degrees"
    return None


def score_bit(off1, on, off2, poses=None, bits_ok=True) -> dict:
    floor, a, b = shares(off1, off2), shares(on, off1), shares(on, off2)
    out = {"floor": floor, "onVsOff1": a, "onVsOff2": b,
           "on": {k: max(a[k], b[k]) for k in a},   # the larger, so one quiet pair cannot hide a change (coordinator)
           "luma": [round(float(luma(x).mean()), 3) for x in (off1, on, off2)]}
    why = None if bits_ok else "bit did not read back as written"
    why = why or (pose_moved(poses) if poses else None)
    if why:
        return dict(out, verdict="VOID", why=why)
    out["verdict"] = "ABOVE FLOOR" if (above(out["on"]["all"], floor["all"]) or above(out["on"]["box"], floor["box"])) \
        else "WITHIN NOISE"
    out["why"] = (f"changed share on {out['on']['all']:.4f} (box {out['on']['box']:.4f}) against off-off "
                  f"{floor['all']:.4f} (box {floor['box']:.4f})")
    return out


def diff_picture(a: np.ndarray, b: np.ndarray, path: str) -> None:
    m = changed(a, b)
    out = (a[..., :3].astype(np.float32) * 0.5).astype(np.uint8)
    out[m] = (255, 0, 0)
    Image.fromarray(out).save(path)


def load(path: str) -> np.ndarray:
    return np.asarray(Image.open(path).convert("RGB"))


def score_dir(out: str, tag: str) -> dict:
    rec = json.load(open(os.path.join(out, f"{tag}.json"), encoding="utf-8"))
    res = {"bits": {}}
    for bit in BITS:
        st = rec.get("bits", {}).get(bit)
        if not st or any(s not in st.get("stills", {}) for s in ("off1", "on", "off2")):
            res["bits"][bit] = {"verdict": "VOID", "why": "stills missing"}
            continue
        imgs = [load(os.path.join(out, st["stills"][s]["file"])) for s in ("off1", "on", "off2")]
        poses = [st["stills"][s]["pose"] for s in ("off1", "on", "off2")]
        res["bits"][bit] = score_bit(*imgs, poses=poses, bits_ok=st.get("bitsOk", False))
        diff_picture(imgs[1], imgs[0], os.path.join(out, f"{tag}-{bit}-diff.png"))
    fk = rec.get("fake")
    last = rec.get("bits", {}).get(BITS[-1], {}).get("stills", {}).get("off2")
    if fk and last and "file" in fk:
        a, b = load(os.path.join(out, last["file"])), load(os.path.join(out, fk["file"]))
        floor = res["bits"].get(BITS[-1], {}).get("floor") or {"all": 0.0, "box": 0.0}
        s = shares(b, a)
        ok = above(s["all"], floor["all"]) or above(s["box"], floor["box"])
        res["fake"] = {"shares": s, "floor": floor, "detected": ok, "samMoved": fk.get("moved")}
        diff_picture(b, a, os.path.join(out, f"{tag}-fake-diff.png"))
    else:
        res["fake"] = {"detected": False, "why": "no fake-case still"}
    if not res["fake"]["detected"]:
        res["verdict"] = "VOID"
        res["why"] = "the fake case (Sam moved sideways) was not detected, so the check cannot show a change"
    else:
        res["verdict"] = "SCORED"
        res["why"] = "; ".join(f"{b}: {r['verdict']}" for b, r in res["bits"].items())
    with open(os.path.join(out, f"{tag}-score.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, indent=1)
    return res


# --- live ------------------------------------------------------------------------------------------------------
def grab(pid: int) -> np.ndarray:
    import drive   # read-only use of its window helpers and ffmpeg path
    hwnd = drive.game_window(pid)
    if not hwnd:
        raise RuntimeError("no game window to grab")
    x, y, w, h = drive.client_rect(hwnd)
    out = subprocess.run([drive.FFMPEG, "-v", "error", "-f", "gdigrab", "-framerate", "25", "-draw_mouse", "0",
                          "-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}", "-i", "desktop",
                          "-frames:v", str(GRABS), "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                         capture_output=True, timeout=60)
    size = w * h * 3
    if len(out.stdout) < GRABS * size:
        raise RuntimeError(f"grab returned {len(out.stdout) // size} of {GRABS} frames of {w}x{h}")
    frames = np.frombuffer(out.stdout, np.uint8, GRABS * size).reshape(GRABS, h, w, 3)
    if not frames.any():
        raise RuntimeError("grab is all black: the game picture did not reach the desktop")
    return np.round(frames.astype(np.float32).mean(axis=0)).astype(np.uint8)


def live(a) -> int:
    import blmem
    import game_bits
    game, proc = blmem.attach(a.pid, a.exe_hash)
    rec = {"pid": a.pid, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "teleport": a.teleport, "bits": {},
           "settings": {"grabs": GRABS, "wait": WAIT, "settle": SETTLE, "fakeShift": FAKE_SHIFT}}
    path = os.path.join(a.out, f"{a.tag}.json")

    def save():
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1)

    def pose():
        controller, pawn = game.player()
        yaw, pitch, _ = game.rotation(controller)
        return {"sam": [round(v, 2) for v in game.location(pawn)], "yaw": round(yaw, 3), "pitch": round(pitch, 3),
                "t": round(time.perf_counter() * 1000, 1)}

    def still(name: str) -> dict:
        img = grab(a.pid)
        f = f"{a.tag}-{name}.png"
        Image.fromarray(img).save(os.path.join(a.out, f))
        return {"file": f, "pose": pose(), "luma": round(float(luma(img).mean()), 3)}

    def write(bit: str, on: bool) -> bool:
        r = game_bits.apply(game, [bit], on)
        rec["bits"][bit].setdefault("writes", []).append(r["bits"][bit])
        return r["ok"]

    code = 0
    try:
        end = time.monotonic() + 60
        while True:
            try:
                game.player()
                break
            except blmem.MemoryError_:
                if time.monotonic() > end:
                    raise
                time.sleep(2)
        for bit in BITS:   # start from all off
            rec["bits"][bit] = {}
            write(bit, False)
        target = [float(v) for v in a.teleport.split(",")]
        _, pawn = game.player()
        rec["teleportFrom"] = [round(v, 1) for v in game.location(pawn)]
        game.set_location(pawn, target)
        time.sleep(SETTLE)
        rec["landed"] = pose()
        for bit in BITS:
            st = rec["bits"][bit]
            st["stills"], ok = {}, True
            time.sleep(WAIT)
            st["stills"]["off1"] = still(f"{bit}-off1")
            ok &= write(bit, True)
            time.sleep(WAIT)
            st["stills"]["on"] = still(f"{bit}-on")
            ok &= write(bit, False)
            time.sleep(WAIT)
            st["stills"]["off2"] = still(f"{bit}-off2")
            st["bitsOk"] = bool(ok)
            save()
        # fake case: Sam moved sideways must be seen
        _, pawn = game.player()
        here = game.location(pawn)
        game.set_location(pawn, [here[0] + FAKE_SHIFT, here[1], here[2]])
        time.sleep(WAIT)
        rec["fake"] = still("fake-moved")
        rec["fake"]["moved"] = round(math.dist(here, rec["fake"]["pose"]["sam"]), 1)
    except Exception as e:   # noqa: BLE001 - keep what was taken
        rec["error"] = f"{type(e).__name__}: {e}"
        code = 1
    finally:
        save()
        proc.close()
    if code == 0:
        res = score_dir(a.out, a.tag)
        print(res["verdict"], res["why"])
    return code


# --- self-test -------------------------------------------------------------------------------------------------
def selftest() -> int:
    import tempfile
    rng = np.random.default_rng(7)
    base = rng.integers(20, 200, (180, 320, 3)).astype(np.uint8)

    def noisy(img, amp=2):
        return np.clip(img.astype(np.int16) + rng.integers(-amp, amp + 1, img.shape), 0, 255).astype(np.uint8)

    def with_sam(img, present=True):
        out = img.copy()
        if not present:
            return out
        out[100:160, 150:170] = 10   # a dark figure in the centre box
        return out

    p0 = {"sam": [0, 0, 0], "yaw": 10.0, "pitch": -5.0}
    poses = [p0, p0, p0]
    results = []

    def check(label, got, want):
        ok = got == want
        results.append(ok)
        print(("PASS  " if ok else "FAIL  ") + f"{label}: {got}" + ("" if ok else f" (expected {want})"))

    off = with_sam(base)
    check("bit changes nothing", score_bit(noisy(off), noisy(off), noisy(off), poses)["verdict"], "WITHIN NOISE")
    check("bit hides Sam", score_bit(noisy(off), noisy(with_sam(base, False)), noisy(off), poses)["verdict"], "ABOVE FLOOR")
    r = score_bit(noisy(off), noisy(with_sam(base, False)), noisy(off), poses)
    check("hidden Sam share is the figure's area", round(r["on"]["all"], 3), round(60 * 20 / (180 * 320), 3))
    flick = noisy(off); flick[0:40, 0:320] = 250   # a light that flickers in off2 only: floor rises
    check("flicker in the off steps raises the floor", score_bit(noisy(off), noisy(off), flick, poses)["verdict"], "WITHIN NOISE")
    moved = [p0, dict(p0, sam=[3, 0, 0]), p0]
    check("pose moved -> VOID", score_bit(noisy(off), noisy(off), noisy(off), moved)["verdict"], "VOID")
    turned = [p0, p0, dict(p0, yaw=10.5)]
    check("camera turned -> VOID", score_bit(noisy(off), noisy(off), noisy(off), turned)["verdict"], "VOID")
    check("bit not read back -> VOID", score_bit(noisy(off), noisy(off), noisy(off), poses, bits_ok=False)["verdict"], "VOID")
    tiny = off.copy(); tiny[120:122, 160:162] = 255   # 4 pixels: below the margin
    check("4-pixel change is noise", score_bit(noisy(off), noisy(tiny), noisy(off), poses)["verdict"], "WITHIN NOISE")
    # A change that only one pair shows clearly: off1 has a dark patch X (576 px, 0.010) that on and off2 lack, so the
    # floor is 0.010; the bit changes patch C (864 px, 0.015). on vs off1 = X + C = 0.025, on vs off2 = C = 0.015.
    # The smaller (0.015) is under 2 x floor and would read WITHIN NOISE; the larger (0.025) must read ABOVE FLOOR.
    o1 = off.copy(); o1[0:18, 0:32] = 0
    on_c = off.copy(); on_c[20:47, 0:32] = 255
    r = score_bit(noisy(o1), noisy(on_c), noisy(off), poses)
    check("one-pair change: shares (off1, off2, floor)", (round(r["onVsOff1"]["all"], 3), round(r["onVsOff2"]["all"], 3),
                                                          round(r["floor"]["all"], 3)), (0.025, 0.015, 0.01))
    check("one-pair change is caught", r["verdict"], "ABOVE FLOOR")

    # whole directory: invisible hides Sam, the others change nothing, fake case shifted picture
    with tempfile.TemporaryDirectory(dir=os.environ.get("TEMP")) as d:
        def put(name, img):
            Image.fromarray(img).save(os.path.join(d, f"t-{name}.png"))
            return {"file": f"t-{name}.png", "pose": p0}
        rec = {"bits": {}}
        for bit in BITS:
            on_img = with_sam(base, bit != "invisible")
            rec["bits"][bit] = {"bitsOk": True, "stills": {"off1": put(f"{bit}-off1", noisy(off)),
                                "on": put(f"{bit}-on", noisy(on_img)), "off2": put(f"{bit}-off2", noisy(off))}}
        rec["fake"] = put("fake", noisy(np.roll(off, 25, axis=1)))
        json.dump(rec, open(os.path.join(d, "t.json"), "w"))
        res = score_dir(d, "t")
        check("dir: invisible", res["bits"]["invisible"]["verdict"], "ABOVE FLOOR")
        check("dir: godmode", res["bits"]["godmode"]["verdict"], "WITHIN NOISE")
        check("dir: fake detected", res["fake"]["detected"], True)
        check("dir: scored", res["verdict"], "SCORED")
        check("dir: diff picture written", os.path.exists(os.path.join(d, "t-invisible-diff.png")), True)
        rec["fake"] = put("fake", noisy(off))   # a fake case that did not move anything
        json.dump(rec, open(os.path.join(d, "t.json"), "w"))
        check("dir: undetected fake -> VOID", score_dir(d, "t")["verdict"], "VOID")
    print(f"{sum(results)} of {len(results)} pass")
    return 0 if all(results) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--score")
    ap.add_argument("--pid", type=int)
    ap.add_argument("--exe-hash", action="append", default=[])
    ap.add_argument("--out")
    ap.add_argument("--tag", default="pic")
    ap.add_argument("--teleport")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.score:
        res = score_dir(a.score, a.tag)
        print(json.dumps(res, indent=1))
        return 0
    if not (a.pid and a.exe_hash and a.out and a.teleport):
        ap.error("--pid, --exe-hash, --out and --teleport are needed")
    os.makedirs(a.out, exist_ok=True)
    return live(a)


if __name__ == "__main__":
    sys.exit(main())
