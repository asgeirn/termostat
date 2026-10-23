"""Configuration from environment variables."""

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


ENV_SENSIBO_API_KEY = "SENSIBO_API_KEY"
ENV_SENSIBO_DEVICE_ID = "SENSIBO_DEVICE_ID"
ENV_LATITUDE = "TERMOSTAT_LATITUDE"
ENV_LONGITUDE = "TERMOSTAT_LONGITUDE"
ENV_ALTITUDE = "TERMOSTAT_ALTITUDE"
ENV_METNO_USER_AGENT = "METNO_USER_AGENT"
ENV_DB = "TERMOSTAT_DB"


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set. Run termostat-configure for suggestions.")
    return value


def _float(name: str, value: str) -> float:
    try:
        return float(value)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {value!r}") from None


@dataclass(frozen=True)
class SensiboConfig:
    api_key: str
    device_id: str


@dataclass(frozen=True)
class WeatherConfig:
    latitude: float
    longitude: float
    altitude: int | None
    user_agent: str


def sensibo_api_key() -> str:
    return _require(ENV_SENSIBO_API_KEY)


def sensibo_config() -> SensiboConfig:
    return SensiboConfig(
        api_key=sensibo_api_key(),
        device_id=_require(ENV_SENSIBO_DEVICE_ID),
    )


def weather_config() -> WeatherConfig:
    latitude = _float(ENV_LATITUDE, _require(ENV_LATITUDE))
    longitude = _float(ENV_LONGITUDE, _require(ENV_LONGITUDE))
    if not -90 <= latitude <= 90:
        raise ConfigError(f"{ENV_LATITUDE} out of range: {latitude}")
    if not -180 <= longitude <= 180:
        raise ConfigError(f"{ENV_LONGITUDE} out of range: {longitude}")

    altitude = None
    if raw := os.environ.get(ENV_ALTITUDE, "").strip():
        altitude = round(_float(ENV_ALTITUDE, raw))

    user_agent = _require(ENV_METNO_USER_AGENT)
    return WeatherConfig(latitude, longitude, altitude, user_agent)


def database_path() -> Path:
    if raw := os.environ.get(ENV_DB, "").strip():
        return Path(raw).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME", "").strip() or "~/.local/share"
    return Path(data_home).expanduser() / "termostat" / "termostat.db"
