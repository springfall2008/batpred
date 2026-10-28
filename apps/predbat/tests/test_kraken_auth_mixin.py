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
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def make_token_response(token="jwt-token-123", refresh="refresh-token-456", exp_offset=3600, payload_as_string=False):
    """Build a mock obtainKrakenToken GraphQL response."""
    exp = int(time.time()) + exp_offset
    payload = {"exp": exp}
    if payload_as_string:
        import json
        payload = json.dumps(payload)
    return {
        "data": {
            "obtainKrakenToken": {
                "token": token,
                "refreshToken": refresh,
                "payload": payload,
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


def test_token_request_parses_scalar_payload_dict():
    """_kraken_token_request handles payload as already-parsed dict (GenericScalar)."""
    mixin = make_mixin(auth_method="api_key", key="test")
    response_data = make_token_response(payload_as_string=False)

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=response_data)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(mixin._kraken_token_request({"APIKey": "test"}))

    assert result is not None
    assert result["token"] == "jwt-token-123"
    assert result["refreshToken"] == "refresh-token-456"
    assert result["exp"] > 0


def test_token_request_parses_scalar_payload_string():
    """_kraken_token_request handles payload as JSON string (GenericScalar variant)."""
    mixin = make_mixin(auth_method="api_key", key="test")
    response_data = make_token_response(payload_as_string=True)

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=response_data)

    mock_session = AsyncMock()
    mock_session.post = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(mixin._kraken_token_request({"APIKey": "test"}))

    assert result is not None
    assert result["token"] == "jwt-token-123"
    assert result["exp"] > 0


def test_token_request_mutation_has_no_payload_subfields():
    """Verify the GraphQL mutation requests 'payload' bare (no subfields like { exp })."""
    mixin = make_mixin(auth_method="api_key", key="test")
    response_data = make_token_response()

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=response_data)

    captured_body = {}

    def capture_post(*args, **kwargs):
        captured_body.update(kwargs.get("json", {}))
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=None)
        return ctx

    mock_session = AsyncMock()
    mock_session.post = MagicMock(side_effect=capture_post)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    with patch("aiohttp.ClientSession", return_value=mock_session):
        asyncio.run(mixin._kraken_token_request({"APIKey": "test"}))

    query = captured_body.get("query", "")
    # "payload" must appear without subfields (not "payload { exp }" or "payload {")
    assert "payload" in query
    assert "payload {" not in query
    assert "payload{" not in query


def run_kraken_auth_mixin_tests(my_predbat=None):
    """Run all KrakenAuthMixin tests. Returns True on failure, False on success.

    Tests are discovered rather than listed. The runner used to carry a hardcoded list, so
    any test added below it was silently skipped by unit_test.py - which invokes this runner,
    not pytest discovery - and a whole feature could regress with the suite still green.
    """
    module = sys.modules[__name__]
    # dir() gives a stable alphabetical order; these tests are independent of each other.
    tests = [obj for name in dir(module) if name.startswith("test_") and callable(obj := getattr(module, name))]

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


CLOUDFRONT_403_BODY = (
    '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN" "http://www.w3.org/TR/html4/loose.dtd">\n'
    "<HTML><HEAD><TITLE>ERROR: The request could not be satisfied</TITLE></HEAD><BODY>\n"
    "<H1>403 ERROR</H1><H2>The request could not be satisfied.</H2>\n"
    "Request blocked.\n"
    "Generated by cloudfront (CloudFront)\n"
    "</BODY></HTML>"
)


def make_http_mixin(status, body_text, json_data=None):
    """Mixin whose real _kraken_token_request will see one response with this status/body."""
    mixin = make_mixin(auth_method="api_key", key="sk_live_test123")

    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.text = AsyncMock(return_value=body_text)
    mock_resp.json = AsyncMock(return_value=json_data or {})

    mock_session = AsyncMock()
    mock_session.post = MagicMock(
        return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_resp),
            __aexit__=AsyncMock(return_value=None),
        )
    )
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    return mixin, mock_session


def edge_blocked_request(mixin):
    """Stand-in for _kraken_token_request that reports a CDN block."""

    async def _blocked(_input_vars):
        mixin.token_mint_edge_blocked = True
        return None

    return AsyncMock(side_effect=_blocked)


def test_token_request_flags_cdn_403():
    """A 403 carrying a CloudFront page is reported as an edge block, not a plain failure."""
    mixin, mock_session = make_http_mixin(403, CLOUDFRONT_403_BODY)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(mixin._kraken_token_request({"APIKey": "sk_live_test123"}))
    assert result is None
    assert mixin.token_mint_edge_blocked is True


def test_token_request_does_not_flag_json_403():
    """A 403 with a JSON body is a credential problem, not an edge block."""
    mixin, mock_session = make_http_mixin(403, '{"errors": [{"message": "Forbidden"}]}')
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(mixin._kraken_token_request({"APIKey": "sk_live_test123"}))
    assert result is None
    assert mixin.token_mint_edge_blocked is False


def test_edge_block_does_not_latch_oauth_failed():
    """A CDN block is transient - it must not permanently disable auth for the process."""
    mixin = make_mixin(auth_method="api_key", key="sk_live_test123")
    mixin._kraken_token_request = edge_blocked_request(mixin)

    result = asyncio.run(mixin.check_and_refresh_oauth_token())

    assert result is False
    assert mixin.oauth_failed is False, "an edge block must not latch oauth_failed"
    assert mixin.token_mint_backoff.until is not None
    assert mixin.token_mint_backoff.count == 1
    # One attempt only - no immediate retry with primary credentials into the same block
    assert mixin._kraken_token_request.call_count == 1


def test_edge_block_suppresses_the_next_mint():
    """While backing off, no token request is made at all."""
    mixin = make_mixin(auth_method="api_key", key="sk_live_test123")
    mixin._kraken_token_request = edge_blocked_request(mixin)
    asyncio.run(mixin.check_and_refresh_oauth_token())

    mixin._kraken_token_request = AsyncMock()
    result = asyncio.run(mixin.check_and_refresh_oauth_token())

    assert result is False
    mixin._kraken_token_request.assert_not_called()
    assert mixin.token_mint_backoff.count == 1, "a suppressed attempt must not grow the backoff"


def test_edge_block_keeps_a_still_valid_token():
    """A token inside the proactive-refresh window has not expired - keep using it."""
    mixin = make_mixin(auth_method="api_key", key="sk_live_test123")
    mixin.access_token = "still-good"
    mixin.token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=3)
    mixin._kraken_token_request = edge_blocked_request(mixin)

    # The mint is refused, but the token itself is still good for ~3 minutes
    assert asyncio.run(mixin.check_and_refresh_oauth_token()) is True
    assert mixin.token_mint_backoff.until is not None

    # Next caller, still inside the window, token still has ~3 minutes of life
    mixin._kraken_token_request = AsyncMock()
    result = asyncio.run(mixin.check_and_refresh_oauth_token())
    assert result is True
    assert mixin.access_token == "still-good"
    mixin._kraken_token_request.assert_not_called()

    # Once it really has expired the backoff suppresses and reports no token
    mixin.token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert asyncio.run(mixin.check_and_refresh_oauth_token()) is False


def test_elapsed_backoff_reopens_the_mint():
    """An elapsed deadline lets the mint through again. Deadline left SET on purpose: a guard
    that tests the field for presence rather than against the clock would back off forever."""
    mixin = make_mixin(auth_method="api_key", key="sk_live_test123")
    mixin._kraken_token_request = edge_blocked_request(mixin)
    asyncio.run(mixin.check_and_refresh_oauth_token())
    assert mixin.token_mint_backoff.until is not None

    mixin.token_mint_backoff.until = datetime.now(timezone.utc) - timedelta(seconds=1)
    mixin._kraken_token_request = AsyncMock(
        return_value={"token": "recovered", "refreshToken": "r", "exp": int(time.time()) + 3600}
    )

    result = asyncio.run(mixin.check_and_refresh_oauth_token())

    assert result is True
    assert mixin.access_token == "recovered"
    assert mixin.token_mint_backoff.until is None
    assert mixin.token_mint_backoff.count == 0
    assert mixin.oauth_failed is False


def test_genuine_auth_failure_still_latches_oauth_failed():
    """Non-edge failures keep the existing behaviour: retry once, then give up."""
    mixin = make_mixin(auth_method="api_key", key="sk_live_test123")
    mixin._kraken_token_request = AsyncMock(return_value=None)

    result = asyncio.run(mixin.check_and_refresh_oauth_token())

    assert result is False
    assert mixin.oauth_failed is True
    assert mixin.token_mint_backoff.until is None


def test_edge_block_with_a_refresh_token_held():
    """A block must not burn the refresh token or recurse into the same block."""
    mixin = make_mixin(auth_method="api_key", key="sk_live_test123")
    mixin.refresh_token = "still-good-refresh"
    mixin.token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    mixin._kraken_token_request = edge_blocked_request(mixin)

    result = asyncio.run(mixin.check_and_refresh_oauth_token())

    assert result is False
    assert mixin.oauth_failed is False
    assert mixin.refresh_token == "still-good-refresh", "the refresh token is not what the CDN objected to"
    assert mixin._kraken_token_request.call_count == 1, "must not recurse into the same block"
    assert mixin.token_mint_backoff.until is not None
    assert mixin._refresh_in_progress is False


def test_redact_braced_spans():
    """Every {...} span is removed, including nested and unterminated ones, so echoed input never survives."""
    from kraken_auth_mixin import redact_braced_spans

    assert redact_braced_spans("got invalid value {'email': 'a@b.c', 'password': 'hunter2'}; Field") == "got invalid value {...}; Field"
    assert redact_braced_spans("a {b {c} d} e") == "a {...} e"
    assert redact_braced_spans("value {'password': 'hunter2'") == "value {...}"
    assert redact_braced_spans("no braces") == "no braces"
    # A brace, quote or backslash inside a quoted credential must not end the span early (#5583 review)
    assert redact_braced_spans("value {'password': 'prefix}example-secret'} tail") == "value {...} tail"
    assert redact_braced_spans('value {"password": "a\\"}b"} tail') == "value {...} tail"
    assert redact_braced_spans("value {'password': \"it's}\"} tail") == "value {...} tail"
    assert redact_braced_spans("value {'password': 'ab\\\\'} tail") == "value {...} tail"
    assert redact_braced_spans("value {'password': 'never closed} tail") == "value {...}"


def test_summarise_token_errors_scrubs_submitted_secrets():
    """A submitted credential is removed verbatim even when Kraken echoes it in a shape the brace matching misreads."""
    from kraken_auth_mixin import summarise_token_errors

    errors = [{"message": "got invalid value {password: ab}cd} for input", "extensions": {"errorCode": "KT-CT-0000"}}]
    # Unquoted, the brace matching alone closes at the first "}" and would leak the rest of the credential
    assert "cd" in summarise_token_errors(errors)
    summary = summarise_token_errors(errors, ["ab}cd"])
    assert summary == "KT-CT-0000: got invalid value {...} for input", summary
    assert "cd" not in summary


def test_token_request_http_400_logs_redacted_reason():
    """A non-200 token response logs the Kraken reason, with the echoed password stripped (#5582)."""
    body = (
        '{"errors": [{"message": "Variable \'$input\' got invalid value {\'email\': \'me@example.com\', \'password\': \'hunter2\'}; '
        "Field 'email' is not defined by type 'ObtainJSONWebTokenInput'.\", \"extensions\": {\"errorCode\": \"KT-CT-0000\"}}]}"
    )
    mixin, mock_session = make_http_mixin(400, body)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(mixin._kraken_token_request({"email": "me@example.com", "password": "hunter2"}))

    assert result is None
    logged = " ".join(str(c.args[0]) for c in mixin.log.call_args_list)
    assert "HTTP 400" in logged
    assert "Field 'email' is not defined by type 'ObtainJSONWebTokenInput'" in logged, logged
    assert "KT-CT-0000" in logged, logged
    assert "hunter2" not in logged and "me@example.com" not in logged, logged


def test_token_request_graphql_rejection_logs_redacted_reason():
    """A 200 response carrying GraphQL errors (a rejected credential) is logged rather than dropped silently (#5582)."""
    # Echoed unquoted, with a brace inside the password: only the submitted-secret scrub stops "ter2" leaking
    error_body = {"errors": [{"message": "Invalid data {password: hun}ter2}.", "extensions": {"errorCode": "KT-CT-1138"}}]}
    mixin, mock_session = make_http_mixin(200, "", json_data=error_body)
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(mixin._kraken_token_request({"email": "me@example.com", "password": "hun}ter2"}))

    assert result is None
    logged = " ".join(str(c.args[0]) for c in mixin.log.call_args_list)
    assert "KT-CT-1138: Invalid data {...}." in logged, logged
    assert "ter2" not in logged and "hun" not in logged, logged


def rate_limited_request(mixin, retry_after=None):
    """Stand-in for _kraken_token_request that reports a rate limit (KT-CT-1199 or HTTP 429)."""

    async def _limited(_input_vars):
        mixin.token_mint_rate_limited = True
        mixin.token_mint_retry_after = retry_after
        return None

    return AsyncMock(side_effect=_limited)


def http_response_of(mock_session):
    """The response a make_http_mixin session hands out, so a test can set its headers."""
    return mock_session.post.return_value.__aenter__.return_value


def test_token_request_flags_http_429():
    """An HTTP 429 is reported as a rate limit, carrying the Retry-After, not as a plain failure."""
    mixin, mock_session = make_http_mixin(429, "Too Many Requests")
    http_response_of(mock_session).headers = {"Retry-After": "900"}
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(mixin._kraken_token_request({"APIKey": "sk_live_test123"}))

    assert result is None
    assert mixin.token_mint_rate_limited is True
    assert mixin.token_mint_retry_after == 900
    assert mixin.token_mint_edge_blocked is False


def test_token_request_flags_kt_ct_1199():
    """A KT-CT-1199 GraphQL error on the mint is reported as a rate limit."""
    mixin, mock_session = make_http_mixin(200, "", json_data=make_error_response("Too many requests.", "KT-CT-1199"))
    with patch("aiohttp.ClientSession", return_value=mock_session):
        result = asyncio.run(mixin._kraken_token_request({"APIKey": "sk_live_test123"}))

    assert result is None
    assert mixin.token_mint_rate_limited is True


def test_token_request_does_not_flag_other_errors():
    """A genuine rejection is not mistaken for a rate limit, so it can still latch oauth_failed."""
    mixin, mock_session = make_http_mixin(200, "", json_data=make_error_response("Invalid API key", "KT-CT-1138"))
    with patch("aiohttp.ClientSession", return_value=mock_session):
        asyncio.run(mixin._kraken_token_request({"APIKey": "sk_live_test123"}))

    assert mixin.token_mint_rate_limited is False


def test_rate_limited_mint_does_not_latch_oauth_failed():
    """A rate-limited mint backs off rather than burning the refresh token and latching oauth_failed."""
    mixin = make_mixin(auth_method="api_key", key="sk_live_test123")
    mixin.refresh_token = "still-good-refresh"
    mixin.token_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    mixin._kraken_token_request = rate_limited_request(mixin)

    result = asyncio.run(mixin.check_and_refresh_oauth_token())

    assert result is False
    assert mixin.oauth_failed is False, "a rate limit must not latch oauth_failed"
    assert mixin.refresh_token == "still-good-refresh"
    assert mixin._kraken_token_request.call_count == 1, "must not retry into the same rate limit"
    assert mixin.token_mint_backoff.until is not None
    logged = " ".join(str(c.args[0]) for c in mixin.log.call_args_list)
    assert "Token mint rate limited" in logged, logged

    # The next caller inside the backoff does not mint
    mixin._kraken_token_request = AsyncMock()
    asyncio.run(mixin.check_and_refresh_oauth_token())
    mixin._kraken_token_request.assert_not_called()


def test_rate_limited_mint_honours_retry_after():
    """A Retry-After longer than the backoff schedule lengthens the wait."""
    mixin = make_mixin(auth_method="api_key", key="sk_live_test123")
    mixin._kraken_token_request = rate_limited_request(mixin, retry_after=2000)
    before = datetime.now(timezone.utc)

    asyncio.run(mixin.check_and_refresh_oauth_token())

    assert mixin.token_mint_backoff.until >= before + timedelta(seconds=1999)


def test_rate_limited_mint_keeps_a_still_valid_token():
    """A token inside the proactive-refresh window is still usable while the mint is rate limited."""
    mixin = make_mixin(auth_method="api_key", key="sk_live_test123")
    mixin.access_token = "still-good"
    mixin.token_expires_at = datetime.now(timezone.utc) + timedelta(minutes=3)
    mixin._kraken_token_request = rate_limited_request(mixin)

    assert asyncio.run(mixin.check_and_refresh_oauth_token()) is True
    assert mixin.access_token == "still-good"
