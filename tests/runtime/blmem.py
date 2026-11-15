"""Camera pose in Blacklist's DX9 process memory (Blacklist_game.exe), for verify_runtime.ps1.

Finds the active PlayerController through the engine's own object and name tables, then reads or writes its
Rotation (the camera direction) and reads its Pawn's Location and the loaded map. No mouse, no window focus.

Addresses (DX9 exe, image base 0x00400000; both accepted exe hashes have identical code):
  GNames       0x0326ED28  TArray<FNameEntry*>: data +0, Num +4, Max +8; name text at entry+8.
               Checked by disassembly of FName_Init 0x00772200 (reads [0x0326ED28], indexes entry*4,
               tests byte [entry+8]).
  GObjObjects  0x032AEF88  TArray<UObject*>: data +0, Num +4, Max +8, free list +0xC/+0x10, grows by 0x3FFF.
               Checked by disassembly of UObject_AddObject 0x0077E370 (grows Max at 0x032AEF90 by 0x3FFF,
               stores the object at [[0x032AEF88] + index*4], index in the low 20 bits of object+4).
Object layout (UObject Outer +0x18, Name +0x20, Class +0x24; UStruct parent +0x28; UField Next +0x2C;
UClass PropertyLink +0x44; UProperty ArrayDim +0x36, ElementSize +0x38, Offset +0x3A; StructProperty struct
+0x80) comes from the DX11 exe's analysis, which shares this engine code. It is not trusted blindly:
check_layout() must pass before anything is read or written, and every property is found by name and type.
"""
from __future__ import annotations

import ctypes
import os
import struct
from ctypes import wintypes

IMAGE_BASE = 0x00400000
GNAMES_VA = 0x0326ED28
GOBJECTS_VA = 0x032AEF88
OUTER, NAME, CLASS = 0x18, 0x20, 0x24
SUPER, NEXT, PROPERTY_LINK = 0x28, 0x2C, 0x44
ARRAY_DIM, ELEMENT_SIZE, OFFSET, STRUCT_OF = 0x36, 0x38, 0x3A, 0x80
NAME_INDEX_MASK = 0x3FFFF
MAX_OBJECTS = 400_000
UNITS_PER_TURN = 65536


class MemoryError_(RuntimeError):
    """A read failed or the game's tables do not look like the expected layout."""


def deg_to_units(deg: float) -> int:
    return int(round(deg * UNITS_PER_TURN / 360.0)) & 0xFFFF


def units_to_deg(units: int) -> float:
    u = units & 0xFFFF
    return (u - UNITS_PER_TURN if u >= 0x8000 else u) * 360.0 / UNITS_PER_TURN


class Process:
    """Read/write access to one x86 process (VM_READ | VM_WRITE | VM_OPERATION | QUERY_LIMITED_INFORMATION)."""

    def __init__(self, pid: int):
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.OpenProcess.restype = wintypes.HANDLE
        self.handle = self.k32.OpenProcess(0x0010 | 0x0020 | 0x0008 | 0x1000, False, pid)
        if not self.handle:
            raise OSError(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")
        self.pid = pid

    def read(self, address: int, size: int) -> bytes:
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t()
        if not self.k32.ReadProcessMemory(self.handle, ctypes.c_void_p(address), buf, size, ctypes.byref(got)) \
                or got.value != size:
            raise MemoryError_(f"read {size} bytes at 0x{address:08X} failed")
        return buf.raw

    def write(self, address: int, data: bytes) -> None:
        got = ctypes.c_size_t()
        if not self.k32.WriteProcessMemory(self.handle, ctypes.c_void_p(address), data, len(data), ctypes.byref(got)) \
                or got.value != len(data):
            raise MemoryError_(f"write {len(data)} bytes at 0x{address:08X} failed")

    def main_module(self) -> tuple[int, str]:
        """(base address, file path) of the exe, from a 32-bit module snapshot."""
        class ME32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                        ("GlblcntUsage", wintypes.DWORD), ("ProccntUsage", wintypes.DWORD),
                        ("modBaseAddr", ctypes.c_void_p), ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
                        ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260)]
        self.k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = self.k32.CreateToolhelp32Snapshot(0x8 | 0x10, self.pid)   # TH32CS_SNAPMODULE | SNAPMODULE32
        if snap in (None, wintypes.HANDLE(-1).value):
            raise OSError(f"module snapshot failed: {ctypes.get_last_error()}")
        try:
            entry = ME32(); entry.dwSize = ctypes.sizeof(ME32)
            if not self.k32.Module32FirstW(snap, ctypes.byref(entry)):
                raise OSError("Module32FirstW failed")
            return int(entry.modBaseAddr), entry.szExePath
        finally:
            self.k32.CloseHandle(snap)

    def close(self) -> None:
        if self.handle:
            self.k32.CloseHandle(self.handle); self.handle = None


class Game:
    """The engine tables of one Blacklist DX9 process (or of a fake memory with read/write for tests)."""

    def __init__(self, memory, module_base: int = IMAGE_BASE):
        self.mem = memory
        self.base = module_base
        self._names: dict[int, str] = {}
        self._props: dict[int, dict[str, tuple[str, int, int, str | None]]] = {}

    # --- raw tables ------------------------------------------------------------------------------------------
    def u32(self, address: int) -> int:
        return struct.unpack("<I", self.mem.read(address, 4))[0]

    def u16(self, address: int) -> int:
        return struct.unpack("<H", self.mem.read(address, 2))[0]

    def _array(self, va: int, label: str) -> tuple[int, int]:
        data, num, cap = struct.unpack("<III", self.mem.read(self.base + va - IMAGE_BASE, 12))
        if num > cap or cap > MAX_OBJECTS or (num and not data) or data & 3:
            raise MemoryError_(f"{label} at 0x{va:08X} does not look like a TArray (data 0x{data:X}, Num {num}, Max {cap})")
        return data, num

    def name(self, value: int) -> str:
        index = value & NAME_INDEX_MASK
        if index not in self._names:
            data, num = self._array(GNAMES_VA, "GNames")
            if index >= num:
                raise MemoryError_(f"name index {index} >= GNames Num {num}")
            entry = self.u32(data + index * 4)
            if not entry:
                raise MemoryError_(f"GNames[{index}] is null")
            addr = entry + 8
            first = min(128, 0x1000 - (addr & 0xFFF))
            raw = self.mem.read(addr, first)
            end = raw.find(b"\0")
            if end < 0 and first < 128:
                raw += self.mem.read(addr + first, 128 - first)
                end = raw.find(b"\0")
            if end < 0:
                raise MemoryError_(f"GNames[{index}] has no terminator")
            self._names[index] = raw[:end].decode("latin-1")
        number = value >> 19
        return f"{self._names[index]}_{number - 1}" if number else self._names[index]

    def obj_name(self, obj: int) -> str:
        return self.name(self.u32(obj + NAME))

    def class_of(self, obj: int) -> int:
        return self.u32(obj + CLASS)

    def class_chain(self, cls: int, limit: int = 64) -> list[str]:
        chain = []
        while cls and len(chain) < limit:
            chain.append(self.obj_name(cls))
            cls = self.u32(cls + SUPER)
        return chain

    def outermost(self, obj: int, limit: int = 32) -> int:
        for _ in range(limit):
            outer = self.u32(obj + OUTER)
            if not outer:
                return obj
            obj = outer
        raise MemoryError_("outer chain too deep")

    def objects(self):
        data, num = self._array(GOBJECTS_VA, "GObjObjects")
        raw = self.mem.read(data, num * 4)
        for (address,) in struct.iter_unpack("<I", raw):
            if address:
                yield address

    def find_class(self, name: str) -> int:
        """The UClass object called `name` (its own class is the class 'Class')."""
        for obj in self.objects():
            try:
                if self.obj_name(obj) == name and self.obj_name(self.class_of(obj)) == "Class":
                    return obj
            except MemoryError_:
                continue
        raise MemoryError_(f"class {name} not found")

    # --- reflection ------------------------------------------------------------------------------------------
    def properties(self, cls: int) -> dict[str, tuple[str, int, int, str | None]]:
        """name -> (property class, offset, element size, struct name) for cls and its parents."""
        if cls in self._props:
            return self._props[cls]
        out: dict[str, tuple[str, int, int, str | None]] = {}
        c = cls
        while c:
            field, seen = self.u32(c + PROPERTY_LINK), set()
            while field and field not in seen and len(seen) < 20000:
                seen.add(field)
                kind = self.obj_name(self.class_of(field))
                struct_name = None
                if kind == "StructProperty" and self.u32(field + STRUCT_OF):
                    struct_name = self.obj_name(self.u32(field + STRUCT_OF))
                out.setdefault(self.obj_name(field), (kind, self.u16(field + OFFSET), self.u16(field + ELEMENT_SIZE), struct_name))
                field = self.u32(field + NEXT)
            c = self.u32(c + SUPER)
        self._props[cls] = out
        return out

    def field(self, cls: int, name: str, kind: str, size: int, struct_name: str | None = None) -> int:
        props = self.properties(cls)
        if name not in props:
            raise MemoryError_(f"property {name} not found on {self.obj_name(cls)}")
        k, off, sz, sn = props[name]
        if k != kind or sz != size or (struct_name and sn != struct_name):
            raise MemoryError_(f"{name} is {k}/{sz}/{sn}, expected {kind}/{size}/{struct_name}")
        return off

    def check_layout(self) -> list[str]:
        """Proof the assumed layout is right for this exe, from facts the engine must satisfy. Returns the facts."""
        pc = self.find_class("PlayerController")
        chain = self.class_chain(pc)
        if chain[:4] != ["PlayerController", "Controller", "Actor", "Object"]:
            raise MemoryError_(f"PlayerController parent chain is {chain}, expected PlayerController>Controller>Actor>Object")
        package = self.obj_name(self.outermost(pc))
        if package != "Engine":
            raise MemoryError_(f"PlayerController class sits in package {package!r}, expected 'Engine' (Outer offset wrong?)")
        actor = self.find_class("Actor")
        self.field(actor, "Rotation", "StructProperty", 12, "Rotator")
        self.field(actor, "Location", "StructProperty", 12, "Vector")
        self.field(pc, "Pawn", "ObjectProperty", 4)
        return [f"PlayerController chain {'>'.join(chain[:4])}", "PlayerController class in package Engine",
                "Actor.Rotation is a 12-byte Rotator, Actor.Location a 12-byte Vector, Controller.Pawn an object reference"]

    # --- the player ------------------------------------------------------------------------------------------
    def player(self) -> tuple[int, int]:
        """(controller, pawn): the single PlayerController instance with a live Pawn whose class derives from Pawn."""
        pc_class = self.find_class("PlayerController")
        pawn_off = self.field(pc_class, "Pawn", "ObjectProperty", 4)
        found = []
        for obj in self.objects():
            try:
                cls = self.class_of(obj)
                if cls == pc_class or "PlayerController" in self.class_chain(cls)[1:]:
                    if self.obj_name(obj).startswith("Default__") or self.u32(obj + OUTER) == 0:
                        continue
                    pawn = self.u32(obj + pawn_off)
                    if pawn and "Pawn" in self.class_chain(self.class_of(pawn)):
                        found.append((obj, pawn))
            except MemoryError_:
                continue
        if len(found) != 1:
            raise MemoryError_(f"expected exactly one PlayerController with a Pawn, found {len(found)} (menu or loading?)")
        return found[0]

    def _vec(self, obj: int, name: str, struct_name: str, fmt: str):
        off = self.field(self.class_of(obj), name, "StructProperty", 12, struct_name)
        return obj + off, struct.unpack(fmt, self.mem.read(obj + off, 12))

    def rotation(self, controller: int) -> tuple[float, float, int]:
        """(yaw degrees, pitch degrees, raw roll) of the controller = the camera direction."""
        _, (pitch, yaw, roll) = self._vec(controller, "Rotation", "Rotator", "<iii")
        return units_to_deg(yaw), units_to_deg(pitch), roll

    def set_rotation(self, controller: int, yaw_deg: float, pitch_deg: float) -> None:
        address, (_, _, roll) = self._vec(controller, "Rotation", "Rotator", "<iii")
        self.mem.write(address, struct.pack("<iii", deg_to_units(pitch_deg), deg_to_units(yaw_deg), roll))

    def location(self, actor: int) -> tuple[float, float, float]:
        return self._vec(actor, "Location", "Vector", "<fff")[1]

    def set_location(self, actor: int, xyz) -> None:
        """Moves an actor by writing Actor.Location (Blacklist's actors have no reflected Velocity to clear). Whether
        the game keeps the new place (and streams the area in) is not known from code: callers must read the
        location back and check it."""
        address, _ = self._vec(actor, "Location", "Vector", "<fff")
        self.mem.write(address, struct.pack("<fff", *(float(v) for v in xyz)))

    def map_name(self, controller: int) -> str:
        return self.obj_name(self.outermost(controller))


def attach(pid: int, exe_hashes: list[str]) -> tuple[Game, Process]:
    """Open the game, check its exe hash against the play setup's list, check the layout. Raises on any mismatch."""
    import hashlib
    proc = Process(pid)
    try:
        base, path = proc.main_module()
        with open(path, "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest().upper()
        if digest not in {h.upper() for h in exe_hashes}:
            raise MemoryError_(f"{os.path.basename(path)} hash {digest[:16]}... is not a known Blacklist DX9 exe")
        game = Game(proc, base)
        game.check_layout()
        return game, proc
    except Exception:
        proc.close()
        raise
