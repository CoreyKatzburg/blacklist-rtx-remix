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

    def _frames(self, dt: float) -> None:
        while self.running:
            time.sleep(dt)
            with self.lock:
                dyaw = self.queued[0] / self.cx
                self.yaw, self.turned = self.yaw + dyaw, self.turned + dyaw
                self.pitch = max(-89.0, min(89.0, self.pitch + self.queued[1] / self.cy))
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


if __name__ == "__main__":
    unittest.main()
