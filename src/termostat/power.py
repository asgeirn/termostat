"""Log the heat pump's power from the Shelly meter, one row per run in power_readings.

Run by termostat-power.timer every minute, separately from termostat-log, so that compressor
cycles and defrosts are resolved to the minute.
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from termostat import config, db
from termostat.config import ConfigError
from termostat.shelly import ShellyClient, ShellyError, store_power_reading
from termostat.timeutil import utc_now


class PowerSource(Protocol):
    host: str

    def get_meter_status(self) -> dict[str, Any]: ...


def warn(message: str) -> None:
    print(f"termostat-power: {message}", file=sys.stderr)


def run(conn: sqlite3.Connection, power: PowerSource, now: datetime) -> int:
    """Read and store one power reading. Returns the number of failed steps."""
    try:
        status = power.get_meter_status()
        with conn:
            store_power_reading(conn, power.host, status, now)
    except ShellyError as error:
        warn(f"power reading: {error}")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="termostat-power",
        description="Log the heat pump's power from the Shelly meter to SQLite.",
    )
    parser.add_argument("--db", type=Path, help=f"database path (default: ${config.ENV_DB} or XDG data dir)")
    args = parser.parse_args(argv)

    try:
        shelly_config = config.shelly_config()
    except ConfigError as error:
        warn(str(error))
        return 2
    if shelly_config is None:
        warn(f"{config.ENV_SHELLY_HOST} is not set")
        return 2

    conn = db.connect(args.db or config.database_path())
    try:
        failures = run(conn, ShellyClient(shelly_config), utc_now())
    finally:
        conn.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
