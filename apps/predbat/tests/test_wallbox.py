# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for the Wallbox EV charger integration
"""

import asyncio
import datetime
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytz

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_infra import run_async
from mock_base import MockBase

from wallbox import (
    WALLBOX_API_URL,
    WALLBOX_AUTH_URL,
    WallboxApiError,
    WallboxAuthError,
    WallboxRateLimitError,
    WallboxTransport,
    basic_auth_header,
)


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
    records the method, URL, headers and JSON body of every request in order.
    """
    calls = []
    queue = list(responses)

    def _request(method, url, **kwargs):
        """Record the request, then return or raise the next queued item."""
        calls.append({"method": method, "url": url, "headers": kwargs.get("headers") or {}, "json": kwargs.get("json")})
        item = queue.pop(0) if queue else _response({})
        if isinstance(item, BaseException):
            raise item
        return item

    session = MagicMock()
    session.request = _request
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session, calls


def _auth_payload(token="jwt-1", refresh="refresh-1", life=3600, refresh_life=86400):
    """Build a sign in response whose TTLs are absolute epoch milliseconds, as Wallbox sends them."""
    now = time.time()
    return {"data": {"attributes": {"token": token, "refresh_token": refresh, "ttl": int((now + life) * 1000), "refresh_token_ttl": int((now + refresh_life) * 1000)}}}


MOCK_GROUPS = {"result": {"groups": [{"chargers": [{"id": 202}, {"id": 101}]}, {"chargers": []}]}}


def test_basic_auth_header():
    """The Basic header is UTF-8, so a colon or accented character in the password survives."""
    assert basic_auth_header("user@example.com", "secret") == "Basic dXNlckBleGFtcGxlLmNvbTpzZWNyZXQ="
    # base64 of "a:pä:ss" encoded as UTF-8
    assert basic_auth_header("a", "pä:ss") == "Basic YTpww6Q6c3M="
    print("  ✓ Basic auth header is built as UTF-8")


def test_transport_signs_in_then_lists_chargers():
    """The first call signs in with Basic auth and the Partner header, then uses the bearer token."""
    session, calls = _session([_response(_auth_payload()), _response(MOCK_GROUPS)])
    transport = WallboxTransport(print, "user@example.com", "secret")
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        chargers = run_async(transport.list_chargers())

    assert chargers == [202, 101], chargers
    assert calls[0]["url"] == WALLBOX_AUTH_URL + "users/signin"
    assert calls[0]["headers"]["Authorization"] == basic_auth_header("user@example.com", "secret")
    assert calls[0]["headers"]["Partner"] == "wallbox"
    assert calls[1]["url"] == WALLBOX_API_URL + "v3/chargers/groups"
    assert calls[1]["headers"]["Authorization"] == "Bearer jwt-1"
    print("  ✓ Transport signs in then lists chargers")


def test_transport_reuses_a_valid_token():
    """A token with life left is reused: two reads make one sign in."""
    session, calls = _session([_response(_auth_payload()), _response({"status_id": 193}), _response({"status_id": 161})])
    transport = WallboxTransport(print, "user@example.com", "secret")
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        first = run_async(transport.get_status(101))
        second = run_async(transport.get_status(101))

    assert first == {"status_id": 193} and second == {"status_id": 161}
    assert [call["url"] for call in calls] == [WALLBOX_AUTH_URL + "users/signin", WALLBOX_API_URL + "chargers/status/101", WALLBOX_API_URL + "chargers/status/101"]
    print("  ✓ A valid token is reused")


def test_transport_refreshes_an_expired_token():
    """An expired token with a live refresh token is refreshed with a bearer call, not a sign in."""
    session, calls = _session([_response(_auth_payload(token="jwt-2")), _response({"status_id": 193})])
    transport = WallboxTransport(print, "user@example.com", "secret")
    transport.token = "jwt-1"
    transport.token_expiry = time.time() - 10
    transport.refresh_token = "refresh-1"
    transport.refresh_expiry = time.time() + 3600
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        run_async(transport.get_status(101))

    assert calls[0]["url"] == WALLBOX_AUTH_URL + "users/refresh-token"
    assert calls[0]["headers"]["Authorization"] == "Bearer refresh-1"
    assert calls[0]["headers"]["Partner"] == "wallbox"
    assert calls[1]["headers"]["Authorization"] == "Bearer jwt-2"
    print("  ✓ An expired token is refreshed")


def test_transport_signs_in_when_the_refresh_is_rejected():
    """A 401 on refresh means the refresh token was revoked: fall back to a full sign in."""
    session, calls = _session([_response({}, status=401), _response(_auth_payload(token="jwt-3")), _response({"status_id": 193})])
    transport = WallboxTransport(print, "user@example.com", "secret")
    transport.token = "jwt-1"
    transport.token_expiry = time.time() - 10
    transport.refresh_token = "refresh-1"
    transport.refresh_expiry = time.time() + 3600
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        run_async(transport.get_status(101))

    assert [call["url"] for call in calls] == [WALLBOX_AUTH_URL + "users/refresh-token", WALLBOX_AUTH_URL + "users/signin", WALLBOX_API_URL + "chargers/status/101"]
    assert calls[2]["headers"]["Authorization"] == "Bearer jwt-3"
    print("  ✓ A rejected refresh falls back to sign in")


def test_transport_bad_credentials():
    """A 403 on sign in is an auth error, recorded with reason auth_error."""
    session, _ = _session([_response({}, status=403)])
    transport = WallboxTransport(print, "user@example.com", "wrong")
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call") as mock_record:
        try:
            run_async(transport.list_chargers())
            raise AssertionError("Expected WallboxAuthError")
        except WallboxAuthError:
            pass
    assert mock_record.call_args.kwargs.get("reason") == "auth_error"
    print("  ✓ Bad credentials raise WallboxAuthError")


def test_transport_retries_once_after_a_revoked_token():
    """A 401 on a data call drops the token, signs in again and retries the call once."""
    session, calls = _session([_response({}, status=401), _response(_auth_payload(token="jwt-9")), _response({"status_id": 193})])
    transport = WallboxTransport(print, "user@example.com", "secret")
    transport.token = "jwt-1"
    transport.token_expiry = time.time() + 3600
    with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call"):
        status = run_async(transport.get_status(101))

    assert status == {"status_id": 193}
    assert calls[1]["url"] == WALLBOX_AUTH_URL + "users/signin"
    assert calls[2]["headers"]["Authorization"] == "Bearer jwt-9"
    print("  ✓ A revoked token is replaced and the call retried once")


def test_transport_rate_limit_and_failures():
    """429 is its own error; a 500, a timeout, a dropped connection and a non-JSON body are API errors."""
    cases = [
        (_response({}, status=429), WallboxRateLimitError, "rate_limit"),
        (_response({}, status=500), WallboxApiError, "server_error"),
        (_response({}, status=404), WallboxApiError, "client_error"),
        (asyncio.TimeoutError(), WallboxApiError, "connection_error"),
        (aiohttp.ClientError("boom"), WallboxApiError, "connection_error"),
        (_response(json_error=ValueError("not json")), WallboxApiError, "decode_error"),
        (_response(["not", "a", "dict"]), WallboxApiError, "decode_error"),
    ]
    for item, expected, reason in cases:
        session, _ = _session([item])
        transport = WallboxTransport(print, "user@example.com", "secret")
        transport.token = "jwt-1"
        transport.token_expiry = time.time() + 3600
        with patch("aiohttp.ClientSession", return_value=session), patch("wallbox.record_api_call") as mock_record:
            try:
                run_async(transport.get_status(101))
                raise AssertionError("Expected {}".format(expected.__name__))
            except expected:
                pass
        assert mock_record.call_args.kwargs.get("reason") == reason, (expected, mock_record.call_args)
    print("  ✓ Rate limit and failures map to the right errors and metric reasons")


def test_wallbox(my_predbat=None):
    """Run every Wallbox test."""
    print("=" * 70)
    print("Wallbox tests")
    print("=" * 70)
    test_basic_auth_header()
    test_transport_signs_in_then_lists_chargers()
    test_transport_reuses_a_valid_token()
    test_transport_refreshes_an_expired_token()
    test_transport_signs_in_when_the_refresh_is_rejected()
    test_transport_bad_credentials()
    test_transport_retries_once_after_a_revoked_token()
    test_transport_rate_limit_and_failures()
    print("=" * 70)
    return False
