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
import json
import os
import re
import shutil
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blmem  # noqa: E402
import gate  # noqa: E402  (file helpers)

FFMPEG = r"d:\ffmpeg\bin\ffmpeg.exe"
CAP_W, CAP_H = 320, 180


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

    def copy_to(self, dest: str) -> str | None:
        if not self.path:
            return None
        with open(self.path, "rb") as f, open(dest, "wb") as out:
            f.seek(self._from()); shutil.copyfileobj(f, out)
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


SCAN = {"F9": 0x43, "F10": 0x44, "F11": 0x57, "F12": 0x58, "F": 0x21, "G": 0x22, "N": 0x31, "X": 0x2D}


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
        self.dump_dirs = [os.path.join(d, "gate") for d in (s, os.path.join(s, ".trex"), r)]
        self.windows: list[dict] = []
        self.game, self.process = blmem.attach(a.pid, a.exe_hash)
        self.controller, self.pawn = self.game.player()
        self.base_yaw = float(spot.get("baseYaw", checkpoint["yaw"]))
        self.base_pitch = float(spot.get("basePitch", checkpoint["pitch"]))
        self.yaw = self.pitch = 0.0       # requested, relative to the base
        self.counts_per_deg_x = float(spot.get("mouseCountsPerDegX", 14.0))
        self.counts_per_deg_y = float(spot.get("mouseCountsPerDegY", -14.0))
        self.calibration: dict = {}
        self._sent: tuple | None = None   # (rotation read when the last correction was sent, time sent)

    def counters(self) -> dict:
        return {"telemetry": self.telemetry.last_frame(), "proxy": self.proxy.last_frame()}

    def _rot(self) -> tuple[float, float]:
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
        """Counts per degree from four settled moves each way per axis. One move read 50 ms later (the old way)
        could catch the camera still moving and gave a pan of 191 degrees instead of 360."""
        samples = {"x": [], "y": []}
        for axis, counts in (("x", 200), ("y", 80)):
            for sign in (1, -1, 1, -1):
                before = self._settled()
                mouse_move_rel(sign * counts if axis == "x" else 0, sign * counts if axis == "y" else 0)
                after = self._settled()
                moved = gate.wrap(after[0] - before[0]) if axis == "x" else after[1] - before[1]
                if abs(moved) > 0.5:
                    samples[axis].append(sign * counts / moved)
        self.calibration = {"samplesX": [round(v, 3) for v in samples["x"]],
                            "samplesY": [round(v, 3) for v in samples["y"]]}
        for axis in ("x", "y"):
            vals = samples[axis]
            if len(vals) < 3 or min(vals) * max(vals) <= 0 or max(map(abs, vals)) / min(map(abs, vals)) > 1.25:
                raise RuntimeError(f"mouse calibration on {axis} is inconsistent: counts per degree {vals}")
            setattr(self, f"counts_per_deg_{axis}", float(np.median(vals)))
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

    def stop(self, name: str, outline: str | None = None) -> None:
        rel = f"frames/{name}.gray"
        self.hold(0.5)
        self.window(name + "_settle", "settle", lambda: self.hold(0.5))
        before = self.rel_pose()
        w = self.window(name, "still", lambda: (self.capture(rel, 20), self.dump(name)),
                        frames=rel, frameSize=[CAP_W, CAP_H], dump=name, **({"outline": outline} if outline else {}))
        w["gamePose"] = [before, self.rel_pose()]   # [yaw, pitch] from game memory at the start and end of the stop

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
        hwnd = game_window(self.a.pid)
        if hwnd:
            front(hwnd)
            self._calibrate_mouse()
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
                 "proxyLog": self.log.copy_to(os.path.join(self.out, "proxy.log"))}
        self.process.close()
        return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["capture", "spot"])
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--exe-hash", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--spot"); ap.add_argument("--checkpoint"); ap.add_argument("--system"); ap.add_argument("--root")
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
        where = s.game.map_name(s.controller)
        if where != cp["map"]:
            raise RuntimeError(f"loaded map {where!r}, checkpoint was recorded in {cp['map']!r}")
        loc = s.game.location(s.pawn)
        gap = float(np.linalg.norm(np.subtract(loc, cp["location"])))
        if gap > 150:
            raise RuntimeError(f"Sam spawned {gap:.0f} units from the recorded checkpoint position")
        s.sweep_and_pan()
        s.goggles()
    except Exception as e:   # recorded in spot.json; gate.py fails the spot
        result["error"] = f"{type(e).__name__}: {e}"
        print("SPOT ERROR", result["error"])
    finally:
        if s:
            result["windows"] = s.windows
            result["mouseCalibration"] = s.calibration
            result.update(s.finish())
        ctypes.windll.winmm.timeEndPeriod(1)
    gate.save_json(os.path.join(a.out, "spot.json"), result)
    print("SPOT DONE", spot["name"], "error" if result["error"] else "ok", len(result["windows"]), "windows")
    return 1 if result["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
