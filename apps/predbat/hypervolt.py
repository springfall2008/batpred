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
import math
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

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

# Websocket reconnect back-off, as the Home Assistant integration uses
BACKOFF_MIN_SECONDS = 3
BACKOFF_MAX_START_SECONDS = 12
BACKOFF_GROWTH = 1.7
BACKOFF_CAP_SECONDS = 300
# Messages on one connection before it counts as good and the back-off resets
BACKOFF_RESET_MESSAGES = 4
# aiohttp pings the socket this often so a dead connection is noticed
WS_HEARTBEAT_SECONDS = 30

# A charger with no message for this long is probed, and at twice this reconnected
STALE_SECONDS = 240

RELEASE_STATES = ("default", "released")
LOCK_STATES = ("unlocked", "pending_lock", "locked")
PILOT_STATES = ("A", "B", "C")
CHARGE_MODES = ["boost", "eco", "super_eco"]
CHARGE_MODE_BATTERY_SAFE = "super_eco_battery_safe"
# The charger feature that adds the Battery Safe mode
FEATURE_BATTERY_SAFE = "home-battery-drain-prevention"

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


def _to_float(value):
    """A finite float, or None when the value is missing or not a number."""
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


@dataclass
class HypervoltCharger:
    """One charger's state, built up from the messages its websockets carry."""

    charger_id: str
    model: str = ""
    name: str = ""
    firmware_version: Optional[str] = None
    api_version: int = 2
    release_state: Optional[str] = None
    charging: bool = False
    pilot_status: Optional[str] = None
    power_w: float = 0.0
    session_id: Optional[int] = None
    session_energy_kwh: float = 0.0
    max_current_a: Optional[int] = None
    charge_mode: Optional[str] = None
    features: list = field(default_factory=list)
    lock_state: Optional[str] = None
    # Epoch seconds of the last message from either socket
    last_message: float = 0.0


def apply_snapshot(charger, items):
    """Fold a sync.snapshot or sync.apply body - a list of single-key objects - into a charger. Bad items are skipped."""
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("release_state") in RELEASE_STATES:
            charger.release_state = item["release_state"]
        if item.get("lock_state") in LOCK_STATES:
            charger.lock_state = item["lock_state"]
        if "max_current" in item:
            milliamps = _to_float(item["max_current"])
            if milliamps is not None:
                charger.max_current_a = int(round(milliamps / 1000.0))
        if item.get("solar_mode") in CHARGE_MODES + [CHARGE_MODE_BATTERY_SAFE]:
            charger.charge_mode = item["solar_mode"]
        if isinstance(item.get("features"), list):
            charger.features = list(item["features"])


def apply_session(charger, result):
    """Fold a session message into a charger: charging, power and the session's energy."""
    if not isinstance(result, dict):
        return
    if "charging" in result:
        charger.charging = bool(result["charging"])
    if not charger.charging:
        charger.power_w = 0.0
    elif "ev_power" in result:
        charger.power_w = _to_float(result["ev_power"]) or 0.0
    elif "true_milli_amps" in result and "voltage" in result:
        milliamps = _to_float(result["true_milli_amps"])
        voltage = _to_float(result["voltage"])
        charger.power_w = milliamps * voltage / 1000.0 if milliamps is not None and voltage is not None else 0.0
    if "session" in result and result["session"] != charger.session_id:
        # A new session: its energy starts again from whatever this message says
        charger.session_id = result["session"]
        charger.session_energy_kwh = 0.0
    watt_hours = _to_float(result.get("watt_hours"))
    if watt_hours is not None:
        # The session's maximum, so the sensor only ever rises within a session
        charger.session_energy_kwh = max(charger.session_energy_kwh, round(watt_hours / 1000.0, 3))


def apply_pilot(charger, result):
    """Fold a get.pilot_status message into a charger: A unplugged, B plugged in, C charging."""
    if isinstance(result, dict) and result.get("pilot_status") in PILOT_STATES:
        charger.pilot_status = result["pilot_status"]


def apply_plugncharge(charger, result):
    """Fold a plugncharge.get reply ({"applied": mode}) or plugncharge.set reply ({"mode": mode, "applied": bool}) into a charger."""
    if not isinstance(result, dict):
        return
    applied = result.get("applied")
    if isinstance(applied, bool):
        if not applied:
            return
        mode = result.get("mode")
    elif isinstance(applied, str):
        mode = applied
    else:
        mode = result.get("mode")
    if mode in CHARGE_MODES + [CHARGE_MODE_BATTERY_SAFE]:
        charger.charge_mode = mode


def connected_state(charger):
    """binary_sensor.<prefix>_connected: the pilot status, or with none yet on while charging and unavailable otherwise."""
    if charger.pilot_status in ("B", "C"):
        return "on"
    if charger.pilot_status == "A":
        return "off"
    return "on" if charger.charging else "unavailable"


def plugged_state(charger):
    """binary_sensor.<prefix>_plugged: the cable alone, from the pilot status and never guessed from charging."""
    if charger.pilot_status in ("B", "C"):
        return "on"
    if charger.pilot_status == "A":
        return "off"
    return "unavailable"


def status_text(charger):
    """The status sensor's state: charging, disconnected, stopped, connected or unknown."""
    if charger.charging:
        return "charging"
    if charger.pilot_status == "A":
        return "disconnected"
    if charger.release_state == "released":
        return "stopped"
    if charger.pilot_status in ("B", "C"):
        return "connected"
    return "unknown"


def charge_mode_options(charger):
    """The charge modes this charger accepts."""
    if FEATURE_BATTERY_SAFE in (charger.features or []):
        return CHARGE_MODES + [CHARGE_MODE_BATTERY_SAFE]
    return list(CHARGE_MODES)


def is_stale(charger, now):
    """Has the charger sent nothing for longer than STALE_SECONDS."""
    return now - charger.last_message > STALE_SECONDS


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


class Backoff:
    """Reconnect delays: a random 3-12 seconds, growing by 1.7 each time to a 300 second cap, until reset."""

    def __init__(self):
        """Start with no delay used."""
        self.delay = None

    def next(self):
        """The next delay in seconds."""
        if self.delay is None:
            self.delay = random.randint(BACKOFF_MIN_SECONDS, BACKOFF_MAX_START_SECONDS)  # nosec B311 - jitter, not security
        else:
            self.delay = min(BACKOFF_CAP_SECONDS, int(self.delay * BACKOFF_GROWTH))
        return self.delay

    def reset(self):
        """Start again from a short delay."""
        self.delay = None


class ChargerLink:
    """One charger's websockets: the open sockets, their reader tasks, and the requests awaiting a reply."""

    def __init__(self, charger_id, api_version):
        """Start with nothing open."""
        self.charger_id = charger_id
        self.api_version = api_version
        self.sync = None
        # The session/in-progress socket, open only while the charger speaks the V2 API
        self.session = None
        self.authenticated = False
        self.backoff = Backoff()
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
        self.chargers = {}
        # Charger id -> its entry from list_chargers(), for the model
        self.charger_info = {}
        self.message_counter = int(time.time() * 1000)
        # Set when a reconnect's sign in is refused: the readers stop until the component signs in again
        self.auth_failed = False
        self.closing = False
        # Replaced by the tests so back-off does not wait in real time
        self.sleep = asyncio.sleep
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
        self.auth_failed = False
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
        chargers = [charger for charger in payload.get("chargers") or [] if isinstance(charger, dict) and charger.get("id") is not None]
        for charger in chargers:
            self.charger_info[str(charger["id"])] = charger
        return chargers

    def next_message_id(self):
        """A request id unique within this transport."""
        self.message_counter += 1
        return str(self.message_counter)

    def charger(self, charger_id):
        """The state record for a charger, made on first use."""
        charger_id = str(charger_id)
        if charger_id not in self.chargers:
            info = self.charger_info.get(charger_id, {})
            self.chargers[charger_id] = HypervoltCharger(charger_id, model=str(info.get("model") or ""), api_version=charger_api_version(charger_id, None))
        return self.chargers[charger_id]

    def on_message(self, charger_id, method, body):
        """Take a state message from a charger - a push, or the reply to a state request - into its record."""
        charger = self.charger(charger_id)
        charger.last_message = time.time()
        if method in ("sync.snapshot", "sync.apply"):
            apply_snapshot(charger, body)
        elif method == "get.session":
            apply_session(charger, body)
        elif method == "get.pilot_status":
            apply_pilot(charger, body)
        elif method in ("plugncharge.get", "plugncharge.set"):
            apply_plugncharge(charger, body)
        elif method == "firmware.version" and body is not None:
            charger.firmware_version = str(body)
            charger.api_version = charger_api_version(charger.charger_id, charger.firmware_version)
        elif method == "get.name" and isinstance(body, str):
            charger.name = body

    async def connect(self, charger_id):
        """Open a charger's sockets in the background. Returns at once; the readers log in and ask for state."""
        charger_id = str(charger_id)
        if charger_id in self.links:
            return
        link = ChargerLink(charger_id, self.charger(charger_id).api_version)
        self.links[charger_id] = link
        self._start_link(link)

    def _start_link(self, link):
        """Start a link's reader tasks: the sync socket, and the session socket for the V2 API."""
        link.tasks = [asyncio.ensure_future(self._reader(link, "sync"))]
        if link.api_version < 3:
            link.tasks.append(asyncio.ensure_future(self._reader(link, "session")))

    async def _stop_link(self, link):
        """Close a link's sockets and end its reader tasks."""
        for socket in (link.sync, link.session):
            if socket is not None:
                try:
                    await socket.close()
                except Exception:  # pylint: disable=broad-except
                    pass
        for task in link.tasks:
            task.cancel()
        for task in link.tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # pylint: disable=broad-except
                pass
        link.tasks = []
        link.sync = None
        link.session = None
        link.authenticated = False
        self._fail_pending(link)

    def _fail_pending(self, link):
        """Fail every command waiting on a link's reply: the connection it was sent on has gone."""
        for future in link.pending.values():
            if not future.done():
                future.set_exception(HypervoltApiError("the connection to charger {} closed".format(link.charger_id)))
        link.pending = {}

    async def reconnect(self, charger_id):
        """Close and reopen one charger's sockets."""
        link = self.links.get(str(charger_id))
        if link is None:
            return
        await self._stop_link(link)
        self._start_link(link)

    async def reconnect_all(self):
        """Close and reopen every charger's sockets, so each logs in again with the current token."""
        for link in list(self.links.values()):
            await self._stop_link(link)
            self._start_link(link)

    async def probe(self, charger_id):
        """Ask a quiet charger for a snapshot without waiting for the reply; its answer stamps last_message."""
        link = self.links.get(str(charger_id))
        if link is None or link.sync is None or not link.authenticated:
            return
        try:
            await self._send(link, "sync.snapshot")
        except Exception as exc:  # pylint: disable=broad-except
            self.log("Warn: hypervolt: could not probe charger {}: {}".format(charger_id, exc))

    async def _reader(self, link, kind):
        """Hold one of a charger's sockets open, reconnecting with back-off, until closed or the sign in is refused.

        kind is "sync" (logs in, carries state and commands) or "session" (V2 API session pushes).
        """
        backoff = link.backoff if kind == "sync" else Backoff()
        url = HYPERVOLT_WS_URL.format(link.charger_id) + ("sync" if kind == "sync" else "session/in-progress")
        while not self.closing:
            try:
                token = await self.authenticate()
            except HypervoltAuthError as exc:
                self.auth_failed = True
                self.log("Error: hypervolt: sign in refused, charger {} is disconnected until it succeeds: {}".format(link.charger_id, exc))
                return
            except HypervoltApiError as exc:
                self.log("Warn: hypervolt: sign in failed for charger {}: {}".format(link.charger_id, exc))
                await self.sleep(backoff.next())
                continue
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url, headers={"Origin": HYPERVOLT_ORIGIN, "User-Agent": USER_AGENT}, heartbeat=WS_HEARTBEAT_SECONDS) as socket:
                        record_api_call("hypervolt", success=True)
                        await self._read_socket(link, kind, socket, token, backoff)
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                record_api_call("hypervolt", success=False, reason="connection_error")
                self.log("Warn: hypervolt: {} socket for charger {} failed: {}".format(kind, link.charger_id, exc))
            if self.closing or (kind == "session" and link.api_version >= 3):
                return
            await self.sleep(backoff.next())

    async def _read_socket(self, link, kind, socket, token, backoff):
        """Read one connection until it closes."""
        if kind == "sync":
            link.sync = socket
        else:
            link.session = socket
        try:
            if kind == "sync":
                await self._send(link, "login", {"token": token, "version": 3})
            count = 0
            while True:
                frame = await socket.receive()
                if frame.type != aiohttp.WSMsgType.TEXT:
                    break
                count += 1
                if count >= BACKOFF_RESET_MESSAGES:
                    backoff.reset()
                if kind == "sync":
                    await self._handle_sync_frame(link, frame.data)
                else:
                    self._handle_session_frame(link, frame.data)
        finally:
            if kind == "sync":
                link.sync = None
                link.authenticated = False
                self._fail_pending(link)
            else:
                link.session = None

    def _handle_session_frame(self, link, text):
        """Route one session socket frame - a bare session object - to on_message as get.session."""
        try:
            message = json.loads(text)
        except ValueError:
            self.log("Warn: hypervolt: unreadable session message from charger {}".format(link.charger_id))
            return
        if self.raw_log:
            self.raw_log("{} <<< session".format(link.charger_id), message)
        if isinstance(message, dict):
            self.on_message(link.charger_id, "get.session", message)

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
                    # Most likely a stale token: renew it before the reconnect
                    self.token_expiry = 0
                    await link.sync.close()
                return
            if future and not future.done():
                future.set_result(result)
            self.on_message(link.charger_id, method, result)
            if method == "firmware.version":
                await self._check_promotion(link)
        elif "method" in message:
            self.on_message(link.charger_id, message["method"], message.get("params"))

    async def _check_promotion(self, link):
        """Move a V2 charger whose firmware speaks the V3 API onto it: ask for its mode and close the session socket."""
        charger = self.chargers.get(link.charger_id)
        if charger is None or charger.api_version <= link.api_version:
            return
        self.log("Info: hypervolt: charger {} firmware {} uses the V3 API".format(link.charger_id, charger.firmware_version))
        link.api_version = charger.api_version
        await self._send(link, "plugncharge.get")
        if link.session is not None:
            await link.session.close()

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
        self.closing = True
        for link in list(self.links.values()):
            await self._stop_link(link)
        self.links = {}
