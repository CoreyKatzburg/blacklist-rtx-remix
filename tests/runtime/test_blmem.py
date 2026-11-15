"""blmem.py against a fake process memory laid out like the DX9 game's engine tables."""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import blmem  # noqa: E402


class FakeMemory:
    """Sparse 32-bit address space with a bump allocator."""

    def __init__(self):
        self.pages, self.next = {}, 0x10000000

    def _page(self, a):
        return self.pages.setdefault(a >> 12, bytearray(4096))

    def read(self, address, size):
        out = bytearray()
        for a in range(address, address + size):
            if (a >> 12) not in self.pages:
                raise blmem.MemoryError_(f"unmapped 0x{a:08X}")
            out.append(self.pages[a >> 12][a & 0xFFF])
        return bytes(out)

    def write(self, address, data):
        for i, b in enumerate(data):
            self._page(address + i)[(address + i) & 0xFFF] = b

    def u32(self, address, value):
        self.write(address, struct.pack("<I", value))

    def alloc(self, size):
        a = self.next
        self.next += (size + 15) & ~15
        self.write(a, bytes(size))
        return a


class World:
    """Names, classes and objects: Object<-Actor<-Controller<-PlayerController, Actor<-Pawn<-ESam."""

    def __init__(self, base=blmem.IMAGE_BASE, pc_super="Controller", pc_package="Engine", controllers=1):
        self.m, self.base = FakeMemory(), base
        self.names, self.objects = [], []
        self.cls = {}
        self.cls["Class"] = self.obj("Class", 0)
        self.m.u32(self.cls["Class"] + blmem.CLASS, self.cls["Class"])
        engine = self.obj("Engine", 0)
        self.package = {"Engine": engine, "Core": self.obj("Core", 0), "Other": self.obj("Other", 0)}
        for n in ("Object", "Actor", "Controller", "PlayerController", "Pawn", "ESam",
                  "StructProperty", "ObjectProperty", "Struct", "Rotator", "Vector", "Level", "Package"):
            outer = engine if n not in ("ESam",) else self.package["Other"]
            if n == "PlayerController":
                outer = self.package[pc_package] if pc_package in self.package else engine
            self.cls[n] = self.obj(n, self.cls["Class"], outer)
        for child, parent in (("Actor", "Object"), ("Controller", "Actor"), ("PlayerController", pc_super),
                              ("Pawn", "Actor"), ("ESam", "Pawn"), ("Level", "Object")):
            self.m.u32(self.cls[child] + blmem.SUPER, self.cls[parent])
        rot = self.prop("Rotation", "StructProperty", 0x110, 12, "Rotator")
        loc = self.prop("Location", "StructProperty", 0x100, 12, "Vector", nxt=rot)
        self.m.u32(self.cls["Actor"] + blmem.PROPERTY_LINK, loc)
        self.m.u32(self.cls["Controller"] + blmem.PROPERTY_LINK, self.prop("Pawn", "ObjectProperty", 0x200, 4))
        mp = self.obj("S_CON_Global", self.cls["Package"])
        level = self.obj("MyLevel", self.cls["Level"], mp)
        self.obj("Default__PlayerController", self.cls["PlayerController"], engine, size=0x300)
        self.pawn = self.obj("ESam_0", self.cls["ESam"], level, size=0x300)
        self.m.write(self.pawn + 0x100, struct.pack("<fff", 100.0, -200.0, 77.0))
        self.pcs = []
        for i in range(controllers):
            pc = self.obj(f"PlayerController_{i}", self.cls["PlayerController"], level, size=0x300)
            self.m.u32(pc + 0x200, self.pawn)
            self.m.write(pc + 0x110, struct.pack("<iii", 0xF000, 0x4000, 7))   # pitch -22.5, yaw 90, roll 7
            self.pcs.append(pc)
        self.tables()

    def name(self, text):
        if text not in self.names:
            self.names.append(text)
        return self.names.index(text)

    def obj(self, name, cls, outer=0, size=0x60):
        a = self.m.alloc(size)
        self.m.u32(a + blmem.NAME, self.name(name))
        self.m.u32(a + blmem.CLASS, cls)
        self.m.u32(a + blmem.OUTER, outer)
        self.objects.append(a)
        return a

    def prop(self, name, kind, offset, size, struct_name=None, nxt=0):
        p = self.obj(name, self.cls[kind], size=0x90)
        self.m.write(p + blmem.ARRAY_DIM, struct.pack("<HHH", 1, size, offset))
        self.m.u32(p + blmem.NEXT, nxt)
        if struct_name:
            self.m.u32(p + blmem.STRUCT_OF, self.cls[struct_name])
        return p

    def tables(self):
        entries = []
        for text in self.names:
            e = self.m.alloc(8 + len(text) + 1)
            self.m.write(e + 8, text.encode() + b"\0")
            entries.append(e)
        names = self.m.alloc(4 * len(entries))
        for i, e in enumerate(entries):
            self.m.u32(names + 4 * i, e)
        objs = self.m.alloc(4 * (len(self.objects) + 2))
        for i, o in enumerate(self.objects):
            self.m.u32(objs + 4 * i, o)          # two trailing null slots, like freed objects
        g = lambda va: self.base + va - blmem.IMAGE_BASE
        self.m.write(g(blmem.GNAMES_VA), struct.pack("<III", names, len(entries), len(entries)))
        self.m.write(g(blmem.GOBJECTS_VA), struct.pack("<III", objs, len(self.objects) + 2, len(self.objects) + 2))

    def game(self):
        return blmem.Game(self.m, self.base)


class BlmemTest(unittest.TestCase):
    def test_layout_and_player(self):
        w = World()
        g = w.game()
        facts = g.check_layout()
        self.assertIn("PlayerController chain PlayerController>Controller>Actor>Object", facts)
        pc, pawn = g.player()
        self.assertEqual((pc, pawn), (w.pcs[0], w.pawn))
        self.assertEqual(g.map_name(pc), "S_CON_Global")
        self.assertEqual(g.location(pawn), (100.0, -200.0, 77.0))
        yaw, pitch, roll = g.rotation(pc)
        self.assertAlmostEqual(yaw, 90.0); self.assertAlmostEqual(pitch, -22.5); self.assertEqual(roll, 7)

    def test_set_rotation_writes_units_and_keeps_roll(self):
        w = World()
        g = w.game()
        pc, _ = g.player()
        g.set_rotation(pc, 450.0, -30.0)
        self.assertEqual(struct.unpack("<iii", w.m.read(pc + 0x110, 12)), ((-5461) & 0xFFFF, 0x4000, 7))
        yaw, pitch, _ = g.rotation(pc)
        self.assertAlmostEqual(yaw, 90.0); self.assertAlmostEqual(pitch, -30.0, places=2)

    def test_relocated_module(self):
        w = World(base=0x00A00000)
        self.assertEqual(w.game().player()[1], w.pawn)

    def test_wrong_parent_chain_is_refused(self):
        with self.assertRaisesRegex(blmem.MemoryError_, "parent chain"):
            World(pc_super="Actor").game().check_layout()

    def test_wrong_outer_offset_is_refused(self):
        with self.assertRaisesRegex(blmem.MemoryError_, "package"):
            World(pc_package="Core").game().check_layout()

    def test_ambiguous_player_is_refused(self):
        with self.assertRaisesRegex(blmem.MemoryError_, "exactly one"):
            World(controllers=2).game().player()

    def test_no_player_in_menu(self):
        with self.assertRaisesRegex(blmem.MemoryError_, "found 0"):
            World(controllers=0).game().player()

    def test_garbage_table_is_refused(self):
        w = World()
        w.m.write(w.base + blmem.GOBJECTS_VA - blmem.IMAGE_BASE, struct.pack("<III", 0x10000000, 9, 3))
        with self.assertRaisesRegex(blmem.MemoryError_, "TArray"):
            w.game().check_layout()

    def test_units(self):
        self.assertEqual(blmem.deg_to_units(90), 0x4000)
        self.assertEqual(blmem.deg_to_units(-90), 0xC000)
        self.assertAlmostEqual(blmem.units_to_deg(0xC000), -90.0)


if __name__ == "__main__":
    unittest.main()
