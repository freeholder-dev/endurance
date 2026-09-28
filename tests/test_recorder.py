import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("endurance", Path(__file__).resolve().parents[1] / "endurance.py")
endurance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(endurance)


class RecorderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.power = root / "power"
        self.proc = root / "proc"
        (self.power / "BAT0").mkdir(parents=True)
        (self.power / "AC").mkdir()
        (self.proc / "sys/kernel/random").mkdir(parents=True)
        (self.proc / "sys/kernel/random/boot_id").write_text("boot-one")
        self.put("BAT0/type", "Battery")
        self.put("BAT0/present", "1")
        self.put("BAT0/status", "Charging")
        self.put("BAT0/capacity", "75")
        self.put("BAT0/energy_now", "45_000_000".replace("_", ""))
        self.put("BAT0/energy_full", "60_000_000".replace("_", ""))
        self.put("BAT0/power_now", "10000000")
        self.put("AC/type", "Mains")
        self.put("AC/online", "1")
        self.patches = [patch.object(endurance, "POWER", self.power), patch.object(endurance, "PROC", self.proc), patch.object(endurance, "STATE", root / "state"), patch.object(endurance, "DB", root / "state/history.sqlite3")]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.now = 1000.0
        patcher = patch.object(endurance.time, "time", side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(endurance.time, "clock_gettime", side_effect=lambda c: self.now if c == endurance.time.CLOCK_MONOTONIC else self.now + self.sleep)
        self.sleep = 0.0
        patcher.start()
        self.addCleanup(patcher.stop)

    def put(self, path, value):
        (self.power / path).write_text(str(value))

    def test_partial_discharge_and_reconnect(self):
        endurance.tick()
        self.now += 20
        self.put("AC/online", "0")
        self.put("BAT0/status", "Discharging")
        endurance.tick()
        self.now += 60
        self.sleep += 20
        self.put("BAT0/capacity", "70")
        self.put("BAT0/energy_now", "42000000")
        endurance.tick()
        self.now += 40
        self.put("AC/online", "1")
        self.put("BAT0/status", "Charging")
        self.put("BAT0/capacity", "68")
        self.put("BAT0/energy_now", "40000000")
        report = endurance.tick()
        session = report["sessions"][0]
        self.assertEqual(session["start_percent"], 75)
        self.assertEqual(session["end_percent"], 68)
        self.assertEqual(session["duration_s"], 100)
        self.assertAlmostEqual(session["suspend_s"], 20)
        self.assertAlmostEqual(session["consumed_wh"], 5)
        self.assertEqual(session["start_observed"], 1)
        self.assertEqual(session["end_reason"], "reconnected")
        self.assertEqual(len(session["samples"]), 3)

    def test_first_observation_is_not_claimed_as_unplug(self):
        self.put("AC/online", "0")
        self.put("BAT0/status", "Discharging")
        report = endurance.tick()
        self.assertEqual(report["sessions"][0]["start_observed"], 0)

    def test_reconnect_does_not_count_charge_gain_as_discharge(self):
        endurance.tick()
        self.now += 20
        self.put("AC/online", "0")
        self.put("BAT0/status", "Discharging")
        endurance.tick()
        self.now += 60
        self.put("BAT0/capacity", "70")
        self.put("BAT0/energy_now", "42000000")
        endurance.tick()
        self.now += 20
        self.put("AC/online", "1")
        self.put("BAT0/status", "Charging")
        self.put("BAT0/capacity", "71")
        self.put("BAT0/energy_now", "43000000")
        session = endurance.tick()["sessions"][0]
        self.assertEqual(session["end_percent"], 70)
        self.assertEqual(session["end_wh"], 42)
        self.assertEqual(session["consumed_wh"], 3)
        self.assertEqual(session["samples"][-1]["energy_wh"], 42)

    def test_reboot_interrupts_session(self):
        self.put("AC/online", "0")
        self.put("BAT0/status", "Discharging")
        endurance.tick()
        self.now += 80
        (self.proc / "sys/kernel/random/boot_id").write_text("boot-two")
        report = endurance.tick()
        self.assertEqual(report["sessions"][1]["end_reason"], "interrupted_by_reboot")
        self.assertEqual(report["sessions"][0]["start_observed"], 0)

    def test_unmonitored_awake_gap_does_not_become_complete_session(self):
        endurance.tick()
        self.now += 20
        self.put("AC/online", "0")
        self.put("BAT0/status", "Discharging")
        endurance.tick()
        self.now += 400
        self.put("AC/online", "1")
        report = endurance.tick()
        self.assertEqual(report["sessions"][0]["end_reason"], "monitor_gap")
        self.assertIsNone(report["median_s"])

    def test_reconnect_after_sleep_is_labelled(self):
        endurance.tick()
        self.now += 20
        self.put("AC/online", "0")
        self.put("BAT0/status", "Discharging")
        endurance.tick()
        self.now += 120
        self.sleep += 100
        self.put("AC/online", "1")
        report = endurance.tick()
        self.assertEqual(report["sessions"][0]["end_reason"], "reconnected_after_sleep")
        self.assertIsNone(report["median_s"])

    def test_corrupt_database_is_detected(self):
        endurance.STATE.mkdir(parents=True)
        endurance.DB.write_text("not a sqlite database")
        with self.assertRaises(sqlite3.DatabaseError):
            endurance.tick()


if __name__ == "__main__":
    unittest.main()
