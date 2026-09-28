#!/usr/bin/env python3
"""Small, unprivileged battery-session recorder for the Omarchy shell."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from contextlib import contextmanager

STATE = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "endurance"
DB = STATE / "history.sqlite3"
POWER = Path(os.environ.get("ENDURANCE_POWER_ROOT", "/sys/class/power_supply"))
PROC = Path(os.environ.get("ENDURANCE_PROC_ROOT", "/proc"))
INTERVAL = 60


def value(path):
    try:
        return path.read_text().strip()
    except (OSError, UnicodeError):
        return None


def number(path, divisor=1):
    raw = value(path)
    try:
        return float(raw) / divisor if raw is not None else None
    except ValueError:
        return None


def battery():
    for path in sorted(POWER.iterdir()):
        if value(path / "type") == "Battery" and value(path / "present") != "0":
            return path
    return None


def observation():
    bat = battery()
    if bat is None:
        return None
    status = value(bat / "status")
    ac_values = [value(p / "online") for p in POWER.iterdir() if value(p / "type") in ("Mains", "USB", "USB_C")]
    # AC online is authoritative; battery status is a fallback where AC is absent.
    if "1" in ac_values:
        on_battery = False
    elif "0" in ac_values:
        on_battery = status == "Discharging"
    else:
        on_battery = status == "Discharging"
    charge = number(bat / "charge_now", 1_000_000)
    full_charge = number(bat / "charge_full", 1_000_000)
    voltage = number(bat / "voltage_now", 1_000_000)
    energy = number(bat / "energy_now", 1_000_000)
    full_energy = number(bat / "energy_full", 1_000_000)
    energy_kind = "measured" if energy is not None else "charge_voltage" if charge is not None and voltage is not None else None
    if energy is None and charge is not None and voltage is not None:
        energy = charge * voltage
        full_energy = full_charge * voltage if full_charge is not None else None
    power = number(bat / "power_now", 1_000_000)
    if power is None:
        current = number(bat / "current_now", 1_000_000)
        power = current * voltage if current is not None and voltage is not None else None
    now = time.time()
    return dict(ts=now, boot=value(PROC / "sys/kernel/random/boot_id"),
                awake=time.clock_gettime(time.CLOCK_MONOTONIC),
                boottime=time.clock_gettime(time.CLOCK_BOOTTIME),
                battery=bat.name, model=value(bat / "model_name"),
                on_battery=on_battery, status=status,
                percent=number(bat / "capacity"), energy_wh=energy,
                full_wh=full_energy, energy_kind=energy_kind,
                power_w=power if on_battery else None)


def process_times():
    totals = {}
    try:
        entries = list(PROC.iterdir())
    except OSError:
        return totals
    for entry in entries:
        if not entry.name.isdigit():
            continue
        line = value(entry / "stat")
        if not line:
            continue
        end = line.rfind(")")
        if end < 0:
            continue
        fields = line[end + 2:].split()
        if len(fields) < 20:
            continue
        try:
            ticks = int(fields[11]) + int(fields[12])
            start = int(fields[19])
        except ValueError:
            continue
        name = line[line.find("(") + 1:end].strip()[:80]
        if name:
            totals[f"{entry.name}:{start}"] = (name, ticks)
    return totals


def connect():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=5000")
        db.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (
      id INTEGER PRIMARY KEY, unplug_ts REAL NOT NULL, reconnect_ts REAL,
      start_percent REAL, end_percent REAL, duration_s REAL, suspend_s REAL DEFAULT 0,
      start_wh REAL, end_wh REAL, consumed_wh REAL, energy_kind TEXT,
      full_wh REAL, battery TEXT, model TEXT, boot TEXT,
      start_observed INTEGER NOT NULL DEFAULT 1, end_reason TEXT
    );
    CREATE TABLE IF NOT EXISTS samples (
      id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id),
      ts REAL NOT NULL, percent REAL, energy_wh REAL, power_w REAL,
      awake REAL, boottime REAL
    );
    CREATE INDEX IF NOT EXISTS samples_session ON samples(session_id, ts);
    CREATE TABLE IF NOT EXISTS process_snapshots (
      session_id INTEGER NOT NULL, pid_key TEXT NOT NULL, name TEXT NOT NULL,
      cpu_ticks INTEGER NOT NULL, PRIMARY KEY(session_id,pid_key)
    );
    CREATE TABLE IF NOT EXISTS process_activity (
      session_id INTEGER NOT NULL, name TEXT NOT NULL, cpu_ticks INTEGER NOT NULL,
      PRIMARY KEY(session_id,name)
    );
    CREATE TABLE IF NOT EXISTS observations (
      id INTEGER PRIMARY KEY, ts REAL, on_battery INTEGER, boot TEXT
    );
    """)
        if "boot" not in {r[1] for r in db.execute("PRAGMA table_info(observations)")}:
            db.execute("ALTER TABLE observations ADD COLUMN boot TEXT")
    except sqlite3.Error:
        db.close()
        raise
    return db


@contextmanager
def database():
    db = connect()
    try:
        with db:
            yield db
    finally:
        db.close()


def last_sample(db, sid):
    return db.execute("SELECT * FROM samples WHERE session_id=? ORDER BY id DESC LIMIT 1", (sid,)).fetchone()


def finish(db, row, obs, reason):
    last = last_sample(db, row["id"])
    endpoint = obs if reason.startswith("reconnected") else None
    end_pct = endpoint["percent"] if endpoint else (last["percent"] if last else row["start_percent"])
    end_wh = endpoint["energy_wh"] if endpoint else (last["energy_wh"] if last else row["start_wh"])
    if endpoint and last:
        # Charging may have raised the reading before the reconnect tick ran.
        if end_pct is not None and last["percent"] is not None:
            end_pct = min(end_pct, last["percent"])
        if end_wh is not None and last["energy_wh"] is not None:
            end_wh = min(end_wh, last["energy_wh"])
        awake_delta = obs["awake"] - last["awake"]
        boot_delta = obs["boottime"] - last["boottime"]
        if 0 <= awake_delta <= boot_delta + 2:
            db.execute("UPDATE sessions SET suspend_s=suspend_s+? WHERE id=?",
                       (max(0, boot_delta - awake_delta), row["id"]))
        db.execute("INSERT INTO samples(session_id,ts,percent,energy_wh,power_w,awake,boottime) VALUES(?,?,?,?,?,?,?)",
                   (row["id"], obs["ts"], end_pct, end_wh, None, obs["awake"], obs["boottime"]))
    end_ts = endpoint["ts"] if endpoint else (last["ts"] if last else row["unplug_ts"])
    consumed = max(0, row["start_wh"] - end_wh) if row["start_wh"] is not None and end_wh is not None else None
    db.execute("""UPDATE sessions SET reconnect_ts=?,end_percent=?,duration_s=?,end_wh=?,consumed_wh=?,end_reason=? WHERE id=?""",
               (end_ts, end_pct, max(0, end_ts - row["unplug_ts"]), end_wh, consumed, reason, row["id"]))
    db.execute("DELETE FROM process_snapshots WHERE session_id=?", (row["id"],))


def sample(db, sid, obs):
    previous = last_sample(db, sid)
    if previous and obs["ts"] - previous["ts"] < 10:
        return
    if previous and obs["boot"]:
        awake_delta = obs["awake"] - previous["awake"]
        boot_delta = obs["boottime"] - previous["boottime"]
        if 0 <= awake_delta <= boot_delta + 2:
            suspend = max(0, boot_delta - awake_delta)
            db.execute("UPDATE sessions SET suspend_s=suspend_s+? WHERE id=?", (suspend, sid))
    db.execute("INSERT INTO samples(session_id,ts,percent,energy_wh,power_w,awake,boottime) VALUES(?,?,?,?,?,?,?)",
               (sid, obs["ts"], obs["percent"], obs["energy_wh"], obs["power_w"], obs["awake"], obs["boottime"]))
    current = process_times()
    previous_processes = {r["pid_key"]: r for r in db.execute("SELECT * FROM process_snapshots WHERE session_id=?", (sid,))}
    for key, (name, ticks) in current.items():
        old = previous_processes.get(key)
        if old and ticks >= old["cpu_ticks"]:
            db.execute("""INSERT INTO process_activity(session_id,name,cpu_ticks) VALUES(?,?,?)
                          ON CONFLICT(session_id,name) DO UPDATE SET cpu_ticks=cpu_ticks+excluded.cpu_ticks""",
                       (sid, name, ticks - old["cpu_ticks"]))
    db.execute("DELETE FROM process_snapshots WHERE session_id=?", (sid,))
    db.executemany("INSERT INTO process_snapshots(session_id,pid_key,name,cpu_ticks) VALUES(?,?,?,?)",
                   ((sid, key, name, ticks) for key, (name, ticks) in current.items()))


def tick():
    obs = observation()
    if obs is None:
        return {"error": "No battery found"}
    with database() as db:
        row = db.execute("SELECT * FROM sessions WHERE reconnect_ts IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        if row and row["boot"] != obs["boot"]:
            finish(db, row, None, "interrupted_by_reboot")
            row = None
        if row and row["battery"] != obs["battery"]:
            finish(db, row, None, "battery_changed")
            row = None
        if row:
            previous = last_sample(db, row["id"])
            if previous and obs["awake"] - previous["awake"] > 5 * INTERVAL:
                finish(db, row, None, "monitor_gap")
                row = None
        if obs["on_battery"]:
            if row is None:
                # On first observation, the actual unplug time is unknowable.
                previous = db.execute("SELECT ts,on_battery,boot FROM observations ORDER BY id DESC LIMIT 1").fetchone()
                observed = (previous is not None and previous["on_battery"] == 0
                            and previous["boot"] == obs["boot"]
                            and 0 <= obs["ts"] - previous["ts"] <= 5 * INTERVAL)
                db.execute("""INSERT INTO sessions(unplug_ts,start_percent,start_wh,energy_kind,full_wh,battery,model,boot,start_observed)
                              VALUES(?,?,?,?,?,?,?,?,?)""",
                           (obs["ts"], obs["percent"], obs["energy_wh"], obs["energy_kind"], obs["full_wh"], obs["battery"], obs["model"], obs["boot"], int(observed)))
                row = db.execute("SELECT * FROM sessions WHERE id=last_insert_rowid()").fetchone()
            sample(db, row["id"], obs)
        elif row:
            previous = last_sample(db, row["id"])
            slept = bool(previous and obs["boottime"] - previous["boottime"] -
                         (obs["awake"] - previous["awake"]) > INTERVAL)
            finish(db, row, obs, "reconnected_after_sleep" if slept else "reconnected")
        db.execute("INSERT INTO observations(ts,on_battery,boot) VALUES(?,?,?)", (obs["ts"], int(obs["on_battery"]), obs["boot"]))
        db.execute("DELETE FROM observations WHERE id NOT IN (SELECT id FROM observations ORDER BY id DESC LIMIT 2)")
    return snapshot(obs)


def snapshot(obs=None):
    obs = obs or observation()
    if obs is None:
        return {"error": "No battery found"}
    with database() as db:
        rows = db.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 12").fetchall()
        sessions = []
        for row in rows:
            item = dict(row)
            samples = [dict(r) for r in db.execute("SELECT ts,percent,energy_wh,power_w FROM samples WHERE session_id=? ORDER BY id", (row["id"],))]
            step = max(1, len(samples) // 60)
            item["samples"] = samples[::step]
            if samples and item["samples"][-1] != samples[-1]:
                item["samples"].append(samples[-1])
            activities = [dict(r) for r in db.execute("SELECT name,cpu_ticks FROM process_activity WHERE session_id=? ORDER BY cpu_ticks DESC LIMIT 6", (row["id"],))]
            item["activity"] = activities
            item["cpu_total"] = db.execute("SELECT COALESCE(SUM(cpu_ticks),0) FROM process_activity WHERE session_id=?", (row["id"],)).fetchone()[0]
            sessions.append(item)
        completed = [r[0] for r in db.execute(
            """SELECT duration_s FROM sessions
               WHERE end_reason='reconnected' AND start_observed=1 AND duration_s IS NOT NULL
               ORDER BY id DESC LIMIT 7""")]
    completed.sort()
    median = None
    if completed:
        n = len(completed)
        median = (completed[(n-1)//2] + completed[n//2]) / 2
    return {"current": obs, "sessions": sessions, "median_s": median}


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "snapshot"
    try:
        result = tick() if command == "tick" else snapshot()
        print(json.dumps(result, separators=(",", ":")))
    except (OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
