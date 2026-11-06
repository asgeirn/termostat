import statistics
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from termostat import control, db
from termostat.timeutil import iso, parse_iso

START = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
SETTINGS = control.Settings()


class ControlTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def reading(self, at: datetime, temperature: float, setpoint: int = 20, on: int = 1, fan: str = "medium_low") -> None:
        self.conn.execute(
            """INSERT INTO readings (logged_at, device_id, temperature, target_temperature, ac_on, mode, fan_level)
               VALUES (?, 'dev', ?, ?, ?, 'heat', ?)""",
            (iso(at), temperature, setpoint, on, fan),
        )
        self.conn.commit()

    def simulate(self, hours: float, temperature, **reading) -> list[dict]:
        """Log a reading every 5 minutes and run the controller 2 minutes later."""
        records = []
        for i in range(int(hours * 12)):
            at = START + timedelta(minutes=5 * i)
            self.reading(at, temperature(at), **reading)
            records.append(control.run(self.conn, at + timedelta(minutes=2)))
        return records


class PlateauTest(unittest.TestCase):
    def test_interpolates_and_extrapolates(self) -> None:
        plateaus = SETTINGS.plateaus
        self.assertAlmostEqual(control.plateau(20, plateaus), 20.6)
        self.assertAlmostEqual(control.plateau(21, plateaus), 21.05)
        self.assertAlmostEqual(control.plateau(17, plateaus), 18.5)
        self.assertAlmostEqual(control.plateau(23, plateaus), 21.95)


class RunTest(ControlTestCase):
    def test_first_run_takes_over_actual_setpoint(self) -> None:
        self.reading(START, 20.6)
        record = control.run(self.conn, START + timedelta(minutes=2))
        self.assertEqual((record["status"], record["setpoint"], record["output"]), ("started", 20, 20.0))
        self.assertEqual(record["model_offset"], 0.0)
        self.assertAlmostEqual(record["p_term"] + record["integral"], 20.0)

    def test_inactive_when_off_stale_or_step_test(self) -> None:
        self.reading(START, 20.6, on=0)
        self.assertIn("not on in heat mode", control.run(self.conn, START + timedelta(minutes=2))["reason"])
        self.assertEqual(control.run(self.conn, START + timedelta(minutes=20))["reason"], "no fresh reading")
        self.reading(START + timedelta(minutes=25), 20.6, fan="auto")
        self.assertIn("fan level", control.run(self.conn, START + timedelta(minutes=27))["reason"])
        self.conn.execute(
            "INSERT INTO step_tests (night, device_id, decided_at, decision, kind, status) VALUES ('x', 'dev', 'x', 'run', 'heat', 'running')"
        )
        self.reading(START + timedelta(minutes=30), 20.6)
        self.assertEqual(control.run(self.conn, START + timedelta(minutes=32))["reason"], "step test running")

        self.conn.execute("UPDATE step_tests SET status = 'completed'")
        self.reading(START + timedelta(minutes=35), 20.6)
        record = control.run(self.conn, START + timedelta(minutes=37))
        self.assertEqual(record["status"], "started")
        self.assertIn("resumed after: step test running", record["reason"])

    def test_restarts_after_gap(self) -> None:
        self.reading(START, 20.6)
        control.run(self.conn, START + timedelta(minutes=2))
        self.reading(START + timedelta(minutes=40), 20.6)
        record = control.run(self.conn, START + timedelta(minutes=42))
        self.assertEqual(record["status"], "started")
        self.assertIn("gap", record["reason"])

    def test_holds_target_by_alternating_setpoints(self) -> None:
        # The room sits at the setpoint-20 plateau. The controller should average a little below
        # 20, alternating between 19 and 20, with the counterfactual room near the target.
        records = self.simulate(36, lambda t: 20.6)
        steady = records[12 * 12:]
        self.assertEqual({r["setpoint"] for r in steady}, {19, 20})
        self.assertAlmostEqual(statistics.fmean(r["controlled_temperature"] for r in steady), SETTINGS.target, delta=0.05)
        changes = [parse_iso(r["run_at"]) for r in records if r["changed"]]
        gaps = [b - a for a, b in zip(changes, changes[1:])]
        self.assertGreaterEqual(min(gaps), SETTINGS.min_dwell)
        self.assertLessEqual(sum(r["changed"] for r in steady), 30)

    def test_integral_raises_setpoint_when_room_is_cold(self) -> None:
        # A cold spell: the room only reaches 19.6 at setpoint 20. The controller goes up and
        # stays there while the counterfactual room is below target.
        records = self.simulate(12, lambda t: 19.6)
        self.assertEqual(records[-1]["setpoint"], 21)
        self.assertGreater(records[-1]["model_offset"], 0.3)
        self.assertLess(records[-1]["controlled_temperature"], SETTINGS.target)

    def test_status(self) -> None:
        self.simulate(3, lambda t: 20.6)
        text = "\n".join(control.status_lines(self.conn, START))
        self.assertIn("36 runs (shadow), 36 controlling", text)
        self.assertIn("Room as controlled:", text)


if __name__ == "__main__":
    unittest.main()
