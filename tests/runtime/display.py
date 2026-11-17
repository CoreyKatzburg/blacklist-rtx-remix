"""Display power state for test launches (coordinator, Nov 16, 16:41: idle GPU power follows whether the displays are
awake, 77.4 W asleep against 81.5 W awake, the same step as the 35 against 42 ms launches; Corey plays with them awake,
so only launches with the displays awake count).

state()        "on", "off" or "dimmed", read from Windows' console display state (GUID_CONSOLE_DISPLAY_STATE, which
               Windows sends to a window as soon as it registers for it), None when Windows sent nothing.
idle_seconds() seconds since the last keyboard or mouse input, real or injected (Windows turns the displays off after
               1,800 s of it on this machine).
wake()         a mouse move of +1 then -1 count (the camera ends where it was), then waits for state "on".
sleep()        SC_MONITORPOWER 2 to every top-level window, then waits for state "off". Send no input afterwards.

CLI (verify_runtime.ps1): display.py state | wake | sleep   prints one JSON line {display, idleSeconds, ...}
                          display.py watch --out <csv> [--interval 2]   appends time,display,idleSeconds until killed.

Whose input (coordinator, Nov 16 21:3x: health run 210808's view stuck at 21:13 and display.csv could not say whether
the gate or Corey had moved the mouse): every input the gate sends is noted with its tick in a gate_sends*.csv file in
the launch folder (send_log, set by drive.py; verify_runtime.ps1 notes its own key taps the same way), and watch adds per
sample the last-input tick, input = none (no input since the last sample), gate (the last input lies within SEND_MATCH_MS
of a noted send) or outside (it does not), and the foreground window's process. An outside input followed within the
same sample by a gate send reads as gate.
"""
import ctypes
import ctypes.wintypes as wt
import json
import sys
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE = 0x0218, 0x8013
WM_SYSCOMMAND, SC_MONITORPOWER = 0x0112, 0xF170
HWND_BROADCAST, HWND_MESSAGE = 0xFFFF, -3
STATES = {0: "off", 1: "on", 2: "dimmed"}


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


# GUID_CONSOLE_DISPLAY_STATE 6FE69556-704A-47A0-8F24-C28D936FDA47
CONSOLE_DISPLAY_STATE = GUID(0x6FE69556, 0x704A, 0x47A0, (ctypes.c_ubyte * 8)(0x8F, 0x24, 0xC2, 0x8D, 0x93, 0x6F, 0xDA, 0x47))


class POWERBROADCAST_SETTING(ctypes.Structure):
    _fields_ = [("PowerSetting", GUID), ("DataLength", wt.DWORD), ("Data", wt.DWORD)]


LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("mi", MOUSEINPUT)]   # 40 bytes on x64, as SendInput expects (MOUSEINPUT is the largest)


user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.RegisterPowerSettingNotification.argtypes = [wt.HANDLE, ctypes.POINTER(GUID), wt.DWORD]
user32.RegisterPowerSettingNotification.restype = wt.HANDLE
user32.UnregisterPowerSettingNotification.argtypes = [wt.HANDLE]
user32.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
kernel32.GetTickCount.restype = wt.DWORD   # unsigned: after 24.8 days of uptime a signed read goes negative
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]

_seen: list = []


@WNDPROC
def _proc(hwnd, msg, wparam, lparam):
    if msg == WM_POWERBROADCAST and wparam == PBT_POWERSETTINGCHANGE and lparam:
        s = ctypes.cast(lparam, ctypes.POINTER(POWERBROADCAST_SETTING)).contents
        _seen.append(int(s.Data))
        return 1
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_class = WNDCLASSW(lpfnWndProc=_proc, hInstance=kernel32.GetModuleHandleW(None), lpszClassName="BlacklistDisplayState")
user32.RegisterClassW(ctypes.byref(_class))


def state(timeout: float = 2.0) -> str | None:
    """Windows sends the current console display state to a window right after it registers for it."""
    _seen.clear()
    hwnd = user32.CreateWindowExW(0, _class.lpszClassName, None, 0, 0, 0, 0, 0, HWND_MESSAGE, None, _class.hInstance, None)
    note = user32.RegisterPowerSettingNotification(hwnd, ctypes.byref(CONSOLE_DISPLAY_STATE), 0)
    try:
        msg, end = wt.MSG(), time.monotonic() + timeout
        while not _seen and time.monotonic() < end:
            while user32.PeekMessageW(ctypes.byref(msg), hwnd, 0, 0, 1):
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.02)
    finally:
        if note:
            user32.UnregisterPowerSettingNotification(note)
        user32.DestroyWindow(hwnd)
    return STATES.get(_seen[-1], str(_seen[-1])) if _seen else None


def last_input_tick() -> int:
    info = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    user32.GetLastInputInfo(ctypes.byref(info))
    return int(info.dwTime)


def idle_seconds() -> float:
    return round(((kernel32.GetTickCount() - last_input_tick()) & 0xFFFFFFFF) / 1000.0, 1)


SENDS_PREFIX = "gate_sends"
SEND_MATCH_MS = 300   # a gate send explains the last input when the two ticks are this close (tick steps are ~16 ms)
send_log: str | None = None   # drive.py: <launch folder>\gate_sends_drive.csv
_last_noted = [0]


def note_send() -> None:
    """Notes that the gate just sent input, at most every 100 ms (drive.py steers the mouse up to 120 times a second)."""
    if not send_log:
        return
    now = int(kernel32.GetTickCount())
    if (now - _last_noted[0]) & 0xFFFFFFFF < 100:
        return
    _last_noted[0] = now
    with open(send_log, "a", newline="") as f:
        f.write(f"{now}\r\n")


def read_sends(folder: str) -> list[int]:
    import glob
    import os
    ticks: list[int] = []
    for p in glob.glob(os.path.join(folder, SENDS_PREFIX + "*.csv")):
        with open(p, encoding="ascii", errors="replace") as f:
            ticks += [int(t) for t in f.read().split() if t.isdigit()]
    return ticks


def input_source(prev_tick: int | None, tick: int, sends: list[int]) -> str:
    """none, gate or outside for the input between two samples (see the module notes)."""
    if prev_tick is None or tick == prev_tick:
        return "none"
    near = lambda s: min((tick - s) & 0xFFFFFFFF, (s - tick) & 0xFFFFFFFF) <= SEND_MATCH_MS
    return "gate" if any(near(s) for s in sends) else "outside"


def foreground_process() -> str:
    """The image name of the process that owns the foreground window, "" when there is none or it cannot be read."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return f"pid {pid.value}"
    try:
        buf, n = ctypes.create_unicode_buffer(1024), wt.DWORD(1024)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return f"pid {pid.value}"
        return buf.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(h)


def _wait_for(want: str, seconds: float) -> tuple[str | None, float]:
    t0, got = time.monotonic(), None
    while time.monotonic() - t0 < seconds:
        got = state()
        if got == want:
            break
        time.sleep(0.25)
    return got, round(time.monotonic() - t0, 2)


def wake(seconds: float = 10.0) -> dict:
    before = state()
    # MOUSEEVENTF_MOVE by +1 then -1 count: a move of 0 counts is not input to Windows (16:58: idle time kept growing
    # and the displays stayed off), and the pair leaves the game camera where it was.
    for dx in (1, -1):
        i = INPUT(type=0, mi=MOUSEINPUT(dx, 0, 0, 0x0001, 0, 0))
        if user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT)) != 1:
            raise OSError(ctypes.get_last_error(), "SendInput failed")
    note_send()
    got, took = _wait_for("on", seconds)
    return {"asked": "awake", "before": before, "display": got, "seconds": took, "idleSeconds": idle_seconds()}


def sleep(seconds: float = 10.0) -> dict:
    before = state()
    user32.PostMessageW(HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, 2)
    got, took = _wait_for("off", seconds)
    return {"asked": "asleep", "before": before, "display": got, "seconds": took, "idleSeconds": idle_seconds()}


ES_CONTINUOUS, ES_DISPLAY_REQUIRED = 0x80000000, 0x00000002
kernel32.SetThreadExecutionState.argtypes = [wt.DWORD]
kernel32.SetThreadExecutionState.restype = wt.DWORD


def display_required() -> bool:
    """Whether any program now holds the displays on (CallNtPowerInformation SystemExecutionState)."""
    flags = wt.ULONG()
    ctypes.WinDLL("powrprof").CallNtPowerInformation(16, None, 0, ctypes.byref(flags), ctypes.sizeof(flags))
    return bool(flags.value & ES_DISPLAY_REQUIRED)


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "state"
    if cmd == "watch":
        out = argv[argv.index("--out") + 1]
        interval = float(argv[argv.index("--interval") + 1]) if "--interval" in argv else 2.0
        # --keep-awake (coordinator, Nov 16; Corey: the displays only sleep, and testing may keep them awake): this
        # process holds ES_DISPLAY_REQUIRED, so Windows does not sleep them for idle time; Windows drops the hold when
        # the gate kills this process at the end of the launch. The power plan is not changed.
        if "--keep-awake" in argv:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_DISPLAY_REQUIRED)
        import os
        if not os.path.exists(out):
            with open(out, "w", newline="") as f:
                f.write("time,display,idleSeconds,displayRequired,lastInputTick,input,foreground\r\n")
        folder, prev = os.path.dirname(os.path.abspath(out)), None
        while True:
            tick = last_input_tick()
            src = input_source(prev, tick, read_sends(folder))
            prev = tick
            with open(out, "a", newline="") as f:
                f.write(f"{time.strftime('%H:%M:%S')},{state()},{idle_seconds()},{int(display_required())},"
                        f"{tick},{src},{foreground_process()}\r\n")
            time.sleep(interval)
    rec = wake() if cmd == "wake" else sleep() if cmd == "sleep" else {"display": state(), "idleSeconds": idle_seconds()}
    print(json.dumps(rec), flush=True)
    return 0 if cmd == "state" or rec["display"] == ("on" if cmd == "wake" else "off") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
