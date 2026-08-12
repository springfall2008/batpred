# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test DEYE Cloud API auth headers, token fetch and _post transport
# -----------------------------------------------------------------------------

"""Tests for DEYE Cloud auth headers, token fetch and the ``_post`` transport (``deye.py``)."""

import hashlib
from unittest.mock import patch
from tests.test_infra import run_async, create_aiohttp_mock_response, create_aiohttp_mock_session
from tests.test_deye_api import MockDeye
from deye_const import DEYE_DEBUG_MAX_CHARS


def test_sha256_and_login_payload():
    """Password hashed lower-hex SHA-256; @ picks email key else username."""
    failed = False
    d = MockDeye()
    if d._sha256("secret") != hashlib.sha256(b"secret").hexdigest().lower():
        print("ERROR: sha256 wrong")
        failed = True
    if d._login_payload("a@b.com") != {"email": "a@b.com"}:
        print("ERROR: email payload wrong")
        failed = True
    if d._login_payload("bob") != {"username": "bob"}:
        print("ERROR: username payload wrong")
        failed = True
    assert not failed, "test_sha256_and_login_payload"


def test_auth_headers_bearer():
    """Auth header carries the current access token as a Bearer."""
    failed = False
    d = MockDeye()
    d.access_token = "tok-123"
    h = d._auth_headers()
    if h.get("Authorization") != "Bearer tok-123":
        print(f"ERROR: header {h}")
        failed = True
    assert not failed, "test_auth_headers_bearer"


def test_post_401_refreshes_then_retries():
    """A 401 triggers handle_oauth_401 then a successful retry."""
    failed = False
    d = MockDeye(auth_method="oauth")
    d.access_token = "old"
    resp_401 = create_aiohttp_mock_response(status=401, json_data={"success": False})
    resp_ok = create_aiohttp_mock_response(status=200, json_data={"success": True, "data": 1})
    session = create_aiohttp_mock_session([resp_401, resp_ok])

    async def fake_refresh():
        """Simulate a successful OAuth refresh by rotating the access token."""
        d.access_token = "new"
        return True

    with patch("aiohttp.ClientSession", return_value=session):
        with patch.object(d, "handle_oauth_401", side_effect=fake_refresh):
            out = run_async(d._post("station_list", {}))
    if not out.get("success"):
        print(f"ERROR: expected success after refresh, got {out}")
        failed = True
    if d.access_token != "new":
        print("ERROR: token not refreshed")
        failed = True
    assert not failed, "test_post_401_refreshes_then_retries"


def test_post_401_app_credentials_recovers_via_fetch_token():
    """In app_credentials mode handle_oauth_401 is a no-op (oauth-only); recovery falls back to fetch_token so an expired token doesn't pin the component until a restart."""
    failed = False
    d = MockDeye(auth_method="app_credentials")
    d.access_token = "old"
    resp_401 = create_aiohttp_mock_response(status=401, json_data={"success": False})
    resp_ok = create_aiohttp_mock_response(status=200, json_data={"success": True, "data": 1})
    session = create_aiohttp_mock_session([resp_401, resp_ok])
    calls = {"fetch_token": 0}

    async def fake_fetch_token():
        """Simulate a fresh app-credentials login rotating the access token."""
        calls["fetch_token"] += 1
        d.access_token = "new"
        return True

    with patch("aiohttp.ClientSession", return_value=session):
        with patch.object(d, "fetch_token", side_effect=fake_fetch_token):
            out = run_async(d._post("station_list", {}))
    if not out.get("success"):
        print(f"ERROR: expected success after re-authenticate, got {out}")
        failed = True
    if calls["fetch_token"] != 1:
        print(f"ERROR: expected fetch_token to be called once, got {calls['fetch_token']}")
        failed = True
    if d.access_token != "new":
        print(f"ERROR: token not refreshed via fetch_token, got {d.access_token}")
        failed = True
    assert not failed, "test_post_401_app_credentials_recovers_via_fetch_token"


def test_is_auth_error_body():
    """A 200-OK body reporting a bad token is classed as auth; other failures and successes are not."""
    failed = False
    d = MockDeye()
    auth_bodies = [
        {"success": False, "msg": "auth invalid token"},
        {"success": False, "msg": "Auth Invalid Token"},
        {"success": False, "msg": "token expired"},
        {"success": False, "msg": "Unauthorized"},
        {"success": False, "msg": "", "code": "INVALID_TOKEN"},
    ]
    for body in auth_bodies:
        if not d.is_auth_error_body(body):
            print(f"ERROR: expected auth error for {body}")
            failed = True
    other_bodies = [
        {"success": True, "msg": "auth invalid token"},  # success wins, never refresh
        {"success": False, "msg": "device offline"},
        {"success": False},
        {},
        None,
        "not-a-dict",
    ]
    for body in other_bodies:
        if d.is_auth_error_body(body):
            print(f"ERROR: did not expect auth error for {body}")
            failed = True
    assert not failed, "test_is_auth_error_body"


def test_post_body_auth_error_refreshes_then_retries():
    """A 200 + {"success": false, "msg": "auth invalid token"} refreshes the token and retries."""
    failed = False
    d = MockDeye(auth_method="oauth")
    d.access_token = "old"
    resp_bad = create_aiohttp_mock_response(status=200, json_data={"success": False, "msg": "auth invalid token"})
    resp_ok = create_aiohttp_mock_response(status=200, json_data={"success": True, "stationList": [{"id": 7}]})
    session = create_aiohttp_mock_session([resp_bad, resp_ok])

    async def fake_refresh():
        """Simulate a successful OAuth refresh by rotating the access token."""
        d.access_token = "new"
        return True

    with patch("aiohttp.ClientSession", return_value=session):
        with patch.object(d, "handle_oauth_401", side_effect=fake_refresh):
            out = run_async(d._post("station_list", {}))
    if not out.get("success"):
        print(f"ERROR: expected success after body-auth refresh, got {out}")
        failed = True
    if d.access_token != "new":
        print(f"ERROR: token not refreshed, got {d.access_token}")
        failed = True
    assert not failed, "test_post_body_auth_error_refreshes_then_retries"


def test_get_station_ids_recovers_from_body_auth_error():
    """The reported failure path end to end: station/list returns the ids after the retry."""
    failed = False
    d = MockDeye(auth_method="oauth")
    d.access_token = "old"
    resp_bad = create_aiohttp_mock_response(status=200, json_data={"success": False, "msg": "auth invalid token"})
    resp_ok = create_aiohttp_mock_response(status=200, json_data={"success": True, "stationList": [{"id": 42}]})
    session = create_aiohttp_mock_session([resp_bad, resp_ok])

    async def fake_refresh():
        """Simulate a successful OAuth refresh by rotating the access token."""
        d.access_token = "new"
        return True

    with patch("aiohttp.ClientSession", return_value=session):
        with patch.object(d, "handle_oauth_401", side_effect=fake_refresh):
            ids = run_async(d.get_station_ids())
    if ids != [42]:
        print(f"ERROR: expected [42] after refresh, got {ids}")
        failed = True
    assert not failed, "test_get_station_ids_recovers_from_body_auth_error"


def test_post_body_auth_error_refreshes_only_once():
    """If the refreshed token is still rejected the body is returned, not retried forever."""
    failed = False
    d = MockDeye(auth_method="oauth")
    d.access_token = "old"
    bad = {"success": False, "msg": "auth invalid token"}
    session = create_aiohttp_mock_session([create_aiohttp_mock_response(status=200, json_data=bad) for _ in range(4)])
    calls = {"refresh": 0}

    async def fake_refresh():
        """Simulate a refresh that hands back a token DEYE still rejects."""
        calls["refresh"] += 1
        return True

    with patch("aiohttp.ClientSession", return_value=session):
        with patch.object(d, "handle_oauth_401", side_effect=fake_refresh):
            out = run_async(d._post("station_list", {}))
    if out != bad:
        print(f"ERROR: expected the failing body back, got {out}")
        failed = True
    if calls["refresh"] != 1:
        print(f"ERROR: expected exactly one refresh, got {calls['refresh']}")
        failed = True
    assert not failed, "test_post_body_auth_error_refreshes_only_once"


def test_post_body_auth_error_app_credentials_logs_in_again():
    """In app_credentials mode a body-level auth error re-authenticates via fetch_token."""
    failed = False
    d = MockDeye(auth_method="app_credentials")
    d.access_token = "old"
    resp_bad = create_aiohttp_mock_response(status=200, json_data={"success": False, "msg": "auth invalid token"})
    resp_ok = create_aiohttp_mock_response(status=200, json_data={"success": True})
    session = create_aiohttp_mock_session([resp_bad, resp_ok])
    calls = {"fetch_token": 0}

    async def fake_fetch_token():
        """Simulate a fresh app-credentials login rotating the access token."""
        calls["fetch_token"] += 1
        d.access_token = "new"
        return True

    with patch("aiohttp.ClientSession", return_value=session):
        with patch.object(d, "fetch_token", side_effect=fake_fetch_token):
            out = run_async(d._post("station_list", {}))
    if not out.get("success"):
        print(f"ERROR: expected success after re-authenticate, got {out}")
        failed = True
    if calls["fetch_token"] != 1:
        print(f"ERROR: expected one fetch_token call, got {calls['fetch_token']}")
        failed = True
    assert not failed, "test_post_body_auth_error_app_credentials_logs_in_again"


def test_post_non_auth_failure_is_not_retried():
    """A non-auth body failure is returned to the caller without a refresh."""
    failed = False
    d = MockDeye(auth_method="oauth")
    body = {"success": False, "msg": "device offline"}
    session = create_aiohttp_mock_session([create_aiohttp_mock_response(status=200, json_data=body)])
    calls = {"refresh": 0}

    async def fake_refresh():
        """Track any unexpected refresh attempt."""
        calls["refresh"] += 1
        return True

    with patch("aiohttp.ClientSession", return_value=session):
        with patch.object(d, "handle_oauth_401", side_effect=fake_refresh):
            out = run_async(d._post("station_list", {}))
    if out != body:
        print(f"ERROR: expected the body passed through, got {out}")
        failed = True
    if calls["refresh"] != 0:
        print(f"ERROR: expected no refresh for a non-auth failure, got {calls['refresh']}")
        failed = True
    assert not failed, "test_post_non_auth_failure_is_not_retried"


def test_get_body_auth_error_refreshes_then_retries():
    """The GET path (order polling) also refreshes on a body-level auth error."""
    failed = False
    d = MockDeye(auth_method="oauth")
    d.access_token = "old"
    resp_bad = create_aiohttp_mock_response(status=200, json_data={"success": False, "msg": "auth invalid token"})
    resp_ok = create_aiohttp_mock_response(status=200, json_data={"success": True})
    session = create_aiohttp_mock_session([resp_bad, resp_ok])
    d.pending_orders["SN1"] = "order-1"

    async def fake_refresh():
        """Simulate a successful OAuth refresh by rotating the access token."""
        d.access_token = "new"
        return True

    with patch("aiohttp.ClientSession", return_value=session):
        with patch.object(d, "handle_oauth_401", side_effect=fake_refresh):
            status = run_async(d.poll_order("SN1"))
    if status != "success":
        print(f"ERROR: expected success after GET refresh, got {status}")
        failed = True
    if d.access_token != "new":
        print(f"ERROR: token not refreshed on GET, got {d.access_token}")
        failed = True
    assert not failed, "test_get_body_auth_error_refreshes_then_retries"


def test_get_401_still_refreshes_then_retries():
    """The pre-existing GET 401 path keeps working after the retry-loop rewrite."""
    failed = False
    d = MockDeye(auth_method="oauth")
    resp_401 = create_aiohttp_mock_response(status=401, json_data={"success": False})
    resp_ok = create_aiohttp_mock_response(status=200, json_data={"success": True, "value": 3})
    session = create_aiohttp_mock_session([resp_401, resp_ok])

    async def fake_refresh():
        """Simulate a successful OAuth refresh."""
        return True

    with patch("aiohttp.ClientSession", return_value=session):
        with patch.object(d, "handle_oauth_401", side_effect=fake_refresh):
            out = run_async(d._get("/order/abc"))
    if out.get("value") != 3:
        print(f"ERROR: expected the retried body, got {out}")
        failed = True
    assert not failed, "test_get_401_still_refreshes_then_retries"


def test_get_401_refresh_failure_returns_empty():
    """A GET 401 whose refresh fails returns {} rather than retrying blindly."""
    failed = False
    d = MockDeye(auth_method="oauth")
    resp_401 = create_aiohttp_mock_response(status=401, json_data={"success": False})
    session = create_aiohttp_mock_session([resp_401])

    async def fake_refresh():
        """Simulate a failed OAuth refresh."""
        return False

    with patch("aiohttp.ClientSession", return_value=session):
        with patch.object(d, "handle_oauth_401", side_effect=fake_refresh):
            out = run_async(d._get("/order/abc"))
    if out != {}:
        print(f"ERROR: expected {{}} when refresh fails, got {out}")
        failed = True
    assert not failed, "test_get_401_refresh_failure_returns_empty"


def test_debug_api_on_by_default_and_can_be_disabled():
    """Tracing is on while DEYE beds in, and setting api_debug False silences it."""
    failed = False
    d = MockDeye()
    if not d.api_debug:
        print("ERROR: api_debug should default to True while DEYE is bedding in")
        failed = True
    d.debug_api("POST", "/station/list", {"a": 1})
    if not any("DEYE API" in m for m in d.log_messages):
        print(f"ERROR: nothing traced while enabled: {d.log_messages}")
        failed = True
    d.api_debug = False
    d.log_messages.clear()
    d.debug_api("POST", "/station/list", {"a": 1})
    if d.log_messages:
        print(f"ERROR: traced while disabled: {d.log_messages}")
        failed = True
    assert not failed, "test_debug_api_on_by_default_and_can_be_disabled"


def test_debug_api_redacts_credentials():
    """Traced bodies mask tokens and passwords at any nesting depth."""
    failed = False
    d = MockDeye()
    d.debug_api("RESP", "/account/token", {"accessToken": "secret-tok", "appSecret": "s3cr3t", "nested": [{"password": "pw", "deviceSn": "SN1"}]})
    trace = " ".join(d.log_messages)
    for leaked in ("secret-tok", "s3cr3t", '"pw"'):
        if leaked in trace:
            print(f"ERROR: {leaked} leaked into the trace: {trace}")
            failed = True
    if "SN1" not in trace:
        print(f"ERROR: non-secret field was lost: {trace}")
        failed = True
    assert not failed, "test_debug_api_redacts_credentials"


def test_debug_api_truncates_long_bodies():
    """A huge body is truncated so the log stays usable."""
    failed = False
    d = MockDeye()
    d.debug_api("RESP", "/device/latest", {"dataList": [{"key": f"k{i}", "value": "x" * 50} for i in range(500)]})
    trace = " ".join(d.log_messages)
    if "truncated" not in trace:
        print("ERROR: expected the body to be truncated")
        failed = True
    if len(trace) > DEYE_DEBUG_MAX_CHARS + 500:
        print(f"ERROR: trace too long at {len(trace)} chars")
        failed = True
    assert not failed, "test_debug_api_truncates_long_bodies"


def test_post_traces_request_and_response_when_debug():
    """_post traces both the outgoing body and the parsed response when debugging."""
    failed = False
    d = MockDeye()
    resp = create_aiohttp_mock_response(status=200, json_data={"success": False, "msg": "config point not supported"})
    session = create_aiohttp_mock_session([resp])
    with patch("aiohttp.ClientSession", return_value=session):
        run_async(d._post("config_battery", {"deviceSn": "SN1"}))
    trace = " ".join(d.log_messages)
    if "POST" not in trace or "/config/battery" not in trace or "SN1" not in trace:
        print(f"ERROR: request not traced: {trace}")
        failed = True
    if "RESP" not in trace or "config point not supported" not in trace:
        print(f"ERROR: response not traced: {trace}")
        failed = True
    assert not failed, "test_post_traces_request_and_response_when_debug"


def run_deye_oauth_tests(my_predbat):
    """Run all DEYE auth tests."""
    failed = False
    for name, fn in [
        ("sha256_login_payload", test_sha256_and_login_payload),
        ("auth_headers_bearer", test_auth_headers_bearer),
        ("post_401_refresh_retry", test_post_401_refreshes_then_retries),
        ("post_401_app_credentials_recover", test_post_401_app_credentials_recovers_via_fetch_token),
        ("is_auth_error_body", test_is_auth_error_body),
        ("post_body_auth_refresh_retry", test_post_body_auth_error_refreshes_then_retries),
        ("station_ids_recover_body_auth", test_get_station_ids_recovers_from_body_auth_error),
        ("post_body_auth_refresh_once", test_post_body_auth_error_refreshes_only_once),
        ("post_body_auth_app_credentials", test_post_body_auth_error_app_credentials_logs_in_again),
        ("post_non_auth_failure_passthrough", test_post_non_auth_failure_is_not_retried),
        ("get_body_auth_refresh_retry", test_get_body_auth_error_refreshes_then_retries),
        ("get_401_refresh_retry", test_get_401_still_refreshes_then_retries),
        ("get_401_refresh_failure_empty", test_get_401_refresh_failure_returns_empty),
        ("debug_api_default_and_disable", test_debug_api_on_by_default_and_can_be_disabled),
        ("debug_api_redacts", test_debug_api_redacts_credentials),
        ("debug_api_truncates", test_debug_api_truncates_long_bodies),
        ("post_traces_request_response", test_post_traces_request_and_response_when_debug),
    ]:
        try:
            if fn():
                print(f"  FAILED: deye_oauth.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in deye_oauth.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
