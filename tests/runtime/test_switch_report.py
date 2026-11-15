"""switch_report.py on a fake spot folder written the way drive.py writes the switch test. No game."""
import json
import os
import tempfile
import unittest

import numpy as np

import switch_report

W, H = 32, 18


class SwitchReport(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "frames"))
        os.makedirs(os.path.join(self.dir, "switch"))
        self.windows = []

    def add(self, name, frames, lights=None):
        rel = f"frames/switch_{name}.gray" if name != "light_switch" else "frames/light_switch.gray"
        np.asarray(frames, np.uint8).tofile(os.path.join(self.dir, rel))
        w = {"name": name, "kind": "switch", "telemetry": [0, 0], "proxy": [0, 0], "frames": rel,
             "frameSize": [W, H], "switch": {"name": name}}
        if lights:
            with open(os.path.join(self.dir, "switch", f"{name}_lights.tsv"), "w") as f:
                f.write(lights)
            w["lights"] = f"{name}_lights.tsv"
        self.windows.append(w)

    def run_report(self):
        with open(os.path.join(self.dir, "spot.json"), "w") as f:
            json.dump({"name": "fake", "windows": self.windows}, f)
        return {r["variant"]: r for r in switch_report.report(self.dir)["variants"]}

    def test_variant_compared_with_base(self):
        base = np.full((30, H, W), 100)
        base[:, 0, :] = 255                         # one full-white row of 18
        self.add("base", base, "settings\tready=1\nlight\tnode=1\twanted=1\ttracked=1\nlight\tnode=2\twanted=0\ttracked=0\n"
                               "flare\tnode=3\nflare\tnode=4\n")
        fog = np.full((30, H, W), 60)
        fog[:, :9, :] = 0                           # half the picture near black
        self.add("fog_off", fog, "light\tnode=1\twanted=1\ttracked=1\n")
        rows = self.run_report()
        self.assertAlmostEqual(rows["base"]["fullWhite"], 1 / 18, places=3)
        self.assertEqual(rows["base"]["nearBlack"], 0.0)
        self.assertEqual((rows["base"]["lights"], rows["base"]["lightsWanted"], rows["base"]["lightsSent"], rows["base"]["flares"]), (2, 1, 1, 2))
        self.assertAlmostEqual(rows["fog_off"]["nearBlack"], 0.5, places=3)
        expected = (60 * 9 / 18) / 255 - (100 * 17 / 18 + 255 / 18) / 255
        self.assertAlmostEqual(rows["fog_off"]["vsBase"]["brightness"], expected, places=3)
        self.assertEqual(rows["fog_off"]["vsBase"]["lights"], -1)

    def test_slow_fade_is_timed(self):
        t = np.arange(600) / 30.0                   # 20 s at 30 frames per second
        level = np.clip(200 - 18 * t, 20, 200)      # 200 down to 20 over 10 s: within 10% (below 38) after 9 s
        self.add("light_switch", np.repeat(level[:, None, None], H, 1).repeat(W, 2))
        self.assertAlmostEqual(self.run_report()["light_switch"]["fadeSeconds"], 9.0, delta=0.1)

    def test_instant_switch_and_no_change(self):
        level = np.full(600, 20.0)
        level[:3] = 200                             # dark from the 4th frame on
        self.add("light_switch", np.repeat(level[:, None, None], H, 1).repeat(W, 2))
        self.assertLessEqual(self.run_report()["light_switch"]["fadeSeconds"], 0.2)
        self.assertIsNone(switch_report.fade_seconds(np.full(600, 80.0), 30.0))


if __name__ == "__main__":
    unittest.main()
