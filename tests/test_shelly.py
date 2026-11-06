import json
import ssl
import tempfile
import unittest
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from unittest import mock

from termostat import db, http
from termostat.config import ShellyConfig
from termostat.shelly import ShellyClient, ShellyError, store_power_reading

NOW = datetime(2026, 11, 6, 6, 5, 50, tzinfo=UTC)

# PM1.GetStatus?id=0 from the real plug (S3PL-30116EU, firmware 2.1.1), 2026-11-06.
STATUS = {
    "id": 0,
    "voltage": 231.1,
    "current": 0.374,
    "apower": 82.1,
    "freq": 49.9,
    "aenergy": {"total": 20.411, "by_minute": [1275.698, 1488.314, 1488.314], "minute_ts": 1793945100},
    "ret_aenergy": {"total": 0.0, "by_minute": [0.0, 0.0, 0.0], "minute_ts": 1793945100},
}

# Shelly.GetDeviceInfo from the same plug (no authentication needed).
DEVICE_INFO = {
    "name": "Varmepumpe",
    "id": "shellyplugpmg3-9070695bb0c8",
    "mac": "9070695BB0C8",
    "slot": 0,
    "model": "S3PL-30116EU",
    "gen": 3,
    "fw_id": "20261026-151255/2.1.1-g07e9ad2",
    "ver": "2.1.1",
    "app": "PlugPMG3",
    "auth_en": True,
    "auth_domain": "shellyplugpmg3-9070695bb0c8",
    "matter": False,
    "provision": "complete",
    "enhanced_security": True,
}


def response(status: int, body: object) -> http.Response:
    return http.Response(status, Message(), json.dumps(body).encode())


class ClientTest(unittest.TestCase):
    def test_plain_host_uses_http(self) -> None:
        client = ShellyClient(ShellyConfig("192.168.1.50", None))
        with mock.patch.object(http, "get", return_value=response(200, STATUS)) as get:
            self.assertEqual(client.get_meter_status(), STATUS)
        url, _, _, auth, context = get.call_args.args
        self.assertEqual(url, "http://192.168.1.50/rpc/PM1.GetStatus?id=0")
        self.assertIsNone(auth)
        self.assertIsNone(context)

    def test_https_url_with_password(self) -> None:
        client = ShellyClient(ShellyConfig("https://192.168.10.191/", "secret"))
        self.assertEqual(client.host, "192.168.10.191")
        with mock.patch.object(http, "get", return_value=response(200, DEVICE_INFO)) as get:
            self.assertEqual(client.get_device_info()["model"], "S3PL-30116EU")
        url, _, _, auth, _ = get.call_args.args
        self.assertEqual(url, "https://192.168.10.191/rpc/Shelly.GetDeviceInfo")
        self.assertEqual(auth, ("admin", "secret"))

    def test_ca_file_builds_tls_context(self) -> None:
        with mock.patch.object(ssl, "create_default_context") as create:
            ShellyClient(ShellyConfig("https://plug", None, Path("/etc/ca.pem")))
        create.assert_called_once_with(cafile=Path("/etc/ca.pem"))

    def test_unauthorized(self) -> None:
        client = ShellyClient(ShellyConfig("plug", None))
        with mock.patch.object(http, "get", return_value=response(401, {})):
            with self.assertRaisesRegex(ShellyError, "SHELLY_PASSWORD"):
                client.get_meter_status()

    def test_network_error(self) -> None:
        client = ShellyClient(ShellyConfig("plug", None))
        with mock.patch.object(http, "get", side_effect=ssl.SSLCertVerificationError("self-signed")):
            with self.assertRaisesRegex(ShellyError, "PM1.GetStatus failed"):
                client.get_meter_status()


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_stores_real_response(self) -> None:
        store_power_reading(self.conn, "192.168.10.191", STATUS, NOW)
        row = self.conn.execute(
            """SELECT logged_at, host, apower, voltage, current, freq, aenergy_total,
                      aenergy_by_minute, aenergy_minute_ts, errors, raw FROM power_readings"""
        ).fetchone()
        self.assertEqual(
            tuple(row[:10]),
            (
                "2026-11-06T06:05:50+00:00",
                "192.168.10.191",
                82.1,
                231.1,
                0.374,
                49.9,
                20.411,
                "[1275.698, 1488.314, 1488.314]",
                1793945100,
                None,
            ),
        )
        self.assertEqual(json.loads(row[10]), STATUS)

