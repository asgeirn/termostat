"""Phase 3: PI controller for the room temperature, in shadow mode. Writes nothing to the heat pump.

Run every 5 minutes by termostat-control.timer, after termostat-log. Each run stores one row in
control_runs; the latest row is the controller's state, so runs are otherwise stateless.

The controller is the outer loop of a cascade: the heat pump's own thermostat holds its setpoint,
and this PI moves the setpoint to bring the Sky's temperature to the target. The PI output is a
continuous setpoint; the heat pump only takes whole degrees, so a first order sigma-delta
quantizer alternates between the two nearest whole degrees to give the right average, with a
minimum time between changes.

Shadow mode. The household keeps control, so the room responds to the actual setpoint, not to
the controller's. A PI fed with the real room would wind up against a plant that never reacts
to it. Instead the controller sees a counterfactual room: the measured temperature plus the
modelled effect of having the controller's setpoint instead of the actual one (model_offset).
That effect is the difference between the measured overnight plateaus for the two setpoints,
approached with the room's time constant. Everything else (outdoor temperature, sun, cooking)
comes from the real measurement. The counterfactual is only as good as the plateau model, so it
shows how the controller behaves (setpoint changes, duty cycles, reaction to disturbances), not
an exact prediction of the room.
"""

import argparse
import math
import sqlite3
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from termostat import config, db
from termostat.timeutil import iso, parse_iso, utc_now

MODE = "shadow"


def _plateaus() -> dict[int, float]:
    # Overnight Sky temperature per setpoint at fan medium_low, outdoor 1–8 °C (STATUS.md, third
    # review). 21 is not measured and is interpolated.
    return {18: 19.3, 19: 20.1, 20: 20.6, 22: 21.5}


@dataclass(frozen=True)
class Settings:
    # Comfort minimum is 20.0 °C; aim a little above it to leave room for the ripple from
    # alternating between two setpoints.
    target: float = 20.3
    comfort_min: float = 20.0
    # PI tuning from the step tests: gain about 0.5–0.7 °C per setpoint degree around 19–20,
    # τ about 2.5 h, no dead time. SIMC with a closed loop time constant of 1.5 h gives Kp 2.8 and
    # Ti 2.5 h; start a little gentler.
    kp: float = 2.0
    ti: timedelta = timedelta(hours=2.5)
    # Setpoint range the controller may use.
    min_setpoint: int = 18
    max_setpoint: int = 22
    # Quantizer: change setpoint when the accumulated difference between the PI output and the
    # setpoint reaches this many °C·h, and never sooner than min_dwell after the last change.
    # Alternating at duty d between two setpoints takes 2·threshold / (d (1 - d)) hours per cycle:
    # 2.4 h at 50 %, about 20 changes a day. A larger threshold means fewer changes but more
    # ripple. Replayed over 2026-10-28 to 11-06: 26 changes a day, RMS error 0.27 °C.
    quantizer_threshold: float = 0.3
    min_dwell: timedelta = timedelta(minutes=30)
    # Shadow model.
    model_tau: timedelta = timedelta(hours=2.5)
    plateaus: dict[int, float] = field(default_factory=_plateaus)
    # Inputs.
    measurement_window: timedelta = timedelta(minutes=15)
    max_measurement_age: timedelta = timedelta(minutes=10)
    # Restart (bumplessly, from the actual setpoint) after a longer gap between runs.
    max_gap: timedelta = timedelta(minutes=20)


def warn(message: str) -> None:
    print(f"termostat-control: {message}", file=sys.stderr)


def plateau(setpoint: float, plateaus: dict[int, float]) -> float:
    """Overnight room temperature for a setpoint, interpolated linearly (extrapolated at the ends)."""
    points = sorted(plateaus.items())
    if setpoint <= points[0][0]:
        (x0, y0), (x1, y1) = points[0], points[1]
    elif setpoint >= points[-1][0]:
        (x0, y0), (x1, y1) = points[-2], points[-1]
    else:
        (x0, y0), (x1, y1) = next((a, b) for a, b in zip(points, points[1:]) if a[0] <= setpoint <= b[0])
    return y0 + (y1 - y0) * (setpoint - x0) / (x1 - x0)


# --- Inputs -----------------------------------------------------------------


def current_inputs(conn: sqlite3.Connection, now: datetime, settings: Settings) -> tuple[dict[str, Any], list[str]]:
    """Indoor temperature and AC state from the logged readings, and reasons not to control."""
    rows = conn.execute(
        """SELECT logged_at, temperature, target_temperature, ac_on, mode, fan_level FROM readings
           WHERE logged_at > ? AND logged_at <= ? ORDER BY logged_at""",
        (iso(now - settings.measurement_window), iso(now)),
    ).fetchall()
    temperatures = [r["temperature"] for r in rows if r["temperature"] is not None]
    latest = rows[-1] if rows else None
    inputs = {
        "temperature": round(statistics.fmean(temperatures), 2) if temperatures else None,
        "actual_setpoint": latest["target_temperature"] if latest else None,
        "fan_level": latest["fan_level"] if latest else None,
    }
    if latest is None or now - parse_iso(latest["logged_at"]) > settings.max_measurement_age:
        return inputs, ["no fresh reading"]
    reasons = []
    if not temperatures:
        reasons.append("no indoor temperature")
    if latest["ac_on"] != 1 or latest["mode"] != "heat":
        reasons.append(f"heat pump is not on in heat mode (on={latest['ac_on']}, mode={latest['mode']})")
    if latest["fan_level"] in (None, "auto"):
        reasons.append(f"fan level is {latest['fan_level']!r}, needs a fixed level")
    if not isinstance(latest["target_temperature"], int):
        reasons.append(f"setpoint is {latest['target_temperature']!r}")
    if conn.execute("SELECT 1 FROM step_tests WHERE status IN ('running', 'restoring')").fetchone():
        reasons.append("step test running")
    return inputs, reasons


# --- Controller -------------------------------------------------------------


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM control_runs ORDER BY id DESC LIMIT 1").fetchone()


def last_change_at(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute(
        "SELECT run_at FROM control_runs WHERE changed = 1 OR status = 'started' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return parse_iso(row[0]) if row else None


def start(inputs: dict[str, Any], settings: Settings, reason: str) -> dict[str, Any]:
    """Bumpless start: take over the actual setpoint, with the integral set to match it."""
    setpoint = min(max(inputs["actual_setpoint"], settings.min_setpoint), settings.max_setpoint)
    controlled = inputs["temperature"]
    error = settings.target - controlled
    p_term = settings.kp * error
    return {
        "status": "started",
        "reason": reason,
        "model_offset": 0.0,
        "controlled_temperature": controlled,
        "error": error,
        "p_term": p_term,
        "integral": setpoint - p_term,
        "output": float(setpoint),
        "quantizer": 0.0,
        "setpoint": setpoint,
        "changed": 0,
    }


def step(
    last: sqlite3.Row, inputs: dict[str, Any], hours: float, changed_at: datetime | None, now: datetime, settings: Settings
) -> dict[str, Any]:
    """One controller update, hours after the previous one."""
    # Shadow model: the room as if the controller's setpoint had been in place since the last run.
    offset_target = plateau(last["setpoint"], settings.plateaus) - plateau(inputs["actual_setpoint"], settings.plateaus)
    decay = math.exp(-hours / (settings.model_tau.total_seconds() / 3600))
    offset = offset_target + (last["model_offset"] - offset_target) * decay
    controlled = inputs["temperature"] + offset

    # PI with conditional integration: don't integrate further into a limit.
    error = settings.target - controlled
    p_term = settings.kp * error
    integral = last["integral"] + settings.kp / (settings.ti.total_seconds() / 3600) * error * hours
    output = p_term + integral
    if (output > settings.max_setpoint and error > 0) or (output < settings.min_setpoint and error < 0):
        integral = last["integral"]
        output = p_term + integral
    output = min(max(output, settings.min_setpoint), settings.max_setpoint)

    # Sigma-delta quantizer with a minimum dwell time.
    setpoint = last["setpoint"]
    quantizer = last["quantizer"] + (output - setpoint) * hours
    changed = 0
    if changed_at is None or now - changed_at >= settings.min_dwell:
        if quantizer >= settings.quantizer_threshold and setpoint < settings.max_setpoint:
            setpoint, changed = setpoint + 1, 1
        elif quantizer <= -settings.quantizer_threshold and setpoint > settings.min_setpoint:
            setpoint, changed = setpoint - 1, 1
    # At a limit the quantizer cannot act, so keep it from accumulating.
    if setpoint in (settings.min_setpoint, settings.max_setpoint):
        quantizer = min(max(quantizer, -settings.quantizer_threshold), settings.quantizer_threshold)

    return {
        "status": "active",
        "reason": None,
        "model_offset": offset,
        "controlled_temperature": controlled,
        "error": error,
        "p_term": p_term,
        "integral": integral,
        "output": output,
        "quantizer": quantizer,
        "setpoint": setpoint,
        "changed": changed,
    }


def run(conn: sqlite3.Connection, now: datetime, settings: Settings = Settings()) -> dict[str, Any]:
    """Run the controller once and store the result. Returns the stored row as a dict."""
    inputs, reasons = current_inputs(conn, now, settings)
    last = last_run(conn)
    if reasons:
        record: dict[str, Any] = {"status": "inactive", "reason": "; ".join(reasons)}
    elif last is None:
        record = start(inputs, settings, "first run")
    elif last["status"] == "inactive":
        record = start(inputs, settings, f"resumed after: {last['reason']}")
    elif now - parse_iso(last["run_at"]) > settings.max_gap:
        record = start(inputs, settings, f"resumed after a gap since {last['run_at']}")
    else:
        hours = (now - parse_iso(last["run_at"])).total_seconds() / 3600
        record = step(last, inputs, hours, last_change_at(conn), now, settings)

    record = {"run_at": iso(now), "mode": MODE, **inputs, "target": settings.target, **record}
    for key in ("model_offset", "controlled_temperature", "error", "p_term", "integral", "output", "quantizer"):
        if record.get(key) is not None:
            record[key] = round(record[key], 4)
    columns = list(record)
    with conn:
        conn.execute(
            f"INSERT INTO control_runs ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
            [record[c] for c in columns],
        )
    return record


# --- Status -----------------------------------------------------------------


def _temperature_stats(values: list[float], settings: Settings) -> str:
    if not values:
        return "–"
    rms = statistics.fmean((v - settings.target) ** 2 for v in values) ** 0.5
    below = sum(v < settings.comfort_min for v in values) / len(values)
    return (f"mean {statistics.fmean(values):.2f}, range {min(values):.1f}–{max(values):.1f}, "
            f"RMS error {rms:.2f}, below {settings.comfort_min:.1f} {below:.0%} of the time")


def status_lines(conn: sqlite3.Connection, since: datetime, settings: Settings = Settings()) -> list[str]:
    rows = conn.execute("SELECT * FROM control_runs WHERE run_at >= ? ORDER BY id", (iso(since),)).fetchall()
    if not rows:
        return [f"No controller runs since {iso(since)}."]
    active = [r for r in rows if r["status"] != "inactive"]
    inactive = Counter(r["reason"] for r in rows if r["status"] == "inactive")
    lines = [
        f"Since {iso(since)}: {len(rows)} runs ({rows[0]['mode']}), {len(active)} controlling, target {settings.target:.1f} °C",
    ]
    if inactive:
        lines.append("Inactive: " + "; ".join(f"{reason} ×{n}" for reason, n in inactive.most_common()))
    if active:
        setpoints = Counter(r["setpoint"] for r in active)
        actual = Counter(r["actual_setpoint"] for r in active)
        lines += [
            "Controller setpoint: " + ", ".join(f"{k} {v / len(active):.0%}" for k, v in sorted(setpoints.items()))
            + f"; {sum(r['changed'] for r in active)} changes",
            "Actual setpoint:     " + ", ".join(f"{k} {v / len(active):.0%}" for k, v in sorted(actual.items())),
            "Room as controlled:  " + _temperature_stats([r["controlled_temperature"] for r in active], settings),
            "Room as measured:    " + _temperature_stats([r["temperature"] for r in active], settings),
        ]
    last = rows[-1]
    lines.append(
        f"Latest {last['run_at']}: {last['status']}"
        + (f" ({last['reason']})" if last["reason"] else "")
        + (f", measured {last['temperature']}, controlled {last['controlled_temperature']}, output {last['output']}, "
           f"setpoint {last['setpoint']} (actual {last['actual_setpoint']})" if last["status"] != "inactive" else "")
    )
    return lines


# --- Main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="termostat-control",
        description="PI temperature controller, in shadow mode (writes nothing to the heat pump).",
    )
    parser.add_argument("--db", type=Path, help=f"database path (default: ${config.ENV_DB} or XDG data dir)")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("run", help="run the controller once (default)")
    status_parser = commands.add_parser("status", help="summarize recent controller runs")
    status_parser.add_argument("--days", type=float, default=1.0, help="period to summarize (default: 1)")
    args = parser.parse_args(argv)

    conn = db.connect(args.db or config.database_path())
    try:
        if args.command == "status":
            print("\n".join(status_lines(conn, utc_now() - timedelta(days=args.days))))
            return 0
        record = run(conn, utc_now())
        if record["status"] == "inactive":
            print(f"Inactive: {record['reason']}")
        elif record["status"] == "started":
            print(f"Started at setpoint {record['setpoint']} ({record['reason']})")
        elif record["changed"]:
            print(f"Would change setpoint to {record['setpoint']} (actual {record['actual_setpoint']})")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
