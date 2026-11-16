# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for jwt_hs256, the standard-library HS256 JWT signer the MCP server uses instead of PyJWT.

The reference tokens below were made by PyJWT 2.13 with REFERENCE_KEY, so tokens issued before the
switch keep verifying after it, and tokens issued after it are byte-for-byte what PyJWT would make.
"""

import base64
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

import jwt_hs256

REFERENCE_KEY = "pyjwt-reference-key"
REFERENCE_AUDIENCE = "http://localhost:8199"
REFERENCE_CLAIMS = {"sub": "client-1", "client_id": "client-1", "aud": REFERENCE_AUDIENCE, "scope": "mcp:read mcp:write", "type": "access", "iat": 1700000000, "exp": 4102444800}
# PyJWT 2.13: jwt.encode(REFERENCE_CLAIMS, REFERENCE_KEY, algorithm="HS256")
# cspell:disable-next-line
REFERENCE_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJjbGllbnQtMSIsImNsaWVudF9pZCI6ImNsaWVudC0xIiwiYXVkIjoiaHR0cDovL2xvY2FsaG9zdDo4MTk5Iiwic2NvcGUiOiJtY3A6cmVhZCBtY3A6d3JpdGUiLCJ0eXBlIjoiYWNjZXNzIiwiaWF0IjoxNzAwMDAwMDAwLCJleHAiOjQxMDI0NDQ4MDB9.L_6hYZsULaPVJktegUOiBzTvwb0Q788HAwQoLRtB3BQ"
# The same claims with exp 1700000600, ten minutes after iat
# cspell:disable-next-line
REFERENCE_EXPIRED_TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJjbGllbnQtMSIsImNsaWVudF9pZCI6ImNsaWVudC0xIiwiYXVkIjoiaHR0cDovL2xvY2FsaG9zdDo4MTk5Iiwic2NvcGUiOiJtY3A6cmVhZCBtY3A6d3JpdGUiLCJ0eXBlIjoiYWNjZXNzIiwiaWF0IjoxNzAwMDAwMDAwLCJleHAiOjE3MDAwMDA2MDB9.nUr805f_KxWM_9NFv1Cquiu1tBUWe1_ZelfR1is8RPE"


def _b64(data):
    """Base64url-encode bytes without padding, as a JWT segment is."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _segment(obj):
    """A JWT header or payload segment for a JSON object."""
    return _b64(json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def _signed(header, claims, key=REFERENCE_KEY):
    """A token with any header and claims, correctly HS256-signed with key, so only the claims or header can be at fault."""
    signing_input = "{}.{}".format(_segment(header), _segment(claims))
    return "{}.{}".format(signing_input, jwt_hs256.sign(signing_input, key))


def _expect_error(name, error_class, words, call):
    """Run call and check it raises error_class with each of words in its message; returns True on a failure."""
    try:
        result = call()
    except error_class as exc:
        message = str(exc).lower()
        missing = [word for word in words if word not in message]
        if missing:
            print("  ERROR: {}: message {!r} does not mention {}".format(name, str(exc), missing))
            return True
        return False
    except Exception as exc:  # pylint: disable=broad-except
        print("  ERROR: {}: raised {} ({}) rather than {}".format(name, type(exc).__name__, exc, error_class.__name__))
        return True
    print("  ERROR: {}: accepted, returning {}".format(name, result))
    return True


def test_jwt_hs256_pyjwt_compatible():
    """Tokens PyJWT issued verify, and new tokens are the ones PyJWT would have issued."""
    failed = False
    print("**** Testing jwt_hs256 against PyJWT reference tokens ****")

    print("Test: a token PyJWT issued decodes to its claims")
    try:
        claims = jwt_hs256.decode(REFERENCE_TOKEN, REFERENCE_KEY, audience=REFERENCE_AUDIENCE)
    except jwt_hs256.JWTError as exc:
        print("  ERROR: the PyJWT reference token was rejected: {}".format(exc))
        failed = True
    else:
        if claims != REFERENCE_CLAIMS:
            print("  ERROR: decoded {} expected {}".format(claims, REFERENCE_CLAIMS))
            failed = True

    print("Test: encoding the same claims gives PyJWT's token byte for byte, with datetimes as whole seconds")
    with_datetimes = dict(REFERENCE_CLAIMS, iat=datetime.fromtimestamp(1700000000, timezone.utc), exp=datetime.fromtimestamp(4102444800, timezone.utc))
    for name, claims in (("integer claims", REFERENCE_CLAIMS), ("datetime claims", with_datetimes)):
        token = jwt_hs256.encode(claims, REFERENCE_KEY)
        if token != REFERENCE_TOKEN:
            print("  ERROR: {}: encoded {} expected {}".format(name, token, REFERENCE_TOKEN))
            failed = True

    print("Test: encode does not change the caller's claims")
    if not isinstance(with_datetimes["exp"], datetime):
        print("  ERROR: encode replaced the caller's datetime with {!r}".format(with_datetimes["exp"]))
        failed = True

    print("Test: a PyJWT token past its exp is rejected as expired, and so is one whose exp is exactly now")
    failed |= _expect_error("expired reference", jwt_hs256.ExpiredSignatureError, ["expired"], lambda: jwt_hs256.decode(REFERENCE_EXPIRED_TOKEN, REFERENCE_KEY, audience=REFERENCE_AUDIENCE))
    failed |= _expect_error("exp equal to now", jwt_hs256.ExpiredSignatureError, ["expired"], lambda: jwt_hs256.decode(REFERENCE_EXPIRED_TOKEN, REFERENCE_KEY, audience=REFERENCE_AUDIENCE, now=1700000600))
    try:
        jwt_hs256.decode(REFERENCE_EXPIRED_TOKEN, REFERENCE_KEY, audience=REFERENCE_AUDIENCE, now=1700000599)
    except jwt_hs256.JWTError as exc:
        print("  ERROR: a token one second before its exp was rejected: {}".format(exc))
        failed = True

    print("Test: decode_unverified reads the claims without the key, for the log line on a rejected token")
    if jwt_hs256.decode_unverified(REFERENCE_EXPIRED_TOKEN).get("exp") != 1700000600:
        print("  ERROR: decode_unverified did not return the claims")
        failed = True
    return failed


def test_jwt_hs256_rejects():
    """Every way a token can be wrong is an error, never a payload."""
    failed = False
    print("**** Testing jwt_hs256 rejections ****")
    header = {"alg": "HS256", "typ": "JWT"}
    good = dict(REFERENCE_CLAIMS)
    decode = jwt_hs256.decode

    print("Test: the wrong key, or a payload changed after signing, fails the signature")
    failed |= _expect_error("wrong key", jwt_hs256.InvalidSignatureError, ["signature"], lambda: decode(REFERENCE_TOKEN, "another-key", audience=REFERENCE_AUDIENCE))
    head, _, signature = REFERENCE_TOKEN.split(".")
    tampered = "{}.{}.{}".format(head, _segment(dict(good, scope="mcp:read mcp:write mcp:control")), signature)
    failed |= _expect_error("tampered payload", jwt_hs256.InvalidSignatureError, ["signature"], lambda: decode(tampered, REFERENCE_KEY, audience=REFERENCE_AUDIENCE))
    # aiohttp reads headers as latin-1, so a client can send a signature with bytes >= 0x80; hmac.compare_digest raises TypeError on non-ASCII str
    non_ascii = "{}.{}.{}".format(head, REFERENCE_TOKEN.split(".")[1], "\u00e9" + signature[1:])
    failed |= _expect_error("non-ASCII signature", jwt_hs256.InvalidSignatureError, ["signature"], lambda: decode(non_ascii, REFERENCE_KEY, audience=REFERENCE_AUDIENCE))

    print("Test: only HS256 is accepted, so an unsigned 'none' token or another algorithm is refused")
    unsigned = "{}.{}.".format(_segment({"alg": "none", "typ": "JWT"}), _segment(good))
    failed |= _expect_error("alg none", jwt_hs256.DecodeError, ["algorithm"], lambda: decode(unsigned, REFERENCE_KEY, audience=REFERENCE_AUDIENCE))
    failed |= _expect_error("alg HS512", jwt_hs256.DecodeError, ["algorithm"], lambda: decode(_signed({"alg": "HS512", "typ": "JWT"}, good), REFERENCE_KEY, audience=REFERENCE_AUDIENCE))

    print("Test: the audience must be the expected one, as a string or within a list")
    failed |= _expect_error("other audience", jwt_hs256.InvalidAudienceError, ["audience"], lambda: decode(REFERENCE_TOKEN, REFERENCE_KEY, audience="https://elsewhere.example"))
    failed |= _expect_error("no aud claim", jwt_hs256.InvalidAudienceError, ["audience"], lambda: decode(_signed(header, {k: v for k, v in good.items() if k != "aud"}), REFERENCE_KEY, audience=REFERENCE_AUDIENCE))
    failed |= _expect_error("aud not a string", jwt_hs256.InvalidAudienceError, ["audience"], lambda: decode(_signed(header, dict(good, aud=8199)), REFERENCE_KEY, audience=REFERENCE_AUDIENCE))
    try:
        decode(_signed(header, dict(good, aud=["https://elsewhere.example", REFERENCE_AUDIENCE])), REFERENCE_KEY, audience=REFERENCE_AUDIENCE)
    except jwt_hs256.JWTError as exc:
        print("  ERROR: a token listing the expected audience was rejected: {}".format(exc))
        failed = True

    print("Test: a token issued in the future, or not valid yet, is refused")
    failed |= _expect_error("future iat", jwt_hs256.ImmatureSignatureError, ["not yet valid"], lambda: decode(REFERENCE_TOKEN, REFERENCE_KEY, audience=REFERENCE_AUDIENCE, now=1699999999))
    failed |= _expect_error("future nbf", jwt_hs256.ImmatureSignatureError, ["not yet valid"], lambda: decode(_signed(header, dict(good, nbf=4000000000)), REFERENCE_KEY, audience=REFERENCE_AUDIENCE))

    print("Test: time claims must be numbers")
    for claim in ("exp", "iat", "nbf"):
        failed |= _expect_error("{} as text".format(claim), jwt_hs256.DecodeError, [claim], lambda claim=claim: decode(_signed(header, dict(good, **{claim: "4102444800"})), REFERENCE_KEY, audience=REFERENCE_AUDIENCE))
    failed |= _expect_error("exp as a boolean", jwt_hs256.DecodeError, ["exp"], lambda: decode(_signed(header, dict(good, exp=True)), REFERENCE_KEY, audience=REFERENCE_AUDIENCE))
    # A null claim is present, not absent: PyJWT rejected "exp": null, and treating it as absent would make a token that never expires
    for claim in ("exp", "iat", "nbf"):
        failed |= _expect_error("{} as null".format(claim), jwt_hs256.DecodeError, [claim], lambda claim=claim: decode(_signed(header, dict(good, **{claim: None})), REFERENCE_KEY, audience=REFERENCE_AUDIENCE))

    print("Test: a malformed token is a DecodeError, from decode and decode_unverified alike")
    signed_list = _signed(header, ["not", "an", "object"])
    for name, token in (("two segments", "abc.def"), ("four segments", "a.b.c.d"), ("not base64", "!!!.@@@.###"), ("not JSON", "{}.{}.{}".format(_b64(b"{not json"), _segment(good), signature)), ("payload a list", signed_list), ("not a string", None)):
        failed |= _expect_error(name, jwt_hs256.DecodeError, [], lambda token=token: decode(token, REFERENCE_KEY, audience=REFERENCE_AUDIENCE))
        failed |= _expect_error(name + " (unverified)", jwt_hs256.DecodeError, [], lambda token=token: jwt_hs256.decode_unverified(token))

    print("Test: every error is a JWTError, so one except clause catches them all")
    for error_class in (jwt_hs256.DecodeError, jwt_hs256.InvalidSignatureError, jwt_hs256.ExpiredSignatureError, jwt_hs256.ImmatureSignatureError, jwt_hs256.InvalidAudienceError):
        if not issubclass(error_class, jwt_hs256.JWTError):
            print("  ERROR: {} is not a JWTError".format(error_class.__name__))
            failed = True
    return failed


def test_mcp_does_not_load_pyjwt():
    """Importing the MCP server loads neither PyJWT nor the cryptography package it pulls in (about 6 MB of idle memory)."""
    failed = False
    print("**** Testing that the MCP server does not import PyJWT or cryptography ****")
    source_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    probe = "import sys, web_mcp; print(sorted(name for name in ('jwt', 'cryptography') if name in sys.modules))"
    result = subprocess.run([sys.executable, "-c", probe], cwd=source_dir, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode != 0:
        print("  ERROR: importing web_mcp failed: {}".format(result.stderr[-2000:]))
        return True
    loaded = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
    if loaded != "[]":
        print("  ERROR: importing web_mcp loaded {}".format(loaded))
        failed = True
    return failed


def run_jwt_hs256_tests(my_predbat=None):
    """Run every jwt_hs256 test, returning True if any of them failed."""
    failed = False
    failed |= test_jwt_hs256_pyjwt_compatible()
    failed |= test_jwt_hs256_rejects()
    failed |= test_mcp_does_not_load_pyjwt()
    return failed
