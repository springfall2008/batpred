# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk Cloud API component
# -----------------------------------------------------------------------------

"""Tests for the Sunsynk Cloud API component (``sunsynk.py``)."""

import predbat  # noqa: F401  (import first - avoids circular import: config.py does `from predbat import THIS_VERSION`)
import argparse
import asyncio
import io
import signal
import aiohttp
import pytz
from contextlib import redirect_stdout
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock, patch
from sunsynk import SunsynkAPI, test_sunsynk_api as run_sunsynk_cli_once
from sunsynk_const import SUNSYNK_REGIONS, SUNSYNK_ENDPOINTS, SUNSYNK_RETRIES, SUNSYNK_MAX_DISCOVERY_PAGES
from tests.test_infra import run_async as run_async_local, create_aiohttp_mock_response, create_aiohttp_mock_session, _make_mock_context


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
        # Most recent body-level API failure message (the `msg` field only, never a
        # credential) and whether the last discovery attempt actually reached the API -
        # mirrors the two attributes initialize() sets on the real component.
        self.last_api_error = ""
        self.discovery_ok = None
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


def test_last_api_error_populated_on_body_level_failure_and_visible_after_fetch_token():
    """Fix 2b: the API's own failure reason is captured so the CLI can show it verbatim.

    Drives the real _request/fetch_token path (auth_method='password_legacy' to avoid
    needing a real RSA public key) against a canned failure body shaped exactly like the
    one a tester actually hit: {"code": 102, "msg": "Account or password error",
    "data": null, "success": false}. Asserts last_api_error is empty beforehand, becomes
    the API's msg during _request, and is still readable after fetch_token() reports the
    login as failed - which is what lets the CLI print "Login failed: <reason>".
    """
    failed = False
    s = MockSunsynk(auth_method="password_legacy")
    if s.last_api_error != "":
        print(f"ERROR: last_api_error should start empty, got {s.last_api_error!r}")
        failed = True
    session = _session_with_request(_mock_json_response(status=200, json_data={"code": 102, "msg": "Account or password error", "data": None, "success": False}))
    with patch("sunsynk.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = session
        ok = run_async_local(s.fetch_token())
    if ok:
        print("ERROR: fetch_token should report failure for a bad-credentials body")
        failed = True
    if s.last_api_error != "Account or password error":
        print(f"ERROR: expected last_api_error 'Account or password error', got {s.last_api_error!r}")
        failed = True
    assert not failed, "test_last_api_error_populated_on_body_level_failure_and_visible_after_fetch_token"


def test_last_api_error_never_holds_a_credential():
    """The failure message is the API's `msg` field only - it must never echo the request body.

    A body-level failure logs the request body too (see debug_api), so this specifically
    checks last_api_error itself never contains the password that was sent, guarding
    against a future change accidentally assigning the wrong side of the exchange to it.
    """
    failed = False
    s = MockSunsynk(auth_method="password_legacy")
    s.password = "correct-horse-battery-staple"
    session = _session_with_request(_mock_json_response(status=200, json_data={"code": 102, "msg": "Account or password error", "data": None, "success": False}))
    with patch("sunsynk.aiohttp.ClientSession") as mock_session_class:
        mock_session_class.return_value = session
        run_async_local(s.fetch_token())
    if "correct-horse-battery-staple" in s.last_api_error:
        print(f"ERROR: last_api_error leaked the password: {s.last_api_error!r}")
        failed = True
    assert not failed, "test_last_api_error_never_holds_a_credential"


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


def test_get_device_list_distinguishes_empty_account_from_failed_discovery():
    """Fix 3: a genuinely empty account must be told apart from a discovery call that failed.

    _get() coerces both a transport/API failure and a body carrying no `infos` at all to
    a dict - but ONLY a real failure ever coerces all the way down to the empty dict {}
    (_get's failure sentinel); a successful call against an empty account still returns
    a non-empty dict with `total`/`infos` present, just empty. discovery_ok is the signal
    the caller (refresh_static, and the CLI) uses to tell those apart - this directly
    feeds the CLI's "logged in, but no inverters" vs "discovery failed" distinction.
    """
    failed = False

    async def fake_get_empty_account(endpoint_key, sn=None, params=None):
        """A successful call against an account with zero inverters."""
        return {"total": 0, "infos": []}

    s = MockSunsynk()
    with patch.object(s, "_get", side_effect=fake_get_empty_account):
        devices = run_async_local(s.get_device_list())
    if devices != []:
        print(f"ERROR: expected no devices for an empty account, got {devices}")
        failed = True
    if s.discovery_ok is not True:
        print(f"ERROR: a successful-but-empty discovery call should set discovery_ok True, got {s.discovery_ok!r}")
        failed = True

    async def fake_get_failed_call(endpoint_key, sn=None, params=None):
        """A failed call, as _get coerces it: the empty-dict failure sentinel."""
        return {}

    s2 = MockSunsynk()
    with patch.object(s2, "_get", side_effect=fake_get_failed_call):
        devices2 = run_async_local(s2.get_device_list())
    if devices2 != []:
        print(f"ERROR: expected no devices when discovery failed, got {devices2}")
        failed = True
    if s2.discovery_ok is not False:
        print(f"ERROR: a failed discovery call should leave discovery_ok False, got {s2.discovery_ok!r}")
        failed = True
    assert not failed, "test_get_device_list_distinguishes_empty_account_from_failed_discovery"


def test_fetch_device_data_maps_telemetry_and_energy():
    """Telemetry from four endpoints is flattened onto the Predbat sensor leaves."""
    failed = False
    s = MockSunsynk()

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return a realistic payload for each realtime endpoint."""
        if endpoint_key == "battery":
            return {"soc": 62, "power": -1500, "voltage": 51.2, "temp": 21.5, "capacity": 280, "chargeVolt": 56.8, "maxChargeCurrentLimit": 100, "etodayChg": 7.4, "etodayDischg": 5.1}
        if endpoint_key == "grid":
            return {"pac": -430, "etodayFrom": 3.2, "etodayTo": 1.1}
        if endpoint_key == "load":
            return {"totalPower": 900, "dailyUsed": 12.6}
        if endpoint_key == "input":
            return {"pac": 2100, "etoday": 9.8}
        return {}

    with patch.object(s, "_get", side_effect=fake_get):
        run_async_local(s.fetch_device_data("INV1"))
    values = s.device_values.get("INV1", {})
    # grid_power is negated to reach Predbat's negative-for-import convention: Sunsynk
    # reports pac negative when EXPORTING, so -430 (an export) must publish as +430. See
    # SUNSYNK_TELEMETRY_NEGATE for the live sample that confirmed it.
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


def test_grid_power_sign_matches_the_live_export_sample():
    """An exporting site publishes POSITIVE grid power, as Predbat's convention requires.

    Pinned to the live sample from inverter 2405116013 on 2026-08-20 11:25, the only kind
    that can distinguish the two signs: pac -1939 W while PV 2708 W served a 117 W load and
    charged the battery at 544 W - a 2047 W surplus whose magnitude matches - with the day's
    counters reading 0.0 kWh imported against 2.8 kWh exported. Sunsynk reports export as
    NEGATIVE, Predbat wants it positive (docs/apps-yaml.md), so this must publish +1939.

    Deliberately a high-power sample. Readings taken near the balance point say nothing:
    the same inverter read pac +19 W and +20 W against a computed surplus of ~0 W earlier
    the same morning, which points the opposite way and is pure noise.
    """
    failed = False
    s = MockSunsynk()

    async def fake_get(endpoint_key, sn=None, params=None):
        """Return the live 2026-08-20 11:25 sample for each realtime endpoint."""
        if endpoint_key == "battery":
            return {"soc": "98.0", "power": -544, "temp": "22.0", "voltage": "53.9"}
        if endpoint_key == "grid":
            return {"pac": -1939, "etodayFrom": "0.0", "etodayTo": "2.8"}
        if endpoint_key == "load":
            return {"totalPower": 117, "dailyUsed": 1.9}
        if endpoint_key == "input":
            return {"pac": 2708, "etoday": 6.4}
        return {}

    with patch.object(s, "_get", side_effect=fake_get):
        run_async_local(s.fetch_device_data("INV1"))
    values = s.device_values.get("INV1", {})
    if values.get("grid_power") != 1939.0:
        print(f"ERROR: exporting site published grid_power {values.get('grid_power')}, expected +1939.0")
        failed = True
    # The same sample also pins battery_power: negative while charging, so Predbat's
    # positive-for-discharge convention needs no flip there either.
    if values.get("battery_power") != -544.0:
        print(f"ERROR: charging battery published battery_power {values.get('battery_power')}, expected -544.0")
        failed = True
    # Guard the fixture itself: PV must exceed load plus battery charge by roughly the
    # published grid power, or this has stopped being a test of a real export.
    surplus = values.get("pv_power", 0) - values.get("load_power", 0) + values.get("battery_power", 0)
    if surplus < 1000 or abs(surplus - values.get("grid_power", 0)) > 250:
        print(f"ERROR: the pinned sample no longer reads as a clear export: surplus {surplus} W vs grid {values.get('grid_power')} W")
        failed = True
    assert not failed, "test_grid_power_sign_matches_the_live_export_sample"


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

    if not hasattr(signal, "SIGALRM"):
        import asyncio

        return run_async_local(asyncio.wait_for(coro, timeout=seconds))

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


class CLIMockSunsynk(MockSunsynk):
    """MockSunsynk with storage forced absent, matching the real standalone CLI's environment.

    ComponentBase.storage would otherwise resolve through self.base (a MagicMock here),
    returning a truthy-but-useless mock rather than None, so load_cache/save_cache would
    try to await a non-awaitable and raise. Forcing None matches mock_base.py's documented
    behaviour (components is always None for a standalone run) and lets Fix 1 keep run()
    and final() silent, exactly as they are for a real tester.
    """

    @property
    def storage(self):
        """No Storage component exists in a standalone CLI run."""
        return None


def _cli_args(auth_method="password_legacy", region="sunsynk", serial=None):
    """Build the argparse.Namespace shape test_sunsynk_api()'s CLI expects from argparse."""
    return argparse.Namespace(username="test@example.com", password="hunter2", region=region, auth_method=auth_method, serial=serial, dump_settings=False, write_test=False)


def _routed_session_factory(canned):
    """Return an aiohttp.ClientSession stand-in constructor that routes by URL suffix.

    ``canned`` maps a URL path suffix (an entry from SUNSYNK_ENDPOINTS, with any {sn}
    already substituted) to the mock response it should return. sunsynk.py's _request
    opens a fresh ClientSession per attempt, so this hands out a fresh routed session on
    every call - each one dispatches by matching the requested URL's suffix rather than
    assuming a fixed call order, so a scripted run() sequence does not need to hard-code
    exactly how many calls each stage makes.
    """

    def make_session(*args, **kwargs):
        """Build one session whose .request(...) dispatches to the matching canned response."""

        def pick(method, url, headers=None, params=None, json=None):
            """Return the canned response's context manager for the matching endpoint."""
            for suffix, response in canned.items():
                if url.endswith(suffix):
                    return _make_mock_context(response)
            raise AssertionError(f"no canned response scripted for {method} {url}")

        session = MagicMock()
        session.request = MagicMock(side_effect=pick)

        async def session_aenter(*_args):
            """Support `async with aiohttp.ClientSession(...) as session`."""
            return session

        async def session_aexit(*_args):
            """No-op session exit."""
            return None

        session.__aenter__ = session_aenter
        session.__aexit__ = session_aexit
        return session

    return make_session


def test_cli_reports_login_failure_with_the_api_reason():
    """Fix 2 (bullet 1) + Fix 2b: bad credentials must be named as a login failure, with the reason.

    This is the exact case the reporting tester hit: run() returns bare False and the old
    CLI printed all three possible causes, leaving the user to dig through Warn: lines for
    "Account or password error". The new CLI must say LOGIN outright and show that reason.
    """
    failed = False
    client = CLIMockSunsynk(auth_method="password_legacy")
    args = _cli_args(auth_method="password_legacy")
    canned = {SUNSYNK_ENDPOINTS["token_legacy"]: _mock_json_response(status=200, json_data={"code": 102, "msg": "Account or password error", "data": None, "success": False})}

    buffer = io.StringIO()
    with patch("sunsynk._build_sunsynk", return_value=client), patch("sunsynk.aiohttp.ClientSession", side_effect=_routed_session_factory(canned)), redirect_stdout(buffer):
        run_async_local(run_sunsynk_cli_once(args))
    output = buffer.getvalue()

    if "LOGIN FAILED" not in output:
        print(f"ERROR: expected the CLI to name login as the failed stage:\n{output}")
        failed = True
    if "Account or password error" not in output:
        print(f"ERROR: expected the API's own failure reason in the output:\n{output}")
        failed = True
    if "login failed, no inverters were discovered, or the first telemetry poll" in output:
        print(f"ERROR: the old ambiguous three-in-one message should be gone:\n{output}")
        failed = True
    assert not failed, "test_cli_reports_login_failure_with_the_api_reason"


def test_cli_reports_empty_account_and_mentions_serial_filter():
    """Fix 2 (bullet 2) + Fix 3: login succeeding but discovery finding nothing is named explicitly.

    Also covers the --serial pitfall this bullet calls out: a serial filter that matches
    no discovered inverter is indistinguishable from a genuinely empty account unless the
    CLI says so, so this drives it WITH --serial set to a serial the account does not have.
    """
    failed = False
    client = CLIMockSunsynk(auth_method="password_legacy", inverter_sn=["ZZZZ"])
    args = _cli_args(auth_method="password_legacy", serial="ZZZZ")
    canned = {
        SUNSYNK_ENDPOINTS["token_legacy"]: _mock_json_response(status=200, json_data={"success": True, "data": {"access_token": "tok-123"}}),
        SUNSYNK_ENDPOINTS["inverter_list"]: _mock_json_response(status=200, json_data={"success": True, "data": {"total": 1, "infos": [{"sn": "OTHER1"}]}}),
    }

    buffer = io.StringIO()
    with patch("sunsynk._build_sunsynk", return_value=client), patch("sunsynk.aiohttp.ClientSession", side_effect=_routed_session_factory(canned)), redirect_stdout(buffer):
        run_async_local(run_sunsynk_cli_once(args))
    output = buffer.getvalue()

    if "LOGIN FAILED" in output:
        print(f"ERROR: login succeeded, must not be reported as a login failure:\n{output}")
        failed = True
    if "no inverters" not in output.lower():
        print(f"ERROR: expected the CLI to say the account returned no inverters:\n{output}")
        failed = True
    if "--serial" not in output or "ZZZZ" not in output:
        print(f"ERROR: expected the CLI to mention the --serial filter that matched nothing:\n{output}")
        failed = True
    assert not failed, "test_cli_reports_empty_account_and_mentions_serial_filter"


def test_cli_reports_telemetry_failure_naming_the_serials():
    """Fix 2 (bullet 3): login and discovery both worked, only the telemetry poll came back empty.

    Discovery finds INV1; static detail and settings both read successfully; all four
    realtime telemetry endpoints then fail, so refresh_live()'s got_any stays False and
    run() defers startup. The CLI must say discovery worked and name the serial(s) found,
    not repeat the login/discovery messaging.
    """
    failed = False
    client = CLIMockSunsynk(auth_method="password_legacy")
    args = _cli_args(auth_method="password_legacy")
    ok_token = _mock_json_response(status=200, json_data={"success": True, "data": {"access_token": "tok-123"}})
    ok_one_inverter = _mock_json_response(status=200, json_data={"success": True, "data": {"total": 1, "infos": [{"sn": "INV1"}]}})
    ok_detail = _mock_json_response(status=200, json_data={"success": True, "data": {"ratePower": 5000}})
    ok_settings = _mock_json_response(status=200, json_data={"success": True, "data": {"batteryLowCap": "10"}})
    fail_telemetry = _mock_json_response(status=200, json_data={"success": False, "msg": "inverter offline"})
    canned = {
        SUNSYNK_ENDPOINTS["token_legacy"]: ok_token,
        SUNSYNK_ENDPOINTS["inverter_list"]: ok_one_inverter,
        SUNSYNK_ENDPOINTS["inverter_detail"].format(sn="INV1"): ok_detail,
        SUNSYNK_ENDPOINTS["settings_read"].format(sn="INV1"): ok_settings,
        SUNSYNK_ENDPOINTS["battery"].format(sn="INV1"): fail_telemetry,
        SUNSYNK_ENDPOINTS["grid"].format(sn="INV1"): fail_telemetry,
        SUNSYNK_ENDPOINTS["load"].format(sn="INV1"): fail_telemetry,
        SUNSYNK_ENDPOINTS["input"].format(sn="INV1"): fail_telemetry,
    }

    buffer = io.StringIO()
    with patch("sunsynk._build_sunsynk", return_value=client), patch("sunsynk.aiohttp.ClientSession", side_effect=_routed_session_factory(canned)), redirect_stdout(buffer):
        run_async_local(run_sunsynk_cli_once(args))
    output = buffer.getvalue()

    if "LOGIN FAILED" in output or "no inverters" in output.lower():
        print(f"ERROR: login and discovery both worked, must not be reported as failed:\n{output}")
        failed = True
    if "telemetry" not in output.lower():
        print(f"ERROR: expected the CLI to name the telemetry poll as the failed stage:\n{output}")
        failed = True
    if "INV1" not in output:
        print(f"ERROR: expected the CLI to name the discovered serial(s):\n{output}")
        failed = True
    assert not failed, "test_cli_reports_telemetry_failure_naming_the_serials"


def test_battery_rate_max_prefers_a_populated_current_field():
    """A zero maxChargeCurrentLimit must not mask a populated chargeCurrentLimit.

    Confirmed live on inverter 2405116013: maxChargeCurrentLimit 0.0 alongside
    chargeCurrentLimit 216.0. Reading only the max field derived a rate of 0, which made
    automatic_config skip battery_rate_max and left Predbat with no charge-rate limit.
    """
    failed = False
    s = MockSunsynk()
    # The live shape: max is zero, the real limit is in chargeCurrentLimit.
    s.device_values["INV1"] = {"chargeCurrentLimit": 216.0, "maxChargeCurrentLimit": 0.0, "chargeVolt": 58.4}
    rate = s.battery_rate_max("INV1")
    if abs(rate - 216.0 * 51.2) > 1:
        print(f"ERROR: expected 216A x 51.2V = 11059W, got {rate}")
        failed = True
    # The other way round must still work - unknown which field other firmware populates.
    s.device_values["INV2"] = {"chargeCurrentLimit": 0.0, "maxChargeCurrentLimit": 100.0, "chargeVolt": 56.8}
    if abs(s.battery_rate_max("INV2") - 100.0 * 51.2) > 1:
        print(f"ERROR: fallback to maxChargeCurrentLimit failed, got {s.battery_rate_max('INV2')}")
        failed = True
    # Both zero is genuinely unknown and must stay 0 rather than be invented.
    s.device_values["INV3"] = {"chargeCurrentLimit": 0.0, "maxChargeCurrentLimit": 0.0, "chargeVolt": 58.4}
    if s.battery_rate_max("INV3") != 0:
        print(f"ERROR: no current limit should derive 0, got {s.battery_rate_max('INV3')}")
        failed = True
    assert not failed, "test_battery_rate_max_prefers_a_populated_current_field"


def test_nominal_pack_voltage_across_the_lifepo4_charge_window():
    """Cell count must come out right for any per-cell charge target in the real range.

    A single assumed volts-per-cell is wrong at both ends: 3.55 turns a 24-cell pack
    charged at 3.65V/cell into 25 cells, and 3.65 turns a 16-cell pack charged at
    3.45V/cell into 15. Both errors are silent and land in soc_max.
    """
    failed = False
    s = MockSunsynk()
    # (charge target, expected cells) across 3.45 / 3.55 / 3.65 V per cell.
    cases = [(55.2, 16), (56.8, 16), (58.4, 16), (82.8, 24), (85.2, 24), (87.6, 24), (110.4, 32), (113.6, 32), (116.8, 32)]
    for charge_volts, cells in cases:
        expect = cells * 3.2
        got = s.nominal_pack_voltage(charge_volts)
        if abs(got - expect) > 0.01:
            print(f"ERROR: chargeVolt {charge_volts} gave {got}V ({got / 3.2:.0f} cells), expected {expect}V ({cells} cells)")
            failed = True
    # A target matching no standard stack must refuse rather than guess.
    if s.nominal_pack_voltage(200.0) != 0:
        print(f"ERROR: an unmatched charge target should derive 0, got {s.nominal_pack_voltage(200.0)}")
        failed = True
    # The explicit override still wins over any inference.
    s.battery_nominal_voltage = 48.0
    if s.nominal_pack_voltage(58.4) != 48.0:
        print("ERROR: sunsynk_battery_nominal_voltage override was ignored")
        failed = True
    assert not failed, "test_nominal_pack_voltage_across_the_lifepo4_charge_window"


def test_inverter_limit_is_the_rating_and_export_limit_is_the_cap():
    """pvMaxLimit caps EXPORT, not the inverter's output, despite its app label.

    Sunsynk's documentation confirms the "Inverter Power Limiter" control is an export cap,
    which is why the app shows the same value on both the System Mode and Grid Settings
    screens. The inverter can still deliver its full ratePower to the house, so reducing
    inverter_limit by it would under-rate the inverter for self-consumption.
    """
    failed = False
    s = MockSunsynk()
    s.device_rated_power["INV1"] = 8000.0
    s.device_settings["INV1"] = {"pvMaxLimit": "7000"}
    if s.inverter_limit("INV1") != 8000.0:
        print(f"ERROR: inverter_limit should be the 8000W rating, got {s.inverter_limit('INV1')}")
        failed = True
    if s.export_limit("INV1") != 7000.0:
        print(f"ERROR: export_limit should be the 7000W cap, got {s.export_limit('INV1')}")
        failed = True
    # Export can never exceed what the inverter can produce, whatever the setting allows.
    s.device_rated_power["INV2"] = 3600.0
    s.device_settings["INV2"] = {"pvMaxLimit": "16000"}
    if s.export_limit("INV2") != 3600.0:
        print(f"ERROR: export must be bounded by the rating, got {s.export_limit('INV2')}")
        failed = True
    # A G98 site: a 3.68kW export cap behind a much larger inverter must survive intact.
    s.device_rated_power["INV3"] = 8000.0
    s.device_settings["INV3"] = {"pvMaxLimit": "3680"}
    if s.export_limit("INV3") != 3680.0 or s.inverter_limit("INV3") != 8000.0:
        print(f"ERROR: G98 case wrong - export {s.export_limit('INV3')}, inverter {s.inverter_limit('INV3')}")
        failed = True
    if s.inverter_limit("UNKNOWN") != 0 or s.export_limit("UNKNOWN") != 0:
        print("ERROR: an unknown serial should derive 0 for both")
        failed = True
    assert not failed, "test_inverter_limit_is_the_rating_and_export_limit_is_the_cap"


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
        ("grid_power_sign", test_grid_power_sign_matches_the_live_export_sample),
        ("telemetry_absent", test_fetch_device_data_absent_fields_are_not_invented),
        ("nominal_pack_voltage", test_nominal_pack_voltage_variants),
        ("capacity_ah_to_kwh", test_battery_capacity_amp_hours_to_kwh),
        ("battery_rate_max", test_battery_rate_max_from_charge_current),
        ("rate_max_field_priority", test_battery_rate_max_prefers_a_populated_current_field),
        ("pack_voltage_window", test_nominal_pack_voltage_across_the_lifepo4_charge_window),
        ("inverter_limit_vs_export", test_inverter_limit_is_the_rating_and_export_limit_is_the_cap),
        ("battery_reserve_min", test_battery_reserve_min_from_settings),
        ("device_list_no_serials_terminates", test_get_device_list_terminates_when_serials_are_missing),
        ("device_list_deduplicates", test_get_device_list_deduplicates_repeated_pages),
        ("device_list_bogus_total_bounded", test_get_device_list_bogus_total_is_bounded_by_page_cap),
        ("device_detail_preserves_rated_power", test_fetch_device_detail_never_clears_a_known_rated_power),
        ("last_api_error_from_body_failure", test_last_api_error_populated_on_body_level_failure_and_visible_after_fetch_token),
        ("last_api_error_no_credential_leak", test_last_api_error_never_holds_a_credential),
        ("device_list_empty_vs_failed", test_get_device_list_distinguishes_empty_account_from_failed_discovery),
        ("cli_login_failure_reason", test_cli_reports_login_failure_with_the_api_reason),
        ("cli_empty_account_serial_hint", test_cli_reports_empty_account_and_mentions_serial_filter),
        ("cli_telemetry_failure_names_serials", test_cli_reports_telemetry_failure_naming_the_serials),
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
