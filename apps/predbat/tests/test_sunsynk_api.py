# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk Cloud API component
# -----------------------------------------------------------------------------

"""Tests for the Sunsynk Cloud API component (``sunsynk.py``)."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
import asyncio
import signal
import aiohttp
import pytz
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock, patch
from sunsynk import SunsynkAPI
from sunsynk_const import SUNSYNK_REGIONS, SUNSYNK_ENDPOINTS, SUNSYNK_RETRIES, SUNSYNK_MAX_DISCOVERY_PAGES
from tests.test_infra import run_async as run_async_local, create_aiohttp_mock_response, create_aiohttp_mock_session


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
        self.device_energy = {}
        self.device_rated_power = {}
        self.battery_nominal_voltage = 0.0
        self.local_schedule = {}
        self.applied_payload = {}
        self.settle_count = {}
        self.control_active = set()
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


def _session_with_request(response):
    """Return a mock aiohttp session whose .request(...) yields the given response.

    sunsynk.py calls ``session.request(method, url, ...)`` rather than ``session.get``/
    ``session.post``, so the shared ``create_aiohttp_mock_session()`` helper (which only
    wires up get/post) needs its already-configured ``.get`` mock aliased onto ``.request``
    too before it is usable for these transport-level tests.
    """
    session = create_aiohttp_mock_session(response)
    session.request = session.get
    return session


def _mock_json_response(status=200, json_data=None):
    """Build a mock aiohttp response whose .json() tolerates sunsynk.py's content_type kwarg.

    ``create_aiohttp_mock_response()``'s json() coroutine takes no arguments, but sunsynk.py
    always calls ``response.json(content_type=None)`` - Sunsynk does not reliably set an
    application/json Content-Type header, so the real code asks aiohttp to parse the body
    regardless. The coroutine is rebound here to accept and ignore whatever kwargs it is
    called with, the same fix ``test_sigenergy.py`` already applies for the same reason.
    """
    response = create_aiohttp_mock_response(status=status, json_data=json_data)

    async def _json(*args, **kwargs):
        """Return the canned JSON payload regardless of the content_type kwarg passed."""
        return {} if json_data is None else json_data

    response.json = _json
    return response


def test_request_real_success_returns_data():
    """The real _request returns the `data` dict from a successful envelope."""
    failed = False
    s = MockSunsynk()
    session = _session_with_request(_mock_json_response(status=200, json_data={"success": True, "data": {"soc": 55}}))
    with patch("sunsynk.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = session
        result = run_async_local(s._request("GET", "battery", sn="INV1"))
    if result != {"soc": 55}:
        print(f"ERROR: expected the data dict, got {result!r}")
        failed = True
    assert not failed, "test_request_real_success_returns_data"


def test_request_real_success_no_data_returns_empty_dict():
    """A successful envelope with no `data` key returns {}, not None.

    This is the settings-write shape, and the distinction is load-bearing for a later
    task: {} means "worked, nothing to report"; None means "failed" and must never be
    confused with it.
    """
    failed = False
    s = MockSunsynk()
    session = _session_with_request(_mock_json_response(status=200, json_data={"success": True}))
    with patch("sunsynk.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = session
        result = run_async_local(s._request("POST", "settings_set", sn="INV1", body={}))
    if result is None:
        print("ERROR: a successful envelope with no data was reported as a failure (None)")
        failed = True
    elif result != {}:
        print(f"ERROR: expected {{}}, got {result!r}")
        failed = True
    assert not failed, "test_request_real_success_no_data_returns_empty_dict"


def test_request_real_failure_body_returns_none_without_refresh():
    """A non-auth failure body returns None and never triggers a token refresh."""
    failed = False
    s = MockSunsynk()
    session = _session_with_request(_mock_json_response(status=200, json_data={"success": False, "msg": "parameter error"}))
    refresh_calls = []

    async def fake_fetch_token():
        """Record a refresh call that must never happen for a non-auth failure."""
        refresh_calls.append(1)
        return True

    with patch("sunsynk.aiohttp.ClientSession") as mock_session_class, patch.object(s, "fetch_token", side_effect=fake_fetch_token):
        mock_session_class.return_value = session
        result = run_async_local(s._request("GET", "battery", sn="INV1"))
    if result is not None:
        print(f"ERROR: expected None on failure, got {result!r}")
        failed = True
    if refresh_calls:
        print("ERROR: fetch_token was called for a non-auth failure")
        failed = True
    assert not failed, "test_request_real_failure_body_returns_none_without_refresh"


def test_request_real_auth_error_triggers_single_refresh_and_retry():
    """A body-level auth failure triggers exactly one refresh, then retries and succeeds."""
    failed = False
    s = MockSunsynk()
    responses = [
        _mock_json_response(status=200, json_data={"success": False, "msg": "invalid token"}),
        _mock_json_response(status=200, json_data={"success": True, "data": {"soc": 42}}),
    ]
    sessions = []

    def session_factory(*args, **kwargs):
        """Hand out one fresh mocked session per aiohttp.ClientSession(...) call."""
        session = _session_with_request(responses.pop(0))
        sessions.append(session)
        return session

    refresh_calls = []

    async def fake_fetch_token():
        """Simulate a successful token refresh that installs a new access token."""
        refresh_calls.append(1)
        s.access_token = "refreshed-token"
        return True

    with patch("sunsynk.aiohttp.ClientSession", side_effect=session_factory), patch.object(s, "fetch_token", side_effect=fake_fetch_token):
        result = run_async_local(s._request("GET", "battery", sn="INV1"))
    if result != {"soc": 42}:
        print(f"ERROR: expected the retried response's data, got {result!r}")
        failed = True
    if len(refresh_calls) != 1:
        print(f"ERROR: expected exactly one refresh, got {len(refresh_calls)}")
        failed = True
    if len(sessions) != 2:
        print(f"ERROR: expected exactly 2 requests (original + one retry), got {len(sessions)}")
        failed = True
    else:
        second_headers = sessions[1].request.call_args.kwargs.get("headers", {})
        if second_headers.get("Authorization") != "Bearer refreshed-token":
            print(f"ERROR: retry did not carry the refreshed token, got {second_headers.get('Authorization')!r}")
            failed = True
    assert not failed, "test_request_real_auth_error_triggers_single_refresh_and_retry"


def test_request_real_auth_error_on_final_attempt_still_retries():
    """Regression test: a refresh earned on the last transport attempt must not be discarded.

    Two transport failures consume the whole SUNSYNK_RETRIES budget, and only the LAST
    transport attempt gets a response at all - one carrying a body-level auth failure. The
    refresh that follows must still earn its own retry and succeed, rather than falling
    through to `return None` because the transport-attempt loop had already run out (fix
    round 1, Finding 1).
    """
    failed = False
    s = MockSunsynk()
    responses = [
        _mock_json_response(status=500),
        _mock_json_response(status=500),
        _mock_json_response(status=200, json_data={"success": False, "msg": "invalid token"}),
        _mock_json_response(status=200, json_data={"success": True, "data": {"ok": True}}),
    ]
    sessions = []

    def session_factory(*args, **kwargs):
        """Hand out one fresh mocked session per aiohttp.ClientSession(...) call."""
        session = _session_with_request(responses.pop(0))
        sessions.append(session)
        return session

    refresh_calls = []

    async def fake_fetch_token():
        """Simulate a successful token refresh that installs a new access token."""
        refresh_calls.append(1)
        s.access_token = "refreshed-token"
        return True

    with patch("sunsynk.aiohttp.ClientSession", side_effect=session_factory), patch.object(s, "fetch_token", side_effect=fake_fetch_token), patch("sunsynk.asyncio.sleep", new_callable=AsyncMock):
        result = run_async_local(s._request("GET", "battery", sn="INV1"))
    if result != {"ok": True}:
        print(f"ERROR: refresh earned on the final transport attempt was discarded, got {result!r}")
        failed = True
    if len(refresh_calls) != 1:
        print(f"ERROR: expected exactly one refresh, got {len(refresh_calls)}")
        failed = True
    if len(sessions) != 4:
        print(f"ERROR: expected 2 transport failures + 1 auth failure + 1 retry = 4 requests, got {len(sessions)}")
        failed = True
    assert not failed, "test_request_real_auth_error_on_final_attempt_still_retries"


def test_request_real_oauth_auth_error_refreshes_through_the_edge_function():
    """On the oauth path a body-level auth failure must refresh via handle_oauth_401.

    fetch_token() has nothing to log in with in oauth mode - the token is injected by
    Predbat.com - so it just reports that a (dead) token is still held and the retry goes
    out with exactly the same bearer. Every other OAuthMixin component goes back
    through the edge function on a 401-equivalent (deye.py's reauthenticate(), fox.py,
    solis.py, teslemetry.py, kraken.py); this is that shape.
    """
    failed = False
    s = MockSunsynk(auth_method="oauth")
    responses = [
        _mock_json_response(status=200, json_data={"success": False, "msg": "invalid token"}),
        _mock_json_response(status=200, json_data={"success": True, "data": {"soc": 42}}),
    ]
    sessions = []

    def session_factory(*args, **kwargs):
        """Hand out one fresh mocked session per aiohttp.ClientSession(...) call."""
        session = _session_with_request(responses.pop(0))
        sessions.append(session)
        return session

    refreshes = []

    async def fake_do_refresh():
        """Stand in for the oauth-refresh edge function, installing a fresh token."""
        refreshes.append(1)
        s.access_token = "refreshed-token"
        return True

    with patch("sunsynk.aiohttp.ClientSession", side_effect=session_factory), patch.object(s, "_do_refresh", side_effect=fake_do_refresh):
        result = run_async_local(s._request("GET", "battery", sn="INV1"))
    if len(refreshes) != 1:
        print(f"ERROR: expected exactly one edge-function refresh on the oauth path, got {len(refreshes)}")
        failed = True
    if result != {"soc": 42}:
        print(f"ERROR: expected the retried response's data, got {result!r}")
        failed = True
    if len(sessions) == 2 and sessions[1].request.call_args.kwargs.get("headers", {}).get("Authorization") != "Bearer refreshed-token":
        print(f"ERROR: the retry did not carry the refreshed token, got {sessions[1].request.call_args.kwargs.get('headers', {}).get('Authorization')!r}")
        failed = True
    assert not failed, "test_request_real_oauth_auth_error_refreshes_through_the_edge_function"


def test_request_real_non200_exhausts_retries_returns_none():
    """A persistent non-200 status is retried up to SUNSYNK_RETRIES times, then gives up."""
    failed = False
    s = MockSunsynk()
    session = _session_with_request(_mock_json_response(status=500))
    with patch("sunsynk.aiohttp.ClientSession") as mock_session_class, patch("sunsynk.asyncio.sleep", new_callable=AsyncMock):
        mock_session_class.return_value = session
        result = run_async_local(s._request("GET", "battery", sn="INV1"))
    if result is not None:
        print(f"ERROR: expected None after exhausting retries, got {result!r}")
        failed = True
    if session.request.call_count != SUNSYNK_RETRIES:
        print(f"ERROR: expected {SUNSYNK_RETRIES} attempts, got {session.request.call_count}")
        failed = True
    assert not failed, "test_request_real_non200_exhausts_retries_returns_none"


def test_request_real_transport_exception_exhausts_retries_returns_none():
    """Transport exceptions (ClientError and TimeoutError) are retried, then give up as None."""
    failed = False
    for exc in (aiohttp.ClientError("connection refused"), asyncio.TimeoutError("timed out")):
        s = MockSunsynk()
        session = create_aiohttp_mock_session(exception=exc)
        session.request = session.get
        with patch("sunsynk.aiohttp.ClientSession") as mock_session_class, patch("sunsynk.asyncio.sleep", new_callable=AsyncMock):
            mock_session_class.return_value = session
            result = run_async_local(s._request("GET", "battery", sn="INV1"))
        if result is not None:
            print(f"ERROR: {type(exc).__name__} expected None after exhausting retries, got {result!r}")
            failed = True
        if session.request.call_count != SUNSYNK_RETRIES:
            print(f"ERROR: {type(exc).__name__} expected {SUNSYNK_RETRIES} attempts, got {session.request.call_count}")
            failed = True
    assert not failed, "test_request_real_transport_exception_exhausts_retries_returns_none"


def test_get_device_list_pages_and_filters():
    """Discovery pages through /inverters and honours the serial filter."""
    failed = False
    s = MockSunsynk(inverter_sn=["INV1"])
    calls = []

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return two pages of inverters, one of which is filtered out."""
        calls.append((endpoint_key, dict(params or {})))
        if endpoint_key != "inverter_list":
            return {}
        page = int((params or {}).get("page", 1))
        if page == 1:
            return {"total": 3, "infos": [{"sn": "INV1"}, {"sn": "INV2"}]}
        return {"total": 3, "infos": [{"sn": "INV3"}]}

    with patch.object(s, "_get", side_effect=fake_get):
        devices = run_async_local(s.get_device_list())
    if devices != ["INV1"]:
        print(f"ERROR: expected ['INV1'] after filtering, got {devices}")
        failed = True
    if len(calls) < 2:
        print(f"ERROR: expected pagination, only {len(calls)} call(s) made")
        failed = True
    assert not failed, "test_get_device_list_pages_and_filters"


def test_get_device_list_unfiltered_returns_all():
    """With no filter, every discovered serial is registered."""
    failed = False
    s = MockSunsynk()

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return a single page of two inverters."""
        return {"total": 2, "infos": [{"sn": "INV1"}, {"sn": "INV2"}]}

    with patch.object(s, "_get", side_effect=fake_get):
        devices = run_async_local(s.get_device_list())
    if devices != ["INV1", "INV2"]:
        print(f"ERROR: expected both serials, got {devices}")
        failed = True
    assert not failed, "test_get_device_list_unfiltered_returns_all"


def test_fetch_device_data_maps_telemetry_and_energy():
    """Telemetry from four endpoints is flattened onto the Predbat sensor leaves."""
    failed = False
    s = MockSunsynk()

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return a realistic payload for each realtime endpoint."""
        if endpoint_key == "battery":
            return {"soc": 62, "power": -1500, "voltage": 51.2, "temp": 21.5, "capacity": 280, "chargeVolt": 56.8, "maxChargeCurrentLimit": 100, "etodayChg": 7.4, "etodayDischg": 5.1}
        if endpoint_key == "grid":
            return {"pac": 430, "etodayFrom": 3.2, "etodayTo": 1.1}
        if endpoint_key == "load":
            return {"totalPower": 900, "dailyUsed": 12.6}
        if endpoint_key == "input":
            return {"pac": 2100, "etoday": 9.8}
        return {}

    with patch.object(s, "_get", side_effect=fake_get):
        run_async_local(s.fetch_device_data("INV1"))
    values = s.device_values.get("INV1", {})
    for leaf, expect in (("soc", 62), ("battery_power", -1500), ("grid_power", 430), ("load_power", 900), ("pv_power", 2100), ("temperature", 21.5)):
        if values.get(leaf) != expect:
            print(f"ERROR: telemetry {leaf} = {values.get(leaf)}, expected {expect}")
            failed = True
    energy = s.device_energy.get("INV1", {})
    for leaf, expect in (("pv_today", 9.8), ("import_today", 3.2), ("export_today", 1.1), ("load_today", 12.6), ("battery_charge_today", 7.4), ("battery_discharge_today", 5.1)):
        if energy.get(leaf) != expect:
            print(f"ERROR: energy {leaf} = {energy.get(leaf)}, expected {expect}")
            failed = True
    assert not failed, "test_fetch_device_data_maps_telemetry_and_energy"


def test_fetch_device_data_absent_fields_are_not_invented():
    """A model that omits a counter leaves it absent rather than publishing a zero."""
    failed = False
    s = MockSunsynk()

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return a battery payload with no energy counters at all."""
        if endpoint_key == "battery":
            return {"soc": 50, "power": 0}
        return {}

    with patch.object(s, "_get", side_effect=fake_get):
        run_async_local(s.fetch_device_data("INV1"))
    energy = s.device_energy.get("INV1", {})
    for leaf in ("battery_charge_today", "pv_today", "import_today"):
        if leaf in energy:
            print(f"ERROR: {leaf} was invented as {energy[leaf]} when the API omitted it")
            failed = True
    if s.device_values.get("INV1", {}).get("soc") != 50:
        print("ERROR: present fields should still be mapped")
        failed = True
    assert not failed, "test_fetch_device_data_absent_fields_are_not_invented"


def test_nominal_pack_voltage_variants():
    """Pack voltage is inferred from the BMS charge target across common stack sizes."""
    failed = False
    s = MockSunsynk()
    # 16 cells -> 51.2V nominal, 24 -> 76.8V, 32 -> 102.4V.
    for charge_volts, expect in ((56.8, 51.2), (85.2, 76.8), (113.6, 102.4)):
        got = s.nominal_pack_voltage(charge_volts)
        if abs(got - expect) > 0.5:
            print(f"ERROR: chargeVolt {charge_volts} gave {got}V nominal, expected about {expect}V")
            failed = True
    # No charge target and no override means no guess.
    if s.nominal_pack_voltage(0) != 0:
        print("ERROR: a missing charge target must not produce a guessed voltage")
        failed = True
    # The explicit override wins.
    s.battery_nominal_voltage = 48.0
    if s.nominal_pack_voltage(0) != 48.0:
        print("ERROR: sunsynk_battery_nominal_voltage override was ignored")
        failed = True
    assert not failed, "test_nominal_pack_voltage_variants"


def test_battery_capacity_amp_hours_to_kwh():
    """Amp-hour capacity becomes kWh using the inferred pack voltage."""
    failed = False
    s = MockSunsynk()
    s.device_values["INV1"] = {"capacity": 280, "chargeVolt": 56.8}
    # 280Ah at 51.2V nominal = 14.336 kWh.
    capacity = s.battery_capacity("INV1")
    if abs(capacity - 14.336) > 0.05:
        print(f"ERROR: capacity {capacity} kWh, expected about 14.34")
        failed = True
    # Without a voltage there must be no guess — a wrong soc_max is worse than none.
    s.device_values["INV2"] = {"capacity": 280}
    if s.battery_capacity("INV2") != 0:
        print(f"ERROR: capacity guessed without a pack voltage: {s.battery_capacity('INV2')}")
        failed = True
    assert not failed, "test_battery_capacity_amp_hours_to_kwh"


def test_battery_rate_max_from_charge_current():
    """The charge-current limit becomes a watt rate using the same pack voltage."""
    failed = False
    s = MockSunsynk()
    s.device_values["INV1"] = {"maxChargeCurrentLimit": 100, "chargeVolt": 56.8}
    # 100A at 51.2V = 5120W.
    rate = s.battery_rate_max("INV1")
    if abs(rate - 5120) > 50:
        print(f"ERROR: battery_rate_max {rate}W, expected about 5120W")
        failed = True
    s.device_values["INV2"] = {"chargeVolt": 56.8}
    if s.battery_rate_max("INV2") != 0:
        print("ERROR: a missing current limit must not produce a guessed rate")
        failed = True
    assert not failed, "test_battery_rate_max_from_charge_current"


def test_battery_reserve_min_from_settings():
    """The inverter's own SOC floor is read from the settings object."""
    failed = False
    s = MockSunsynk()
    s.device_settings["INV1"] = {"batteryLowCap": "14"}
    if s.battery_reserve_min("INV1") != 14:
        print(f"ERROR: reserve min {s.battery_reserve_min('INV1')}, expected 14")
        failed = True
    if s.battery_reserve_min("UNKNOWN") != 0:
        print("ERROR: an unknown serial should report no floor, not crash")
        failed = True
    assert not failed, "test_battery_reserve_min_from_settings"


class _HardTimeout(Exception):
    """Raised when a guarded coroutine exceeds its hard wall-clock timeout."""


def _run_with_hard_timeout(coro, seconds=5):
    """Run coro to completion, or raise _HardTimeout after `seconds` of wall-clock time.

    An asyncio-level timeout (``asyncio.wait_for``) cannot be trusted to catch a runaway
    ``while``/``for`` loop that only ever awaits mocked coroutines resolving with no real
    suspension: each ``await`` completes synchronously without handing control back to the
    event loop's own scheduler, so a ``call_later`` timeout callback never gets a chance to
    run - confirmed empirically against a tight loop shaped exactly like the bug this guards
    against, which hung indefinitely under ``asyncio.wait_for`` but was caught in ~2 seconds
    by ``SIGALRM``. SIGALRM is a genuine OS-level interrupt that CPython checks between
    bytecode instructions, so it fires even inside a loop that never truly yields.
    """

    def _handler(signum, frame):
        """Convert the SIGALRM into a Python exception so pytest-free assertions can catch it."""
        raise _HardTimeout(f"operation exceeded its {seconds}s hard timeout - suspected infinite loop")

    old_handler = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        return run_async_local(coro)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def test_get_device_list_terminates_when_serials_are_missing():
    """A page whose entries never carry `sn` must not spin the discovery loop forever.

    Regression test for Finding 1: driven against the pre-fix method, a page that is
    non-empty but contributes no usable serial let `len(serials)` sit at 0 forever while
    `total` stayed above it, so the loop never terminated (observed: millions of calls
    within seconds, hard-interrupted rather than finishing).
    """
    failed = False
    s = MockSunsynk()
    calls = []

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return an unbounded-looking total whose entries never carry an `sn` field."""
        calls.append(1)
        return {"total": 10, "infos": [{"notsn": "X"}, {"notsn": "Y"}]}

    devices = None
    with patch.object(s, "_get", side_effect=fake_get):
        try:
            devices = _run_with_hard_timeout(s.get_device_list())
        except _HardTimeout as error:
            print(f"ERROR: {error}")
            failed = True
    if devices is not None and devices != []:
        print(f"ERROR: expected no serials when none carry 'sn', got {devices}")
        failed = True
    if len(calls) > 5:
        print(f"ERROR: expected the loop to give up quickly once a page adds nothing new, made {len(calls)} calls")
        failed = True
    assert not failed, "test_get_device_list_terminates_when_serials_are_missing"


def test_get_device_list_deduplicates_repeated_pages():
    """A server that resends the same page must not inflate the device list with duplicates.

    Regression test for Finding 3: driven against the pre-fix method, unconditional
    ``serials.append`` on every page meant a repeated page kept adding the same two
    serials forever (bounded only by `total`), rather than being recognised as no new
    progress and stopped.
    """
    failed = False
    s = MockSunsynk()
    calls = []

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return the identical two-serial page for every requested page number."""
        calls.append(1)
        return {"total": 10, "infos": [{"sn": "INV1"}, {"sn": "INV2"}]}

    devices = None
    with patch.object(s, "_get", side_effect=fake_get):
        try:
            devices = _run_with_hard_timeout(s.get_device_list())
        except _HardTimeout as error:
            print(f"ERROR: {error}")
            failed = True
    if devices is not None and devices != ["INV1", "INV2"]:
        print(f"ERROR: expected exactly the two unique serials with no duplicates, got {devices}")
        failed = True
    if len(calls) > 5:
        print(f"ERROR: expected the loop to stop quickly once a repeated page adds nothing new, made {len(calls)} calls")
        failed = True
    assert not failed, "test_get_device_list_deduplicates_repeated_pages"


def test_get_device_list_bogus_total_is_bounded_by_page_cap():
    """A hugely inflated `total` must not be trusted past the discovery page cap.

    Regression test for Finding 2: driven against the pre-fix method, a `total` of
    5,000,000 with every page genuinely contributing two new serials (so the "no new
    progress" guard alone cannot help) ran 2,500,000 calls before it happened to satisfy
    `len(serials) >= total`. A hard page cap is defence in depth against a `total` that
    is corrupt or simply never reachable.
    """
    failed = False
    s = MockSunsynk()
    calls = []

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return two brand-new serials per page against an absurd, unreachable total."""
        page = int((params or {}).get("page", 1))
        calls.append(page)
        return {"total": 5000000, "infos": [{"sn": f"INV{page}-A"}, {"sn": f"INV{page}-B"}]}

    devices = None
    with patch.object(s, "_get", side_effect=fake_get):
        try:
            devices = _run_with_hard_timeout(s.get_device_list())
        except _HardTimeout as error:
            print(f"ERROR: {error}")
            failed = True
    if devices is not None and len(calls) > SUNSYNK_MAX_DISCOVERY_PAGES:
        print(f"ERROR: expected discovery to stop at the {SUNSYNK_MAX_DISCOVERY_PAGES}-page cap, made {len(calls)} calls")
        failed = True
    if devices is not None and len(devices) != len(calls) * 2:
        print(f"ERROR: expected {len(calls) * 2} serials from {len(calls)} pages, got {len(devices) if devices is not None else None}")
        failed = True
    assert not failed, "test_get_device_list_bogus_total_is_bounded_by_page_cap"


def test_fetch_device_detail_never_clears_a_known_rated_power():
    """A previously-known rated power survives both an omitted field and a failed refetch."""
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 5000.0

    async def fake_get_no_field(endpoint_key, sn=None, params=None):
        """Return inverter detail that omits the rated-power field entirely."""
        return {"model": "SUN-5K"}

    with patch.object(s, "_get", side_effect=fake_get_no_field):
        run_async_local(s.fetch_device_detail("INV1"))
    if s.device_rated_power.get("INV1") != 5000.0:
        print(f"ERROR: omitting ratePower cleared the known rating, got {s.device_rated_power.get('INV1')}")
        failed = True

    async def fake_get_failure(endpoint_key, sn=None, params=None):
        """Simulate a failed fetch, which _get coerces to {}."""
        return {}

    with patch.object(s, "_get", side_effect=fake_get_failure):
        run_async_local(s.fetch_device_detail("INV1"))
    if s.device_rated_power.get("INV1") != 5000.0:
        print(f"ERROR: a failed fetch cleared the known rating, got {s.device_rated_power.get('INV1')}")
        failed = True
    assert not failed, "test_fetch_device_detail_never_clears_a_known_rated_power"


def run_sunsynk_api_tests(my_predbat):
    """Run all Sunsynk API tests."""
    failed = False
    for name, fn in [
        ("base_url_and_source", test_sunsynk_base_url_and_source),
        ("is_auth_error_body", test_is_auth_error_body),
        ("request_success", test_request_returns_data_on_success),
        ("request_failure", test_request_returns_empty_on_failure),
        ("endpoint_paths", test_endpoint_paths_render_serial),
        ("request_real_success", test_request_real_success_returns_data),
        ("request_real_success_no_data", test_request_real_success_no_data_returns_empty_dict),
        ("request_real_failure_no_refresh", test_request_real_failure_body_returns_none_without_refresh),
        ("request_real_auth_refresh_retry", test_request_real_auth_error_triggers_single_refresh_and_retry),
        ("request_real_refresh_on_final_attempt", test_request_real_auth_error_on_final_attempt_still_retries),
        ("request_real_oauth_refresh", test_request_real_oauth_auth_error_refreshes_through_the_edge_function),
        ("request_real_non200_exhausts_retries", test_request_real_non200_exhausts_retries_returns_none),
        ("request_real_transport_exception_exhausts_retries", test_request_real_transport_exception_exhausts_retries_returns_none),
        ("device_list_paging", test_get_device_list_pages_and_filters),
        ("device_list_unfiltered", test_get_device_list_unfiltered_returns_all),
        ("telemetry_mapping", test_fetch_device_data_maps_telemetry_and_energy),
        ("telemetry_absent", test_fetch_device_data_absent_fields_are_not_invented),
        ("nominal_pack_voltage", test_nominal_pack_voltage_variants),
        ("capacity_ah_to_kwh", test_battery_capacity_amp_hours_to_kwh),
        ("battery_rate_max", test_battery_rate_max_from_charge_current),
        ("battery_reserve_min", test_battery_reserve_min_from_settings),
        ("device_list_no_serials_terminates", test_get_device_list_terminates_when_serials_are_missing),
        ("device_list_deduplicates", test_get_device_list_deduplicates_repeated_pages),
        ("device_list_bogus_total_bounded", test_get_device_list_bogus_total_is_bounded_by_page_cap),
        ("device_detail_preserves_rated_power", test_fetch_device_detail_never_clears_a_known_rated_power),
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
