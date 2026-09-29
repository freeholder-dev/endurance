import importlib.util
import os
from contextlib import closing
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

    def test_stale_ac_observation_does_not_claim_unplug_time(self):
        endurance.tick()
        self.now += 600
        self.put("AC/online", "0")
        self.put("BAT0/status", "Discharging")
        self.assertEqual(endurance.tick()["sessions"][0]["start_observed"], 0)

    def test_previous_boot_ac_observation_does_not_claim_unplug_time(self):
        endurance.tick()
        self.now += 20
        (self.proc / "sys/kernel/random/boot_id").write_text("boot-two")
        self.put("AC/online", "0")
        self.put("BAT0/status", "Discharging")
        self.assertEqual(endurance.tick()["sessions"][0]["start_observed"], 0)

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

    def test_reboot_with_charge_gain_interrupts_session(self):
        self.put("AC/online", "0")
        self.put("BAT0/status", "Discharging")
        endurance.tick()
        self.now += 80
        (self.proc / "sys/kernel/random/boot_id").write_text("boot-two")
        self.put("BAT0/capacity", "80")
        report = endurance.tick()
        self.assertEqual(report["sessions"][1]["end_reason"], "interrupted_by_reboot")
        self.assertEqual(report["sessions"][0]["start_observed"], 0)

    def test_hibernate_restart_without_charge_continues_session(self):
        endurance.tick()
        self.now += 20
        self.put("AC/online", "0")
        self.put("BAT0/status", "Discharging")
        first = endurance.tick()["sessions"][0]
        self.now += 7200
        (self.proc / "sys/kernel/random/boot_id").write_text("boot-two")
        self.put("BAT0/capacity", "56")
        with patch.object(endurance.time, "clock_gettime", side_effect=lambda c: 2000 if c == endurance.time.CLOCK_MONOTONIC else 2500):
            report = endurance.tick()
        self.assertEqual(len(report["sessions"]), 1)
        session = report["sessions"][0]
        self.assertEqual(session["id"], first["id"])
        self.assertEqual(session["start_observed"], 1)
        self.assertIsNone(session["end_reason"])
        self.assertEqual(session["unobserved_s"], 7200)
        self.assertEqual(session["suspend_s"], 0)
        self.now += 60
        self.put("AC/online", "1")
        self.put("BAT0/status", "Charging")
        with patch.object(endurance.time, "clock_gettime", side_effect=lambda c: 2060 if c == endurance.time.CLOCK_MONOTONIC else 2560):
            report = endurance.tick()
        self.assertEqual(report["sessions"][0]["end_reason"], "reconnected")
        self.assertIsNone(report["median_s"])

    def test_legacy_sessions_table_gains_unobserved_gap_column(self):
        with closing(endurance.connect()) as db:
            db.execute("ALTER TABLE sessions DROP COLUMN unobserved_s")
            db.commit()
        report = endurance.tick()
        self.assertEqual(report["sessions"], [])
        with closing(sqlite3.connect(endurance.DB)) as db:
            columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        self.assertIn("unobserved_s", columns)

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

    def test_existing_storage_is_made_private(self):
        endurance.STATE.mkdir(mode=0o755)
        endurance.DB.write_bytes(b"")
        sidecar = Path(f"{endurance.DB}-journal")
        sidecar.write_bytes(b"")
        endurance.STATE.chmod(0o755)
        endurance.DB.chmod(0o644)
        sidecar.chmod(0o644)
        with closing(endurance.connect()):
            pass
        self.assertEqual(endurance.STATE.stat().st_mode & 0o777, 0o700)
        self.assertEqual(endurance.DB.stat().st_mode & 0o777, 0o600)
        if sidecar.exists():
            self.assertEqual(sidecar.stat().st_mode & 0o777, 0o600)

    def test_new_database_is_private(self):
        previous = os.umask(0o022)
        try:
            with endurance.database() as db:
                db.execute("INSERT INTO observations(ts,on_battery) VALUES(?,?)", (1, 0))
            self.assertEqual(endurance.STATE.stat().st_mode & 0o777, 0o700)
            self.assertEqual(endurance.DB.stat().st_mode & 0o777, 0o600)
            observed = os.umask(0o022)
            self.assertEqual(observed, 0o022)
        finally:
            os.umask(previous)

    def test_symlink_storage_is_rejected(self):
        target = endurance.STATE.parent / "outside.sqlite3"
        target.write_bytes(b"untouched")
        endurance.STATE.mkdir()
        endurance.DB.symlink_to(target)
        with self.assertRaises(OSError):
            endurance.connect()
        self.assertEqual(target.read_bytes(), b"untouched")
        endurance.DB.unlink()
        Path(f"{endurance.DB}-journal").symlink_to(target)
        with self.assertRaises(OSError):
            endurance.connect()
        self.assertEqual(target.read_bytes(), b"untouched")

    def test_symlink_state_directory_is_rejected(self):
        target = endurance.STATE.parent / "other-state"
        target.mkdir()
        endurance.STATE.symlink_to(target, target_is_directory=True)
        with self.assertRaises(OSError):
            endurance.connect()

    def test_legacy_observations_table_is_migrated(self):
        endurance.STATE.mkdir(parents=True)
        with closing(sqlite3.connect(endurance.DB)) as db:
            with db:
                db.execute("CREATE TABLE observations (id INTEGER PRIMARY KEY, ts REAL, on_battery INTEGER)")
                db.execute("INSERT INTO observations(ts,on_battery) VALUES(?,?)", (self.now - 20, 0))
        self.put("AC/online", "0")
        self.put("BAT0/status", "Discharging")
        self.assertEqual(endurance.tick()["sessions"][0]["start_observed"], 0)


if __name__ == "__main__":
    unittest.main()
