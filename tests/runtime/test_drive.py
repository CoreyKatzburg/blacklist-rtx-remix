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

    def test_calibration_drops_one_move_the_game_ate_after_the_warm_up(self):
        # No-Remix Custodial, Nov 16: counts per degree 4.7, 47.8, 32.1, 39.3, 41.1 raised "inconsistent" and the
        # spot was lost. Fake: the game drops 90% of the second x move (the first one counted).
        s = self.run_with(60, 7.0, -9.0, 14.0, -14.0)
        mouse, n = self.game.mouse, [0]

        def eat_second_x(dx, dy):
            n[0] += bool(dx)
            mouse(int(dx * 0.1) if n[0] == 2 else dx, dy)
        with mock.patch.object(drive, "mouse_move_rel", eat_second_x):
            s._calibrate_mouse()
        self.assertAlmostEqual(s.counts_per_deg_x, 7.0, delta=0.3)
        self.assertEqual(len(s.calibration["droppedX"]), 1)
        self.assertGreater(s.calibration["droppedX"][0], 7.0 * 5)

    def test_calibration_still_fails_when_the_moves_disagree(self):
        s = self.run_with(60, 7.0, -9.0, 14.0, -14.0)
        mouse, n = self.game.mouse, [0]

        def wild_x(dx, dy):   # every counted x move lands at a different rate: nothing to agree on
            n[0] += bool(dx)
            mouse(int(dx * (1.0, 1.0, 0.2, 1.0, 0.45, 1.0)[n[0] - 1] * (2.2 if n[0] == 4 else 1.0)) if dx else 0, dy)
        with mock.patch.object(drive, "mouse_move_rel", wild_x), self.assertRaisesRegex(RuntimeError, "inconsistent"):
            s._calibrate_mouse()

    def test_snap_pose_corrects_with_settled_mouse_steps(self):
        # No-Remix custodial, Nov 16 05:21: written rotations did not hold, and stops at pitch +-30 ended 1 to 2.3
        # degrees off. Fake: 178 FPS, each input eased over 30 frames, calibration 12% low, start 2.3 degrees off.
        s = self.run_with(178, 117.0, -53.0, 103.0, -47.0)
        self.game.ease_frames = 30
        s.yaw, s.pitch = 2.3, -2.0
        steps = s.snap_pose()
        self.assertGreater(steps, 0)
        yaw, pitch, _ = self.game.rotation(0)
        self.assertLess(abs(gate.wrap(yaw - (s.base_yaw + 2.3))), 0.3)
        self.assertLess(abs(pitch + 2.0), 0.3)
        with mock.patch.object(drive, "mouse_move_rel", lambda dx, dy: None):   # fake: the game ignores the mouse
            s.yaw = 0.0
            self.assertEqual(s.snap_pose(), -1)

    def test_off_pose_stop_is_retried_and_marked(self):
        # Paladin reference, Nov 16: stops were captured 4 to 157 degrees from the asked view and nothing said so.
        def run(poses_per_try, no_remix=True, traces=None):
            s = drive.Spot.__new__(drive.Spot)
            s.yaw, s.pitch, s.no_remix, s.windows, s.spot = 90.0, -30.0, no_remix, [], {}
            s.counters = lambda: {"telemetry": 0, "proxy": 0}
            seq = iter([p for pair in poses_per_try for p in pair])
            s.rel_pose = lambda: next(seq)
            during = iter(traces or [])
            s.pose_trace = lambda body: (body(), next(during, ([on], 0)))[1]   # (the camera during each capture, failed reads)
            saved = []
            s.save_partial = lambda: saved.append(len(s.windows))
            moves = []
            s.move = lambda name, yaw, pitch, seconds: moves.append((name, yaw, pitch))
            s.snap_pose = lambda: 0
            s.hold = lambda seconds, until=None: None
            s.steady = lambda: 0.0
            s.capture = lambda rel, frames: None
            s._try_raster = lambda name, how: {}
            s.dump = lambda tag: None
            s.sam_moved = lambda name: 0.0
            s.stop("y90_p-30")
            self.assertEqual(saved, list(range(1, len(s.windows) + 1)))   # spot.json rewritten after every window
            return s, moves
        on, off = [90.2, -30.1], [-91.0, -30.0]   # -91 is 179 degrees from 90 once wrapped, not 181
        s, moves = run([[off, on], [on, [91.6, -30.0]], [on, on]])
        kept = [w for w in s.windows if w["kind"] == "still"]
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["name"], "y90_p-30")
        self.assertEqual(kept[0]["poseTries"], 3)
        self.assertEqual([t["offPoseDeg"] for t in kept[0]["offPoseTries"]], [179.0, 1.6])
        self.assertNotIn("offPose", kept[0])
        self.assertEqual([w["name"] for w in s.windows if w["kind"] == "off_pose"],
                         ["y90_p-30_settle_offpose1", "y90_p-30_offpose1", "y90_p-30_settle_offpose2", "y90_p-30_offpose2"])
        self.assertEqual([m[1:] for m in moves], [(90.0, -30.0), (90.0, -30.0)])   # steered back to the asked view
        s, _ = run([[on, [90.0, -31.2]]] * 3)   # off on every try: kept, but marked
        kept = [w for w in s.windows if w["kind"] == "still"]
        self.assertTrue(kept[0]["offPose"])
        self.assertEqual(kept[0]["offPoseDeg"], 1.2)
        s, moves = run([[on, [90.9, -29.1]]], no_remix=False)   # 0.9 degrees: on pose, one try
        self.assertEqual((s.windows[-1]["poseTries"], moves), (1, []))
        # on pose at the start and end, but the camera swung away during the capture: still off-pose
        s, moves = run([[on, on], [on, on]], traces=[([on, [96.0, -30.0], on], 0), ([on, on], 0)])
        self.assertEqual(s.windows[-1]["poseTries"], 2)
        self.assertEqual(s.windows[-1]["offPoseTries"][0]["offPoseDeg"], 6.0)
        # every read during the capture failed: on pose at the start and end, but nothing shows it held, so off-pose
        s, moves = run([[on, on]] * 3, traces=[([], 12)] * 3)
        kept = s.windows[-1]
        self.assertEqual((kept["poseTries"], kept["offPose"], kept["poseTraceEmpty"], kept["poseTraceFailedReads"]),
                         (3, True, True, 12))

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

    def test_waits_for_the_player_to_settle_after_a_load(self):
        # con_mall reference, Nov 16: drive.py read the player mid-load and failed on "read 4 bytes at 0x45".
        cp = {"map": "consumption", "location": [0.0, 0.0, 0.0]}
        reads = iter([drive.blmem.MemoryError_("read 4 bytes at 0x00000045 failed"),   # still loading
                      (1, 2, "menu", (0.0, 0.0, 0.0)),            # the menu's world: wrong map
                      (3, 4, "consumption", (9000.0, 0.0, 0.0)),  # right map, Sam not placed yet
                      (5, 6, "consumption", (10.0, 0.0, 0.0)),    # a player the game replaces next second
                      (7, 8, "consumption", (12.0, 0.0, 0.0)), (7, 8, "consumption", (12.0, 0.0, 0.0)),
                      (7, 8, "consumption", (12.0, 0.0, 0.0))])
        now = {}

        def player():
            r = next(reads)
            if isinstance(r, Exception):
                raise r
            now.update(map=r[2], loc=r[3])
            return r[0], r[1]
        s = drive.Spot.__new__(drive.Spot)
        s.game = mock.Mock(player=player, map_name=lambda c: now["map"], location=lambda p: now["loc"])
        with mock.patch.object(drive.time, "sleep"):
            self.assertEqual(s.wait_player(cp), (7, 8))
            reads = iter([(1, 2, "menu", (0.0, 0.0, 0.0))] * 200)
            with self.assertRaisesRegex(RuntimeError, "did not settle.*'menu'"):
                s.wait_player(cp, timeout=0.0)

    def test_a_replaced_controller_is_found_again(self):
        # con_green_hall reference, Nov 16: "property Rotation not found on IntProperty" after the first window.
        s = drive.Spot.__new__(drive.Spot)
        s.controller, s.pawn, s.windows, s.relinks, s._link_lock = 1, 2, [{}], [], threading.Lock()

        def rotation(controller):
            if controller == 1:
                raise drive.blmem.MemoryError_("property Rotation not found on IntProperty")
            return 30.0, -5.0, 0
        s.game = mock.Mock(rotation=rotation, player=lambda: (3, 4))
        self.assertEqual(s._rot(), (30.0, -5.0))
        self.assertEqual((s.controller, s.pawn), (3, 4))
        self.assertEqual([(r["afterWindow"], r["old"], r["new"]) for r in s.relinks], [(1, ["0x1", "0x2"], ["0x3", "0x4"])])

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

    def test_a_cutscene_spot_keeps_the_telemetry_of_the_whole_launch(self):
        # Benghazi, run 20261115-214203: the cutscene ended at frame 4987 and telemetry.jsonl began at 5033.
        import json, os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "rtx_telemetry.jsonl")
            with open(src, "w") as fh:
                fh.writelines(json.dumps({"frame": f}) + "\n" for f in range(4778, 4988))   # the cutscene
            s = drive.Spot.__new__(drive.Spot)
            s.telemetry = drive.Tail([src], True)   # drive.py starts once the cutscene is over
            with open(src, "a") as fh:
                fh.writelines(json.dumps({"frame": f}) + "\n" for f in range(5033, 5040))
            s.proxy = s.pgate = s.log = s.lptrace = drive.Tail([os.path.join(tmp, "absent")], False)
            s.out, s.process = tmp, mock.Mock()
            frames = lambda name: [json.loads(l)["frame"] for l in open(os.path.join(tmp, name))]
            s.spot = {"cinematic": True}
            files = s.finish()
            self.assertEqual(frames("telemetry.jsonl")[0], 5033)
            self.assertEqual(files["telemetryLaunch"], "telemetry_launch.jsonl")
            self.assertEqual(frames("telemetry_launch.jsonl")[:1] + frames("telemetry_launch.jsonl")[-1:], [4778, 5039])
            s.spot = {}
            self.assertNotIn("telemetryLaunch", s.finish())

    def test_a_trace_held_without_read_sharing_is_skipped_and_reported(self):
        # Nov 16 06:32: the proxy held lightpub_trace.csv via _wfopen_s and drive.py died at its start.
        import ctypes, os, tempfile
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        with tempfile.TemporaryDirectory() as tmp:
            held = os.path.join(tmp, "lightpub_trace.csv")
            h = k32.CreateFileW(held, 0x40000000, 0, None, 2, 0x80, None)   # GENERIC_WRITE, no sharing, CREATE_ALWAYS
            self.assertNotEqual(h, wintypes.HANDLE(-1).value)
            try:
                s = drive.Spot.__new__(drive.Spot)
                s.lptrace = drive.Tail([held], False)
                self.assertIsNone(s.lptrace.path)
                self.assertEqual(s.lptrace.last_frame(), 0)
                s.telemetry = s.proxy = s.pgate = s.log = drive.Tail([os.path.join(tmp, "absent")], False)
                s.out, s.process, s.spot = tmp, mock.Mock(), {}
                files = s.finish()
                self.assertIsNone(files["lightpubTrace"])
                self.assertIn("without read sharing", files["lightpubTraceError"])
            finally:
                k32.CloseHandle(h)


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
        # Nov 16 04:58-06:22: every no-Remix grab was black and the raster was written as a picture
        with self.assertRaisesRegex(RuntimeError, "all black"):
            drive.average_grabs(bytes(len(raw)), w, h, 4)
        drive.average_grabs(bytes(len(raw) - 1) + b"\x01", w, h, 4)   # one grey level in one pixel is a picture

    def test_a_black_still_capture_ends_the_spot(self):
        import os, tempfile
        s = drive.Spot.__new__(drive.Spot)
        s.hold = lambda seconds, until=None: None
        with tempfile.TemporaryDirectory() as tmp:
            s.out = tmp

            def fake_capture(content):
                def start(path, frames):
                    with open(path, "wb") as fh:
                        fh.write(content)
                    return mock.Mock(poll=lambda: 0)
                return start
            with mock.patch.object(drive, "start_capture", fake_capture(bytes(57600 * 20))):
                with self.assertRaisesRegex(RuntimeError, "all black"):
                    s.capture("black.gray", 20)
            with mock.patch.object(drive, "start_capture", fake_capture(bytes(57600 * 20 - 1) + b"\x07")):
                s.capture("dark.gray", 20)   # a nearly black room is still a picture

    def test_bench_runs_the_light_switch_after_the_lit_room(self):
        # Worker B's fog fade sweep: each variant launch needs the lit bench window first, then the fade.
        s = drive.Spot.__new__(drive.Spot)
        order = []
        s.move = lambda *a: None
        s.hold = lambda *a, **k: None
        s.bench_window = lambda name, *a: order.append(name)
        s.window = lambda name, kind, body, **k: order.append(name)
        s.light_switch = lambda flip: order.append(("light_switch", flip["dumpsAt"]))
        s.bench({"settle": 0, "frames": 1, "lightSwitch": {"key": "SPACE", "seconds": 20, "dumpsAt": [1, 18]}})
        self.assertEqual(order, ["bench_y0_p0", "y0_p0", ("light_switch", [1, 18])])
        order.clear()
        s.bench({"settle": 0, "frames": 1})
        self.assertNotIn("light_switch", [o[0] for o in order if isinstance(o, tuple)])

    def test_bench_extra_stops_debug_views_targets_then_capture(self):
        # Worker E2's rtx.captureAllInstances run: every variant measures the same windows in the same order, and
        # the USD capture (only in the "on" launch) comes last, after the custodial target, so it cannot disturb
        # what the other variants are compared on. A debug view is always set back to 0, even when its grab fails.
        s = drive.Spot.__new__(drive.Spot)
        order, ini = [], []
        s.spot = {"name": "con_boiler", "teleport": {"location": [1, 2, 3], "yaw": -154}}
        s.ini = "ini"
        s.move = lambda *a: None
        s.hold = lambda *a, **k: None
        s.bench_window = lambda name, *a: order.append(name)
        s.stop = lambda name, *a, **k: order.append(name)
        s.teleport = lambda tp: order.append(("teleport", tp["yaw"])) or {}
        s._try_raster = lambda tag, label: {}
        s.usd_capture = lambda c: order.append(("usd", c["teleport"]["yaw"])) or {"name": c["name"]}

        def window(name, kind, body, **k):
            if name.endswith("_screen") and k.get("debugViewIdx") == 23 and fail_grab:
                raise RuntimeError("grab failed")
            order.append(name)
        s.window = window
        cfg = {"settle": 0, "frames": 1, "stops": [[0, -30]], "debugViews": {"albedo": 23},
               "targets": [{"name": "con_custodial", "teleport": {"location": [4, 5, 6], "yaw": -67}}]}
        with mock.patch.object(drive, "set_ini", lambda path, sec, key, val: ini.append((key, val))):
            fail_grab = False
            self.assertEqual(s.bench(cfg), {})
            self.assertEqual(order, ["bench_y0_p0", "y0_p0", "y0_p-30", "y0_p0_albedo_screen", ("teleport", -67),
                                     "con_custodial_teleport", "con_custodial_bench_y0_p0", "con_custodial_y0_p0"])
            self.assertEqual(ini, [("rtx.debugView.debugViewIdx", "23"), ("rtx.debugView.debugViewIdx", "0")])
            order.clear()
            got = s.bench({**cfg, "usdCapture": True})
            self.assertEqual(order[-1], ("usd", -154))   # back at the boiler, after the custodial target
            self.assertEqual(got, {"captures": [{"name": "con_boiler"}]})
            ini.clear()
            fail_grab = True
            with self.assertRaises(RuntimeError):
                s.bench(cfg)
            self.assertEqual(ini[-1], ("rtx.debugView.debugViewIdx", "0"))

    def test_quit_keys_and_exit_wait(self):
        # Worker C's quit test: Alt+F4 must be a chord (Alt held while F4 goes down and up), and the arrows must be
        # the arrow block (extended), or the menu sees numpad keys.
        sent = []
        def record(n, ptr, size):
            ki = ctypes.cast(ptr, ctypes.POINTER(drive._INPUT)).contents.u.ki
            sent.append((ki.scan, ki.flags))
        import ctypes
        with mock.patch.object(drive.user32, "SendInput", record), mock.patch.object(drive.time, "sleep"):
            drive.tap("ALT+F4")
            drive.tap("UP")
        alt, f4 = drive.SCAN["ALT"], drive.SCAN["F4"]
        self.assertEqual([s for s, _ in sent[:4]], [alt, f4, f4, alt])
        self.assertEqual([f & 2 for _, f in sent[:4]], [0, 0, 2, 2])   # down, down, up, up
        self.assertTrue(all(f & 1 for _, f in sent[4:]))
        import subprocess, sys
        p = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(1.5); raise SystemExit(3)"])
        try:
            self.assertIsNone(drive.wait_exit(p.pid, 0.2))   # still running
            self.assertEqual(drive.wait_exit(p.pid, 10), 3)
        finally:
            p.wait()
        # The game ignored injected Alt+F4 (20261116-080214), so a quit step can post SC_CLOSE, the message Alt+F4
        # becomes, to the window instead of typing it. A real window must close on it.
        with mock.patch.object(drive, "tap") as typed:
            drive.quit_step(0, "ENTER")
            self.assertEqual(typed.call_args.args, ("ENTER",))
        n = subprocess.Popen(["notepad.exe"])
        try:
            hwnd, deadline = 0, time.monotonic() + 10
            while not hwnd and time.monotonic() < deadline:
                time.sleep(0.2)
                hwnd = drive.game_window(n.pid)
            # Worker C's mid-play minimize: the window really is minimized during the wait and back afterwards.
            m = drive.minimize_restore(hwnd, 0.5)
            self.assertEqual((m["minimized"], m["restored"]), (True, True))
            with mock.patch.object(drive, "tap", side_effect=AssertionError("typed instead of posted")):
                drive.quit_step(hwnd, "SC_CLOSE")
            self.assertEqual(drive.wait_exit(n.pid, 10), 0)
        finally:
            n.kill(); n.wait()

    def test_minimize_restore_does_not_wait_for_a_frozen_window(self):
        # 20261116-093307 01_fix_off: the device was lost while minimized, the game window stopped taking messages,
        # and the restore held the driver for 6 minutes. A window whose thread sleeps without reading messages
        # stands in for the frozen game. The restore came 2 s after the loss, before Windows marks a window frozen
        # (5 s; after that it shows a stand-in window and even a plain ShowWindow returns), so the first call is made
        # at once: it must not wait. The second, once Windows has marked it, must say the window was frozen.
        import ctypes, subprocess, sys
        child = ("import ctypes, sys, time\n"
                 "u = ctypes.WinDLL('user32')\n"
                 "u.CreateWindowExW.restype = ctypes.c_void_p\n"
                 "h = u.CreateWindowExW(0, 'STATIC', 'frozen', 0x10CF0000, 100, 100, 300, 200, None, None, None, None)\n"
                 "m = ctypes.create_string_buffer(64)\n"
                 "while u.PeekMessageW(m, None, 0, 0, 1): u.DispatchMessageW(m)\n"
                 "print(h, flush=True)\n"
                 "t = time.monotonic()\n"   # read messages for 1 s, so the minimize happens, then freeze
                 "while time.monotonic() - t < 1.0:\n"
                 "    while u.PeekMessageW(m, None, 0, 0, 1): u.DispatchMessageW(m)\n"
                 "    time.sleep(0.01)\n"
                 "time.sleep(60)\n")
        p = subprocess.Popen([sys.executable, "-I", "-c", child], stdout=subprocess.PIPE, text=True)
        try:
            hwnd = int(p.stdout.readline())

            def restore(seconds):
                result = {}
                t = threading.Thread(target=lambda: result.update(drive.minimize_restore(hwnd, seconds)), daemon=True)
                t.start(); t.join(seconds + 3)
                self.assertFalse(t.is_alive(), "minimize_restore waited for the frozen window")
                return result
            first = restore(1.5)   # minimized while it still reads messages, restored after it froze
            self.assertEqual((first["minimized"], first["hungAtRestore"]), (True, False))
            # Windows calls a window frozen after 5 s without reading messages; a posted message makes it notice.
            drive.user32.PostMessageW(ctypes.c_void_p(hwnd), 0, 0, 0)
            deadline = time.monotonic() + 15
            while not drive.user32.IsHungAppWindow(ctypes.c_void_p(hwnd)) and time.monotonic() < deadline:
                time.sleep(0.5)
            self.assertTrue(drive.user32.IsHungAppWindow(ctypes.c_void_p(hwnd)), "the stand-in window never froze")
            self.assertTrue(restore(0.2)["hungAtRestore"])
        finally:
            p.kill(); p.wait()

    def test_usd_capture_keeps_only_its_own_files_and_puts_the_settings_back(self):
        # Worker E's capture: the draw dump and the Remix capture asked together, copied out, the new capture files
        # removed from the game folder (older ones stay), replacement assets back on, and a capture that Remix
        # refused is an error, not a quiet success.
        import os, shutil, tempfile, types

        def run(writes_usd):
            tmp = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, tmp, True)
            system, out = os.path.join(tmp, "SYSTEM"), os.path.join(tmp, "out")
            caps, dumps = os.path.join(system, "rtx-remix", "captures"), os.path.join(system, "framedump")
            logs = os.path.join(system, "rtx-remix", "logs")
            for d in (os.path.join(caps, "textures"), dumps, logs, out):
                os.makedirs(d)
            for p in (os.path.join(caps, "textures", "old.dds"), os.path.join(dumps, "f9_draws.tsv")):
                open(p, "w").close()   # f9: an earlier launch reached frame 9 too, so the name is taken
                os.utime(p, ns=(10**18, 10**18))
            with open(os.path.join(logs, "remix-dxvk.log"), "w") as f:
                f.write("[GameCapturer][aaaa] End USD export\n")   # an earlier capture's line must not count
            ini = os.path.join(system, "d3d9_proxy.ini")
            with open(ini, "w", newline="") as f:
                f.write("[Proxy]\r\nCaptureDraws=0\r\n[RemixConfig]\r\nrtx.x = 1\r\n")

            def press(key):
                self.assertEqual(key, "CTRL+SHIFT+Q")
                with open(ini) as f:
                    text = f.read()
                self.assertIn("rtx.enableReplacementAssets = False", text)   # else Remix refuses the capture
                self.assertIn("CaptureDraws=1", text)                       # asked in the same second
                with open(os.path.join(dumps, "f9_draws.tsv"), "w") as f:
                    f.write("this launch")
                with open(os.path.join(logs, "remix-dxvk.log"), "a") as f:
                    if writes_usd:
                        os.makedirs(os.path.join(caps, "meshes"))
                        open(os.path.join(caps, "capture_1.usda"), "w").close()
                        open(os.path.join(caps, "meshes", "m.usd"), "w").close()
                        f.write("[GameCapturer][bbbb] New capture\n[GameCapturer][bbbb] End USD export\n")
                    else:
                        f.write("[GameCapturer] Cannot begin capture when replacement assets are enabled/loaded.\n")
            s = drive.Spot.__new__(drive.Spot)
            s.a = types.SimpleNamespace(system=system, pid=1)
            s.ini, s.out = ini, out
            s.proxy = types.SimpleNamespace(last_frame=lambda: 9)
            s.teleport, s.move, s.stop = (lambda tp: {"sideways": 0}), (lambda *a: None), (lambda name: None)
            s.rt_is_on, s.hold = (lambda: {"rayTracedLastFrames": [True]}), (lambda seconds, until=None: None)
            with mock.patch.object(drive, "tap", press), mock.patch.object(drive, "game_window", lambda pid: 5), \
                    mock.patch.object(drive, "front", lambda h: True):
                rec = s.usd_capture({"name": "con_boiler", "teleport": {}})
            with open(ini) as f:
                text = f.read()
            return rec, caps, os.path.join(out, "con_boiler", "capture"), text

        rec, caps, dest, ini_text = run(True)
        self.assertNotIn("error", rec)
        self.assertEqual(rec["drawDump"], ["f9_draws.tsv"])
        self.assertEqual(rec["usd"], ["capture_1.usda", os.path.join("meshes", "m.usd")])
        self.assertTrue(os.path.exists(os.path.join(dest, "meshes", "m.usd")))
        self.assertEqual(set(drive.files_under(caps)), {os.path.join("textures", "old.dds")})
        self.assertIn("rtx.enableReplacementAssets = True", ini_text)
        self.assertIn("CaptureDraws=0", ini_text)
        self.assertNotIn("captureShowMenuOnHotkey", ini_text)
        rec, *_ = run(False)
        self.assertEqual(rec["error"], "no new USD capture file")
        self.assertIn("Cannot begin capture", rec["remixLog"][0])

    def test_light_dump_takes_a_rewritten_file_whose_name_an_earlier_launch_used(self):
        # 082350 con_bright_hall_samgear: the sam_gear step's f6059_lights.tsv was written, but a launch at 07:25 had
        # made a file with that name, and a check by name missed it (no light dump for the step).
        import os, shutil, tempfile, types
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        dumps = os.path.join(tmp, "framedump")
        os.makedirs(dumps)
        old = os.path.join(dumps, "f6059_lights.tsv")
        open(old, "w").close()
        os.utime(old, ns=(10**18, 10**18))
        ini = os.path.join(tmp, "d3d9_proxy.ini")
        with open(ini, "w", newline="") as f:
            f.write("[Proxy]\r\nCaptureDraws=0\r\n")
        s = drive.Spot.__new__(drive.Spot)
        s.a, s.ini = types.SimpleNamespace(system=tmp), ini

        def hold(seconds, until=None):
            with open(old, "w") as f:
                f.write("this launch")
        s.hold = hold
        with mock.patch.object(drive.time, "sleep"):
            got = s.light_dump(os.path.join(tmp, "sam_gear_lights.tsv"))
        self.assertEqual(got, "sam_gear_lights.tsv")
        with open(os.path.join(tmp, "sam_gear_lights.tsv")) as f:
            self.assertEqual(f.read(), "this launch")

    def test_screen_probe_tells_a_lost_game_picture_from_a_dead_display(self):
        # Nov 16: the black no-Remix runs could be the display (off after 30 min idle) or the game picture lost after
        # Present. The marker window's colour in the same grab separates them.
        import numpy as np
        w, h = 64, 48
        info = {"width": w, "height": h, "marker": [40, 24, 20, 20], "colour": [200, 40, 160]}

        def grab(game_value, marker_on):
            img = np.full((h, w, 3), game_value, np.uint8)
            if marker_on:
                img[24:44, 40:60] = info["colour"]
            return img.tobytes()
        client = (0, 0, w, h)
        self.assertEqual(drive.probe_verdict(grab(90, True), info, client)["verdict"], "ok")
        self.assertTrue(drive.probe_verdict(grab(0, True), info, client)["verdict"].startswith("game picture lost"))
        self.assertTrue(drive.probe_verdict(grab(0, False), info, client)["verdict"].startswith("display or session"))
        self.assertEqual(drive.probe_verdict(b"", info, client)["verdict"], "no grab")
        # the marker's own pixels must not count as game picture
        self.assertEqual(drive.probe_verdict(grab(0, True), info, client)["gameLumaMax"], 0.0)

    def test_old_dumps_from_an_earlier_spot_are_not_copied(self):
        # Run 063147: Remix wrote no dumps at con_bright_hall (not ray tracing), and con_custodial's files of the
        # same names were copied and scored as con_bright_hall's picture.
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            game, out = os.path.join(tmp, "gate"), os.path.join(tmp, "out")
            os.makedirs(game), os.makedirs(os.path.join(out, "gate"))
            for n in ("y0_p0_final.pfm", "y0_p0_depth.pfm", "y0_p0_gamedepth.pfm"):
                with open(os.path.join(game, n), "wb") as fh:
                    fh.write(b"custodial")
                os.utime(os.path.join(game, n), (1, 1))
            s = drive.Spot.__new__(drive.Spot)
            s.dump_dirs, s.out, s.ini = [game], out, os.path.join(tmp, "d3d9_proxy.ini")

            def proxy_answers(seconds, until=None):   # the proxy writes its game depth; Remix writes nothing
                with open(os.path.join(game, "y0_p0_gamedepth.pfm"), "wb") as fh:
                    fh.write(b"bright_hall")
            s.hold = proxy_answers
            with mock.patch.object(drive, "set_ini", lambda *a: None), mock.patch.object(drive.time, "sleep"):
                s.dump("y0_p0")
            self.assertEqual(os.listdir(os.path.join(out, "gate")), ["y0_p0_gamedepth.pfm"])
            with open(os.path.join(out, "gate", "y0_p0_gamedepth.pfm"), "rb") as fh:
                self.assertEqual(fh.read(), b"bright_hall")

    def test_a_dead_or_black_screen_read_is_an_error_not_an_empty_list(self):
        # No-Remix reference, Nov 16: every shot was black, so samples_ms was [] with noise 0 and no error.
        for name, chunk in (("black", lambda n: bytes(n)), ("dead", lambda n: b"")):
            class Out:
                def read(self, n):
                    time.sleep(0.002)
                    return chunk(n)
            proc = mock.Mock(stdout=Out())
            s = drive.Spot.__new__(drive.Spot)
            s.counts_per_deg_x, s.proxy = 10.0, mock.Mock(path=None)
            s.hold = lambda seconds, until=None: time.sleep(min(seconds, 0.05))
            with mock.patch.object(drive.subprocess, "Popen", lambda *a, **k: proc), \
                    mock.patch.object(drive, "mouse_move_rel", lambda dx, dy: None):
                r = s.mouse_lag()
            self.assertIsNone(r["samples_ms"], name)
            self.assertIn("no turn showed on screen", r.get("error", ""), name)
            self.assertIn("all black" if name == "black" else "under 10 shots", r["error"], name)
            self.assertEqual(len(r["trials"]), 3, name)   # gives up after three dead turns, not forty
            self.assertIsNone(gate.spot_mouse_lag({"mouse_lag": r}))

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

    def test_live_ini_step_needs_its_own_reload_line_then_stray_lines(self):
        import types
        want = {"reloaded": "settings reloaded (scale 1, stand-in skip unplaced 1, skip moving 0)",
                "after": "lightpub: stray", "count": 2, "timeout": 30}
        old = "lightpub: settings reloaded (scale 1, stand-in skip unplaced 1, skip moving 0)\nlightpub: stray a\n"

        def run(chunks):
            # the log grows by one chunk per check; text before `mark` is from earlier steps and must not count
            text = [old]
            feed = iter(chunks)
            def new_text():
                return text[0]
            s = drive.Spot.__new__(drive.Spot)
            s.log = types.SimpleNamespace(new_text=new_text)
            def hold(seconds, until=None):
                for _ in range(len(chunks) + 1):
                    if until():
                        return
                    text[0] += next(feed, "")
            s.hold = hold
            clock = iter(range(0, 10_000, 1))
            with mock.patch.object(drive.time, "monotonic", lambda: float(next(clock))):
                return s.await_log(want, len(old))

        # the setting did not load: the reload line shows the old value, so the step must not be measured
        with self.assertRaisesRegex(RuntimeError, "no proxy.log line"):
            run(["lightpub: stray b\n", "lightpub: settings reloaded (scale 1, stand-in skip unplaced 0, skip moving 0)\n",
                 "lightpub: stray c\n", "lightpub: stray d\n"])
        # a stray line before the reload belongs to the previous settings
        got = run(["lightpub: stray b\n", "lightpub: settings reloaded (scale 1, stand-in skip unplaced 1, skip moving 0)\n",
                   "lightpub: stray c\n", "lightpub: stray d\n"])
        self.assertEqual(got["afterLines"], ["lightpub: stray c", "lightpub: stray d"])
        self.assertNotIn("short", got)
        got = run(["lightpub: settings reloaded (scale 1, stand-in skip unplaced 1, skip moving 0)\n", "lightpub: stray c\n"])
        self.assertIn("1 of 2", got["short"])

    def test_ini_key_lands_in_its_section_not_after_a_comment_naming_it(self):
        # Live failure in 20261116-080214 con_bright_hall_samgear: the staged ini's comment "; [Lights] with ..."
        # was taken for the header, so StandInSkipSamGear went into [Proxy] and the proxy kept its default 0.
        import configparser, os, tempfile
        staged = ("[Proxy]\r\nEnableDebuggingTelemetry=1\r\n; Lights: defaults to 1. To bring them back, add\r\n"
                  "; [Lights] with StandInSkipUnplaced=0.\r\n")

        def read(text):   # sections as GetPrivateProfileInt sees them
            p = configparser.ConfigParser(comment_prefixes=(";",), strict=False, interpolation=None)
            p.optionxform = str
            p.read_string(text)
            return {s: dict(p[s]) for s in p.sections()}

        with tempfile.TemporaryDirectory() as tmp:
            ini = os.path.join(tmp, "d3d9_proxy.ini")
            with open(ini, "w", encoding="latin-1", newline="") as f:
                f.write(staged)
            for value in ("0", "1"):
                drive.set_ini(ini, "Lights", "StandInSkipSamGear", value)
            drive.set_ini(ini, "Proxy", "GateDump", "a")
            drive.set_ini(ini, "Lights", "StandInSkipMoving", "1")
            drive.set_ini(ini, "Proxy", "EnableDebuggingTelemetry", "0")
            with open(ini, encoding="latin-1", newline="") as f:
                text = f.read()
        got = read(text)
        self.assertEqual(got["Lights"], {"StandInSkipSamGear": "1", "StandInSkipMoving": "1"})
        self.assertEqual(got["Proxy"], {"EnableDebuggingTelemetry": "0", "GateDump": "a"})
        self.assertIn("; [Lights] with StandInSkipUnplaced=0.\r\n", text)
        self.assertNotIn("\n", text.replace("\r\n", ""))   # CRLF kept everywhere

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
            # Run 20261116-063147 con_bright_hall: the whole launch drew without ray tracing and was scored as Remix
            on = lambda flags: drive.Spot.rt_is_on(types.SimpleNamespace(
                hold=lambda seconds, until=None: None, **vars(spot_with(flags))))
            self.assertEqual(on([False] * 5 + [True] * 10)["rayTracedLastFrames"], [True] * 10)
            with self.assertRaisesRegex(RuntimeError, "not ray tracing"):
                on([False] * 40)
            with self.assertRaisesRegex(RuntimeError, "not ray tracing"):   # one raster frame at the end
                on([True] * 12 + [False])


if __name__ == "__main__":
    unittest.main()
