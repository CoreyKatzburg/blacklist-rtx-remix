"""Test bits in Blacklist's DX9 process memory: Sam invisible, Sam godmode, guards blind. Sets or clears them, reads
each one back, and reports what it wrote and what it read. A bit that does not read back as asked is reported as
failed, never as set.

Bits (offset and mask on the object; found by worker G in the game's own writers, Nov 16):
  invisible  Sam's pawn +0x374 bit 0x04    (SetInvisible writer 0x00F09720)
  godmode    Sam's pawn +0x4E0 bit 0x10    (SetGodmode writer 0x011BFA00)
  blind      every guard +0x4E4 bits 0xA00 (NpcSetBlind writer 0x015821F0). A guard is a live pawn other than Sam
             whose class has a bool property on each of the two bits; other pawns are counted and left alone.
Before writing, each bit must be owned by a bool property of the object's class (the bool's bit mask is read at
property+0x80); a bit with no owner is not written and reads as failed. Writes go one byte at a time, so the other
bits in the same 32-bit word keep their values.

In Python (drive.py already holds a blmem.Game):
    import game_bits
    result = game_bits.apply(game, ["godmode", "blind"], on=True)    # on=False clears them
    result["ok"]   # True only when every asked bit read back as asked
Command line:
    game_bits.py --pid N --exe-hash H [--exe-hash H2] --set godmode,blind [--out result.json]
    game_bits.py --pid N --exe-hash H --clear godmode,blind
    game_bits.py --selftest [--break-readback]
Exit codes: 0 every bit read back as asked, 1 a bit failed, 2 refused (unknown or empty bit list, or attach failed).

Result (apply and the command line print the same dict):
  on        True to set, False to clear          asked  the bit names, in the order given
  ok        every asked bit read back as asked   controller, pawn  hex addresses of the player found (None if none)
  error     why nothing could be written (no player found), else None
  bits      per name:
    invisible / godmode: ok, on, offset, mask, owner (bool property name, "" when the game leaves it unnamed),
                         before, after (the 32-bit word before writing and as read back), error
    blind:               ok, on, offset, mask, guardsFound, guardsWritten, guardsReadBack, otherPawns,
                         failed (up to 20 of {name, error}), error
    blind is ok only when guardsFound > 0 and guardsReadBack == guardsFound.
--break-readback (self-test only) makes the read-back return the asked value without reading memory; the self-test
must then fail, which shows that its read-back cases can fail.
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blmem  # noqa: E402

BOOL_MASK = 0x80                      # UBoolProperty bit mask at property+0x80 (probe_live check_bool_layout, Nov 16)
SAM_BITS = {"invisible": (0x374, 0x04), "godmode": (0x4E0, 0x10)}
BLIND = (0x4E4, 0xA00)
NAMES = ("invisible", "godmode", "blind")
FAILED_LISTED = 20


def _readback(game, obj: int, offset: int, mask: int, on: bool) -> tuple[bool, int]:
    """(bits read back as asked, the word read). The self-test's --break-readback replaces this."""
    word = game.u32(obj + offset)
    return (word & mask) == (mask if on else 0), word


def _bools(game, cls: int) -> dict[tuple[int, int], str]:
    """(offset, one-bit mask) -> bool property name, for cls and its parents."""
    out, seen, c = {}, set(), cls
    while c:
        field = game.u32(c + blmem.PROPERTY_LINK)
        while field and field not in seen and len(seen) < 20000:
            seen.add(field)
            if game.obj_name(game.class_of(field)) == "BoolProperty":
                out.setdefault((game.u16(field + blmem.OFFSET), game.u32(field + BOOL_MASK)), game.obj_name(field))
            field = game.u32(field + blmem.NEXT)
        c = game.u32(c + blmem.SUPER)
    return out


def _owner(bools: dict, offset: int, mask: int) -> str | None:
    """Names of the bool properties on each bit of mask, joined; None when a bit has no bool property."""
    names = []
    for bit in range(32):
        m = 1 << bit
        if mask & m:
            if (offset, m) not in bools:
                return None
            names.append(bools[(offset, m)])
    return ",".join(names)


def _write(game, obj: int, offset: int, mask: int, on: bool) -> None:
    for byte in range(4):
        m = (mask >> (8 * byte)) & 0xFF
        if m:
            old = game.mem.read(obj + offset + byte, 1)[0]
            new = (old | m) if on else (old & ~m & 0xFF)
            if new != old:
                game.mem.write(obj + offset + byte, bytes([new]))


def _sam_bit(game, pawn: int, offset: int, mask: int, on: bool) -> dict:
    r = {"ok": False, "on": on, "offset": f"0x{offset:X}", "mask": f"0x{mask:X}", "owner": None,
         "before": None, "after": None, "error": None}
    try:
        r["owner"] = _owner(_bools(game, game.class_of(pawn)), offset, mask)
        if r["owner"] is None:
            r["error"] = f"no bool property on bit 0x{mask:X} at +0x{offset:X} of Sam's class: not written"
            return r
        r["before"] = f"0x{game.u32(pawn + offset):08X}"
        _write(game, pawn, offset, mask, on)
        ok, word = _readback(game, pawn, offset, mask, on)
        r["after"] = f"0x{word:08X}"
        r["ok"] = ok
        if not ok:
            r["error"] = f"read back 0x{word & mask:X} after writing {'on' if on else 'off'}"
    except blmem.MemoryError_ as e:
        r["error"] = f"memory: {e}"
    return r


def _pawns(game, sam: int):
    """Live pawns other than Sam: (address, name, class)."""
    for obj in game.objects():
        try:
            if obj == sam or not game.u32(obj + blmem.OUTER):
                continue
            name = game.obj_name(obj)
            cls = game.class_of(obj)
            if name.startswith("Default__") or "Pawn" not in game.class_chain(cls)[1:]:
                continue
            yield obj, name, cls
        except blmem.MemoryError_:
            continue


def _blind(game, sam: int, on: bool) -> dict:
    offset, mask = BLIND
    r = {"ok": False, "on": on, "offset": f"0x{offset:X}", "mask": f"0x{mask:X}", "guardsFound": 0,
         "guardsWritten": 0, "guardsReadBack": 0, "otherPawns": 0, "failed": [], "error": None}
    cache: dict[int, str | None] = {}
    try:
        for obj, name, cls in _pawns(game, sam):
            if cls not in cache:
                try:
                    cache[cls] = _owner(_bools(game, cls), offset, mask)
                except blmem.MemoryError_:
                    cache[cls] = None
            if cache[cls] is None:
                r["otherPawns"] += 1
                continue
            r["guardsFound"] += 1
            try:
                _write(game, obj, offset, mask, on)
                r["guardsWritten"] += 1
                ok, word = _readback(game, obj, offset, mask, on)
                if ok:
                    r["guardsReadBack"] += 1
                else:
                    r["failed"].append({"name": name, "error": f"read back 0x{word & mask:X}"})
            except blmem.MemoryError_ as e:
                r["failed"].append({"name": name, "error": f"memory: {e}"})
    except blmem.MemoryError_ as e:
        r["error"] = f"memory: {e}"
    r["failed"] = r["failed"][:FAILED_LISTED]
    if not r["error"] and r["guardsFound"] == 0:
        r["error"] = f"no guards found ({r['otherPawns']} other pawns have no blind bits)"
    r["ok"] = not r["error"] and r["guardsFound"] > 0 and r["guardsReadBack"] == r["guardsFound"]
    return r


def check_names(bits) -> list[str]:
    """The asked names as a list; ValueError for an empty list, an unknown name or a name given twice."""
    bits = [b.strip() for b in (bits.split(",") if isinstance(bits, str) else bits) if b.strip()]
    if not bits:
        raise ValueError("no bits asked")
    unknown = [b for b in bits if b not in NAMES]
    if unknown:
        raise ValueError(f"unknown bits {unknown}; known: {', '.join(NAMES)}")
    if len(set(bits)) != len(bits):
        raise ValueError(f"a bit is named twice: {bits}")
    return bits


def apply(game, bits, on: bool = True) -> dict:
    """Set (on=True) or clear (on=False) the named bits and read each back. Raises ValueError only for a bad bit
    list, before any write; every other problem is reported in the result as a failed bit."""
    bits = check_names(bits)
    res = {"on": on, "asked": bits, "ok": False, "controller": None, "pawn": None, "error": None,
           "bits": {}}
    try:
        controller, pawn = game.player()
    except blmem.MemoryError_ as e:
        res["error"] = f"no player: {e}"
        res["bits"] = {b: {"ok": False, "on": on, "error": res["error"]} for b in bits}
        return res
    res["controller"], res["pawn"] = f"0x{controller:08X}", f"0x{pawn:08X}"
    for b in bits:
        res["bits"][b] = _blind(game, pawn, on) if b == "blind" else _sam_bit(game, pawn, *SAM_BITS[b], on)
    res["ok"] = all(r["ok"] for r in res["bits"].values())
    return res


def apply_pid(pid: int, exe_hashes: list[str], bits, on: bool = True) -> dict:
    """apply() on a running game: attach (exe hash and layout checked by blmem.attach), write, close."""
    check_names(bits)
    game, proc = blmem.attach(pid, exe_hashes)
    try:
        return apply(game, bits, on)
    finally:
        proc.close()


# --- self-test -------------------------------------------------------------------------------------------------
def selftest(break_readback: bool = False) -> int:
    global _readback
    import test_blmem

    class Mem:
        """The fake memory with a write counter and addresses whose writes are dropped (the game resetting a bit)."""

        def __init__(self, m):
            self.m, self.writes, self.drop = m, 0, set()

        def read(self, a, n):
            return self.m.read(a, n)

        def write(self, a, data):
            self.writes += 1
            if not any(a <= d < a + len(data) for d in self.drop):
                self.m.write(a, data)

    class W(test_blmem.World):
        """Sam with invisible and godmode bools, `guards` EAIPawns with the two blind bools, one pawn without them."""

        def __init__(self, guards=3, sam_has_god=True):
            super().__init__()
            m, engine = self.m, self.package["Engine"]
            for n in ("BoolProperty", "EAIPawn", "EOtherPawn"):
                self.cls[n] = self.obj(n, self.cls["Class"], engine)
            for child in ("EAIPawn", "EOtherPawn"):
                m.u32(self.cls[child] + blmem.SUPER, self.cls["Pawn"])
            spec = [("bInvisible", 0x374, 0x04), ("bFlagA", 0x374, 0x08)] + \
                ([("bGod", 0x4E0, 0x10)] if sam_has_god else []) + [("bFlag", 0x4E0, 0x20)]
            m.u32(self.cls["Pawn"] + blmem.PROPERTY_LINK, self.bools(spec))
            m.u32(self.cls["EAIPawn"] + blmem.PROPERTY_LINK, self.bools([("bBlindA", 0x4E4, 0x200),
                                                                         ("bBlindB", 0x4E4, 0x800)]))
            level = struct.unpack("<I", m.read(self.pawn + blmem.OUTER, 4))[0]
            self.objects.remove(self.pawn)
            self.pawn = self.obj("ESam_1", self.cls["ESam"], level, size=0x600)
            for pc in self.pcs:
                m.u32(pc + 0x200, self.pawn)
            m.u32(self.pawn + 0x4E0, 0x00038020)          # neighbour bits that must survive
            m.u32(self.pawn + 0x4E4, 0x00000100)
            self.guards = [self.obj(f"EAIPawn_{i}", self.cls["EAIPawn"], level, size=0x600) for i in range(guards)]
            for g in self.guards:
                m.u32(g + 0x4E4, 0x00000101)
            self.other = self.obj("EOtherPawn_0", self.cls["EOtherPawn"], level, size=0x600)
            m.u32(self.other + 0x4E4, 0x00000100)
            self.obj("Default__EAIPawn", self.cls["EAIPawn"], engine, size=0x600)
            self.tables()
            self.mem = Mem(self.m)

        def bools(self, spec):
            nxt = 0
            for name, offset, mask in spec:
                p = self.prop(name, "BoolProperty", offset, 4, nxt=nxt)
                self.m.u32(p + BOOL_MASK, mask)
                nxt = p
            return nxt

        def game(self):
            return blmem.Game(self.mem, self.base)

        def word(self, obj, off):
            return struct.unpack("<I", self.m.read(obj + off, 4))[0]

    if break_readback:
        _readback = lambda game, obj, offset, mask, on: (True, (mask if on else 0))  # noqa: E731
    results = []

    def check(label, ok, detail=""):
        results.append(bool(ok))
        print(("PASS  " if ok else "FAIL  ") + label + (f"  [{detail}]" if detail else ""))

    # 1. Sam's bits read back: set, then clear; neighbour bits untouched
    w = W()
    r = apply(w.game(), ["invisible", "godmode"], True)
    check("invisible and godmode set and read back", r["ok"] and r["bits"]["godmode"]["ok"] and r["bits"]["invisible"]["ok"]
          and w.word(w.pawn, 0x4E0) == 0x00038030 and w.word(w.pawn, 0x374) & 0x0C == 0x04,
          f"godmode {r['bits']['godmode']['before']} -> {r['bits']['godmode']['after']}")
    r = apply(w.game(), "invisible,godmode", False)
    check("invisible and godmode cleared and read back", r["ok"] and w.word(w.pawn, 0x4E0) == 0x00038020
          and w.word(w.pawn, 0x374) & 0x04 == 0, f"godmode after {r['bits']['godmode']['after']}")

    # 2. blind: 3 guards written and read back; the other pawn, the Default__ object and Sam untouched; then cleared
    w = W(guards=3)
    r = apply(w.game(), ["blind"], True)["bits"]["blind"]
    check("blind on: 3 guards found, written, read back", r["ok"] and (r["guardsFound"], r["guardsWritten"],
          r["guardsReadBack"], r["otherPawns"]) == (3, 3, 3, 1) and all(w.word(g, 0x4E4) == 0xB01 for g in w.guards)
          and w.word(w.other, 0x4E4) == 0x100 and w.word(w.pawn, 0x4E4) == 0x100, json.dumps(r))
    r = apply(w.game(), ["blind"], False)["bits"]["blind"]
    check("blind off: 3 guards cleared and read back", r["ok"] and r["guardsReadBack"] == 3
          and all(w.word(g, 0x4E4) == 0x101 for g in w.guards))

    # 3. FAKE: godmode write dropped (the game puts it back) -> godmode failed, invisible still ok, overall not ok
    w = W()
    w.mem.drop.add(w.pawn + 0x4E0)
    r = apply(w.game(), ["godmode", "invisible"], True)
    g = r["bits"]["godmode"]
    check("FAKE godmode that does not read back is failed, not set", not r["ok"] and not g["ok"]
          and r["bits"]["invisible"]["ok"] and "read back" in (g["error"] or ""), json.dumps(g))

    # 4. FAKE: one guard drops the write -> blind failed with 2 of 3 read back, the guard named
    w = W(guards=3)
    w.mem.drop.add(w.guards[1] + 0x4E5)
    r = apply(w.game(), ["blind"], True)["bits"]["blind"]
    check("FAKE blind with one guard not reading back is failed", not r["ok"] and r["guardsFound"] == 3
          and r["guardsWritten"] == 3 and r["guardsReadBack"] == 2 and [f["name"] for f in r["failed"]] == ["EAIPawn_1"],
          json.dumps(r))

    # 5. FAKE: a clear that does not read back is failed too
    w = W()
    apply(w.game(), ["godmode"], True)
    w.mem.drop.add(w.pawn + 0x4E0)
    r = apply(w.game(), ["godmode"], False)
    check("FAKE godmode clear that does not read back is failed", not r["ok"] and w.word(w.pawn, 0x4E0) & 0x10)

    # 6. FAKE: blind with zero guards found
    w = W(guards=0)
    r = apply(w.game(), ["blind"], True)["bits"]["blind"]
    check("FAKE blind with zero guards found is failed", not r["ok"] and r["guardsFound"] == 0 and r["otherPawns"] == 1
          and "no guards" in (r["error"] or ""), json.dumps(r))

    # 7. FAKE: unknown, empty or doubled bit names refused before any write
    for bad in (["godmode", "fly"], [], "godmode,godmode"):
        w = W()
        try:
            apply(w.game(), bad, True)
            check(f"FAKE bits {bad!r} refused", False, "accepted")
        except ValueError as e:
            check(f"FAKE bits {bad!r} refused, 0 writes", w.mem.writes == 0, str(e))

    # 8. FAKE: Sam's class has no bool on the godmode bit -> not written, failed
    w = W(sam_has_god=False)
    r = apply(w.game(), ["godmode"], True)
    check("FAKE godmode with no bool property is failed, not written", not r["ok"] and w.mem.writes == 0
          and w.word(w.pawn, 0x4E0) == 0x00038020, r["bits"]["godmode"]["error"])

    # 9. FAKE: no player (menu or loading) -> every asked bit failed, nothing written
    w = W()
    for pc in w.pcs:
        w.m.u32(pc + 0x200, 0)
    r = apply(w.game(), ["godmode", "blind"], True)
    check("FAKE no player: every bit failed, 0 writes", not r["ok"] and w.mem.writes == 0
          and all(not b["ok"] for b in r["bits"].values()), r["error"])

    n, total = sum(results), len(results)
    print(f"{n} of {total} pass" + ("  (read-back broken on purpose: this run must fail)" if break_readback else ""))
    return 0 if n == total else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--break-readback", action="store_true", help="self-test only: fake a read-back that always agrees")
    ap.add_argument("--pid", type=int)
    ap.add_argument("--exe-hash", action="append", default=[])
    ap.add_argument("--set", dest="set_bits")
    ap.add_argument("--clear", dest="clear_bits")
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.selftest:
        return selftest(a.break_readback)
    if not a.pid or not a.exe_hash or (a.set_bits is None) == (a.clear_bits is None):
        ap.error("give --pid, at least one --exe-hash, and exactly one of --set or --clear")
    try:
        res = apply_pid(a.pid, a.exe_hash, a.set_bits if a.set_bits is not None else a.clear_bits,
                        on=a.set_bits is not None)
    except (ValueError, blmem.MemoryError_, OSError) as e:
        print(json.dumps({"ok": False, "refused": f"{type(e).__name__}: {e}"}))
        return 2
    text = json.dumps(res, indent=1)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
    print(text)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
