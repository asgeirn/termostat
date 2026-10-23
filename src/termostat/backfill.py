"""Backfill indoor measurements from the Sensibo historicalMeasurements endpoint.

Run once at install time (systemd/install.sh) to cover the period before logging starts.
Safe to run repeatedly; measurements already stored are left unchanged.
"""

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any

from termostat import config, db
from termostat.config import ConfigError
from termostat.sensibo import SensiboClient, SensiboError
from termostat.timeutil import parse_sensibo_time

# Sensibo history is limited to 24 hours for this account.
DEFAULT_DAYS = 1


def parse_measurements(result: Any) -> dict[str, dict[str, float | None]]:
    """Normalize the response to {time: {"temperature": ..., "humidity": ...}}.

    Accepts both {"temperature": [{"time", "value"}], "humidity": [...]} and a list of
    {"time", "temperature", "humidity"} objects.
    """
    by_time: dict[str, dict[str, float | None]] = {}

    def add(time: Any, field: str, value: Any) -> None:
        if (parsed := parse_sensibo_time(time)) is None or not isinstance(value, int | float):
            return
        by_time.setdefault(parsed, {"temperature": None, "humidity": None})[field] = float(value)

    if isinstance(result, dict):
        for field in ("temperature", "humidity"):
            for entry in result.get(field) or []:
                if isinstance(entry, dict):
                    add(entry.get("time"), field, entry.get("value"))
    elif isinstance(result, list):
        for entry in result:
            if isinstance(entry, dict):
                for field in ("temperature", "humidity"):
                    add(entry.get("time"), field, entry.get(field))
    return by_time


def store_measurements(conn: sqlite3.Connection, device_id: str, measurements: dict[str, dict[str, float | None]]) -> int:
    """Insert new measurements. Returns the number of rows added."""
    added = 0
    for time, values in sorted(measurements.items()):
        cursor = conn.execute(
            """INSERT INTO historical_measurements (device_id, time, temperature, humidity) VALUES (?, ?, ?, ?)
               ON CONFLICT(device_id, time) DO UPDATE SET
                   temperature = COALESCE(historical_measurements.temperature, excluded.temperature),
                   humidity = COALESCE(historical_measurements.humidity, excluded.humidity)
               WHERE historical_measurements.temperature IS NULL OR historical_measurements.humidity IS NULL""",
            (device_id, time, values["temperature"], values["humidity"]),
        )
        added += cursor.rowcount
    return added


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="termostat-backfill",
        description="Backfill indoor measurements from Sensibo's historical measurements.",
    )
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"number of days to fetch (default: {DEFAULT_DAYS})")
    parser.add_argument("--db", type=Path, help=f"database path (default: ${config.ENV_DB} or XDG data dir)")
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error("--days must be at least 1")

    try:
        sensibo_config = config.sensibo_config()
    except ConfigError as error:
        print(f"termostat-backfill: {error}", file=sys.stderr)
        return 2

    client = SensiboClient(sensibo_config.api_key)
    try:
        result = client.get_historical_measurements(sensibo_config.device_id, args.days)
    except SensiboError as error:
        print(f"termostat-backfill: {error}", file=sys.stderr)
        return 1

    measurements = parse_measurements(result)
    if not measurements:
        print("termostat-backfill: no measurements in response", file=sys.stderr)
        return 1

    conn = db.connect(args.db or config.database_path())
    try:
        with conn:
            added = store_measurements(conn, sensibo_config.device_id, measurements)
    finally:
        conn.close()

    times = sorted(measurements)
    print(f"Fetched {len(measurements)} measurements from {times[0]} to {times[-1]}; {added} new or completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
