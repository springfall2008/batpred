# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk Cloud API component
# -----------------------------------------------------------------------------

"""Tests for the Sunsynk Cloud API component (``sunsynk.py``)."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
import pytz
from datetime import datetime
from unittest.mock import MagicMock, patch
from sunsynk import SunsynkAPI
from sunsynk_const import SUNSYNK_REGIONS, SUNSYNK_ENDPOINTS
from tests.test_infra import run_async as run_async_local


class MockSunsynk(SunsynkAPI):
    """Test double: build a SunsynkAPI without the full component lifecycle."""

    def __init__(self, auth_method="password", region="sunsynk", inverter_sn=None, control_enable=True):
        """Set up a minimal SunsynkAPI instance for tests, bypassing ComponentBase.__init__."""
        self.prefix = "predbat"
        self.automatic = False
        self.automatic_ignore_pv = False
        self.region = region
        self.username = "test@example.com"
        self.password = "hunter2"
        self.auth_method = auth_method
        self.control_enable = control_enable
        self.inverter_sn_filter = inverter_sn or []
        self.device_list = []
        self.device_detail = {}
        self.device_values = {}
        self.device_settings = {}
        self.device_capacity = {}
        self.device_pack_voltage = {}
        self.device_energy = {}
        self.device_rated_power = {}
        self.battery_nominal_voltage = 0.0
        self.local_schedule = {}
        self.applied_payload = {}
        self.settle_count = {}
        self.control_active = set()
        self.cached_values = {}
        self._tier_refreshed = {}
        self._cache_restored = False
        self._soc_floor_warned = set()
        self.log_messages = []
        self.local_tz = pytz.timezone("Europe/London")
        self.base = MagicMock()
        self.base.args = {"user_id": "test-sunsynk-1"}
        self.base.midnight_utc = datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        self.base.minutes_now = 0  # local minutes-since-midnight; tests set this for time-aware control
        # Straight through, like the component: _init_oauth owns self.auth_method, so
        # collapsing the modes here would hide password_legacy from fetch_token.
        self._init_oauth(auth_method, "test-token", None, "sunsynk")

    def log(self, message):
        """Capture logs."""
        self.log_messages.append(message)

    def update_success_timestamp(self):
        """No-op for tests."""
        pass


def test_sunsynk_base_url_and_source():
    """base_url and source resolve from the configured region."""
    failed = False
    for region in ("sunsynk", "inteless"):
        s = MockSunsynk(region=region)
        if s.base_url != SUNSYNK_REGIONS[region]["host"]:
            print(f"ERROR: {region} base_url {s.base_url}")
            failed = True
        if s.source != SUNSYNK_REGIONS[region]["source"]:
            print(f"ERROR: {region} source {s.source}")
            failed = True
    # An unknown region must fall back to the default rather than crash on startup.
    s = MockSunsynk(region="nonsense")
    if s.base_url != SUNSYNK_REGIONS["sunsynk"]["host"]:
        print(f"ERROR: unknown region did not fall back, got {s.base_url}")
        failed = True
    assert not failed, "test_sunsynk_base_url_and_source"


def test_is_auth_error_body():
    """Body-level auth failures are detected; genuine non-auth failures are not."""
    failed = False
    auth_bodies = [
        {"success": False, "msg": "Invalid Token"},
        {"success": False, "msg": "token expired"},
        {"success": False, "msg": "Unauthorized"},
    ]
    other_bodies = [
        {"success": True, "msg": "Success"},
        {"success": False, "msg": "inverter offline"},
        {"success": False, "msg": "parameter error"},
        {},
    ]
    for body in auth_bodies:
        if not SunsynkAPI.is_auth_error_body(body):
            print(f"ERROR: {body} not detected as an auth error")
            failed = True
    for body in other_bodies:
        if SunsynkAPI.is_auth_error_body(body):
            print(f"ERROR: {body} wrongly detected as an auth error")
            failed = True
    assert not failed, "test_is_auth_error_body"


def test_request_returns_data_on_success():
    """A successful envelope yields its data dict."""
    failed = False
    s = MockSunsynk()

    async def fake_request(method, endpoint_key, sn=None, params=None, body=None):
        """Fake transport returning a successful battery payload."""
        return {"soc": 55, "power": -1200}

    with patch.object(s, "_request", side_effect=fake_request):
        out = run_async_local(s._get("battery", sn="INV1"))
    if out.get("soc") != 55:
        print(f"ERROR: expected soc 55, got {out}")
        failed = True
    assert not failed, "test_request_returns_data_on_success"


def test_request_returns_empty_on_failure():
    """A failed envelope yields {} so callers fail closed rather than act on junk."""
    failed = False
    s = MockSunsynk()

    async def fake_request(method, endpoint_key, sn=None, params=None, body=None):
        """Fake transport returning a failure."""
        return {}

    with patch.object(s, "_request", side_effect=fake_request):
        out = run_async_local(s._get("battery", sn="INV1"))
    if out != {}:
        print(f"ERROR: expected {{}} on failure, got {out}")
        failed = True
    assert not failed, "test_request_returns_empty_on_failure"


def test_endpoint_paths_render_serial():
    """Templated endpoints render the serial into the path."""
    failed = False
    for endpoint in ("battery", "grid", "load", "input", "settings_read", "settings_set", "inverter_detail"):
        path = SUNSYNK_ENDPOINTS[endpoint].format(sn="ABC123")
        if "ABC123" not in path or "{sn}" in path:
            print(f"ERROR: endpoint {endpoint} rendered as {path}")
            failed = True
    assert not failed, "test_endpoint_paths_render_serial"


def run_sunsynk_api_tests(my_predbat):
    """Run all Sunsynk API tests."""
    failed = False
    for name, fn in [
        ("base_url_and_source", test_sunsynk_base_url_and_source),
        ("is_auth_error_body", test_is_auth_error_body),
        ("request_success", test_request_returns_data_on_success),
        ("request_failure", test_request_returns_empty_on_failure),
        ("endpoint_paths", test_endpoint_paths_render_serial),
    ]:
        try:
            if fn():
                print(f"  FAILED: sunsynk_api.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sunsynk_api.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
