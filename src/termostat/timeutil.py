from datetime import UTC, datetime
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_sensibo_time(value: Any) -> str | None:
    """Sensibo returns times either as a string or as {"time": ..., "secondsAgo": ...}."""
    if isinstance(value, dict):
        value = value.get("time")
    if not isinstance(value, str) or not value:
        return None
    try:
        return iso(parse_iso(value))
    except ValueError:
        return value
