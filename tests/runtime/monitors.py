"""Which monitors Windows sees, and the desktop layout, for the monitor test (worker C's spec, Nov 16 18:07).

Morning launches ran with both DisplayPort monitors (HKC2731, ACI27A8) switched off at their buttons. Asleep,
deactivated (a software disconnect) and detached (the card no longer sees the monitor) are different states, and
tests/analysis/monitor_steps.py scores each step by what this module records.

monitors()       one entry per monitor the card knows or knew: {name, primary, available, active, power}
                 available = QueryDisplayConfig(QDC_ALL_PATHS) targetAvailable; active = it has a path in
                 QDC_ONLY_ACTIVE_PATHS; power = display.state() (the console display state, one value for all).
raw()            the lists those come from: names with targetAvailable, active path count, SM_CMONITORS and
                 Get-PnpDevice -Class Monitor -PresentOnly.
layout()         per active monitor: name, primary, width, height, x, y, refresh Hz. Compare before and after.
game_window(pid) {minimized, foreground, rect [w, h]} of the process's main visible top-level window.

CLI: monitors.py snapshot [--pid N]   prints one JSON line {time, monitors, raw, layout, gameWindow}
     monitors.py layout               prints the layout JSON
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)

QDC_ALL_PATHS, QDC_ONLY_ACTIVE_PATHS = 1, 2
GET_TARGET_NAME = 2
PATH_ACTIVE = 1
SM_CMONITORS = 80
KNOWN = ["HKC2731", "ACI27A8"]   # Corey's two DisplayPort monitors; listed with available false when the card drops one


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]


class RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", ctypes.c_uint32), ("Denominator", ctypes.c_uint32)]


class PATH_SOURCE(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("statusFlags", ctypes.c_uint32)]


class PATH_TARGET(ctypes.Structure):
    _fields_ = [("adapterId", LUID), ("id", ctypes.c_uint32), ("modeInfoIdx", ctypes.c_uint32),
                ("outputTechnology", ctypes.c_uint32), ("rotation", ctypes.c_uint32), ("scaling", ctypes.c_uint32),
                ("refreshRate", RATIONAL), ("scanLineOrdering", ctypes.c_uint32), ("targetAvailable", wt.BOOL),
                ("statusFlags", ctypes.c_uint32)]


class PATH_INFO(ctypes.Structure):
    _fields_ = [("sourceInfo", PATH_SOURCE), ("targetInfo", PATH_TARGET), ("flags", ctypes.c_uint32)]


class SOURCE_MODE(ctypes.Structure):
    _fields_ = [("width", ctypes.c_uint32), ("height", ctypes.c_uint32), ("pixelFormat", ctypes.c_uint32),
                ("x", wt.LONG), ("y", wt.LONG)]


class MODE_UNION(ctypes.Union):
    _fields_ = [("source", SOURCE_MODE), ("raw", ctypes.c_ubyte * 48), ("_align", ctypes.c_uint64)]


class MODE_INFO(ctypes.Structure):
    _fields_ = [("infoType", ctypes.c_uint32), ("id", ctypes.c_uint32), ("adapterId", LUID), ("u", MODE_UNION)]


class INFO_HEADER(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapterId", LUID), ("id", ctypes.c_uint32)]


class TARGET_NAME(ctypes.Structure):
    _fields_ = [("header", INFO_HEADER), ("flags", ctypes.c_uint32), ("outputTechnology", ctypes.c_uint32),
                ("edidManufactureId", ctypes.c_uint16), ("edidProductCodeId", ctypes.c_uint16),
                ("connectorInstance", ctypes.c_uint32), ("friendly", wt.WCHAR * 64), ("path", wt.WCHAR * 128)]


assert ctypes.sizeof(PATH_INFO) == 72 and ctypes.sizeof(MODE_INFO) == 64 and ctypes.sizeof(TARGET_NAME) == 420

user32.GetDisplayConfigBufferSizes.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                               ctypes.POINTER(ctypes.c_uint32)]
user32.QueryDisplayConfig.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(PATH_INFO),
                                      ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(MODE_INFO), ctypes.c_void_p]
user32.DisplayConfigGetDeviceInfo.argtypes = [ctypes.c_void_p]


def query(flags):
    """(paths, modes) from QueryDisplayConfig; retries when the topology changes between the two calls."""
    for _ in range(5):
        n_p, n_m = ctypes.c_uint32(), ctypes.c_uint32()
        if user32.GetDisplayConfigBufferSizes(flags, ctypes.byref(n_p), ctypes.byref(n_m)):
            continue
        paths, modes = (PATH_INFO * n_p.value)(), (MODE_INFO * n_m.value)()
        rc = user32.QueryDisplayConfig(flags, ctypes.byref(n_p), paths, ctypes.byref(n_m), modes, None)
        if rc == 0:
            return list(paths[:n_p.value]), list(modes[:n_m.value])
        time.sleep(0.2)
    raise OSError("QueryDisplayConfig kept failing")


def _key(t):
    return (t.adapterId.LowPart, t.adapterId.HighPart, t.id)


def target_name(t) -> str:
    """The EDID id, such as HKC2731, from the monitor device path (\\\\?\\DISPLAY#HKC2731#...)."""
    n = TARGET_NAME()
    n.header.type, n.header.size, n.header.adapterId, n.header.id = GET_TARGET_NAME, ctypes.sizeof(n), t.adapterId, t.id
    if user32.DisplayConfigGetDeviceInfo(ctypes.byref(n)) != 0:
        return f"target{t.id}"
    parts = n.path.split("#")
    return parts[1] if len(parts) > 2 else (n.friendly or f"target{t.id}")


def _targets():
    """{key: {name, available}} over every target in QDC_ALL_PATHS (one target shows once per source)."""
    out = {}
    for p in query(QDC_ALL_PATHS)[0]:
        k = _key(p.targetInfo)
        d = out.setdefault(k, {"name": target_name(p.targetInfo), "available": False})
        d["available"] = d["available"] or bool(p.targetInfo.targetAvailable)
    return out


def layout() -> list[dict]:
    paths, modes = query(QDC_ONLY_ACTIVE_PATHS)
    out = []
    for p in paths:
        if not p.flags & PATH_ACTIVE:
            continue
        s = modes[p.sourceInfo.modeInfoIdx].u.source if p.sourceInfo.modeInfoIdx < len(modes) else None
        r = p.targetInfo.refreshRate
        out.append({"name": target_name(p.targetInfo), "primary": bool(s and s.x == 0 and s.y == 0),
                    "width": s.width if s else None, "height": s.height if s else None,
                    "x": s.x if s else None, "y": s.y if s else None,
                    "refreshHz": round(r.Numerator / r.Denominator, 2) if r.Denominator else None})
    return sorted(out, key=lambda m: m["name"])


def _pnp() -> list[dict]:
    cmd = ("Get-PnpDevice -Class Monitor -PresentOnly -ErrorAction SilentlyContinue | "
           "Select-Object FriendlyName,InstanceId,Status | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["pwsh", "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                             timeout=30).stdout.strip()
        got = json.loads(out) if out else []
        return got if isinstance(got, list) else [got]
    except Exception as e:  # recorded, never fatal: the QueryDisplayConfig lists decide the state
        return [{"error": str(e)}]


def _power():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import display
    return display.state()


def monitors(known: list[str] | None = None, pnp: bool = True) -> tuple[list[dict], dict]:
    """(monitor list, raw lists). known names stay listed with available false when the card no longer reports them."""
    targets = _targets()
    lay = layout()
    active = {m["name"]: m for m in lay}
    power = _power()
    names = {}
    for t in targets.values():
        if t["name"].startswith("target"):  # an empty connector: no monitor path, kept only in raw
            continue
        names[t["name"]] = names.get(t["name"], False) or t["available"]
    for k in KNOWN if known is None else known:
        names.setdefault(k, False)
    mons = [{"name": n, "primary": bool(active.get(n, {}).get("primary")), "available": avail,
             "active": n in active, "power": power} for n, avail in sorted(names.items())]
    raw = {"targets": [{"name": t["name"], "targetAvailable": t["available"]} for t in targets.values()],
           "activePaths": len(lay), "smCmonitors": user32.GetSystemMetrics(SM_CMONITORS), "pnpMonitors": _pnp() if pnp else None,
           "displayState": power}
    return mons, raw


def game_window(pid: int) -> dict | None:
    found = []
    EnumProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):  # GW_OWNER
            found.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    if not found:
        return None
    h = found[0]
    rc = wt.RECT()
    user32.GetClientRect(h, ctypes.byref(rc))
    return {"minimized": bool(user32.IsIconic(h)), "foreground": user32.GetForegroundWindow() == h,
            "rect": [rc.right - rc.left, rc.bottom - rc.top]}


class SOURCE_NAME(ctypes.Structure):
    _fields_ = [("header", INFO_HEADER), ("gdi", wt.WCHAR * 32)]


class PHYSICAL_MONITOR(ctypes.Structure):
    _fields_ = [("h", wt.HANDLE), ("desc", wt.WCHAR * 128)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD),
                ("szDevice", wt.WCHAR * 32)]


dxva2 = ctypes.WinDLL("dxva2", use_last_error=True)
# Without argtypes ctypes passes a monitor handle as a C int, and a handle above 2**31 fails inside the enumeration
# callback ("int too long to convert"): that monitor then gets no DDC handle (no-game probe, Nov 16 21:42).
user32.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.c_void_p]
dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [wt.HMONITOR, ctypes.POINTER(wt.DWORD)]
dxva2.GetPhysicalMonitorsFromHMONITOR.argtypes = [wt.HMONITOR, wt.DWORD, ctypes.c_void_p]
dxva2.SetVCPFeature.argtypes = [wt.HANDLE, wt.BYTE, wt.DWORD]
dxva2.GetVCPFeatureAndVCPFeatureReply.argtypes = [wt.HANDLE, wt.BYTE, ctypes.c_void_p, ctypes.POINTER(wt.DWORD),
                                                  ctypes.POINTER(wt.DWORD)]
_ddc: dict = {}   # name -> physical monitor handle, opened while the monitor was attached and kept for this process


def ddc_open() -> dict:
    """Opens (once per monitor, kept open) a DDC/CI handle for every attached monitor, keyed by EDID name."""
    gdi = {}
    for p in query(QDC_ONLY_ACTIVE_PATHS)[0]:
        n = SOURCE_NAME()
        n.header.type, n.header.size = 1, ctypes.sizeof(n)
        n.header.adapterId, n.header.id = p.sourceInfo.adapterId, p.sourceInfo.id
        if user32.DisplayConfigGetDeviceInfo(ctypes.byref(n)) == 0:
            gdi[n.gdi] = target_name(p.targetInfo)
    MonProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

    def cb(hmon, hdc, rc, lp):
        mi = MONITORINFOEXW(cbSize=ctypes.sizeof(MONITORINFOEXW))
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        name = gdi.get(mi.szDevice)
        if name and name not in _ddc:
            cnt = wt.DWORD()
            dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, ctypes.byref(cnt))
            arr = (PHYSICAL_MONITOR * cnt.value)()
            if cnt.value == 1 and dxva2.GetPhysicalMonitorsFromHMONITOR(hmon, cnt, arr):
                _ddc[name] = arr[0].h or 0   # ctypes reads handle 0 back as None
        return True

    user32.EnumDisplayMonitors(None, None, MonProc(cb), 0)
    return _ddc


DDC_ON, DDC_STANDBY, DDC_OFF = 1, 4, 5   # VCP 0xD6 power mode; both monitors list D6(01 04 05) in their capabilities


def ddc_power(name: str, value: int) -> bool:
    """Sets VCP 0xD6 on one monitor through its kept handle. True when the monitor acknowledged the write."""
    h = ddc_open().get(name)   # a physical monitor handle can be 0, so test for None
    return bool(h is not None and dxva2.SetVCPFeature(h, 0xD6, value))


def ddc_read(name: str) -> int | None:
    h = ddc_open().get(name)
    cur, mx = wt.DWORD(), wt.DWORD()
    return cur.value if h is not None and dxva2.GetVCPFeatureAndVCPFeatureReply(h, 0xD6, None, ctypes.byref(cur), ctypes.byref(mx)) else None


def snapshot(pid: int | None = None, known: list[str] | None = None, pnp: bool = True) -> dict:
    mons, raw = monitors(known, pnp)
    return {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "monitors": mons, "raw": raw, "layout": layout(),
            "gameWindow": game_window(pid) if pid else None}


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "snapshot"
    if cmd == "layout":
        print(json.dumps(layout()))
        return 0
    if cmd == "snapshot":
        pid = int(argv[argv.index("--pid") + 1]) if "--pid" in argv else None
        print(json.dumps(snapshot(pid)))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
