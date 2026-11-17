"""Display state classes (display_state.py) and their use in settings_report.py: a frame time is compared only between
launches in the same known display state (Nov 17: plug off ran about 12 % faster per frame than plug on)."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import display_state  # noqa: E402
import settings_report  # noqa: E402


class DisplayStateSelfTest(unittest.TestCase):
    def test_self_test(self):
        self.assertEqual(display_state.self_test(), 0)


class SettingsReportDisplayState(unittest.TestCase):
    def report(self, rows):
        with tempfile.TemporaryDirectory() as run:
            names = [r["variant"] for r in rows]
            for n in names:
                os.makedirs(os.path.join(run, n))
            with open(os.path.join(run, "manifest.json"), "w") as f:
                json.dump({"variants": names}, f)
            by_name = {r["variant"]: dict(r) for r in rows}
            with mock.patch.object(settings_report, "variant_row", lambda folder: by_name[os.path.basename(folder)]):
                out, md = settings_report.report(run)
        return {r["variant"]: r for r in out}, md

    def test_plug_off_variant_against_plug_on_base_is_not_compared(self):
        rows, md = self.report([
            {"variant": "01_base", "displayState": "plug on", "frameMs": 37.5},
            {"variant": "02_fog_off", "displayState": "plug off", "frameMs": 33.2},
            {"variant": "03_flares_off", "displayState": "plug on", "frameMs": 36.9},
            {"variant": "04_base", "displayState": "plug off", "frameMs": 33.0}])
        self.assertEqual(rows["02_fog_off"]["frameMsVsBase"], "NOT COMPARABLE")
        self.assertEqual(rows["03_flares_off"]["frameMsVsBase"], -0.6)
        self.assertNotIn("baseNoiseMs", rows["01_base"])   # the plug-off base is no noise sample
        self.assertIn("NOT COMPARABLE", md)

    def test_unknown_base_compares_nothing(self):
        rows, _ = self.report([
            {"variant": "01_base", "displayState": "unknown", "frameMs": 37.5},
            {"variant": "02_fog_off", "displayState": "unknown", "frameMs": 36.0}])
        self.assertEqual(rows["02_fog_off"]["frameMsVsBase"], "NOT COMPARABLE")

    def test_all_plug_on_compares_and_measures_noise(self):
        rows, _ = self.report([
            {"variant": "01_base", "displayState": "plug on", "frameMs": 37.5},
            {"variant": "02_fog_off", "displayState": "plug on", "frameMs": 36.0},
            {"variant": "03_base", "displayState": "plug on", "frameMs": 37.8}])
        self.assertEqual(rows["02_fog_off"]["frameMsVsBase"], -1.5)
        self.assertEqual(rows["01_base"]["baseNoiseMs"], 0.3)


if __name__ == "__main__":
    unittest.main()
