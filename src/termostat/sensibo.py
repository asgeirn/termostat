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

    def _request(self, method: str, path: str, body: Any = None, **params: str | int) -> Any:
        query = urlencode({**params, "apiKey": self._api_key})
        # The URL contains the API key, so never include it in error messages.
        try:
            response = http.request(method, f"{self._base_url}{path}?{query}", {"Accept": "application/json"}, body)
        except OSError as error:
            raise SensiboError(f"{method} {path} failed: {error}") from None

        if response.status != 200:
            snippet = response.body[:200].decode("utf-8", "replace")
            raise SensiboError(f"{method} {path} returned HTTP {response.status}: {snippet}")
        try:
            data = response.json()
        except json.JSONDecodeError as error:
            raise SensiboError(f"{method} {path} returned invalid JSON: {error}") from None
        if data.get("status") != "success":
            raise SensiboError(f"{method} {path} returned status {data.get('status')!r}")
        return data["result"]

    def _get(self, path: str, **params: str | int) -> Any:
        return self._request("GET", path, **params)

    def list_devices(self, fields: list[str]) -> list[dict[str, Any]]:
        return self._get("/users/me/pods", fields=",".join(fields))

    def get_device(self, device_id: str, fields: list[str]) -> dict[str, Any]:
        return self._get(f"/pods/{quote(device_id, safe='')}", fields=",".join(fields))

    def get_ac_states(self, device_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self._get(f"/pods/{quote(device_id, safe='')}/acStates", limit=limit)

    def get_historical_measurements(self, device_id: str, days: int = 1) -> Any:
        return self._get(f"/pods/{quote(device_id, safe='')}/historicalMeasurements", days=days)

    def set_ac_property(self, device_id: str, name: str, value: Any) -> dict[str, Any]:
        """Change one AC state property. Returns the resulting AC state change.

        Sensibo still sends the full state to the unit, so this also resynchronises it after
        IR remote use.
        """
        path = f"/pods/{quote(device_id, safe='')}/acStates/{quote(name, safe='')}"
        result = self._request("PATCH", path, {"newValue": value})
        if isinstance(result, dict) and result.get("status") not in (None, "Success"):
            raise SensiboError(
                f"PATCH {path} returned AC state status {result.get('status')!r}: {result.get('failureReason')}"
            )
        return result
