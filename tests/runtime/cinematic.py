"""Records a level's opening cutscene as numbers: per screen frame, brightness and the shares of near-black,
near-white and full-white pixels, matched to the game frame and camera position the proxy logged at that moment.

Started by verify_runtime.ps1 while the game loads. It waits for the level to start (the proxy's "AutoRaytracing ->
on" line for this process, or with --signal camera the first real camera in the proxy's frame log, which also works
without Remix), then grabs the screen until the camera has stood still for --settle seconds (the cutscene handed over
to the normal play camera) or --seconds pass. The cutscene starts at the first logged camera after the signal and ends
where the camera settles; both are found from the camera numbers, not from the picture.

Screen frames are timestamped on arrival with time.perf_counter, which reads the same counter as the proxy's frame log
(QueryPerformanceCounter in ms), so each screen frame maps to the last game frame logged before it.

Writes to --out: cinematic.csv (one row per screen frame), cinematic.json (start, end, summary), cinematic.raw
(640x360 8-bit grey frames, for re-analysis)."""
import argparse
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from drive import FFMPEG, files_under, set_ini, user32, written_since  # noqa: E402

W, H, FPS = 640, 360, 30
NEAR_BLACK, NEAR_WHITE, FULL_WHITE = 8, 230, 250   # of 255; near-black and full-white as in switch_report.py


class Growing:
    """New complete lines of a file that keeps growing."""
    def __init__(self, path: str):
        self.path, self.pos, self.rest = path, 0, b""
        self.pos = os.path.getsize(path) if os.path.exists(path) else 0

    def lines(self) -> list[str]:
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self.pos:   # the file was started over
            self.pos, self.rest = 0, b""
        if size == self.pos:
            return []
        with open(self.path, "rb") as fh:
            fh.seek(self.pos)
            data = fh.read(size - self.pos)
        self.pos = size
        data = self.rest + data
        parts = data.split(b"\n")
        self.rest = parts.pop()
        return [p.decode("latin-1").rstrip("\r") for p in parts]


def camera_row(line: str):
    """(frame, ms, (x, y, z) or None) from one d3d9_proxy_frames.csv row; None for other lines."""
    row = next(csv.reader([line]), [])
    try:
        frame, ms = int(row[0]), float(row[1])
    except (ValueError, IndexError):
        return None
    cam = None
    for pub in (row[6] if len(row) > 6 else "").split("|"):
        p = pub.split(":")
        if len(p) >= 9 and p[2] == "0":
            xyz = tuple(map(float, p[3:6]))
            if any(abs(v) > 1e-3 for v in xyz):
                cam = xyz
            break
    return frame, ms, cam


def find_span(rows: list, settle_s: float, after_ms: float):
    """(start row, end row) of the cutscene: the first real camera at or after after_ms, and the first frame from
    which the camera then stays within 1 unit for settle_s seconds. end is None while it has not settled."""
    cams = [r for r in rows if r[2] is not None and r[1] >= after_ms]
    if not cams:
        return None, None
    for i, r in enumerate(cams):
        j = i
        while j + 1 < len(cams) and math.dist(cams[j + 1][2], r[2]) < 1.0:
            j += 1
        if cams[j][1] - r[1] >= settle_s * 1000:
            return cams[0], r
    return cams[0], None


def letterbox(frames: np.ndarray) -> tuple[int, int]:
    """(top, bottom) rows that are near-black in every frame: the cutscene's letterbox bars."""
    dark = frames.max(axis=(0, 2)) < NEAR_BLACK
    top = int(np.argmin(dark)) if not dark.all() else len(dark)
    bottom = int(np.argmin(dark[::-1])) if not dark.all() else 0
    return top, bottom


def draw_dump(ini: str, system: str, out: str, timeout: float = 8.0) -> dict:
    """One proxy draw dump in the middle of the cutscene (worker F, Nov 16): [Proxy] CaptureDraws=1 until a new
    framedump\\f<frame>_draws.tsv is written, then 0. The new draw and light dumps, and any new shaders in psdump and
    vsdump, are copied to out\\drawdump. Files are found by write time: frame numbers repeat between launches."""
    folders = ("framedump", "psdump", "vsdump")
    before = {f: files_under(os.path.join(system, f)) for f in folders}
    rec = {"askedMs": round(time.perf_counter() * 1000.0, 1), "askedAt": time.strftime("%H:%M:%S")}
    try:
        set_ini(ini, "Proxy", "CaptureDraws", "1")
        t = time.perf_counter()
        draws = []
        while time.perf_counter() - t < timeout and not draws:
            time.sleep(0.1)
            draws = [n for n in written_since(os.path.join(system, "framedump"), before["framedump"])
                     if n.endswith("_draws.tsv")]
    finally:
        set_ini(ini, "Proxy", "CaptureDraws", "0")
    time.sleep(1.0)   # let the proxy finish the file and the shaders it dumps with it
    if not draws:
        rec["error"] = f"no new _draws.tsv within {timeout:.0f} s"
        return rec
    rec["frame"] = int(draws[0].split("_")[0].lstrip("f"))
    rec["files"] = []
    for f in folders:
        for n in written_since(os.path.join(system, f), before[f]):
            dest = os.path.join(out, "drawdump", f, n)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(os.path.join(system, f, n), dest)
            rec["files"].append(os.path.join(f, n))
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, default=0, help="the game; 0 = whichever game logs the line first (one game "
                                                         "at a time, and the recorder starts before the game exists)")
    ap.add_argument("--log", required=True, help="the game's d3d9_proxy.log")
    ap.add_argument("--frames", required=True, help="the game's d3d9_proxy_frames.csv")
    ap.add_argument("--out", required=True)
    ap.add_argument("--signal", choices=["autort", "camera"], default="autort")
    ap.add_argument("--wait", type=float, default=480.0, help="seconds to wait for the level to start")
    ap.add_argument("--seconds", type=float, default=60.0, help="longest recording")
    ap.add_argument("--settle", type=float, default=1.0)
    ap.add_argument("--dump-at", type=float, default=0.0, help="seconds after the cutscene starts for one proxy draw "
                                                                 "dump (0 = none); needs --ini and --system")
    ap.add_argument("--ini", help="the game's d3d9_proxy.ini"); ap.add_argument("--system", help="the game's SYSTEM folder")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    log, frames = Growing(a.log), Growing(a.frames)
    rows: list = []
    marker = f"[pid {a.pid}] lightpub: AutoRaytracing -> on" if a.pid else "] lightpub: AutoRaytracing -> on"
    t_wait = time.perf_counter()
    signal_ms = None
    while signal_ms is None:
        if time.perf_counter() - t_wait > a.wait:
            print("CINEMATIC ERROR level did not start"); return 1
        for line in frames.lines():
            r = camera_row(line)
            if r:
                rows.append(r)
                if a.signal == "camera" and r[2] is not None:
                    signal_ms = r[1]
        if a.signal == "autort" and any(marker in line for line in log.lines()):
            signal_ms = time.perf_counter() * 1000.0
        time.sleep(0.03)

    w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    proc = subprocess.Popen([FFMPEG, "-v", "error", "-f", "gdigrab", "-framerate", str(FPS), "-draw_mouse", "0",
                             "-offset_x", "0", "-offset_y", "0", "-video_size", f"{w}x{h}", "-i", "desktop",
                             "-vf", f"scale={W}:{H}:flags=area,format=gray", "-f", "rawvideo", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    shots: list = []   # (perf ms on arrival, mean, nearBlack, nearWhite, fullWhite)
    raw = open(os.path.join(a.out, "cinematic.raw"), "wb")

    def reader():
        size = W * H
        while True:
            buf = b""
            while len(buf) < size:
                chunk = proc.stdout.read(size - len(buf))
                if not chunk:
                    return
                buf += chunk
            t = time.perf_counter() * 1000.0
            f = np.frombuffer(buf, np.uint8)
            raw.write(buf)
            shots.append((t, float(f.mean()) / 255.0, float((f < NEAR_BLACK).mean()),
                          float((f >= NEAR_WHITE).mean()), float((f >= FULL_WHITE).mean())))
    th = threading.Thread(target=reader, daemon=True)
    th.start()
    t0 = time.perf_counter()
    end = None
    dump: dict = {}
    dumper = None
    while time.perf_counter() - t0 < a.seconds:
        time.sleep(0.1)
        rows += [r for r in map(camera_row, frames.lines()) if r]
        first, end = find_span(rows, a.settle, signal_ms)
        # The draw dump runs beside the grab, so the screen record has no gap around the dumped frame.
        if a.dump_at > 0 and dumper is None and first and time.perf_counter() * 1000.0 >= first[1] + a.dump_at * 1000:
            dump["cutsceneStartFrame"] = first[0]
            dumper = threading.Thread(target=lambda: dump.update(draw_dump(a.ini, a.system, a.out)), daemon=True)
            dumper.start()
        if end is not None and time.perf_counter() * 1000.0 > end[1] + a.settle * 1000 + 1000:
            break
    if dumper:
        dumper.join(15.0)
    proc.kill(); proc.wait(); th.join(2.0); raw.close()
    rows += [r for r in map(camera_row, frames.lines()) if r]
    start, end = find_span(rows, a.settle, signal_ms)

    times = [r[1] for r in rows]
    out_rows = []
    for i, (t, mean, nb, nw, fw) in enumerate(shots):
        k = int(np.searchsorted(times, t, side="right")) - 1
        r = rows[k] if k >= 0 else None
        out_rows.append({"shot": i, "ms": round(t, 1), "gameFrame": r[0] if r else None,
                         "camX": round(r[2][0], 1) if r and r[2] else None,
                         "camY": round(r[2][1], 1) if r and r[2] else None,
                         "camZ": round(r[2][2], 1) if r and r[2] else None,
                         "brightness": round(mean, 4), "nearBlack": round(nb, 4), "nearWhite": round(nw, 4),
                         "fullWhite": round(fw, 4),
                         "inCutscene": bool(start and end and start[1] <= t < end[1])})
    # The stats above are full-frame. Letterbox bars (rows black in every cutscene frame) would sit in nearBlack and
    # dilute the others, so the same stats are also given for the active picture between the bars.
    frames_raw = np.fromfile(os.path.join(a.out, "cinematic.raw"), np.uint8)
    frames_raw = frames_raw[: frames_raw.size // (W * H) * W * H].reshape(-1, H, W)
    ids = [r["shot"] for r in out_rows if r["inCutscene"] and r["shot"] < len(frames_raw)]
    top, bottom = letterbox(frames_raw[ids]) if ids else (0, 0)
    for r in out_rows:
        if r["shot"] < len(frames_raw):
            act = frames_raw[r["shot"], top:H - bottom]
            r.update(activeBrightness=round(float(act.mean()) / 255.0, 4), activeNearBlack=round(float((act < NEAR_BLACK).mean()), 4),
                     activeNearWhite=round(float((act >= NEAR_WHITE).mean()), 4), activeFullWhite=round(float((act >= FULL_WHITE).mean()), 4))
    with open(os.path.join(a.out, "cinematic.csv"), "w", newline="") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(out_rows[0]) if out_rows else ["shot"])
        wr.writeheader(); wr.writerows(out_rows)
    inside = [r for r in out_rows if r["inCutscene"]]
    summary = {
        "signal": a.signal, "signalMs": round(signal_ms, 1),
        "startGameFrame": start[0] if start else None, "startMs": round(start[1], 1) if start else None,
        "endGameFrame": end[0] if end else None, "endMs": round(end[1], 1) if end else None,
        "seconds": round((end[1] - start[1]) / 1000.0, 2) if start and end else None,
        "startShot": inside[0]["shot"] if inside else None, "endShot": inside[-1]["shot"] if inside else None,
        "shots": len(out_rows), "shotsInCutscene": len(inside), "frameSize": [W, H],
        "region": "brightness/nearBlack/nearWhite/fullWhite: full frame (whole screen, any letterbox bars included); "
                  "active*: rows between the letterbox bars only",
        "letterboxRows": {"top": top, "bottom": bottom, "activeFraction": round((H - top - bottom) / H, 4)},
        "thresholds": {"nearBlack": f"< {NEAR_BLACK}/255", "nearWhite": f">= {NEAR_WHITE}/255",
                       "fullWhite": f">= {FULL_WHITE}/255"},
    }
    if inside:
        for k in ("brightness", "nearWhite", "fullWhite", "nearBlack",
                  "activeBrightness", "activeNearWhite", "activeFullWhite", "activeNearBlack"):
            v = np.array([r[k] for r in inside])
            summary[k] = {"mean": round(float(v.mean()), 4), "median": round(float(np.median(v)), 4),
                          "max": round(float(v.max()), 4), "min": round(float(v.min()), 4)}
    if a.dump_at > 0:
        # Where the dumped frame fell: inside the cutscene or not, and the screen shots logged on that game frame.
        if "frame" in dump:
            on = [r for r in out_rows if r["gameFrame"] == dump["frame"]]
            dump["inCutscene"] = bool(start and end and start[0] <= dump["frame"] < end[0])
            dump["shots"] = [r["shot"] for r in on]
            dump["shotBrightness"] = [r["brightness"] for r in on]
        elif not dump:
            dump["error"] = "the cutscene never started, so no dump was asked"
        summary["drawDump"] = dump
    with open(os.path.join(a.out, "cinematic.json"), "w") as fh:
        json.dump(summary, fh, indent=1)
    if a.dump_at > 0:
        print("DRAWDUMP", json.dumps({k: v for k, v in dump.items() if k != "files"}), flush=True)
    ok = bool(start and end and inside)
    print("CINEMATIC", "ok" if ok else "ERROR no cutscene found", json.dumps({k: summary[k] for k in
          ("startGameFrame", "endGameFrame", "seconds", "shotsInCutscene")}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
