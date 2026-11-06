"""Phase 2: autonomous overnight step tests.

Run every 5 minutes by termostat-steptest.timer. Shortly after local midnight it decides whether
to run a step test tonight, based on the weather forecast for the night, how stable the room has
been, recent manual changes and the current AC state. If so, it moves the setpoint by STEP
degrees (up for a "heat" test, down for a "cool" test) and restores the previous setpoint at
05:30 local time, or earlier if the room passes a safety limit. A manual change during the test
aborts it and is left in place.

Every decision, write and result is stored in step_tests and step_test_events.
"""

import argparse
import json
import math
import sqlite3
import statistics
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol

from termostat import backfill, config, db
from termostat.config import ConfigError
from termostat.sensibo import SensiboClient, SensiboError
from termostat.timeutil import iso, parse_iso, parse_sensibo_time, utc_now

DEVICE_FIELDS = ["id", "measurements", "acState", "connectionStatus"]

# Setpoint change per kind of test, in the order they are run.
KINDS = {"heat": +2, "cool": -2}


@dataclass(frozen=True)
class Settings:
    # When to decide and start (local time), and when to restore.
    start_window: timedelta = timedelta(minutes=30)
    end_time: time = time(5, 30)
    # Completed tests wanted per kind. Tests aborted at a safety limit also count. Raised from 1
    # on 2026-11-06 to repeat both tests with power metering and the drift-corrected analysis.
    runs_per_kind: int = 2
    # Weather: the night must be stable.
    max_outdoor_spread: float = 3.0
    max_wind_speed: float = 8.0
    max_forecast_age: timedelta = timedelta(hours=3)
    # Room: settled and nobody using the app.
    stable_window: timedelta = timedelta(minutes=60)
    min_stable_readings: int = 9
    max_indoor_range: float = 0.4
    # The analysis corrects for the room's drift before the step (usually still cooling after the
    # evening, 0.1–0.2 °C/h at midnight), so only refuse drift too large to correct reliably. Over
    # 2 hours, since the slope over 1 hour is dominated by wiggles at the sensor's 0.1 °C resolution.
    slope_window: timedelta = timedelta(hours=2)
    max_indoor_slope: float = 0.2  # °C per hour
    manual_quiet_period: timedelta = timedelta(hours=3)
    max_measurement_age: timedelta = timedelta(minutes=10)
    # Never write a setpoint outside this range.
    min_setpoint: int = 16
    max_setpoint: int = 24
    # Restore early if the room gets this cold (cool test) or warm (heat test).
    abort_below: float = 19.0
    abort_above: float = 23.0


class Sensibo(Protocol):
    def get_device(self, device_id: str, fields: list[str]) -> dict[str, Any]: ...

    def set_ac_property(self, device_id: str, name: str, value: Any) -> dict[str, Any]: ...

    def get_historical_measurements(self, device_id: str, days: int = 1) -> Any: ...


def warn(message: str) -> None:
    print(f"termostat-steptest: {message}", file=sys.stderr)


def local_at(night: date, at: time) -> datetime:
    """The given wall-clock time on a date, in the system's local time zone."""
    return datetime.combine(night, at).astimezone()


def decision_night(now: datetime, settings: Settings) -> date | None:
    """The night to decide for, if now is within the start window after local midnight."""
    local = now.astimezone()
    if local - local_at(local.date(), time(0)) < settings.start_window:
        return local.date()
    return None


def next_night(now: datetime, settings: Settings) -> date:
    local = now.astimezone()
    if decision_night(now, settings) is not None:
        return local.date()
    return local.date() + timedelta(days=1)


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


def add_event(conn: sqlite3.Connection, test_id: int, now: datetime, event: str, detail: Any = None) -> None:
    conn.execute(
        "INSERT INTO step_test_events (test_id, at, event, detail) VALUES (?, ?, ?, ?)",
        (test_id, iso(now), event, None if detail is None else _json(detail)),
    )


# --- Decision ---------------------------------------------------------------


def choose_kind(conn: sqlite3.Connection, settings: Settings) -> str | None:
    """The kind of test with the fewest finished runs, or None if all are done."""
    done = dict.fromkeys(KINDS, 0)
    for row in conn.execute(
        """SELECT kind, COUNT(*) FROM step_tests
           WHERE status = 'completed' OR (status = 'aborted' AND end_reason = 'limit') GROUP BY kind"""
    ):
        if row[0] in done:
            done[row[0]] = row[1]
    kind = min(KINDS, key=lambda k: done[k])
    return kind if done[kind] < settings.runs_per_kind else None


def check_device(
    device: dict[str, Any], kind: str | None, now: datetime, settings: Settings, checks: dict[str, Any]
) -> list[str]:
    reasons = []
    ac = device.get("acState") or {}
    measurements = device.get("measurements") or {}
    measured_at = parse_sensibo_time(measurements.get("time"))
    checks["device"] = {
        "is_alive": (device.get("connectionStatus") or {}).get("isAlive"),
        "temperature": measurements.get("temperature"),
        "humidity": measurements.get("humidity"),
        "measured_at": measured_at,
        "ac_state": {k: ac.get(k) for k in ("on", "mode", "fanLevel", "targetTemperature", "swing")},
    }
    if not checks["device"]["is_alive"]:
        reasons.append("device is not connected")
    if not isinstance(measurements.get("temperature"), int | float):
        reasons.append("no indoor temperature")
    if measured_at is None or now - parse_iso(measured_at) > settings.max_measurement_age:
        reasons.append(f"indoor measurement is stale ({measured_at})")
    if ac.get("on") is not True or ac.get("mode") != "heat":
        reasons.append(f"heat pump is not on in heat mode (on={ac.get('on')}, mode={ac.get('mode')})")
    if ac.get("fanLevel") in (None, "auto"):
        reasons.append(f"fan level is {ac.get('fanLevel')!r}, needs a fixed level")
    target = ac.get("targetTemperature")
    if not isinstance(target, int):
        reasons.append(f"setpoint is {target!r}")
    elif kind is not None:
        step = target + KINDS[kind]
        checks["step_target_temperature"] = step
        if not settings.min_setpoint <= step <= settings.max_setpoint:
            reasons.append(f"step setpoint {step} is outside {settings.min_setpoint}–{settings.max_setpoint}")
    return reasons


def check_indoor(conn: sqlite3.Connection, now: datetime, settings: Settings, checks: dict[str, Any]) -> list[str]:
    window = max(settings.stable_window, settings.slope_window)
    samples = [
        (parse_iso(t), v)
        for t, v in conn.execute(
            """SELECT logged_at, temperature FROM readings
               WHERE logged_at >= ? AND logged_at <= ? AND temperature IS NOT NULL ORDER BY logged_at""",
            (iso(now - window), iso(now)),
        )
    ]
    temperatures = [v for t, v in samples if t >= now - settings.stable_window]
    slope = _slope_per_hour([(t, v) for t, v in samples if t >= now - settings.slope_window])
    checks["indoor"] = {
        "readings": len(temperatures),
        "min": min(temperatures, default=None),
        "max": max(temperatures, default=None),
        "mean": round(statistics.fmean(temperatures), 2) if temperatures else None,
        "slope_per_hour": slope,
    }
    if len(temperatures) < settings.min_stable_readings:
        return [f"only {len(temperatures)} readings in the last {settings.stable_window}"]
    reasons = []
    spread = max(temperatures) - min(temperatures)
    if spread > settings.max_indoor_range + 1e-9:
        reasons.append(f"indoor temperature varied {spread:.1f} °C in the last {settings.stable_window}")
    if slope is not None and abs(slope) > settings.max_indoor_slope + 1e-9:
        reasons.append(f"indoor temperature is drifting {slope:+.2f} °C/h over the last {settings.slope_window}")
    return reasons


def check_manual_changes(conn: sqlite3.Connection, now: datetime, settings: Settings, checks: dict[str, Any]) -> list[str]:
    last = conn.execute("SELECT MAX(changed_at) FROM ac_state_changes").fetchone()[0]
    checks["last_ac_state_change"] = last
    if last and now - parse_iso(last) < settings.manual_quiet_period:
        return [f"AC state changed at {last}, less than {settings.manual_quiet_period} ago"]
    return []


def check_forecast(
    conn: sqlite3.Connection, now: datetime, start: datetime, end: datetime, settings: Settings, checks: dict[str, Any]
) -> tuple[int | None, list[str]]:
    forecast = conn.execute("SELECT id, fetched_at FROM forecasts ORDER BY id DESC LIMIT 1").fetchone()
    if forecast is None:
        checks["forecast"] = None
        return None, ["no weather forecast"]
    first = start.replace(minute=0, second=0, microsecond=0)
    steps = conn.execute(
        "SELECT * FROM forecast_steps WHERE forecast_id = ? AND time >= ? AND time <= ? ORDER BY time",
        (forecast["id"], iso(first), iso(end)),
    ).fetchall()

    def values(field: str) -> list[float]:
        return [s[field] for s in steps if s[field] is not None]

    temperatures, winds = values("air_temperature"), values("wind_speed")
    clouds, precipitation = values("cloud_area_fraction"), values("precipitation_1h")
    checks["forecast"] = {
        "forecast_id": forecast["id"],
        "fetched_at": forecast["fetched_at"],
        "steps": len(steps),
        "first_step": steps[0]["time"] if steps else None,
        "last_step": steps[-1]["time"] if steps else None,
        "outdoor_min": min(temperatures, default=None),
        "outdoor_max": max(temperatures, default=None),
        "outdoor_mean": round(statistics.fmean(temperatures), 1) if temperatures else None,
        "wind_max": max(winds, default=None),
        "cloud_mean": round(statistics.fmean(clouds)) if clouds else None,
        "precipitation_sum": round(sum(precipitation), 1),
    }

    reasons = []
    if now - parse_iso(forecast["fetched_at"]) > settings.max_forecast_age:
        reasons.append(f"latest forecast is from {forecast['fetched_at']}")
    if not steps or parse_iso(steps[0]["time"]) > start or parse_iso(steps[-1]["time"]) < end - timedelta(hours=1):
        reasons.append("forecast does not cover the night")
    elif not temperatures:
        reasons.append("forecast has no outdoor temperature")
    else:
        spread = max(temperatures) - min(temperatures)
        if spread > settings.max_outdoor_spread:
            reasons.append(f"outdoor temperature varies {spread:.1f} °C during the night")
        if winds and max(winds) > settings.max_wind_speed:
            reasons.append(f"wind up to {max(winds):.1f} m/s during the night")
    return forecast["id"], reasons


def evaluate(
    conn: sqlite3.Connection, sensibo: Sensibo, device_id: str, night: date, now: datetime, settings: Settings
) -> dict[str, Any]:
    """Collect everything the decision is based on. Writes nothing."""
    start, end = local_at(night, time(0)), local_at(night, settings.end_time)
    checks: dict[str, Any] = {"settings": {k: str(v) for k, v in asdict(settings).items()}}
    reasons = []

    kind = choose_kind(conn, settings)
    if kind is None:
        reasons.append("all step tests are done")

    device = None
    try:
        device = sensibo.get_device(device_id, DEVICE_FIELDS)
        reasons += check_device(device, kind, now, settings, checks)
    except SensiboError as error:
        reasons.append(f"device reading failed: {error}")

    reasons += check_indoor(conn, now, settings, checks)
    reasons += check_manual_changes(conn, now, settings, checks)
    forecast_id, forecast_reasons = check_forecast(conn, now, start, end, settings, checks)
    reasons += forecast_reasons

    return {
        "night": night,
        "kind": kind,
        "device": device,
        "start": start,
        "end": end,
        "forecast_id": forecast_id,
        "checks": checks,
        "reasons": reasons,
    }


# --- Running a test ---------------------------------------------------------


def start_test(
    conn: sqlite3.Connection, sensibo: Sensibo, device_id: str, night: date, now: datetime, settings: Settings
) -> int:
    decision = evaluate(conn, sensibo, device_id, night, now, settings)
    run_test = not decision["reasons"]
    ac = (decision["device"] or {}).get("acState") or {}
    temperature = ((decision["device"] or {}).get("measurements") or {}).get("temperature")
    kind = decision["kind"]
    step = ac["targetTemperature"] + KINDS[kind] if run_test else None

    with conn:
        test_id = conn.execute(
            """INSERT INTO step_tests (
                   night, device_id, decided_at, decision, reasons, checks, forecast_id, kind, status,
                   baseline_on, baseline_mode, baseline_fan_level, baseline_target_temperature,
                   step_target_temperature, start_temperature, planned_end_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                night.isoformat(), device_id, iso(now), "run" if run_test else "skip",
                _json(decision["reasons"]), _json(decision["checks"]), decision["forecast_id"], kind,
                "running" if run_test else "skipped",
                None if ac.get("on") is None else int(bool(ac.get("on"))), ac.get("mode"), ac.get("fanLevel"),
                ac.get("targetTemperature"), step, temperature if run_test else None,
                iso(decision["end"]) if run_test else None,
            ),
        ).lastrowid
        assert test_id is not None
        add_event(conn, test_id, now, "decision", {"run": run_test, "kind": kind, "reasons": decision["reasons"]})
    if not run_test:
        print(f"Skipping step test for the night of {night}: {'; '.join(decision['reasons'])}")
        return 0

    try:
        response = sensibo.set_ac_property(device_id, "targetTemperature", step)
    except SensiboError as error:
        warn(f"setting step setpoint failed: {error}")
        with conn:
            conn.execute(
                "UPDATE step_tests SET status = 'restoring', ended_at = ?, end_reason = 'start_failed' WHERE id = ?",
                (iso(now), test_id),
            )
            add_event(conn, test_id, now, "step_failed", {"error": str(error)})
        # The write may still have reached the unit, so make sure the baseline is set.
        return 1 + restore(conn, sensibo, device_id, test_row(conn, test_id), now)

    with conn:
        conn.execute("UPDATE step_tests SET started_at = ? WHERE id = ?", (iso(now), test_id))
        add_event(conn, test_id, now, "step_written", {"target_temperature": step, "response": response})
    print(f"Started {kind} step test: setpoint {ac['targetTemperature']} -> {step}, until {decision['end']:%H:%M}")
    return 0


def test_row(conn: sqlite3.Connection, test_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM step_tests WHERE id = ?", (test_id,)).fetchone()


def _state_matches(ac: dict[str, Any], test: sqlite3.Row, target: int) -> bool:
    return (
        ac.get("on") is (test["baseline_on"] == 1)
        and ac.get("mode") == test["baseline_mode"]
        and ac.get("fanLevel") == test["baseline_fan_level"]
        and ac.get("targetTemperature") == target
    )


def monitor(conn: sqlite3.Connection, sensibo: Sensibo, device_id: str, test: sqlite3.Row, now: datetime, settings: Settings) -> int:
    """Check a running test, and restore the baseline when it is over."""
    test_id = test["id"]
    try:
        device = sensibo.get_device(device_id, DEVICE_FIELDS)
    except SensiboError as error:
        warn(f"device reading failed: {error}")
        with conn:
            add_event(conn, test_id, now, "device_error", {"error": str(error)})
        device = None

    if device is not None:
        ac = device.get("acState") or {}
        ac_summary = {k: ac.get(k) for k in ("on", "mode", "fanLevel", "targetTemperature")}
        baseline = test["baseline_target_temperature"]

        if test["status"] == "restoring" and _state_matches(ac, test, baseline):
            # An earlier restore attempt reported an error but did reach the unit.
            with conn:
                finish(conn, test, now, "already_restored", ac_summary)
            analyze_after_test(conn, sensibo, device_id, test_id, now)
            return 0

        # While running, only the step state is expected. While restoring, the step state (restore
        # not yet applied) or the baseline is. Anything else is someone using the app.
        expected = [test["step_target_temperature"]] + ([baseline] if test["status"] == "restoring" else [])
        if not any(_state_matches(ac, test, target) for target in expected):
            warn("AC state was changed during the step test; aborting without restoring")
            with conn:
                conn.execute(
                    """UPDATE step_tests SET status = 'aborted', ended_at = COALESCE(ended_at, ?),
                       end_reason = COALESCE(end_reason, 'override') WHERE id = ?""",
                    (iso(now), test_id),
                )
                add_event(conn, test_id, now, "override", ac_summary)
            analyze_after_test(conn, sensibo, device_id, test_id, now)
            return 0

        temperature = (device.get("measurements") or {}).get("temperature")
        if test["status"] == "running" and isinstance(temperature, int | float):
            if (test["kind"] == "cool" and temperature < settings.abort_below) or (
                test["kind"] == "heat" and temperature > settings.abort_above
            ):
                warn(f"room temperature {temperature} passed the safety limit; restoring early")
                with conn:
                    end_test(conn, test_id, now, "limit", {"temperature": temperature})
                test = test_row(conn, test_id)

    if test["status"] == "running" and now >= parse_iso(test["planned_end_at"]):
        with conn:
            end_test(conn, test_id, now, "completed", None)
        test = test_row(conn, test_id)

    if test["status"] == "restoring":
        return restore(conn, sensibo, device_id, test, now)
    return 0


def end_test(conn: sqlite3.Connection, test_id: int, now: datetime, reason: str, detail: Any) -> None:
    conn.execute(
        "UPDATE step_tests SET status = 'restoring', ended_at = ?, end_reason = ? WHERE id = ?",
        (iso(now), reason, test_id),
    )
    add_event(conn, test_id, now, f"end_{reason}", detail)


def finish(conn: sqlite3.Connection, test: sqlite3.Row, now: datetime, event: str, detail: Any) -> None:
    status = "completed" if test["end_reason"] == "completed" else "aborted"
    conn.execute("UPDATE step_tests SET status = ?, restored_at = ? WHERE id = ?", (status, iso(now), test["id"]))
    add_event(conn, test["id"], now, event, detail)


def restore(conn: sqlite3.Connection, sensibo: Sensibo, device_id: str, test: sqlite3.Row, now: datetime) -> int:
    """Set the baseline setpoint again. Returns the number of failures; retried on the next run."""
    baseline = test["baseline_target_temperature"]
    try:
        response = sensibo.set_ac_property(device_id, "targetTemperature", baseline)
    except SensiboError as error:
        warn(f"restoring setpoint {baseline} failed, will retry: {error}")
        with conn:
            add_event(conn, test["id"], now, "restore_failed", {"error": str(error)})
        return 1
    with conn:
        finish(conn, test, now, "restored", {"target_temperature": baseline, "response": response})
    print(f"Restored setpoint {baseline} after {test['kind']} step test ({test['end_reason']})")
    analyze_after_test(conn, sensibo, device_id, test["id"], now)
    return 0


def run(conn: sqlite3.Connection, sensibo: Sensibo, device_id: str, now: datetime, settings: Settings = Settings()) -> int:
    """One timer run. Returns the number of failures."""
    active = conn.execute(
        "SELECT * FROM step_tests WHERE status IN ('running', 'restoring') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if active is not None:
        return monitor(conn, sensibo, device_id, active, now, settings)

    night = decision_night(now, settings)
    if night is None or conn.execute("SELECT 1 FROM step_tests WHERE night = ?", (night.isoformat(),)).fetchone():
        return 0
    return start_test(conn, sensibo, device_id, night, now, settings)


# --- Analysis ---------------------------------------------------------------


def fit_fopdt(points: list[tuple[float, float]], initial_slope: float = 0.0) -> dict[str, float] | None:
    """Fit dy(t) = s τ (1 - exp(-t/τ)) + K∞ (1 - exp(-(t - θ)/τ)) (step part 0 for t <= θ) by least squares.

    points are (hours since the step, change from the pre-step level). s is the room's slope before
    the step in °C/h: a first order system drifting at that rate settles s τ further on, with the
    same time constant, whether or not the step is made. Grid search over θ and τ; K∞ has a closed
    form for each pair.
    """
    if len(points) < 5:
        return None
    best: tuple[float, float, float, float] | None = None
    for theta_step in range(0, 21):  # dead time 0–60 minutes
        theta = theta_step * 0.05
        for tau_step in range(2, 301):  # time constant 0.1–15 hours
            tau = tau_step * 0.05
            f = [1 - math.exp(-(t - theta) / tau) if t > theta else 0.0 for t, _ in points]
            ff = sum(x * x for x in f)
            if ff == 0:
                continue
            y = [v - initial_slope * tau * (1 - math.exp(-t / tau)) for t, v in points]
            k = sum(x * v for x, v in zip(f, y)) / ff
            sse = sum((v - k * x) ** 2 for x, v in zip(f, y))
            if best is None or sse < best[0]:
                best = (sse, k, tau, theta)
    if best is None:
        return None
    sse, k, tau, theta = best
    return {
        "delta_final": round(k, 3),
        "tau_hours": round(tau, 2),
        "dead_time_minutes": round(theta * 60, 1),
        "rmse": round(math.sqrt(sse / len(points)), 3),
        "tau_at_bound": tau >= 15.0 or tau <= 0.1,
    }


def _samples(conn: sqlite3.Connection, device_id: str, since: datetime, until: datetime) -> tuple[str, list[tuple[datetime, float]]]:
    """Indoor temperature samples, from the 90 s history if it covers the period, else the readings."""
    history = [
        (parse_iso(t), v)
        for t, v in conn.execute(
            """SELECT time, temperature FROM historical_measurements
               WHERE device_id = ? AND time >= ? AND time <= ? AND temperature IS NOT NULL ORDER BY time""",
            (device_id, iso(since), iso(until)),
        )
    ]
    expected = (until - since).total_seconds() / 90
    if len(history) >= 0.8 * expected:
        return "historical_measurements", history

    readings: dict[datetime, float] = {}
    for measured, logged, value in conn.execute(
        """SELECT measured_at, logged_at, temperature FROM readings
           WHERE device_id = ? AND logged_at >= ? AND logged_at <= ? AND temperature IS NOT NULL""",
        (device_id, iso(since), iso(until)),
    ):
        readings[parse_iso(measured or logged)] = value
    return "readings", sorted(readings.items())


def _slope_per_hour(samples: list[tuple[datetime, float]]) -> float | None:
    if len(samples) < 3:
        return None
    t0 = samples[0][0]
    xs = [(t - t0).total_seconds() / 3600 for t, _ in samples]
    ys = [v for _, v in samples]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx, 3) if sxx else None


def analyze(conn: sqlite3.Connection, test_id: int, now: datetime, settings: Settings = Settings()) -> dict[str, Any] | None:
    """Fit a first order plus dead time model to a test's response and store it.

    The main fit corrects for the room's drift over settings.slope_window before the step;
    fit_uncorrected assumes the room would have stayed at its pre-step level.
    """
    test = test_row(conn, test_id)
    if test is None or test["started_at"] is None:
        return None
    started = parse_iso(test["started_at"])
    ended = parse_iso(test["ended_at"] or test["restored_at"] or iso(now))
    source, samples = _samples(conn, test["device_id"], started - settings.slope_window, ended)
    pre = [(t, v) for t, v in samples if t < started]
    post = [(t, v) for t, v in samples if started <= t <= ended]
    recent_pre = [v for t, v in pre if t >= started - timedelta(minutes=30)]
    if not recent_pre or not post:
        return None

    level = statistics.fmean(recent_pre)
    delta_setpoint = test["step_target_temperature"] - test["baseline_target_temperature"]
    outdoor = [
        r[0]
        for r in conn.execute(
            "SELECT outdoor_temperature FROM readings WHERE logged_at >= ? AND logged_at <= ? AND outdoor_temperature IS NOT NULL",
            (iso(started), iso(ended)),
        )
    ]
    points = [((t - started).total_seconds() / 3600, v - level) for t, v in post]
    slope = _slope_per_hour(pre)
    fit = fit_fopdt(points, slope or 0.0)
    uncorrected = fit_fopdt(points)
    result = {
        "source": source,
        "samples": len(post),
        "duration_hours": round((ended - started).total_seconds() / 3600, 2),
        "pre_step_level": round(level, 2),
        "pre_step_slope_per_hour": slope,
        "pre_step_slope_window_hours": settings.slope_window.total_seconds() / 3600,
        "delta_setpoint": delta_setpoint,
        "end_temperature": post[-1][1],
        "min_temperature": min(v for _, v in post),
        "max_temperature": max(v for _, v in post),
        "outdoor_mean": round(statistics.fmean(outdoor), 1) if outdoor else None,
        "fan_level": test["baseline_fan_level"],
        "fit": fit,
        "gain": round(fit["delta_final"] / delta_setpoint, 3) if fit and delta_setpoint else None,
        "fit_uncorrected": uncorrected,
        "gain_uncorrected": (
            round(uncorrected["delta_final"] / delta_setpoint, 3) if uncorrected and delta_setpoint else None
        ),
    }
    with conn:
        conn.execute(
            "UPDATE step_tests SET result = ?, analyzed_at = ? WHERE id = ?", (_json(result), iso(now), test_id)
        )
        add_event(conn, test_id, now, "analyzed", {"source": source, "fit": fit})
    return result


def analyze_after_test(conn: sqlite3.Connection, sensibo: Sensibo, device_id: str, test_id: int, now: datetime) -> None:
    """Fetch the 90 s history covering the test, then analyze it. Best effort."""
    try:
        measurements = backfill.parse_measurements(sensibo.get_historical_measurements(device_id, 1))
        with conn:
            backfill.store_measurements(conn, device_id, measurements)
    except SensiboError as error:
        warn(f"fetching historical measurements failed, analyzing logged readings: {error}")
    analyze(conn, test_id, now)


# --- Command line -----------------------------------------------------------


def _format_checks(decision: dict[str, Any]) -> Iterator[str]:
    checks = decision["checks"]
    yield f"Night of {decision['night']} ({decision['start']:%Y-%m-%d %H:%M} – {decision['end']:%H:%M}), next test: {decision['kind'] or 'none'}"
    if device := checks.get("device"):
        ac = device["ac_state"]
        yield (f"  Device:   {device['temperature']} °C, measured {device['measured_at']}; "
               f"on={ac['on']} mode={ac['mode']} fan={ac['fanLevel']} setpoint={ac['targetTemperature']}"
               + (f" -> {checks['step_target_temperature']}" if "step_target_temperature" in checks else ""))
    indoor = checks["indoor"]
    yield (f"  Indoor:   {indoor['readings']} readings in the last hour, {indoor['min']} – {indoor['max']} °C, "
           f"slope {indoor.get('slope_per_hour')} °C/h over the last {checks['settings']['slope_window']}")
    yield f"  Manual:   last AC state change {checks['last_ac_state_change']}"
    if forecast := checks.get("forecast"):
        yield (f"  Forecast: {forecast['outdoor_min']} – {forecast['outdoor_max']} °C, wind max {forecast['wind_max']} m/s, "
               f"cloud {forecast['cloud_mean']} %, precipitation {forecast['precipitation_sum']} mm "
               f"({forecast['steps']} steps, fetched {forecast['fetched_at']})")
    if decision["reasons"]:
        yield "Would skip:"
        yield from (f"  - {reason}" for reason in decision["reasons"])
    else:
        yield "Would run."


def print_status(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT * FROM step_tests ORDER BY night").fetchall()
    if not rows:
        print("No step tests recorded.")
    for row in rows:
        line = f"{row['night']}  {row['status']:<9} {row['kind'] or '-':<4}"
        if row["decision"] == "skip":
            line += "  " + "; ".join(json.loads(row["reasons"] or "[]"))
        else:
            line += (f"  setpoint {row['baseline_target_temperature']} -> {row['step_target_temperature']}"
                     f" ({row['baseline_fan_level']}), started {row['started_at']}, ended {row['ended_at']}"
                     f" ({row['end_reason']}), restored {row['restored_at']}")
            if row["result"]:
                result = json.loads(row["result"])
                if fit := result.get("fit"):
                    line += (f"\n            Δ∞ {fit['delta_final']:+.2f} °C (gain {result['gain']}), "
                             f"τ {fit['tau_hours']} h, dead time {fit['dead_time_minutes']} min, "
                             f"RMSE {fit['rmse']}, outdoor {result['outdoor_mean']} °C, {result['source']}")
                    if "fit_uncorrected" in result:
                        line += (f"\n            pre-step slope {result['pre_step_slope_per_hour']} °C/h; without drift "
                                 f"correction Δ∞ {result['fit_uncorrected']['delta_final']:+.2f} °C "
                                 f"(gain {result['gain_uncorrected']})")
        print(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="termostat-steptest",
        description="Run overnight step tests on the heat pump. Run every 5 minutes by termostat-steptest.timer.",
    )
    parser.add_argument("--db", type=Path, help=f"database path (default: ${config.ENV_DB} or XDG data dir)")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("run", help="decide, start, monitor or restore as needed (default)")
    commands.add_parser("check", help="show what would be decided for the next night, without writing anything")
    commands.add_parser("status", help="list recorded step tests and results")
    analyze_parser = commands.add_parser("analyze", help="refit the model for a test from the stored data")
    analyze_parser.add_argument("night", help="night of the test (YYYY-MM-DD)")
    args = parser.parse_args(argv)
    command = args.command or "run"

    conn = db.connect(args.db or config.database_path())
    try:
        if command == "status":
            print_status(conn)
            return 0
        if command == "analyze":
            row = conn.execute("SELECT id FROM step_tests WHERE night = ?", (args.night,)).fetchone()
            if row is None or analyze(conn, row["id"], utc_now()) is None:
                warn(f"no step test data to analyze for {args.night}")
                return 1
            print_status(conn)
            return 0

        try:
            sensibo_config = config.sensibo_config()
        except ConfigError as error:
            warn(str(error))
            return 2
        client = SensiboClient(sensibo_config.api_key)
        now = utc_now()
        if command == "check":
            decision = evaluate(conn, client, sensibo_config.device_id, next_night(now, Settings()), now, Settings())
            print("\n".join(_format_checks(decision)))
            return 0
        return 1 if run(conn, client, sensibo_config.device_id, now) else 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
