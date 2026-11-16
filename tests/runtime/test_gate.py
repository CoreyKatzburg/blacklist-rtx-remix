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

    def test_large_shift_compares_without_shift_not_nan(self):
        # Phase correlation can report a shift that leaves no area to compare (16 of 36 rows here). That returned NaN,
        # which every "> limit" test silently reads as "not a flash". Expected value: the unshifted 98th percentile
        # of 8x8 block means of |a - b| over the 2 px border that the comparison keeps.
        a = self.rng.uniform(0, 255, (36, 64)).astype(np.float32)
        b = np.roll(a, (16, 0), axis=(0, 1))
        d = np.abs(a - b)[2:-2, 2:-2]
        blocks = d[:32, :56].reshape(4, 8, 7, 8).mean(axis=(1, 3))
        self.assertAlmostEqual(gate.mc_error(a, b), float(np.percentile(blocks, 98)), places=4)

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

    def test_is_zero_or_empty_camera(self):
        self.assertFalse(gate.is_zero_or_empty_camera(["0", "1.0", "1.6", "0.5", "3", "0"]))  # 6 cols -> unknown
        self.assertFalse(gate.is_zero_or_empty_camera(["0", "1.0"]))                            # cut-short -> unknown
        self.assertFalse(gate.is_zero_or_empty_camera(["0", "1.0", "14.0", "0.5", "1500", "1", "G:0:0:1."]))
        self.assertFalse(gate.is_zero_or_empty_camera(["0", "1.0", "14.0", "0.5", "1500", "1",
                                                       "G:0:0:-:0.0:0.0:1.0:0.0:0.0:0.0"]))
        self.assertTrue(gate.is_zero_or_empty_camera(["0", "1.0", "1.6", "0.5", "3", "0", ""]))
        self.assertTrue(gate.is_zero_or_empty_camera(["545", "1.0", "1.6", "0.5", "29", "3",
                                                      "G:0:0:-0.000:-0.000:-0.000:1.0:0.0:0.0:0.0"]))
        self.assertFalse(gate.is_zero_or_empty_camera(["10", "1.0", "1.6", "0.5", "29", "3",
                                                       "G:0:1:10.0:20.0:30.0:1.0:0.0:0.0:0.0"]))
        self.assertFalse(gate.is_zero_or_empty_camera(["3050", "1.0", "16.0", "1.0", "1428", "8",
                                                       "G:0:0:3769.8:3085.6:225.2:0.14:0.08:-0.98:0.0"]))

    def test_spot_mouse_lag(self):
        self.assertIsNone(gate.spot_mouse_lag({}))
        self.assertIsNone(gate.spot_mouse_lag({"mouse_lag": {"samples_ms": [20.0] * 19}}))
        res = gate.spot_mouse_lag({"mouse_lag": {"samples_ms": [20.0] * 10 + [24.0] * 10,
                                                 "game_share_ms": 14.0, "after_game_ms": 8.0}})
        self.assertIsNotNone(res)
        self.assertAlmostEqual(res["median"], 22.0)
        self.assertAlmostEqual(res["spread"], 4.0)
        self.assertAlmostEqual(res["game"], 14.0)
        self.assertAlmostEqual(res["after"], 8.0)

    def test_brightness_hdr_clip_and_black_raster_guard(self):
        b_dir = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "analysis", "brightness"))
        if b_dir not in sys.path:
            sys.path.insert(0, b_dir)
        import pfm_brightness  # noqa: E402

        f3 = np.full((36, 64, 3), 3.0, np.float32)
        r1 = np.full((36, 64, 3), 1.0, np.float32)
        cmp_hdr = pfm_brightness.compare_final_to_raster(f3, r1)
        self.assertAlmostEqual(cmp_hdr["gamma_mean_ratio"], 1.0, places=5)

        r0 = np.zeros((36, 64, 3), np.float32)
        cmp_blk = pfm_brightness.compare_final_to_raster(f3, r0)
        self.assertTrue(cmp_blk["black_raster"])
        self.assertIsNone(cmp_blk["gamma_mean_ratio"])


class LatencyTableTest(unittest.TestCase):
    def test_present_row_reads_the_present_duration_not_the_clock(self):
        # Real rows: the entry clock is ~5.5 million ms, so reading it as a duration gave a 5,616,411 ms median.
        n = 30
        proxy = {f: [str(f), f"{5_500_000 + 41.0 * f:.3f}", "45.000", f"{40.0 + f % 3:.3f}", "1400", "8"]
                 for f in range(1, n + 1)}
        spot = {"name": "s", "_data": {"telemetry": {}, "proxy": proxy}, "windows": []}
        window = {"kind": "still", "telemetry": [0, n], "proxy": [0, n]}
        rows = gate.build_latency_contributors([spot], [(spot, window)])
        present = [r for r in rows if r["contributor"].startswith("D3D9 Present")]
        game = [r for r in rows if r["contributor"].startswith("Game Engine CPU")]
        self.assertEqual(len(present), 1)
        self.assertAlmostEqual(present[0]["medianMs"], float(np.median([40.0 + f % 3 for f in range(1, n + 1)])), 3)
        self.assertAlmostEqual(game[0]["medianMs"], float(np.median([45.0 - 40.0 - f % 3 for f in range(1, n + 1)])), 3)


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
                    if case == "near_target_fps":
                        fps_check = next(c for c in result["checks"] if c["check"] == "min_median_fps")
                        self.assertIn("89.96 FPS", fps_check["detail"])
                    if case == "near_target_89_996":
                        fps_check = next(c for c in result["checks"] if c["check"] == "min_median_fps")
                        self.assertIn("89.996 FPS", fps_check["detail"])
                    if case == "load_count_mismatch":
                        hitch = next(c for c in result["checks"] if c["check"] == "frames_over_50ms")
                        self.assertEqual(hitch["value"], 1)
                        self.assertIn("1 loading-screen frames excluded", hitch["detail"])

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

    def test_clean_quit_real_sessions(self):
        fail_dir = r"D:\BlacklistOverhaul\experiments\playtest-evidence\steam-session-20261114-1855"
        pass_dir = r"D:\BlacklistOverhaul\experiments\playtest-evidence\play-setup-logs\before-20261113-212037"
        if os.path.isdir(fail_dir):
            st, val, detail = gate.check_clean_quit_dirs([fail_dir])
            self.assertEqual(st, gate.FAIL, detail)
            self.assertIn("VK_ERROR_DEVICE_LOST", detail)
        if os.path.isdir(pass_dir):
            st, val, detail = gate.check_clean_quit_dirs([pass_dir])
            self.assertEqual(st, gate.PASS, detail)
            self.assertIn("not-disposed", detail)


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
