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

    def mouse(self, dx: int, dy: int) -> None:
        with self.lock:
            self.pending[0] += dx; self.pending[1] += dy

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


if __name__ == "__main__":
    unittest.main()
