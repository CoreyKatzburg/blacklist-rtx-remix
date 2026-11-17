"""monitors.monitors(): the per-monitor states worker C's monitor_steps.py scores (detached, deactivated, asleep,
attached), built from fake QueryDisplayConfig lists. Run: python -I -m unittest test_monitors.py"""
import unittest
from unittest import mock

import monitors as M

HKC = {"name": "HKC2731", "primary": True, "width": 2560, "height": 1440, "x": 0, "y": 0, "refreshHz": 239.97}


def run(targets, layout, power="on", known=("ACI27A8", "HKC2731")):
    with mock.patch.object(M, "_targets", return_value=dict(enumerate(targets))), \
         mock.patch.object(M, "layout", return_value=layout), mock.patch.object(M, "_power", return_value=power):
        mons, raw = M.monitors(list(known), pnp=False)
    return {m["name"]: m for m in mons}, raw


class MonitorStates(unittest.TestCase):
    def test_both_attached_with_an_empty_connector(self):
        # Nov 16 18:14: the card also lists an empty connector (target4352) that is never available.
        mons, raw = run([{"name": "ACI27A8", "available": True}, {"name": "HKC2731", "available": True},
                         {"name": "target4352", "available": False}],
                        [HKC, {**HKC, "name": "ACI27A8", "primary": False, "x": -2560}])
        self.assertEqual(sorted(mons), ["ACI27A8", "HKC2731"])   # the connector is not a monitor
        self.assertIn("target4352", [t["name"] for t in raw["targets"]])   # but stays in the raw list
        self.assertTrue(mons["HKC2731"]["primary"] and not mons["ACI27A8"]["primary"])
        self.assertTrue(all(m["available"] and m["active"] and m["power"] == "on" for m in mons.values()))

    def test_second_detached_is_listed_unavailable(self):
        # A monitor the card no longer reports must still be listed, as detached, or second_off cannot be scored.
        mons, _ = run([{"name": "HKC2731", "available": True}], [HKC])
        self.assertEqual(mons["ACI27A8"], {"name": "ACI27A8", "primary": False, "available": False, "active": False,
                                           "power": "on"})

    def test_deactivated_is_available_but_not_active(self):
        mons, _ = run([{"name": "ACI27A8", "available": True}, {"name": "HKC2731", "available": True}], [HKC])
        self.assertTrue(mons["ACI27A8"]["available"])
        self.assertFalse(mons["ACI27A8"]["active"])

    def test_asleep_keeps_paths_and_reads_power_off(self):
        mons, _ = run([{"name": "ACI27A8", "available": True}, {"name": "HKC2731", "available": True}],
                      [HKC, {**HKC, "name": "ACI27A8", "primary": False, "x": -2560}], power="off")
        self.assertTrue(all(m["active"] and m["power"] == "off" for m in mons.values()))

    def test_one_target_available_on_any_source_counts(self):
        # A target shows once per source path; available on one of them means attached.
        for order in ((True, False), (False, True)):
            mons, _ = run([{"name": "HKC2731", "available": a} for a in order], [HKC])
            self.assertTrue(mons["HKC2731"]["available"], order)


if __name__ == "__main__":
    unittest.main()
