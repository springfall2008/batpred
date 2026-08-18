# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Test Sunsynk Cloud authentication
# -----------------------------------------------------------------------------

"""Tests for Sunsynk login: the pure-Python RSA helper and the three auth methods."""

import base64
import time
from unittest.mock import patch
from sunsynk_const import parse_rsa_public_key, rsa_encrypt_pkcs1v15
from tests.test_sunsynk_api import MockSunsynk
from tests.test_infra import run_async as run_async_local

# A real 1024-bit RSA public key, DER SubjectPublicKeyInfo, base64 — the same shape
# Sunsynk's /anonymous/publicKey returns (no PEM armour).
SUNSYNK_TEST_PUBLIC_KEY = "MIGeMA0GCSqGSIb3DQEBAQUAA4GMADCBiAKBgFwp+M48x3PUYA63ZF2xEl4pFrh+1qQuk4B0UeTKCAqU51A8BURJdRs4ECXJEdJnxgO3hlkJyjVgBaeJgajxTu+c1oyOtQn9KVvW" "/Se0LEytkZRABnOsJkGprKWuNDm6N5YXPEH5yfnAfCL7Drnsn8rj3RjPmkzCg8XHI6xHGQD3AgMBAAE="
SUNSYNK_TEST_MODULUS = 0x5C29F8CE3CC773D4600EB7645DB1125E2916B87ED6A42E93807451E4CA080A94E7503C054449751B381025C911D267C603B7865909CA356005A78981A8F14EEF9CD68C8EB509FD295BD6FD27B42C4CAD9194400673AC2641A9ACA5AE3439BA3796173C41F9C9F9C07C22FB0EB9EC9FCAE3DD18CF9A4CC283C5C723AC471900F7
# Private exponent, used ONLY by this test to decrypt and inspect the padding.
SUNSYNK_TEST_PRIVATE_D = 0x438FD67F5964328527E5A1E046CE87A87F2128C927E53394ED95AD1DB5A784C4F8CCD888593180521E71B7EC0379E54398CB4606AA2691A4D28053F7B8E12CA643EC0257950C49747469A6092B548F9358DCBD311FC69088457B4A76213C07F7937C8745144F9F8EF7DA792DA35AA4FB5E5458B6A36ACDAD3327C4066AF8A01
SUNSYNK_TEST_KEY_BYTES = 128


def _decrypt(ciphertext_b64):
    """Decrypt with the test private key, returning the raw padded encryption block."""
    raw = base64.b64decode(ciphertext_b64)
    plain = pow(int.from_bytes(raw, "big"), SUNSYNK_TEST_PRIVATE_D, SUNSYNK_TEST_MODULUS)
    return plain.to_bytes(SUNSYNK_TEST_KEY_BYTES, "big")


def test_parse_rsa_public_key():
    """The DER SubjectPublicKeyInfo parser recovers the exact modulus and exponent."""
    failed = False
    modulus, exponent = parse_rsa_public_key(base64.b64decode(SUNSYNK_TEST_PUBLIC_KEY))
    if modulus != SUNSYNK_TEST_MODULUS:
        print(f"ERROR: modulus mismatch, got {hex(modulus)}")
        failed = True
    if exponent != 65537:
        print(f"ERROR: exponent expected 65537, got {exponent}")
        failed = True
    assert not failed, "test_parse_rsa_public_key"


def test_parse_rsa_public_key_rejects_rubbish():
    """A body that is not a DER key raises rather than returning a bogus modulus.

    The near-full-length truncations (``[:-1]``, ``[:-2]``, ``[:-3]``) are here because a
    truncation that only clips the last one to three bytes of the exponent still parses
    every earlier TLV correctly, so it exercises a different failure path from an
    early/gross truncation: without a bounds check in ``_read_tlv``, Python's silent
    slice truncation lets these "succeed" with a corrupted exponent (0, 1 or 256 for
    this key) instead of raising — and an exponent of 1 would send the password
    effectively unencrypted.
    """
    failed = False
    full_key = base64.b64decode(SUNSYNK_TEST_PUBLIC_KEY)
    cases = [
        ("empty", b""),
        ("not a sequence", b"\x02\x01\x05"),
        ("truncated", full_key[:20]),
        ("truncated by 1 byte", full_key[:-1]),
        ("truncated by 2 bytes", full_key[:-2]),
        ("truncated by 3 bytes", full_key[:-3]),
    ]
    for name, payload in cases:
        try:
            modulus, exponent = parse_rsa_public_key(payload)
            print(f"ERROR: {name} was accepted as a public key (modulus={hex(modulus)}, exponent={exponent})")
            failed = True
        except (ValueError, IndexError):
            pass
    assert not failed, "test_parse_rsa_public_key_rejects_rubbish"


def test_rsa_encrypt_round_trip():
    """Encryption produces a well-formed PKCS#1 v1.5 type-2 block recovering the password."""
    failed = False
    for password in ("hunter2", "a", "x" * 117, "pa55 w0rd! £é"):
        block = _decrypt(rsa_encrypt_pkcs1v15(SUNSYNK_TEST_PUBLIC_KEY, password))
        if block[0] != 0x00 or block[1] != 0x02:
            print(f"ERROR: {password!r} block header {block[:2].hex()} is not 0002")
            failed = True
            continue
        separator = block.index(b"\x00", 2)
        padding = block[2:separator]
        if len(padding) < 8:
            print(f"ERROR: {password!r} padding only {len(padding)} bytes, PKCS#1 requires >= 8")
            failed = True
        if not all(padding):
            print(f"ERROR: {password!r} padding contains a zero byte, which truncates the message")
            failed = True
        recovered = block[separator + 1 :].decode("utf-8")
        if recovered != password:
            print(f"ERROR: {password!r} round-tripped to {recovered!r}")
            failed = True
    assert not failed, "test_rsa_encrypt_round_trip"


def test_rsa_encrypt_is_randomised():
    """The same password encrypts differently each time, as PKCS#1 v1.5 requires."""
    failed = False
    first = rsa_encrypt_pkcs1v15(SUNSYNK_TEST_PUBLIC_KEY, "hunter2")
    second = rsa_encrypt_pkcs1v15(SUNSYNK_TEST_PUBLIC_KEY, "hunter2")
    if first == second:
        print("ERROR: identical ciphertexts, padding is not randomised")
        failed = True
    if len(base64.b64decode(first)) != SUNSYNK_TEST_KEY_BYTES:
        print(f"ERROR: ciphertext is {len(base64.b64decode(first))} bytes, expected {SUNSYNK_TEST_KEY_BYTES}")
        failed = True
    assert not failed, "test_rsa_encrypt_is_randomised"


def test_rsa_encrypt_rejects_oversize_password():
    """A password too long for the key is refused rather than silently truncated."""
    failed = False
    try:
        rsa_encrypt_pkcs1v15(SUNSYNK_TEST_PUBLIC_KEY, "x" * 118)
        print("ERROR: oversize password was accepted")
        failed = True
    except ValueError:
        pass
    assert not failed, "test_rsa_encrypt_rejects_oversize_password"


def test_password_login_uses_rsa_and_signs():
    """The default method fetches a public key, encrypts the password and signs both calls."""
    failed = False
    s = MockSunsynk(auth_method="password")
    seen = {}

    async def fake_request(method, endpoint_key, sn=None, params=None, body=None):
        """Record each auth call and return a plausible Sunsynk response."""
        seen[endpoint_key] = {"method": method, "params": params, "body": body}
        if endpoint_key == "public_key":
            return SUNSYNK_TEST_PUBLIC_KEY
        return {"access_token": "tok-abc", "refresh_token": "ref-abc", "expires_in": 3600}

    with patch.object(s, "_request", side_effect=fake_request):
        ok = run_async_local(s.fetch_token())
    if not ok:
        print("ERROR: fetch_token returned False")
        failed = True
    if "public_key" not in seen:
        print("ERROR: the public key endpoint was never called")
        failed = True
    else:
        params = seen["public_key"].get("params") or {}
        for key in ("nonce", "source", "sign"):
            if key not in params:
                print(f"ERROR: public key request missing {key}")
                failed = True
    token_body = (seen.get("token") or {}).get("body") or {}
    if token_body.get("password") == s.password:
        print("ERROR: the plaintext password was sent on the RSA path")
        failed = True
    if not token_body.get("password"):
        print("ERROR: no encrypted password in the token request")
        failed = True
    for key in ("nonce", "sign", "source", "client_id", "grant_type", "username"):
        if key not in token_body:
            print(f"ERROR: token request missing {key}")
            failed = True
    if s.access_token != "tok-abc":
        print(f"ERROR: access token not stored, got {s.access_token!r}")
        failed = True
    assert not failed, "test_password_login_uses_rsa_and_signs"


def test_legacy_login_sends_plaintext_and_skips_public_key():
    """password_legacy posts once, with the plaintext password and no public-key call."""
    failed = False
    s = MockSunsynk(auth_method="password_legacy")
    seen = {}

    async def fake_request(method, endpoint_key, sn=None, params=None, body=None):
        """Record each auth call and return a plausible Sunsynk response."""
        seen[endpoint_key] = {"method": method, "params": params, "body": body}
        return {"access_token": "tok-legacy", "refresh_token": "ref-legacy", "expires_in": 3600}

    with patch.object(s, "_request", side_effect=fake_request):
        ok = run_async_local(s.fetch_token())
    if not ok:
        print("ERROR: legacy fetch_token returned False")
        failed = True
    if "public_key" in seen:
        print("ERROR: the legacy path fetched a public key")
        failed = True
    if "token_legacy" not in seen:
        print("ERROR: the legacy token endpoint was not called")
        failed = True
    body = (seen.get("token_legacy") or {}).get("body") or {}
    if body.get("password") != "hunter2":
        print(f"ERROR: legacy password should be plaintext, got {body.get('password')!r}")
        failed = True
    if body.get("areaCode") != "sunsynk":
        print(f"ERROR: legacy request missing areaCode, got {body.get('areaCode')!r}")
        failed = True
    if s.access_token != "tok-legacy":
        print(f"ERROR: access token not stored, got {s.access_token!r}")
        failed = True
    assert not failed, "test_legacy_login_sends_plaintext_and_skips_public_key"


def test_rsa_login_never_falls_back_to_plaintext():
    """A failing public-key step must not downgrade to sending the plaintext password.

    Auto-downgrade would turn any externally-triggerable failure of the public-key call
    into a plaintext credential transmission. TLS-intercepting middleboxes are common,
    and against one of those the RSA layer is the only thing protecting the password.
    """
    failed = False
    s = MockSunsynk(auth_method="password")
    seen = []

    async def fake_request(method, endpoint_key, sn=None, params=None, body=None):
        """Fail the public key call, and record anything sent afterwards."""
        seen.append((endpoint_key, body))
        if endpoint_key == "public_key":
            return {}
        return {"access_token": "tok-should-not-happen", "expires_in": 3600}

    with patch.object(s, "_request", side_effect=fake_request):
        ok = run_async_local(s.fetch_token())
    if ok:
        print("ERROR: fetch_token reported success after the public key call failed")
        failed = True
    for endpoint_key, body in seen:
        if endpoint_key == "token_legacy":
            print("ERROR: the RSA path fell back to the legacy endpoint")
            failed = True
        if body and body.get("password") == "hunter2":
            print(f"ERROR: plaintext password sent to {endpoint_key}")
            failed = True
    if not any("legacy" in str(m).lower() for m in s.log_messages):
        print("ERROR: no diagnostic pointing the user at password_legacy")
        failed = True
    assert not failed, "test_rsa_login_never_falls_back_to_plaintext"


def test_oauth_method_skips_login_entirely():
    """The Predbat.com path uses the injected token and never calls a login endpoint."""
    failed = False
    s = MockSunsynk(auth_method="oauth")
    seen = []

    async def fake_request(method, endpoint_key, sn=None, params=None, body=None):
        """Record any call, which for this method should never happen."""
        seen.append(endpoint_key)
        return {}

    with patch.object(s, "_request", side_effect=fake_request):
        ok = run_async_local(s.fetch_token())
    if not ok:
        print("ERROR: oauth fetch_token should succeed with an injected token")
        failed = True
    if seen:
        print(f"ERROR: oauth path called login endpoints: {seen}")
        failed = True
    if s.access_token != "test-token":
        print(f"ERROR: injected token not used, got {s.access_token!r}")
        failed = True
    assert not failed, "test_oauth_method_skips_login_entirely"


def test_debug_trace_redacts_credentials():
    """Debug tracing never writes a password or bearer token to the log, at any nesting depth.

    The flat case is the request side. The NESTED case is the one that mattered and the one
    a top-level-only redact() missed: _request traces the whole {code, msg, success, data}
    response envelope, and the login response carries access_token/refresh_token one level
    down inside `data`, so a Sunsynk bearer token - full control of the user's inverter -
    was logged verbatim. api_debug defaults to True specifically so testers paste raw
    traffic into GitHub issues, and docs/components.md promises "with credentials redacted".
    A list of envelopes is covered too, since a Sunsynk `data` is as often a list as a dict.
    """
    failed = False
    s = MockSunsynk()
    s.debug_api("POST", "token", {"username": "test@example.com", "password": "hunter2", "sign": "abc123", "access_token": "tok-abc"})
    s.debug_api("<-", "token", {"code": 0, "msg": "Success", "success": True, "data": {"access_token": "nested-tok", "refresh_token": "nested-ref", "expires_in": 3600, "user": {"email": "test@example.com", "password": "nested-pw"}}})
    s.debug_api("<-", "inverter_list", {"success": True, "data": {"infos": [{"sn": "INV1", "token": "list-tok"}]}})
    joined = " ".join(str(m) for m in s.log_messages)
    for secret in ("hunter2", "tok-abc", "abc123", "nested-tok", "nested-ref", "nested-pw", "list-tok"):
        if secret in joined:
            print(f"ERROR: {secret!r} leaked into the debug log")
            failed = True
    if "test@example.com" not in joined:
        print("ERROR: non-secret fields should still be traced")
        failed = True
    if "INV1" not in joined or "3600" not in joined:
        print("ERROR: non-secret nested fields should survive redaction so the trace stays useful")
        failed = True
    assert not failed, "test_debug_trace_redacts_credentials"


def test_run_refreshes_an_expired_oauth_token():
    """An expired injected token must be refreshed before the cycle polls anything.

    fetch_token() for oauth only reports whether a token is held, and nothing ever called
    check_and_refresh_oauth_token(): token_expires_at and token_hash were accepted, plumbed
    into _init_oauth and then dead. docs/inverter-setup.md says the platform "injects and
    refreshes" the token, so an expired one broke every call until the process restarted.
    """
    failed = False
    s = MockSunsynk(auth_method="oauth")
    s.token_expires_at = 0  # expired, so _token_needs_refresh() is True
    refreshes = []

    async def fake_do_refresh():
        """Stand in for the oauth-refresh edge function, installing a fresh token."""
        refreshes.append(1)
        s.access_token = "refreshed-token"
        s.token_expires_at = time.time() + 3600
        return True

    async def fake_get(endpoint_key, sn=None, params=None):
        """No inverters, so the cycle stops straight after discovery."""
        return {}

    with patch.object(s, "_do_refresh", side_effect=fake_do_refresh), patch.object(s, "_get", side_effect=fake_get):
        run_async_local(s.run(0, True))
    if not refreshes:
        print("ERROR: run() never attempted an OAuth refresh for an expired injected token")
        failed = True
    assert not failed, "test_run_refreshes_an_expired_oauth_token"


def test_run_stops_when_the_oauth_token_needs_reauthorisation():
    """A token the edge function says needs re-authorisation stops the cycle before any polling."""
    failed = False
    s = MockSunsynk(auth_method="oauth")
    s.oauth_failed = True  # the edge function already reported needs_reauth
    calls = []

    async def fake_get(endpoint_key, sn=None, params=None):
        """Record any poll, which must not happen with a dead token."""
        calls.append(endpoint_key)
        return {}

    with patch.object(s, "_get", side_effect=fake_get):
        result = run_async_local(s.run(0, True))
    if calls:
        print(f"ERROR: the cycle kept polling with a token that needs re-authorisation: {calls}")
        failed = True
    if result is not False:
        print(f"ERROR: run() should return False when the OAuth token cannot be refreshed, got {result!r}")
        failed = True
    assert not failed, "test_run_stops_when_the_oauth_token_needs_reauthorisation"


def run_sunsynk_auth_tests(my_predbat):
    """Run all Sunsynk authentication tests."""
    failed = False
    for name, fn in [
        ("parse_public_key", test_parse_rsa_public_key),
        ("parse_rejects_rubbish", test_parse_rsa_public_key_rejects_rubbish),
        ("encrypt_round_trip", test_rsa_encrypt_round_trip),
        ("encrypt_randomised", test_rsa_encrypt_is_randomised),
        ("encrypt_oversize", test_rsa_encrypt_rejects_oversize_password),
        ("password_login_rsa", test_password_login_uses_rsa_and_signs),
        ("legacy_login_plaintext", test_legacy_login_sends_plaintext_and_skips_public_key),
        ("no_plaintext_fallback", test_rsa_login_never_falls_back_to_plaintext),
        ("oauth_skips_login", test_oauth_method_skips_login_entirely),
        ("oauth_run_refreshes", test_run_refreshes_an_expired_oauth_token),
        ("oauth_run_stops_on_reauth", test_run_stops_when_the_oauth_token_needs_reauthorisation),
        ("debug_redaction", test_debug_trace_redacts_credentials),
    ]:
        try:
            if fn():
                print(f"  FAILED: sunsynk_auth.{name}")
                failed = True
        except Exception as e:
            print(f"  EXCEPTION in sunsynk_auth.{name}: {e}")
            import traceback

            traceback.print_exc()
            failed = True
    return failed
