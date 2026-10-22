# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Kraken API key generator, behind the web UI's Advanced -> Kraken API Key page.

E.ON Next stopped accepting email/password for the Kraken API in October 2026 and its website
offers no API key. A user who is signed in to their supplier's website can copy the token their
browser sends to Kraken; with it, these calls read the account's current key and, when the user
confirms, mint a new one.

Kraken returns an existing key masked (sk_live_ plus 4 characters, then asterisks). The only way to
see a full key is regenerateSecretKey, which REPLACES the old one and so stops every other app using
it - which is why checking and generating are separate steps.

Nothing here logs or stores the token or the key; callers must not either.

_kraken_request() is a second GraphQL POST path beside KrakenAPI.async_graphql_query() in kraken.py,
on purpose: that one is bound to the component's own token lifecycle (refresh, retry on auth errors),
while this uses a one-off token the user pasted and must never refresh or retry with it. They share
the error redaction (summarise_token_errors); a fix to how Kraken responses are read in one may well
belong in the other too.
"""

import asyncio
import json

import aiohttp

from kraken_auth_mixin import summarise_token_errors

KRAKEN_KEY_TIMEOUT_SECONDS = 30
KRAKEN_TOKEN_PREFIXES = ("authorization:", "jwt ", "bearer ")

KRAKEN_LIVE_SECRET_KEY_QUERY = "{ viewer { liveSecretKey } }"
KRAKEN_REGENERATE_SECRET_KEY_MUTATION = "mutation { regenerateSecretKey { key } }"
KRAKEN_OBTAIN_TOKEN_MUTATION = """mutation obtainKrakenToken($input: ObtainJSONWebTokenInput!) {
    obtainKrakenToken(input: $input) {
        token
    }
}"""


def normalise_kraken_token(text):
    """Return the pasted token with whitespace and any copied header name or JWT/Bearer prefix removed."""
    token = str(text or "").strip()
    stripped = True
    while stripped:
        stripped = False
        for prefix in KRAKEN_TOKEN_PREFIXES:
            if token.lower().startswith(prefix):
                token = token[len(prefix) :].strip()
                stripped = True
    # A JWT never contains whitespace, and a header copied wrapped across lines would otherwise keep
    # its line breaks - which aiohttp refuses with a plain ValueError rather than a ClientError
    return "".join(token.split())


async def _kraken_request(base_url, payload, secrets, token=None):
    """POST one GraphQL request to Kraken; return (data, None) on success or (None, log-safe error)."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = token
    try:
        timeout = aiohttp.ClientTimeout(total=KRAKEN_KEY_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{base_url}/v1/graphql/", json=payload, headers=headers) as response:
                status = response.status
                text = await response.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return None, "Could not reach Kraken ({})".format(type(e).__name__)

    try:
        body = json.loads(text)
    except ValueError:
        return None, "Kraken returned HTTP {}".format(status)
    if not isinstance(body, dict):
        return None, "Kraken returned HTTP {}".format(status)
    if body.get("errors"):
        return None, summarise_token_errors(body.get("errors"), secrets) or "Kraken refused the request"
    if status != 200:
        return None, "Kraken returned HTTP {}".format(status)
    data = body.get("data")
    if data is None:
        return {}, None
    if not isinstance(data, dict):
        return None, "Kraken returned an unexpected response"
    return data, None


async def check_kraken_key(base_url, token):
    """Report the account's current API key without changing it.

    Returns {"status": "none"} when there is no key, {"status": "existing", "masked": ...} when Kraken
    only shows it masked, {"status": "full", "key": ...} if it comes back whole, {"status": "unknown"} when
    the reply does not answer the question (no viewer, or no liveSecretKey field), or {"status": "error", "error": ...}.
    "unknown" is kept apart from "none" because only "none" lets the page generate without a confirmation.
    """
    data, error = await _kraken_request(base_url, {"query": KRAKEN_LIVE_SECRET_KEY_QUERY}, [token], token=token)
    if error:
        return {"status": "error", "error": error}
    viewer = data.get("viewer")
    if not isinstance(viewer, dict) or "liveSecretKey" not in viewer:
        return {"status": "unknown"}
    key = viewer.get("liveSecretKey")
    if key is None or key == "":
        return {"status": "none"}
    if not isinstance(key, str):
        return {"status": "unknown"}
    if "*" in key:
        return {"status": "existing", "masked": key}
    return {"status": "full", "key": key}


async def generate_kraken_key(base_url, token):
    """Mint a new API key, replacing any existing one, then prove it signs in.

    Returns {"status": "generated", "key": ..., "verified": bool} or {"status": "error", "error": ...}. Once
    regenerate succeeds the old key is gone, so a failed sign-in check still returns the new key.
    """
    data, error = await _kraken_request(base_url, {"query": KRAKEN_REGENERATE_SECRET_KEY_MUTATION}, [token], token=token)
    if error:
        return {"status": "error", "error": error}
    regenerated = data.get("regenerateSecretKey")
    key = regenerated.get("key") if isinstance(regenerated, dict) else None
    if not isinstance(key, str) or not key:
        return {"status": "error", "error": "Kraken did not return a key"}

    verify_payload = {"query": KRAKEN_OBTAIN_TOKEN_MUTATION, "variables": {"input": {"APIKey": key}}}
    data, error = await _kraken_request(base_url, verify_payload, [token, key])
    verified = not error and isinstance(data.get("obtainKrakenToken"), dict) and bool(data["obtainKrakenToken"].get("token"))
    return {"status": "generated", "key": key, "verified": verified}
