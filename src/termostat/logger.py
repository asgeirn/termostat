"""Phase 1: log indoor measurements, AC state and weather forecast. No control.

Intended to run from cron every 5-10 minutes.
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from termostat import config, db, metno
from termostat.config import ConfigError, WeatherConfig
from termostat.metno import ForecastStep, MetnoError
from termostat.sensibo import SensiboClient, SensiboError
from termostat.timeutil import iso, parse_iso, parse_sensibo_time, utc_now

DEVICE_FIELDS = ["id", "measurements", "acState", "connectionStatus"]

# Used when met.no does not send a usable Expires header.
DEFAULT_FORECAST_TTL = timedelta(minutes=30)


class DeviceSource(Protocol):
    def get_device(self, device_id: str, fields: list[str]) -> dict[str, Any]: ...

    def get_ac_states(self, device_id: str, limit: int = 20) -> list[dict[str, Any]]: ...


def warn(message: str) -> None:
    print(f"termostat-log: {message}", file=sys.stderr)


# --- Weather forecast -------------------------------------------------------


def _location_key(weather: WeatherConfig) -> str:
    params = metno.location_params(weather)
    return f"{params['lat']},{params['lon']},{params.get('altitude', '')}"


def update_forecast(conn: sqlite3.Connection, weather: WeatherConfig, now: datetime) -> int | None:
    """Fetch a new forecast if the cached one has expired. Returns the current forecast id."""
    location = _location_key(weather)
    cached = conn.execute("SELECT * FROM metno_cache WHERE location = ?", (location,)).fetchone()

    if cached and cached["forecast_id"] and cached["expires"] and now < parse_iso(cached["expires"]):
        return cached["forecast_id"]

    if_modified_since = cached["last_modified"] if cached and cached["forecast_id"] else None
    response = metno.fetch_forecast(weather, if_modified_since)
    expires = iso(response.expires) if response.expires else iso(now + DEFAULT_FORECAST_TTL)

    if response.status == 304:
        forecast_id = cached["forecast_id"]
        last_modified = response.last_modified or cached["last_modified"]
    else:
        assert response.body is not None
        forecast_id = _store_forecast(conn, weather, response.body, now)
        last_modified = response.last_modified

    conn.execute(
        """INSERT INTO metno_cache (location, forecast_id, expires, last_modified) VALUES (?, ?, ?, ?)
           ON CONFLICT(location) DO UPDATE SET
               forecast_id = excluded.forecast_id,
               expires = excluded.expires,
               last_modified = excluded.last_modified""",
        (location, forecast_id, expires, last_modified),
    )
    return forecast_id


def _store_forecast(conn: sqlite3.Connection, weather: WeatherConfig, body: dict[str, Any], now: datetime) -> int:
    updated_at = body.get("properties", {}).get("meta", {}).get("updated_at")
    steps = metno.parse_timeseries(body)
    cursor = conn.execute(
        "INSERT INTO forecasts (fetched_at, updated_at, latitude, longitude, altitude) VALUES (?, ?, ?, ?, ?)",
        (iso(now), iso(parse_iso(updated_at)) if updated_at else None,
         round(weather.latitude, 4), round(weather.longitude, 4), weather.altitude),
    )
    forecast_id = cursor.lastrowid
    assert forecast_id is not None
    conn.executemany(
        f"""INSERT INTO forecast_steps
            (forecast_id, time, {", ".join(metno.INSTANT_FIELDS)}, precipitation_1h, symbol_1h)
            VALUES (?, ?, {", ".join("?" for _ in metno.INSTANT_FIELDS)}, ?, ?)""",
        [
            (forecast_id, iso(step.time), *(step.instant[f] for f in metno.INSTANT_FIELDS),
             step.precipitation_1h, step.symbol_1h)
            for step in steps
        ],
    )
    return forecast_id


def cached_forecast_id(conn: sqlite3.Connection, weather: WeatherConfig) -> int | None:
    row = conn.execute(
        "SELECT forecast_id FROM metno_cache WHERE location = ?", (_location_key(weather),)
    ).fetchone()
    return row["forecast_id"] if row else None


def load_steps(conn: sqlite3.Connection, forecast_id: int) -> list[ForecastStep]:
    rows = conn.execute(
        "SELECT * FROM forecast_steps WHERE forecast_id = ? ORDER BY time", (forecast_id,)
    ).fetchall()
    return [
        ForecastStep(
            time=parse_iso(row["time"]),
            instant={field: row[field] for field in metno.INSTANT_FIELDS},
            precipitation_1h=row["precipitation_1h"],
            symbol_1h=row["symbol_1h"],
        )
        for row in rows
    ]


# --- Sensibo ----------------------------------------------------------------


def _bool_or_none(value: Any) -> int | None:
    return None if value is None else int(bool(value))


def store_reading(
    conn: sqlite3.Connection,
    device_id: str,
    device: dict[str, Any],
    now: datetime,
    forecast_id: int | None,
    outdoor: dict[str, float | None] | None,
) -> None:
    measurements = device.get("measurements") or {}
    ac_state = device.get("acState") or {}
    connection = device.get("connectionStatus") or {}
    outdoor = outdoor or {}
    conn.execute(
        """INSERT INTO readings (
               logged_at, device_id, measured_at, temperature, humidity, feels_like,
               ac_on, mode, fan_level, target_temperature, temperature_unit, swing, is_alive,
               forecast_id, outdoor_temperature, outdoor_relative_humidity, outdoor_wind_speed,
               outdoor_wind_from_direction, outdoor_cloud_area_fraction, outdoor_air_pressure,
               outdoor_precipitation_1h, raw_device)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            iso(now),
            device_id,
            parse_sensibo_time(measurements.get("time")),
            measurements.get("temperature"),
            measurements.get("humidity"),
            measurements.get("feelsLike"),
            _bool_or_none(ac_state.get("on")),
            ac_state.get("mode"),
            ac_state.get("fanLevel"),
            ac_state.get("targetTemperature"),
            ac_state.get("temperatureUnit"),
            ac_state.get("swing"),
            _bool_or_none(connection.get("isAlive")),
            forecast_id if outdoor else None,
            outdoor.get("air_temperature"),
            outdoor.get("relative_humidity"),
            outdoor.get("wind_speed"),
            outdoor.get("wind_from_direction"),
            outdoor.get("cloud_area_fraction"),
            outdoor.get("air_pressure_at_sea_level"),
            outdoor.get("precipitation_1h"),
            json.dumps(device, separators=(",", ":")),
        ),
    )


def store_ac_state_changes(
    conn: sqlite3.Connection, device_id: str, changes: list[dict[str, Any]], now: datetime
) -> int:
    """Store AC state changes not seen before. Returns the number of new changes."""
    new = 0
    for change in changes:
        change_id = change.get("id")
        if not change_id:
            continue
        ac_state = change.get("acState") or {}
        cursor = conn.execute(
            """INSERT OR IGNORE INTO ac_state_changes (
                   id, device_id, changed_at, first_seen_at, status, reason, changed_properties,
                   ac_on, mode, fan_level, target_temperature, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                change_id,
                device_id,
                parse_sensibo_time(change.get("time")),
                iso(now),
                change.get("status"),
                change.get("reason"),
                json.dumps(change.get("changedProperties")),
                _bool_or_none(ac_state.get("on")),
                ac_state.get("mode"),
                ac_state.get("fanLevel"),
                ac_state.get("targetTemperature"),
                json.dumps(change, separators=(",", ":")),
            ),
        )
        new += cursor.rowcount
    return new


# --- Main -------------------------------------------------------------------


def run(
    conn: sqlite3.Connection,
    sensibo: DeviceSource,
    device_id: str,
    weather: WeatherConfig,
    now: datetime,
) -> int:
    """Run one logging cycle. Returns the number of failed steps."""
    failures = 0

    forecast_id = None
    try:
        with conn:
            forecast_id = update_forecast(conn, weather, now)
    except (MetnoError, KeyError, TypeError, ValueError) as error:
        warn(f"weather forecast: {error}")
        failures += 1
        forecast_id = cached_forecast_id(conn, weather)

    outdoor = None
    if forecast_id is not None:
        outdoor = metno.interpolate(load_steps(conn, forecast_id), now)
        if outdoor is None:
            warn("cached weather forecast does not cover the current time")

    try:
        device = sensibo.get_device(device_id, DEVICE_FIELDS)
        with conn:
            store_reading(conn, device_id, device, now, forecast_id, outdoor)
    except SensiboError as error:
        warn(f"device reading: {error}")
        failures += 1

    try:
        changes = sensibo.get_ac_states(device_id)
        with conn:
            store_ac_state_changes(conn, device_id, changes, now)
    except SensiboError as error:
        warn(f"AC state history: {error}")
        failures += 1

    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="termostat-log",
        description="Log indoor measurements, AC state and weather forecast to SQLite.",
    )
    parser.add_argument("--db", type=Path, help=f"database path (default: ${config.ENV_DB} or XDG data dir)")
    args = parser.parse_args(argv)

    try:
        sensibo_config = config.sensibo_config()
        weather = config.weather_config()
    except ConfigError as error:
        warn(str(error))
        return 2

    conn = db.connect(args.db or config.database_path())
    try:
        failures = run(conn, SensiboClient(sensibo_config.api_key), sensibo_config.device_id, weather, utc_now())
    finally:
        conn.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
