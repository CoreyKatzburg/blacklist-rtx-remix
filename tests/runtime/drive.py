"""Live part of verify_runtime.ps1: measures one spot in the running game (or records a checkpoint).

  drive.py capture --pid N --exe-hash H [...] --out checkpoint.json
      Records the map, Sam's position and the camera direction right after a checkpoint loaded.
  drive.py spot --pid N --exe-hash H [...] --spot spot.json --checkpoint checkpoint.json --system DIR --root DIR --out DIR
      At the checkpoint's spawn: 24-stop step-and-hold sweep (8 yaws x 3 pitches, 15+ still frames and one image
      dump per stop), a 360 degree smooth relative-mouse pan, a final stop back at the start, an optional gunfire
      burst window, and the goggle check. Camera pose is driven via real relative mouse input (SendInput
      MOUSEEVENTF_MOVE) closed-loop against game rotation, because Blacklist overwrites direct memory writes.
Screen frames come from ffmpeg gdigrab (works with the monitors off; ddagrab does not) with a hard timeout.
"""
from __future__ import annotations

import argparse
import ctypes
import collections
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blmem  # noqa: E402
import gate  # noqa: E402  (file helpers)

FFMPEG = r"d:\ffmpeg\bin\ffmpeg.exe"
CAP_W, CAP_H = 320, 180
RASTER_GRABS = 10   # full-size grabs averaged per reference picture (snow, dust and dither average out; worker F2)


# --- append-only files the game writes ----------------------------------------------------------------------
class Tail:
    """One growing file (telemetry JSONL, proxy CSV, proxy log): remembers where this spot started."""

    def __init__(self, candidates: list[str], json_lines: bool):
        self.candidates, self.json = candidates, json_lines
        self.path = next((p for p in candidates if os.path.exists(p)), None)
        self.start = os.path.getsize(self.path) if self.path else 0
        self.start_frame = self._newest() if self.path else 0

    def _newest(self) -> int:
        """Frame number of the newest complete line (0 if none, or not a frame file)."""
        with open(self.path, "rb") as f:
            f.seek(max(0, os.path.getsize(self.path) - 65536))
            lines = f.read().splitlines()
        for line in reversed(lines[1:] if len(lines) > 1 else lines):
            try:
                return int(json.loads(line)["frame"]) if self.json else int(line.split(b",")[0])
            except (ValueError, KeyError, IndexError, TypeError):
                continue
        return 0

    def last_frame(self) -> int:
        if not self.path:
            self.path = next((p for p in self.candidates if os.path.exists(p)), None)   # appeared after the start
            if not self.path:
                return 0
        return self._newest()

    def _from(self) -> int:
        """Where this spot's part starts; 0 if the writer started the file over since (Remix truncates it at each
        launch): the file got shorter, or its newest frame is older than the newest one when the spot began."""
        if os.path.getsize(self.path) < self.start or self._newest() < self.start_frame:
            self.start, self.start_frame = 0, 0
        return self.start

    def new_text(self) -> str:
        if not self.path:
            return ""
        with open(self.path, "rb") as f:
            f.seek(self._from())
            return f.read().decode("latin-1")

    def copy_to(self, dest: str, whole: bool = False) -> str | None:
        """This spot's part of the file, or with whole=True all of it (the files restart at each launch)."""
        if not self.path:
            return None
        with open(self.path, "rb") as f, open(dest, "wb") as out:
            f.seek(0 if whole else self._from()); shutil.copyfileobj(f, out)
        return os.path.basename(dest)


def set_ini(path: str, section: str, key: str, value: str) -> None:
    """Sets key=value in [section] of the game folder's d3d9_proxy.ini (the proxy re-reads it about once a second)."""
    with open(path, encoding="latin-1", newline="") as f:
        text = f.read()
    line = f"{key} = {value}" if section == "RemixConfig" else f"{key}={value}"
    pattern = re.compile(r"(?m)^" + re.escape(key) + r"[ \t]*=[^\r\n]*")   # keeps the file's CRLF line ends
    if pattern.search(text):
        text = pattern.sub(line, text, count=1)
    elif f"[{section}]" in text:
        text = text.replace(f"[{section}]", f"[{section}]\r\n{line}", 1)
    else:
        text += f"\r\n[{section}]\r\n{line}\r\n"
    write_ini(path, text)


def remove_ini_key(path: str, key: str) -> None:
    with open(path, encoding="latin-1", newline="") as f:
        text = f.read()
    updated = re.sub(r"(?m)^" + re.escape(key) + r"[ \t]*=[^\r\n]*(\r?\n)?", "", text)
    if updated != text:
        write_ini(path, updated)


def write_ini(path: str, text: str) -> None:
    """Replaces the ini in one step. Windows refuses the replace while the proxy has the file open for its
    once-a-second re-read, so this retries for up to 2 s."""
    tmp = path + ".gate.tmp"
    with open(tmp, "w", encoding="latin-1", newline="") as f:
        f.write(text)
    deadline = time.monotonic() + 2.0
    while True:
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                os.remove(tmp)
                raise
            time.sleep(0.02)

# --- window, keys, screen ------------------------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None


def game_window(pid: int) -> int:
    found = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
            found.append(hwnd)
        return True
    user32.EnumWindows(proc(cb), None)
    return found[0] if found else 0


def front(hwnd: int) -> bool:
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return True
    a = user32.GetWindowThreadProcessId(ctypes.c_void_p(fg), None)
    me = ctypes.windll.kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(me, a, True); user32.ShowWindow(ctypes.c_void_p(hwnd), 9)
    user32.SetForegroundWindow(ctypes.c_void_p(hwnd)); user32.AttachThreadInput(me, a, False)
    time.sleep(0.3)
    return user32.GetForegroundWindow() == hwnd


SCAN = {"F9": 0x43, "F10": 0x44, "F11": 0x57, "F12": 0x58, "F": 0x21, "G": 0x22, "N": 0x31, "X": 0x2D, "SPACE": 0x39}

# Switch test variants (spots.json "switches" overrides): each changes exactly one thing against "base".
SWITCHES = [
    {"name": "base"},
    # Remix keeps the last [RemixConfig] value it was sent, so those variants also name the rtx.conf value to send back.
    {"name": "fog_off", "ini": [["RemixConfig", "rtx.volumetrics.enable", "False"]],
     "restore": [["RemixConfig", "rtx.volumetrics.enable", "True"]]},
    {"name": "flare_balls_off", "ini": [["Lights", "FlareStandIns", "0"]]},
    {"name": "fill_lights_off", "ini": [["Lights", "Fill", "0"]]},
    {"name": "game_postfx_off", "ini": [["Proxy", "HidePostFx", "1"]]},
    {"name": "goggles_on", "goggles": True},
    {"name": "base_again"},
]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_void_p)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("vk", ctypes.c_ushort), ("scan", ctypes.c_ushort), ("flags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("extra", ctypes.c_void_p)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUT_UNION)]


def tap(key: str) -> None:
    for up in (0, 2):
        i = _INPUT(type=1, u=_INPUT_UNION(ki=_KEYBDINPUT(0, SCAN[key], 0x8 | up, 0, None)))
        user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(_INPUT))
        time.sleep(0.12)


def mouse_move_rel(dx: int, dy: int) -> None:
    if dx == 0 and dy == 0:
        return
    i = _INPUT(type=0, u=_INPUT_UNION(mi=_MOUSEINPUT(int(dx), int(dy), 0, 0x0001, 0, None)))
    user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(_INPUT))


def mouse_fire_burst(shots: int = 5, interval: float = 0.12) -> None:
    for _ in range(shots):
        down = _INPUT(type=0, u=_INPUT_UNION(mi=_MOUSEINPUT(0, 0, 0, 0x0002, 0, None)))
        up = _INPUT(type=0, u=_INPUT_UNION(mi=_MOUSEINPUT(0, 0, 0, 0x0004, 0, None)))
        user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(_INPUT))
        time.sleep(0.04)
        user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(_INPUT))
        time.sleep(interval)
def start_capture(path: str, frames: int = 0, fps: int = 30, seconds: float = 0.0) -> subprocess.Popen:
    """Primary monitor, scaled to 320x180 gray, raw; stops after `frames` frames or `seconds` seconds.
    gdigrab keeps working while the monitors sleep (ddagrab hangs)."""
    w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
    limit = ["-t", f"{seconds:.2f}"] if seconds else ["-frames:v", str(frames)]
    return subprocess.Popen([FFMPEG, "-v", "error", "-y", "-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "0",
                             "-offset_x", "0", "-offset_y", "0", "-video_size", f"{w}x{h}", "-i", "desktop", *limit,
                             "-vf", f"scale={CAP_W}:{CAP_H}:flags=area,format=gray", "-f", "rawvideo", path],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def client_rect(hwnd: int) -> tuple[int, int, int, int]:
    """(left, top, width, height) on the screen of the window's client area: the game picture without title bar or
    borders."""
    import ctypes.wintypes as wt
    r, pt = wt.RECT(), wt.POINT(0, 0)
    user32.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
    user32.ClientToScreen(ctypes.c_void_p(hwnd), ctypes.byref(pt))
    return pt.x, pt.y, r.right - r.left, r.bottom - r.top


def average_grabs(raw: bytes, w: int, h: int, n: int) -> tuple[np.ndarray, list[float]]:
    """The mean of n RGB24 screen grabs as display-encoded floats in 0..1 (u8 / 255, NOT turned linear: Remix's
    <tag>_final.pfm is gamma-encoded with ModernFilter=1, so both pictures must be compared in the same units; worker
    F2, Nov 15), and each grab's mean Rec.709 luma in the same units, so a flash during the grabs shows up."""
    size = w * h * 3
    if len(raw) < n * size:
        raise RuntimeError(f"screen grab returned {len(raw) // size} of {n} frames of {w}x{h}")
    frames = np.frombuffer(raw, np.uint8, n * size).reshape(n, h, w, 3)
    acc = np.zeros((h, w, 3), np.float32)   # exact: at most 10 * 255 per pixel
    lumas = []
    for f in frames:
        acc += f
        lumas.append(round(float((f.astype(np.float32) @ np.float32([0.2126, 0.7152, 0.0722])).mean()) / 255.0, 4))
    return acc / np.float32(n * 255.0), lumas


def recent_telemetry(path: str | None, after_frame: int, tail_bytes: int = 2 << 20) -> list[dict]:
    """Remix telemetry records (one JSON line per frame, ~26 KB each) newer than after_frame, from the file's end."""
    if not path or not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        start = max(0, os.path.getsize(path) - tail_bytes)
        f.seek(start)
        lines = f.read().splitlines()[1 if start else 0:]   # a read from the middle starts inside a line
    out = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if int(rec.get("frame", -1)) > after_frame:
            out.append(rec)
    return out


def chained_dll(log_text: str, pid: int) -> tuple[str, str] | None:
    """(source, path) of the d3d9 the proxy handed the game's drawing to in process `pid`, from the proxy log line
    'chained <source> DLL: <path>'; None when that process logged none."""
    found = re.findall(rf"\[pid {pid}\] chained (.+?) DLL: ([^\r\n]+)", log_text)
    return (found[-1][0], found[-1][1].strip()) if found else None


def check_renderer(chained: tuple[str, str] | None, no_remix: bool) -> str:
    """'remix' or 'system' for the chained DLL; raises when it is not the one this run is meant to measure, so a
    no-Remix reference can never be recorded through Remix (or a Remix run through the plain driver)."""
    if not chained:
        raise RuntimeError("the proxy log has no 'chained ... DLL' line for this game process")
    path = chained[1].replace("/", "\\").lower()
    kind = "system" if path.endswith(("\\syswow64\\d3d9.dll", "\\system32\\d3d9.dll")) else \
        "remix" if path.endswith("\\d3d9_remix.dll") else "unknown"
    if kind != ("system" if no_remix else "remix"):
        raise RuntimeError(f"the game drew through {chained[1]} ({chained[0]}); this run needs "
                           f"{'Windows d3d9 (no Remix)' if no_remix else 'Remix'}")
    return kind


def proxy_camera_dirs(path: str, since_ms: float) -> list[tuple[float, tuple]]:
    """(ms, camera forward vector) of the game frames the proxy logged at or after since_ms. The ms column is
    QueryPerformanceCounter time, the same clock as time.perf_counter."""
    with open(path, "rb") as f:
        f.seek(max(0, os.path.getsize(path) - 524288))
        lines = f.read().decode("latin-1").splitlines()[1:]
    out = []
    for line in lines:
        row = line.split(",", 6)
        try:
            ms = float(row[1])
        except (ValueError, IndexError):
            continue
        if ms < since_ms or len(row) < 7:
            continue
        for pub in row[6].split("|"):
            p = pub.split(":")
            if len(p) >= 9 and p[2] == "0":
                out.append((ms, tuple(map(float, p[6:9]))))
                break
    return out


def first_change(shots, t0: float, base, threshold: float) -> float | None:
    """Arrival time of the first screen shot after t0 whose mean absolute difference from `base` exceeds `threshold`
    (grey levels of 255)."""
    for t, img in shots:
        if t > t0 and float(np.abs(img - base).mean()) > threshold:
            return t
    return None


def first_turned(dirs, t0: float, degrees: float = 0.5) -> float | None:
    """Time of the first game frame after t0 whose camera points more than `degrees` away from the last one before."""
    before = [d for t, d in dirs if t < t0]
    if not before:
        return None
    ref = np.array(before[-1])
    for t, d in dirs:
        if t >= t0 and math.degrees(math.acos(max(-1.0, min(1.0, float(np.dot(ref, d)))))) > degrees:
            return t
    return None


# --- one spot ------------------------------------------------------------------------------------------------
class Spot:
    def __init__(self, a, spot: dict, checkpoint: dict):
        self.a, self.spot, self.cp = a, spot, checkpoint
        self.out = a.out
        os.makedirs(os.path.join(self.out, "frames"), exist_ok=True)
        os.makedirs(os.path.join(self.out, "gate"), exist_ok=True)
        s, r = a.system, a.root
        self.ini = os.path.join(s, "d3d9_proxy.ini")
        self.telemetry = Tail([os.path.join(d, "rtx_telemetry.jsonl") for d in (s, os.path.join(s, ".trex"), r)], True)
        self.proxy = Tail([os.path.join(s, "d3d9_proxy_frames.csv")], False)
        self.pgate = Tail([os.path.join(s, "d3d9_proxy_gate.csv")], False)
        self.log = Tail([os.path.join(s, "d3d9_proxy.log")], False)
        # worker B's light publishing trace (frame,node,id,change,old,new), one file per game session
        self.lptrace = Tail([os.path.join(s, "lightpub_trace.csv")], False)
        self.dump_dirs = [os.path.join(d, "gate") for d in (s, os.path.join(s, ".trex"), r)]
        self.windows: list[dict] = []
        self.game, self.process = blmem.attach(a.pid, a.exe_hash)
        self.relinks: list[dict] = []
        self._link_lock = threading.Lock()
        self.controller, self.pawn = self.wait_player(checkpoint)
        self.base_yaw = float(spot.get("baseYaw", checkpoint["yaw"]))
        self.base_pitch = float(spot.get("basePitch", checkpoint["pitch"]))
        self.yaw = self.pitch = 0.0       # requested, relative to the base
        self.counts_per_deg_x = float(spot.get("mouseCountsPerDegX", 14.0))
        self.counts_per_deg_y = float(spot.get("mouseCountsPerDegY", -14.0))
        self.calibration: dict = {}
        self._sent: tuple | None = None   # (rotation read when the last correction was sent, time sent)
        self.home: list[float] | None = None   # where Sam stands for this spot (spawn, or where the teleport left him)
        self.no_remix = bool(getattr(a, "no_remix", False))   # reference run: the game draws through Windows d3d9

    def sam_moved(self, where: str) -> float | None:
        """Sideways distance Sam has moved from `home`; raises past 150 units. Green hallway, Nov 15: enemies
        found Sam after the teleport, he ended up 3000 units away, the game swung the camera mid-stop, and the
        stops were recorded at the wrong place with a pose error of 166 degrees."""
        if self.home is None:
            return None
        loc = self.game.location(self.pawn)
        d = float(np.hypot(loc[0] - self.home[0], loc[1] - self.home[1]))
        if d > 150:
            raise RuntimeError(f"Sam moved {d:.0f} units from his spot by {where} (enemy contact, or the game "
                               f"moved him); the stops after this would not be at the spot")
        return round(d, 1)

    def counters(self) -> dict:
        return {"telemetry": self.telemetry.last_frame(), "proxy": self.proxy.last_frame()}

    def teleport(self, tp: dict) -> dict:
        """Moves Sam from the checkpoint spawn to tp["location"] and makes tp["yaw"] the base direction. The game
        has to stream the area in and keep Sam there: he must stay within 150 units sideways and not fall more
        than 300 units, read back after 8 s, or the spot fails."""
        want = [float(v) for v in tp["location"]]
        self.game.set_location(self.pawn, want)
        self.hold(8.0)
        got = [float(v) for v in self.game.location(self.pawn)]
        side, drop = float(np.hypot(got[0] - want[0], got[1] - want[1])), want[2] - got[2]
        info = {"asked": want, "landed": [round(v, 1) for v in got], "sideways": round(side, 1), "drop": round(drop, 1)}
        if side > 150 or drop > 300:
            raise RuntimeError(f"teleport did not hold: {info}")
        self.base_yaw, self.base_pitch = float(tp["yaw"]), float(tp.get("pitch", 0.0))
        self.home = got
        return info

    def wait_player(self, cp: dict, timeout: float = 90.0, steady: int = 3) -> tuple[int, int]:
        """(controller, pawn) once `steady` reads one second apart find the same pair, in the checkpoint's map,
        with Sam within 150 units of the checkpoint position. Without Remix the gate starts drive.py as soon as a
        camera is published, which can be mid-load: con_mall (Nov 16) failed on its first read ("read 4 bytes at
        0x45") with 1 MB of textures loaded, and con_green_hall's controller was replaced after the first window."""
        last, same, why, end = None, 0, "no read yet", time.monotonic() + timeout
        while True:
            pair = None
            try:
                pair = self.game.player()
                where = self.game.map_name(pair[0])
                gap = float(np.linalg.norm(np.subtract(self.game.location(pair[1]), cp["location"])))
                why = f"map {where!r} (checkpoint {cp['map']!r}), Sam {gap:.0f} units from the checkpoint position"
                if where != cp["map"] or gap > 150:
                    pair = None
            except Exception as e:   # noqa: BLE001 - a half-loaded level; read again
                why, pair = f"{type(e).__name__}: {e}", None
            same = same + 1 if pair is not None and pair == last else int(pair is not None)
            last = pair
            if same >= steady:
                return pair
            if time.monotonic() > end:
                raise RuntimeError(f"the level did not settle within {timeout:.0f} s; last read: {why}")
            time.sleep(1.0)

    def relink(self) -> None:
        """The controller read failed: the game replaced the player objects (checkpoint reload, or a level that was
        still loading). Find them again and record it in spot.json; sam_moved still catches a Sam put elsewhere."""
        with self._link_lock:
            old = (self.controller, self.pawn)
            self.controller, self.pawn = self.game.player()
            self.relinks.append({"at": time.strftime("%H:%M:%S"), "afterWindow": len(self.windows),
                                 "old": [hex(v) for v in old], "new": [hex(self.controller), hex(self.pawn)]})
            print("RELINK", self.relinks[-1], flush=True)

    def _rot(self) -> tuple[float, float]:
        controller = self.controller
        try:
            yaw, pitch, _ = self.game.rotation(controller)
        except blmem.MemoryError_:
            if controller == self.controller:   # another thread may have relinked already
                self.relink()
            yaw, pitch, _ = self.game.rotation(self.controller)
        return yaw, pitch

    def rel_pose(self) -> list[float]:
        """The game's camera direction now, relative to the base (yaw wrapped to -180..180)."""
        yaw, pitch = self._rot()
        return [round(gate.wrap(yaw - self.base_yaw), 2), round(pitch - self.base_pitch, 2)]

    def _settled(self, quiet: float = 0.25, timeout: float = 3.0) -> tuple[float, float]:
        """The rotation once it has not changed for `quiet` seconds (several game frames)."""
        last, since, end = self._rot(), time.monotonic(), time.monotonic() + timeout
        while time.monotonic() < end:
            time.sleep(0.01)
            now = self._rot()
            if now != last:
                last, since = now, time.monotonic()
            elif time.monotonic() - since >= quiet:
                break
        return last

    def _steer_toward(self, target_rel_yaw: float, target_rel_pitch: float, gain: float = 0.8) -> bool:
        """One correction toward the target, at most once per game frame: the game applies mouse input once per
        frame, so a correction sent before the previous one shows up in the rotation would be added on top of it
        (at 22 FPS and one send every 8 ms that stacked about five corrections and swung the camera). Returns True
        when the camera is on target."""
        cur = self._rot()
        err_yaw = gate.wrap((self.base_yaw + target_rel_yaw) - cur[0])
        err_pitch = max(-80.0, min(80.0, self.base_pitch + target_rel_pitch)) - cur[1]
        if abs(err_yaw) < 0.15 and abs(err_pitch) < 0.15:
            self._sent = None
            return True
        if self._sent and self._sent[0] == cur and time.monotonic() - self._sent[1] < 0.25:
            return False   # the last correction is not applied yet
        # Without Remix (~178 FPS) the game eases each input over several frames, so the first changed frame is not
        # the end of it: stacked corrections swung the camera +-5 degrees and stops landed 4 to 7 degrees off
        # (Custodial reference, Nov 16). There, wait until the camera has stayed still for 50 ms.
        if cur != getattr(self, "_seen", None):
            self._seen, self._seen_at = cur, time.monotonic()
        if getattr(self, "no_remix", False) and self._sent and time.monotonic() - self._seen_at < 0.05:
            return False
        dx = int(round(max(-900.0, min(900.0, err_yaw * self.counts_per_deg_x * gain))))
        dy = int(round(max(-600.0, min(600.0, err_pitch * self.counts_per_deg_y * gain))))
        if dx == 0 and abs(err_yaw) >= 0.15:
            dx = 1 if err_yaw * self.counts_per_deg_x > 0 else -1
        if dy == 0 and abs(err_pitch) >= 0.15:
            dy = 1 if err_pitch * self.counts_per_deg_y > 0 else -1
        mouse_move_rel(dx, dy)
        self._sent = (cur, time.monotonic())
        return False

    def _calibrate_mouse(self) -> None:
        """Counts per degree from five settled moves per axis, alternating direction. One move read 50 ms later (the old way)
        could catch the camera still moving and gave a pan of 191 degrees instead of 360. A first warm-up move per
        axis is discarded: after a load the game ate part of the first input (Paladin, Nov 15: 3.97 counts per
        degree, then 2.43 three times)."""
        samples = {"x": [], "y": []}
        warmup = {}
        for axis, counts in (("x", 200), ("y", 80)):
            for i, sign in enumerate((1, -1, 1, -1, 1, -1)):   # even count: the camera ends where it started
                before = self._settled()
                mouse_move_rel(sign * counts if axis == "x" else 0, sign * counts if axis == "y" else 0)
                after = self._settled()
                moved = gate.wrap(after[0] - before[0]) if axis == "x" else after[1] - before[1]
                if i == 0:
                    warmup[axis] = round(sign * counts / moved, 3) if abs(moved) > 0.5 else None
                elif abs(moved) > 0.5:
                    samples[axis].append(sign * counts / moved)
        self.calibration = {"samplesX": [round(v, 3) for v in samples["x"]],
                            "samplesY": [round(v, 3) for v in samples["y"]],
                            "warmupX": warmup["x"], "warmupY": warmup["y"]}
        # Without Remix the game runs at ~178 FPS and the turn per count varied 45 to 69 between moves (Paladin,
        # Nov 16); steering and the pan correct from game memory, so only the rough rate is needed there.
        spread = 2.0 if getattr(self, "no_remix", False) else 1.25
        self.calibration["spreadLimit"] = spread
        # One move the game partly ate is dropped (Custodial reference, Nov 16: 4.7 counts per degree, then 32 to 48
        # four times); only one may be dropped, and at least three must still agree within the spread.
        for axis in ("x", "y"):
            vals = samples[axis]
            mid = float(np.median(vals)) if vals else 0.0
            kept = [v for v in vals if mid and v * mid > 0 and max(abs(v), abs(mid)) / min(abs(v), abs(mid)) <= spread]
            if len(kept) < 3 or len(vals) - len(kept) > 1 or max(map(abs, kept)) / min(map(abs, kept)) > spread:
                raise RuntimeError(f"mouse calibration on {axis} is inconsistent: counts per degree {vals}")
            self.calibration[f"dropped{axis.upper()}"] = [round(v, 3) for v in vals if v not in kept]
            setattr(self, f"counts_per_deg_{axis}", float(np.median(kept)))
        self.calibration.update(countsPerDegX=round(self.counts_per_deg_x, 3), countsPerDegY=round(self.counts_per_deg_y, 3))

    def pose(self, yaw: float, pitch: float) -> bool:
        self.yaw, self.pitch = yaw, pitch
        return self._steer_toward(yaw, pitch)

    def hold(self, seconds: float, until=None) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not (until and until()):
            time.sleep(0.05)

    def window(self, name: str, kind: str, body, **extra) -> dict:
        start = self.counters()
        body()
        end = self.counters()
        w = {"name": name, "kind": kind, "yaw": self.yaw, "pitch": self.pitch,
             "telemetry": [start["telemetry"], end["telemetry"]], "proxy": [start["proxy"], end["proxy"]], **extra}
        self.windows.append(w)
        if getattr(self, "save_partial", None):
            self.save_partial()
        return w

    def move(self, name: str, yaw: float, pitch: float, seconds: float) -> None:
        y0, p0 = self.yaw, self.pitch

        def body():
            t0 = time.monotonic()
            while (t := (time.monotonic() - t0) / seconds) < 1.0:
                self.pose(y0 + (yaw - y0) * t, p0 + (pitch - p0) * t)
                time.sleep(0.008)
            settle_end = time.monotonic() + 3.0
            while not self.pose(yaw, pitch) and time.monotonic() < settle_end:
                time.sleep(0.008)
        self.window(name, "move", body)
    def capture(self, rel: str, frames: int) -> None:
        proc = start_capture(os.path.join(self.out, rel), frames)
        try:
            self.hold(frames / 30.0 + 10.0, until=lambda: proc.poll() is not None)
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait()

    def dump(self, tag: str) -> None:
        """Asks the proxy (and through it Remix) for this frame's depth/colour dumps; copies what arrives."""
        set_ini(self.ini, "Proxy", "GateDump", tag)
        names = [f"{tag}_final.pfm", f"{tag}_depth.pfm", f"{tag}_gamedepth.pfm"]
        found = lambda: [os.path.join(d, n) for d in self.dump_dirs for n in names if os.path.exists(os.path.join(d, n))]
        self.hold(8.0, until=lambda: len(found()) >= len(names))
        time.sleep(0.3)   # let the last write finish
        for p in found():
            shutil.copy2(p, os.path.join(self.out, "gate", os.path.basename(p)))

    def steady(self, fps: float = 10.0, span: float = 1.0, timeout: float = 15.0) -> float:
        """Waits until the game has drawn at least fps*span frames in the last `span` seconds; returns the seconds
        waited. In the Paladin the game stalled 0.5 to 1.6 s per frame for several seconds after the full turn,
        so the stop after it caught 2 game frames and the flicker checks of the whole run were NOT MEASURED."""
        t0, seen = time.monotonic(), []
        while (now := time.monotonic()) - t0 < timeout:
            seen.append((now, self.proxy.last_frame()))
            older = [f for t, f in seen if now - t >= span]
            if older and seen[-1][1] - older[-1] >= fps * span:
                return round(now - t0, 2)
            time.sleep(0.1)
        return round(timeout, 2)

    def raster(self, tag: str, method: str, **extra) -> dict:
        """The reference picture <tag>_raster.pfm in gate\\ (gate.py and worker F2's scorer read it beside
        <tag>_final.pfm): the mean of RASTER_GRABS full-size grabs of the game window's client area, display-encoded
        0..1, unmasked, plus <tag>_raster.json saying where the camera was and how the picture was made."""
        hwnd = game_window(self.a.pid)
        if not hwnd:
            raise RuntimeError("no game window to grab")
        x, y, w, h = client_rect(hwnd)
        rot, loc = self._rot(), self.game.location(self.pawn)
        t0 = time.monotonic()
        out = subprocess.run([FFMPEG, "-v", "error", "-f", "gdigrab", "-framerate", "25", "-draw_mouse", "0",
                              "-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}", "-i", "desktop",
                              "-frames:v", str(RASTER_GRABS), "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                             capture_output=True, timeout=60)
        seconds = time.monotonic() - t0
        img, lumas = average_grabs(out.stdout, w, h, RASTER_GRABS)
        gate.write_pfm(os.path.join(self.out, "gate", f"{tag}_raster.pfm"), img)
        info = {"tag": tag, "method": method, "grabs": RASTER_GRABS, "grabSeconds": round(seconds, 2),
                "clientRect": [x, y, w, h], "encoding": "display-encoded u8/255 (not linear), RGB, unmasked",
                "yawPitch": [round(float(rot[0]), 2), round(float(rot[1]), 2)], "relPose": self.rel_pose(),
                "location": [round(float(v), 1) for v in loc], "lumaPerGrab": lumas,
                "luma": [min(lumas), round(float(np.mean(lumas)), 4), max(lumas)], **extra}
        gate.save_json(os.path.join(self.out, "gate", f"{tag}_raster.json"), info)
        return info

    def _try_raster(self, tag: str, method: str, before_raster=None) -> dict:
        """{"raster": path} or {"rasterError": why}: a failed reference picture is recorded in the window and the spot
        goes on (gate.py then has no raster picture for that stop) instead of losing the stops after it."""
        try:
            extra = before_raster() if before_raster else {}
            self.raster(tag, method, **extra)
            return {"raster": f"gate/{tag}_raster.pfm"}
        except Exception as e:   # noqa: BLE001 - recorded, not fatal
            print("RASTER ERROR", tag, f"{type(e).__name__}: {e}")
            return {"rasterError": f"{type(e).__name__}: {e}"}

    def rt_is_off(self, since_frame: int) -> dict:
        """Raises unless Remix's telemetry has at least 10 frames after since_frame and the last 10 are not ray traced."""
        recs = recent_telemetry(self.telemetry.path, since_frame)
        traced = [bool(r.get("rayTraced")) for r in recs[-10:]]
        if len(traced) < 10 or any(traced):
            raise RuntimeError(f"ray tracing was not off: rayTraced of the last {len(traced)} frames {traced}")
        return {"rayTracedLastFrames": traced}

    def raster_rt_off(self, tag: str) -> None:
        """Method A reference picture inside a Remix run: ray tracing off (the proxy then gives the game back its own
        lighting and post effects), checked off in Remix's telemetry, grabbed, then switched back on."""
        got: dict = {}

        def body():
            before = self.telemetry.last_frame()
            self.set_rt(False)
            got.update(self._try_raster(tag, "rt_off", lambda: self.rt_is_off(before)))
        try:
            self.window(tag + "_raster", "raster", body).update(got)
        finally:
            self.set_rt(True)

    def snap_pose(self) -> int:
        """Without Remix the game runs at ~178 FPS and eases each mouse input over several frames, so mouse steering
        left reference stops 4 to 28 degrees off (Custodial and bright hall, Nov 16) and Paladin stops up to 157 off.
        There the stop's exact direction is written into the controller's rotation in game memory, and checked again
        once the camera is still. Returns how many corrections it took (0: the mouse already got there), or -1 when
        the camera was still off after SNAP_STEPS (recorded as poseWrites).

        The memory write does not hold: the controller has no other rotation field, and the game rewrites Rotation
        from its own state, so the reference run of Nov 16 05:21 (con_custodial) ended every written stop where the
        mouse had left it, 1 to 2.3 degrees off at pitch +-30 ("poseWrites -1"). So the corrections are mouse moves,
        each sent only once the camera has been still for 0.25 s: the game eases one input over many frames at
        178 FPS, and a correction sent mid-ease is added on top of it."""
        for steps in range(self.SNAP_STEPS + 1):
            yaw, pitch = self._settled()
            err_yaw = gate.wrap(self.base_yaw + self.yaw - yaw)
            err_pitch = self.base_pitch + self.pitch - pitch
            if abs(err_yaw) < 0.3 and abs(err_pitch) < 0.3:
                return steps
            if steps < self.SNAP_STEPS:
                dx = int(round(err_yaw * self.counts_per_deg_x * 0.8)) if abs(err_yaw) >= 0.3 else 0
                dy = int(round(err_pitch * self.counts_per_deg_y * 0.8)) if abs(err_pitch) >= 0.3 else 0
                mouse_move_rel(dx or (int(math.copysign(1, err_yaw * self.counts_per_deg_x)) if abs(err_yaw) >= 0.3 else 0),
                               dy or (int(math.copysign(1, err_pitch * self.counts_per_deg_y)) if abs(err_pitch) >= 0.3 else 0))
        return -1

    SNAP_STEPS = 6

    OFF_POSE_DEG = 1.0   # a stop further than this from the asked view (yaw or pitch) is off-pose
    POSE_TRIES = 3

    def pose_trace(self, body) -> list[list[float]]:
        """Runs body() while reading the camera direction from game memory every 50 ms; returns the readings."""
        seen: list[list[float]] = []
        done = threading.Event()

        def sample():
            while not done.is_set():
                try:
                    seen.append(self.rel_pose())
                except Exception:
                    pass
                done.wait(0.05)
        t = threading.Thread(target=sample, daemon=True)
        t.start()
        try:
            body()
        finally:
            done.set(); t.join(1.0)
        return seen

    def pose_error(self, poses: list[list[float]]) -> float:
        """Largest yaw or pitch distance, in degrees, of the game-memory poses from the stop's asked view."""
        return round(max(max(abs(gate.wrap(y - self.yaw)), abs(p - self.pitch)) for y, p in poses), 2)

    def stop(self, name: str, outline: str | None = None) -> None:
        """One still stop. The camera direction is read from game memory before and after the capture; a stop more
        than OFF_POSE_DEG from the asked view is renamed <name>_offpose<n> (kind "off_pose", which no check
        reads), steered back and taken again, up to POSE_TRIES times (worker F2, Nov 16: 24 of 25 Paladin reference
        stops were off by up to 69.5 degrees). The kept window records offPoseDeg, poseTries and, when every try
        missed, offPose true so a check can refuse it."""
        rel = f"frames/{name}.gray"
        missed: list[dict] = []
        for attempt in range(1, self.POSE_TRIES + 1):
            if attempt > 1:
                self.move(f"to_{name}_retry{attempt}", self.yaw, self.pitch, 0.3)
            snapped = self.snap_pose() if self.no_remix else None
            self.hold(0.5)
            waited = self.steady()
            settle = self.window(name + "_settle", "settle", lambda: self.hold(0.5), steadyWait=waited)
            before = self.rel_pose()
            got: dict = {}
            if self.no_remix:   # no Remix dumps to ask for; the reference picture is the screen itself
                grab = lambda: got.update(self._try_raster(name, "no_remix"))
            else:
                grab, got = (lambda: self.dump(name)), {"dump": name}
            trace: list = []
            w = self.window(name, "still", lambda: trace.extend(self.pose_trace(lambda: (self.capture(rel, 20), grab()))),
                            frames=rel, frameSize=[CAP_W, CAP_H], **({"outline": outline} if outline else {}))
            w.update(got)
            w["gamePose"] = [before, self.rel_pose()]   # [yaw, pitch] from game memory at the start and end
            if trace:   # the direction every 50 ms during the capture, at most 20 of them kept
                w["poseTrace"] = trace[::max(1, len(trace) // 20)]
            if snapped is not None:
                w["poseWrites"] = snapped
            w["offPoseDeg"] = self.pose_error(w["gamePose"] + trace)
            print(f"stop {name} try {attempt}: {w['offPoseDeg']} degrees off, poseWrites {snapped}, "
                  f"start {before} end {w['gamePose'][1]}", flush=True)
            if w["offPoseDeg"] <= self.OFF_POSE_DEG or attempt == self.POSE_TRIES:
                break
            for x in (settle, w):   # kept for the record under another name; no check reads kind "off_pose"
                x["name"], x["kind"] = f"{x['name']}_offpose{attempt}", "off_pose"
            missed.append({"try": attempt, "offPoseDeg": w["offPoseDeg"], "gamePose": w["gamePose"]})
        w["poseTries"] = attempt
        if missed:
            w["offPoseTries"] = missed
        if w["offPoseDeg"] > self.OFF_POSE_DEG:
            w["offPose"] = True
            print(f"OFF POSE {name}: {w['offPoseDeg']} degrees from the asked view after {attempt} tries")
        w["samMoved"] = self.sam_moved(name)
        if not self.no_remix and name in self.spot.get("rasterStops", ["y0_p0"]):
            self.raster_rt_off(name)

    def pan(self, seconds: float) -> float:
        """One full turn in `seconds`: steady mouse motion at the calibrated rate, plus a correction once per game
        frame that pulls the turn measured in game memory toward the schedule, so a calibration error cannot
        shorten the circle. Returns the degrees turned, summed from game memory."""
        step_hz = 120.0
        steps = max(1, int(round(seconds * step_hz)))
        rem, turned = 0.0, 0.0
        last = self._rot()[0]
        t0 = time.monotonic()
        for s_idx in range(steps + int(3.0 * step_hz)):   # up to 3 s more to finish the circle
            now_yaw = self._rot()[0]
            fresh = now_yaw != last
            turned += gate.wrap(now_yaw - last)
            last = now_yaw
            target = 360.0 * min(1.0, (s_idx + 1) / steps)
            if s_idx >= steps and abs(target - turned) < 0.5:
                break
            want = (360.0 / steps if s_idx < steps else 0.0) * self.counts_per_deg_x + rem
            if fresh or s_idx >= steps:
                want += (target - turned) * self.counts_per_deg_x * (0.3 if s_idx < steps else 0.05)
            dx = int(round(want))
            rem = want - dx if s_idx < steps else 0.0
            mouse_move_rel(dx, 0)
            self.yaw = target
            target_t = t0 + (s_idx + 1) / step_hz
            now = time.monotonic()
            if target_t > now:
                time.sleep(target_t - now)
        self.yaw = 0.0
        settle_end = time.monotonic() + 1.0
        while not self._steer_toward(0.0, 0.0) and time.monotonic() < settle_end:
            time.sleep(0.008)
        return turned + gate.wrap(self._settled()[0] - last)

    def sweep_and_pan(self) -> None:
        pitches = self.spot.get("pitches", [0, -30, 30])
        yaws = [i * 360.0 / self.spot.get("yawStops", 8) for i in range(self.spot.get("yawStops", 8))]
        stops = [(float(y), float(p)) for p in pitches for y in yaws]
        if stops[0] != (0.0, 0.0):
            raise ValueError("the first pitch must be 0: the first stop is the reference pose (yaw 0, pitch 0)")
        self.move("to_start", 0.0, 0.0, 0.5)
        self.hold(2.0)
        for i, (y, p) in enumerate(stops):
            if i:
                self.move(f"to_y{y:.0f}_p{p:.0f}", y, p, 0.5)
            self.stop(f"y{y:.0f}_p{p:.0f}", outline="before" if i == 0 else None)
        self.move("to_pan", 0.0, 0.0, 1.0)
        self.hold(1.0)
        seconds = float(self.spot.get("panSeconds", 30))
        rel = "frames/pan.gray"
        proc = start_capture(os.path.join(self.out, rel), fps=60, seconds=seconds + 0.3)
        time.sleep(0.3)   # ffmpeg start-up; the capture then spans the pan, not the still camera after it
        turned = {}
        try:
            w = self.window("pan360", "pan", lambda: turned.update(deg=self.pan(seconds)),
                            frames=rel, frameSize=[CAP_W, CAP_H])
            w["gameTurned"] = round(turned["deg"], 1)   # degrees, summed from game memory during the pan
        finally:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
        self.stop("y0_p0_after", outline="after")
        if self.spot.get("gunfire", False):
            self.window("gunfire", "combat", lambda: (mouse_fire_burst(6, 0.12), self.hold(0.5)))
    def bench(self, cfg: dict) -> None:
        """Settings sweep (verify_runtime.ps1 -Variants): one launch per setting, so this only has to measure the
        reference pose. Face y0_p0, let textures and the radiance cache settle, then one "still" window of at least
        cfg["frames"] game frames (proxy frame log, which stays on when Remix telemetry is off) while nvidia-smi
        samples video memory every 200 ms into gpu_mem.csv, then one GateDump of the same pose.
        cfg["revisit"] (worker C, Nov 16, to tell warm-up from memory pressure): afterwards look once round the room
        (yawStops stops, 1 s each, so more of it streams in), come back to y0_p0 and measure it again as the separate
        window "bench_y0_p0_end" (gpu_mem_end.csv)."""
        settle, want = float(cfg.get("settle", 5.0)), int(cfg.get("frames", 150))
        self.move("to_bench", 0.0, 0.0, 1.0)
        self.hold(settle)
        self.bench_window("bench_y0_p0", want, "bench", "gpu_mem.csv")
        self.window("y0_p0", "dump", lambda: self.dump("y0_p0"), dump="y0_p0")
        if cfg.get("revisit"):
            n = int(self.spot.get("yawStops", 8))
            for i in range(1, n):
                self.move(f"look_y{i * 360.0 / n:.0f}", i * 360.0 / n, 0.0, 0.5)
                self.hold(1.0)
            self.move("to_bench_end", 0.0, 0.0, 1.0)
            self.hold(settle)
            self.bench_window("bench_y0_p0_end", want, "bench_end", "gpu_mem_end.csv")
        # cfg["liveSteps"] (worker C, Nov 16, to separate memory from launch-to-launch variation): rtx keys changed
        # live through [RemixConfig] while holding y0_p0, one window of liveStepSeconds per step. Whether Remix took a
        # change is read afterwards from textureBudgetMB in each window's telemetry, not assumed.
        if cfg.get("liveSteps"):
            self.move("to_live", 0.0, 0.0, 1.0)
            seconds = float(cfg.get("liveStepSeconds", 60))
            for i, keys in enumerate(cfg["liveSteps"]):
                for k, v in keys.items():
                    set_ini(self.ini, "RemixConfig", k, str(v))
                self.window(f"live_step{i + 1}", "still", lambda: self.hold(seconds), liveRtx=keys)

    def bench_window(self, name: str, want: int, tag: str, smi_file: str) -> dict:
        """One still window of at least `want` game frames with nvidia-smi sampling into `smi_file`."""
        rel = f"frames/{tag}.gray"
        # One nvidia-smi call per sample (gpu_sampler.ps1): -lms writes through a 4 KB buffer, to -f and to stdout
        # alike, so the terminate below (a kill on Windows) lost the tail, or all of a short window (Nov 16).
        smi = subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                os.path.join(os.path.dirname(os.path.abspath(__file__)), "gpu_sampler.ps1"),
                                "-Out", os.path.join(self.out, smi_file), "-IntervalMs", "200", "-Query",
                                "timestamp,memory.total,memory.used,memory.free,clocks.gr,clocks.mem,pstate,utilization.gpu"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cap = start_capture(os.path.join(self.out, rel), fps=30, seconds=60.0)
        start = self.proxy.last_frame()
        try:
            w = self.window(name, "still", lambda: self.hold(60.0, until=lambda: self.proxy.last_frame() - start >= want),
                            frames=rel, frameSize=[CAP_W, CAP_H], benchFrames=want, gpuMem=smi_file)
        finally:
            for p in (cap, smi):
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill(); p.wait()
        w["gameFrames"] = self.proxy.last_frame() - start
        w["gamePose"] = [self.rel_pose()]
        w["samMoved"] = self.sam_moved(tag)
        return w

    def light_dump(self, dest: str) -> str | None:
        """One frame of the proxy's light and flare dump (framedump\\f<frame>_lights.tsv, written when
        [Proxy] CaptureDraws goes from 0 to 1), copied to `dest`."""
        folder = os.path.join(self.a.system, "framedump")
        before = set(os.listdir(folder)) if os.path.isdir(folder) else set()
        set_ini(self.ini, "Proxy", "CaptureDraws", "1")
        new = lambda: [n for n in (os.listdir(folder) if os.path.isdir(folder) else []) if n.endswith("_lights.tsv") and n not in before]
        self.hold(6.0, until=lambda: bool(new()))
        set_ini(self.ini, "Proxy", "CaptureDraws", "0")
        time.sleep(0.5)
        if not new():
            return None
        shutil.copy2(os.path.join(folder, new()[0]), dest)
        return os.path.basename(dest)

    def switch_test(self) -> None:
        """At the reference pose, the same capture once per variant with exactly one thing changed (an ini edit,
        or the goggles on). The ini is put back byte for byte after each variant. switch_report.py turns the
        windows into numbers."""
        keys = self.spot.get("goggleKeys", {"toggle": "F9", "cycle": "F10"})
        with open(self.ini, encoding="latin-1", newline="") as f:
            original = f.read()
        self.move("to_switch", 0.0, 0.0, 1.0)
        os.makedirs(os.path.join(self.out, "switch"), exist_ok=True)
        for v in self.spot.get("switches", SWITCHES):
            name = v["name"]
            mark = len(self.log.new_text())
            try:
                for section, key, value in v.get("ini", []):
                    set_ini(self.ini, section, key, value)
                if v.get("goggles"):
                    hwnd = game_window(self.a.pid)
                    if hwnd:
                        front(hwnd)
                    tap(keys["toggle"])
                self.hold(3.0)
                proof = self.await_log(v["expectLog"], mark) if v.get("expectLog") else None
                rel = f"frames/switch_{name}.gray"
                w = self.window(f"switch_{name}", "switch", lambda: (self.capture(rel, 30), self.dump(f"switch_{name}")),
                                frames=rel, frameSize=[CAP_W, CAP_H], dump=f"switch_{name}", switch=v)
                w["lights"] = self.light_dump(os.path.join(self.out, "switch", f"{name}_lights.tsv"))
                if proof:
                    w["logProof"] = proof
                w["gamePose"] = [self.rel_pose()]
                w["samMoved"] = self.sam_moved(f"switch_{name}")
            finally:
                if v.get("goggles"):
                    tap(keys["toggle"])
                if v.get("restore"):
                    for section, key, value in v["restore"]:
                        set_ini(self.ini, section, key, value)
                    self.hold(3.0)
                write_ini(self.ini, original)
                self.hold(3.0)

    def await_log(self, want: dict, mark: int) -> dict:
        """Proof that a live ini edit took (worker B, Nov 16): after `mark` in this spot's proxy.log, a line holding
        want["reloaded"] (such as "settings reloaded (... skip unplaced 1, skip moving 0)"), then at least
        want["count"] lines holding want["after"] (such as "lightpub: stray"). Raises when the reload line is missing,
        so the step is never measured on settings that did not load."""
        reloaded, after, count = want["reloaded"], want.get("after", ""), int(want.get("count", 0))
        found: dict = {}
        last = [0.0]

        def seen() -> bool:
            if time.monotonic() - last[0] < 0.5:   # proxy.log can be megabytes; read it twice a second, not 20 times
                return False
            last[0] = time.monotonic()
            lines = self.log.new_text()[mark:].splitlines()
            at = next((i for i, line in enumerate(lines) if reloaded in line), None)
            if at is None:
                return False
            found["reloadLine"] = lines[at]
            found["afterLines"] = [line for line in lines[at + 1:] if after and after in line]
            return len(found["afterLines"]) >= count
        self.hold(float(want.get("timeout", 60)), until=seen)
        if "reloadLine" not in found:
            raise RuntimeError(f"no proxy.log line '{reloaded}' after the ini edit")
        if len(found["afterLines"]) < count:
            found["short"] = f"{len(found['afterLines'])} of {count} '{after}' lines before the timeout"
        return found

    def light_switch(self, flip: dict) -> None:
        """Faces the reference pose, presses the use key (the spot stands Sam at a light switch) and records the
        screen for flip["seconds"], so the time the room takes to go dark can be measured."""
        self.move("to_light_switch", 0.0, 0.0, 1.0)
        rel, key = "frames/light_switch.gray", flip.get("key", "SPACE")
        hwnd = game_window(self.a.pid)
        if hwnd:
            front(hwnd)
        frames = int(30 * flip.get("seconds", 20))
        dumps: list[dict] = []

        def body():
            # Light dumps during and after the fade (worker B, Nov 16: the last run had none after the press), each
            # at flip["dumpsAt"] seconds after the key; "at" records when it really landed.
            proc = start_capture(os.path.join(self.out, rel), frames)
            try:
                tap(key)
                t0 = time.monotonic()
                os.makedirs(os.path.join(self.out, "switch"), exist_ok=True)
                for at in flip.get("dumpsAt", []):
                    self.hold(max(0.0, t0 + at - time.monotonic()))
                    frame, sent = self.proxy.last_frame(), round(time.monotonic() - t0, 2)
                    got = self.light_dump(os.path.join(self.out, "switch", f"light_switch_{at:g}s_lights.tsv"))
                    dumps.append({"asked": at, "at": sent, "proxyFrameAtAsk": frame, "lights": got})
                self.hold(frames / 30.0 + 10.0, until=lambda: proc.poll() is not None)
            finally:
                if proc.poll() is None:
                    proc.kill(); proc.wait()
        w = self.window("light_switch", "switch", body, frames=rel, frameSize=[CAP_W, CAP_H],
                        switch={"name": "light_switch", "key": key})
        w["lightDumps"] = dumps

    def mouse_lag(self, trials: int = 20, degrees: float = 5.0) -> dict:
        """Mouse-to-screen time, measured the same way with and without Remix: at a still camera, a quick turn of
        `degrees` is sent at a known moment and screen shots (a 480x270 patch up and left of centre, away from Sam,
        grabbed by gdigrab at up to 120 per second) are timestamped as they arrive; the lag is the time until a shot
        differs from the one before the turn by more than 3x the still-camera noise. gameMs is the time until the
        proxy logged the first game frame with the turned camera, so screenMs - gameMs is what happens after the
        game hands the frame on (Remix and the bridge, or the plain driver), plus the screen grab's own delay."""
        w, h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        pw, ph = 480, 270
        proc = subprocess.Popen([FFMPEG, "-v", "error", "-f", "gdigrab", "-framerate", "120", "-draw_mouse", "0",
                                 "-offset_x", str(w // 4 - pw // 2), "-offset_y", str(h // 3 - ph // 2),
                                 "-video_size", f"{pw}x{ph}", "-i", "desktop", "-vf", "format=gray",
                                 "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        shots: collections.deque = collections.deque(maxlen=600)

        def reader():
            size = pw * ph
            while True:
                buf = b""
                while len(buf) < size:
                    chunk = proc.stdout.read(size - len(buf))
                    if not chunk:
                        return
                    buf += chunk
                shots.append((time.perf_counter() * 1000.0, np.frombuffer(buf, np.uint8).astype(np.int16)))
        threading.Thread(target=reader, daemon=True).start()
        counts = int(round(degrees * self.counts_per_deg_x))
        results, sign = [], 1
        try:
            self.hold(1.5)
            # Until `trials` turns showed on screen (gate.py needs 20 samples), at most 2x trials turns: in the Paladin
            # its moving screens hid 9 of 20 turns in the noise (no-Remix reference, Nov 16).
            while sum(r.get("screenMs") is not None for r in results) < trials and len(results) < 2 * trials:
                self.hold(0.5)
                tq = time.perf_counter() * 1000.0
                self.hold(0.3)
                still = [img for t, img in list(shots) if t >= tq]
                if len(still) < 3:
                    results.append({"error": "screen grab delivered under 10 shots a second"}); continue
                base = still[-1]
                noise = max(float(np.abs(img - base).mean()) for img in still)
                threshold = max(1.5, 3.0 * noise)
                t0 = time.perf_counter() * 1000.0
                mouse_move_rel(sign * counts, 0)
                self.hold(0.6)
                seen = first_change(list(shots), t0, base, threshold)
                turned = first_turned(proxy_camera_dirs(self.proxy.path, t0 - 500.0), t0) if self.proxy.path else None
                results.append({"screenMs": round(seen - t0, 1) if seen else None,
                                "gameMs": round(turned - t0, 1) if turned else None,
                                "noise": round(noise, 2), "threshold": round(threshold, 2)})
                sign = -sign
            span = list(shots)
            fps = (len(span) - 1) * 1000.0 / (span[-1][0] - span[0][0]) if len(span) > 1 else 0.0
        finally:
            proc.kill(); proc.wait()
        # Format agreed with worker A (gate.py input_latency_ms): samples_ms in turn order, only turns the screen showed;
        # the gate takes medians and spread itself.
        screen = [r["screenMs"] for r in results if r.get("screenMs") is not None]
        game = [r["gameMs"] for r in results if r.get("gameMs") is not None]
        after = [r["screenMs"] - r["gameMs"] for r in results if r.get("screenMs") is not None and r.get("gameMs") is not None]
        med = lambda v: round(float(np.median(v)), 1) if v else None
        return {"samples_ms": screen, "game_share_ms": med(game), "after_game_ms": med(after),
                "method": f"{degrees:g} degree mouse turn at a still camera, alternating left/right until {trials} turns "
                          f"showed on screen (at most {2 * trials} turns); time "
                          "from SendInput to the first gdigrab shot (480x270 patch, ~120/s, arrival-stamped with "
                          "perf_counter) that differs by > 3x still noise; game share = to the first proxy-logged "
                          "frame with the turned camera (same clock)",
                "trials": results, "captureFps": round(fps, 1)}

    def set_rt(self, on: bool) -> None:
        set_ini(self.ini, "Lights", "AutoRaytracing", "1" if on else "0")
        if on:
            remove_ini_key(self.ini, "rtx.enableRaytracing")
        else:
            set_ini(self.ini, "RemixConfig", "rtx.enableRaytracing", "False")
        self.hold(5.0)
    def goggles(self) -> None:
        keys = self.spot.get("goggleKeys", {"toggle": "F9", "cycle": "F10"})
        hwnd = game_window(self.a.pid)
        if not hwnd or not front(hwnd):
            print("goggles: game window did not come to the front; the goggle check will be NOT MEASURED")
        previous = None
        for mode in range(1, int(self.spot.get("goggleModes", 3)) + 1):
            mark = len(self.log.new_text())
            tap(keys["toggle"] if mode == 1 else keys["cycle"])
            self.hold(2.0)
            text = self.log.new_text()[mark:]
            shots = {}
            for rt in ("on", "off"):
                if rt == "off":
                    self.set_rt(False)
                rel = f"frames/goggles{mode}_{rt}.gray"
                w = self.window(f"goggles{mode}_rt{rt}", "goggles", lambda: self.capture(rel, 8),
                                goggles={"mode": mode, "rt": rt, "shot": rel, "frameSize": [CAP_W, CAP_H]})
                shots[rt] = w
                if rt == "off":
                    self.set_rt(True)
            img = self._mean_shot(shots["on"]["goggles"]["shot"])
            if mode == 1:
                toggled = "Goggles: on" in text
            else:
                toggled = "Goggles: off" not in text and img is not None and previous is not None and \
                    (abs(img.mean() - previous.mean()) > 8 or np.corrcoef(img.ravel(), previous.ravel())[0, 1] < 0.95)
            for w in shots.values():
                w["goggles"]["toggled"] = bool(toggled)
            previous = img
        mark = len(self.log.new_text())
        tap(keys["toggle"])
        self.hold(2.0)
        if "Goggles: off" not in self.log.new_text()[mark:]:
            print("goggles: no 'Goggles: off' after the last toggle; marking all modes untoggled")
            for w in self.windows:
                if w["kind"] == "goggles":
                    w["goggles"]["toggled"] = False

    def _mean_shot(self, rel: str):
        p = os.path.join(self.out, rel)
        if not os.path.exists(p) or os.path.getsize(p) < CAP_W * CAP_H:
            return None
        raw = np.fromfile(p, np.uint8)
        n = raw.size // (CAP_W * CAP_H)
        return raw[: n * CAP_W * CAP_H].reshape(n, CAP_H, CAP_W).astype(np.float32).mean(axis=0)

    def finish(self) -> dict:
        files = {"telemetry": self.telemetry.copy_to(os.path.join(self.out, "telemetry.jsonl")),
                 "proxyFrames": self.proxy.copy_to(os.path.join(self.out, "proxy_frames.csv")),
                 "proxyGate": self.pgate.copy_to(os.path.join(self.out, "proxy_gate.csv")),
                 "proxyLog": self.log.copy_to(os.path.join(self.out, "proxy.log")),
                 "lightpubTrace": self.lptrace.copy_to(os.path.join(self.out, "lightpub_trace.csv"))}
        if self.spot.get("cinematic"):
            # The cutscene plays before drive.py starts, so the spot's part above misses it (run 20261115-214203:
            # telemetry from frame 5033, the cutscene ended at 4987). These copies cover the whole launch, so
            # Remix's own picture during the cutscene sits beside cinematic.csv (worker F2).
            files["telemetryLaunch"] = self.telemetry.copy_to(os.path.join(self.out, "telemetry_launch.jsonl"), whole=True)
            files["proxyFramesLaunch"] = self.proxy.copy_to(os.path.join(self.out, "proxy_frames_launch.csv"), whole=True)
        self.process.close()
        return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["capture", "spot"])
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--exe-hash", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--spot"); ap.add_argument("--checkpoint"); ap.add_argument("--system"); ap.add_argument("--root")
    ap.add_argument("--no-remix", action="store_true", help="reference run: the proxy chains Windows' d3d9; each still "
                    "stop saves <tag>_raster.pfm instead of asking Remix for dumps; no switch test or goggle check")
    a = ap.parse_args()
    if a.mode == "capture":
        game, proc = blmem.attach(a.pid, a.exe_hash)
        controller, pawn = game.player()
        yaw, pitch, _ = game.rotation(controller)
        info = {"map": game.map_name(controller), "location": list(game.location(pawn)), "yaw": yaw, "pitch": pitch,
                "layout": game.check_layout(), "recorded": time.strftime("%Y-%m-%d %H:%M:%S")}
        proc.close()
        gate.save_json(a.out, info)
        print(json.dumps(info))
        return 0
    spot, cp = gate.load_json(a.spot), gate.load_json(a.checkpoint)
    os.makedirs(a.out, exist_ok=True)
    result = {"name": spot["name"], "mission": spot.get("mission"), "nearField": spot.get("nearField", 300),
              "error": None, "windows": []}
    s = None
    ctypes.windll.winmm.timeBeginPeriod(1)
    try:
        s = Spot(a, spot, cp)
        # spot.json after every window, marked unfinished, so a drive.py killed at verify_runtime's time limit still
        # leaves its stops (Paladin reference, Nov 16: a timeout lost all 24); the last write below replaces it.
        s.save_partial = lambda: gate.save_json(os.path.join(a.out, "spot.json"), {
            **result, "windows": s.windows, "mouseCalibration": s.calibration,
            "error": f"drive.py did not finish: this record was written after window {len(s.windows)}"})
        with open(os.path.join(a.system, "d3d9_proxy.log"), encoding="latin-1") as f:
            chained = chained_dll(f.read(), a.pid)
        result["renderer"] = {"chained": list(chained) if chained else None, "kind": check_renderer(chained, a.no_remix)}
        where = s.game.map_name(s.controller)
        if where != cp["map"]:
            raise RuntimeError(f"loaded map {where!r}, checkpoint was recorded in {cp['map']!r}")
        loc = s.game.location(s.pawn)
        gap = float(np.linalg.norm(np.subtract(loc, cp["location"])))
        if gap > 150:
            raise RuntimeError(f"Sam spawned {gap:.0f} units from the recorded checkpoint position")
        s.home = list(loc)
        if "teleport" in spot:
            result["teleport"] = s.teleport(spot["teleport"])
        hwnd = game_window(a.pid)
        if spot.get("bench"):
            if not hwnd:
                raise RuntimeError("no game window to bring to the front for the benchmark")
            front(hwnd)
            s._calibrate_mouse()
            s.bench(spot["bench"])   # one pose per launch: nothing else
        else:
            if hwnd:
                front(hwnd)
                s._calibrate_mouse()
                if int(spot.get("mouseLag", 1)):
                    lag: dict = {}
                    s.window("mouse_lag", "lag", lambda: lag.update(s.mouse_lag()))
                    result["mouse_lag"] = lag   # top-level key worker A's gate.py reads
                    back = time.monotonic() + 3.0   # the trials alternate direction; put the camera back on the stop
                    while not s.pose(s.yaw, s.pitch) and time.monotonic() < back:
                        time.sleep(0.008)
            # "switchAfterLight": the switches run after the light switch, at the same stop (worker B, Nov 16).
            late = bool(spot.get("switchAfterLight"))
            if spot.get("switchTest") and not a.no_remix and not late:   # the switches are Remix settings
                s.switch_test()
            if not spot.get("switchOnly"):
                s.sweep_and_pan()
                if int(spot.get("goggleModes", 3)) and not a.no_remix:   # the goggle check compares ray tracing on/off
                    s.goggles()
            if spot.get("lightSwitch"):
                s.light_switch(spot["lightSwitch"])
            if spot.get("switchTest") and not a.no_remix and late:
                s.switch_test()
    except Exception as e:   # recorded in spot.json; gate.py fails the spot
        result["error"] = f"{type(e).__name__}: {e}"
        print("SPOT ERROR", result["error"])
        traceback.print_exc(file=sys.stdout)   # where it failed, in drive.log
    finally:
        if s:
            result["windows"] = s.windows
            result["relinks"] = s.relinks
            result["mouseCalibration"] = s.calibration
            result.update(s.finish())
        ctypes.windll.winmm.timeEndPeriod(1)
    gate.save_json(os.path.join(a.out, "spot.json"), result)
    print("SPOT DONE", spot["name"], "error" if result["error"] else "ok", len(result["windows"]), "windows")
    return 1 if result["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
