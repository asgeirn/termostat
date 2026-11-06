import contextlib
import io
import os
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from termostat import db, power
from termostat.shelly import ShellyError

NOW = datetime(2026, 11, 6, 6, 21, tzinfo=UTC)


class FakeMeter:
    host = "plug"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def get_meter_status(self):
        if self.fail:
            raise ShellyError("PM1.GetStatus failed: timed out")
        return {"id": 0, "apower": 82.1, "aenergy": {"total": 20.411, "by_minute": [1275.698], "minute_ts": 1}}


class RunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        self.conn = db.connect(self.path)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_logs_power(self) -> None:
        self.assertEqual(power.run(self.conn, FakeMeter(), NOW), 0)
        row = self.conn.execute("SELECT logged_at, host, apower, aenergy_total FROM power_readings").fetchone()
        self.assertEqual(tuple(row), ("2026-11-06T06:21:00+00:00", "plug", 82.1, 20.411))

    def test_failure_is_reported(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(power.run(self.conn, FakeMeter(fail=True), NOW), 1)
        self.assertIn("timed out", err.getvalue())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM power_readings").fetchone()[0], 0)

    def test_main_requires_shelly_host(self) -> None:
        with mock.patch.dict(os.environ, {"SHELLY_HOST": ""}), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(power.main(["--db", str(self.path)]), 2)
        self.assertIn("SHELLY_HOST is not set", err.getvalue())


if __name__ == "__main__":
    unittest.main()
