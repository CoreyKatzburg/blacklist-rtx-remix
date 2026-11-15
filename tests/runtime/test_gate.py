"""gate.py: the math on synthetic inputs, then whole fake runs (good must pass, each broken one must fail on its
own check, missing data must be NOT MEASURED), plus the driver's pure helpers."""
import json
import math
import os
import sys
import tempfile
import threading
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import drive  # noqa: E402
import fakerun  # noqa: E402
import gate  # noqa: E402


def write(path, text, mode="w"):
    with open(path, mode, newline="") as f:
        f.write(text)


def read(path):
    with open(path, newline="") as f:
        return f.read()


class MathTest(unittest.TestCase):
    def setUp(self):
        self.rng = np.random.default_rng(3)

    def pan(self, n=40, step=3):
        return np.array([fakerun.view(i * step / fakerun.PX_PER_DEG, 0, self.rng) for i in range(n)], np.float32)

    def test_smooth_pan_has_no_flash(self):
        self.assertEqual(gate.find_flashes(self.pan()), [])

    def test_one_frame_flash_found(self):
        f = self.pan(); f[17] = np.clip(f[17] * 0.4, 0, 255)
        self.assertEqual(gate.find_flashes(f), [17])

    def test_two_frame_flash_found_once(self):
        f = self.pan(); f[10] += 60; f[11] += 60
        self.assertEqual(gate.find_flashes(f), [10])

    def test_local_pop_found(self):
        f = self.pan(); f[25, 10:30, 50:74] = 0
        self.assertEqual(gate.find_flashes(f), [25])

    def test_duplicate_captures_are_not_flashes(self):
        f = self.pan(); f = np.concatenate([f[:12], f[11:12], f[12:]])
        self.assertEqual(len(gate.dedupe(f)), 40)
        self.assertEqual(gate.find_flashes(f), [])

    def test_lasting_change_is_not_a_flash(self):
        f = self.pan(); f[20:] = np.clip(f[20:] + 40, 0, 255)   # e.g. a light switched on and stayed on
        self.assertEqual(gate.find_flashes(f), [])

    def test_still_flicker_catches_shimmer_and_pulsing_not_single_pop(self):
        calm = np.array([fakerun.view(0, 0, self.rng) for _ in range(18)], np.float32)
        self.assertLess(max(gate.still_flicker(calm)), 1.0)
        pop = calm.copy(); pop[7, 20:40, 40:64] = 255 - pop[7, 20:40, 40:64]
        self.assertLess(max(gate.still_flicker(pop)), 2.0)                  # single-frame pop belongs to flashes
        shimmer = calm.copy(); shimmer[1::2, 20:36, 40:56] = np.clip(shimmer[1::2, 20:36, 40:56] + 10, 0, 255)
        self.assertEqual(gate.find_flashes(shimmer), [])                    # below the flashes floor of 12...
        self.assertGreater(gate.still_flicker(shimmer)[0], gate.THRESHOLDS["still_block_jitter"])
        pulse = np.array([np.clip(calm[i] + 12 * math.sin(i * 2 * math.pi / 12), 0, 255) for i in range(18)], np.float32)
        self.assertEqual(gate.find_flashes(pulse), [])                      # ...and slow pulsing is missed by flashes
        self.assertGreater(gate.still_flicker(pulse)[1], gate.THRESHOLDS["still_luma_drift"])

    def test_neon(self):
        img = np.full((10, 10, 3), 0.5, np.float32)
        img[0, 0] = (1.2, 1.1, 1.15)         # white highlight
        img[1, 1] = (1.5, 0.1, 1.2)          # neon
        img[2, 2] = (np.nan, 0, 0)
        nan, neon = gate.neon_stats(img)
        self.assertEqual(nan, 1); self.assertAlmostEqual(neon, 0.01)

    def test_outline_overlap(self):
        d = np.full((200, 300), 1500.0, np.float32); d[50:150, 120:170] = 240.0
        self.assertAlmostEqual(gate.outline_overlap(d, d, 300)[0], 1.0)
        self.assertAlmostEqual(gate.outline_overlap(d, gate.nearest_resize(d, (100, 150)), 300)[0], 1.0)
        moved = np.roll(d, 12, axis=1)
        self.assertLess(gate.outline_overlap(d, moved, 300)[0], 0.6)
        empty = np.full_like(d, 1500.0)
        self.assertEqual(gate.outline_overlap(d, empty, 300)[0], 0.0)
        self.assertIsNone(gate.outline_overlap(empty, d, 300)[0])     # Sam not in view: can't be measured

    def test_pfm_roundtrip(self):
        with tempfile.TemporaryDirectory() as t:
            img = np.arange(24, dtype=np.float32).reshape(2, 4, 3); img[0, 0, 0] = np.inf
            gate.write_pfm(os.path.join(t, "a.pfm"), img)
            np.testing.assert_array_equal(gate.read_pfm(os.path.join(t, "a.pfm")), img)
            gate.write_pfm(os.path.join(t, "b.pfm"), img[..., 1])
            np.testing.assert_array_equal(gate.read_pfm(os.path.join(t, "b.pfm")), img[..., 1])

    def test_camera_angles(self):
        cy, cp = math.radians(-120), math.radians(20)
        pub = f"P:22:1:0:0:0:1:0:0:0|G:40:0:0:0:0:{math.cos(cp) * math.cos(cy)}:{math.cos(cp) * math.sin(cy)}:{math.sin(cp)}:0"
        y, p = gate.camera_angles(["5", "1", "2", "3", "4", "2", pub])
        self.assertAlmostEqual(y, -120, places=4); self.assertAlmostEqual(p, 20, places=4)
        self.assertIsNone(gate.camera_angles(["5", "1", "2", "3", "4", "0", ""]))
        self.assertAlmostEqual(gate.wrap(350), -10)

    def test_telemetry_contract(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "t.jsonl")
            write(p, '{"frame":1,"frameMs":3}\nnot json\n')
            data, problems = gate.read_telemetry(p)
            self.assertIn(1, data)
            self.assertTrue(any("frameTimeMs" in x for x in problems))
            self.assertTrue(any("not JSON" in x for x in problems))


class FakeRunTest(unittest.TestCase):
    """Every case in fakerun.CASES, each a full run folder written exactly as drive.py writes it."""

    def test_cases(self):
        with tempfile.TemporaryDirectory() as t:
            for case in fakerun.CASES:
                with self.subTest(case=case):
                    ok, result, why = fakerun.check_case(t, case)
                    self.assertTrue(ok, f"{case}: {why}")
                    self.assertTrue(os.path.exists(os.path.join(t, case, "result.md")))
                    if case == "good":
                        md = read(os.path.join(t, case, "result.md"))
                        self.assertIn("## Top 10 Latency Contributors", md)
                        self.assertGreaterEqual(len(result.get("latencyContributors", [])), 4)

    def test_missing_spot_folder_fails(self):
        with tempfile.TemporaryDirectory() as t:
            run = fakerun.make_run(t, "good")
            gate.save_json(os.path.join(run, "manifest.json"), {"spots": ["s1", "never_ran"]})
            r = gate.evaluate(run)
            self.assertEqual(r["status"], gate.FAIL)
            self.assertIn("never_ran: setup", [c["check"] for c in r["checks"] if c["status"] == gate.FAIL])

    def test_empty_manifest_fails(self):
        with tempfile.TemporaryDirectory() as t:
            gate.save_json(os.path.join(t, "manifest.json"), {"spots": []})
            self.assertEqual(gate.evaluate(t)["status"], gate.FAIL)


class DriveHelpersTest(unittest.TestCase):
    def test_set_ini_replaces_and_inserts(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "d3d9_proxy.ini")
            write(p, "[Proxy]\r\nLog=1\r\nGateDump=old\r\n[Lights]\r\nAutoRaytracing=1\r\n[RemixConfig]\r\n")
            drive.set_ini(p, "Proxy", "GateDump", "y45_p0")
            drive.set_ini(p, "Lights", "AutoRaytracing", "0")
            drive.set_ini(p, "RemixConfig", "rtx.enableRaytracing", "False")
            text = read(p)
            self.assertIn("GateDump=y45_p0\r\n", text)
            self.assertNotIn("old", text)
            self.assertIn("AutoRaytracing=0", text)
            self.assertIn("[RemixConfig]\r\nrtx.enableRaytracing = False", text)

    def test_set_ini_waits_while_the_proxy_reads_the_file(self):
        # The proxy re-reads the ini about once a second. While it has the file open, Windows refuses the
        # replace (WinError 5). That ended a gate spot after 50 windows on 2026-11-12.
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "d3d9_proxy.ini")
            write(p, "[Proxy]\r\nGateDump=old\r\n")
            reader = open(p, "rb")                       # like the proxy: open without delete sharing
            timer = threading.Timer(0.3, reader.close)
            timer.start()
            try:
                drive.set_ini(p, "Proxy", "GateDump", "y90_p0")
            finally:
                timer.cancel()
                reader.close()
            self.assertIn("GateDump=y90_p0", read(p))
            self.assertFalse(os.path.exists(p + ".gate.tmp"))

    def test_tail_reads_last_frame_and_new_text(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "rtx_telemetry.jsonl")
            write(p, '{"frame": 7}\n')
            tail = drive.Tail([os.path.join(t, "missing.jsonl"), p], True)
            write(p, '{"frame": 8}\n{"frame": 9}\n{"fra', "a")    # last line still being written
            self.assertEqual(tail.last_frame(), 9)
            self.assertEqual(tail.new_text(), '{"frame": 8}\n{"frame": 9}\n{"fra')
            write(p, '{"frame": 0}\n')                     # writer truncated the file
            self.assertEqual(tail.new_text(), '{"frame": 0}\n')
            c = os.path.join(t, "f.csv")
            write(c, "1,2\n2,3\n")
            self.assertEqual(drive.Tail([c], False).last_frame(), 2)
            self.assertEqual(drive.Tail([os.path.join(t, "nope")], False).last_frame(), 0)


if __name__ == "__main__":
    unittest.main()
