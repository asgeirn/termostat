import contextlib
import csv
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from termostat import backfill, db, logger, report

START = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)


def device(temperature: float, setpoint: int, fan: str) -> dict:
    return {
        "measurements": {"temperature": temperature, "humidity": 35.0},
        "acState": {"on": True, "mode": "heat", "fanLevel": fan, "targetTemperature": setpoint},
        "connectionStatus": {"isAlive": True},
    }


class ReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        conn = db.connect(self.path)
        with conn:
            # Readings every 10 minutes for 6 hours: 21.0 for the first 3 hours, then 22.0.
            for i in range(36):
                now = START + timedelta(minutes=10 * i)
                temperature = 21.0 if i < 18 else 22.0
                outdoor: dict[str, float | None] = {"air_temperature": -5.0, "wind_speed": 3.0, "cloud_area_fraction": 50.0}
                logger.store_reading(conn, "abc", device(temperature, 22 if i < 18 else 23, "medium"), now, None, outdoor)
            # Historical measurements for hour 0 only, at a different value, to check they take precedence.
            backfill.store_measurements(
                conn, "abc",
                {(START + timedelta(minutes=m)).isoformat(): {"temperature": 20.5, "humidity": 40.0} for m in range(0, 60, 2)},
            )
            logger.store_ac_state_changes(
                conn, "abc",
                [{"id": "c1", "time": (START + timedelta(hours=3)).isoformat(), "reason": "UserRequest",
                  "changedProperties": ["targetTemperature"], "acState": {"on": True, "mode": "heat", "targetTemperature": 23}}],
                START,
            )
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_report(self, *args: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = report.main([
                "--db", str(self.path), "--device", "abc", "--utc",
                "--since", "2026-01-15T00:00", "--until", "2026-01-15T06:00", *args,
            ])
        self.assertEqual(code, 0)
        return out.getvalue()

    def test_json_hourly(self) -> None:
        data = json.loads(self.run_report("--format", "json", "--interval", "hour", "--target", "21.5"))
        buckets = data["buckets"]
        self.assertEqual(len(buckets), 6)
        # Hour 0 uses historical measurements, the rest use readings.
        self.assertEqual(buckets[0]["indoor_mean"], 20.5)
        self.assertEqual(buckets[0]["humidity_mean"], 40.0)
        self.assertEqual(buckets[1]["indoor_mean"], 21.0)
        self.assertEqual(buckets[5]["indoor_mean"], 22.0)
        self.assertEqual((buckets[3]["setpoint_min"], buckets[3]["setpoint_max"]), (23, 23))
        self.assertEqual(buckets[0]["outdoor_mean"], -5.0)
        self.assertEqual(buckets[0]["fan_level"], "medium")

        summary = data["summary"]
        self.assertEqual(summary["readings"], 36)
        self.assertEqual(summary["indoor_samples"], 30 + 30)  # 30 historical + 5 hours x 6 readings
        self.assertEqual(summary["setpoint_range"], [22, 23])
        self.assertEqual(summary["ac_state_changes"], {"UserRequest": 1})
        target = summary["target"]
        self.assertEqual(target["max_below"], -1.0)
        self.assertEqual(target["max_above"], 0.5)
        self.assertEqual(target["within_0_2"], 0.0)
        self.assertEqual(target["within_0_5"], 0.5)

    def test_raw_uses_readings_only(self) -> None:
        data = json.loads(self.run_report("--format", "json", "--interval", "raw"))
        self.assertEqual(len(data["buckets"]), 36)
        self.assertEqual(data["buckets"][0]["indoor_mean"], 21.0)
        self.assertEqual(data["summary"]["indoor_samples"], 36)

    def test_csv(self) -> None:
        rows = list(csv.DictReader(io.StringIO(self.run_report("--format", "csv", "--interval", "hour"))))
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["start"], "2026-01-15T00:00:00+00:00")
        self.assertEqual(rows[5]["indoor_mean"], "22.0")

    def test_table(self) -> None:
        text = self.run_report("--changes", "--target", "21.5")
        self.assertIn("(15m intervals)", text)
        self.assertIn("Setpoint:  22–23 °C", text)
        self.assertIn("Target:    21.5 °C", text)
        self.assertIn("UserRequest", text)
        self.assertIn("2026-01-15 05:45", text)

    def test_default_interval(self) -> None:
        self.assertEqual(report.default_interval(START, START + timedelta(hours=6)), "15m")
        self.assertEqual(report.default_interval(START, START + timedelta(days=1)), "hour")
        self.assertEqual(report.default_interval(START, START + timedelta(days=7)), "day")

    def test_sparkline(self) -> None:
        self.assertEqual(report.sparkline([1.0, None, 2.0]), "▁ █")
        self.assertEqual(report.sparkline([1.0, 1.0]), "▅▅")


if __name__ == "__main__":
    unittest.main()
