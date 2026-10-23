"""Client for the Sensibo REST API (see sensibo.openapi.yaml)."""

import json
from typing import Any
from urllib.parse import quote, urlencode

from termostat import http

BASE_URL = "https://home.sensibo.com/api/v2"


class SensiboError(Exception):
    pass


class SensiboClient:
    def __init__(self, api_key: str, base_url: str = BASE_URL) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")

    def _get(self, path: str, **params: str | int) -> Any:
        query = urlencode({**params, "apiKey": self._api_key})
        # The URL contains the API key, so never include it in error messages.
        try:
            response = http.get(f"{self._base_url}{path}?{query}", {"Accept": "application/json"})
        except OSError as error:
            raise SensiboError(f"GET {path} failed: {error}") from None

        if response.status != 200:
            snippet = response.body[:200].decode("utf-8", "replace")
            raise SensiboError(f"GET {path} returned HTTP {response.status}: {snippet}")
        try:
            data = response.json()
        except json.JSONDecodeError as error:
            raise SensiboError(f"GET {path} returned invalid JSON: {error}") from None
        if data.get("status") != "success":
            raise SensiboError(f"GET {path} returned status {data.get('status')!r}")
        return data["result"]

    def list_devices(self, fields: list[str]) -> list[dict[str, Any]]:
        return self._get("/users/me/pods", fields=",".join(fields))

    def get_device(self, device_id: str, fields: list[str]) -> dict[str, Any]:
        return self._get(f"/pods/{quote(device_id, safe='')}", fields=",".join(fields))

    def get_ac_states(self, device_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self._get(f"/pods/{quote(device_id, safe='')}/acStates", limit=limit)

    def get_historical_measurements(self, device_id: str, days: int = 1) -> Any:
        return self._get(f"/pods/{quote(device_id, safe='')}/historicalMeasurements", days=days)
