"""The Amp desktop app's window during a launch (coordinator, Nov 17: in launch (i) the Amp WebView's GPU process kept
submitting 3d work through the whole game, 5.6 % before the scene and about 88 % during it; whether that costs Remix
time is not known).

Reads, and only for the Amp-window test changes, one thing: whether the Amp.exe main window is minimized.
  state()        {"found", "hwnd", "pid", "title", "minimized", "maximized", "rect", "normalRect", "monitor",
                  "monitorDevice", "monitorRect", "webviewGpuPid"}; found false when no Amp window exists
  minimize(h)    ShowWindow SW_SHOWMINNOACTIVE: minimized without changing which window has the focus (the game stays
                 in front)
  restore(h)     ShowWindow SW_SHOWNOACTIVATE: back to its size before (maximized stays maximized), focus unchanged
No other window is touched, no setting changed, the app is never closed.

Safety (the Amp app hosts the runner every agent works through): before the first minimize the caller starts
watchdog(hwnd, deadline) as a separate detached process. At the deadline it restores the window if it is still
minimized, whatever happened to the caller, and writes one line to its log.

  amp_window.py state                         the state as JSON
  amp_window.py minimize | restore            act on the Amp main window, print the state read back
  amp_window.py watchdog --hwnd H --at EPOCH [--log FILE]
  amp_window.py --self-test                   the pure parts (monitor naming, read-back rule); touches no window
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
SW_SHOWMAXIMIZED, SW_SHOWNOACTIVATE, SW_SHOWMINNOACTIVE = 3, 4, 7
WPF_RESTORETOMAXIMIZED = 2
APP_EXE = "amp.exe"
ASKS = ("as_is", "minimized")   # a live step's "ampWindow"


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wt.UINT), ("flags", wt.UINT), ("showCmd", wt.UINT), ("ptMinPosition", wt.POINT),
                ("ptMaxPosition", wt.POINT), ("rcNormalPosition", wt.RECT)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD),
                ("szDevice", wt.WCHAR * 32)]


user32.MonitorFromWindow.restype = wt.HANDLE
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(MONITORINFOEXW)]
user32.GetWindowPlacement.argtypes = [wt.HWND, ctypes.POINTER(WINDOWPLACEMENT)]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.IsIconic.argtypes = [wt.HWND]
user32.IsWindow.argtypes = [wt.HWND]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
kernel32.OpenProcess.restype = wt.HANDLE


def _exe(pid: int) -> str:
    h = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf, n = ctypes.create_unicode_buffer(1024), wt.DWORD(1024)
        return os.path.basename(buf.value) if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ""
    finally:
        kernel32.CloseHandle(h)


def find() -> tuple[int, int, str]:
    """(hwnd, pid, title) of the Amp app's main window: visible, unowned, titled, top-level, process Amp.exe."""
    found = []
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):    # GW_OWNER
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            n = user32.GetWindowTextLengthW(hwnd)
            if n and _exe(pid.value).lower() == APP_EXE:
                t = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, t, n + 1)
                found.append((hwnd, pid.value, t.value))
        return True
    user32.EnumWindows(proc(cb), 0)
    return found[0] if found else (0, 0, "")


def _rect(r: wt.RECT) -> list[int]:
    return [r.left, r.top, r.right, r.bottom]


def monitor_name(mon_rect: list[int], lay: list[dict]) -> str | None:
    """The monitors.layout() name whose top-left corner and size equal the monitor rectangle."""
    for m in lay:
        if [m.get("x"), m.get("y"), m.get("x", 0) + m.get("width", 0), m.get("y", 0) + m.get("height", 0)] == mon_rect:
            return m.get("name")
    return None


def webview_gpu_pid(app_pid: int) -> int | None:
    """The WebView2 GPU process that draws the Amp window (command line: type=gpu-process, webview-exe-name=Amp.exe)."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedgewebview2.exe'\" | Where-Object { $_.CommandLine -match "
          "'--type=gpu-process' -and $_.CommandLine -match 'webview-exe-name=Amp.exe' } | "
          "Select-Object -ExpandProperty ProcessId")
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                             timeout=30).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return None
    pids = [int(p) for p in out if p.isdigit()]
    return pids[0] if len(pids) == 1 else None


def state(with_webview: bool = False) -> dict:
    hwnd, pid, title = find()
    if not hwnd:
        return {"found": False}
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    user32.GetWindowPlacement(hwnd, ctypes.byref(wp))
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    mi = MONITORINFOEXW()
    mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
    user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), ctypes.byref(mi))   # nearest; the restored rect if minimized
    try:
        import monitors
        lay = monitors.layout()
    except Exception:   # the record says the name is unknown rather than failing the window
        lay = []
    out = {"found": True, "hwnd": hwnd, "pid": pid, "title": title, "minimized": bool(user32.IsIconic(hwnd)),
           "maximized": wp.showCmd == SW_SHOWMAXIMIZED or bool(wp.flags & WPF_RESTORETOMAXIMIZED and user32.IsIconic(hwnd)),
           "rect": _rect(r), "normalRect": _rect(wp.rcNormalPosition), "monitorDevice": mi.szDevice,
           "monitorRect": _rect(mi.rcMonitor), "monitor": monitor_name(_rect(mi.rcMonitor), lay),
           "time": time.strftime("%H:%M:%S")}
    if with_webview:
        out["webviewGpuPid"] = webview_gpu_pid(pid)
    return out


def minimize(hwnd: int) -> None:
    user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)


def restore(hwnd: int) -> None:
    # Checked Nov 17 11:13 with no game: a maximized Amp window minimized this way comes back maximized at the same
    # rectangle, and the foreground window stays the same.
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)


def read_back(asked: str, st: dict) -> bool:
    """True when the window read back in the asked state: "minimized" or "as_is" (not minimized)."""
    return bool(st.get("found")) and st.get("minimized") == (asked == "minimized")


def start_watchdog(hwnd: int, deadline: float, log: str) -> int:
    """A detached process, outside this process tree and its job, that restores the window at `deadline` (epoch s)."""
    flags = 0x00000008 | 0x00000200 | 0x01000000   # DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP, BREAKAWAY_FROM_JOB
    args = [sys.executable, "-I", os.path.abspath(__file__), "watchdog", "--hwnd", str(hwnd), "--at", f"{deadline:.1f}",
            "--log", log]
    try:
        p = subprocess.Popen(args, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:   # the job does not allow breakaway: still detached and in its own group
        p = subprocess.Popen(args, creationflags=flags & ~0x01000000, close_fds=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return p.pid


def watchdog(hwnd: int, at: float, log: str | None) -> int:
    def note(msg):
        if log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} pid {os.getpid()} {msg}\n")
    note(f"watching hwnd {hwnd}, restore at {time.strftime('%H:%M:%S', time.localtime(at))}")
    while time.time() < at:
        time.sleep(min(1.0, max(0.0, at - time.time())))
    if not user32.IsWindow(hwnd):
        note("window gone; nothing to do")
        return 0
    was = bool(user32.IsIconic(hwnd))
    restore(hwnd)
    note(f"deadline: minimized {was}, after restore minimized {bool(user32.IsIconic(hwnd))}")
    return 0


def self_test() -> int:
    lay = [{"name": "ACI27A8", "x": -2560, "y": 0, "width": 2560, "height": 1440},
           {"name": "HKC2731", "x": 0, "y": 0, "width": 2560, "height": 1440}]
    cases = [("left monitor named", monitor_name([-2560, 0, 0, 1440], lay) == "ACI27A8"),
             ("primary named", monitor_name([0, 0, 2560, 1440], lay) == "HKC2731"),
             ("a 1920 monitor at 0,0 is not the 2560 one", monitor_name([0, 0, 1920, 1080], lay) is None),
             ("asked minimized, read minimized", read_back("minimized", {"found": True, "minimized": True})),
             ("asked minimized, read not minimized", not read_back("minimized", {"found": True, "minimized": False})),
             ("asked as is, read as is", read_back("as_is", {"found": True, "minimized": False})),
             ("asked as is, read minimized", not read_back("as_is", {"found": True, "minimized": True})),
             ("no Amp window", not read_back("as_is", {"found": False}))]
    for name, ok in cases:
        print(f"{'ok  ' if ok else 'FAIL'} {name}")
    bad = sum(not ok for _, ok in cases)
    print("self-test", "PASS" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if argv[:1] == ["--self-test"]:
        return self_test()
    if argv[:1] == ["state"]:
        print(json.dumps(state(with_webview=True)))
        return 0
    if argv[:1] in (["minimize"], ["restore"]):
        hwnd = find()[0]
        if not hwnd:
            print(json.dumps({"found": False}))
            return 1
        (minimize if argv[0] == "minimize" else restore)(hwnd)
        time.sleep(0.5)
        print(json.dumps(state()))
        return 0
    if argv[:1] == ["watchdog"]:
        kv = dict(zip(argv[1::2], argv[2::2]))
        return watchdog(int(kv["--hwnd"]), float(kv["--at"]), kv.get("--log"))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
