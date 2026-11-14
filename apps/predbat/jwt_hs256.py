# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""
HS256 JSON Web Tokens with only the standard library.

The MCP server signs its OAuth access tokens with HS256 and nothing else. PyJWT did this before, but
importing it loads the cryptography package (about 6 MB of idle memory) for algorithms Predbat never
uses. encode() makes the same token PyJWT would for the same claims, and decode() checks what PyJWT's
decode() checked for these tokens: the HS256 signature, exp, iat, nbf and the audience. Tokens issued
by either verify with the other.
"""

import base64
import binascii
import hashlib
import hmac
import json
import time
from calendar import timegm
from datetime import datetime

ALGORITHM = "HS256"
_HEADER = {"alg": ALGORITHM, "typ": "JWT"}
# Claims PyJWT turns from a datetime into whole seconds since the epoch
_TIME_CLAIMS = ("exp", "iat", "nbf")


class JWTError(Exception):
    """Base class for a token that cannot be accepted."""


class DecodeError(JWTError):
    """The token is malformed, uses another algorithm, or has a claim of the wrong type."""


class InvalidSignatureError(DecodeError):
    """The signature does not match the token, so it was not signed with this key or was changed."""


class ExpiredSignatureError(JWTError):
    """The token's exp has passed."""


class ImmatureSignatureError(JWTError):
    """The token's iat or nbf is in the future."""


class InvalidAudienceError(JWTError):
    """The token is not for the expected audience, or names none."""


def _b64encode(data):
    """Base64url without padding, as every JWT segment is."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(segment):
    """Undo _b64encode, raising DecodeError on anything that is not base64url."""
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except (binascii.Error, ValueError) as exc:
        raise DecodeError("Invalid token segment: {}".format(exc)) from exc


def _json_object(segment, what):
    """Decode one segment to a JSON object (dict), raising DecodeError otherwise."""
    try:
        value = json.loads(_b64decode(segment))
    except ValueError as exc:
        raise DecodeError("Invalid {} JSON: {}".format(what, exc)) from exc
    if not isinstance(value, dict):
        raise DecodeError("Invalid {}: must be a JSON object".format(what))
    return value


def sign(signing_input, key):
    """The base64url HMAC-SHA256 signature of signing_input (header.payload) under key (str or bytes)."""
    if isinstance(key, str):
        key = key.encode("utf-8")
    return _b64encode(hmac.new(key, signing_input.encode("ascii"), hashlib.sha256).digest())


def encode(claims, key):
    """
    Sign claims as an HS256 JWT and return the token.

    exp, iat and nbf may be datetimes; like PyJWT, they are sent as whole seconds since the epoch.
    The caller's dict is not changed.
    """
    payload = dict(claims)
    for name in _TIME_CLAIMS:
        if isinstance(payload.get(name), datetime):
            payload[name] = timegm(payload[name].utctimetuple())
    header_segment = _b64encode(json.dumps(_HEADER, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    payload_segment = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = "{}.{}".format(header_segment, payload_segment)
    return "{}.{}".format(signing_input, sign(signing_input, key))


def _split(token):
    """The header, payload and signature segments of token, raising DecodeError unless there are exactly three."""
    if not isinstance(token, str):
        raise DecodeError("Invalid token type: {}".format(type(token).__name__))
    parts = token.split(".")
    if len(parts) != 3:
        raise DecodeError("Not enough segments" if len(parts) < 3 else "Too many segments")
    return parts


def decode_unverified(token):
    """The token's claims without checking anything, for logging a token that was rejected. Never trust the result."""
    header_segment, payload_segment, _ = _split(token)
    _json_object(header_segment, "header")
    return _json_object(payload_segment, "payload")


def _time_claim(claims, name):
    """A numeric time claim, or None when absent; null, a bool or text is a DecodeError, as PyJWT rejected them."""
    if name not in claims:
        return None
    value = claims[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DecodeError("The {} claim must be a number".format(name))
    return value


def decode(token, key, audience, leeway=0, now=None):
    """
    Verify an HS256 token signed with key and return its claims.

    The signature must match and the header must say HS256. exp, when present, must be in the
    future, and iat and nbf, when present, must not be. The aud claim must be audience or a list
    containing it. now (seconds since the epoch) is for tests. Raises a JWTError subclass on any
    failure, laid out as PyJWT's: InvalidSignatureError is a DecodeError, the others are not.
    """
    header_segment, payload_segment, signature = _split(token)
    header = _json_object(header_segment, "header")
    if header.get("alg") != ALGORITHM:
        raise DecodeError("The specified algorithm is not allowed: {}".format(header.get("alg")))
    # Compare bytes: compare_digest raises TypeError for a non-ASCII str, and a client can send one in the signature
    expected = sign("{}.{}".format(header_segment, payload_segment), key).encode("ascii")
    if not hmac.compare_digest(expected, signature.encode("utf-8", "surrogatepass")):
        raise InvalidSignatureError("Signature verification failed")
    claims = _json_object(payload_segment, "payload")

    if now is None:
        now = time.time()
    exp = _time_claim(claims, "exp")
    iat = _time_claim(claims, "iat")
    nbf = _time_claim(claims, "nbf")
    if exp is not None and exp <= now - leeway:
        raise ExpiredSignatureError("Signature has expired")
    if iat is not None and iat > now + leeway:
        raise ImmatureSignatureError("The token is not yet valid (iat)")
    if nbf is not None and nbf > now + leeway:
        raise ImmatureSignatureError("The token is not yet valid (nbf)")

    if "aud" not in claims:
        raise InvalidAudienceError("Token is missing the audience (aud) claim")
    token_audience = claims["aud"]
    if isinstance(token_audience, str):
        token_audience = [token_audience]
    if not isinstance(token_audience, list) or not all(isinstance(item, str) for item in token_audience):
        raise InvalidAudienceError("Invalid audience claim")
    if audience not in token_audience:
        raise InvalidAudienceError("Audience doesn't match")
    return claims
