import tempfile
import unittest
from pathlib import Path

from termostat import backfill, db

SERIES_RESPONSE = {
    "temperature": [
        {"time": "2026-01-15T12:00:00Z", "value": 21.2},
        {"time": "2026-01-15T12:01:30Z", "value": 21.3},
    ],
    "humidity": [
        {"time": "2026-01-15T12:00:00Z", "value": 35.0},
        {"time": "2026-01-15T12:01:30Z", "value": 35.1},
    ],
}

LIST_RESPONSE = [
    {"time": "2026-01-15T12:00:00Z", "temperature": 21.2, "humidity": 35.0},
    {"time": "2026-01-15T12:03:00Z", "temperature": 21.4, "humidity": 35.2},
]


class ParseTest(unittest.TestCase):
    def test_series_format(self) -> None:
        parsed = backfill.parse_measurements(SERIES_RESPONSE)
        self.assertEqual(
            parsed,
            {
                "2026-01-15T12:00:00+00:00": {"temperature": 21.2, "humidity": 35.0},
                "2026-01-15T12:01:30+00:00": {"temperature": 21.3, "humidity": 35.1},
            },
        )

    def test_list_format(self) -> None:
        parsed = backfill.parse_measurements(LIST_RESPONSE)
        self.assertEqual(parsed["2026-01-15T12:03:00+00:00"], {"temperature": 21.4, "humidity": 35.2})

    def test_ignores_garbage(self) -> None:
        self.assertEqual(backfill.parse_measurements({"temperature": [{"time": None, "value": 1}, "x"]}), {})
        self.assertEqual(backfill.parse_measurements(None), {})


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_rerun_is_idempotent(self) -> None:
        self.assertEqual(backfill.store_measurements(self.conn, "abc", backfill.parse_measurements(SERIES_RESPONSE)), 2)
        self.assertEqual(backfill.store_measurements(self.conn, "abc", backfill.parse_measurements(SERIES_RESPONSE)), 0)
        self.assertEqual(backfill.store_measurements(self.conn, "abc", backfill.parse_measurements(LIST_RESPONSE)), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM historical_measurements").fetchone()[0], 3)

    def test_completes_missing_values(self) -> None:
        backfill.store_measurements(self.conn, "abc", {"2026-01-15T12:00:00+00:00": {"temperature": 21.2, "humidity": None}})
        added = backfill.store_measurements(self.conn, "abc", {"2026-01-15T12:00:00+00:00": {"temperature": 99.0, "humidity": 35.0}})
        self.assertEqual(added, 1)
        row = self.conn.execute("SELECT temperature, humidity FROM historical_measurements").fetchone()
        self.assertEqual((row["temperature"], row["humidity"]), (21.2, 35.0))


if __name__ == "__main__":
    unittest.main()
