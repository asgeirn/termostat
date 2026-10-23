"""Client for the MET Norway Locationforecast 2.0 API.

Terms of service: https://api.met.no/doc/TermsOfService
- Identify the application with a User-Agent including contact information.
- Use at most 4 decimals for coordinates.
- Honour Expires and use If-Modified-Since.
"""

import json
import math
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

from termostat import http
from termostat.config import WeatherConfig
from termostat.timeutil import parse_iso

FORECAST_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"

INSTANT_FIELDS = (
    "air_temperature",
    "relative_humidity",
    "wind_speed",
    "wind_from_direction",
    "cloud_area_fraction",
    "air_pressure_at_sea_level",
)


class MetnoError(Exception):
    pass


@dataclass
class ForecastResponse:
    status: int
    expires: datetime | None
    last_modified: str | None
    body: dict[str, Any] | None


@dataclass
class ForecastStep:
    time: datetime
    instant: dict[str, float | None]
    precipitation_1h: float | None
    symbol_1h: str | None


def location_params(config: WeatherConfig) -> dict[str, str]:
    params = {"lat": str(round(config.latitude, 4)), "lon": str(round(config.longitude, 4))}
    if config.altitude is not None:
        params["altitude"] = str(config.altitude)
    return params


def fetch_forecast(config: WeatherConfig, if_modified_since: str | None = None) -> ForecastResponse:
    headers = {"User-Agent": config.user_agent, "Accept": "application/json"}
    if if_modified_since:
        headers["If-Modified-Since"] = if_modified_since

    try:
        response = http.get(f"{FORECAST_URL}?{urlencode(location_params(config))}", headers)
    except OSError as error:
        raise MetnoError(f"Forecast request failed: {error}") from None

    expires = None
    if raw_expires := response.headers.get("Expires"):
        try:
            expires = parsedate_to_datetime(raw_expires)
        except (TypeError, ValueError):
            pass
    last_modified = response.headers.get("Last-Modified")

    if response.status == 304:
        return ForecastResponse(304, expires, last_modified, None)
    # 203 means the API version is deprecated, but the content is still valid.
    if response.status not in (200, 203):
        snippet = response.body[:200].decode("utf-8", "replace")
        raise MetnoError(f"Forecast request returned HTTP {response.status}: {snippet}")
    try:
        body = response.json()
    except json.JSONDecodeError as error:
        raise MetnoError(f"Forecast response is not valid JSON: {error}") from None
    return ForecastResponse(response.status, expires, last_modified, body)


def parse_timeseries(body: dict[str, Any]) -> list[ForecastStep]:
    steps = []
    for entry in body["properties"]["timeseries"]:
        data = entry["data"]
        details = data.get("instant", {}).get("details", {})
        next_1h = data.get("next_1_hours", {})
        steps.append(
            ForecastStep(
                time=parse_iso(entry["time"]),
                instant={field: details.get(field) for field in INSTANT_FIELDS},
                precipitation_1h=next_1h.get("details", {}).get("precipitation_amount"),
                symbol_1h=next_1h.get("summary", {}).get("symbol_code"),
            )
        )
    steps.sort(key=lambda step: step.time)
    return steps


def interpolate(steps: list[ForecastStep], at: datetime) -> dict[str, float | None] | None:
    """Estimate conditions at a point in time from the forecast steps.

    Values are linearly interpolated between the surrounding steps; wind direction is
    interpolated on the circle. Returns None if the forecast does not cover the time.
    """
    if not steps:
        return None
    if at < steps[0].time:
        # Forecasts start at the current hour, so allow a small gap before the first step.
        if (steps[0].time - at).total_seconds() > 3600:
            return None
        return _values(steps[0])
    for before, after in zip(steps, steps[1:]):
        if before.time <= at <= after.time:
            span = (after.time - before.time).total_seconds()
            fraction = (at - before.time).total_seconds() / span if span else 0.0
            return _interpolate_steps(before, after, fraction)
    if at == steps[-1].time:
        return _values(steps[-1])
    return None


def _values(step: ForecastStep) -> dict[str, float | None]:
    return {**step.instant, "precipitation_1h": step.precipitation_1h}


def _interpolate_steps(before: ForecastStep, after: ForecastStep, fraction: float) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for field in INSTANT_FIELDS:
        a, b = before.instant.get(field), after.instant.get(field)
        if a is None or b is None:
            result[field] = a if fraction < 0.5 else b
        elif field == "wind_from_direction":
            result[field] = _interpolate_angle(a, b, fraction)
        else:
            result[field] = a + (b - a) * fraction
    # Precipitation is an amount for the hour following each step, so take the step we're in.
    result["precipitation_1h"] = before.precipitation_1h
    return result


def _interpolate_angle(a: float, b: float, fraction: float) -> float:
    x = (1 - fraction) * math.cos(math.radians(a)) + fraction * math.cos(math.radians(b))
    y = (1 - fraction) * math.sin(math.radians(a)) + fraction * math.sin(math.radians(b))
    return math.degrees(math.atan2(y, x)) % 360
