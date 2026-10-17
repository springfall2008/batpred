"""Local Kraken auth mixin for OSS users.

Handles API key and email/password authentication against the Kraken GraphQL
API (shared by Octopus, EDF, E.ON). Manages JWT + refresh token lifecycle
locally — no SaaS edge functions needed.

GraphQL mutation: obtainKrakenToken(input: ObtainJSONWebTokenInput!)
  - API key mode: input = { APIKey: "..." }
  - Email mode:   input = { email: "...", password: "..." }
  - Refresh:      input = { refreshToken: "..." }
All return: { token, refreshToken, payload }

Note: On EDF/E.ON, `payload` is a GenericScalar (JSON object, not a GraphQL
type with subfields). Must be requested bare and parsed as JSON.
"""

import aiohttp
import asyncio
import json
from datetime import datetime, timezone, timedelta
from utils import is_edge_block_body, token_mint_backoff_seconds, TOKEN_MINT_BACKOFF_LOG_INTERVAL_SECONDS

# Longest token-error summary written to the log, so one odd response cannot flood it
TOKEN_ERROR_SUMMARY_MAX_CHARS = 300


def redact_braced_spans(text):
    """Replace every {...} span (nested or unterminated) with a placeholder.

    Kraken echoes the submitted mutation input inside braces when it rejects it, and that input
    holds the password or API key, so nothing between braces may reach the log. Quoted strings
    inside a span are skipped whole, escapes included, so a brace or quote inside a credential
    cannot end the span early; anything left unterminated is hidden to the end of the text.
    """
    out = []
    depth = 0
    quote = None
    escaped = False
    for char in text:
        if depth == 0:
            if char == "{":
                out.append("{...}")
                depth = 1
            else:
                out.append(char)
        elif quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
    return "".join(out)


def summarise_token_errors(errors, secrets=()):
    """Return a log-safe 'CODE: message' summary of obtainKrakenToken GraphQL errors, with submitted input redacted.

    Each value in secrets (the submitted credentials) is removed verbatim before the brace redaction,
    so a credential still cannot leak if Kraken echoes it in a shape the brace matching misreads.
    """
    if not isinstance(errors, list):
        return None
    parts = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        extensions = error.get("extensions")
        code = extensions.get("errorCode") if isinstance(extensions, dict) else None
        message = str(error.get("message") or "")
        for secret in secrets:
            if secret:
                message = message.replace(secret, "***")
        message = redact_braced_spans(message).strip()
        parts.append(f"{code}: {message}" if code else message)
    summary = "; ".join(part for part in parts if part)
    return summary[:TOKEN_ERROR_SUMMARY_MAX_CHARS] or None


class KrakenAuthMixin:
    """Local Kraken auth — API key or email/password → JWT, with local refresh."""

    def _init_kraken_auth(self, auth_method, key=None, email=None, password=None):
        """Initialise Kraken auth state. Call from the consuming class __init__."""
        self.auth_method = auth_method
        self._api_key = key
        self._email = email
        self._password = password
        self.access_token = None
        self.refresh_token = None
        self.token_expires_at = None
        self.oauth_failed = False
        self._refresh_in_progress = False
        # Set by _kraken_token_request when the mint is refused by a CDN/WAF rather than by
        # Kraken itself - see check_and_refresh_oauth_token
        self.token_mint_edge_blocked = False
        self.token_mint_blocked_until = None
        self.token_mint_block_count = 0
        self.token_mint_backoff_logged_at = None

    def start_token_mint_backoff(self):
        """Back off from the token mint after a CDN/WAF block.

        Each consecutive block doubles the wait, capped by token_mint_backoff_seconds().
        """
        self.token_mint_block_count += 1
        delay = token_mint_backoff_seconds(self.token_mint_block_count)
        now = datetime.now(timezone.utc)
        self.token_mint_blocked_until = now + timedelta(seconds=delay)
        self.token_mint_backoff_logged_at = now
        self.log(f"Warn: Kraken: Token mint edge/WAF blocked (block {self.token_mint_block_count}) - this is rate limiting, not a bad credential. Backing off for {delay}s")

    def log_token_mint_backoff(self, now):
        """Repeat the backoff reason occasionally so it stays visible in a short log window"""
        if self.token_mint_backoff_logged_at and (now - self.token_mint_backoff_logged_at).total_seconds() < TOKEN_MINT_BACKOFF_LOG_INTERVAL_SECONDS:
            return
        self.token_mint_backoff_logged_at = now
        self.log(f"Warn: Kraken: Token mint still edge/WAF blocked (block {self.token_mint_block_count}) - suppressing mints until {self.token_mint_blocked_until}")

    def clear_token_mint_backoff(self):
        """Clear the mint backoff after a successful token mint"""
        if self.token_mint_block_count:
            self.log(f"Kraken: Token mint recovered after {self.token_mint_block_count} edge/WAF block(s)")
        self.token_mint_block_count = 0
        self.token_mint_blocked_until = None
        self.token_mint_backoff_logged_at = None

    async def check_and_refresh_oauth_token(self):
        """Ensure a valid access token is available, refreshing or re-obtaining as needed.

        Returns True if a valid token is available, False on permanent auth failure.
        """
        if self.oauth_failed:
            return False
        if self._refresh_in_progress:
            return self.access_token is not None

        now = datetime.now(timezone.utc)
        if self.access_token and self.token_expires_at:
            if self.token_expires_at > now + timedelta(minutes=5):
                return True

        # Re-minting while the mint is edge blocked would only be refused again. A token inside
        # the five minute proactive refresh window has NOT actually expired, so keep using it
        # rather than manufacturing an outage the block itself would not have caused.
        if self.token_mint_blocked_until and now < self.token_mint_blocked_until:
            if self.access_token and self.token_expires_at and self.token_expires_at > now:
                return True
            self.log_token_mint_backoff(now)
            return False

        self._refresh_in_progress = True
        try:
            self.token_mint_edge_blocked = False
            if self.refresh_token:
                result = await self._kraken_token_request({"refreshToken": self.refresh_token})
            elif self.auth_method == "api_key" and self._api_key:
                result = await self._kraken_token_request({"APIKey": self._api_key})
            elif self.auth_method == "email" and self._email and self._password:
                result = await self._kraken_token_request({"email": self._email, "password": self._password})
            else:
                self.oauth_failed = True
                return False

            if result:
                self.access_token = result["token"]
                self.refresh_token = result["refreshToken"]
                self.token_expires_at = datetime.fromtimestamp(result["exp"], tz=timezone.utc)
                self.clear_token_mint_backoff()
                return True

            if self.token_mint_edge_blocked:
                # A CDN block says nothing about the credential, so do NOT set oauth_failed:
                # that latch is only cleared on re-initialise, so treating rate limiting as a
                # permanent auth failure disables the provider until the process restarts.
                # Retrying with primary credentials would just hit the same block, so back off
                # instead and keep whatever token we still hold.
                self.start_token_mint_backoff()
                # Re-read the clock: `now` was sampled before the request, which may have taken
                # long enough for a token that was valid then to have expired by now. Returning
                # True here promises the caller a usable token.
                return bool(self.access_token and self.token_expires_at and self.token_expires_at > datetime.now(timezone.utc))

            if self.refresh_token:
                # Refresh token is stale — clear it and retry with primary credentials
                self.refresh_token = None
                self._refresh_in_progress = False
                return await self.check_and_refresh_oauth_token()
            self.oauth_failed = True
            return False
        finally:
            self._refresh_in_progress = False

    async def handle_oauth_401(self):
        """Handle a 401 response by discarding all tokens and re-obtaining from scratch.

        Returns True if a fresh token was obtained, False on failure.
        """
        self.access_token = None
        self.refresh_token = None
        self.token_expires_at = None
        return await self.check_and_refresh_oauth_token()

    async def _kraken_token_request(self, input_vars):
        """Execute the obtainKrakenToken GraphQL mutation with the given input variables.

        Returns a dict with token, refreshToken, exp on success, or None on failure.
        """
        # EDF/E.ON return `payload` as a GenericScalar — request it bare (no subfields).
        # The scalar is a JSON object with { exp, origIat, sub, ... }.
        mutation = """mutation obtainKrakenToken($input: ObtainJSONWebTokenInput!) {
            obtainKrakenToken(input: $input) {
                token
                refreshToken
                payload
            }
        }"""
        url = f"{self.base_url}/v1/graphql/"
        secrets = [str(value) for value in input_vars.values() if value]
        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url,
                    json={"query": mutation, "variables": {"input": input_vars}},
                    headers={"Content-Type": "application/json"},
                ) as response:
                    if response.status != 200:
                        # A 403 carrying a CDN block page is rate limiting, not a rejected
                        # credential. Kraken reports genuine auth problems as HTTP 200 with a
                        # GraphQL error body, so a non-JSON 403 comes from the edge.
                        body = await response.text()
                        if response.status == 403 and is_edge_block_body(body):
                            self.token_mint_edge_blocked = True
                        else:
                            # A GraphQL error body says why (e.g. a schema rejection of the email
                            # login) but echoes the input, so log only the redacted summary.
                            try:
                                summary = summarise_token_errors(json.loads(body).get("errors"), secrets)
                            except (ValueError, AttributeError):
                                summary = None
                            detail = f": {summary}" if summary else ""
                            self.log(f"Warn: Kraken: Token request failed with HTTP {response.status}{detail}")
                        return None
                    data = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

        if data.get("errors"):
            summary = summarise_token_errors(data.get("errors"), secrets)
            self.log(f"Warn: Kraken: Token request rejected{': ' + summary if summary else ''}")
            return None

        token_data = data.get("data", {}).get("obtainKrakenToken")
        if not token_data or not token_data.get("token"):
            return None

        refresh_token = token_data.get("refreshToken")
        # payload is a GenericScalar — may be a JSON string or already-parsed dict
        payload = token_data.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, TypeError):
                payload = {}

        exp = payload.get("exp", 0)
        if not refresh_token or not exp:
            return None

        return {
            "token": token_data["token"],
            "refreshToken": refresh_token,
            "exp": exp,
        }
