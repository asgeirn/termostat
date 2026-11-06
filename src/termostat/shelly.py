"""Client for a Shelly Gen2+ power meter (Shelly Plug PM Gen3) over its local RPC API.

The Plug PM Gen3 is metering only: it has no relay, so there is nothing to switch.

API: https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/PM1
GET http://<host>/rpc/PM1.GetStatus?id=0 returns (real device, S3PL-30116EU, firmware 2.1.1):
    {"id": 0, "voltage": 231.1, "current": 0.374, "apower": 82.1, "freq": 49.9,
     "aenergy": {"total": 20.411, "by_minute": [1275.698, 1488.314, 1488.314], "minute_ts": 1793945100},
     "ret_aenergy": {"total": 0.0, "by_minute": [0.0, 0.0, 0.0], "minute_ts": 1793945100}}
aenergy.total is in Wh, by_minute in mWh. The device can serve HTTPS with a user-installed
certificate; SHELLY_CA_FILE then names the CA to trust.
"""

import json
import sqlite3
import ssl
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from termostat import http
from termostat.config import ShellyConfig
from termostat.timeutil import iso

# Gen2+ devices use digest authentication with this fixed user name when a password is set.
USERNAME = "admin"
TIMEOUT = 10.0
METER_ID = 0


class ShellyError(Exception):
    pass


class ShellyClient:
    def __init__(self, config: ShellyConfig, timeout: float = TIMEOUT) -> None:
        scheme = "https" if config.host.startswith("https://") else "http"
        host = config.host.removeprefix("http://").removeprefix("https://").rstrip("/")
        self.host = host
        self._base_url = f"{scheme}://{host}/rpc"
        self._auth = (USERNAME, config.password) if config.password else None
        self._timeout = timeout
        self._context = ssl.create_default_context(cafile=config.ca_file) if config.ca_file else None

    def _rpc(self, method: str, **params: str | int) -> Any:
        url = f"{self._base_url}/{method}" + (f"?{urlencode(params)}" if params else "")
        try:
            response = http.get(url, {"Accept": "application/json"}, self._timeout, self._auth, self._context)
        except OSError as error:
            raise ShellyError(f"{method} failed: {error}") from None
        if response.status == 401:
            raise ShellyError(f"{method} requires authentication; check SHELLY_PASSWORD")
        if response.status != 200:
            snippet = response.body[:200].decode("utf-8", "replace")
            raise ShellyError(f"{method} returned HTTP {response.status}: {snippet}")
        try:
            return response.json()
        except json.JSONDecodeError as error:
            raise ShellyError(f"{method} returned invalid JSON: {error}") from None

    def get_device_info(self) -> dict[str, Any]:
        return self._rpc("Shelly.GetDeviceInfo")

    def get_meter_status(self) -> dict[str, Any]:
        return self._rpc("PM1.GetStatus", id=METER_ID)


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def store_power_reading(conn: sqlite3.Connection, host: str, status: dict[str, Any], now: datetime) -> None:
    energy = status.get("aenergy") or {}
    by_minute = energy.get("by_minute")
    conn.execute(
        """INSERT INTO power_readings (
               logged_at, host, apower, voltage, current, freq, aenergy_total,
               aenergy_by_minute, aenergy_minute_ts, errors, raw)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            iso(now),
            host,
            _number(status.get("apower")),
            _number(status.get("voltage")),
            _number(status.get("current")),
            _number(status.get("freq")),
            _number(energy.get("total")),
            json.dumps(by_minute) if isinstance(by_minute, list) else None,
            energy.get("minute_ts") if isinstance(energy.get("minute_ts"), int) else None,
            json.dumps(status["errors"]) if status.get("errors") else None,
            json.dumps(status, separators=(",", ":")),
        ),
    )
