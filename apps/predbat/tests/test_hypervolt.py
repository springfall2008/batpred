# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for the Hypervolt EV charger integration
"""

import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_infra import run_async

from hypervolt import (
    HYPERVOLT_API_URL,
    HYPERVOLT_TOKEN_URL,
    HypervoltApiError,
    HypervoltAuthError,
    HypervoltTransport,
    charger_api_version,
    charger_major_version,
)

# Charger ids are decimal; their hex form is 12 digits for a V2 and 16 for a V3
V2_ID = str(int("a" * 12, 16))
V3_ID = str(int("a" * 16, 16))


def _response(json_data=None, status=200, json_error=None):
    """Build a mock aiohttp response usable as an async context manager."""
    response = MagicMock()
    response.status = status
    if json_error is not None:
        response.json = AsyncMock(side_effect=json_error)
    else:
        response.json = AsyncMock(return_value=json_data)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    return response


def _session(responses):
    """Build a mock aiohttp session whose request() returns the next queued response.

    A queued exception instance is raised instead. Returns (session, calls), where calls
    records the method, URL, headers and form body of every request in order.
    """
    calls = []
    queue = list(responses)

    def _request(method, url, **kwargs):
        """Record the request, then return or raise the next queued item."""
        calls.append({"method": method, "url": url, "headers": kwargs.get("headers") or {}, "data": kwargs.get("data")})
        item = queue.pop(0) if queue else _response({})
        if isinstance(item, BaseException):
            raise item
        return item

    session = MagicMock()
    session.request = _request
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session, calls


def _token_payload(access="jwt-1", refresh="refresh-1", life=300):
    """A Keycloak token response."""
    return {"access_token": access, "refresh_token": refresh, "expires_in": life, "token_type": "Bearer"}


MOCK_CHARGERS = {"chargers": [{"id": V3_ID, "model": "Home 3 Pro", "adopted_at": "2024-01-01T00:00:00.000Z"}]}


def test_api_version_from_id():
    """The charger generation comes from the id's length in bytes; updated V2 firmware uses the V3 API."""
    assert charger_major_version(V2_ID) == 2
    assert charger_major_version(V3_ID) == 3
    assert charger_major_version("junk") == 3
    assert charger_api_version(V2_ID, None) == 2
    assert charger_api_version(V2_ID, "1.0") == 3
    assert charger_api_version(V2_ID, 1) == 3
    assert charger_api_version(V2_ID, "202111231810.abcdef") == 2
    assert charger_api_version(V3_ID, None) == 3
    print("  ✓ API version from charger id and firmware")


def test_sign_in():
    """The first call is a password grant to Keycloak with the integration's client id."""
    session, calls = _session([_response(_token_payload())])
    transport = HypervoltTransport(print, "user@example.com", "secret")
    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        token = run_async(transport.authenticate())

    assert token == "jwt-1"
    assert calls[0]["method"] == "POST" and calls[0]["url"] == HYPERVOLT_TOKEN_URL
    assert calls[0]["data"] == {"client_id": "home-assistant", "grant_type": "password", "scope": "openid profile email offline_access", "username": "user@example.com", "password": "secret"}
    assert transport.access_token == "jwt-1" and transport.refresh_token == "refresh-1"
    assert abs(transport.token_expiry - (time.time() + 300)) < 5
    print("  ✓ Signs in with a password grant")


def test_token_reused_then_refreshed():
    """A token with time to spare is reused; one inside the margin is refreshed, not signed in again."""
    session, calls = _session([_response(_token_payload(life=3600)), _response(_token_payload(access="jwt-2", refresh="refresh-2"))])
    transport = HypervoltTransport(print, "user@example.com", "secret")
    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        run_async(transport.authenticate())
        run_async(transport.authenticate())
        assert len(calls) == 1
        assert transport.token_due() is False
        transport.token_expiry = time.time() + 60
        assert transport.token_due() is True
        token = run_async(transport.authenticate())

    assert token == "jwt-2"
    assert calls[1]["data"] == {"client_id": "home-assistant", "grant_type": "refresh_token", "refresh_token": "refresh-1"}
    assert transport.refresh_token == "refresh-2"
    print("  ✓ Token reused, then refreshed")


def test_refresh_rejected_then_sign_in():
    """A rejected refresh token falls back to a password sign in."""
    session, calls = _session([_response({"error": "invalid_grant"}, status=400), _response(_token_payload(access="jwt-3"))])
    transport = HypervoltTransport(print, "user@example.com", "secret")
    transport.access_token = "jwt-old"
    transport.refresh_token = "refresh-old"
    transport.token_expiry = time.time() - 1
    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        token = run_async(transport.authenticate())

    assert token == "jwt-3"
    assert [call["data"]["grant_type"] for call in calls] == ["refresh_token", "password"]
    print("  ✓ A rejected refresh falls back to sign in")


def test_bad_credentials():
    """A rejected sign in raises HypervoltAuthError without the password in its text."""
    for status in (400, 401):
        session, _ = _session([_response({"error": "invalid_grant"}, status=status)])
        transport = HypervoltTransport(print, "user@example.com", "s3cr3t-pw")
        with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
            try:
                run_async(transport.authenticate())
                raise AssertionError("Expected HypervoltAuthError")
            except HypervoltAuthError as exc:
                assert "s3cr3t-pw" not in str(exc)
        assert transport.access_token is None
    print("  ✓ Bad credentials raise HypervoltAuthError")


def test_list_chargers():
    """The charger list is read with the bearer token; a 401 signs in again once; a 500 is an API error."""
    session, calls = _session([_response(_token_payload()), _response(MOCK_CHARGERS)])
    transport = HypervoltTransport(print, "user@example.com", "secret")
    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        chargers = run_async(transport.list_chargers())
    assert chargers == MOCK_CHARGERS["chargers"]
    assert calls[1]["method"] == "GET" and calls[1]["url"] == HYPERVOLT_API_URL + "users/me?includes=chargers"
    assert calls[1]["headers"]["Authorization"] == "Bearer jwt-1"

    session, calls = _session([_response({}, status=401), _response(_token_payload(access="jwt-9")), _response(MOCK_CHARGERS)])
    transport.token_expiry = time.time() + 3600
    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        chargers = run_async(transport.list_chargers())
    assert chargers == MOCK_CHARGERS["chargers"]
    assert calls[2]["headers"]["Authorization"] == "Bearer jwt-9"

    session, _ = _session([_response({}, status=500)])
    with patch("aiohttp.ClientSession", return_value=session), patch("hypervolt.record_api_call"):
        try:
            run_async(transport.list_chargers())
            raise AssertionError("Expected HypervoltApiError")
        except HypervoltApiError:
            pass
    print("  ✓ Lists chargers")


def test_hypervolt(my_predbat=None):
    """Run every Hypervolt test."""
    print("=" * 70)
    print("Hypervolt tests")
    print("=" * 70)
    test_api_version_from_id()
    test_sign_in()
    test_token_reused_then_refreshed()
    test_refresh_rejected_then_sign_in()
    test_bad_credentials()
    test_list_chargers()
    print("=" * 70)
    return False
