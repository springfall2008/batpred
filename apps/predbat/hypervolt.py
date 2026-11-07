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

import argparse
import asyncio
import copy
import getpass
import json
import math
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import aiohttp

from car_charger_control import CarChargerControl, parse_control_setting
from component_base import ComponentBase
from coordinator import charger_with_stub_car
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
# Refresh the access token once it has less than this left, or less than half its life for a short-lived token
TOKEN_MARGIN_SECONDS = 600
# Token lifetime assumed when the sign in reply does not say (Keycloak's default)
DEFAULT_TOKEN_LIFE_SECONDS = 300

# Seconds to wait for the reply to a command
COMMAND_TIMEOUT_SECONDS = 10
# Request ids remembered for matching replies; older ones are dropped
MAX_SENT_IDS = 50

MIN_CURRENT_A = 6
MAX_CURRENT_A = 32
# ev_power under this fraction of true_milli_amps x voltage is taken as a stale reading (seen at the start of a charge),
# once current x voltage is at least the minimum - below it a noisy current reading could invent power
EV_POWER_TRUST_FRACTION = 0.5
EV_POWER_MIN_MEASURED_W = 500

# Websocket reconnect back-off, as the Home Assistant integration uses
BACKOFF_MIN_SECONDS = 3
BACKOFF_MAX_START_SECONDS = 12
BACKOFF_GROWTH = 1.7
BACKOFF_CAP_SECONDS = 300
# Messages on one connection before it counts as good and the back-off resets
BACKOFF_RESET_MESSAGES = 4
# aiohttp pings the socket this often so a dead connection is noticed
WS_HEARTBEAT_SECONDS = 30

HYPERVOLT_STORAGE_MODULE = "hypervolt"
HYPERVOLT_CONTROL_STATE = "control_state"

# The charger list is re-read this often, to pick up a charger added to the account
CHARGER_LIST_SECONDS = 30 * 60

# A charger with no message for this long is stale (published, but skipped by control), and at twice this reconnected
STALE_SECONDS = 240
# A quiet charger is probed well before it goes stale, so its answer arrives in time
PROBE_SECONDS = STALE_SECONDS // 2
# Sign in back-off after a refused password, so old credentials cannot lock the account
AUTH_BACKOFF_MIN_SECONDS = 120
AUTH_BACKOFF_MAX_SECONDS = 3600

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


def charger_sort_key(charger_id):
    """Sort key putting numeric charger ids in numeric order, ahead of any that are not numbers."""
    text = str(charger_id)
    return (0, int(text), "") if text.isdigit() else (1, 0, text)


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
    else:
        metered = _to_float(result.get("ev_power"))
        milliamps = _to_float(result.get("true_milli_amps"))
        voltage = _to_float(result.get("voltage"))
        measured = milliamps * voltage / 1000.0 if milliamps is not None and voltage is not None else None
        if metered is not None and (measured is None or measured < EV_POWER_MIN_MEASURED_W or metered >= measured * EV_POWER_TRUST_FRACTION):
            charger.power_w = metered
        elif measured is not None:
            # A V3 charger reports ev_power as 0 or 3 W for the first few messages of a charge while
            # true_milli_amps and voltage already show ~30 A at 231 V, so current x voltage wins then
            charger.power_w = measured
        # A message with neither leaves the last power alone
    if "session" in result and result["session"] != charger.session_id:
        # A new session: its energy starts again from whatever this message says
        charger.session_id = result["session"]
        charger.session_energy_kwh = 0.0
    watt_hours = _to_float(result.get("watt_hours"))
    if watt_hours is not None:
        kwh = round(watt_hours / 1000.0, 3)
        if charger.api_version >= 3 and "session" not in result and kwh < charger.session_energy_kwh:
            # The V3 API sends no session id, and its watt_hours drops to 0 when a session ends
            charger.session_energy_kwh = 0.0
        # The session's maximum, so the sensor only ever rises within a session
        charger.session_energy_kwh = max(charger.session_energy_kwh, kwh)


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


def release_command(charge):
    """The command that lets a charger charge (True) or stops it (False): Hypervolt calls a stop a release."""
    return "sync.apply", {"release": not charge}


def max_current_command(amps):
    """The command that sets the maximum charging current, clamped to 6-32 A and sent in milliamps."""
    return "sync.apply", {"max_current": int(min(MAX_CURRENT_A, max(MIN_CURRENT_A, amps))) * 1000}


def charge_mode_command(api_version, mode):
    """The command that sets the charge mode: plugncharge.set on the V3 API, sync.apply solar_mode on the V2 API."""
    if api_version >= 3:
        return "plugncharge.set", {"mode": mode}
    return "sync.apply", {"solar_mode": mode}


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
        self.token_margin = TOKEN_MARGIN_SECONDS
        # Readers reconnecting together share one sign in rather than racing with the same refresh token
        self.auth_lock = asyncio.Lock()
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
        return not self.access_token or self.token_expiry - self.token_margin <= time.time()

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
            life = _to_float(payload.get("expires_in")) or DEFAULT_TOKEN_LIFE_SECONDS
            self.token_margin = min(TOKEN_MARGIN_SECONDS, life / 2.0)
            self.token_expiry = time.time() + life
        except (KeyError, TypeError, ValueError) as exc:
            self._clear_tokens()
            record_api_call("hypervolt", success=False, reason="decode_error")
            raise HypervoltApiError("unexpected sign in response from Hypervolt") from exc
        record_api_call("hypervolt", success=True)
        self.auth_failed = False
        return self.access_token

    async def authenticate(self, retry_refused=False):
        """Return a usable access token: reuse it, refresh it, or sign in. One sign in at a time.

        Once a password sign in has been refused, only a caller passing retry_refused (the
        component's run loop, which backs off) tries again; the readers get the refusal at once,
        so old credentials cannot be sent once per socket.
        """
        if not self.token_due():
            return self.access_token
        async with self.auth_lock:
            # Another caller may have renewed it while this one waited
            if not self.token_due():
                return self.access_token
            if self.auth_failed and not retry_refused:
                raise HypervoltAuthError("an earlier sign in was refused")
            try:
                return await self._renew_token()
            except HypervoltAuthError:
                self.auth_failed = True
                raise

    async def _renew_token(self):
        """Refresh the token, falling back to a password sign in."""
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

    def link_alive(self, charger_id):
        """Is the charger's sync reader running."""
        link = self.links.get(str(charger_id))
        return bool(link and link.tasks and not link.tasks[0].done())

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
            token = None
            try:
                if kind == "sync":
                    # Only the sync socket logs in; the session socket carries no token
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
            except Exception as exc:  # pylint: disable=broad-except
                # Anything unexpected reconnects with back-off rather than leaving the charger without a reader
                self.log("Warn: hypervolt: {} socket for charger {} stopped unexpectedly: {}".format(kind, link.charger_id, exc))
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
                try:
                    if kind == "sync":
                        await self._handle_sync_frame(link, frame.data)
                    else:
                        self._handle_session_frame(link, frame.data)
                except (asyncio.CancelledError, aiohttp.ClientError, OSError):
                    raise
                except Exception as exc:  # pylint: disable=broad-except
                    # One bad message must not end the reader: log it and read the next
                    self.log("Warn: hypervolt: could not handle a message from charger {}: {}".format(link.charger_id, exc))
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
        if charger_major_version(link.charger_id) < 3:
            # The integration only ever sees this pushed on a change; asking means a restart need not wait for the cable to move.
            # A V3 charger refuses it ("get.pilot_status not allowed") but pushes the pilot status straight after login anyway.
            # Decided by the hardware, not the API: a V2 promoted to the V3 API keeps asking, as it did before.
            await self._send(link, "get.pilot_status")

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
                error = message.get("error")
                if not isinstance(error, dict):
                    # Seen as a bare string, e.g. "plugncharge.get not allowed"
                    error = {"error": error}
                if method == "login":
                    await self._login_refused(link)
                    return
                if future and not future.done():
                    # A V3 charger puts the reason in "message", e.g. {"code": -32601, "data": null, "message": "get.pilot_status not allowed"}
                    reason = error.get("message") or error.get("error") or ""
                    future.set_exception(HypervoltRefusedError("charger {} refused {}: {} {}".format(link.charger_id, method, error.get("code", ""), reason).strip()))
                return
            result = message.get("result")
            if method == "login":
                if isinstance(result, dict) and result.get("authenticated"):
                    link.authenticated = True
                    await self._request_state(link)
                else:
                    await self._login_refused(link)
                return
            if future and not future.done():
                future.set_result(result)
            self.on_message(link.charger_id, method, result)
            if method == "firmware.version":
                await self._check_promotion(link)
        elif "method" in message:
            self.on_message(link.charger_id, message["method"], message.get("params"))

    async def _login_refused(self, link):
        """A charger refused the login: most likely a stale token, so renew it and let the reader reconnect."""
        self.log("Warn: hypervolt: charger {} refused the login".format(link.charger_id))
        self.token_expiry = 0
        if link.sync is not None:
            await link.sync.close()

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
            try:
                await self._send(link, method, params, future=future)
            except (aiohttp.ClientError, OSError, RuntimeError) as exc:
                raise HypervoltApiError("could not send {} to charger {}: {}".format(method, charger_id, exc)) from exc
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


hypervolt_attribute_table = {
    "status": {"friendly_name": "Hypervolt Status", "icon": "mdi:information-outline"},
    "power": {"friendly_name": "Hypervolt Power", "icon": "mdi:lightning-bolt", "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
    "session_energy": {"friendly_name": "Hypervolt Session Energy", "icon": "mdi:lightning-bolt", "unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
    "connected": {"friendly_name": "Hypervolt Car Connected", "icon": "mdi:ev-plug-type2"},
    "plugged": {"friendly_name": "Hypervolt Car Plugged In", "icon": "mdi:ev-plug-type2"},
    "charging": {"friendly_name": "Hypervolt Charging", "icon": "mdi:battery-charging"},
    "charging_switch": {"friendly_name": "Hypervolt Charge", "icon": "mdi:ev-station"},
    "max_charging_current": {"friendly_name": "Hypervolt Maximum Charging Current", "icon": "mdi:current-ac", "unit_of_measurement": "A", "min": MIN_CURRENT_A, "max": MAX_CURRENT_A, "step": 1},
    "charge_mode": {"friendly_name": "Hypervolt Charge Mode", "icon": "mdi:solar-power"},
    "control": {"friendly_name": "Hypervolt Charge Control", "icon": "mdi:ev-station"},
}


class HypervoltAPI(ComponentBase, CarChargerControl):
    """Hypervolt component: monitoring, controls and Predbat-led charging for Hypervolt chargers.

    Predbat-led charging runs on the shared CarChargerControl loop, so a Hypervolt follows Octopus
    Intelligent the way the other chargers do: left to Octopus when Octopus drives it, and driven
    to the dispatches when Octopus drives the car and octopus_intelligent_charger_follows_car is on.
    The component keeps one thing of its own: released_by_predbat, the chargers Predbat stopped and
    still owes a release, which is saved so a restart can never strand a stopped charger.
    """

    def initialize(self, username, password, automatic=True, hypervolt_control=False):
        """Set up component state and the transport."""
        self.automatic = automatic
        self.hypervolt_control = parse_control_setting(hypervolt_control)
        # The control switch is saved by save_control_state(), alongside the released list
        self.charger_control_setup("hypervolt", "Hypervolt", control=self.hypervolt_control, switch_prefix="hypervolt", control_setting="hypervolt_control")
        # Charger ids in car order: entry N is car N. Append-only, so a car never changes number while Predbat runs
        self.car_order = []
        self.listed = False
        self.queued_events = []
        # Charger id -> when Predbat last reconnected it, so a silent charger is given time to answer
        self.reconnected_at = {}
        # Seconds the first run waits for a charger's first message
        self.first_wait_seconds = 20
        self.auth_backoff_seconds = 0
        self.auth_retry_at = 0
        self._auto_configured_order = None
        self.api_2_noted = set()
        # Chargers Predbat stopped and still owes a release
        self.released_by_predbat = set()
        self.control_state_loaded = False
        self.control_state_dirty = False
        self.release_announced = False
        self.refusal_warned = False
        self.transport = None
        if not username or not password:
            self.log("Error: hypervolt: hypervolt_username and hypervolt_password must both be set")
            return
        self.transport = HypervoltTransport(self.log, username, password)

    def entity_prefix(self, charger):
        """Return the entity name prefix for a charger, e.g. predbat_hypervolt_12345."""
        return "{}_hypervolt_{}".format(self.prefix, charger.charger_id)

    def update_car_order(self, listed_ids):
        """Give every listed charger a car number, keeping the numbers already handed out; new chargers are appended in id order."""
        for charger_id in sorted((str(listed_id) for listed_id in listed_ids), key=charger_sort_key):
            if charger_id not in self.car_order:
                self.car_order.append(charger_id)

    def ordered_chargers(self):
        """The chargers with a state record, in car order."""
        return [self.transport.chargers[charger_id] for charger_id in self.car_order if charger_id in self.transport.chargers]

    def build_discovery(self):
        """Describe each charger, and the car it charges, for the discovery catalogue.

        One charger record per charger in car order (ordered_chargers()), keyed "hypervolt:{id}". The
        Hypervolt charger id is the device's own hardware id - its length even encodes the generation,
        see charger_major_version() - so it is reported as the serial. Hypervolt knows nothing about the
        vehicle, so each car is a stub. The entities are the four automatic_config() binds, filtered to
        those Home Assistant has seen; car_charging_now is left out because it only re-binds the power
        sensor. Neither the 32A maximum nor the live current setting is a device-reported rating, so no
        ratings are reported.

        Never reported: the charger's name, which the user chooses.

        Returns None when the account has no charger.
        """
        chargers = []
        cars = []
        for charger in self.ordered_chargers():
            prefix = self.entity_prefix(charger)
            descriptors = {
                "car_charging_planned": {"entity_id": "binary_sensor.{}_connected".format(prefix), "domain": "binary_sensor", "access": "r"},
                "car_charging_plugged": {"entity_id": "binary_sensor.{}_plugged".format(prefix), "domain": "binary_sensor", "access": "r"},
                "car_charging_energy": {"entity_id": "sensor.{}_session_energy".format(prefix), "domain": "sensor", "access": "r"},
                "car_charging_power": {"entity_id": "sensor.{}_power".format(prefix), "domain": "sensor", "access": "r"},
            }
            info = {"vendor": "Hypervolt"}
            if charger.model:
                info["model"] = charger.model
            if charger.firmware_version:
                info["firmware"] = charger.firmware_version
            record, car = charger_with_stub_car(
                "hypervolt:{}".format(charger.charger_id),
                hardware_ids={"serial": str(charger.charger_id)},
                info=info,
                entities=self.discovery_entities(descriptors),
            )
            chargers.append(record)
            cars.append(car)
        if not chargers:
            return None
        return {"automatic": self.automatic, "chargers": chargers, "cars": cars}

    def fresh_chargers(self, now):
        """The chargers that have sent something within STALE_SECONDS."""
        return [charger for charger in self.ordered_chargers() if not is_stale(charger, now)]

    async def run(self, seconds, first):
        """Keep the account signed in and the sockets healthy, then handle controls and publish."""
        if not self.transport:
            return False
        # Every cycle until it succeeds: a read that failed at start-up must not lose the released list
        await self.load_control_state()
        if first:
            self.enable_control()
        try:
            if first or self.transport.auth_failed or self.transport.token_due():
                if not first and time.time() < self.auth_retry_at:
                    self.log("Error: hypervolt: sign in was refused, check hypervolt_username and hypervolt_password; next attempt in {} minutes".format(int((self.auth_retry_at - time.time()) / 60) + 1))
                    return False
                # Readers stop when a sign in is refused, so they are restarted once it works again. A plain
                # refresh leaves the sockets alone: the token is only sent at login, and a reader that is
                # dropped reconnects with whatever token is current
                restart = self.transport.auth_failed and not first
                try:
                    await self.transport.authenticate(retry_refused=True)
                except HypervoltAuthError:
                    if not first:
                        self.auth_backoff_seconds = min(AUTH_BACKOFF_MAX_SECONDS, max(AUTH_BACKOFF_MIN_SECONDS, self.auth_backoff_seconds * 2))
                        self.auth_retry_at = time.time() + self.auth_backoff_seconds
                    raise
                self.auth_backoff_seconds = 0
                self.auth_retry_at = 0
                if restart:
                    await self.transport.reconnect_all()
            await self.refresh_charger_list(seconds, first)
            if first:
                await self.wait_for_first_message()
            await self.check_links(time.time())
            await self.process_events()
            await self.publish_data()
            if self.automatic and self.car_order and self._auto_configured_order != self.car_order:
                # Once per car order: at start-up, and again only when a charger is added
                self.automatic_config()
                self._auto_configured_order = list(self.car_order)
            await self.control_tick(self.now_utc_exact)
        except HypervoltAuthError as exc:
            self.log("Error: hypervolt: sign in failed, check hypervolt_username and hypervolt_password: {}".format(exc))
            return False
        except HypervoltError as exc:
            self.log("Warn: hypervolt: {}".format(exc))
            return False
        # Every cycle, before the empty-account and stale-charger returns below: a quiet charger is still
        # hardware to describe, and a report that failed to file is retried next run() - see
        # ComponentBase.refresh_discovery()
        self.refresh_discovery()
        if not self.car_order:
            # Signed in, but no charger on the account yet: started, as Wallbox does, but with no success stamped
            return True
        if not self.fresh_chargers(time.time()):
            self.log("Warn: hypervolt: no charger has sent anything for {} seconds".format(STALE_SECONDS))
            return False
        self.update_success_timestamp()
        return True

    def automatic_config(self):
        """Wire the charger entities into Predbat's car charging inputs, as per-car lists in car order.

        car_charging_now takes the power sensors, which count as charging from
        CAR_CHARGING_NOW_POWER_W; a value the user set in apps.yaml is kept. car_charging_soc is
        not set: the charger cannot know the car's state of charge.
        """
        prefixes = ["{}_hypervolt_{}".format(self.prefix, charger_id) for charger_id in self.car_order]
        energy_entities = ["sensor.{}_session_energy".format(prefix) for prefix in prefixes]
        planned_entities = ["binary_sensor.{}_connected".format(prefix) for prefix in prefixes]
        plugged_entities = ["binary_sensor.{}_plugged".format(prefix) for prefix in prefixes]
        power_entities = ["sensor.{}_power".format(prefix) for prefix in prefixes]

        self.log("Info: hypervolt: setting car_charging_energy to {}".format(energy_entities))
        self.set_arg_auto("car_charging_energy", energy_entities)
        self.log("Info: hypervolt: setting car_charging_planned to {}".format(planned_entities))
        self.set_arg_auto("car_charging_planned", planned_entities)
        self.log("Info: hypervolt: setting car_charging_plugged to {}".format(plugged_entities))
        self.set_arg_auto("car_charging_plugged", plugged_entities)
        self.log("Info: hypervolt: setting car_charging_power and car_charging_now to {}".format(power_entities))
        self.set_arg_auto("car_charging_power", power_entities)
        self.set_arg_auto("car_charging_now", power_entities, overwrite=False)
        try:
            num_cars = int(self.get_arg("num_cars", 0) or 0)
        except (TypeError, ValueError):
            num_cars = 0
        if num_cars < len(self.car_order):
            self.log("Info: hypervolt: setting num_cars to {}".format(len(self.car_order)))
            self.set_arg("num_cars", len(self.car_order))
        for charger in self.ordered_chargers():
            if charger.api_version < 3 and charger.charger_id not in self.api_2_noted:
                self.api_2_noted.add(charger.charger_id)
                self.log("Info: hypervolt: charger {} uses the V2 API, which only shows a car as connected while it charges; see the Hypervolt section of docs/car-charging.md".format(charger.charger_id))

    async def refresh_charger_list(self, seconds, first):
        """Read the account's chargers on the first run and every CHARGER_LIST_SECONDS, connecting any new one."""
        if not (first or not self.listed or (seconds % CHARGER_LIST_SECONDS) == 0):
            return
        try:
            chargers = await self.transport.list_chargers()
        except HypervoltApiError as exc:
            if first or not self.listed:
                raise
            # The sockets carry on regardless: a failed refresh only delays finding a new charger
            self.log("Warn: hypervolt: could not refresh the charger list: {}".format(exc))
            return
        self.listed = True
        if not chargers and first:
            self.log("Warn: hypervolt: signed in but no chargers were found on the account")
        self.update_car_order(charger["id"] for charger in chargers)
        for charger_id in self.car_order:
            await self.transport.connect(charger_id)

    async def wait_for_first_message(self):
        """Give the sockets up to first_wait_seconds to deliver something, so the first run has data to publish."""
        deadline = time.time() + self.first_wait_seconds
        while self.car_order and not self.fresh_chargers(time.time()) and time.time() < deadline:
            await asyncio.sleep(1)

    async def check_links(self, now):
        """Probe a quiet charger, and reconnect a silent one that has not been reconnected recently."""
        for charger in self.ordered_chargers():
            if not self.transport.auth_failed and not self.transport.link_alive(charger.charger_id):
                self.log("Warn: hypervolt: charger {} has no reader running, reconnecting".format(charger.charger_id))
                self.reconnected_at[charger.charger_id] = now
                await self.transport.reconnect(charger.charger_id)
                continue
            silence = now - charger.last_message
            if silence > 2 * STALE_SECONDS:
                if now - self.reconnected_at.get(charger.charger_id, 0) > 2 * STALE_SECONDS:
                    self.log("Warn: hypervolt: charger {} has sent nothing for {} seconds, reconnecting".format(charger.charger_id, int(silence)))
                    self.reconnected_at[charger.charger_id] = now
                    await self.transport.reconnect(charger.charger_id)
            elif silence > PROBE_SECONDS:
                await self.transport.probe(charger.charger_id)

    async def process_events(self):
        """Run every queued control. Returns True when at least one ran."""
        ran = False
        while self.queued_events:
            handler, *event_args = self.queued_events.pop(0)
            try:
                await handler(*event_args)
            except HypervoltAuthError:
                raise
            except HypervoltRefusedError as exc:
                # Something the user asked for, so every refusal is reported, with Hypervolt's reason
                self.log("Warn: hypervolt: control refused: {}".format(exc))
            except Exception as exc:  # pylint: disable=broad-except
                # Dropped rather than retried: an event left on the queue would block every later one
                self.log("Warn: hypervolt: control failed: {}".format(exc))
            ran = True
        return ran

    def charger_for_entity(self, entity_id):
        """Find the charger an entity belongs to, or None. The trailing underscore anchors the match to a whole id."""
        for charger in self.ordered_chargers():
            if "{}_".format(self.entity_prefix(charger)) in entity_id:
                return charger
        return None

    async def switch_event(self, entity_id, service):
        """Queue a switch service call for the run loop."""
        self.queued_events.append((self.switch_event_handler, entity_id, service))

    async def number_event(self, entity_id, value):
        """Queue a number change for the run loop."""
        self.queued_events.append((self.number_event_handler, entity_id, value))

    async def select_event(self, entity_id, value):
        """Queue a select change for the run loop."""
        self.queued_events.append((self.select_event_handler, entity_id, value))

    async def send_command(self, charger, command):
        """Send a (method, params) command to a charger and wait for its reply."""
        method, params = command
        return await self.transport.request(charger.charger_id, method, params)

    async def switch_event_handler(self, entity_id, service):
        """Let a charger charge or stop it, in response to its charge switch."""
        if await self.charger_control_guest_event(entity_id, service):
            return
        if service not in ("turn_on", "turn_off"):
            return
        if entity_id.endswith("_hypervolt_control"):
            await self.charger_control_set_enabled(service == "turn_on")
            return
        charger = self.charger_for_entity(entity_id)
        if not charger or not entity_id.endswith("_charging"):
            return
        charge = service == "turn_on"
        self.log("Info: hypervolt: {} charging on {}".format("allowing" if charge else "stopping", charger.name or charger.charger_id))
        await self.send_command(charger, release_command(charge))

    async def number_event_handler(self, entity_id, value):
        """Set the maximum charging current, clamped to 6-32 A."""
        if not entity_id.endswith("_max_charging_current"):
            return
        charger = self.charger_for_entity(entity_id)
        if not charger:
            return
        amps = _to_float(value)
        if amps is None:
            self.log("Warn: hypervolt: ignoring charging current '{}' for {}".format(value, charger.name or charger.charger_id))
            return
        command = max_current_command(amps)
        self.log("Info: hypervolt: setting {} maximum charging current to {}A".format(charger.name or charger.charger_id, command[1]["max_current"] // 1000))
        await self.send_command(charger, command)

    async def select_event_handler(self, entity_id, value):
        """Set the charge mode, if the charger offers it."""
        if not entity_id.endswith("_charge_mode"):
            return
        charger = self.charger_for_entity(entity_id)
        if not charger or value not in charge_mode_options(charger):
            return
        self.log("Info: hypervolt: setting {} charge mode to {}".format(charger.name or charger.charger_id, value))
        await self.send_command(charger, charge_mode_command(charger.api_version, value))

    def charger_attributes(self, charger, key):
        """A copy of one entity's attributes with the charger's name in the friendly name."""
        attributes = dict(hypervolt_attribute_table[key])
        name = charger.name.strip()
        label = name if "hypervolt" in name.lower() else ("Hypervolt {}".format(name) if name else "Hypervolt")
        attributes["friendly_name"] = attributes["friendly_name"].replace("Hypervolt", label, 1)
        return attributes

    async def publish_data(self):
        """Publish every known charger as Predbat entities."""
        if self.charger_control_active:
            # Published only while control could act on it, so the switch is never a lie
            self.dashboard_item("switch.{}_hypervolt_control".format(self.prefix), state="on" if self.charger_control_enabled else "off", attributes=hypervolt_attribute_table["control"], app="hypervolt")
            self.charger_control_publish_guest("hypervolt")
        now = time.time()
        for charger in self.ordered_chargers():
            prefix = self.entity_prefix(charger)
            status_attributes = self.charger_attributes(charger, "status")
            status_attributes.update(
                {
                    "model": charger.model,
                    "name": charger.name,
                    "firmware_version": charger.firmware_version,
                    "api_version": charger.api_version,
                    "release_state": charger.release_state,
                    "pilot_status": charger.pilot_status,
                    "lock_state": charger.lock_state,
                    "features": charger.features,
                    "stale": is_stale(charger, now),
                }
            )
            mode_attributes = self.charger_attributes(charger, "charge_mode")
            mode_attributes["options"] = charge_mode_options(charger)

            self.dashboard_item("sensor.{}_status".format(prefix), state=status_text(charger), attributes=status_attributes, app="hypervolt")
            self.dashboard_item("sensor.{}_power".format(prefix), state=charger.power_w, attributes=self.charger_attributes(charger, "power"), app="hypervolt")
            self.dashboard_item("sensor.{}_session_energy".format(prefix), state=charger.session_energy_kwh, attributes=self.charger_attributes(charger, "session_energy"), app="hypervolt")
            connected = connected_state(charger)
            if connected == "unavailable" and charger.charger_id in self.released_by_predbat and charger.release_state == "released":
                # Fail open: a charger Predbat stopped must keep its car's plan, or it is never started again
                connected = "on"
            self.dashboard_item("binary_sensor.{}_connected".format(prefix), state=connected, attributes=self.charger_attributes(charger, "connected"), app="hypervolt")
            self.dashboard_item("binary_sensor.{}_plugged".format(prefix), state=plugged_state(charger), attributes=self.charger_attributes(charger, "plugged"), app="hypervolt")
            self.dashboard_item("binary_sensor.{}_charging".format(prefix), state="on" if charger.charging else "off", attributes=self.charger_attributes(charger, "charging"), app="hypervolt")
            self.dashboard_item("switch.{}_charging".format(prefix), state="on" if charger.release_state == "default" else "off", attributes=self.charger_attributes(charger, "charging_switch"), app="hypervolt")
            self.dashboard_item("number.{}_max_charging_current".format(prefix), state=charger.max_current_a if charger.max_current_a is not None else "unavailable", attributes=self.charger_attributes(charger, "max_charging_current"), app="hypervolt")
            self.dashboard_item("select.{}_charge_mode".format(prefix), state=charger.charge_mode or "unavailable", attributes=mode_attributes, app="hypervolt")

    def enable_control(self):
        """Decide whether Predbat-led charging can run, and say why when it cannot.

        Control needs automatic configuration, because it is that which makes charger N car N.
        """
        self.charger_control_active = False
        if not self.hypervolt_control:
            return
        if not self.automatic:
            self.log("Warn: hypervolt: hypervolt_control needs hypervolt_automatic to map each charger to a car, charge control is disabled")
            return
        self.charger_control_active = True
        self.log("Info: hypervolt: Predbat-led charge control enabled")

    async def load_control_state(self):
        """Restore the control switch and the chargers Predbat still owes a release.

        A read that raises is tried again next cycle; what is read is merged into what this
        session already knows. Fails soft with no Storage component.
        """
        if self.control_state_loaded or self.storage is None:
            return
        try:
            saved = await self.storage.load(HYPERVOLT_STORAGE_MODULE, HYPERVOLT_CONTROL_STATE)
        except Exception as exc:  # pylint: disable=broad-except
            self.log("Warn: hypervolt: could not read the saved charge control state: {}".format(exc))
            return
        self.control_state_loaded = True
        if not isinstance(saved, dict):
            return
        if "control_enabled" in saved:
            self.charger_control_enabled = bool(saved["control_enabled"])
        if isinstance(saved.get("released"), list):
            self.released_by_predbat.update(str(charger_id) for charger_id in saved["released"])

    async def save_control_state(self):
        """Persist the control switch and the released list. A failed write leaves control_state_dirty set, so the next cycle tries again."""
        if self.storage is None:
            self.control_state_dirty = False
            return True
        state = {"control_enabled": self.charger_control_enabled, "released": sorted(self.released_by_predbat)}
        try:
            saved = await self.storage.save(HYPERVOLT_STORAGE_MODULE, HYPERVOLT_CONTROL_STATE, state)
        except Exception as exc:  # pylint: disable=broad-except
            saved = False
            self.log("Warn: hypervolt: could not save the charge control state: {}".format(exc))
        self.control_state_dirty = saved is False
        if not self.control_state_dirty:
            # What is on disk is now this session's state, so there is nothing older left to read
            self.control_state_loaded = True
        return not self.control_state_dirty

    async def charger_control_save_enabled(self):
        """Save the control switch together with the released list, which share one stored document."""
        await self.save_control_state()

    def charger_control_read_only_now(self):
        """Is Predbat in read only mode.

        Read with get_arg: the base's set_read_only attribute is True from reset() until the first
        update_pred(), which comes after this component's first run, so reading it would release
        every charger Predbat had stopped on each restart. Axle's VPP read only sets only the
        attribute, so its own flag is read as well.
        """
        return bool(self.get_arg("set_read_only", False)) or bool(getattr(self.base, "set_read_only_axle", False))

    def charger_control_chargers(self):
        """The chargers in car order, keyed by charger id, so charger N follows car N."""
        return [(charger.charger_id, charger) for charger in self.ordered_chargers()]

    def charger_control_car_count(self):
        """One car per charger: automatic configuration raises num_cars to match."""
        return len(self.car_order)

    def charger_control_connected(self, charger):
        """Can Predbat act on this charger now: fresh, not unplugged, and with a car known to be there.

        A charger Predbat stopped counts as there even with no pilot status, as a V2 API charger
        never reports a car that is not drawing power - otherwise it would never be let charge again.
        One whose state is not known yet is left alone rather than stopped.
        """
        if is_stale(charger, time.time()) or charger.pilot_status == "A":
            return False
        return connected_state(charger) == "on" or charger.release_state == "released"

    def charger_control_car_plugged(self, charger):
        """Is a car plugged in, for ending guest charging - an unknown pilot status reads as plugged in."""
        return charger.pilot_status != "A"

    def charger_control_drifted(self, charger, charge):
        """Has the charger been stopped or let charge by something else, e.g. the Hypervolt app."""
        if charger.release_state is None:
            return False
        return charger.release_state != ("default" if charge else "released")

    async def charger_control_send(self, charger, charge, car_n):
        """Let the charger charge, or stop it. A charger already in that state is sent nothing."""
        name = charger.name or charger.charger_id
        if charge:
            if charger.release_state != "default":
                self.log("Info: hypervolt: allowing {} to charge for car {}".format(name, car_n))
                await self.send_command(charger, release_command(True))
            self.forget(charger)
            return
        if charger.release_state == "released":
            # Already stopped - by Predbat before a restart, or by hand - so nothing to send
            return
        self.log("Info: hypervolt: stopping {} for car {}".format(name, car_n))
        # Recorded and stored before it is sent: a stop whose reply is lost may still have been applied
        if charger.charger_id not in self.released_by_predbat:
            self.released_by_predbat.add(charger.charger_id)
            await self.save_control_state()
        await self.send_command(charger, release_command(False))

    async def charger_control_release_one(self, charger, charge):
        """Hand a charger back: let it charge again if Predbat stopped it, and forget it once nothing is owed.

        One not heard from recently cannot be trusted or sent anything, so it stays owed whatever it
        last read; control_tick() retries it each cycle. Otherwise a charger back in "default" is owed
        nothing, and one whose state is unknown since a restart is sent the release anyway - letting
        a charger charge is harmless if it already can - rather than waiting on a report that may
        never come.
        """
        if charger.charger_id in self.released_by_predbat:
            if is_stale(charger, time.time()):
                # Even one that last read "default": a stop whose reply was lost may still have landed
                return
        if charger.charger_id in self.released_by_predbat and charger.release_state != "default":
            # Even when unplugged: whether a stop survives the cable coming out is not known
            self.log("Info: hypervolt: releasing {}".format(charger.name or charger.charger_id))
            await self.send_command(charger, release_command(True))
        self.forget(charger)

    async def charger_control_hand_to_octopus(self, charger, charge):
        """Hand a charger to Octopus, letting it charge again if Predbat had stopped it.

        The shared default leaves a charger Predbat stopped for Octopus to start. Whether Octopus
        can undo a Hypervolt stop is not known, and a stop it cannot undo would hold the car off
        for good, so a Hypervolt is released instead: the cost is that it may draw until Octopus
        next sets it, rather than a car left unable to charge.
        """
        await self.charger_control_release_one(charger, charge)

    def forget(self, charger):
        """Drop a charger from the released list."""
        if charger.charger_id in self.released_by_predbat:
            self.released_by_predbat.discard(charger.charger_id)
            self.control_state_dirty = True

    def sync_held(self):
        """Line the shared loop's record up with released_by_predbat, the record that survives a restart.

        A charger Predbat stopped is held as stopped, so the shared loop knows to release it; one
        held as stopped that is no longer owed a release is dropped.
        """
        for charger_id in self.released_by_predbat:
            if charger_id in self.transport.chargers:
                self.charger_control_state.setdefault(charger_id, False)
        for charger_id, charge in list(self.charger_control_state.items()):
            if charge is False and charger_id not in self.released_by_predbat:
                del self.charger_control_state[charger_id]

    async def settle_released(self):
        """Settle every charger owed a release whose situation has changed, whether or not control runs.

        Back to "default": nothing is owed. Unplugged while stopped, or while its state is not known
        since a restart: released before it is forgotten, as a stop that outlived the unplug would
        hold the next car too. Done here rather than in the shared loop, which leaves an unplugged
        charger alone.
        """
        now = time.time()
        for charger_id in sorted(self.released_by_predbat):
            charger = self.transport.chargers.get(charger_id)
            if charger is None or is_stale(charger, now):
                continue
            if charger.release_state == "default":
                self.forget(charger)
            elif charger.pilot_status == "A":
                # Stopped, or not yet known to be otherwise since a restart
                self.log("Info: hypervolt: the car was unplugged from {}".format(charger.name or charger.charger_id))
                await self.guarded(self.charger_control_release_one(charger, False), charger)

    async def guarded(self, action, charger=None):
        """Run a control step so that a refused or failed command is reported rather than failing the run.

        A refusal is warned about once, as it repeats every cycle; a sign in failure is not an
        HypervoltApiError, so it still reaches run().
        """
        try:
            await action
        except HypervoltRefusedError as exc:
            if not self.refusal_warned:
                self.log("Warn: hypervolt: charge control refused: {}. Monitoring continues".format(exc))
                self.refusal_warned = True
        except HypervoltApiError as exc:
            self.log("Warn: hypervolt: charge control failed{}: {}".format(" for {}".format(charger.name or charger.charger_id) if charger else "", exc))

    async def control_tick(self, now):
        """Run one cycle of charge control on the shared loop.

        With control off, chargers Predbat stopped in an earlier session are still released, so
        turning hypervolt_control off can never strand a car.
        """
        try:
            await self.settle_released()
            self.sync_held()
            if not self.charger_control_active:
                if self.charger_control_state:
                    if not self.release_announced:
                        self.log("Info: hypervolt: releasing the chargers because charge control is not enabled")
                        self.release_announced = True
                    await self.guarded(self.charger_control_release())
                return
            await self.guarded(self.charger_control_tick(now))
            if self.charger_control_released is not None and self.released_by_predbat:
                # The shared loop releases once per reason; a charger that could not be sent the release
                # then (not heard from) is still owed, so it is tried again each cycle until it goes through
                self.sync_held()
                await self.guarded(self.charger_control_release())
        finally:
            if self.control_state_dirty:
                await self.save_control_state()

    async def final(self):
        """Close every socket so a restart leaves no reader task behind."""
        if self.transport:
            await self.transport.close()


def print_charger_table(chargers):  # pragma: no cover
    """Print a summary of each charger's state."""
    print("{:<22} {:<16} {:<13} {:>4} {:>8} {:>9} {:>12} {:>6} {:<10} {:>8}".format("CHARGER", "NAME", "STATUS", "API", "PILOT", "POWER W", "SESSION kWh", "MAX A", "MODE", "RELEASE"))
    for charger in chargers:
        print(
            "{:<22} {:<16} {:<13} {:>4} {:>8} {:>9.0f} {:>12.3f} {:>6} {:<10} {:>8}".format(
                charger.charger_id,
                charger.name[:16],
                status_text(charger),
                charger.api_version,
                charger.pilot_status or "-",
                charger.power_w,
                charger.session_energy_kwh,
                charger.max_current_a or "-",
                charger.charge_mode or "-",
                charger.release_state or "-",
            )
        )


async def run_hypervolt_cli(args):  # pragma: no cover
    """Sign in, connect to every charger, print what arrives, and optionally send one command."""
    transport = HypervoltTransport(print, args.username, args.password)
    if args.raw:
        transport.raw_log = lambda label, message: print("{} {}".format(label, json.dumps(message, sort_keys=True, default=str)))
    chargers = await transport.list_chargers()
    if not chargers:
        print("No chargers found on this account")
        return
    print("Chargers: {}".format(", ".join("{} ({})".format(charger["id"], charger.get("model", "?")) for charger in chargers)))
    for charger in chargers:
        await transport.connect(charger["id"])
    try:
        print("Listening for {}s...".format(args.wait))
        await asyncio.sleep(args.wait)
        print("")
        print_charger_table(transport.chargers.values())

        target = str(args.charger) if args.charger is not None else str(chargers[0]["id"])
        if target not in transport.chargers:
            print("\nCharger {} is not on this account".format(target))
            return
        command = None
        if args.start:
            command = release_command(True)
        elif args.stop:
            command = release_command(False)
        elif args.max_current is not None:
            command = max_current_command(args.max_current)
        elif args.mode:
            command = charge_mode_command(transport.chargers[target].api_version, args.mode)
        if not command:
            return
        print("\nSending {} {} to {}...".format(command[0], json.dumps(command[1]), target))
        print("Reply: {}".format(json.dumps(await transport.request(target, command[0], command[1]), default=str)))
        print("Listening for {}s for the change...".format(args.wait))
        await asyncio.sleep(args.wait)
        print_charger_table([transport.chargers[target]])
    finally:
        await transport.close()


def main():  # pragma: no cover
    """Main function for command line execution."""
    parser = argparse.ArgumentParser(description="Test the Hypervolt API. Use --raw to capture messages for the test fixtures")
    parser.add_argument("--username", required=True, help="Hypervolt account email address")
    parser.add_argument("--password", default=None, help="Hypervolt account password. Leave it out to be asked for it, which keeps it out of your shell history and the process list")
    parser.add_argument("--raw", action="store_true", help="Print every message sent and received, as JSON, with the token hidden")
    parser.add_argument("--wait", type=int, default=20, help="Seconds to listen for messages before printing, and again after a command (default 20)")
    parser.add_argument("--charger", default=None, help="Charger id to control; defaults to the first one")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--start", action="store_true", help="Let the charger charge (release false)")
    group.add_argument("--stop", action="store_true", help="Stop the charger (release true)")
    group.add_argument("--max-current", type=int, default=None, help="Set the maximum charging current in amps (6-32)")
    group.add_argument("--mode", choices=CHARGE_MODES + [CHARGE_MODE_BATTERY_SAFE], default=None, help="Set the charge mode")
    args = parser.parse_args()
    if args.password is None:
        args.password = getpass.getpass("Hypervolt password: ")
    try:
        asyncio.run(run_hypervolt_cli(args))
    except HypervoltError as exc:
        # A refused command or a failed sign in is an answer, not a crash
        print("\nHypervolt call failed: {}".format(exc))


if __name__ == "__main__":
    main()
