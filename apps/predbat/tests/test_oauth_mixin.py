# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test OAuth Mixin
# -----------------------------------------------------------------------------

import time
import aiohttp
from unittest.mock import MagicMock, patch
from tests.test_infra import run_async, create_aiohttp_mock_response, create_aiohttp_mock_session

# Import the real mixin (will exist in SaaS builds)
from oauth_mixin import OAuthMixin


class MockOAuthComponent(OAuthMixin):
    """Mock component that uses the OAuthMixin for testing."""

    def __init__(self):
        self.log_messages = []
        self.base = MagicMock()
        self.base.args = {"user_id": "test-instance-id-1234"}

    def log(self, message):
        self.log_messages.append(message)


def test_parse_expiry():
    """Test _parse_expiry with various input formats."""
    failed = False
    comp = MockOAuthComponent()
    comp._init_oauth("oauth", "token123", None, "fox_ess")

    # ISO timestamp with Z
    result = comp._parse_expiry("2026-03-06T12:00:00Z")
    if result <= 0:
        print("ERROR: _parse_expiry failed for ISO Z format")
        failed = True

    # ISO timestamp with timezone offset
    result = comp._parse_expiry("2026-03-06T12:00:00+00:00")
    if result <= 0:
        print("ERROR: _parse_expiry failed for ISO +00:00 format")
        failed = True

    # Float passthrough
    result = comp._parse_expiry(1709712000.0)
    if result != 1709712000.0:
        print(f"ERROR: _parse_expiry float passthrough returned {result}")
        failed = True

    # Int passthrough
    result = comp._parse_expiry(1709712000)
    if result != 1709712000.0:
        print(f"ERROR: _parse_expiry int passthrough returned {result}")
        failed = True

    # None returns 0
    result = comp._parse_expiry(None)
    if result != 0:
        print(f"ERROR: _parse_expiry(None) returned {result}")
        failed = True

    # Invalid string returns 0 and logs warning
    comp.log_messages = []
    result = comp._parse_expiry("not-a-date")
    if result != 0:
        print(f"ERROR: _parse_expiry('not-a-date') returned {result}")
        failed = True
    if not any("Warn" in m for m in comp.log_messages):
        print("ERROR: _parse_expiry('not-a-date') did not log warning")
        failed = True

    if not failed:
        print("PASS: _parse_expiry handles all formats correctly")
    return failed


def test_init_oauth_api_key_mode():
    """Test _init_oauth sets up API key mode correctly."""
    failed = False
    comp = MockOAuthComponent()
    comp._init_oauth("api_key", "my-api-key", None, "fox_ess")

    if comp.auth_method != "api_key":
        print(f"ERROR: auth_method is {comp.auth_method}, expected 'api_key'")
        failed = True
    if comp.access_token is not None:
        print(f"ERROR: access_token should be None in api_key mode, got {comp.access_token}")
        failed = True
    if comp.token_expires_at is not None:
        print(f"ERROR: token_expires_at should be None in api_key mode")
        failed = True
    if comp.oauth_failed:
        print("ERROR: oauth_failed should be False")
        failed = True

    if not failed:
        print("PASS: _init_oauth api_key mode correct")
    return failed


def test_init_oauth_oauth_mode():
    """Test _init_oauth sets up OAuth mode correctly."""
    failed = False
    comp = MockOAuthComponent()
    comp._init_oauth("oauth", "access-token-xyz", "2026-03-06T12:00:00Z", "fox_ess")

    if comp.auth_method != "oauth":
        print(f"ERROR: auth_method is {comp.auth_method}, expected 'oauth'")
        failed = True
    if comp.access_token != "access-token-xyz":
        print(f"ERROR: access_token is {comp.access_token}")
        failed = True
    if comp.token_expires_at is None or comp.token_expires_at <= 0:
        print(f"ERROR: token_expires_at not parsed: {comp.token_expires_at}")
        failed = True
    if comp.provider_name != "fox_ess":
        print(f"ERROR: provider_name is {comp.provider_name}")
        failed = True

    if not failed:
        print("PASS: _init_oauth oauth mode correct")
    return failed


def test_init_oauth_none_defaults_to_api_key():
    """Test _init_oauth with None auth_method defaults to api_key."""
    failed = False
    comp = MockOAuthComponent()
    comp._init_oauth(None, "key", None, "fox_ess")

    if comp.auth_method != "api_key":
        print(f"ERROR: None auth_method should default to 'api_key', got {comp.auth_method}")
        failed = True

    if not failed:
        print("PASS: _init_oauth None defaults to api_key")
    return failed


def test_token_needs_refresh_api_key():
    """Token refresh not needed in api_key mode."""
    failed = False
    comp = MockOAuthComponent()
    comp._init_oauth("api_key", "key", None, "fox_ess")

    if comp._token_needs_refresh():
        print("ERROR: _token_needs_refresh should be False for api_key mode")
        failed = True

    if not failed:
        print("PASS: _token_needs_refresh false for api_key")
    return failed


def test_token_needs_refresh_valid_token():
    """Token with > 2h remaining should not need refresh."""
    failed = False
    comp = MockOAuthComponent()
    # Set expiry 3 hours from now
    future_expiry = time.time() + 3 * 3600
    comp._init_oauth("oauth", "token", str(future_expiry), "fox_ess")
    comp.token_expires_at = future_expiry

    if comp._token_needs_refresh():
        print("ERROR: Token with 3h remaining should NOT need refresh")
        failed = True

    if not failed:
        print("PASS: _token_needs_refresh false for valid token")
    return failed


def test_token_needs_refresh_expiring_token():
    """Token with < 2h remaining should need refresh."""
    failed = False
    comp = MockOAuthComponent()
    # Set expiry 1 hour from now (less than 2h threshold)
    expiry = time.time() + 3600
    comp._init_oauth("oauth", "token", None, "fox_ess")
    comp.token_expires_at = expiry

    if not comp._token_needs_refresh():
        print("ERROR: Token with 1h remaining SHOULD need refresh")
        failed = True

    if not failed:
        print("PASS: _token_needs_refresh true for expiring token")
    return failed


def test_token_needs_refresh_no_expiry():
    """Token with no expiry should need refresh (safety)."""
    failed = False
    comp = MockOAuthComponent()
    comp._init_oauth("oauth", "token", None, "fox_ess")

    if not comp._token_needs_refresh():
        print("ERROR: Token with no expiry SHOULD need refresh")
        failed = True

    if not failed:
        print("PASS: _token_needs_refresh true for no expiry")
    return failed


def test_check_and_refresh_api_key_mode():
    """check_and_refresh_oauth_token returns True immediately for api_key mode."""
    failed = False
    comp = MockOAuthComponent()
    comp._init_oauth("api_key", "key", None, "fox_ess")

    result = run_async(comp.check_and_refresh_oauth_token())
    if not result:
        print("ERROR: check_and_refresh should return True for api_key mode")
        failed = True

    if not failed:
        print("PASS: check_and_refresh true for api_key")
    return failed


def test_check_and_refresh_oauth_failed():
    """check_and_refresh returns False when oauth_failed is set."""
    failed = False
    comp = MockOAuthComponent()
    comp._init_oauth("oauth", "token", None, "fox_ess")
    comp.oauth_failed = True

    result = run_async(comp.check_and_refresh_oauth_token())
    if result:
        print("ERROR: check_and_refresh should return False when oauth_failed=True")
        failed = True

    if not failed:
        print("PASS: check_and_refresh false when oauth_failed")
    return failed


def test_check_and_refresh_token_still_valid():
    """check_and_refresh returns True without calling refresh when token is valid."""
    failed = False
    comp = MockOAuthComponent()
    future_expiry = time.time() + 3 * 3600
    comp._init_oauth("oauth", "token", None, "fox_ess")
    comp.token_expires_at = future_expiry

    result = run_async(comp.check_and_refresh_oauth_token())
    if not result:
        print("ERROR: check_and_refresh should return True for valid token")
        failed = True

    if not failed:
        print("PASS: check_and_refresh true for valid token (no refresh)")
    return failed


def test_do_refresh_success():
    """Test successful token refresh via edge function."""
    failed = False
    comp = MockOAuthComponent()
    comp._init_oauth("oauth", "old-token", None, "fox_ess")
    comp.token_expires_at = 0  # Force refresh

    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "success": True,
            "access_token": "new-token-abc",
            "expires_at": "2026-03-07T12:00:00Z",
        },
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch.dict("os.environ", {"SUPABASE_URL": "https://test.supabase.co", "SUPABASE_KEY": "test-key"}):
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = run_async(comp._do_refresh())

    if not result:
        print("ERROR: _do_refresh should return True on success")
        failed = True
    if comp.access_token != "new-token-abc":
        print(f"ERROR: access_token not updated, got {comp.access_token}")
        failed = True
    if comp.token_expires_at is None or comp.token_expires_at <= 0:
        print(f"ERROR: token_expires_at not updated: {comp.token_expires_at}")
        failed = True
    if comp.oauth_failed:
        print("ERROR: oauth_failed should not be set on success")
        failed = True
    if comp._refresh_in_progress:
        print("ERROR: _refresh_in_progress should be cleared after refresh")
        failed = True

    if not failed:
        print("PASS: _do_refresh success updates token")
    return failed


def test_do_refresh_needs_reauth():
    """Test refresh failure with needs_reauth sets oauth_failed."""
    failed = False
    comp = MockOAuthComponent()
    comp._init_oauth("oauth", "old-token", None, "fox_ess")
    comp.token_expires_at = 0

    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={"success": False, "error": "needs_reauth"},
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch.dict("os.environ", {"SUPABASE_URL": "https://test.supabase.co", "SUPABASE_KEY": "test-key"}):
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = run_async(comp._do_refresh())

    if result:
        print("ERROR: _do_refresh should return False on needs_reauth")
        failed = True
    if not comp.oauth_failed:
        print("ERROR: oauth_failed should be True after needs_reauth")
        failed = True

    if not failed:
        print("PASS: _do_refresh needs_reauth sets oauth_failed")
    return failed


def test_do_refresh_http_error():
    """Test refresh with non-200 HTTP status."""
    failed = False
    comp = MockOAuthComponent()
    comp._init_oauth("oauth", "old-token", None, "fox_ess")
    comp.token_expires_at = 0

    mock_response = create_aiohttp_mock_response(status=500, json_data={})
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch.dict("os.environ", {"SUPABASE_URL": "https://test.supabase.co", "SUPABASE_KEY": "test-key"}):
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = run_async(comp._do_refresh())

    if result:
        print("ERROR: _do_refresh should return False on HTTP 500")
        failed = True

    if not failed:
        print("PASS: _do_refresh returns False on HTTP error")
    return failed


def test_do_refresh_network_error():
    """Test refresh with network timeout."""
    failed = False
    comp = MockOAuthComponent()
    comp._init_oauth("oauth", "old-token", None, "fox_ess")
    comp.token_expires_at = 0

    mock_session = create_aiohttp_mock_session(exception=aiohttp.ClientError("Connection refused"))

    with patch.dict("os.environ", {"SUPABASE_URL": "https://test.supabase.co", "SUPABASE_KEY": "test-key"}):
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = run_async(comp._do_refresh())

    if result:
        print("ERROR: _do_refresh should return False on network error")
        failed = True
    if comp._refresh_in_progress:
        print("ERROR: _refresh_in_progress should be cleared after error")
        failed = True

    if not failed:
        print("PASS: _do_refresh returns False on network error")
    return failed


def test_do_refresh_missing_env_vars():
    """Test refresh when SUPABASE_URL/SUPABASE_KEY not set."""
    failed = False
    comp = MockOAuthComponent()
    comp._init_oauth("oauth", "old-token", None, "fox_ess")
    comp.token_expires_at = 0

    # Clear env vars
    with patch.dict("os.environ", {}, clear=True):
        result = run_async(comp._do_refresh())

    if not result:
        print("ERROR: _do_refresh should return True (skip) when env vars missing")
        failed = True

    if not failed:
        print("PASS: _do_refresh skips gracefully when env vars missing")
    return failed


def test_do_refresh_missing_instance_id():
    """Test refresh when instance_id (user_id) not in config."""
    failed = False
    comp = MockOAuthComponent()
    comp._init_oauth("oauth", "old-token", None, "fox_ess")
    comp.token_expires_at = 0
    comp.base.args = {}  # No user_id

    with patch.dict("os.environ", {"SUPABASE_URL": "https://test.supabase.co", "SUPABASE_KEY": "test-key"}):
        result = run_async(comp._do_refresh())

    if not result:
        print("ERROR: _do_refresh should return True (skip) when no instance_id")
        failed = True

    if not failed:
        print("PASS: _do_refresh skips gracefully when no instance_id")
    return failed


def test_handle_oauth_401_api_key():
    """handle_oauth_401 returns False for api_key mode."""
    failed = False
    comp = MockOAuthComponent()
    comp._init_oauth("api_key", "key", None, "fox_ess")

    result = run_async(comp.handle_oauth_401())
    if result:
        print("ERROR: handle_oauth_401 should return False for api_key mode")
        failed = True

    if not failed:
        print("PASS: handle_oauth_401 false for api_key")
    return failed


def test_handle_oauth_401_forces_refresh():
    """handle_oauth_401 forces a refresh even if token looks valid."""
    failed = False
    comp = MockOAuthComponent()
    future_expiry = time.time() + 3 * 3600  # Token still valid
    comp._init_oauth("oauth", "old-token", None, "fox_ess")
    comp.token_expires_at = future_expiry

    mock_response = create_aiohttp_mock_response(
        status=200,
        json_data={
            "success": True,
            "access_token": "refreshed-after-401",
            "expires_at": "2026-03-07T12:00:00Z",
        },
    )
    mock_session = create_aiohttp_mock_session(mock_response)

    with patch.dict("os.environ", {"SUPABASE_URL": "https://test.supabase.co", "SUPABASE_KEY": "test-key"}):
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = run_async(comp.handle_oauth_401())

    if not result:
        print("ERROR: handle_oauth_401 should return True on successful refresh")
        failed = True
    if comp.access_token != "refreshed-after-401":
        print(f"ERROR: access_token not updated after 401 refresh, got {comp.access_token}")
        failed = True
    if comp.token_expires_at != 0:
        # handle_oauth_401 sets token_expires_at = 0 to force refresh, then _do_refresh updates it
        # After successful refresh, it should have a new value
        pass

    if not failed:
        print("PASS: handle_oauth_401 forces refresh and updates token")
    return failed


def test_refresh_dedup():
    """Concurrent refresh calls should not double-fire."""
    failed = False
    comp = MockOAuthComponent()
    comp._init_oauth("oauth", "token", None, "fox_ess")
    comp.token_expires_at = 0
    comp._refresh_in_progress = True  # Simulate another coroutine refreshing

    # check_and_refresh should return True (skip) when refresh in progress
    result = run_async(comp.check_and_refresh_oauth_token())
    if not result:
        print("ERROR: check_and_refresh should return True when refresh already in progress")
        failed = True

    if not failed:
        print("PASS: refresh dedup works correctly")
    return failed


def run_oauth_mixin_tests(my_predbat):
    """Run all OAuth mixin tests."""
    failed = False

    tests = [
        ("parse_expiry", test_parse_expiry),
        ("init_api_key", test_init_oauth_api_key_mode),
        ("init_oauth", test_init_oauth_oauth_mode),
        ("init_none_default", test_init_oauth_none_defaults_to_api_key),
        ("needs_refresh_apikey", test_token_needs_refresh_api_key),
        ("needs_refresh_valid", test_token_needs_refresh_valid_token),
        ("needs_refresh_expiring", test_token_needs_refresh_expiring_token),
        ("needs_refresh_no_expiry", test_token_needs_refresh_no_expiry),
        ("check_apikey_mode", test_check_and_refresh_api_key_mode),
        ("check_oauth_failed", test_check_and_refresh_oauth_failed),
        ("check_valid_token", test_check_and_refresh_token_still_valid),
        ("refresh_success", test_do_refresh_success),
        ("refresh_needs_reauth", test_do_refresh_needs_reauth),
        ("refresh_http_error", test_do_refresh_http_error),
        ("refresh_network_error", test_do_refresh_network_error),
        ("refresh_missing_env", test_do_refresh_missing_env_vars),
        ("refresh_missing_instance", test_do_refresh_missing_instance_id),
        ("401_apikey", test_handle_oauth_401_api_key),
        ("401_forces_refresh", test_handle_oauth_401_forces_refresh),
        ("refresh_dedup", test_refresh_dedup),
    ]

    for name, test_func in tests:
        try:
            result = test_func()
            if result:
                print(f"  FAILED: oauth_mixin.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in oauth_mixin.{name}: {e}")
            failed = True

    return failed
