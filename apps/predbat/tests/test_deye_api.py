# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test DEYE Cloud API component
# -----------------------------------------------------------------------------

"""Tests for the DEYE Cloud API component (``deye.py``)."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
import pytz
from datetime import datetime
from unittest.mock import MagicMock, patch
from deye import DeyeAPI
from deye_const import DEYE_BASE_URLS
from tests.test_infra import run_async as run_async_local


class MockDeye(DeyeAPI):
    """Test double: build a DeyeAPI without the full component lifecycle."""

    def __init__(self, auth_method="app_credentials", data_center="eu", inverter_sn=None):
        """Set up a minimal DeyeAPI instance for tests, bypassing ComponentBase.__init__."""
        self.prefix = "predbat"
        self.automatic = False
        self.automatic_ignore_pv = False
        self.data_center = data_center
        self.company_id = ""
        self.app_id = ""
        self.app_secret = "test-secret"
        self.username = ""
        self.password = ""
        self.token_hash = ""
        self.inverter_sn_filter = inverter_sn or []
        self.device_list = []
        self.device_values = {}
        self.device_battery_config = {}
        self.local_schedule = {}
        self.pending_orders = {}
        self.applied_payload = {}
        self.cached_values = {}
        self.log_messages = []
        self.local_tz = pytz.timezone("Europe/London")
        self.base = MagicMock()
        self.base.args = {"user_id": "test-deye-1"}
        self.base.midnight_utc = datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        self._init_oauth(auth_method, "test-token", None, "deye")

    def log(self, message):
        """Capture logs."""
        self.log_messages.append(message)

    def update_success_timestamp(self):
        """No-op for tests."""
        pass


def test_deye_base_url():
    """base_url resolves from the data centre."""
    failed = False
    d = MockDeye(data_center="eu")
    if d.base_url != DEYE_BASE_URLS["eu"]:
        print(f"ERROR: base_url {d.base_url}")
        failed = True
    assert not failed, "test_deye_base_url"


def test_get_device_list_filters_inverters():
    """Only INVERTER devices are kept; sn filter is honoured."""
    failed = False
    d = MockDeye(inverter_sn=["INV1"])

    async def fake_post(endpoint_key, body):
        """Fake DEYE POST: return a station list, then a paginated device list."""
        if endpoint_key == "station_list":
            return {"success": True, "stationList": [{"id": 10}]}
        if endpoint_key == "station_device":
            return {
                "success": True,
                "total": 2,
                "deviceListItems": [
                    {"deviceType": "INVERTER", "deviceSn": "INV1"},
                    {"deviceType": "METER", "deviceSn": "MET9"},
                ],
            }
        return {"success": True}

    with patch.object(d, "_post", side_effect=fake_post):
        devices = run_async_local(d.get_device_list())
    if devices != ["INV1"]:
        print(f"ERROR: devices {devices}")
        failed = True
    assert not failed, "test_get_device_list_filters_inverters"
