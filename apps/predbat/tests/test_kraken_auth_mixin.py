# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone, timedelta

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def make_token_response(token="jwt-token-123", refresh="refresh-token-456", exp_offset=3600):
    """Build a mock obtainKrakenToken GraphQL response."""
    exp = int(time.time()) + exp_offset
    return {
        "data": {
            "obtainKrakenToken": {
                "token": token,
                "refreshToken": refresh,
                "payload": {"exp": exp},
            }
        }
    }


def make_error_response(message="Auth failed", error_code=None):
    """Build a mock GraphQL error response."""
    error = {"message": message}
    if error_code:
        error["extensions"] = {"errorCode": error_code}
    return {"errors": [error]}


def make_mixin(auth_method="api_key", key=None, email=None, password=None):
    """Create a KrakenAuthMixin instance with mock base."""
    from kraken_auth_mixin import KrakenAuthMixin

    mixin = KrakenAuthMixin.__new__(KrakenAuthMixin)
    mixin.base = MagicMock()
    mixin.base.session = AsyncMock()
    mixin.base_url = "https://api.edfgb-kraken.energy"
    mixin.log = MagicMock()
    mixin._init_kraken_auth(auth_method, key=key, email=email, password=password)
    return mixin


def test_init_api_key_mode():
    mixin = make_mixin(auth_method="api_key", key="sk_live_test123")
    assert mixin.auth_method == "api_key"
    assert mixin._api_key == "sk_live_test123"
    assert mixin.access_token is None
    assert mixin.refresh_token is None
    assert mixin.oauth_failed is False


def test_init_email_mode():
    mixin = make_mixin(auth_method="email", email="user@edf.com", password="secret")
    assert mixin.auth_method == "email"
    assert mixin._email == "user@edf.com"
    assert mixin._password == "secret"
    assert mixin.access_token is None


def test_obtain_token_api_key():
    mixin = make_mixin(auth_method="api_key", key="sk_live_test123")
    mixin._kraken_token_request = AsyncMock(return_value={
        "token": "jwt-token-123",
        "refreshToken": "refresh-token-456",
        "exp": int(time.time()) + 3600,
    })
    result = asyncio.run(mixin.check_and_refresh_oauth_token())
    assert result is True
    assert mixin.access_token == "jwt-token-123"
    assert mixin.refresh_token == "refresh-token-456"
    mixin._kraken_token_request.assert_called_once_with({"APIKey": "sk_live_test123"})


def test_obtain_token_email():
    mixin = make_mixin(auth_method="email", email="user@edf.com", password="secret")
    mixin._kraken_token_request = AsyncMock(return_value={
        "token": "jwt-email-token",
        "refreshToken": "refresh-email-token",
        "exp": int(time.time()) + 3600,
    })
    result = asyncio.run(mixin.check_and_refresh_oauth_token())
    assert result is True
    assert mixin.access_token == "jwt-email-token"
    mixin._kraken_token_request.assert_called_once_with({"email": "user@edf.com", "password": "secret"})


def test_refresh_uses_refresh_token():
    mixin = make_mixin(auth_method="api_key", key="sk_live_test123")
    mixin.access_token = "old-token"
    mixin.refresh_token = "existing-refresh"
    mixin.token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    mixin._kraken_token_request = AsyncMock(return_value={
        "token": "new-jwt",
        "refreshToken": "new-refresh",
        "exp": int(time.time()) + 3600,
    })
    result = asyncio.run(mixin.check_and_refresh_oauth_token())
    assert result is True
    assert mixin.access_token == "new-jwt"
    mixin._kraken_token_request.assert_called_once_with({"refreshToken": "existing-refresh"})


def test_valid_token_not_refreshed():
    mixin = make_mixin(auth_method="api_key", key="sk_live_test123")
    mixin.access_token = "valid-token"
    mixin.refresh_token = "valid-refresh"
    mixin.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    mixin._kraken_token_request = AsyncMock()
    result = asyncio.run(mixin.check_and_refresh_oauth_token())
    assert result is True
    mixin._kraken_token_request.assert_not_called()


def test_refresh_failure_retries_with_credentials():
    mixin = make_mixin(auth_method="api_key", key="sk_live_test123")
    mixin.refresh_token = "bad-refresh"
    mixin.token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    call_count = 0
    async def side_effect(input_vars):
        nonlocal call_count
        call_count += 1
        if "refreshToken" in input_vars:
            return None
        return {
            "token": "recovered-jwt",
            "refreshToken": "new-refresh",
            "exp": int(time.time()) + 3600,
        }
    mixin._kraken_token_request = AsyncMock(side_effect=side_effect)
    result = asyncio.run(mixin.check_and_refresh_oauth_token())
    assert result is True
    assert mixin.access_token == "recovered-jwt"
    assert call_count == 2


def test_total_auth_failure_sets_oauth_failed():
    mixin = make_mixin(auth_method="api_key", key="sk_live_test123")
    mixin._kraken_token_request = AsyncMock(return_value=None)
    result = asyncio.run(mixin.check_and_refresh_oauth_token())
    assert result is False
    assert mixin.oauth_failed is True


def test_handle_oauth_401_clears_and_reobtains():
    mixin = make_mixin(auth_method="api_key", key="sk_live_test123")
    mixin.access_token = "old"
    mixin.refresh_token = "old-refresh"
    mixin.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    mixin._kraken_token_request = AsyncMock(return_value={
        "token": "fresh-jwt",
        "refreshToken": "fresh-refresh",
        "exp": int(time.time()) + 3600,
    })
    result = asyncio.run(mixin.handle_oauth_401())
    assert result is True
    assert mixin.access_token == "fresh-jwt"
    mixin._kraken_token_request.assert_called_once_with({"APIKey": "sk_live_test123"})


def test_oauth_failed_short_circuits():
    mixin = make_mixin(auth_method="api_key", key="sk_live_test123")
    mixin.oauth_failed = True
    mixin._kraken_token_request = AsyncMock()
    result = asyncio.run(mixin.check_and_refresh_oauth_token())
    assert result is False
    mixin._kraken_token_request.assert_not_called()


def run_kraken_auth_mixin_tests(my_predbat=None):
    """Run all KrakenAuthMixin tests. Returns True on failure, False on success."""
    tests = [
        test_init_api_key_mode,
        test_init_email_mode,
        test_obtain_token_api_key,
        test_obtain_token_email,
        test_refresh_uses_refresh_token,
        test_valid_token_not_refreshed,
        test_refresh_failure_retries_with_credentials,
        test_total_auth_failure_sets_oauth_failed,
        test_handle_oauth_401_clears_and_reobtains,
        test_oauth_failed_short_circuits,
    ]
    for test_func in tests:
        try:
            test_func()
        except Exception as e:
            print(f"  FAIL: {test_func.__name__}: {e}")
            import traceback
            traceback.print_exc()
            return True
        print(f"  OK: {test_func.__name__}")
    return False
