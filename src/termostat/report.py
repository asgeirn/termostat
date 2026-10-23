"""Report logged history: indoor and outdoor conditions, setpoint and fan level over time."""

import argparse
import csv
import json
import os
import sqlite3
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from termostat import config, db
from termostat.timeutil import iso, parse_iso, utc_now

INTERVALS = ("raw", "15m", "hour", "day")
SPARK = "▁▂▃▄▅▆▇█"


@dataclass
class Sample:
    time: datetime
    temperature: float | None
    humidity: float | None


@dataclass
class Bucket:
    start: datetime
    indoor: list[float] = field(default_factory=list)
    humidity: list[float] = field(default_factory=list)
    outdoor: list[float] = field(default_factory=list)
    wind: list[float] = field(default_factory=list)
    cloud: list[float] = field(default_factory=list)
    setpoints: list[int] = field(default_factory=list)
    fans: list[str] = field(default_factory=list)
    on: list[int] = field(default_factory=list)
    readings: int = 0


# --- Loading ----------------------------------------------------------------


def load_readings(conn: sqlite3.Connection, device_id: str | None, since: datetime, until: datetime) -> list[sqlite3.Row]:
    query = "SELECT * FROM readings WHERE logged_at >= ? AND logged_at < ?"
    params: list[Any] = [iso(since), iso(until)]
    if device_id:
        query += " AND device_id = ?"
        params.append(device_id)
    return conn.execute(query + " ORDER BY logged_at", params).fetchall()


def load_historical(conn: sqlite3.Connection, device_id: str | None, since: datetime, until: datetime) -> list[Sample]:
    query = "SELECT time, temperature, humidity FROM historical_measurements WHERE time >= ? AND time < ?"
    params: list[Any] = [iso(since), iso(until)]
    if device_id:
        query += " AND device_id = ?"
        params.append(device_id)
    return [Sample(parse_iso(r["time"]), r["temperature"], r["humidity"]) for r in conn.execute(query + " ORDER BY time", params)]


def load_changes(conn: sqlite3.Connection, device_id: str | None, since: datetime, until: datetime) -> list[sqlite3.Row]:
    query = "SELECT * FROM ac_state_changes WHERE changed_at >= ? AND changed_at < ?"
    params: list[Any] = [iso(since), iso(until)]
    if device_id:
        query += " AND device_id = ?"
        params.append(device_id)
    return conn.execute(query + " ORDER BY changed_at", params).fetchall()


def reading_time(row: sqlite3.Row) -> datetime:
    return parse_iso(row["measured_at"] or row["logged_at"])


def indoor_samples(readings: list[sqlite3.Row], historical: list[Sample]) -> list[Sample]:
    """Merge indoor measurements, preferring Sensibo's historical data where it exists.

    Both sources measure the same sensor; historical measurements have finer resolution.
    For each UTC hour, historical samples are used if there are any, otherwise readings.
    """
    def hour(dt: datetime) -> datetime:
        return dt.replace(minute=0, second=0, microsecond=0)

    historical_hours = {hour(s.time) for s in historical}
    from_readings = [
        Sample(reading_time(r), r["temperature"], r["humidity"])
        for r in readings
        if hour(reading_time(r)) not in historical_hours
    ]
    return sorted(historical + from_readings, key=lambda s: s.time)


# --- Bucketing --------------------------------------------------------------


def bucket_start(dt: datetime, interval: str, utc: bool) -> datetime:
    local = dt.astimezone(UTC) if utc else dt.astimezone()
    match interval:
        case "raw":
            return local
        case "15m":
            return local.replace(minute=local.minute - local.minute % 15, second=0, microsecond=0)
        case "hour":
            return local.replace(minute=0, second=0, microsecond=0)
        case "day":
            return local.replace(hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(interval)


def build_buckets(readings: list[sqlite3.Row], samples: list[Sample], interval: str, utc: bool) -> list[Bucket]:
    buckets: dict[datetime, Bucket] = {}

    def get(dt: datetime) -> Bucket:
        start = bucket_start(dt, interval, utc)
        key = start.replace(tzinfo=None)  # group by local wall-clock time
        return buckets.setdefault(key, Bucket(start))

    for row in readings:
        bucket = get(parse_iso(row["logged_at"]))
        bucket.readings += 1
        if interval == "raw":
            _append(bucket.indoor, row["temperature"])
            _append(bucket.humidity, row["humidity"])
        _append(bucket.outdoor, row["outdoor_temperature"])
        _append(bucket.wind, row["outdoor_wind_speed"])
        _append(bucket.cloud, row["outdoor_cloud_area_fraction"])
        _append(bucket.setpoints, row["target_temperature"])
        _append(bucket.fans, row["fan_level"])
        _append(bucket.on, row["ac_on"])

    if interval != "raw":
        for sample in samples:
            bucket = get(sample.time)
            _append(bucket.indoor, sample.temperature)
            _append(bucket.humidity, sample.humidity)

    return [buckets[key] for key in sorted(buckets)]


def _append(values: list, value: Any) -> None:
    if value is not None:
        values.append(value)


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _range(values: list) -> tuple[Any, Any] | None:
    return (min(values), max(values)) if values else None


def _most_common(values: list) -> Any:
    return Counter(values).most_common(1)[0][0] if values else None


def bucket_record(bucket: Bucket) -> dict[str, Any]:
    indoor = _range(bucket.indoor)
    setpoints = _range(bucket.setpoints)
    return {
        "start": bucket.start.isoformat(timespec="seconds"),
        "indoor_mean": _round(_mean(bucket.indoor), 2),
        "indoor_min": indoor[0] if indoor else None,
        "indoor_max": indoor[1] if indoor else None,
        "humidity_mean": _round(_mean(bucket.humidity), 1),
        "outdoor_mean": _round(_mean(bucket.outdoor), 1),
        "wind_mean": _round(_mean(bucket.wind), 1),
        "cloud_mean": _round(_mean(bucket.cloud), 0),
        "setpoint_min": setpoints[0] if setpoints else None,
        "setpoint_max": setpoints[1] if setpoints else None,
        "fan_level": _most_common(bucket.fans),
        "fan_levels": sorted(set(bucket.fans)),
        "on_fraction": _round(_mean([float(v) for v in bucket.on]), 2),
        "readings": bucket.readings,
        "indoor_samples": len(bucket.indoor),
    }


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


# --- Summary ----------------------------------------------------------------


def summarize(
    readings: list[sqlite3.Row],
    samples: list[Sample],
    changes: list[sqlite3.Row],
    target: float | None,
) -> dict[str, Any]:
    indoor = [s.temperature for s in samples if s.temperature is not None]
    humidity = [s.humidity for s in samples if s.humidity is not None]
    outdoor = [r["outdoor_temperature"] for r in readings if r["outdoor_temperature"] is not None]
    setpoints = [r["target_temperature"] for r in readings if r["target_temperature"] is not None]
    fans = [r["fan_level"] for r in readings if r["fan_level"] is not None]
    on = [r["ac_on"] for r in readings if r["ac_on"] is not None]

    summary: dict[str, Any] = {
        "readings": len(readings),
        "indoor_samples": len(indoor),
        "indoor": _stats(indoor, 2),
        "humidity": _stats(humidity, 1),
        "outdoor": _stats(outdoor, 1),
        "setpoint_range": list(_range(setpoints) or []),
        "fan_levels": dict(Counter(fans).most_common()),
        "on_fraction": _round(_mean([float(v) for v in on]), 2),
        "ac_state_changes": dict(Counter(c["reason"] or "unknown" for c in changes).most_common()),
    }
    if target is not None and indoor:
        errors = [t - target for t in indoor]
        summary["target"] = {
            "target": target,
            "mean_error": round(statistics.fmean(errors), 2),
            "rms_error": round(statistics.fmean(e * e for e in errors) ** 0.5, 2),
            "max_below": round(min(errors), 1),
            "max_above": round(max(errors), 1),
            "within_0_2": round(sum(abs(e) <= 0.2 for e in errors) / len(errors), 3),
            "within_0_5": round(sum(abs(e) <= 0.5 for e in errors) / len(errors), 3),
        }
    return summary


def _stats(values: list[float], digits: int) -> dict[str, float] | None:
    if not values:
        return None
    return {
        "mean": round(statistics.fmean(values), digits),
        "min": min(values),
        "max": max(values),
        "stdev": round(statistics.pstdev(values), digits + 1),
    }


# --- Output -----------------------------------------------------------------


def _fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _fmt_range(low: Any, high: Any, digits: int = 1) -> str:
    if low is None:
        return "–"
    return _fmt(low, digits) if low == high else f"{_fmt(low, digits)}–{_fmt(high, digits)}"


def sparkline(values: list[float | None]) -> str:
    present = [v for v in values if v is not None]
    if not present:
        return ""
    low, high = min(present), max(present)
    if low == high:
        return "".join(" " if v is None else SPARK[len(SPARK) // 2] for v in values)
    span = high - low
    return "".join(" " if v is None else SPARK[round((v - low) / span * (len(SPARK) - 1))] for v in values)


def _summary_range(stats: dict[str, float]) -> str:
    if stats["min"] == stats["max"]:
        return f"{stats['min']:.1f}"
    return f"{stats['min']:.1f} to {stats['max']:.1f}"


def label(start: datetime, interval: str) -> str:
    match interval:
        case "day":
            return start.strftime("%Y-%m-%d %a")
        case "raw":
            return start.strftime("%Y-%m-%d %H:%M:%S")
        case _:
            return start.strftime("%Y-%m-%d %H:%M")


def render_table(
    since: datetime, until: datetime, interval: str, buckets: list[Bucket],
    summary: dict[str, Any], changes: list[sqlite3.Row] | None, utc: bool,
) -> str:
    def local(dt: datetime) -> str:
        return (dt.astimezone(UTC) if utc else dt.astimezone()).strftime("%Y-%m-%d %H:%M %Z")

    out = [f"Period: {local(since)} – {local(until)}  ({interval} intervals)", ""]

    s = summary
    if s["indoor"]:
        i = s["indoor"]
        out.append(f"Indoor:    mean {i['mean']:.2f} °C, range {_summary_range(i)}, stdev {i['stdev']:.2f}")
    if s["humidity"]:
        h = s["humidity"]
        out.append(f"Humidity:  mean {h['mean']:.1f} %, range {_summary_range(h)}")
    if s["outdoor"]:
        o = s["outdoor"]
        out.append(f"Outdoor:   mean {o['mean']:.1f} °C, range {_summary_range(o)}")
    if s["setpoint_range"]:
        out.append(f"Setpoint:  {_fmt_range(*s['setpoint_range'])} °C")
    if s["fan_levels"]:
        total = sum(s["fan_levels"].values())
        out.append("Fan:       " + ", ".join(f"{k} {v / total:.0%}" for k, v in s["fan_levels"].items()))
    if s["on_fraction"] is not None:
        out.append(f"AC on:     {s['on_fraction']:.0%} of readings")
    if s["ac_state_changes"]:
        out.append("Changes:   " + ", ".join(f"{k} ×{v}" for k, v in s["ac_state_changes"].items()))
    if t := s.get("target"):
        out.append(
            f"Target:    {t['target']:.1f} °C, mean error {t['mean_error']:+.2f}, RMS {t['rms_error']:.2f}, "
            f"range {t['max_below']:+.1f}/{t['max_above']:+.1f}, "
            f"within ±0.2 {t['within_0_2']:.0%}, within ±0.5 {t['within_0_5']:.0%}"
        )
    out.append(f"Samples:   {s['readings']} readings, {s['indoor_samples']} indoor measurements")

    records = [bucket_record(b) for b in buckets]
    if len(records) > 1:
        out += [
            "",
            f"Indoor  {sparkline([r['indoor_mean'] for r in records])}",
            f"Outdoor {sparkline([r['outdoor_mean'] for r in records])}",
        ]

    headers = ["Time", "Indoor °C", "Min–max", "RH %", "Out °C", "Wind", "Cloud %", "Setpoint", "Fan", "On"]
    rows = [
        [
            label(b.start, interval),
            _fmt(r["indoor_mean"], 2 if interval != "raw" else 1),
            _fmt_range(r["indoor_min"], r["indoor_max"]),
            _fmt(r["humidity_mean"]),
            _fmt(r["outdoor_mean"]),
            _fmt(r["wind_mean"]),
            _fmt(r["cloud_mean"], 0),
            _fmt_range(r["setpoint_min"], r["setpoint_max"], 0),
            "/".join(r["fan_levels"]) or "–",
            "–" if r["on_fraction"] is None else f"{r['on_fraction']:.0%}",
        ]
        for b, r in zip(buckets, records)
    ]
    out += ["", *_table(headers, rows)]

    if changes is not None:
        out += ["", "AC state changes:"]
        if not changes:
            out.append("  (none)")
        change_rows = [
            [
                label(parse_iso(c["changed_at"]).astimezone(UTC if utc else None), "raw"),
                c["reason"] or "–",
                ", ".join(json.loads(c["changed_properties"] or "null") or []) or "–",
                "on" if c["ac_on"] else "off",
                c["mode"] or "–",
                _fmt(c["target_temperature"]),
                c["fan_level"] or "–",
            ]
            for c in changes
        ]
        if change_rows:
            out += _table(["Time", "Reason", "Changed", "Power", "Mode", "Setpoint", "Fan"], change_rows)

    return "\n".join(out)


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]

    def line(cells: list[str]) -> str:
        return "  ".join(c.ljust(w) if i == 0 else c.rjust(w) for i, (c, w) in enumerate(zip(cells, widths))).rstrip()

    return [line(headers), line(["-" * w for w in widths]), *(line(r) for r in rows)]


CSV_FIELDS = [
    "start", "indoor_mean", "indoor_min", "indoor_max", "humidity_mean", "outdoor_mean", "wind_mean",
    "cloud_mean", "setpoint_min", "setpoint_max", "fan_level", "on_fraction", "readings", "indoor_samples",
]


def write_csv(buckets: list[Bucket]) -> None:
    writer = csv.DictWriter(sys.stdout, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for bucket in buckets:
        writer.writerow(bucket_record(bucket))


def change_record(change: sqlite3.Row) -> dict[str, Any]:
    return {
        "time": change["changed_at"],
        "reason": change["reason"],
        "changed_properties": json.loads(change["changed_properties"] or "null"),
        "on": None if change["ac_on"] is None else bool(change["ac_on"]),
        "mode": change["mode"],
        "target_temperature": change["target_temperature"],
        "fan_level": change["fan_level"],
    }


# --- Main -------------------------------------------------------------------


def parse_time_arg(value: str) -> datetime:
    """Parse a date or datetime; naive values are local time (or UTC with --utc, applied later)."""
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid date/time: {value!r} (use e.g. 2026-01-15 or 2026-01-15T06:00)") from None


def resolve_period(args: argparse.Namespace, now: datetime) -> tuple[datetime, datetime]:
    def aware(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC) if args.utc else dt.astimezone()
        return dt.astimezone(UTC)

    until = aware(args.until) if args.until else now
    if args.since:
        since = aware(args.since)
    else:
        since = until - timedelta(days=args.days)
    return since, until


def default_interval(since: datetime, until: datetime) -> str:
    span = until - since
    if span <= timedelta(hours=6):
        return "15m"
    if span <= timedelta(days=3):
        return "hour"
    return "day"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="termostat-report",
        description="Report logged history. Times are local unless --utc is given.",
    )
    period = parser.add_argument_group("period")
    period.add_argument("--days", type=float, default=1.0, help="report the last N days (default: 1)")
    period.add_argument("--since", type=parse_time_arg, help="start date/time, e.g. 2026-01-15 or 2026-01-15T06:00")
    period.add_argument("--until", type=parse_time_arg, help="end date/time (default: now)")
    parser.add_argument("--interval", choices=INTERVALS, help="aggregation interval (default: based on period)")
    parser.add_argument("--target", type=float, help="target temperature, to report error statistics")
    parser.add_argument("--changes", action="store_true", help="list AC state changes")
    parser.add_argument("--format", choices=("table", "csv", "json"), default="table")
    parser.add_argument("--utc", action="store_true", help="use UTC instead of local time")
    parser.add_argument("--device", help=f"device id (default: ${config.ENV_SENSIBO_DEVICE_ID}, or all devices)")
    parser.add_argument("--db", type=Path, help=f"database path (default: ${config.ENV_DB} or XDG data dir)")
    args = parser.parse_args(argv)

    since, until = resolve_period(args, utc_now())
    if since >= until:
        parser.error("start of period must be before the end")
    interval = args.interval or default_interval(since, until)
    device_id = args.device or os.environ.get(config.ENV_SENSIBO_DEVICE_ID, "").strip() or None

    db_path = args.db or config.database_path()
    if not db_path.exists():
        print(f"termostat-report: database not found: {db_path}", file=sys.stderr)
        return 2

    conn = db.connect(db_path)
    try:
        readings = load_readings(conn, device_id, since, until)
        historical = load_historical(conn, device_id, since, until)
        changes = load_changes(conn, device_id, since, until)
    finally:
        conn.close()

    if not readings and not historical:
        print("No data in the selected period.", file=sys.stderr)
        return 1

    samples = indoor_samples(readings, historical)
    if interval == "raw":
        summary_samples = [Sample(reading_time(r), r["temperature"], r["humidity"]) for r in readings]
    else:
        summary_samples = samples
    buckets = build_buckets(readings, samples, interval, args.utc)
    summary = summarize(readings, summary_samples, changes, args.target)

    match args.format:
        case "table":
            print(render_table(since, until, interval, buckets, summary, changes if args.changes else None, args.utc))
        case "csv":
            write_csv(buckets)
        case "json":
            json.dump(
                {
                    "since": iso(since),
                    "until": iso(until),
                    "interval": interval,
                    "device_id": device_id,
                    "summary": summary,
                    "buckets": [bucket_record(b) for b in buckets],
                    "changes": [change_record(c) for c in changes],
                },
                sys.stdout,
                indent=2,
                ensure_ascii=False,
            )
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
