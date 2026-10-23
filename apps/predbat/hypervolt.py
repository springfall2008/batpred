# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Hypervolt EV charger cloud API library.
# Hypervolt publishes no API: this is the one the Home Assistant custom integration
# gndean/home-assistant-hypervolt-charger uses, reimplemented here over aiohttp.
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""Hypervolt EV charger integration.

Monitors every charger on a Hypervolt account over its websockets, wires them into
Predbat's car charging inputs, exposes charge, current and mode controls, and can stop
and start each charger from Predbat's own car charging plan.
"""

import asyncio
import time

import aiohttp

from predbat_metrics import record_api_call

HYPERVOLT_TOKEN_URL = "https://kc.prod.hypervolt.co.uk/realms/retail-customers/protocol/openid-connect/token"
HYPERVOLT_API_URL = "https://api.hypervolt.co.uk/"
# The client Hypervolt issued for the Home Assistant integration; see the design's risks section
HYPERVOLT_CLIENT_ID = "home-assistant"
HYPERVOLT_SCOPE = "openid profile email offline_access"
USER_AGENT = "Predbat"

API_TIMEOUT = 30
# Refresh the access token once it has less than this left
TOKEN_MARGIN_SECONDS = 600

# Updated V2 firmware reports a short version such as "1" or "1.0" and speaks the V3 API
UPDATED_V2_FIRMWARE_MAX_LEN = 7


class HypervoltError(Exception):
    """Base class for every Hypervolt failure."""


class HypervoltAuthError(HypervoltError):
    """The credentials were rejected."""


class HypervoltApiError(HypervoltError):
    """A call failed for any reason other than the credentials."""


class HypervoltRefusedError(HypervoltApiError):
    """The charger answered a command with an error."""


class HypervoltTimeoutError(HypervoltApiError):
    """A command had no reply in time."""


def charger_major_version(charger_id):
    """The charger generation from its id: 12 hex digits is a V2, 16 a V3, and anything else is taken as a V3."""
    try:
        id_hex = "{:x}".format(int(str(charger_id)))
    except (TypeError, ValueError):
        return 3
    # Rounded up to whole bytes, as the Home Assistant integration does
    num_digits = (len(id_hex) + 1) // 2 * 2
    if num_digits == 12:
        return 2
    return 3


def charger_api_version(charger_id, firmware_version):
    """Which API a charger speaks: 3 for a V3, and for a V2 once its firmware shows it has been updated."""
    if charger_major_version(charger_id) >= 3:
        return 3
    if firmware_version is None:
        return 2
    if len(str(firmware_version).strip()) <= UPDATED_V2_FIRMWARE_MAX_LEN:
        return 3
    return 2


class HypervoltTransport:
    """Owns the Hypervolt tokens, the REST call, and the websockets to each charger."""

    def __init__(self, log, username, password):
        """Store the credentials and start with no token."""
        self.log = log
        self.username = username
        self.password = password
        self.access_token = None
        self.refresh_token = None
        # Absolute expiry time, in epoch seconds
        self.token_expiry = 0

    def _clear_tokens(self):
        """Forget both tokens so the next call signs in from scratch."""
        self.access_token = None
        self.refresh_token = None
        self.token_expiry = 0

    def token_due(self):
        """Is the access token missing or within TOKEN_MARGIN_SECONDS of expiring."""
        return not self.access_token or self.token_expiry - TOKEN_MARGIN_SECONDS <= time.time()

    async def _call(self, method, url, headers=None, data=None):
        """Perform one HTTP request and return (status, decoded JSON or None)."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(method, url, headers=headers or {}, data=data, timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)) as response:
                    status = response.status
                    try:
                        payload = await response.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError):
                        payload = None
                    return status, payload
        except asyncio.TimeoutError as exc:
            record_api_call("hypervolt", success=False, reason="connection_error")
            raise HypervoltApiError("timed out calling {}".format(url)) from exc
        except aiohttp.ClientError as exc:
            record_api_call("hypervolt", success=False, reason="connection_error")
            raise HypervoltApiError("request to {} failed: {}".format(url, exc)) from exc

    async def _token_request(self, form):
        """Post one token request; store and return the access token, or raise HypervoltAuthError when refused."""
        status, payload = await self._call("POST", HYPERVOLT_TOKEN_URL, headers={"User-Agent": USER_AGENT}, data=form)
        if status in (400, 401, 403):
            record_api_call("hypervolt", success=False, reason="auth_error")
            raise HypervoltAuthError("Hypervolt refused the {} (HTTP {})".format("sign in" if form.get("grant_type") == "password" else "token refresh", status))
        if status < 200 or status >= 300:
            record_api_call("hypervolt", success=False, reason="server_error" if status >= 500 else "client_error")
            raise HypervoltApiError("HTTP {} from the Hypervolt sign in".format(status))
        try:
            self.access_token = payload["access_token"]
            self.refresh_token = payload.get("refresh_token", self.refresh_token)
            self.token_expiry = time.time() + float(payload.get("expires_in", 0))
        except (KeyError, TypeError, ValueError) as exc:
            self._clear_tokens()
            record_api_call("hypervolt", success=False, reason="decode_error")
            raise HypervoltApiError("unexpected sign in response from Hypervolt") from exc
        record_api_call("hypervolt", success=True)
        return self.access_token

    async def authenticate(self):
        """Return a usable access token: reuse it, refresh it, or sign in."""
        if not self.token_due():
            return self.access_token
        if self.refresh_token:
            try:
                return await self._token_request({"client_id": HYPERVOLT_CLIENT_ID, "grant_type": "refresh_token", "refresh_token": self.refresh_token})
            except HypervoltAuthError:
                self.log("Info: hypervolt: the refresh token was rejected, signing in again")
        self._clear_tokens()
        return await self._token_request({"client_id": HYPERVOLT_CLIENT_ID, "grant_type": "password", "scope": HYPERVOLT_SCOPE, "username": self.username, "password": self.password})

    async def _get(self, path):
        """One authenticated GET, signing in again once if the token was revoked early."""
        for attempt in range(2):
            token = await self.authenticate()
            status, payload = await self._call("GET", HYPERVOLT_API_URL + path, headers={"Authorization": "Bearer {}".format(token), "Accept": "application/json", "User-Agent": USER_AGENT})
            if status in (401, 403) and attempt == 0:
                self._clear_tokens()
                continue
            if status in (401, 403):
                record_api_call("hypervolt", success=False, reason="auth_error")
                raise HypervoltAuthError("Hypervolt rejected the credentials for {}".format(path))
            if status < 200 or status >= 300:
                record_api_call("hypervolt", success=False, reason="server_error" if status >= 500 else "client_error")
                raise HypervoltApiError("HTTP {} from {}".format(status, path))
            if not isinstance(payload, dict):
                record_api_call("hypervolt", success=False, reason="decode_error")
                raise HypervoltApiError("unexpected response from {}".format(path))
            record_api_call("hypervolt", success=True)
            return payload
        raise HypervoltApiError("unreachable")  # pragma: no cover

    async def list_chargers(self):
        """Return the account's chargers, each a dict with id, model and adopted_at."""
        payload = await self._get("users/me?includes=chargers")
        chargers = payload.get("chargers")
        return [charger for charger in chargers or [] if isinstance(charger, dict) and charger.get("id") is not None]
