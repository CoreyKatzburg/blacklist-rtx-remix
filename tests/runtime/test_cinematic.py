"""Cutscene start and end from the camera numbers (cinematic.find_span), on the Benghazi opening's shape."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cinematic  # noqa: E402


def path(moving_ms: float, still_ms: float, step_ms: float = 45.0):
    """Loading frames with no camera, then a camera gliding 4 units a frame, then standing still."""
    rows, t, f = [], 0.0, 0
    for _ in range(20):
        rows.append((f, t, None)); f += 1; t += step_ms
    x = 3754.0
    while t < 900 + moving_ms:
        rows.append((f, t, (x, 3107.0, 225.0))); f += 1; t += step_ms; x -= 4.0
    end = (f, t)
    while t < 900 + moving_ms + still_ms:
        rows.append((f, t, (x, 3107.0, 225.0))); f += 1; t += step_ms
    return rows, end


class Span(unittest.TestCase):
    def test_start_is_first_camera_and_end_is_where_it_settles(self):
        rows, end = path(9700, 3000)
        start, settled = cinematic.find_span(rows, 1.0, 0.0)
        self.assertEqual(start[0], 20)                 # first frame with a camera, not the loading frames
        self.assertEqual((settled[0], settled[1]), end)

    def test_no_end_while_the_camera_never_stays_still(self):
        rows, _ = path(9700, 600)                      # still for only 0.6 s
        self.assertIsNone(cinematic.find_span(rows, 1.0, 0.0)[1])

    def test_camera_rows_before_the_signal_are_ignored(self):
        rows, end = path(9700, 3000)
        start, _ = cinematic.find_span(rows, 1.0, rows[100][1])
        self.assertEqual(start[0], 100)

    def test_letterbox_is_rows_black_in_every_frame(self):
        import numpy as np
        f = np.full((3, 10, 4), 200, np.uint8)
        f[:, :2] = 0; f[:, -3:] = 0      # bars: 2 rows on top, 3 at the bottom
        f[1, 2] = 3                       # one dark frame in the picture's first row is not a bar
        self.assertEqual(cinematic.letterbox(f), (2, 3))
        self.assertEqual(cinematic.letterbox(np.full((2, 6, 4), 90, np.uint8)), (0, 0))

    def test_camera_row_reads_the_proxy_frame_log(self):
        line = "5288,110659068.244,347.310,166.463,1292,8,G:0:0:3783.534:3067.816:225.799:0.15:0.07:-0.98:0.0|G:84:1:1:2:3"
        self.assertEqual(cinematic.camera_row(line), (5288, 110659068.244, (3783.534, 3067.816, 225.799)))
        self.assertEqual(cinematic.camera_row("540,1663.1,1.6,1,1,1,G:0:0:0.000:-0.000:0.000:1:0:0:0")[2], None)
        self.assertIsNone(cinematic.camera_row("frame,ms"))

    def test_draw_dump_takes_only_new_files_and_turns_the_capture_off(self):
        # Worker F's mid-cutscene dump: a fake proxy writes f5000_draws.tsv and a new shader when CaptureDraws=1.
        # A dump of the same name from an earlier launch, and an older shader, must not count; the ini ends at 0.
        import re, tempfile, threading, time
        with tempfile.TemporaryDirectory() as root:
            system, out = os.path.join(root, "SYSTEM"), os.path.join(root, "out")
            for f in ("framedump", "psdump", "vsdump"):
                os.makedirs(os.path.join(system, f))
            old = os.path.join(system, "framedump", "f5000_draws.tsv")
            for p in (old, os.path.join(system, "psdump", "ps_old.bin")):
                open(p, "w").close(); os.utime(p, (1, 1))
            ini = os.path.join(system, "d3d9_proxy.ini")
            with open(ini, "w", newline="") as f:
                f.write("[Proxy]\r\nCaptureDraws=0\r\n")
            def proxy():
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if re.search(r"CaptureDraws=1", open(ini).read()):
                        time.sleep(0.3)
                        with open(old, "w") as f:
                            f.write("rewritten")
                        open(os.path.join(system, "psdump", "ps_new.bin"), "w").close()
                        return
                    time.sleep(0.05)
            t = threading.Thread(target=proxy); t.start()
            rec = cinematic.draw_dump(ini, system, out, timeout=4.0)
            t.join()
            self.assertEqual(rec.get("frame"), 5000)
            self.assertEqual(sorted(rec["files"]), [os.path.join("framedump", "f5000_draws.tsv"),
                                                    os.path.join("psdump", "ps_new.bin")])
            self.assertIn("CaptureDraws=0", open(ini).read())
            self.assertFalse(os.path.exists(os.path.join(out, "drawdump", "psdump", "ps_old.bin")))
            # Nothing written: an error, not a quiet success, and the capture still ends at 0.
            rec = cinematic.draw_dump(ini, system, out, timeout=0.5)
            self.assertIn("error", rec)
            self.assertIn("CaptureDraws=0", open(ini).read())


if __name__ == "__main__":
    unittest.main()
