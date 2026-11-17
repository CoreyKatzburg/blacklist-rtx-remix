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
        lpulse = calm.copy(); lpulse[6:11, 16:32, 40:64] = np.clip(lpulse[6:11, 16:32, 40:64] + 32, 0, 255)
        self.assertEqual(gate.find_flashes(lpulse), [])                     # 5-frame local pulse > max_len=3
        self.assertGreater(gate.still_flicker(lpulse)[2], gate.THRESHOLDS["still_block_pulse"])
        strobe = calm.copy(); strobe[2::4, 16:32, 40:56] = np.clip(strobe[2::4, 16:32, 40:56] + 10, 0, 255)
        self.assertEqual(gate.find_flashes(strobe), [])                     # 1-in-4 local strobe below flashes floor
        self.assertGreater(gate.still_flicker(strobe)[3], gate.THRESHOLDS["still_block_step4"])

    def test_overlay_box_masked_in_screen_capture_checks(self):
        # The NVIDIA performance overlay (tests/analysis/overlay_mask.py) changes its digits every frame. Screen-capture
        # frames must have its box blanked the same way in every frame, and nothing outside it: a change only inside
        # the box reads no flash or flicker, a change starting one row below or one column left is still caught, and
        # the unmasked frames do read the inside shimmer as flicker (so the compare can fail). The box is about 1% of
        # the picture, under flashes' 98th-percentile block cut, so flashes alone cannot show the unmasked failure.
        w, h = 128, 72
        x0, y0, x1, y1 = gate.overlay_mask.box_at(w, h)
        base = self.rng.uniform(40, 150, (h, w)).astype(np.float32)
        spot = {"_dir": None, "_data": {"telemetry": {}}}
        win = {"frames": "w.gray", "frameSize": [w, h]}

        def frames_with(region, amount, every=None):
            f = np.array([base + self.rng.uniform(-1, 1, (h, w)) for _ in range(18)], np.float32)
            for i in (every if every is not None else [6]):
                f[i][region] += amount
            return np.clip(f, 0, 255).astype(np.uint8)

        inside = (slice(y0, y1 + 1), slice(x0, x1 + 1))
        below = (slice(y1 + 1, y1 + 9), slice(x0, x1 + 1))
        left = (slice(y0, y0 + 16), slice(x0 - 16, x0))
        with tempfile.TemporaryDirectory() as t:
            spot["_dir"] = t
            path = os.path.join(t, "w.gray")
            for name, region, flash in (("inside", inside, []), ("below", below, [6]), ("left", left, [6])):
                with self.subTest(case=name):
                    frames_with(region, 80).tofile(path)
                    masked, src = gate.window_frames(spot, win)
                    raw = gate.read_gray(path, w, h)
                    self.assertIn("overlay box masked", src)
                    self.assertEqual(gate.find_flashes(masked), flash)
                    np.testing.assert_array_equal(masked[:, y1 + 1, x0:x1 + 1], raw[:, y1 + 1, x0:x1 + 1])
                    np.testing.assert_array_equal(masked[:, y0:y1 + 1, x0 - 1], raw[:, y0:y1 + 1, x0 - 1])
                    self.assertEqual(float(np.abs(masked[:, y0:y1 + 1, x0:x1 + 1]).max()), 0.0)
            frames_with(inside, 30, every=range(1, 18, 2)).tofile(path)      # digits shimmering every other frame
            masked, _ = gate.window_frames(spot, win)
            self.assertGreater(gate.still_flicker(gate.read_gray(path, w, h))[0], gate.THRESHOLDS["still_block_jitter"])
            self.assertLessEqual(gate.still_flicker(masked)[0], gate.THRESHOLDS["still_block_jitter"])

            def goggles(region):
                on = frames_with(region, 0, every=[])
                off = on.copy(); off[(slice(None),) + region] = np.clip(off[(slice(None),) + region] + 80.0, 0, 255)
                on.tofile(os.path.join(t, "on.gray")); off.tofile(os.path.join(t, "off.gray"))
                s = {"name": "g", "_dir": t, "windows": [
                    {"kind": "goggles", "goggles": {"mode": 1, "rt": rt, "toggled": True, "shot": f"{rt}.gray",
                                                    "frameSize": [w, h]}} for rt in ("on", "off")]}
                run = gate.Run.__new__(gate.Run); run.metrics = []
                gate.goggles_check(run, [s])
                return run.metrics[0]["detail"]
            self.assertIn("corr 1.00, brightness gap 0.000, missing blocks 0.0%", goggles(inside))
            self.assertNotIn("brightness gap 0.000", goggles(below))

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
        # input_latency_ms reads the game share (trials gameMs), not the screen samples: give the two different values.
        self.assertIsNone(gate.spot_mouse_lag({}))
        trials = [{"screenMs": None, "gameMs": 90.0 + (i % 2) * 8.0} for i in range(20)]
        self.assertIsNone(gate.spot_mouse_lag({"mouse_lag": {"samples_ms": [20.0] * 20, "trials": trials[:19]}}))
        res = gate.spot_mouse_lag({"mouse_lag": {"samples_ms": [20.0] * 20, "game_share_ms": 1.0,
                                                 "trials": trials + [{"screenMs": 30.0, "gameMs": None}]}})
        self.assertAlmostEqual(res["median"], 94.0)
        self.assertAlmostEqual(res["spread"], 8.0)
        self.assertEqual(res["n"], 20)
        self.assertAlmostEqual(gate.spot_screen_lag({"mouse_lag": {"samples_ms": [20.0] * 10 + [24.0] * 10}})["median"],
                               22.0)

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
                        rt_c = next(c for c in result["checks"] if c["check"] == "remix_ray_tracing")
                        self.assertIn("timeToFirstRayTracedS=not measured", rt_c["detail"])
                        self.assertIn("preRtCompiles=-", rt_c["detail"])
                    if case == "launch_rt_delay":
                        rt_c = next(c for c in result["checks"] if c["check"] == "remix_ray_tracing")
                        self.assertIn("timeToFirstRayTracedS=2.0s", rt_c["detail"])
                        self.assertIn("preRtCompiles=3", rt_c["detail"])
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

    def test_clean_quit_20261116_082350(self):
        # quit_menu: ray tracing on, the minimize lost the device and the game exited 0xC0000005 without Terminate.
        # quit_altf4: ray tracing on, closed by SC_CLOSE, exit 0, no device loss.
        base = r"D:\BlacklistOverhaul\experiments\remix-gate\20261116-082350"
        if not os.path.isdir(base):
            return
        st, val, detail = gate.check_clean_quit_dirs([os.path.join(base, "quit_menu")])
        self.assertEqual(st, gate.FAIL, detail)
        self.assertIn("VK_ERROR_DEVICE_LOST", detail)
        self.assertIn("0xC0000005", detail)
        st, val, detail = gate.check_clean_quit_dirs([os.path.join(base, "quit_altf4")])
        self.assertEqual(st, gate.PASS, detail)

    def test_clean_quit_crash_exit_without_device_loss(self):
        # A crash exit fails even when remix-dxvk.log is clean and Terminate was never logged.
        with tempfile.TemporaryDirectory() as t:
            with open(os.path.join(t, "remix-dxvk.log"), "w") as f:
                f.write("[08:00:00.000] info:  nothing wrong here\n")
            with open(os.path.join(t, "drive.log"), "w") as f:
                f.write('QUIT {"exited": true, "exitCode": 3221225477}\n')
            st, val, detail = gate.check_clean_quit_dirs([t])
            self.assertEqual(st, gate.FAIL, detail)
            self.assertIn("non-zero exit", detail)

    def test_ray_tracing_checks_on_20261116_063147(self):
        base = r"D:\BlacklistOverhaul\experiments\remix-gate\20261116-063147"
        if not os.path.isdir(base):
            return
        for spot_name, want_rt, want_cs in (("con_bright_hall", gate.FAIL, gate.FAIL),
                                            ("con_custodial", gate.PASS, gate.PASS)):
            sdir = os.path.join(base, spot_name)
            if not os.path.isdir(sdir):
                continue
            s = gate.load_json(os.path.join(sdir, "spot.json"))
            s["_dir"] = sdir
            tel, _ = gate.read_telemetry(gate.spot_path(s, s.get("telemetry")))
            s["_data"] = {
                "telemetry": tel,
                "proxy": gate.read_csv_frames(gate.spot_path(s, s.get("proxyFrames")), 7),
                "pgate": gate.read_csv_frames(gate.spot_path(s, s.get("proxyGate")), 6),
            }
            with tempfile.TemporaryDirectory() as td:
                gate.save_json(os.path.join(td, "manifest.json"), {"spots": [spot_name]})
                r = gate.Run(td)
                gate.ray_tracing_checks(r, [s], "rtx_telemetry.jsonl")
                by_name = {m["check"]: m for m in r.metrics}
                self.assertEqual(by_name["remix_ray_tracing"]["status"], want_rt, (spot_name, by_name["remix_ray_tracing"]))
                self.assertEqual(by_name["remix_compile_stall"]["status"], want_cs, (spot_name, by_name["remix_compile_stall"]))

    def test_frame_gap_on_06_dlss_perf_1(self):
        """Worker C's case: the DLSS performance launch of the settings sweep has Present-to-Present gaps over 50 ms in
        its measured windows while no Present call and no Remix frame time is over 50 ms. frame_gap_over_50ms must
        FAIL on it (3 gaps: 5410, 5435, 6397; C's fourth, 5689, is the frame before look_y135 starts) and present_stalls
        must PASS. The same rows with those gaps set to 33 ms must PASS."""
        base = r"D:\BlacklistOverhaul\experiments\remix-gate\20261116-113127-settings"
        if not os.path.isdir(os.path.join(base, "06_dlss_perf_1")):
            self.skipTest("settings sweep not on this machine")
        run = gate.Run(base)
        run.manifest = {"spots": ["06_dlss_perf_1"]}
        good = gate.load_spots(run)
        pairs = [(s, w) for s in good for w in s["windows"] if w["kind"] in gate.MEASURED]
        gate.proxy_over_50ms(run, "frame_gap_over_50ms", 2, "a Present-to-Present gap", "frame_gap_max", pairs, False)
        gate.proxy_over_50ms(run, "present_stalls", 3, "Present", "present_stalls_max", pairs, False)
        by = {m["check"]: m for m in run.metrics}
        self.assertEqual((by["frame_gap_over_50ms"]["status"], by["frame_gap_over_50ms"]["value"]), (gate.FAIL, 3))
        self.assertIn("worst 61.5 ms at frame 5410", by["frame_gap_over_50ms"]["detail"])
        self.assertEqual(by["present_stalls"]["status"], gate.PASS, by["present_stalls"])
        prx = good[0]["_data"]["proxy"]
        for pf in (5410, 5435, 6397):
            prx[pf][2] = "33"
        run.metrics.clear()
        gate.proxy_over_50ms(run, "frame_gap_over_50ms", 2, "a Present-to-Present gap", "frame_gap_max", pairs, False)
        self.assertEqual(run.metrics[0]["status"], gate.PASS, run.metrics[0])
        # The window-start rule must not hide a gap: 5689 is printed even when the check passes.
        self.assertRegex(run.metrics[0]["detail"], r"1 over 50 ms at window starts, not counted: [\d.]+ ms at frame 5689")

    def test_glow_blobs_counts_on_real_stops(self):
        """Ledger rows 1 and 2: worker B's glow_blobs.py (9ddc95d) through the gate's own call and parsing, against the
        original game at con_bright_hall (reference 000454). 142638 y0_p0, stand-in balls on: 12 orbs and 2 puffs, no
        lost glow. 142638's balls-off switch step: 0/0/0. 063147 y0_p0 (never ray traced, dark): 6 of 21 original
        glows lost, the reverse direction. y135_p0: poses 20 cm apart, not compared (exit 2)."""
        g = r"D:\BlacklistOverhaul\experiments\remix-gate"
        og = os.path.join(g, "reference-20261116-000454", "con_bright_hall")
        rx = os.path.join(g, "20261115-142638", "con_bright_hall")
        dark = os.path.join(g, "20261116-063147", "con_bright_hall")
        if not all(os.path.isdir(d) for d in (og, rx, dark)):
            self.skipTest("runs 142638, 063147 or reference 000454 not on this machine")

        def counts(remix, still, orig):
            code, out = gate.glow_blobs_stop(remix, og, still, orig)
            return code, tuple(int(rx_.search(out)[1]) if rx_.search(out) else None for rx_ in gate._GLOW_COUNTS.values())

        self.assertEqual(counts(rx, "y0_p0", "y0_p0"), (1, (12, 2, 0)))
        self.assertEqual(counts(rx, "switch_flare_balls_off", "y0_p0"), (0, (0, 0, 0)))
        self.assertEqual(counts(dark, "y0_p0", "y0_p0"), (1, (13, 5, 6)))
        self.assertEqual(gate.glow_blobs_stop(rx, og, "y135_p0", "y135_p0")[0], 2)
    def test_lamps_dimmed_on_142638(self):
        """lights_lamps_dimmed (worker B's 32a905d): 142638 con_bright_hall's switch steps against switch_base,
        switch_base_again and reference 000454's y0_p0. Real: every change dims 0 of 5 lamps, PASS; the goggles switch
        is left out. Fake: glow_blobs reporting 1 dimmed lamp on one change must FAIL with that change named; a pose
        mismatch (exit 2) must give NOT MEASURED, not PASS."""
        g = r"D:\BlacklistOverhaul\experiments\remix-gate"
        if not all(os.path.isdir(os.path.join(g, r, "con_bright_hall"))
                   for r in ("20261115-142638", "reference-20261116-000454")):
            self.skipTest("run 142638 or reference 000454 not on this machine")

        def spots(r):
            run = gate.Run(os.path.join(g, r))
            run.manifest = {"spots": ["con_bright_hall"]}
            return gate.load_spots(run)

        good, ref = spots("20261115-142638"), {s["name"]: s for s in spots("reference-20261116-000454")}

        def score():
            run = gate.Run.__new__(gate.Run)
            run.dir, run.manifest, run.metrics = "", {}, []
            gate.lamps_dimmed_check(run, good, ref, True)
            return run.metrics[0]

        # 142638 records no Sam location (no raster json), so the reference position rule places it by the camera alone.
        m = score()
        self.assertEqual((m["status"], m["value"]), (gate.PASS, 0), m)
        self.assertIn("0 of 4 changes", m["detail"])
        # The reference still is picked by pose: y0_p0_after is 0.2 cm from the switch pose, y0_p0 0.9 cm.
        self.assertIn("switch_flare_balls_off (vs original y0_p0_after) 0 of", m["detail"])
        self.assertNotIn("goggles", m["detail"])
        real = gate.glow_dimmed_stop
        try:
            gate.glow_dimmed_stop = lambda r, o, on, os_, ch, ag: (
                (1, "glow_blobs: dimmed 1 of 5 lamps the original shows (fake): FAIL") if ch == "switch_flare_balls_off"
                else real(r, o, on, os_, ch, ag))
            m = score()
            self.assertEqual((m["status"], m["value"]), (gate.FAIL, 1), m)
            self.assertIn("switch_flare_balls_off (vs original y0_p0_after) 1 of 5", m["detail"])
            gate.glow_dimmed_stop = lambda *a: (2, "glow_blobs: NOT MEASURED, changed pose differs from on by 40 cm")
            self.assertEqual(score()["status"], gate.NOT_MEASURED)
            # The see-through probe's names (worker B): on = switch_on, change = switch_off, on_again = switch_on_again.
            calls = []
            gate.glow_dimmed_stop = lambda *a: (calls.append(a[2:]) or
                                                (0, "glow_blobs: dimmed 0 of 5 lamps the original shows (fake): PASS"))
            rename = {"switch_base": "switch_on", "switch_flare_balls_off": "switch_off",
                      "switch_base_again": "switch_on_again"}
            probe = dict({k: v for k, v in good[0].items() if k != "_positions"}, windows=[dict(w, name=rename[w["name"]]) if w["name"] in rename else w
                                           for w in good[0]["windows"] if w["kind"] != "switch" or w["name"] in rename])
            run = gate.Run.__new__(gate.Run)
            run.dir, run.manifest, run.metrics = "", {}, []
            gate.lamps_dimmed_check(run, [probe], ref, True)
            self.assertEqual(calls, [("switch_on", "y0_p0_after", "switch_off", "switch_on_again")])
            self.assertEqual(run.metrics[0]["status"], gate.PASS, run.metrics[0])
            # No reference still at the on pose (only y90_p0 left): NOT MEASURED, never a fixed y0_p0.
            far = {k: dict(v, windows=[w for w in v["windows"] if w["name"] == "y90_p0"]) for k, v in ref.items()}
            calls.clear()
            run.metrics.clear()
            gate.lamps_dimmed_check(run, good, far, True)
            self.assertEqual(run.metrics[0]["status"], gate.NOT_MEASURED, run.metrics[0])
            self.assertIn("no reference still within 8 cm", run.metrics[0]["detail"])
            self.assertEqual(calls, [])
        finally:
            gate.glow_dimmed_stop = real

    @staticmethod
    def _pose_spot(td, name, stills, loc_of=None, teleport_before=None, trace=True):
        """A fake spot: stills = [(name, yaw asked, pitch asked, camera xyz, world yaw, world pitch)], one 5-frame
        window each, in order; loc_of: name -> Sam's raster-json location; teleport_before: still name preceded by a
        teleport window."""
        d = os.path.join(td, name)
        os.makedirs(os.path.join(d, "gate"))
        rows, wins = [], []
        for i, (n, ry, rp, xyz, wy, wp) in enumerate(stills):
            if n == teleport_before:
                wins.append({"name": f"{n}_teleport", "kind": "teleport", "teleport": {"landed": (loc_of or {}).get(n)}})
            fx, fy, fz = (math.cos(math.radians(wp)) * math.cos(math.radians(wy)),
                          math.cos(math.radians(wp)) * math.sin(math.radians(wy)), math.sin(math.radians(wp)))
            for fr in range(i * 5 + 1, i * 5 + 6):
                rows.append(f"{fr},0,16,1,100,1,G:0:0:{xyz[0]}:{xyz[1]}:{xyz[2]}:{fx}:{fy}:{fz}\n")
            wins.append({"name": n, "kind": "still", "yaw": ry, "pitch": rp, "frames": [i * 5, i * 5 + 5],
                         "proxy": [i * 5, i * 5 + 5]})
            if (loc_of or {}).get(n):
                write(os.path.join(d, "gate", f"{n}_raster.json"), json.dumps({"location": loc_of[n]}))
        path = os.path.join(d, "proxy_frames.csv")
        if trace:
            write(path, "".join(rows))
        return {"name": name, "_dir": d, "windows": wins, "_data": {"proxy": gate.read_csv_frames(path, 7)}}

    @staticmethod
    def _stub_run():
        run = gate.Run.__new__(gate.Run)
        run.dir, run.manifest, run.metrics = "", {}, []
        return run

    def test_pose_after_teleport_is_relative_to_the_teleport(self):
        """Worker B's 96.94 deg case: drive.py asks a still's yaw relative to the last teleport's yaw. A still after a
        teleport (yaw 170) asked at yaw 0 and measured at world 170 must read 0 deg off, whether the teleport window is
        recorded or only Sam's raster-json location moved (the unrecorded return teleport). Without either marker it
        is scored against the spawn's stop and reads 170, which is what the old rule did."""
        base = [("y0_p0", 0, 0, (0, 0, 0), 0.0, 0.0), ("y90_p0", 90, 0, (0, 0, 0), 90.0, 0.0),
                ("lampsA", 0, 0, (300, 0, 0), 170.0, 0.0)]
        here, there = [0.0, 0.0, 0.0], [300.0, 0.0, 0.0]
        cases = {"teleport window": (None, "lampsA", 0.0),
                 "location only": (dict(y0_p0=here, y90_p0=here, lampsA=there), None, 0.0),
                 "no marker (old rule)": (None, None, 170.0)}
        with tempfile.TemporaryDirectory() as td:
            for i, (case, (locs, tp, want)) in enumerate(cases.items()):
                with self.subTest(case):
                    run = self._stub_run()
                    gate.pose_check(run, [self._pose_spot(td, f"s{i}", base, locs, tp)])
                    self.assertAlmostEqual(run.metrics[0]["value"], want, delta=0.05, msg=run.metrics[0])

    def test_reference_position_rule_on_real_con_boiler(self):
        """E2's find: references 175705 and 174116 took con_boiler y0_p0 from another place (Sam 7.9 and 7.3 units
        off, the camera 65 forward) at a matching angle; 045456 matches Remix launch 085935 within the limits. With the
        position limits opened up, 175705 passes, so the new rule is what rejects it."""
        g = r"D:\BlacklistOverhaul\experiments\remix-gate"
        runs = ("20261116-085935", "reference-20261116-175705", "reference-20261116-174116",
                "reference-20261116-045456")
        if not all(os.path.isdir(os.path.join(g, r)) for r in runs):
            self.skipTest("con_boiler runs not on this machine")

        def sp(r, name):
            d = os.path.join(g, r, name)
            s = gate.load_json(os.path.join(d, "spot.json"))
            s["_dir"] = d
            return s

        def check(r):
            return gate.reference_position(sp(runs[0], "capture_boiler_custodial"), "con_boiler_y0_p0",
                                           sp(r, "con_boiler"), "y0_p0")

        for r, sam, cam in ((runs[1], "Sam 7.9", "camera 65.1"), (runs[2], "Sam 7.3", "camera 64.4")):
            ok, why = check(r)
            self.assertIs(ok, False, why)
            self.assertIn(sam, why)
            self.assertIn(cam, why)
        ok, why = check(runs[3])
        self.assertIs(ok, True, why)
        self.assertIn("Sam 1.0 apart, camera 2.9 apart", why)
        saved = gate.REF_SAM_UNITS, gate.REF_CAM_UNITS
        try:
            gate.REF_SAM_UNITS = gate.REF_CAM_UNITS = 1e9
            self.assertIs(check(runs[1])[0], True)
        finally:
            gate.REF_SAM_UNITS, gate.REF_CAM_UNITS = saved

    def test_reference_position_rule_fake_spots(self):
        """Fake stops: the rule accepts a reference within 2 units of Sam and 4 of the camera at the same angle, and
        rejects Sam 3 apart, the camera 5 apart, and a matching place with the yaw 3 deg off (the old angle rule).
        No Sam location on one side is placed by the camera alone, with "Sam location not recorded" in the line: the
        camera within the limit is accepted, 5 apart rejected. No camera trace reads None, with or without Sam. reference_position_check turns a rejected stop into NOT
        MEASURED with both distances, never FAIL."""
        sam, cam = [-12165.0, -2519.0, -676.5], (-11974.0, -2476.0, -629.0)

        def stop(xyz=cam, yaw=0.0, loc=sam):
            return [("y0_p0", 0, 0, xyz, yaw, 0.0)], ({"y0_p0": loc} if loc else None)

        with tempfile.TemporaryDirectory() as td:
            remix = self._pose_spot(td, "remix", *stop())
            cases = {"near": (stop((cam[0] + 3, cam[1], cam[2]), loc=[sam[0] + 1.5, sam[1], sam[2]]), True, True),
                     "sam 3 apart": (stop(loc=[sam[0], sam[1] + 3, sam[2]]), True, False),
                     "camera 5 apart": (stop((cam[0] + 5, cam[1], cam[2])), True, False),
                     "yaw 3 deg off": (stop(yaw=3.0), True, False),
                     "no camera trace": (stop(), False, None),
                     "no Sam, camera near": (stop((cam[0] + 3, cam[1], cam[2]), loc=None), True, True),
                     "no Sam, camera 5 apart": (stop((cam[0] + 5, cam[1], cam[2]), loc=None), True, False),
                     "no camera, no Sam": (stop(loc=None), False, None)}
            for i, (case, ((stills, locs), trace, want)) in enumerate(cases.items()):
                with self.subTest(case):
                    ref = self._pose_spot(td, f"ref{i}", stills, locs, trace=trace)
                    ok, why = gate.reference_position(remix, "y0_p0", ref, "y0_p0")
                    self.assertIs(ok, want, why)
                    if case.startswith("no Sam"):
                        self.assertIn("Sam location not recorded (reference", why)
                    run = self._stub_run()
                    remix["name"] = ref["name"]
                    gate.reference_position_check(run, [remix], {ref["name"]: ref})
                    m = run.metrics[0]
                    self.assertEqual(m["status"], gate.PASS if want else gate.NOT_MEASURED, m)
                    if want is False:
                        self.assertRegex(m["detail"], r"Sam (location not recorded|[\d.]+ apart).*, camera [\d.]+ apart")

    def test_session_mode_fake_sessions(self):
        with tempfile.TemporaryDirectory() as td:
            for case in fakerun.SESSION_CASES:
                ok, why = fakerun.check_session_case(td, case)
                self.assertTrue(ok, f"{case}: {why}")

    def test_session_freezes_on_20261115_135449(self):
        """Worker C's freeze spec on Corey's session: (a) 0 freezes in play, and the 4931 ms gap on the last frame
        67921 is the quit (quit_gap_ms 4931); (b) frame 40000's gap set to 1500 ms fails on exactly that frame;
        (c) with freeze_scan's edge rule off the quit frame 67921 fails, so the rule is what keeps the quit out."""
        src = os.path.join(r"D:\BlacklistOverhaul\experiments\playtest-evidence\play-setup-logs",
                           "before-20261115-135449", "d3d9_proxy_frames.csv")
        if not os.path.exists(src):
            self.skipTest("session logs not on this machine")

        def score(path):
            run = gate.Run.__new__(gate.Run)
            run.dir, run.manifest, run.metrics = "", {}, []
            gate.freeze_checks(run, path)
            return {m["check"]: m for m in run.metrics}

        by = score(src)
        m = by["freezes_in_play_over_1s"]
        self.assertEqual((m["status"], m["value"]), (gate.PASS, 0), m)
        self.assertEqual(by["quit_gap_ms"]["value"], 4931)
        self.assertIn("4931 ms on last frame 67921", by["quit_gap_ms"]["detail"])
        self.assertIn("end frame 67921: 4931 ms", by["freeze_gaps_excluded"]["detail"])

        with tempfile.TemporaryDirectory() as td:
            dst = os.path.join(td, "d3d9_proxy_frames.csv")
            with open(src, encoding="ascii", errors="replace") as fi, open(dst, "w", encoding="ascii") as fo:
                for line in fi:
                    if line.startswith("40000,"):
                        p = line.split(",", 3)
                        line = ",".join(p[:2] + ["1500.000", p[3]])
                    fo.write(line)
            m = score(dst)["freezes_in_play_over_1s"]
            self.assertEqual((m["status"], m["value"]), (gate.FAIL, 1), m)
            self.assertIn("frame 40000: gap 1500 ms", m["detail"])

        fs = gate.freeze_scan_module()
        orig = fs.classify
        fs.classify = lambda frames, limit, edge_rule=True: orig(frames, limit, edge_rule=False)
        try:
            m = score(src)["freezes_in_play_over_1s"]
        finally:
            fs.classify = orig
        self.assertEqual(m["status"], gate.FAIL, m)
        self.assertIn("frame 67921: gap 4931 ms", m["detail"])

    def test_session_mode_on_20261115_135449(self):
        """Corey's free-play session: goggles go off at proxy frame 41475 and Remix rebuilds 94 static meshes on the
        next frame (the dark frame he saw), so goggles_toggle_rebuilds must FAIL and name that goggles-off."""
        d = r"D:\BlacklistOverhaul\experiments\playtest-evidence\play-setup-logs\before-20261115-135449"
        if not os.path.exists(os.path.join(d, "rtx_telemetry.jsonl")):
            self.skipTest("session logs not on this machine")
        result = gate.evaluate_session(d)
        by = {c["check"]: c for c in result["checks"]}
        g = by["goggles_toggle_rebuilds"]
        self.assertEqual(g["status"], gate.FAIL, g)
        self.assertIn("94 at 41475", g["detail"])
        # Around that goggles-off: 64.7 ms at 41472 (inside goggles, not loading) counts, 49.1 ms at 41477 is under 50.
        slow = {fr: (ms, why) for fr, ms, why in result["slowFrames"]}
        self.assertEqual(slow.get(41472), (64.7, None))
        self.assertNotIn(41477, slow)
        self.assertEqual(by["frames_over_50ms"]["status"], gate.FAIL)
        # compile_wait.py's reference: first ray-traced frame 27.1 s after launch start.
        self.assertIn("timeToFirstRayTracedS=27.1s", by["remix_ray_tracing"]["detail"])
        self.assertEqual(by["remix_ray_tracing"]["status"], gate.PASS, by["remix_ray_tracing"])
        self.assertEqual(by["compile_wait_stuck"]["status"], gate.PASS)
        self.assertEqual(by["never_ray_traced"]["status"], gate.PASS)
        self.assertNotIn("instanced_shared_slot_draws", by)   # removed: the game has no hardware instancing


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
