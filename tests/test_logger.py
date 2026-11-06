import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from termostat import db, logger, metno
from termostat.config import WeatherConfig
from termostat.metno import ForecastResponse, MetnoError
from termostat.sensibo import SensiboError

NOW = datetime(2026, 1, 15, 12, 30, tzinfo=UTC)
WEATHER = WeatherConfig(latitude=59.91387, longitude=10.75225, altitude=90, user_agent="test/1.0 test@example.com")


def forecast_body() -> dict:
    def step(hour: int, temperature: float, direction: float) -> dict:
        return {
            "time": f"2026-01-15T{hour:02d}:00:00Z",
            "data": {
                "instant": {
                    "details": {
                        "air_temperature": temperature,
                        "relative_humidity": 80.0,
                        "wind_speed": 4.0,
                        "wind_from_direction": direction,
                        "cloud_area_fraction": 50.0,
                        "air_pressure_at_sea_level": 1010.0,
                    }
                },
                "next_1_hours": {
                    "summary": {"symbol_code": "cloudy"},
                    "details": {"precipitation_amount": 0.2},
                },
            },
        }

    return {
        "properties": {
            "meta": {"updated_at": "2026-01-15T11:45:00Z"},
            "timeseries": [step(12, -4.0, 350.0), step(13, -6.0, 10.0), step(14, -7.0, 20.0)],
        }
    }


DEVICE = {
    "id": "abc123",
    "measurements": {"temperature": 21.3, "humidity": 35.2, "time": {"time": "2026-01-15T12:29:10.123456Z", "secondsAgo": 50}},
    "acState": {"on": True, "mode": "heat", "fanLevel": "medium", "targetTemperature": 22, "temperatureUnit": "C"},
    "connectionStatus": {"isAlive": True},
}

AC_STATES = [
    {
        "id": "state-2",
        "time": {"time": "2026-01-15T10:00:00Z", "secondsAgo": 9000},
        "status": "Success",
        "reason": "UserRequest",
        "changedProperties": ["targetTemperature"],
        "acState": {"on": True, "mode": "heat", "fanLevel": "medium", "targetTemperature": 22},
    },
    {
        "id": "state-1",
        "time": {"time": "2026-01-14T22:00:00Z", "secondsAgo": 52200},
        "status": "Success",
        "reason": "ExternalIrCommand",
        "changedProperties": ["fanLevel"],
        "acState": {"on": True, "mode": "heat", "fanLevel": "medium", "targetTemperature": 21},
    },
]


class FakeSensibo:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def get_device(self, device_id, fields):
        if self.fail:
            raise SensiboError("boom")
        return DEVICE

    def get_ac_states(self, device_id, limit=20):
        if self.fail:
            raise SensiboError("boom")
        return AC_STATES


class InterpolateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.steps = metno.parse_timeseries(forecast_body())

    def values(self, at: datetime) -> dict:
        values = metno.interpolate(self.steps, at)
        assert values is not None
        return values

    def test_between_steps(self) -> None:
        values = self.values(NOW)
        self.assertAlmostEqual(values["air_temperature"], -5.0)
        self.assertEqual(values["precipitation_1h"], 0.2)

    def test_wind_direction_wraps(self) -> None:
        values = self.values(NOW)
        self.assertAlmostEqual(min(values["wind_from_direction"], 360 - values["wind_from_direction"]), 0.0, places=6)

    def test_exact_last_step(self) -> None:
        values = self.values(datetime(2026, 1, 15, 14, tzinfo=UTC))
        self.assertEqual(values["air_temperature"], -7.0)

    def test_outside_forecast(self) -> None:
        self.assertIsNone(metno.interpolate(self.steps, datetime(2026, 1, 15, 15, tzinfo=UTC)))
        self.assertIsNone(metno.interpolate(self.steps, datetime(2026, 1, 15, 10, tzinfo=UTC)))
        self.assertIsNotNone(metno.interpolate(self.steps, datetime(2026, 1, 15, 11, 30, tzinfo=UTC)))

    def test_location_params_truncated(self) -> None:
        self.assertEqual(metno.location_params(WEATHER), {"lat": "59.9139", "lon": "10.7523", "altitude": "90"})


class RunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def fetch(self, status=200, expires=NOW + timedelta(minutes=30)):
        body = forecast_body() if status == 200 else None
        response = ForecastResponse(status, expires, "Thu, 15 Jan 2026 11:45:00 GMT", body)
        return mock.patch.object(metno, "fetch_forecast", return_value=response)

    def test_logs_reading_with_outdoor_conditions(self) -> None:
        with self.fetch() as fetch:
            failures = logger.run(self.conn, FakeSensibo(), "abc123", WEATHER, NOW)
        self.assertEqual(failures, 0)
        fetch.assert_called_once_with(WEATHER, None)

        row = self.conn.execute("SELECT * FROM readings").fetchone()
        self.assertEqual(row["temperature"], 21.3)
        self.assertEqual(row["fan_level"], "medium")
        self.assertEqual(row["target_temperature"], 22)
        self.assertEqual(row["ac_on"], 1)
        self.assertEqual(row["measured_at"], "2026-01-15T12:29:10+00:00")
        self.assertAlmostEqual(row["outdoor_temperature"], -5.0)
        self.assertIsNotNone(row["forecast_id"])

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM forecast_steps").fetchone()[0], 3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ac_state_changes").fetchone()[0], 2)

    def test_respects_expires_and_if_modified_since(self) -> None:
        with self.fetch():
            logger.run(self.conn, FakeSensibo(), "abc123", WEATHER, NOW)

        with self.fetch() as fetch:
            logger.run(self.conn, FakeSensibo(), "abc123", WEATHER, NOW + timedelta(minutes=10))
        fetch.assert_not_called()

        later = NOW + timedelta(minutes=40)
        with self.fetch(status=304, expires=later + timedelta(minutes=30)) as fetch:
            failures = logger.run(self.conn, FakeSensibo(), "abc123", WEATHER, later)
        self.assertEqual(failures, 0)
        fetch.assert_called_once_with(WEATHER, "Thu, 15 Jan 2026 11:45:00 GMT")

        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0], 1)
        forecast_ids = {r[0] for r in self.conn.execute("SELECT forecast_id FROM readings")}
        self.assertEqual(len(forecast_ids), 1)
        # AC state changes are deduplicated across runs.
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ac_state_changes").fetchone()[0], 2)

    def test_forecast_failure_uses_cached_forecast(self) -> None:
        with self.fetch():
            logger.run(self.conn, FakeSensibo(), "abc123", WEATHER, NOW)

        later = NOW + timedelta(minutes=40)
        with mock.patch.object(metno, "fetch_forecast", side_effect=MetnoError("down")):
            failures = logger.run(self.conn, FakeSensibo(), "abc123", WEATHER, later)
        self.assertEqual(failures, 1)
        row = self.conn.execute("SELECT * FROM readings ORDER BY id DESC").fetchone()
        self.assertIsNotNone(row["outdoor_temperature"])

    def test_sensibo_failure_is_reported(self) -> None:
        with self.fetch():
            failures = logger.run(self.conn, FakeSensibo(fail=True), "abc123", WEATHER, NOW)
        self.assertEqual(failures, 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
