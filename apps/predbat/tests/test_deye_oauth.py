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


def run_deye_oauth_tests(my_predbat):
    """Run all DEYE auth tests."""
    failed = False
    for name, fn in [
        ("sha256_login_payload", test_sha256_and_login_payload),
        ("auth_headers_bearer", test_auth_headers_bearer),
        ("post_401_refresh_retry", test_post_401_refreshes_then_retries),
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
