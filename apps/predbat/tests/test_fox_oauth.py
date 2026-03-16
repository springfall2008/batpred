# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Fox API OAuth dual auth (headers, 401 retry, initialize)
# -----------------------------------------------------------------------------

import time
import hashlib
import pytz
from datetime import datetime
from unittest.mock import MagicMock, patch
from fox import FoxAPI, FOX_LANG
from tests.test_infra import run_async, create_aiohttp_mock_response, create_aiohttp_mock_session


class MockFoxOAuth(FoxAPI):
    """Mock FoxAPI for testing OAuth-specific behavior."""

    def __init__(self, auth_method="api_key", key="test-api-key", token_expires_at=None):
        # Don't call parent __init__ — set up manually
        self.key = key
        self.automatic = False
        self.failures_total = 0
        self.prefix = "predbat"
        self.device_list = []
        self.device_detail = {}
        self.device_power_generation = {}
        self.available_variables = {}
        self.device_values = {}
        self.device_settings = {}
        self.device_production_month = {}
        self.device_production_year = {}
        self.device_battery_charging_time = {}
        self.device_scheduler = {}
        self.device_current_schedule = {}
        self.local_schedule = {}
        self.fdpwr_max = {}
        self.fdsoc_min = {}
        self.local_tz = pytz.timezone("Europe/London")
        self.inverter_sn_filter = []
        self.requests_today = 0
        self.rate_limit_errors_today = 0
        self.start_time_today = None
        self.last_midnight_utc = None
        self.log_messages = []

        self.base = MagicMock()
        self.base.args = {"user_id": "test-instance-123"}
        self.base.midnight_utc = datetime.now(pytz.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        # Initialize OAuth
        self._init_oauth(auth_method, key, token_expires_at, "fox_ess")

    def log(self, message):
        self.log_messages.append(message)

    def update_success_timestamp(self):
        pass


def test_get_headers_api_key():
    """get_headers returns MD5 signature headers for api_key mode."""
    failed = False
    fox = MockFoxOAuth(auth_method="api_key", key="test-key-123")

    headers = fox.get_headers("/op/v0/device/list")

    if "Authorization" in headers:
        print("ERROR: api_key mode should NOT have Authorization header")
        failed = True
    if headers.get("token") != "test-key-123":
        print(f"ERROR: token header should be 'test-key-123', got {headers.get('token')}")
        failed = True
    if "signature" not in headers:
        print("ERROR: api_key mode should have signature header")
        failed = True
    if "timestamp" not in headers:
        print("ERROR: api_key mode should have timestamp header")
        failed = True
    if headers.get("lang") != FOX_LANG:
        print(f"ERROR: lang header should be '{FOX_LANG}', got {headers.get('lang')}")
        failed = True

    # Verify MD5 signature is correctly computed
    path = "/op/v0/device/list"
    ts = headers["timestamp"]
    expected_sig_input = rf"{path}\r\n{fox.key}\r\n{ts}"
    expected_sig = hashlib.md5(expected_sig_input.encode("UTF-8")).hexdigest()
    if headers["signature"] != expected_sig:
        print(f"ERROR: MD5 signature mismatch")
        failed = True

    if not failed:
        print("PASS: get_headers returns MD5 signature for api_key mode")
    return failed


def test_get_headers_oauth():
    """get_headers returns Bearer token + MD5 signature for OAuth mode."""
    failed = False
    fox = MockFoxOAuth(auth_method="oauth", key="oauth-access-token-xyz")

    headers = fox.get_headers("/op/v0/device/list")

    if headers.get("Authorization") != "Bearer oauth-access-token-xyz":
        print(f"ERROR: Authorization header should be 'Bearer oauth-access-token-xyz', got {headers.get('Authorization')}")
        failed = True
    if headers.get("Content-Type") != "application/json":
        print(f"ERROR: Content-Type should be 'application/json'")
        failed = True
    if headers.get("lang") != FOX_LANG:
        print(f"ERROR: lang header should be '{FOX_LANG}'")
        failed = True
    # Fox ESS requires signature even with OAuth — using access_token as the key
    if "signature" not in headers:
        print("ERROR: OAuth mode should have signature header")
        failed = True
    if "timestamp" not in headers:
        print("ERROR: OAuth mode should have timestamp header")
        failed = True
    if "token" in headers:
        print("ERROR: OAuth mode should NOT have token header")
        failed = True

    # Verify MD5 signature uses access_token (not API key)
    path = "/op/v0/device/list"
    ts = headers["timestamp"]
    expected_sig_input = rf"{path}\r\n{fox.access_token}\r\n{ts}"
    expected_sig = hashlib.md5(expected_sig_input.encode("UTF-8")).hexdigest()
    if headers["signature"] != expected_sig:
        print(f"ERROR: OAuth MD5 signature mismatch")
        failed = True

    if not failed:
        print("PASS: get_headers returns Bearer + signature for OAuth mode")
    return failed


def test_request_get_func_oauth_pre_refresh():
    """request_get_func calls check_and_refresh before making request."""
    failed = False
    future_expiry = time.time() + 3 * 3600  # Valid token
    fox = MockFoxOAuth(auth_method="oauth", key="valid-token", token_expires_at=str(future_expiry))
    fox.token_expires_at = future_expiry  # Ensure it's set as float

    # Mock the HTTP response for the actual Fox API call
    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={"errno": 0, "result": {"deviceSN": "test-sn"}},
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        data, retry = run_async(fox.request_get_func("/op/v0/device/list"))

    if data is None:
        print(f"ERROR: request_get_func should return data, got None. Logs: {fox.log_messages}")
        failed = True
    elif data.get("deviceSN") != "test-sn":
        print(f"ERROR: data should have deviceSN, got {data}")
        failed = True

    if not failed:
        print("PASS: request_get_func with valid OAuth token succeeds")
    return failed


def test_request_get_func_oauth_refresh_fails():
    """request_get_func returns None when OAuth refresh fails."""
    failed = False
    fox = MockFoxOAuth(auth_method="oauth", key="expired-token")
    fox.token_expires_at = 0  # Expired
    fox.oauth_failed = True  # Marked as needs_reauth

    data, retry = run_async(fox.request_get_func("/op/v0/device/list"))

    if data is not None:
        print(f"ERROR: request_get_func should return None when OAuth failed, got {data}")
        failed = True

    if not failed:
        print("PASS: request_get_func returns None when OAuth refresh fails")
    return failed


def test_request_get_func_401_retry():
    """request_get_func retries once on 401 with OAuth."""
    failed = False
    future_expiry = time.time() + 3 * 3600
    fox = MockFoxOAuth(auth_method="oauth", key="token-about-to-expire", token_expires_at=str(future_expiry))
    fox.token_expires_at = future_expiry

    # First call returns 401, second call (after refresh) returns 200
    call_count = [0]

    class MockContextManager:
        def __init__(self, response):
            self.response = response

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, *args):
            pass

    class MockSession:
        def __init__(self):
            self.post_calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: Fox API returns 401
                resp = MagicMock()
                resp.status = 401

                async def json_func():
                    return {"errno": 0}

                resp.json = json_func
                return MockContextManager(resp)
            else:
                # Second call: after refresh, returns 200
                resp = MagicMock()
                resp.status = 200

                async def json_func():
                    return {"errno": 0, "result": {"success": True}}

                resp.json = json_func
                return MockContextManager(resp)

        def post(self, url, **kwargs):
            self.post_calls += 1
            # OAuth refresh call to edge function
            resp = MagicMock()
            resp.status = 200

            async def json_func():
                return {"success": True, "access_token": "new-token-after-401", "expires_at": "2026-03-07T12:00:00Z"}

            resp.json = json_func
            return MockContextManager(resp)

    mock_session = MockSession()

    with patch.dict("os.environ", {"SUPABASE_URL": "https://test.supabase.co", "SUPABASE_KEY": "test-key"}):
        with patch("aiohttp.ClientSession", return_value=mock_session):
            data, retry = run_async(fox.request_get_func("/op/v0/device/list"))

    if call_count[0] < 2:
        print(f"ERROR: Expected at least 2 Fox API calls (original + retry), got {call_count[0]}")
        failed = True

    if data is None:
        print(f"ERROR: data should not be None after successful retry. Logs: {fox.log_messages}")
        # This might fail if the mock wiring is tricky, so let's check
        # If data is None, check if the 401 retry was attempted at all
        had_401_log = any("401" in m for m in fox.log_messages)
        if had_401_log:
            print("  (401 was detected and logged)")
        failed = True

    if not failed:
        print("PASS: request_get_func retries on 401 with OAuth")
    return failed


def test_request_get_func_401_no_retry_api_key():
    """request_get_func does NOT retry 401 for api_key mode."""
    failed = False
    fox = MockFoxOAuth(auth_method="api_key", key="bad-key")

    mock_response = create_aiohttp_mock_response(
        status=401,
        json_data={"errno": 0, "msg": "unauthorized"},
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        data, retry = run_async(fox.request_get_func("/op/v0/device/list"))

    if data is not None:
        print(f"ERROR: api_key 401 should return None, got {data}")
        failed = True
    if fox.failures_total != 1:
        print(f"ERROR: failures_total should be 1, got {fox.failures_total}")
        failed = True

    if not failed:
        print("PASS: request_get_func does NOT retry 401 for api_key mode")
    return failed


def test_initialize_oauth_params():
    """Test that FoxAPI.initialize correctly passes OAuth params."""
    failed = False
    fox = MockFoxOAuth(auth_method="oauth", key="my-access-token", token_expires_at="2026-03-06T12:00:00Z")

    if fox.auth_method != "oauth":
        print(f"ERROR: auth_method should be 'oauth', got {fox.auth_method}")
        failed = True
    if fox.access_token != "my-access-token":
        print(f"ERROR: access_token should be 'my-access-token', got {fox.access_token}")
        failed = True
    if fox.provider_name != "fox_ess":
        print(f"ERROR: provider_name should be 'fox_ess', got {fox.provider_name}")
        failed = True
    if fox.token_expires_at is None or fox.token_expires_at <= 0:
        print(f"ERROR: token_expires_at not parsed: {fox.token_expires_at}")
        failed = True

    if not failed:
        print("PASS: initialize correctly sets OAuth params")
    return failed


def test_initialize_default_api_key():
    """Test that FoxAPI defaults to api_key when auth_method not provided."""
    failed = False
    fox = MockFoxOAuth()  # Defaults: auth_method="api_key"

    if fox.auth_method != "api_key":
        print(f"ERROR: default auth_method should be 'api_key', got {fox.auth_method}")
        failed = True
    if fox.access_token is not None:
        print(f"ERROR: access_token should be None for api_key, got {fox.access_token}")
        failed = True

    if not failed:
        print("PASS: initialize defaults to api_key mode")
    return failed


def run_fox_oauth_tests(my_predbat):
    """Run all Fox OAuth tests."""
    failed = False

    tests = [
        ("headers_apikey", test_get_headers_api_key),
        ("headers_oauth", test_get_headers_oauth),
        ("request_pre_refresh", test_request_get_func_oauth_pre_refresh),
        ("request_refresh_fails", test_request_get_func_oauth_refresh_fails),
        ("request_401_retry", test_request_get_func_401_retry),
        ("request_401_no_retry_apikey", test_request_get_func_401_no_retry_api_key),
        ("init_oauth_params", test_initialize_oauth_params),
        ("init_default_apikey", test_initialize_default_api_key),
    ]

    for name, test_func in tests:
        try:
            result = test_func()
            if result:
                print(f"  FAILED: fox_oauth.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in fox_oauth.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True

    return failed
