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
import copy
import json
import time
from typing import Any, Callable, Optional

import aiohttp

from predbat_metrics import record_api_call

HYPERVOLT_TOKEN_URL = "https://kc.prod.hypervolt.co.uk/realms/retail-customers/protocol/openid-connect/token"
HYPERVOLT_API_URL = "https://api.hypervolt.co.uk/"
HYPERVOLT_WS_URL = "wss://api.hypervolt.co.uk/ws/charger/{}/"
HYPERVOLT_ORIGIN = "https://hypervolt.co.uk"
# The client Hypervolt issued for the Home Assistant integration; see the design's risks section
HYPERVOLT_CLIENT_ID = "home-assistant"
HYPERVOLT_SCOPE = "openid profile email offline_access"
USER_AGENT = "Predbat"

API_TIMEOUT = 30
# Refresh the access token once it has less than this left
TOKEN_MARGIN_SECONDS = 600

# Seconds to wait for the reply to a command
COMMAND_TIMEOUT_SECONDS = 10
# Request ids remembered for matching replies; older ones are dropped
MAX_SENT_IDS = 50

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


class ChargerLink:
    """One charger's websockets: the open sockets, their reader tasks, and the requests awaiting a reply."""

    def __init__(self, charger_id, api_version):
        """Start with nothing open."""
        self.charger_id = charger_id
        self.api_version = api_version
        self.sync = None
        self.authenticated = False
        self.tasks = []
        # Request id -> future, for commands whose caller is waiting on the reply
        self.pending = {}
        # Request id -> method, for every request sent, so a reply can be matched to what it answers
        self.sent = {}


def redact(message):
    """A copy of a message with the access token hidden, for logging."""
    if isinstance(message, dict) and isinstance(message.get("params"), dict) and "token" in message["params"]:
        message = copy.deepcopy(message)
        message["params"]["token"] = "********"
    return message


class HypervoltTransport:
    """Owns the Hypervolt tokens, the REST call, and the websockets to each charger."""

    def __init__(self, log, username, password):
        """Store the credentials and start with no token or links."""
        self.log = log
        self.username = username
        self.password = password
        self.access_token = None
        self.refresh_token = None
        # Absolute expiry time, in epoch seconds
        self.token_expiry = 0
        self.links = {}
        self.message_counter = int(time.time() * 1000)
        # Called with ("<charger id> >>>" or "<charger id> <<<", message) for every message, token hidden
        self.raw_log: Optional[Callable[[str, dict], None]] = None

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

    def next_message_id(self):
        """A request id unique within this transport."""
        self.message_counter += 1
        return str(self.message_counter)

    def on_message(self, charger_id, method, body):
        """Take a state message from a charger: a push, or the reply to a state request."""

    async def connect(self, charger_id):
        """Open a charger's sync socket in the background. Returns at once; the reader logs in and asks for state."""
        charger_id = str(charger_id)
        if charger_id in self.links:
            return
        link = ChargerLink(charger_id, charger_api_version(charger_id, None))
        self.links[charger_id] = link
        link.tasks.append(asyncio.ensure_future(self._sync_reader(link)))

    async def _send(self, link, method, params=None, future=None):
        """Send one request on the sync socket and return its id. A future is registered before the send, so no reply can beat it."""
        message_id = self.next_message_id()
        message = {"jsonrpc": "2.0", "id": message_id, "method": method}
        if params is not None:
            message["params"] = params
        link.sent[message_id] = method
        if future is not None:
            link.pending[message_id] = future
        while len(link.sent) > MAX_SENT_IDS:
            link.sent.pop(next(iter(link.sent)))
        if self.raw_log:
            self.raw_log("{} >>>".format(link.charger_id), redact(message))
        await link.sync.send_str(json.dumps(message))
        return message_id

    async def _request_state(self, link):
        """Ask a freshly logged in charger for everything the component reads."""
        await self._send(link, "sync.snapshot")
        if link.api_version >= 3:
            # The V2 API answers "plugncharge.get not allowed"
            await self._send(link, "plugncharge.get")
        await self._send(link, "firmware.version")
        await self._send(link, "get.name")

    async def _sync_reader(self, link):
        """Hold a charger's sync socket open: log in, then read until it closes."""
        token = await self.authenticate()
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(HYPERVOLT_WS_URL.format(link.charger_id) + "sync", headers={"Origin": HYPERVOLT_ORIGIN, "User-Agent": USER_AGENT}) as socket:
                record_api_call("hypervolt", success=True)
                link.sync = socket
                try:
                    await self._send(link, "login", {"token": token, "version": 3})
                    while True:
                        frame = await socket.receive()
                        if frame.type != aiohttp.WSMsgType.TEXT:
                            break
                        await self._handle_sync_frame(link, frame.data)
                finally:
                    link.sync = None
                    link.authenticated = False

    async def _handle_sync_frame(self, link, text):
        """Route one sync socket frame: a reply to its caller and on_message, a push to on_message."""
        try:
            message = json.loads(text)
        except ValueError:
            self.log("Warn: hypervolt: unreadable message from charger {}".format(link.charger_id))
            return
        if not isinstance(message, dict):
            return
        if self.raw_log:
            self.raw_log("{} <<<".format(link.charger_id), message)
        if "id" in message and ("result" in message or "error" in message):
            message_id = str(message["id"])
            method = link.sent.pop(message_id, None)
            future = link.pending.pop(message_id, None)
            if method is None:
                # A reply nobody is waiting for: late, or from an earlier connection
                return
            if "error" in message:
                error = message.get("error") or {}
                if future and not future.done():
                    future.set_exception(HypervoltRefusedError("charger {} refused {}: {} {}".format(link.charger_id, method, error.get("code", ""), error.get("error", error)).strip()))
                return
            result = message.get("result")
            if method == "login":
                if isinstance(result, dict) and result.get("authenticated"):
                    link.authenticated = True
                    await self._request_state(link)
                else:
                    self.log("Warn: hypervolt: charger {} refused the login".format(link.charger_id))
                    await link.sync.close()
                return
            if future and not future.done():
                future.set_result(result)
            self.on_message(link.charger_id, method, result)
        elif "method" in message:
            self.on_message(link.charger_id, message["method"], message.get("params"))

    async def request(self, charger_id, method, params=None):
        """Send a command to a charger and return its reply's result.

        Raises HypervoltRefusedError when the charger answers with an error,
        HypervoltTimeoutError after COMMAND_TIMEOUT_SECONDS, and HypervoltApiError
        when the charger has no logged in sync socket.
        """
        link = self.links.get(str(charger_id))
        if link is None or link.sync is None or not link.authenticated:
            raise HypervoltApiError("charger {} is not connected".format(charger_id))
        future = asyncio.get_running_loop().create_future()
        try:
            await self._send(link, method, params, future=future)
            return await asyncio.wait_for(future, COMMAND_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise HypervoltTimeoutError("charger {} did not answer {}".format(charger_id, method)) from exc
        finally:
            link.pending = {key: value for key, value in link.pending.items() if value is not future}

    async def close(self):
        """Close every socket and end every reader task."""
        for link in self.links.values():
            if link.sync is not None:
                try:
                    await link.sync.close()
                except Exception:  # pylint: disable=broad-except
                    pass
            for task in link.tasks:
                task.cancel()
            for task in link.tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # pylint: disable=broad-except
                    pass
        self.links = {}
