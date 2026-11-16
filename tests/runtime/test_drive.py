"""Camera steering of drive.py against a fake game that, like Blacklist at gate frame rates, applies mouse input once
per frame and one frame late. Run from this folder: python -m unittest test_drive (about 15 s, no game)."""
import threading
import time
import unittest
from unittest import mock

import drive
import gate


class FakeGame:
    def __init__(self, fps: float, counts_per_deg_x: float, counts_per_deg_y: float, yaw: float = 10.0):
        self.cx, self.cy = counts_per_deg_x, counts_per_deg_y
        self.yaw, self.pitch, self.turned = yaw, 0.0, 0.0
        self.pending, self.queued = [0, 0], [0, 0]
        self.history: list[tuple[float, float]] = []
        self.lock, self.running = threading.Lock(), True
        self.thread = threading.Thread(target=self._frames, args=(1.0 / fps,), daemon=True)
        self.thread.start()

    ease_frames = 1   # frames the game spreads each mouse input over (time-based easing, so many frames at high FPS)

    def _frames(self, dt: float) -> None:
        easing: list[list[float]] = []   # [counts x left, counts y left, frames left]
        while self.running:
            time.sleep(dt)
            with self.lock:
                if any(self.queued):
                    easing.append([float(self.queued[0]), float(self.queued[1]), float(self.ease_frames)])
                mx = my = 0.0
                for e in easing:
                    mx, my = mx + e[0] / e[2], my + e[1] / e[2]
                    e[0], e[1], e[2] = e[0] - e[0] / e[2], e[1] - e[1] / e[2], e[2] - 1
                easing = [e for e in easing if e[2] > 0]
                dyaw = mx / self.cx
                self.yaw, self.turned = self.yaw + dyaw, self.turned + dyaw
                self.pitch = max(-89.0, min(89.0, self.pitch + my / self.cy))
                self.queued, self.pending = self.pending, [0, 0]
                self.history.append((self.turned, self.pitch))

    eat_first = 0.0   # share of the first input on each axis the game drops (seen live right after a load)

    def mouse(self, dx: int, dy: int) -> None:
        with self.lock:
            for i, d in ((0, dx), (1, dy)):
                if d and self.eat_first:
                    eaten = getattr(self, "eaten", set())
                    if i not in eaten:
                        self.eaten = eaten | {i}
                        d = int(d * (1.0 - self.eat_first))
                self.pending[i] += d

    def rotation(self, controller: int):
        with self.lock:
            return gate.wrap(self.yaw), self.pitch, 0

    def stop(self) -> None:
        self.running = False
        self.thread.join()


def make_spot(game: FakeGame, counts_per_deg_x: float, counts_per_deg_y: float) -> drive.Spot:
    s = drive.Spot.__new__(drive.Spot)   # no process to attach to
    s.game, s.controller = game, 0
    s.base_yaw, s.base_pitch = game.rotation(0)[:2]
    s.yaw = s.pitch = 0.0
    s.counts_per_deg_x, s.counts_per_deg_y = counts_per_deg_x, counts_per_deg_y
    s.calibration, s._sent, s.windows = {}, None, []
    s.counters = lambda: {"telemetry": 0, "proxy": 0}
    return s


class Steering(unittest.TestCase):
    def run_with(self, fps, true_x, true_y, believed_x, believed_y):
        self.game = FakeGame(fps, true_x, true_y)
        self.addCleanup(self.game.stop)
        patch = mock.patch.object(drive, "mouse_move_rel", self.game.mouse)
        patch.start()
        self.addCleanup(patch.stop)
        return make_spot(self.game, believed_x, believed_y)

    def test_calibration_finds_the_game_rate(self):
        s = self.run_with(22, 7.0, -9.0, 14.0, -14.0)
        s._calibrate_mouse()
        self.assertAlmostEqual(s.counts_per_deg_x, 7.0, delta=0.2)
        self.assertAlmostEqual(s.counts_per_deg_y, -9.0, delta=0.3)

    def test_calibration_survives_the_game_eating_part_of_the_first_input(self):
        # Live Paladin run: the first move read 3.97 counts per degree, the next three 2.43. Without the discarded
        # warm-up move this raised "inconsistent" and the spot was lost.
        s = self.run_with(22, 7.0, -9.0, 14.0, -14.0)
        self.game.eat_first = 0.4
        s._calibrate_mouse()
        self.assertAlmostEqual(s.counts_per_deg_x, 7.0, delta=0.2)
        self.assertAlmostEqual(s.counts_per_deg_y, -9.0, delta=0.3)
        self.assertGreater(s.calibration["warmupX"], 7.0 * 1.25)

    def test_stop_waits_out_a_stall_before_capturing(self):
        # Paladin, Nov 15: frames of 0.5 to 1.6 s for several seconds after the full turn.
        s = self.run_with(22, 7.0, -9.0, 7.0, -9.0)
        t0 = time.monotonic()
        s.proxy = mock.Mock(last_frame=lambda: int(max(0.0, time.monotonic() - t0 - 2.0) * 22))   # stalled 2 s
        waited = s.steady()
        self.assertGreater(waited, 2.0)
        self.assertLess(waited, 4.0)

    def test_spot_fails_once_sam_leaves_his_spot(self):
        # Green hallway, Nov 15: Sam ended up 3000 units from the teleport and the stops were taken elsewhere.
        s = drive.Spot.__new__(drive.Spot)
        s.pawn, s.home = 0, [100.0, 200.0, 50.0]
        s.game = mock.Mock(location=lambda pawn: (160.0, 280.0, 20.0))   # 100 units sideways
        self.assertEqual(s.sam_moved("y0_p0"), 100.0)
        s.game = mock.Mock(location=lambda pawn: (100.0, 360.0, 50.0))   # 160 units sideways
        with self.assertRaisesRegex(RuntimeError, "Sam moved 160 units"):
            s.sam_moved("y45_p0")

    def test_move_lands_on_the_stop_without_swinging_past_it(self):
        s = self.run_with(22, 7.0, -9.0, 7.0, -9.0)
        start = len(self.game.history)
        s.move("to_y45_p-30", 45.0, -30.0, 0.5)
        time.sleep(0.15)   # input still in flight lands
        path = self.game.history[start:]
        yaw, pitch = path[-1]
        self.assertLess(abs(yaw - 45.0), 0.5)
        self.assertLess(abs(pitch + 30.0), 0.5)
        self.assertLess(max(t for t, _ in path), 45.0 + 3.0, "yaw swung past the stop")
        self.assertGreater(min(p for _, p in path), -30.0 - 3.0, "pitch swung past the stop")

    def test_move_lands_on_the_stop_when_the_game_eases_input_over_many_fast_frames(self):
        # No-Remix reference, Custodial, Nov 16: ~178 FPS and the game eases each input over several frames; a
        # correction went out as soon as the first frame of the last one showed, they stacked, the camera swung
        # +-5 degrees and stops were recorded 4 to 7 degrees off (y0_p0 at pitch 4.6, y180_p0 at yaw -174).
        s = self.run_with(178, 45.0, -26.0, 45.0, -26.0)
        s.no_remix = True
        self.game.ease_frames = 8
        s.move("to_y135_p0", 135.0, -20.0, 1.0)
        time.sleep(0.15)   # input still in flight lands
        yaw, pitch = self.game.history[-1]
        self.assertLess(abs(yaw - 135.0), 0.5)
        self.assertLess(abs(pitch + 20.0), 0.5)

    def test_pan_turns_a_full_circle_when_the_calibration_is_half_the_true_rate(self):
        s = self.run_with(22, 7.0, -9.0, 3.5, -9.0)   # open loop this would turn 180 degrees
        before = self.game.turned
        turned = s.pan(2.0)
        actual = self.game.turned - before
        self.assertLess(abs(actual - 360.0), 3.0)
        self.assertLess(abs(turned - actual), 1.0)


class MouseLag(unittest.TestCase):
    """The mouse-to-screen helpers: they must time the first real change, and report nothing for noise alone."""
    def test_screen_change_is_the_first_shot_past_the_threshold(self):
        import numpy as np
        base = np.full((4, 4), 100, np.int16)
        shots = [(990.0, base + 9), (1010.0, base + 1), (1027.0, base + 2), (1043.0, base + 40), (1060.0, base + 60)]
        # the +9 shot came before the turn and must not count; +1 and +2 are noise under a 3-level threshold
        self.assertEqual(drive.first_change(shots, 1000.0, base, 3.0), 1043.0)
        self.assertIsNone(drive.first_change(shots[:3], 1000.0, base, 3.0))

    def test_game_turn_needs_the_camera_to_point_elsewhere(self):
        import math
        d = lambda deg: (math.cos(math.radians(deg)), math.sin(math.radians(deg)), 0.0)
        dirs = [(980.0, d(10)), (995.0, d(10)), (1012.0, d(10.2)), (1030.0, d(13)), (1050.0, d(15))]
        self.assertEqual(drive.first_turned(dirs, 1000.0), 1030.0)   # 0.2 degrees of wobble is not the turn
        self.assertIsNone(drive.first_turned(dirs[:3], 1000.0))
        self.assertIsNone(drive.first_turned(dirs[2:], 1000.0))      # no frame before the turn to compare with

    def test_proxy_rows_are_read_from_the_frame_log(self):
        import os, tempfile
        rows = ["0,900.0,1,1,1,1,G:0:0:1:2:3:1.0:0.0:0.0:0",
                "1,1000.5,1,1,1,1,G:5:1:9:9:9:0:0:1:0|G:0:0:1:2:3:0.0:1.0:0.0:0",
                "2,1016.0,1,1,1,1,"]
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "f.csv")
            with open(p, "w") as fh:
                fh.write("\n".join(["header"] + rows) + "\n")
            self.assertEqual(drive.proxy_camera_dirs(p, 950.0), [(1000.5, (0.0, 1.0, 0.0))])


class Reference(unittest.TestCase):
    """The no-Remix reference pictures: same units as Remix's final.pfm, and never recorded through the wrong renderer."""
    def test_grabs_are_averaged_in_display_units_and_a_flash_shows(self):
        import numpy as np
        w, h = 4, 2
        grey = np.full((h, w, 3), 128, np.uint8)
        flash = np.full((h, w, 3), 255, np.uint8)
        raw = b"".join(f.tobytes() for f in [grey, grey, flash, grey])
        img, lumas = drive.average_grabs(raw, w, h, 4)
        # mid grey must stay 128/255 = 0.502 like final.pfm; turned linear it would be 0.216 (a fake 2.3x error)
        self.assertAlmostEqual(float(img[0, 0, 0]), (128 * 3 + 255) / 4 / 255, places=5)
        self.assertEqual(img.shape, (h, w, 3))
        self.assertEqual((min(lumas), max(lumas)), (0.502, 1.0))
        with self.assertRaisesRegex(RuntimeError, "3 of 4 frames"):
            drive.average_grabs(raw[: 3 * w * h * 3], w, h, 4)

    def test_renderer_must_match_the_run(self):
        log = ("2026-11-15 21:42:24.180 [pid 25472] chained RTX Remix DLL: D:\\Steam\\x\\SYSTEM\\d3d9_remix.dll\r\n"
               "2026-11-15 21:50:00.000 [pid 999] chained INI DLL: C:\\Windows\\SysWOW64\\d3d9.dll\r\n")
        self.assertEqual(drive.check_renderer(drive.chained_dll(log, 25472), no_remix=False), "remix")
        self.assertEqual(drive.check_renderer(drive.chained_dll(log, 999), no_remix=True), "system")
        with self.assertRaisesRegex(RuntimeError, "needs Windows d3d9"):   # a "reference" drawn by Remix
            drive.check_renderer(drive.chained_dll(log, 25472), no_remix=True)
        with self.assertRaisesRegex(RuntimeError, "needs Remix"):          # a Remix run that fell back to Windows
            drive.check_renderer(drive.chained_dll(log, 999), no_remix=False)
        with self.assertRaisesRegex(RuntimeError, "no 'chained"):          # another process's line does not count
            drive.check_renderer(drive.chained_dll(log, 1234), no_remix=True)

    def test_calibration_spread_is_looser_only_without_remix(self):
        # Paladin, Nov 16, no Remix (~178 FPS): counts per degree 45 to 69 between moves. 1.6x must pass a reference
        # run and still fail a Remix run; 2.5x fails both.
        def calibrate(rates, no_remix):
            pos = [0.0, 0.0]
            seq = iter(rates * 4)
            def move(dx, dy):
                r = next(seq)
                pos[0] += dx / r
                pos[1] += dy / r
            s = drive.Spot.__new__(drive.Spot)
            s.no_remix = no_remix
            s._settled = lambda: (pos[0], pos[1], 0)
            with mock.patch.object(drive, "mouse_move_rel", move):
                s._calibrate_mouse()
            return s
        loose = [50.0, 80.0, 50.0, 80.0, 50.0, 80.0]
        self.assertEqual(calibrate(loose, True).calibration["spreadLimit"], 2.0)
        with self.assertRaisesRegex(RuntimeError, "inconsistent"):
            calibrate(loose, False)
        with self.assertRaisesRegex(RuntimeError, "inconsistent"):
            calibrate([40.0, 100.0, 40.0, 100.0, 40.0, 100.0], True)

    def test_rt_off_picture_needs_ten_frames_not_ray_traced(self):
        import json, os, tempfile, types
        def spot_with(flags):
            p = os.path.join(tmp, "t.jsonl")
            with open(p, "w") as fh:
                fh.write("\n".join(json.dumps({"frame": 100 + i, "rayTraced": f}) for i, f in enumerate(flags)) + "\n")
            return types.SimpleNamespace(telemetry=types.SimpleNamespace(path=p))
        with tempfile.TemporaryDirectory() as tmp:
            ok = drive.Spot.rt_is_off(spot_with([True] * 5 + [False] * 12), 100)
            self.assertEqual(ok["rayTracedLastFrames"], [False] * 10)
            with self.assertRaisesRegex(RuntimeError, "was not off"):   # Remix ignored the switch
                drive.Spot.rt_is_off(spot_with([True] * 5 + [False] * 11 + [True]), 100)
            with self.assertRaisesRegex(RuntimeError, "last 6 frames"):   # too few frames since the switch
                drive.Spot.rt_is_off(spot_with([False] * 20), 113)


if __name__ == "__main__":
    unittest.main()
