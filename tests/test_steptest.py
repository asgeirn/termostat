import json
import math
import os
import tempfile
import time
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from termostat import db, steptest
from termostat.sensibo import SensiboError
from termostat.timeutil import iso

# Local midnight starting the night of 2026-11-02 (CET, UTC+1).
MIDNIGHT = datetime(2026, 11, 1, 23, 0, tzinfo=UTC)
DECIDE = MIDNIGHT + timedelta(minutes=1)
RESTORE = MIDNIGHT + timedelta(hours=5, minutes=31)


class FakeSensibo:
    def __init__(self) -> None:
        self.ac = {"on": True, "mode": "heat", "fanLevel": "medium_low", "targetTemperature": 20, "swing": "rangeFull"}
        self.temperature = 20.8
        self.now = DECIDE
        self.writes: list[int] = []
        self.fail_writes = 0
        self.fail_reads = False

    def get_device(self, device_id, fields):
        if self.fail_reads:
            raise SensiboError("read boom")
        return {
            "id": device_id,
            "measurements": {"temperature": self.temperature, "humidity": 70.0, "time": {"time": iso(self.now - timedelta(seconds=40))}},
            "acState": dict(self.ac),
            "connectionStatus": {"isAlive": True},
        }

    def set_ac_property(self, device_id, name, value):
        if self.fail_writes:
            self.fail_writes -= 1
            raise SensiboError("write boom")
        self.ac[name] = value
        self.writes.append(value)
        return {"id": f"change-{len(self.writes)}", "status": "Success", "reason": "UserRequest", "acState": dict(self.ac)}

    def get_historical_measurements(self, device_id, days=1):
        raise SensiboError("history not available in tests")


class StepTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/Oslo"
        time.tzset()
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")
        self.sensibo = FakeSensibo()
        self.add_forecast()
        self.add_readings(MIDNIGHT - timedelta(hours=2), MIDNIGHT + timedelta(minutes=1), lambda t: 20.8)
        self.conn.execute(
            "INSERT INTO ac_state_changes (id, device_id, changed_at, first_seen_at) VALUES ('c1', 'dev', ?, ?)",
            (iso(MIDNIGHT - timedelta(hours=9)), iso(MIDNIGHT - timedelta(hours=9))),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()
        if self.old_tz is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = self.old_tz
        time.tzset()

    def add_forecast(self, wind: float = 3.0, temperatures: list[float] | None = None) -> None:
        fetched = MIDNIGHT - timedelta(minutes=25)
        forecast_id = self.conn.execute(
            "INSERT INTO forecasts (fetched_at, latitude, longitude) VALUES (?, 59.7, 10.3)", (iso(fetched),)
        ).lastrowid
        temperatures = temperatures or [6.0, 5.8, 5.5, 5.2, 5.0, 4.9, 4.8, 4.8]
        for hour, temperature in enumerate(temperatures):
            self.conn.execute(
                """INSERT INTO forecast_steps (forecast_id, time, air_temperature, wind_speed, cloud_area_fraction, precipitation_1h)
                   VALUES (?, ?, ?, ?, 90, 0)""",
                (forecast_id, iso(MIDNIGHT - timedelta(hours=1) + timedelta(hours=hour)), temperature, wind),
            )
        self.conn.commit()

    def add_readings(self, since: datetime, until: datetime, temperature) -> None:
        t = since
        while t <= until:
            self.conn.execute(
                """INSERT INTO readings (logged_at, device_id, measured_at, temperature, outdoor_temperature)
                   VALUES (?, 'dev', ?, ?, 5.5)""",
                (iso(t), iso(t - timedelta(seconds=45)), round(temperature(t), 1)),
            )
            t += timedelta(minutes=5)
        self.conn.commit()

    def run_at(self, now: datetime) -> int:
        self.sensibo.now = now
        return steptest.run(self.conn, self.sensibo, "dev", now)

    def test(self):
        return self.conn.execute("SELECT * FROM step_tests ORDER BY id DESC").fetchone()

    def events(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT event FROM step_test_events ORDER BY id")]


class DecisionTest(StepTestCase):
    def test_outside_window_does_nothing(self) -> None:
        self.assertEqual(self.run_at(MIDNIGHT - timedelta(minutes=4)), 0)
        self.assertEqual(self.run_at(MIDNIGHT + timedelta(minutes=31)), 0)
        self.assertIsNone(self.test())

    def test_decision_night_uses_local_time(self) -> None:
        settings = steptest.Settings()
        self.assertEqual(steptest.decision_night(DECIDE, settings), date(2026, 11, 2))
        self.assertIsNone(steptest.decision_night(MIDNIGHT - timedelta(minutes=1), settings))
        # Summer time: midnight is 22:00 UTC.
        self.assertEqual(steptest.decision_night(datetime(2026, 6, 1, 22, 10, tzinfo=UTC), settings), date(2026, 6, 2))

    def test_skips_on_wind_and_records_why(self) -> None:
        self.add_forecast(wind=12.0)
        self.run_at(DECIDE)
        row = self.test()
        self.assertEqual((row["decision"], row["status"], row["night"]), ("skip", "skipped", "2026-11-02"))
        self.assertIn("wind up to 12.0 m/s during the night", json.loads(row["reasons"]))
        self.assertEqual(json.loads(row["checks"])["forecast"]["wind_max"], 12.0)
        self.assertEqual(self.sensibo.writes, [])
        # Decided once per night.
        self.run_at(DECIDE + timedelta(minutes=5))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM step_tests").fetchone()[0], 1)

    def test_skips_on_unstable_outdoor_temperature(self) -> None:
        self.add_forecast(temperatures=[6.0, 5.0, 4.0, 3.0, 2.0, 1.0, 0.0, -1.0])
        self.run_at(DECIDE)
        self.assertTrue(any("outdoor temperature varies" in r for r in json.loads(self.test()["reasons"])))

    def test_skips_after_recent_manual_change(self) -> None:
        self.conn.execute(
            "INSERT INTO ac_state_changes (id, device_id, changed_at, first_seen_at) VALUES ('c2', 'dev', ?, ?)",
            (iso(MIDNIGHT - timedelta(hours=1)), iso(MIDNIGHT - timedelta(hours=1))),
        )
        self.run_at(DECIDE)
        self.assertEqual(self.test()["decision"], "skip")

    def test_skips_when_room_unsettled(self) -> None:
        self.conn.execute("UPDATE readings SET temperature = 21.4 WHERE logged_at = ?", (iso(MIDNIGHT - timedelta(minutes=30)),))
        self.run_at(DECIDE)
        self.assertTrue(any("indoor temperature varied" in r for r in json.loads(self.test()["reasons"])))

    def test_skips_with_auto_fan(self) -> None:
        self.sensibo.ac["fanLevel"] = "auto"
        self.run_at(DECIDE)
        self.assertEqual(self.test()["decision"], "skip")
        self.assertEqual(self.sensibo.writes, [])

    def test_kind_order_and_completion(self) -> None:
        settings = steptest.Settings()
        self.assertEqual(steptest.choose_kind(self.conn, settings), "heat")
        self.conn.execute(
            "INSERT INTO step_tests (night, device_id, decided_at, decision, kind, status) VALUES ('2026-10-01', 'dev', 'x', 'run', 'heat', 'completed')"
        )
        self.assertEqual(steptest.choose_kind(self.conn, settings), "cool")
        self.conn.execute(
            """INSERT INTO step_tests (night, device_id, decided_at, decision, kind, status, end_reason)
               VALUES ('2026-10-02', 'dev', 'x', 'run', 'cool', 'aborted', 'limit')"""
        )
        self.assertIsNone(steptest.choose_kind(self.conn, settings))


def heat_response(t: datetime) -> float:
    hours = max(0.0, (t - DECIDE).total_seconds() / 3600 - 0.25)
    return 20.8 + 1.6 * (1 - math.exp(-hours / 3.0))


class RunTest(StepTestCase):
    def test_full_heat_test(self) -> None:
        self.assertEqual(self.run_at(DECIDE), 0)
        row = self.test()
        self.assertEqual((row["decision"], row["status"], row["kind"]), ("run", "running", "heat"))
        self.assertEqual((row["baseline_target_temperature"], row["step_target_temperature"]), (20, 22))
        self.assertEqual(row["planned_end_at"], "2026-11-02T04:30:00+00:00")
        self.assertEqual(self.sensibo.writes, [22])

        self.add_readings(DECIDE + timedelta(minutes=5), RESTORE, heat_response)
        self.sensibo.temperature = 22.0
        self.assertEqual(self.run_at(MIDNIGHT + timedelta(hours=3)), 0)
        self.assertEqual(self.test()["status"], "running")

        self.assertEqual(self.run_at(RESTORE), 0)
        row = self.test()
        self.assertEqual((row["status"], row["end_reason"]), ("completed", "completed"))
        self.assertEqual(self.sensibo.writes, [22, 20])
        self.assertEqual(self.events(), ["decision", "step_written", "end_completed", "restored", "analyzed"])

        result = json.loads(row["result"])
        self.assertEqual(result["source"], "readings")
        self.assertEqual(result["pre_step_level"], 20.8)
        self.assertAlmostEqual(result["fit"]["tau_hours"], 3.0, delta=0.6)
        self.assertAlmostEqual(result["gain"], 0.8, delta=0.08)

        # Nothing more happens, and the next night picks the cool test.
        self.run_at(RESTORE + timedelta(minutes=5))
        self.assertEqual(self.sensibo.writes, [22, 20])
        self.assertEqual(steptest.choose_kind(self.conn, steptest.Settings()), "cool")

    def test_manual_change_aborts_without_restoring(self) -> None:
        self.run_at(DECIDE)
        self.sensibo.ac["targetTemperature"] = 21
        self.run_at(DECIDE + timedelta(hours=1))
        row = self.test()
        self.assertEqual((row["status"], row["end_reason"]), ("aborted", "override"))
        self.assertEqual(self.sensibo.writes, [22])
        self.assertIsNone(row["restored_at"])

    def test_manual_change_back_to_baseline_is_an_override(self) -> None:
        self.run_at(DECIDE)
        self.sensibo.ac["targetTemperature"] = 20
        self.run_at(DECIDE + timedelta(hours=1))
        self.assertEqual(self.test()["end_reason"], "override")

    def test_cool_test_restores_early_at_limit(self) -> None:
        self.conn.execute(
            "INSERT INTO step_tests (night, device_id, decided_at, decision, kind, status) VALUES ('2026-10-01', 'dev', 'x', 'run', 'heat', 'completed')"
        )
        self.run_at(DECIDE)
        self.assertEqual(self.sensibo.writes, [18])
        self.sensibo.temperature = 18.9
        self.run_at(DECIDE + timedelta(hours=4))
        row = self.test()
        self.assertEqual((row["kind"], row["status"], row["end_reason"]), ("cool", "aborted", "limit"))
        self.assertEqual(self.sensibo.writes, [18, 20])

    def test_failed_restore_is_retried(self) -> None:
        self.run_at(DECIDE)
        self.sensibo.fail_writes = 2
        self.assertEqual(self.run_at(RESTORE), 1)
        self.assertEqual(self.test()["status"], "restoring")
        self.assertEqual(self.run_at(RESTORE + timedelta(minutes=5)), 1)
        self.assertEqual(self.run_at(RESTORE + timedelta(minutes=10)), 0)
        row = self.test()
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["restored_at"], iso(RESTORE + timedelta(minutes=10)))
        self.assertEqual(self.events().count("restore_failed"), 2)

    def test_device_read_failure_still_restores(self) -> None:
        self.run_at(DECIDE)
        self.sensibo.fail_reads = True
        self.assertEqual(self.run_at(RESTORE), 0)
        self.assertEqual(self.test()["status"], "completed")
        self.assertIn("device_error", self.events())

    def test_failed_start_restores_baseline(self) -> None:
        self.sensibo.fail_writes = 1
        self.assertEqual(self.run_at(DECIDE), 1)
        row = self.test()
        self.assertEqual((row["status"], row["end_reason"]), ("aborted", "start_failed"))
        self.assertEqual(self.sensibo.writes, [20])

    def test_restore_that_errored_but_applied_is_detected(self) -> None:
        # Both the step and the restore report an error, but the unit ends up at the baseline.
        def write(device_id, name, value):
            self.sensibo.ac[name] = 20
            raise SensiboError("timeout")

        self.sensibo.set_ac_property = write
        self.run_at(DECIDE)
        self.assertEqual(self.test()["status"], "restoring")
        self.run_at(DECIDE + timedelta(minutes=5))
        self.assertEqual(self.test()["status"], "aborted")
        self.assertIn("already_restored", self.events())


class FitTest(unittest.TestCase):
    def test_recovers_parameters(self) -> None:
        points = [(i * 0.025, 1.5 * (1 - math.exp(-(i * 0.025 - 0.2) / 2.5)) if i * 0.025 > 0.2 else 0.0) for i in range(220)]
        fit = steptest.fit_fopdt(points)
        assert fit is not None
        self.assertAlmostEqual(fit["delta_final"], 1.5, delta=0.02)
        self.assertAlmostEqual(fit["tau_hours"], 2.5, delta=0.05)
        self.assertAlmostEqual(fit["dead_time_minutes"], 12.0, delta=3.0)

    def test_too_few_points(self) -> None:
        self.assertIsNone(steptest.fit_fopdt([(0.1, 0.0)]))


if __name__ == "__main__":
    unittest.main()
