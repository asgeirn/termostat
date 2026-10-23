"""List Sensibo devices and suggest environment variables for termostat."""

import os
import shlex
import sys
from typing import Any

from termostat import config
from termostat.config import ConfigError
from termostat.sensibo import SensiboClient, SensiboError

DEVICE_FIELDS = [
    "id",
    "room",
    "productModel",
    "measurements",
    "acState",
    "connectionStatus",
    "location",
    "remoteCapabilities",
]


def _location(device: dict[str, Any]) -> tuple[float, float] | None:
    location = device.get("location") or {}
    lat_lon = location.get("latLon")
    if isinstance(lat_lon, list) and len(lat_lon) == 2 and all(isinstance(v, int | float) for v in lat_lon):
        return float(lat_lon[0]), float(lat_lon[1])
    lat, lon = location.get("latitude"), location.get("longitude")
    if isinstance(lat, int | float) and isinstance(lon, int | float):
        return float(lat), float(lon)
    return None


def _heat_capabilities(device: dict[str, Any]) -> tuple[list[Any], list[str]]:
    heat = ((device.get("remoteCapabilities") or {}).get("modes") or {}).get("heat") or {}
    temperatures = ((heat.get("temperatures") or {}).get("C") or {}).get("values") or []
    return temperatures, heat.get("fanLevels") or []


def describe(device: dict[str, Any]) -> list[str]:
    room = (device.get("room") or {}).get("name") or "(unnamed)"
    measurements = device.get("measurements") or {}
    ac = device.get("acState") or {}
    alive = (device.get("connectionStatus") or {}).get("isAlive")

    lines = [
        f"{room}",
        f"  id:           {device.get('id')}",
        f"  model:        {device.get('productModel', '?')}",
        f"  connected:    {'yes' if alive else 'no' if alive is not None else '?'}",
        f"  measurements: {measurements.get('temperature', '?')} °C, {measurements.get('humidity', '?')} %RH",
        f"  AC state:     {'on' if ac.get('on') else 'off'}, mode {ac.get('mode', '?')}, "
        f"target {ac.get('targetTemperature', '?')}°{ac.get('temperatureUnit', '')}, fan {ac.get('fanLevel', '?')}",
    ]
    temperatures, fan_levels = _heat_capabilities(device)
    if temperatures:
        lines.append(f"  heat setpoint range: {min(temperatures)}–{max(temperatures)} °C")
    if fan_levels:
        lines.append(f"  heat fan levels:     {', '.join(fan_levels)}")
    if location := _location(device):
        lines.append(f"  location:     {location[0]:.4f}, {location[1]:.4f}")
    return lines


def suggestions(devices: list[dict[str, Any]]) -> list[str]:
    api_key = shlex.quote(os.environ.get(config.ENV_SENSIBO_API_KEY, "").strip())
    lines = [
        "# Save as ~/.config/termostat/env (chmod 600). The format works both as a systemd",
        "# EnvironmentFile and in a shell: set -a; . ~/.config/termostat/env; set +a",
        "",
        "# Sensibo",
        f"{config.ENV_SENSIBO_API_KEY}={api_key}",
    ]
    if len(devices) == 1:
        lines.append(f"{config.ENV_SENSIBO_DEVICE_ID}={shlex.quote(devices[0]['id'])}")
    else:
        lines.append("# Pick one of:")
        for device in devices:
            room = (device.get("room") or {}).get("name") or "(unnamed)"
            lines.append(f"# {config.ENV_SENSIBO_DEVICE_ID}={shlex.quote(device['id'])}  # {room}")

    lines += ["", "# MET Norway Locationforecast"]

    lat = os.environ.get(config.ENV_LATITUDE, "").strip()
    lon = os.environ.get(config.ENV_LONGITUDE, "").strip()
    if lat and lon:
        lines.append("# Location from current environment")
    else:
        locations = [loc for device in devices if (loc := _location(device))]
        if locations:
            lines.append("# Location from the Sensibo device; check that it is accurate")
            lat, lon = f"{locations[0][0]:.4f}", f"{locations[0][1]:.4f}"
        else:
            lines.append("# No location found; fill in from e.g. norgeskart.no (max 4 decimals) and uncomment")
            lat, lon = "", ""
    lines.append(f"{config.ENV_LATITUDE}={shlex.quote(lat)}" if lat else f"# {config.ENV_LATITUDE}=")
    lines.append(f"{config.ENV_LONGITUDE}={shlex.quote(lon)}" if lon else f"# {config.ENV_LONGITUDE}=")

    lines.append("# Optional: metres above sea level, improves temperature accuracy")
    altitude = os.environ.get(config.ENV_ALTITUDE, "").strip()
    lines.append(f"{config.ENV_ALTITUDE}={shlex.quote(altitude)}" if altitude else f"# {config.ENV_ALTITUDE}=")

    user_agent = os.environ.get(config.ENV_METNO_USER_AGENT, "").strip()
    if not user_agent:
        lines.append("# Required by the met.no terms of service: app name and contact (email or URL)")
        user_agent = "termostat/0.1 your-email@example.com"
    lines.append(f"{config.ENV_METNO_USER_AGENT}={shlex.quote(user_agent)}")

    lines += [
        "",
        "# Optional: database location",
        f"# {config.ENV_DB}={shlex.quote(str(config.database_path()))}",
    ]
    return lines


def main(argv: list[str] | None = None) -> int:
    try:
        api_key = config.sensibo_api_key()
    except ConfigError as error:
        print(f"termostat-configure: {error}", file=sys.stderr)
        return 2

    try:
        devices = SensiboClient(api_key).list_devices(DEVICE_FIELDS)
    except SensiboError as error:
        print(f"termostat-configure: {error}", file=sys.stderr)
        return 1

    if not devices:
        print("No Sensibo devices found for this API key.", file=sys.stderr)
        return 1

    # Device descriptions go to stderr, so stdout can be redirected to the environment file.
    print(f"Found {len(devices)} Sensibo device(s):\n", file=sys.stderr)
    for device in devices:
        print("\n".join(describe(device)) + "\n", file=sys.stderr)

    current = os.environ.get(config.ENV_SENSIBO_DEVICE_ID, "").strip()
    if current and current not in {d.get("id") for d in devices}:
        print(f"Warning: {config.ENV_SENSIBO_DEVICE_ID}={current} does not match any device.\n", file=sys.stderr)

    print("Suggested environment file:\n", file=sys.stderr)
    print("\n".join(suggestions(devices)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
