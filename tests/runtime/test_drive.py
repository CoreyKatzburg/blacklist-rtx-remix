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

    def test_reference_stills_run_only_without_remix_at_their_own_length(self):
        # Worker B, Nov 16: a 210-frame original-game still at the con_bright_hall_keys view (block pulse needs more
        # than one 52-56 frame cycle; the reference stills were 20 frames).
        def run(no_remix, extra=(), blocked=()):
            s = drive.Spot.__new__(drive.Spot)
            s.yaw, s.pitch, s.no_remix, s.windows = 0.0, 0.0, no_remix, []
            s.spot = {"teleport": {"yaw": -90}, "referenceStills": [
                {"name": "keys_y90_p5", "yaw": 90, "pitch": 5, "frames": 210}, *extra,
                {"name": "plain", "yaw": -45, "pitch": 0}]}

            def teleport(tp):
                if tp in blocked:
                    raise RuntimeError("teleport did not hold")
                moves.append(("at", tp["yaw"])); return {}
            s.teleport = teleport
            s.counters = lambda: {"telemetry": 0, "proxy": 0}
            s.save_partial = lambda: None
            moves, caps = [], []

            def move(name, yaw, pitch, seconds):
                moves.append((name, yaw, pitch)); s.yaw, s.pitch = yaw, pitch
            s.move = move
            s.rel_pose = lambda: [s.yaw, s.pitch]
            s.pose_trace = lambda body: (body(), ([[s.yaw, s.pitch]], 0))[1]
            s.snap_pose = lambda: 0
            s.hold = lambda seconds, until=None: None
            s.steady = lambda: 0.0
            s.capture = lambda rel, frames: caps.append((rel, frames))
            s._try_raster = lambda name, how: {}
            s.dump = lambda tag: None
            s.sam_moved = lambda name: 0.0
            s.reference_stills()
            return s, moves, caps
        s, moves, caps = run(True)
        self.assertEqual(moves, [("to_keys_y90_p5", 90.0, 5.0), ("to_plain", -45.0, 0.0)])
        self.assertEqual(caps, [("frames/keys_y90_p5.gray", 210), ("frames/plain.gray", 20)])
        self.assertEqual([w["name"] for w in s.windows if w["kind"] == "still"], ["keys_y90_p5", "plain"])
        self.assertEqual(run(False)[1:], ([], []))   # a Remix run of the same spot is unchanged
        # Worker B's lamp pose A: a still with its own teleport (fallback when blocked), then back to the spawn for
        # the next still; a pose whose teleports all fail is skipped and the next still is still at the spawn.
        lamps = {"name": "lampsA", "teleport": {"yaw": 170}, "fallback": {"yaw": 175}, "yaw": 0, "pitch": 30,
                 "frames": 60}
        for blocked, at in (((), [("at", 170)]), (({"yaw": 170},), [("at", 175)]), (({"yaw": 170}, {"yaw": 175}), [])):
            s, moves, caps = run(True, [lamps], blocked)
            self.assertEqual(moves, [("to_keys_y90_p5", 90.0, 5.0)] + at + ([("to_lampsA", 0.0, 30.0)] if at else [])
                             + [("at", -90), ("to_plain", -45.0, 0.0)], blocked)
            self.assertEqual([c[1] for c in caps], [210, 60, 20] if at else [210, 20])
            tp = [w for w in s.windows if w["name"] == "lampsA_teleport"][0]
            self.assertEqual(tp.get("fallback"), {0: False, 1: True}.get(len(blocked)))

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

    def test_first_teleport_teleports_again_after_a_checkpoint_reset(self):
        # No-Remix reference, Nov 16 17:57: con_bright_hall's teleport "landed" exactly on the checkpoint position, and
        # at con_green_hall and con_mall the player objects vanished a few seconds after the teleport.
        cp, want = {"map": "m", "location": [0.0, 0.0, 0.0]}, [5000.0, 0.0, 0.0]

        def spot(script):
            """script: per set_location call, 'hold' (Sam stays), 'land_reset' (the game puts him back on the
            checkpoint with new objects before the 8 s check) or 'watch_reset' (lands, then reset on the first watch
            read), 'elsewhere' (lands 3000 units off, no reset)."""
            st = {"loc": (0.0, 0.0, 0.0), "pair": (1, 2), "calls": 0, "pending": None}
            s = drive.Spot.__new__(drive.Spot)
            s.cp, s.controller, s.pawn, s.hold = cp, 1, 2, lambda t: None

            def set_location(pawn, loc):
                kind = script[st["calls"]]
                st["calls"] += 1
                if kind == "land_reset":
                    st["pair"] = (st["pair"][0] + 2, st["pair"][1] + 2)
                elif kind == "elsewhere":
                    st["loc"] = (loc[0] - 3000.0, loc[1], loc[2])
                else:
                    st["loc"] = tuple(loc)
                    st["pending"] = "reset" if kind == "watch_reset" else None

            def player():
                if st["pending"] == "reset":
                    st["pending"], st["loc"] = None, (0.0, 0.0, 0.0)
                    st["pair"] = (st["pair"][0] + 2, st["pair"][1] + 2)
                return st["pair"]
            s.game = mock.Mock(set_location=set_location, location=lambda p: st["loc"], player=player)
            s.wait_player = lambda c: st["pair"]
            return s, st

        with mock.patch.object(drive.time, "sleep"):
            for script, resets in ((["land_reset", "hold"], 1), (["watch_reset", "hold"], 1),
                                   (["land_reset", "watch_reset", "hold"], 2), (["hold"], 0)):
                s, st = spot(script)
                info = s.first_teleport({"location": want, "yaw": 90}, watch=0.05)
                self.assertEqual((len(info["resets"]), st["calls"], info["landed"]), (resets, len(script), want), script)
                self.assertEqual((s.controller, s.pawn), st["pair"], script)
                self.assertEqual(s.home, want)
            s, _ = spot(["land_reset"] * 3)
            with self.assertRaisesRegex(RuntimeError, "after all 3 teleports"):
                s.first_teleport({"location": want, "yaw": 90}, watch=0.05)
            s, _ = spot(["elsewhere"])   # a landing that is not a reset still fails as before
            with self.assertRaisesRegex(RuntimeError, "teleport did not hold"):
                s.first_teleport({"location": want, "yaw": 90}, watch=0.05)

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

    def series_spot(self, clock, location, player=lambda: (1, 2), proxy_path=None):
        """A Spot for teleport_series on a fake clock: hold() and sleep() move the clock, move() records the base yaw
        it turned to, and the proxy frame number is the 5 ms frame the clock is at (frame 0 at 99.0 s)."""
        s = drive.Spot.__new__(drive.Spot)
        s.spot, s.cp = {"fallSeconds": 1.0, "spawnHold": 5.0}, {"map": "m", "location": [-6285.7, -2420.7, 1188.0],
                                                                "yaw": 80.0, "pitch": -5.0}
        s.controller, s.pawn, s.windows, s.yaw, s.pitch, s.moves, s.placed = 1, 2, [], 0.0, 0.0, [], []
        s.hold = lambda t: clock.__setitem__(0, clock[0] + t)
        s.move = lambda name, y, p, sec: s.moves.append((name, s.base_yaw, s.base_pitch))
        s.rel_pose = lambda: [0.0, 0.0]
        frame = lambda: int(round((clock[0] * 1000 - 99000) / 5))   # noqa: E731
        s.counters = lambda: {"telemetry": 0, "proxy": frame()}
        s.proxy = mock.Mock(path=proxy_path, last_frame=frame)

        def set_location(pawn, loc):
            s.placed.append([round(v, 1) for v in loc])
            clock[1] = (list(loc), clock[0])
        s.game = mock.Mock(set_location=set_location, location=location, player=player)
        s.wait_player = lambda cp: (7, 8)
        return s

    def test_teleport_series_records_the_fall_landing_and_both_cameras(self):
        # Worker G, Nov 16 (con_boiler fall test): the fall's frame times stop when Sam lands, and each camera is the
        # median over its own one-second window, not over the whole teleport.
        import math, os, tempfile
        clock = [95.0, None]

        def location(pawn):
            loc, at = clock[1]
            t = clock[0] - at
            if loc[2] > 0:   # the spawn
                return tuple(loc)
            return (loc[0], loc[1] + 5.2, loc[2] - 200.0 * t if t < 0.405 else -674.0)
        y = math.radians(-154.0)
        rows = []
        for k in range(2601):
            ms = 99000 + 5 * k
            if 107000 <= ms <= 108000:
                cam = "-12030:-2700:-600:1:0:0" if k != 1700 else "5:5:5:0:1:0"   # one stray frame
            elif 110000 <= ms <= 111000:
                cam = f"-12040:-2510:-620:{math.cos(y):.6f}:{math.sin(y):.6f}:0"
            else:
                cam = "0:0:0:0:1:0"
            dt = 5.0 if ms <= 100500 else 9.0
            rows.append(f"{k},{ms}.0,{dt},0.5,100,8,G:0:0:{cam}:0|G:9:1:1:1:1:1:0:0:0")
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "f.csv")
            with open(p, "w") as fh:
                fh.write("\n".join(rows) + "\n")
            s = self.series_spot(clock, location, proxy_path=p)
            with mock.patch.object(drive.time, "sleep", lambda t: clock.__setitem__(0, clock[0] + t)), \
                 mock.patch.object(drive.time, "perf_counter", lambda: clock[0]):
                out = s.teleport_series([{"group": "old1", "location": [-12165, -2524, -592], "count": 1, "yaw": -154}])
        r = out[0]
        self.assertEqual(s.placed, [[-6285.7, -2420.7, 1188.0], [-12165.0, -2524.0, -592.0]])   # spawn, then target
        self.assertEqual([m[1:] for m in s.moves], [(80.0, -5.0), (-154.0, 0.0)])   # spawn view, then the series yaw
        self.assertEqual((r["group"], r["held"], r["teleport"]),
                         ("old1", True, {"asked": [-12165.0, -2524.0, -592.0], "landed": [-12165.0, -2518.8, -674.0],
                                         "yaw": -154.0}))
        self.assertEqual(r["landedAfterS"], 0.45)
        self.assertEqual((len(r["fallFrameMs"]), set(r["fallFrameMs"])), (92, {5.0}))   # 100000..100455 ms
        self.assertEqual(r["at8s"]["camera"], [-12030.0, -2700.0, -600.0, 0.0, 0.0])
        self.assertEqual(r["atStop"]["camera"], [-12040.0, -2510.0, -620.0, -154.0, 0.0])
        self.assertEqual(r["at8s"]["samLanded"], [-12165.0, -2518.8, -674.0])

    def test_teleport_series_goes_on_after_a_teleport_the_game_undoes(self):
        clock = [95.0, None]
        calls = {"n": 0}

        def location(pawn):
            loc, _ = clock[1]
            if loc[2] < 0 and len([p for p in s.placed if p[2] < 0]) == 2 and calls["n"] < 1:
                calls["n"] += 1
                raise drive.blmem.MemoryError_("read 4 bytes at 0x45")   # the second teleport: a reload
            return tuple(loc)
        s = self.series_spot(clock, location, player=lambda: (s.controller, s.pawn))
        with mock.patch.object(drive.time, "sleep", lambda t: clock.__setitem__(0, clock[0] + t)), \
             mock.patch.object(drive.time, "perf_counter", lambda: clock[0]):
            out = s.teleport_series([{"group": "old1", "location": [-12165, -2524, -592], "count": 2},
                                     {"group": "floor", "location": [-12165, -2518.8, -674], "count": 1}])
        self.assertEqual([(r["group"], r["index"], r["held"]) for r in out],
                         [("old1", 1, True), ("old1", 2, False), ("floor", 3, True)])
        self.assertIn("read 4 bytes", out[1]["error"])
        self.assertEqual((s.controller, s.pawn), (7, 8))   # found the player again
        self.assertEqual(len(s.placed), 6)   # spawn and target for each of the three
        self.assertIsNone(out[0]["fallFrameMs"])   # no proxy frame log: no frame times, not an error

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

    def test_live_steps_set_the_display_state_and_wake_it_at_the_end(self):
        # Coordinator, Nov 16: idle GPU power follows the display state; the proof is live steps at one held pose,
        # awake / asleep / awake / asleep / awake, with no input sent while a step holds. Each step records what
        # Windows reported before and after its hold; a launch that fails while asleep still wakes the displays.
        for fail_at in (None, 2):
            s = drive.Spot.__new__(drive.Spot)
            s.ini, s.windows, calls = "x.ini", [], []
            s.move = lambda *a: None
            s.bench_window = lambda *a: None
            s.dump = lambda tag: None
            s.counters = lambda: {"telemetry": 0, "proxy": 0}
            s.yaw = s.pitch = 0.0

            def hold(seconds, until=None):
                calls.append(("hold", seconds))
                if fail_at and sum(c[0] == "hold" for c in calls) == fail_at + 1:
                    raise RuntimeError("game gone")
            s.hold = hold
            fake = {"now": "on"}

            def wake():
                calls.append("wake"); fake["now"] = "on"; return {"asked": "awake", "display": "on"}

            def sleep():
                calls.append("sleep"); fake["now"] = "off"; return {"asked": "asleep", "display": "off"}
            steps = [{"display": d} for d in ("awake", "asleep", "awake", "asleep", "awake")]
            with mock.patch.object(drive.display, "wake", wake), mock.patch.object(drive.display, "sleep", sleep), \
                    mock.patch.object(drive.display, "state", lambda: fake["now"]), \
                    mock.patch.object(drive.display, "idle_seconds", lambda: 1.0), \
                    mock.patch.object(drive, "set_ini", lambda *a: calls.append(("ini",) + a)):
                if fail_at:
                    with self.assertRaises(RuntimeError):
                        s.bench({"settle": 0, "frames": 1, "liveSteps": steps, "liveStepSeconds": 45})
                    self.assertEqual(calls[-1], "wake")   # woken after the failure in the second (asleep) step
                    continue
                s.bench({"settle": 0, "frames": 1, "liveSteps": steps, "liveStepSeconds": 45})
            live = [w for w in s.windows if w["name"].startswith("live_step")]
            self.assertEqual([(w["display"]["asked"], w["display"]["display"], w["display"]["after"]) for w in live],
                             [("awake", "on", "on"), ("asleep", "off", "off")] * 2 + [("awake", "on", "on")])
            self.assertEqual([w["liveRtx"] for w in live], [{}] * 5)   # "display" is not written to the ini
            self.assertNotIn("ini", [c[0] for c in calls if isinstance(c, tuple)])
            self.assertEqual([c for c in calls if c in ("wake", "sleep") or c == ("hold", 45.0)],
                             ["wake", ("hold", 45.0), "sleep", ("hold", 45.0)] * 2 + ["wake", ("hold", 45.0), "wake"])

    def monitor_launch(self, tmp, corey=(3, 33), fail_in=None, minimized_without_monitor=False, second_ignores_on=0):
        """Runs bench liveSteps both_on, second_off, both_off, both_on_again against fake monitors. Each hold advances
        25 telemetry frames per second held (25 for a poll). corey = (polls after the second monitor is back at which
        both detach, at which both come back); None = Corey never switches. Returns (spot, ddc writes, fake)."""
        import os
        s = drive.Spot.__new__(drive.Spot)
        s.ini, s.out, s.windows = "x.ini", tmp, []
        s.a = type("A", (), {"pid": 1234})()
        s.move = lambda *a: None
        s.bench_window = lambda *a: None
        s.dump = lambda tag: None
        s.yaw = s.pitch = 0.0
        fake = {"frame": 1000, "state": {"HKC2731": "attached", "ACI27A8": "attached"}, "armed": None, "polls": 0,
                "minimized": False, "ons": 0}
        ddc = []
        s.counters = lambda: {"telemetry": fake["frame"], "proxy": fake["frame"]}

        def hold(seconds, until=None):
            if fail_in and fake.get("step") == fail_in and not fake.get("failed"):
                fake["failed"] = True
                raise RuntimeError("game gone")
            fake["frame"] += int(max(seconds, 1.0) * 25)
        s.hold = hold

        def tick():
            fake["polls"] += 1
            if fake["armed"] is not None and corey is not None:
                n = fake["polls"] - fake["armed"]
                if n == corey[0]:
                    fake["state"] = {k: "detached" for k in fake["state"]}
                    fake["minimized"] = minimized_without_monitor
                elif n == corey[1]:   # back attached, but the display state reads off for 3 more reads
                    fake["state"] = {k: "attached" for k in fake["state"]}
                    fake["dark"] = 3

        def mons():
            tick()
            return [{"name": n, "primary": n == "HKC2731" and st == "attached", "available": st == "attached",
                     "active": st == "attached", "power": "off" if fake.get("dark") else "on"} for n, st in sorted(fake["state"].items())]

        def power(name, value):
            ddc.append((name, value))
            if value == drive.monitors.DDC_OFF:
                fake["state"][name] = "detached"
            elif fake["ons"] < second_ignores_on:
                fake["ons"] += 1
            else:
                fake["state"][name] = "attached"
                fake["armed"] = fake["polls"]   # Corey's switch is counted from when both are back on
            return True

        def snapshot(pid=None, known=None, pnp=True):
            m = mons()
            return {"time": "t", "monitors": m, "raw": {"pnp": pnp, "frame": fake["frame"]}, "layout": [x["name"] for x in m if x["active"]],
                    "gameWindow": {"minimized": fake["minimized"], "foreground": not fake["minimized"], "rect": [1, 1]}}

        def front(hwnd):
            fake["minimized"] = False
            return True

        def state():
            if fake.get("dark"):
                fake["dark"] -= 1
                return "off"
            return "on"
        steps = [{"monitorStep": m} for m in drive.MONITOR_STEPS]
        orig_step = drive.Spot.monitor_step

        def step(self_, tag, name, *a, **k):
            fake["step"] = name
            return orig_step(self_, tag, name, *a, **k)
        with mock.patch.object(drive.monitors, "monitors", lambda known=None, pnp=True: (mons(), {})), \
                mock.patch.object(drive.monitors, "snapshot", snapshot), \
                mock.patch.object(drive.monitors, "layout", lambda: [n for n, st in sorted(fake["state"].items())
                                                                      if st == "attached"]), \
                mock.patch.object(drive.monitors, "ddc_power", power), \
                mock.patch.object(drive.monitors, "ddc_open", lambda: {}), \
                mock.patch.object(drive.display, "wake", lambda: {"asked": "awake", "display": "on"}), \
                mock.patch.object(drive.display, "state", state), \
                mock.patch.object(drive, "game_window", lambda pid: 77), mock.patch.object(drive, "front", front), \
                mock.patch.object(drive.Spot, "monitor_step", step), \
                mock.patch.object(drive, "set_ini", lambda *a: self.fail("a monitor step wrote the ini")):
            s.bench({"settle": 0, "frames": 1, "liveSteps": steps, "liveStepSeconds": 45, "monitorPollSeconds": 0, "monitorBackSeconds": 0.2,
                     "monitorWaitSeconds": 5 if corey else 0.2})
        self.assertTrue(os.path.exists(os.path.join(tmp, "monitor_layout.json")))
        return s, ddc, fake

    def score_monitor_launch(self, s, gpu):
        """Writes the spot folder the launch would leave (spot.json from the driver's own windows, telemetry.jsonl
        with gpu[step] ms on every frame of each step's range) and scores it with monitor_steps.py."""
        import json, os, sys, tempfile
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "analysis"))
        import monitor_steps
        with tempfile.TemporaryDirectory() as spot:
            with open(os.path.join(spot, "spot.json"), "w") as f:
                json.dump({"windows": s.windows}, f)
            with open(os.path.join(spot, "telemetry.jsonl"), "w") as f:
                for w in s.windows:
                    if w.get("monitorStep") and w.get("telemetry"):
                        a, b = w["telemetry"]
                        for fr in range(a + 1, b + 1):
                            f.write(json.dumps({"frame": fr, "qpcMs": fr * 40.0, "gpuTimeMs": gpu[w["monitorStep"]],
                                                "rayTraced": True}) + "\n")
            code, lines, v = monitor_steps.score(monitor_steps.steps_from(*monitor_steps.load_spot(spot)))
        return code, lines, v

    def test_monitor_steps_record_what_monitor_steps_py_scores_and_put_the_second_monitor_back(self):
        # Worker C's monitor test (Nov 16): both_on, second_off (DDC/CI off of ACI27A8, put back after its hold),
        # both_off (Corey switches both off; the step runs while both read detached), both_on_again (once both are
        # attached and the display reads on). The driver's own windows, written as a spot folder, must score.
        import tempfile
        gpu = {"both_on": 38.7, "second_off": 38.6, "both_off": 35.8, "both_on_again": 38.6}
        with tempfile.TemporaryDirectory() as tmp:
            s, ddc, _ = self.monitor_launch(tmp)
        live = [w for w in s.windows if w.get("monitorStep")]
        self.assertEqual([w["monitorStep"] for w in live], list(drive.MONITOR_STEPS))
        self.assertEqual(ddc, [("ACI27A8", 5), ("ACI27A8", 1)])
        self.assertTrue(live[1]["secondBack"]["back"])
        for w in live:
            self.assertEqual(set(w["monitors"]), {"start", "end"})
            self.assertEqual(set(w["gameWindow"]), {"start", "end"})
            self.assertEqual(len(w["clock"]), 2)
            self.assertGreater(w["monitorsRaw"]["end"]["frame"], w["monitorsRaw"]["start"]["frame"])   # after the hold
        off = live[2]
        self.assertEqual([m["available"] for m in off["monitors"]["start"] + off["monitors"]["end"]], [False] * 4)
        # Corey's polls 3 (both detach) to 33 (both back): the start snapshot is poll 4, the range ends at the last
        # detached poll (32), so 28 polls of 25 frames; the poll that saw them back is kept but not in the range.
        self.assertEqual(off["telemetry"][1] - off["telemetry"][0], 28 * 25)
        self.assertTrue(all(m["available"] for m in off["firstBack"]["monitors"]))
        code, lines, v = self.score_monitor_launch(s, gpu)
        self.assertEqual(code, 0, lines)
        self.assertEqual((v["q1"][0], v["q2"][0]), ("DETACHED SAME AS ASLEEP", "SECOND MONITOR FREE"), lines)

    def test_monitor_both_off_skipped_or_minimized_is_recorded_not_voided(self):
        import tempfile
        gpu = {"both_on": 38.7, "second_off": 38.6, "both_off": 35.8, "both_on_again": 38.6}
        with tempfile.TemporaryDirectory() as tmp:
            s, _, _ = self.monitor_launch(tmp, corey=None)
        off = [w for w in s.windows if w.get("monitorStep") == "both_off"][0]
        self.assertIn("skipped", off)
        self.assertNotIn("telemetry", off)
        self.assertEqual([w["monitorStep"] for w in s.windows if w.get("monitorStep")], list(drive.MONITOR_STEPS))
        code, lines, v = self.score_monitor_launch(s, gpu)
        self.assertEqual(code, 0, lines)
        self.assertTrue(v["q1"][0].startswith("NOT ANSWERED (skipped"), lines)
        # The game minimized with no monitor: recorded, both_on_again brings it to the front, Q1 not answered.
        with tempfile.TemporaryDirectory() as tmp:
            s, _, _ = self.monitor_launch(tmp, minimized_without_monitor=True)
        code, lines, v = self.score_monitor_launch(s, gpu)
        self.assertEqual(code, 0, lines)
        self.assertTrue(v["q1"][0].startswith("NOT ANSWERED (game minimized"), lines)
        self.assertTrue(any(l.startswith("FINDING") and "minimized" in l for l in lines), lines)

    def test_monitor_second_comes_back_when_the_launch_fails_or_the_first_write_is_ignored(self):
        import json, os, tempfile
        for fail_in in ("second_off", "both_off"):
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaisesRegex(RuntimeError, "game gone"):
                    self.monitor_launch(tmp, fail_in=fail_in)
                with open(os.path.join(tmp, "monitor_layout.json")) as f:
                    rec = json.load(f)
            self.assertTrue(rec["sameLayout"], (fail_in, rec))
            self.assertEqual("secondBack" in rec, fail_in == "second_off", (fail_in, rec))
        with tempfile.TemporaryDirectory() as tmp:
            s, ddc, _ = self.monitor_launch(tmp, second_ignores_on=1)
        self.assertEqual(ddc, [("ACI27A8", 5), ("ACI27A8", 1), ("ACI27A8", 1)])
        self.assertTrue([w for w in s.windows if w.get("monitorStep") == "second_off"][0]["secondBack"]["back"])
        bare = drive.Spot.__new__(drive.Spot)   # an unknown step stops the launch before any monitor is touched
        bare.ini, bare.windows, bare.yaw, bare.pitch = "x.ini", [], 0.0, 0.0
        bare.move = bare.bench_window = bare.hold = lambda *a, **k: None
        bare.dump = lambda tag: None
        bare.counters = lambda: {"telemetry": 0, "proxy": 0}
        with mock.patch.object(drive.monitors, "ddc_power", lambda *a: self.fail("a monitor was switched")), \
                self.assertRaisesRegex(ValueError, "monitorStep 'all_off'"):
            bare.bench({"settle": 0, "frames": 1, "liveSteps": [{"monitorStep": "both_on"}, {"monitorStep": "all_off"}]})

    def test_switch_poses_run_before_the_spot_switches_and_return_sam(self):
        # Worker B's must-stay lamp poses: the same on / off / on_again steps at two more poses in the probe launch,
        # Sam still within each; pose A's wall may push Sam, so its fallback is tried; a pose whose teleports all
        # fail is skipped (NOT MEASURED); Sam is back at the spot's teleport before the spot's own switches.
        spawn, a, a2 = {"location": [0, 0, 0], "yaw": -90}, {"location": [1, 0, 0], "yaw": 170}, {"location": [2, 0, 0], "yaw": 175}
        steps = [{"name": "on"}, {"name": "off", "ini": [["Lights", "FlareStandIns", "0"]]}, {"name": "on_again"}]
        for blocked in ([], [a], [a, a2]):
            s = drive.Spot.__new__(drive.Spot)
            s.spot, order = {"name": "con_bright_hall", "teleport": spawn}, []
            s.move = s.hold = lambda *a, **k: None
            s.bench_window = lambda name, *a: None
            s.window = lambda name, kind, body, **k: order.append((name, k.get("fallback"))) if kind == "teleport" else None

            def teleport(tp):
                if tp in blocked:
                    raise RuntimeError("teleport did not hold")
                order.append(("at", tp["yaw"])); return {}
            s.teleport = teleport
            s.switch_test = lambda: order.append((s.spot.get("switchView"), s.spot.get("switchFrames"),
                                                  [v["name"] for v in s.spot["switches"]]))
            s.bench({"settle": 0, "frames": 1, "switches": steps, "switchPoses": [
                {"name": "lampsA", "teleport": a, "fallback": a2, "view": [0, 30], "frames": 60, "switches": steps},
                {"name": "lampsB", "view": [225, 0], "frames": 60, "switches": steps}]})
            poseA = ([("at", 170), ("lampsA_teleport", False)] if not blocked else
                     [("at", 175), ("lampsA_teleport", True)] if len(blocked) == 1 else [("lampsA_teleport", None)])
            if len(blocked) < 2:
                poseA.append(([0, 30], 60, ["lampsA_on", "lampsA_off", "lampsA_on_again"]))
            self.assertEqual(order, poseA + [
                ("at", -90), ("lampsB_teleport", False), ([225, 0], 60, ["lampsB_on", "lampsB_off", "lampsB_on_again"]),
                ("at", -90), (None, None, ["on", "off", "on_again"])], blocked)

    def test_switch_steps_take_their_own_debug_views_and_capture(self):
        # Worker B's see-through probe: every step grabs its debug views after its still, the view is 0 again before
        # the next step (even when a grab fails), and only the step that asks for it takes the USD capture.
        import os, tempfile
        views = {"opaque": 5, "direct": 100}
        steps = [{"name": "on", "debugViews": views},
                 {"name": "off", "ini": [["Lights", "FlareStandIns", "0"]], "debugViews": views},
                 {"name": "on_again", "debugViews": views, "usdCapture": True}]
        for fail in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                s = drive.Spot.__new__(drive.Spot)
                order, idx = [], ["0"]
                s.spot = {"name": "con_bright_hall", "teleport": {"location": [1, 2, 3], "yaw": -90}, "switches": steps}
                s.out, s.ini = tmp, os.path.join(tmp, "d3d9_proxy.ini")
                with open(s.ini, "w") as fh:
                    fh.write("[Proxy]\n")
                s.a = mock.Mock(pid=1)
                s.log = mock.Mock(new_text=lambda: "")
                s.move = s.hold = lambda *a, **k: None
                s.light_dump = lambda dest, draws=False: None
                s.rel_pose = lambda: [0.0, 0.0]
                s.sam_moved = lambda tag: False
                s._try_raster = lambda tag, label: {}
                s.usd_capture = lambda c: order.append(("usd", c["name"])) or {"name": c["name"]}

                def window(name, kind, body, **k):
                    if fail and name == "switch_off_direct_screen":
                        raise RuntimeError("grab failed")
                    order.append((name, idx[0]) if name.endswith("_screen") else name)
                    return {}

                def set_ini(path, section, key, value):
                    if key == "rtx.debugView.debugViewIdx":
                        idx[0] = value
                s.window = window
                with mock.patch.object(drive, "set_ini", set_ini):
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "grab failed"):
                            s.switch_test()
                    else:
                        s.switch_test()
                self.assertEqual(idx[0], "0")
                if fail:
                    continue
                self.assertEqual(order, [
                    "switch_on", ("switch_on_opaque_screen", "5"), ("switch_on_direct_screen", "100"),
                    "switch_off", ("switch_off_opaque_screen", "5"), ("switch_off_direct_screen", "100"),
                    "switch_on_again", ("switch_on_again_opaque_screen", "5"), ("switch_on_again_direct_screen", "100"),
                    ("usd", "switch_on_again")])

    def test_switch_steps_take_their_own_frames_raster_and_draw_dump(self):
        # Worker E2's con_boiler launch (Nov 16): base1 takes 60 frames and a ray-tracing-off raster, base3 keeps its
        # draw dump (worker C's texture fill counters); a step without these keys keeps the spot's 30 frames.
        import os, tempfile
        steps = [{"name": "base1", "frames": 60, "rasterRtOff": True}, {"name": "hide"},
                 {"name": "base3", "frames": 60, "rasterRtOff": True, "drawDump": True}]
        with tempfile.TemporaryDirectory() as tmp:
            s = drive.Spot.__new__(drive.Spot)
            order = []
            s.spot = {"name": "con_boiler", "teleport": {"location": [1, 2, 3], "yaw": -154}, "switches": steps}
            s.out, s.ini = tmp, os.path.join(tmp, "d3d9_proxy.ini")
            with open(s.ini, "w") as fh:
                fh.write("[Proxy]\n")
            s.a = mock.Mock(pid=1)
            s.log = mock.Mock(new_text=lambda: "")
            s.move = s.hold = lambda *a, **k: None
            s.capture = lambda rel, n: order.append(("capture", rel, n))
            s.dump = lambda tag: None
            s.light_dump = lambda dest, draws=False: order.append(("lights", os.path.basename(dest), draws))
            s.raster_rt_off = lambda tag: order.append(("raster", tag))
            s.rel_pose = lambda: [0.0, 0.0]
            s.sam_moved = lambda tag: False
            s.window = lambda name, kind, body, **k: (body(), {})[1]
            with mock.patch.object(drive, "set_ini", lambda *a: None):
                s.switch_test()
        self.assertEqual(order, [
            ("capture", "frames/switch_base1.gray", 60), ("lights", "base1_lights.tsv", False), ("raster", "switch_base1"),
            ("capture", "frames/switch_hide.gray", 30), ("lights", "hide_lights.tsv", False),
            ("capture", "frames/switch_base3.gray", 60), ("lights", "base3_lights.tsv", True), ("raster", "switch_base3")])

    def test_light_dump_keeps_the_same_frames_draw_dump_once_it_is_whole(self):
        # drawDump: the proxy writes f<frame>_draws.tsv beside the light dump and it may still be growing when the
        # light dump appears, so it is copied only once its size has held still; without draws it is not copied.
        import os, tempfile
        for draws in (True, False):
            with tempfile.TemporaryDirectory() as tmp:
                s = drive.Spot.__new__(drive.Spot)
                s.a = mock.Mock(system=tmp)
                s.ini = os.path.join(tmp, "x.ini")
                fd = os.path.join(tmp, "framedump")
                os.makedirs(fd)
                growth = [b"a" * 10, b"b" * 10, b"c" * 10]

                def set_ini(path, section, key, value):
                    if key == "CaptureDraws" and value == "1":
                        open(os.path.join(fd, "f10_lights.tsv"), "w").write("lights")
                        open(os.path.join(fd, "f10_draws.tsv"), "wb").write(growth.pop(0))

                def hold(seconds, until=None):
                    for _ in range(100):
                        if until and until():
                            return
                        if growth:   # still being written: grows between polls for the first few polls
                            open(os.path.join(fd, "f10_draws.tsv"), "ab").write(growth.pop(0))
                s.hold = hold
                dest = os.path.join(tmp, "base3_lights.tsv")
                with mock.patch.object(drive, "set_ini", set_ini), mock.patch.object(drive.time, "sleep", lambda t: None):
                    self.assertEqual(s.light_dump(dest, draws=draws), "base3_lights.tsv")
                got = os.path.join(tmp, "base3_draws.tsv")
                self.assertEqual(os.path.exists(got), draws)
                if draws:
                    self.assertEqual(os.path.getsize(got), 30)   # the whole file, not the first 10 bytes

    def test_live_steps_run_one_bench_load_each_for_their_own_length(self):
        # Worker C's bench bump (Nov 16): a live step may run the bench window's capture or card sampler, started and
        # stopped around its hold, for its own "seconds"; neither key reaches [RemixConfig], and each load leaves its
        # proof (pictures captured, sampler rows) in the window. An unknown load stops the launch before it starts.
        import os, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            s = drive.Spot.__new__(drive.Spot)
            s.ini, s.out, s.windows, calls = "x.ini", tmp, [], []
            s.move = lambda *a: None
            s.bench_window = lambda *a: None
            s.dump = lambda tag: None
            s.counters = lambda: {"telemetry": 0, "proxy": 0}
            s.yaw = s.pitch = 0.0
            s.hold = lambda seconds, until=None: calls.append(("hold", seconds))

            class Proc:
                def __init__(self, kind):
                    self.kind = kind

                def terminate(self):
                    calls.append(("stop", self.kind))

                def wait(self, timeout=None):
                    return 0

            def capture(path, frames=0, fps=30, seconds=0.0):
                calls.append(("capture", fps, seconds))
                open(path, "wb").write(b"\0" * drive.CAP_W * drive.CAP_H * 600)
                return Proc("capture")

            def sampler(out, name):
                calls.append(("sampler", name))
                open(os.path.join(out, name), "w").write("timestamp, memory.total\n" + "x, 1\n" * 100)
                return Proc("sampler")
            steps = [{"rtx.qualityDLSS": "3"}, {"benchLoad": "none", "seconds": 20},
                     {"benchLoad": "capture", "seconds": 20}, {"benchLoad": "sampler", "seconds": 20}]
            with mock.patch.object(drive, "start_capture", capture), mock.patch.object(drive, "start_gpu_sampler", sampler), \
                    mock.patch.object(drive, "set_ini", lambda *a: calls.append(("ini",) + a[1:])):
                s.bench({"settle": 0, "frames": 1, "liveSteps": steps, "liveStepSeconds": 10})
                with self.assertRaisesRegex(ValueError, "benchLoad 'grab'"):
                    s.bench({"settle": 0, "frames": 1, "liveSteps": [{"benchLoad": "grab"}]})
        live = [w for w in s.windows if w["name"].startswith("live_step")]
        self.assertEqual([w.get("benchLoad") for w in live], [None, "none", "capture", "sampler"])
        self.assertEqual([w.get("benchLoadProof") for w in live], [None, None, {"captureFrames": 600}, {"samplerRows": 100}])
        self.assertEqual([w["liveRtx"] for w in live], [{"rtx.qualityDLSS": "3"}, {}, {}, {}])
        self.assertEqual([c for c in calls if c[0] == "ini"], [("ini", "RemixConfig", "rtx.qualityDLSS", "3")])
        self.assertEqual([c for c in calls if c[0] in ("capture", "sampler", "stop") or c in (("hold", 10.0), ("hold", 20.0))],
                         [("hold", 10.0), ("hold", 20.0), ("capture", 30, 20.0), ("hold", 20.0), ("stop", "capture"),
                          ("sampler", "gpu_mem_live_step4.csv"), ("hold", 20.0), ("stop", "sampler")])

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

    def test_draw_log_asks_both_logs_in_one_write_and_retakes_a_capture_the_logged_frame_missed(self):
        # Worker C's white-tank launch: Remix's draw log and the proxy's draw dump must reach the same frame, so the tag
        # and CaptureDraws=1 arrive in one ini write; the tag is cleared by sending it empty. The capture's logged frame
        # can fall outside the capture (about 0.6 s against a re-read about once a second): here the first try misses
        # (cap=0 rows only), the second hits, and the third is not taken.
        import os, re, shutil, tempfile, types
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        system, out = os.path.join(tmp, "SYSTEM"), os.path.join(tmp, "out")
        caps, dumps = os.path.join(system, "rtx-remix", "captures"), os.path.join(system, "framedump")
        logs, gate_dir = os.path.join(system, "rtx-remix", "logs"), os.path.join(system, "gate")
        for d in (caps, dumps, logs, gate_dir, out):
            os.makedirs(d)
        rlog = os.path.join(logs, "remix-dxvk.log")
        open(rlog, "w").close()
        ini = os.path.join(system, "d3d9_proxy.ini")
        with open(ini, "w", newline="") as f:
            f.write("[Proxy]\r\nCaptureDraws=0\r\n[RemixConfig]\r\nrtx.x = 1\r\n")
        state = {"frame": 0, "capturing": False, "captures": 0, "last_tag": "", "dumped": False}
        writes: list[str] = []
        real_write = drive.write_ini

        def record(path, text):
            writes.append(text)
            real_write(path, text)

        def frame(seconds=0, until=None):
            # One proxy re-read and one Remix frame per hold.
            state["frame"] += 1
            with open(ini) as f:
                text = f.read()
            m = re.search(r"(?m)^rtx\.drawLog\.tag = (.*)$", text)
            tag = m.group(1).strip() if m else ""
            if tag and tag != state["last_tag"]:
                with open(os.path.join(gate_dir, f"{tag}_remix_draws.tsv"), "w") as f:
                    f.write(f"remixframe\tframe={state['frame']}\ttag={tag}\tdraws=1\n"
                            f"geom=1\tcap={1 if state['capturing'] else 0}\tcapmat=-1\n")
            state["last_tag"] = tag
            if "CaptureDraws=1" in text and not state["dumped"]:
                with open(os.path.join(dumps, f"f{state['frame']}_draws.tsv"), "w") as f:
                    f.write("draws")
            state["dumped"] = "CaptureDraws=1" in text
            if state["capturing"]:   # the capture ends after this frame and its export is logged
                state["capturing"] = False
                with open(rlog, "a") as f:
                    f.write(f"[GameCapturer][{state['captures']}] End USD export\n")
            return until() if until else None

        def press(key):
            self.assertEqual(key, "CTRL+SHIFT+Q")
            state["captures"] += 1
            # The first capture ends before the proxy's next re-read: its logged frame misses it.
            state["capturing"] = state["captures"] > 1
            open(os.path.join(caps, f"capture_{state['captures']}.usda"), "w").close()
            with open(rlog, "a") as f:
                f.write(f"[GameCapturer][{state['captures']}] New capture\n")
                if state["captures"] == 1:
                    f.write("[GameCapturer][1] End USD export\n")

        s = drive.Spot.__new__(drive.Spot)
        s.a = types.SimpleNamespace(system=system, pid=1)
        s.ini, s.out = ini, out
        s.proxy = types.SimpleNamespace(last_frame=lambda: state["frame"])
        s.teleport, s.move, s.stop = (lambda tp: {"sideways": 0}), (lambda *a: None), (lambda name: None)
        s.rt_is_on, s.hold = (lambda: {"rayTracedLastFrames": [True]}), frame
        with mock.patch.object(drive, "tap", press), mock.patch.object(drive, "game_window", lambda pid: 5), \
                mock.patch.object(drive, "front", lambda h: True), mock.patch.object(drive, "write_ini", record):
            rec = s.usd_capture({"name": "con_boiler", "teleport": {}, "drawLog": "con_boiler_y0_p0"})
        self.assertNotIn("error", rec)
        self.assertEqual([d["tag"] for d in rec["drawLog"]], ["con_boiler_y0_p0", "con_boiler_y0_p0_cap",
                                                             "con_boiler_y0_p0_cap2"])
        self.assertEqual([d["capRows"] for d in rec["drawLog"]], [0, 0, 1])
        self.assertTrue(all(d["remixFile"] and d["drawDump"] for d in rec["drawLog"]))
        self.assertEqual(state["captures"], 2)
        dest = os.path.join(out, "con_boiler", "capture")
        self.assertTrue(os.path.exists(os.path.join(dest, "con_boiler_y0_p0_cap2_remix_draws.tsv")))
        # Every write that sets a tag also sets CaptureDraws=1, and the end state is both cleared.
        for text in writes:
            if re.search(r"(?m)^rtx\.drawLog\.tag = \S", text):
                self.assertIn("CaptureDraws=1", text)
        with open(ini) as f:
            text = f.read()
        self.assertIn("CaptureDraws=0", text)
        self.assertRegex(text, r"(?m)^rtx\.drawLog\.tag = $")
        self.assertIn("rtx.enableReplacementAssets = True", text)

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
            self.assertEqual(len(r["trials"]), 20, name)   # no proxy log: `trials` turns, not forty
            self.assertIsNone(gate.spot_screen_lag({"mouse_lag": r}))

    def test_a_broken_grab_still_gives_the_game_share_of_every_turn(self):
        # The gate's input lag is the game share since 8e779a5; a black or repeating grab used to skip the turn
        # (no mouse move, no gameMs) and stop after three, so the gate had no input lag at all (coordinator, Nov 16).
        for name in ("black", "repeats"):
            sent = []

            class Out:
                def read(self, n):
                    time.sleep(0.0002 if name == "repeats" else 0.01)
                    return bytes(n) if name == "black" else b"\x50" * n
            s = drive.Spot.__new__(drive.Spot)
            s.counts_per_deg_x, s.proxy = 10.0, mock.Mock(path="proxy.log")
            s.hold = lambda seconds, until=None: time.sleep(min(seconds, 0.05))
            # the game turns the camera 25 ms after each mouse move, whatever the screen grab shows
            with mock.patch.object(drive.subprocess, "Popen", lambda *a, **k: mock.Mock(stdout=Out())), \
                    mock.patch.object(drive, "mouse_move_rel", lambda dx, dy: sent.append(dx)), \
                    mock.patch.object(drive, "proxy_camera_dirs", lambda path, since: []), \
                    mock.patch.object(drive, "first_turned", lambda dirs, t0: t0 + 25.0):
                r = s.mouse_lag()
            self.assertEqual(len(sent), 20, name)
            self.assertEqual([t["gameMs"] for t in r["trials"]], [25.0] * 20, name)
            self.assertIsNone(r["samples_ms"], name)
            self.assertTrue(all("error" in t and t["screenMs"] is None for t in r["trials"]), name)
            self.assertEqual(gate.spot_mouse_lag({"mouse_lag": r})["n"], 20, name)
            self.assertIsNone(gate.spot_screen_lag({"mouse_lag": r}), name)

    def _lag_with_fake_grab(self, burst):
        """mouse_lag on a fake gdigrab: ~100 real pictures a second, and the picture changes when the mouse turns.
        `burst`: when the fake hands back the same picture thousands of times a second ("always", "after_turn")."""
        state = {"turned": 0, "fast": False}

        class Out:
            def read(self, n):
                time.sleep(0.0002 if burst == "always" or state["fast"] else 0.01)
                return bytes([40 + 100 * (state["turned"] % 2)]) * n

        def turn(dx, dy):
            state["turned"] += 1
            state["fast"] = burst == "after_turn"

        def hold(seconds, until=None):
            if seconds == 0.5:          # the start of the next turn: the still window runs at the normal rate again
                state["fast"] = False
            time.sleep(min(seconds, 0.05))
        s = drive.Spot.__new__(drive.Spot)
        s.counts_per_deg_x, s.proxy, s.hold = 10.0, mock.Mock(path=None), hold
        with mock.patch.object(drive.subprocess, "Popen", lambda *a, **k: mock.Mock(stdout=Out())), \
                mock.patch.object(drive, "mouse_move_rel", turn):
            return s.mouse_lag()

    def test_a_grab_that_repeats_pictures_is_an_error_not_a_lag(self):
        # Nov 16 04:54 to 07:30 (monitors off): gdigrab gave 5,700 to 7,000 shots a second of one picture (noise 0),
        # saw 0 to 9 of 40 turns, and the 9 it saw (063147 con_bright_hall) were timed as lag.
        ok = self._lag_with_fake_grab(None)
        self.assertNotIn("error", ok)
        self.assertEqual(len(ok["samples_ms"]), 20)
        self.assertLess(ok["captureFps"], 240)
        self.assertIsNotNone(gate.spot_screen_lag({"mouse_lag": ok}))

        always = self._lag_with_fake_grab("always")
        self.assertIsNone(always["samples_ms"])
        self.assertIn("screen grab repeats pictures", always["error"])
        self.assertEqual(len(always["trials"]), 20)       # every turn is still sent
        self.assertEqual(always["trials"][0]["different"], 1)

        # the still windows look normal, but the grab floods after each turn: the turns it timed are not used
        late = self._lag_with_fake_grab("after_turn")
        self.assertTrue(all("error" not in t for t in late["trials"]))
        self.assertGreater(late["captureFps"], 240)
        self.assertIsNone(late["samples_ms"])
        self.assertIn("over the whole measurement", late["error"])
        self.assertIsNone(gate.spot_screen_lag({"mouse_lag": late}))

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
