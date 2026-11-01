"""SQLite storage. All timestamps are stored as UTC ISO 8601 strings."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY,
    logged_at TEXT NOT NULL,
    device_id TEXT NOT NULL,
    measured_at TEXT,
    temperature REAL,
    humidity REAL,
    feels_like REAL,
    ac_on INTEGER,
    mode TEXT,
    fan_level TEXT,
    target_temperature INTEGER,
    temperature_unit TEXT,
    swing TEXT,
    is_alive INTEGER,
    forecast_id INTEGER REFERENCES forecasts(id),
    outdoor_temperature REAL,
    outdoor_relative_humidity REAL,
    outdoor_wind_speed REAL,
    outdoor_wind_from_direction REAL,
    outdoor_cloud_area_fraction REAL,
    outdoor_air_pressure REAL,
    outdoor_precipitation_1h REAL,
    raw_device TEXT
);
CREATE INDEX IF NOT EXISTS readings_logged_at ON readings(logged_at);

CREATE TABLE IF NOT EXISTS ac_state_changes (
    id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    changed_at TEXT,
    first_seen_at TEXT NOT NULL,
    status TEXT,
    reason TEXT,
    changed_properties TEXT,
    ac_on INTEGER,
    mode TEXT,
    fan_level TEXT,
    target_temperature INTEGER,
    raw TEXT
);
CREATE INDEX IF NOT EXISTS ac_state_changes_changed_at ON ac_state_changes(changed_at);

CREATE TABLE IF NOT EXISTS historical_measurements (
    device_id TEXT NOT NULL,
    time TEXT NOT NULL,
    temperature REAL,
    humidity REAL,
    PRIMARY KEY (device_id, time)
);

CREATE TABLE IF NOT EXISTS forecasts (
    id INTEGER PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    updated_at TEXT,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    altitude INTEGER
);

CREATE TABLE IF NOT EXISTS forecast_steps (
    forecast_id INTEGER NOT NULL REFERENCES forecasts(id),
    time TEXT NOT NULL,
    air_temperature REAL,
    relative_humidity REAL,
    wind_speed REAL,
    wind_from_direction REAL,
    cloud_area_fraction REAL,
    air_pressure_at_sea_level REAL,
    precipitation_1h REAL,
    symbol_1h TEXT,
    PRIMARY KEY (forecast_id, time)
);

CREATE TABLE IF NOT EXISTS metno_cache (
    location TEXT PRIMARY KEY,
    forecast_id INTEGER REFERENCES forecasts(id),
    expires TEXT,
    last_modified TEXT
);

-- One row per night the step test timer considered (see steptest.py).
CREATE TABLE IF NOT EXISTS step_tests (
    id INTEGER PRIMARY KEY,
    night TEXT NOT NULL UNIQUE,          -- local date of the midnight the test starts at
    device_id TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    decision TEXT NOT NULL,              -- run, skip
    reasons TEXT,                        -- JSON list of reasons to skip
    checks TEXT,                         -- JSON object with all values the decision was based on
    forecast_id INTEGER REFERENCES forecasts(id),
    kind TEXT,                           -- heat, cool
    status TEXT NOT NULL,                -- skipped, running, restoring, completed, aborted
    baseline_on INTEGER,
    baseline_mode TEXT,
    baseline_fan_level TEXT,
    baseline_target_temperature INTEGER,
    step_target_temperature INTEGER,
    start_temperature REAL,
    started_at TEXT,
    planned_end_at TEXT,
    ended_at TEXT,
    end_reason TEXT,                     -- completed, limit, override, start_failed
    restored_at TEXT,
    result TEXT,                         -- JSON: fitted first order plus dead time model
    analyzed_at TEXT
);

CREATE TABLE IF NOT EXISTS step_test_events (
    id INTEGER PRIMARY KEY,
    test_id INTEGER NOT NULL REFERENCES step_tests(id),
    at TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT                          -- JSON
);
CREATE INDEX IF NOT EXISTS step_test_events_test_id ON step_test_events(test_id);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
