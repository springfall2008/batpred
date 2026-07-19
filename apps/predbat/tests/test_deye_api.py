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
from unittest.mock import MagicMock
from deye import DeyeAPI
from deye_const import DEYE_BASE_URLS


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
