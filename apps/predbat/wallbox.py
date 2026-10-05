# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Wallbox EV charger cloud API library.
# The API is the one the Home Assistant wallbox integration and the wallbox PyPI
# library use; it is reimplemented here over aiohttp.
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""Wallbox EV charger integration.

Monitors every charger on a Wallbox account, wires them into Predbat's car charging
inputs, exposes pause/resume, lock, charging current and Eco-Smart controls, and can
pause and resume each charger from Predbat's own car charging plan.
"""

import argparse
import asyncio
import base64
import json
import math
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

from component_base import ComponentBase
from predbat_metrics import record_api_call
from utils import parse_car_plan_windows, in_car_plan_window

WALLBOX_AUTH_URL = "https://user-api.wall-box.com/"
WALLBOX_API_URL = "https://api.wall-box.com/"

API_TIMEOUT = 30
USER_AGENT = "Predbat"

# A token is treated as expired this long before Wallbox says it is, so a call never
# leaves with a token that dies in flight
TOKEN_MARGIN_SECONDS = 120


# remote-action codes, from the Wallbox portal
REMOTE_ACTION_RESUME = 1
REMOTE_ACTION_PAUSE = 2
REMOTE_ACTION_RESUME_SCHEDULE = 9

# How long the command line mode waits before reading a charger back after a control
COMMAND_SETTLE_SECONDS = 8

# components.py's is_alive() fails a component whose last successful update is more than
# 60 minutes old, so the poll interval is capped at half that
MIN_POLL_SECONDS = 60
MAX_POLL_SECONDS = 30 * 60
DEFAULT_POLL_SECONDS = 120
CHARGER_LIST_SECONDS = 30 * 60

# Rate limit back-off, in 60 second run cycles. The cap keeps recovery well inside the
# 60 minute health window.
RATE_LIMIT_MIN_CYCLES = 2
RATE_LIMIT_MAX_CYCLES = 15

MIN_CHARGING_CURRENT = 6
# Used as the upper bound only when the charger does not report one
DEFAULT_MAX_CHARGING_CURRENT = 32

WALLBOX_STORAGE_MODULE = "wallbox"
WALLBOX_CONTROL_STATE = "control_state"

# Status id to (text, car connected). The text is the Home Assistant wallbox integration's
# wording, so states match what existing users already see. A None status id means the
# charger reported nothing, which Wallbox treats as disconnected.
WALLBOX_STATUS = {
    0: ("Disconnected", False),
    14: ("Error", False),
    15: ("Error", False),
    161: ("Ready", False),
    162: ("Ready", False),
    163: ("Disconnected", False),
    164: ("Waiting", True),
    165: ("Locked", False),
    166: ("Updating", False),
    177: ("Scheduled", True),
    178: ("Paused", True),
    179: ("Scheduled", True),
    180: ("Waiting for car demand", True),
    181: ("Waiting for car demand", True),
    182: ("Paused", True),
    183: ("Waiting in queue by Power Sharing", True),
    184: ("Waiting in queue by Power Sharing", True),
    185: ("Waiting in queue by Power Boost", True),
    186: ("Waiting in queue by Power Boost", True),
    187: ("Waiting MID failed", True),
    188: ("Waiting MID safety margin exceeded", True),
    189: ("Waiting in queue by Eco-Smart", True),
    193: ("Charging", True),
    194: ("Charging", True),
    195: ("Charging", True),
    196: ("Discharging", True),
    209: ("Locked", False),
    210: ("Locked, car connected", True),
}
CHARGING_STATUS_IDS = (193, 194, 195)
# No car is plugged in: Disconnected, Ready, and Locked with no car
UNPLUGGED_STATUS_IDS = (0, 161, 162, 163, 165, 209)
PAUSED_STATUS_IDS = (178, 182)

ECO_SMART_OFF = "off"
ECO_SMART_ECO = "eco_mode"
ECO_SMART_FULL_SOLAR = "full_solar"
ECO_SMART_OPTIONS = [ECO_SMART_OFF, ECO_SMART_ECO, ECO_SMART_FULL_SOLAR]


def charger_sort_key(charger_id):
    """Sort key putting numeric charger ids in numeric order, ahead of any that are not numbers."""
    text = str(charger_id)
    return (0, int(text), "") if text.isdigit() else (1, 0, text)


def _to_float(value, default=0.0):
    """Convert a value to float, returning the default when it is missing or not a number."""
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    # JSON may carry Infinity or NaN, which would otherwise be published as a reading
    return result if math.isfinite(result) else default


def _to_int(value, default=0):
    """Convert a value to int, returning the default when it is missing or not a number."""
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


@dataclass
class WallboxCharger:
    """One charger's state, normalised from a Wallbox status payload."""

    charger_id: str
    name: str
    serial: str
    part_number: str
    software_version: str
    status_id: int
    status: str
    connected: bool
    charging: bool
    paused: bool
    locked: bool
    power_w: float
    session_energy_kwh: float
    max_charging_current: int
    max_available_current: int
    eco_smart: Optional[str]


def normalise_charger(charger_id, payload):
    """Turn one status payload into a WallboxCharger, with safe defaults for anything missing."""
    if not isinstance(payload, dict):
        payload = {}
    config = payload.get("config_data")
    if not isinstance(config, dict):
        config = {}
    software = config.get("software")
    if not isinstance(software, dict):
        software = {}

    status_id = payload.get("status_id")
    if status_id is None:
        status_id = 0
    status_id = _to_int(status_id, -1)
    status, connected = WALLBOX_STATUS.get(status_id, ("Unknown", False))

    eco_smart = None
    eco_block = config.get("ecosmart")
    if isinstance(eco_block, dict) and eco_block.get("mode") is not None:
        if not eco_block.get("enabled"):
            eco_smart = ECO_SMART_OFF
        elif _to_int(eco_block.get("mode"), 0) == 1:
            eco_smart = ECO_SMART_FULL_SOLAR
        else:
            eco_smart = ECO_SMART_ECO

    return WallboxCharger(
        charger_id=str(charger_id),
        name=str(payload.get("name") or "Wallbox {}".format(charger_id)),
        serial=str(config.get("serial_number") or ""),
        part_number=str(config.get("part_number") or ""),
        software_version=str(software.get("currentVersion") or ""),
        status_id=status_id,
        status=status,
        connected=connected,
        charging=status_id in CHARGING_STATUS_IDS,
        paused=status_id in PAUSED_STATUS_IDS,
        locked=bool(config.get("locked")),
        power_w=round(_to_float(payload.get("charging_power")) * 1000.0, 1),
        session_energy_kwh=_to_float(payload.get("added_energy")),
        max_charging_current=_to_int(config.get("max_charging_current")),
        max_available_current=_to_int(payload.get("max_available_power")),
        eco_smart=eco_smart,
    )


class WallboxError(Exception):
    """Base class for every Wallbox API failure."""


class WallboxAuthError(WallboxError):
    """Wallbox rejected the credentials or the token."""


class WallboxRateLimitError(WallboxError):
    """Wallbox answered 429, too many requests."""


class WallboxPermissionError(WallboxError):
    """Wallbox refused a write because the account lacks admin rights over the charger."""


class WallboxApiError(WallboxError):
    """Any other failure: a bad status, a timeout, a dropped connection or an unreadable body."""


def basic_auth_header(username, password):
    """Build an HTTP Basic Authorization header value, encoded as UTF-8."""
    token = base64.b64encode("{}:{}".format(username, password).encode("utf-8")).decode("ascii")
    return "Basic " + token


class WallboxTransport:
    """Owns the Wallbox tokens and performs every HTTP call."""

    def __init__(self, log, username, password):
        """Store the credentials and start with no token."""
        self.log = log
        self.username = username
        self.password = password
        self.token = None
        self.refresh_token = None
        # Absolute expiry times, in epoch seconds
        self.token_expiry = 0
        self.refresh_expiry = 0

    def _clear_tokens(self):
        """Forget both tokens so the next call signs in from scratch."""
        self.token = None
        self.refresh_token = None
        self.token_expiry = 0
        self.refresh_expiry = 0

    def _store_tokens(self, payload):
        """Take the tokens and their expiry times from a sign in or refresh response."""
        try:
            attributes = payload["data"]["attributes"]
            self.token = attributes["token"]
            self.refresh_token = attributes.get("refresh_token")
            # Wallbox sends absolute expiry times in epoch milliseconds
            self.token_expiry = float(attributes["ttl"]) / 1000.0
            self.refresh_expiry = float(attributes.get("refresh_token_ttl", 0)) / 1000.0
        except (KeyError, TypeError, ValueError) as exc:
            self._clear_tokens()
            raise WallboxApiError("unexpected sign in response from Wallbox") from exc

    async def _call(self, method, url, headers, body=None, write=False):
        """Perform one HTTP request and return its decoded JSON object.

        Args:
            method: The HTTP method.
            url: The full URL.
            headers: The request headers.
            body: A JSON-serialisable body, or None.
            write: True for a control call. A 403 is then a rights problem rather than
                bad credentials, and an empty response body is accepted.
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(method, url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)) as response:
                    status = response.status
                    if status == 429:
                        record_api_call("wallbox", success=False, reason="rate_limit")
                        raise WallboxRateLimitError("Wallbox rate limit reached calling {}".format(url))
                    if status == 403 and write:
                        record_api_call("wallbox", success=False, reason="auth_error")
                        raise WallboxPermissionError("Wallbox refused {} - the account needs admin rights over the charger".format(url))
                    if status in (401, 403):
                        record_api_call("wallbox", success=False, reason="auth_error")
                        raise WallboxAuthError("Wallbox rejected the credentials for {}".format(url))
                    if status < 200 or status >= 300:
                        record_api_call("wallbox", success=False, reason="server_error" if status >= 500 else "client_error")
                        raise WallboxApiError("HTTP {} from {}".format(status, url))
                    try:
                        payload = await response.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError) as exc:
                        if write:
                            payload = {}
                        else:
                            record_api_call("wallbox", success=False, reason="decode_error")
                            raise WallboxApiError("unreadable response from {}".format(url)) from exc
                    if not isinstance(payload, dict):
                        if write:
                            payload = {}
                        else:
                            record_api_call("wallbox", success=False, reason="decode_error")
                            raise WallboxApiError("unexpected response from {}".format(url))
                    record_api_call("wallbox", success=True)
                    return payload
        except asyncio.TimeoutError as exc:
            record_api_call("wallbox", success=False, reason="connection_error")
            raise WallboxApiError("timed out calling {}".format(url)) from exc
        except aiohttp.ClientError as exc:
            record_api_call("wallbox", success=False, reason="connection_error")
            raise WallboxApiError("request to {} failed: {}".format(url, exc)) from exc

    async def authenticate(self):
        """Make sure there is a usable token: reuse it, refresh it, or sign in."""
        now = time.time()
        if self.token and self.token_expiry - TOKEN_MARGIN_SECONDS > now:
            return
        if self.refresh_token and self.refresh_expiry - TOKEN_MARGIN_SECONDS > now:
            headers = {"Authorization": "Bearer {}".format(self.refresh_token), "Partner": "wallbox", "Accept": "application/json", "User-Agent": USER_AGENT}
            try:
                self._store_tokens(await self._call("GET", WALLBOX_AUTH_URL + "users/refresh-token", headers))
                return
            except WallboxAuthError:
                self.log("Info: wallbox: the refresh token was rejected, signing in again")
        self._clear_tokens()
        headers = {"Authorization": basic_auth_header(self.username, self.password), "Partner": "wallbox", "Accept": "application/json", "User-Agent": USER_AGENT}
        self._store_tokens(await self._call("GET", WALLBOX_AUTH_URL + "users/signin", headers))

    def _headers(self):
        """The headers every data call carries."""
        return {
            "Authorization": "Bearer {}".format(self.token),
            "Accept": "application/json",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": USER_AGENT,
        }

    async def _api(self, method, path, body=None, write=False):
        """Perform one authenticated data call, signing in again once if the token was revoked."""
        await self.authenticate()
        try:
            return await self._call(method, WALLBOX_API_URL + path, self._headers(), body=body, write=write)
        except WallboxAuthError:
            # The token was revoked before its stated expiry. Start again once.
            self._clear_tokens()
            await self.authenticate()
            return await self._call(method, WALLBOX_API_URL + path, self._headers(), body=body, write=write)

    async def list_chargers(self):
        """Return the id of every charger on the account, across all groups."""
        payload = await self._api("GET", "v3/chargers/groups")
        charger_ids = []
        result = payload.get("result")
        groups = result.get("groups") if isinstance(result, dict) else None
        for group in groups or []:
            for charger in (group or {}).get("chargers") or []:
                if isinstance(charger, dict) and charger.get("id") is not None:
                    charger_ids.append(charger["id"])
        return charger_ids

    async def get_status(self, charger_id):
        """Return the raw status payload of one charger."""
        return await self._api("GET", "chargers/status/{}".format(charger_id))

    async def _remote_action(self, charger_id, action):
        """Send one remote-action code to a charger."""
        return await self._api("POST", "v3/chargers/{}/remote-action".format(charger_id), body={"action": action}, write=True)

    async def pause(self, charger_id):
        """Pause the charging session. Only has an effect while the charger is charging."""
        return await self._remote_action(charger_id, REMOTE_ACTION_PAUSE)

    async def resume(self, charger_id):
        """Resume a paused charging session. Has no effect on a Scheduled or Waiting charger."""
        return await self._remote_action(charger_id, REMOTE_ACTION_RESUME)

    async def resume_schedule(self, charger_id):
        """Hand the charger back to its own schedule and Eco-Smart mode after a manual stop."""
        return await self._remote_action(charger_id, REMOTE_ACTION_RESUME_SCHEDULE)

    async def set_locked(self, charger_id, locked):
        """Lock or unlock the charger."""
        return await self._api("PUT", "v2/charger/{}".format(charger_id), body={"locked": 1 if locked else 0}, write=True)

    async def set_max_charging_current(self, charger_id, amps):
        """Set the maximum charging current in amps."""
        return await self._api("PUT", "v2/charger/{}".format(charger_id), body={"maxChargingCurrent": int(amps)}, write=True)

    async def set_eco_smart(self, charger_id, mode):
        """Set the Eco-Smart solar charging mode to one of ECO_SMART_OPTIONS."""
        if mode not in ECO_SMART_OPTIONS:
            raise WallboxApiError("unknown Eco-Smart mode '{}'".format(mode))
        attributes = {"enabled": 0 if mode == ECO_SMART_OFF else 1, "mode": 1 if mode == ECO_SMART_FULL_SOLAR else 0}
        return await self._api("PUT", "v4/chargers/{}/eco-smart".format(charger_id), body={"data": {"attributes": attributes, "type": "eco_smart"}}, write=True)


wallbox_attribute_table = {
    "status": {"friendly_name": "Wallbox Status", "icon": "mdi:information-outline"},
    "power": {"friendly_name": "Wallbox Power", "icon": "mdi:lightning-bolt", "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
    "session_energy": {"friendly_name": "Wallbox Session Energy", "icon": "mdi:lightning-bolt", "unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
    "connected": {"friendly_name": "Wallbox Car Connected", "icon": "mdi:ev-plug-type2"},
    "charging": {"friendly_name": "Wallbox Charging", "icon": "mdi:battery-charging"},
    "charging_switch": {"friendly_name": "Wallbox Charge", "icon": "mdi:ev-station"},
    "locked": {"friendly_name": "Wallbox Locked", "icon": "mdi:lock"},
    "max_charging_current": {"friendly_name": "Wallbox Maximum Charging Current", "icon": "mdi:current-ac", "unit_of_measurement": "A", "min": MIN_CHARGING_CURRENT, "step": 1},
    "eco_smart": {"friendly_name": "Wallbox Eco-Smart", "icon": "mdi:solar-power", "options": ECO_SMART_OPTIONS},
    "control": {"friendly_name": "Wallbox Charge Control", "icon": "mdi:ev-station"},
}


class WallboxAPI(ComponentBase):
    """Wallbox component: monitoring, controls and Predbat-led charging for Wallbox chargers."""

    def initialize(self, username, password, automatic=True, wallbox_control=False, poll_seconds=120):
        """Set up component state and the transport."""
        self.automatic = automatic
        self.wallbox_control = bool(wallbox_control)
        # ComponentBase.start() calls run() every 60 seconds, so the interval is a whole number of those
        self.poll_seconds = min(MAX_POLL_SECONDS, max(MIN_POLL_SECONDS, int(round(_to_float(poll_seconds, DEFAULT_POLL_SECONDS) / 60.0)) * 60))

        self.chargers = {}
        self.charger_ids = []
        self.stale_ids = set()
        self.queued_events = []
        self.skip_cycles = 0
        self.backoff_cycles = 0
        self.permission_warned = False
        # Charger ids in car order: entry N is car N. Append-only, so a car never changes number while Predbat runs
        self.car_order = []
        self._auto_configured_order = None
        self.control_active = False
        self.paused_by_predbat = set()
        self.control_windows = {}
        self.lock_warned = set()
        self.control_state_loaded = False
        self.control_enabled = True
        self.transport = None

        if not username or not password:
            self.log("Error: wallbox: wallbox_username and wallbox_password must both be set")
            return
        self.transport = WallboxTransport(self.log, username, password)

    def entity_prefix(self, charger):
        """Return the entity name prefix for a charger, e.g. predbat_wallbox_12345."""
        return "{}_wallbox_{}".format(self.prefix, charger.charger_id)

    def update_car_order(self, listed_ids):
        """Give every listed charger a car number, keeping the numbers already handed out.

        The order comes from the account's charger list rather than from the chargers that
        happened to answer, so a status failure cannot shift it. New chargers are appended
        in numeric id order; a charger added later becomes the next car and the existing
        cars keep their numbers.
        """
        for charger_id in sorted((str(listed_id) for listed_id in listed_ids), key=charger_sort_key):
            if charger_id not in self.car_order:
                self.car_order.append(charger_id)

    def ordered_chargers(self):
        """The chargers that have been read, in car order."""
        return [self.chargers[charger_id] for charger_id in self.car_order if charger_id in self.chargers]

    def warn_permission(self):
        """Say once that the account cannot control the charger."""
        if not self.permission_warned:
            self.log("Warn: wallbox: Wallbox refused a control - the account needs admin rights over the charger. Monitoring continues")
            self.permission_warned = True

    def automatic_config(self):
        """Wire the charger entities into Predbat's car charging inputs.

        Each argument is a per-car list in charger id order, so charger N is car N.
        car_charging_now takes the power sensors, which count as charging from
        CAR_CHARGING_NOW_POWER_W; a value the user set in apps.yaml is kept. Session
        energy resets with each session, which the shared incrementing-sensor handling
        already copes with. car_charging_soc is not set: a Type 2 connector cannot
        report the car's state of charge.
        """
        if not self.car_order:
            return
        prefixes = ["{}_wallbox_{}".format(self.prefix, charger_id) for charger_id in self.car_order]
        energy_entities = ["sensor.{}_session_energy".format(prefix) for prefix in prefixes]
        planned_entities = ["binary_sensor.{}_connected".format(prefix) for prefix in prefixes]
        power_entities = ["sensor.{}_power".format(prefix) for prefix in prefixes]

        self.log("Info: wallbox: setting car_charging_energy to {}".format(energy_entities))
        self.set_arg_auto("car_charging_energy", energy_entities)
        self.log("Info: wallbox: setting car_charging_planned to {}".format(planned_entities))
        self.set_arg_auto("car_charging_planned", planned_entities)
        self.log("Info: wallbox: setting car_charging_power and car_charging_now to {}".format(power_entities))
        self.set_arg_auto("car_charging_power", power_entities)
        self.set_arg_auto("car_charging_now", power_entities, overwrite=False)
        if _to_int(self.get_arg("num_cars", 0), 0) < len(self.car_order):
            self.log("Info: wallbox: setting num_cars to {}".format(len(self.car_order)))
            self.set_arg("num_cars", len(self.car_order))

    async def run(self, seconds, first):
        """Process queued control events, then poll and publish."""
        if not self.transport:
            return False
        # Every cycle until it succeeds: a read that failed at start-up must not lose the paused list
        await self.load_control_state()
        if first:
            self.enable_control()
            self.skip_cycles = 0
        if self.skip_cycles > 0:
            self.skip_cycles -= 1
            return True
        try:
            refresh = await self.process_events()
            if first or refresh or (seconds % self.poll_seconds) == 0:
                return await self.poll(seconds, first)
            return True
        except WallboxRateLimitError:
            self.backoff_cycles = min(RATE_LIMIT_MAX_CYCLES, max(RATE_LIMIT_MIN_CYCLES, self.backoff_cycles * 2))
            self.skip_cycles = self.backoff_cycles
            self.log("Warn: wallbox: rate limited by the Wallbox API, pausing polling for {} minutes".format(self.backoff_cycles))
            # After start-up one rate limit is not a failure, but a first run that got no
            # data must not mark the component started
            return not first
        except WallboxAuthError as exc:
            self.log("Error: wallbox: sign in failed, check wallbox_username and wallbox_password: {}".format(exc))
            return False
        except WallboxError as exc:
            self.log("Warn: wallbox: poll failed: {}".format(exc))
            return False

    async def process_events(self):
        """Run every queued control. Returns True when at least one ran, so the caller polls afresh."""
        refresh = False
        while self.queued_events:
            handler, *event_args = self.queued_events[0]
            try:
                await handler(*event_args)
            except WallboxRateLimitError:
                # Left on the queue, so it is retried once the back-off has passed
                raise
            except WallboxPermissionError:
                self.warn_permission()
            except Exception as exc:
                # Anything else is dropped too: an event left on the queue would stop every later poll
                self.log("Warn: wallbox: control failed: {}".format(exc))
            self.queued_events.pop(0)
            refresh = True
        return refresh

    async def poll(self, seconds, first):
        """Read every charger, publish, and run automatic configuration and control."""
        if first or not self.charger_ids or (seconds % CHARGER_LIST_SECONDS) == 0:
            self.charger_ids = sorted(await self.transport.list_chargers(), key=charger_sort_key)
            self.update_car_order(self.charger_ids)
        if not self.charger_ids:
            if first:
                self.log("Warn: wallbox: signed in but no chargers were found on the account")
            return True

        chargers = {}
        stale_ids = set()
        for charger_id in self.charger_ids:
            key = str(charger_id)
            try:
                chargers[key] = normalise_charger(charger_id, await self.transport.get_status(charger_id))
            except WallboxApiError as exc:
                self.log("Warn: wallbox: could not read charger {}: {}".format(charger_id, exc))
                # Keep the last record so the charger keeps its place in the car order,
                # marked stale so control does not act on old state
                if key in self.chargers:
                    chargers[key] = self.chargers[key]
                    stale_ids.add(key)
        if len(stale_ids) == len(chargers):
            raise WallboxApiError("no charger could be read")

        self.chargers = chargers
        self.stale_ids = stale_ids
        await self.publish_data()
        if self.automatic and self._auto_configured_order != self.car_order:
            # Once per car order: at start-up, and again only when a charger is added
            self.automatic_config()
            self._auto_configured_order = list(self.car_order)
        try:
            await self.control_tick(self.now_utc_exact)
        except WallboxPermissionError:
            self.warn_permission()
        except WallboxApiError as exc:
            # Monitoring succeeded, so a refused control is a warning, not a failed cycle
            self.log("Warn: wallbox: charge control failed: {}".format(exc))
        self.backoff_cycles = 0
        self.update_success_timestamp()
        return True

    async def publish_data(self):
        """Publish every known charger as Predbat entities."""
        if self.control_active:
            # Published only while control could act on it, so the switch is never a lie
            self.dashboard_item("switch.{}_wallbox_control".format(self.prefix), state="on" if self.control_enabled else "off", attributes=wallbox_attribute_table["control"], app="wallbox")
        for charger in self.ordered_chargers():
            prefix = self.entity_prefix(charger)
            status_attributes = dict(wallbox_attribute_table["status"])
            status_attributes.update({"status_id": charger.status_id, "name": charger.name, "serial_number": charger.serial, "part_number": charger.part_number, "software_version": charger.software_version})
            current_attributes = dict(wallbox_attribute_table["max_charging_current"])
            current_attributes["max"] = charger.max_available_current if charger.max_available_current >= MIN_CHARGING_CURRENT else DEFAULT_MAX_CHARGING_CURRENT

            self.dashboard_item("sensor.{}_status".format(prefix), state=charger.status, attributes=status_attributes, app="wallbox")
            self.dashboard_item("sensor.{}_power".format(prefix), state=charger.power_w, attributes=wallbox_attribute_table["power"], app="wallbox")
            self.dashboard_item("sensor.{}_session_energy".format(prefix), state=charger.session_energy_kwh, attributes=wallbox_attribute_table["session_energy"], app="wallbox")
            self.dashboard_item("binary_sensor.{}_connected".format(prefix), state="on" if charger.connected else "off", attributes=wallbox_attribute_table["connected"], app="wallbox")
            self.dashboard_item("binary_sensor.{}_charging".format(prefix), state="on" if charger.charging else "off", attributes=wallbox_attribute_table["charging"], app="wallbox")
            self.dashboard_item("switch.{}_charging".format(prefix), state="on" if charger.charging else "off", attributes=wallbox_attribute_table["charging_switch"], app="wallbox")
            self.dashboard_item("switch.{}_locked".format(prefix), state="on" if charger.locked else "off", attributes=wallbox_attribute_table["locked"], app="wallbox")
            self.dashboard_item("number.{}_max_charging_current".format(prefix), state=charger.max_charging_current, attributes=current_attributes, app="wallbox")
            if charger.eco_smart is not None:
                self.dashboard_item("select.{}_eco_smart".format(prefix), state=charger.eco_smart, attributes=wallbox_attribute_table["eco_smart"], app="wallbox")

    def charger_for_entity(self, entity_id):
        """Find the charger an entity belongs to, or None.

        The trailing underscore anchors the match to a whole id, so charger 10 cannot
        claim charger 101's entities.
        """
        for charger in self.chargers.values():
            if "{}_".format(self.entity_prefix(charger)) in entity_id:
                return charger
        return None

    def api_id(self, charger):
        """The id in the form the API listed it, for a normalised charger."""
        for charger_id in self.charger_ids:
            if str(charger_id) == charger.charger_id:
                return charger_id
        return charger.charger_id

    async def switch_event(self, entity_id, service):
        """Queue a switch service call for the run loop."""
        self.queued_events.append((self.switch_event_handler, entity_id, service))

    async def number_event(self, entity_id, value):
        """Queue a number change for the run loop."""
        self.queued_events.append((self.number_event_handler, entity_id, value))

    async def select_event(self, entity_id, value):
        """Queue a select change for the run loop."""
        self.queued_events.append((self.select_event_handler, entity_id, value))

    async def switch_event_handler(self, entity_id, service):
        """Pause/resume or lock/unlock in response to a charger switch."""
        if service not in ("turn_on", "turn_off"):
            return
        if entity_id.endswith("_wallbox_control"):
            self.control_enabled = service == "turn_on"
            self.log("Info: wallbox: charge control switched {}".format("on" if self.control_enabled else "off"))
            await self.save_control_state()
            return
        charger = self.charger_for_entity(entity_id)
        if not charger:
            return
        turn_on = service == "turn_on"
        if entity_id.endswith("_charging"):
            self.log("Info: wallbox: {} charging on {}".format("resuming" if turn_on else "pausing", charger.name))
            if turn_on:
                await self.transport.resume(self.api_id(charger))
            else:
                await self.transport.pause(self.api_id(charger))
        elif entity_id.endswith("_locked"):
            self.log("Info: wallbox: {} {}".format("locking" if turn_on else "unlocking", charger.name))
            await self.transport.set_locked(self.api_id(charger), turn_on)

    async def number_event_handler(self, entity_id, value):
        """Set the maximum charging current, clamped to what the charger allows."""
        if not entity_id.endswith("_max_charging_current"):
            return
        charger = self.charger_for_entity(entity_id)
        if not charger:
            return
        try:
            amps = int(float(value))
        except (TypeError, ValueError, OverflowError):
            self.log("Warn: wallbox: ignoring charging current '{}' for {}".format(value, charger.name))
            return
        upper = charger.max_available_current if charger.max_available_current >= MIN_CHARGING_CURRENT else DEFAULT_MAX_CHARGING_CURRENT
        amps = max(MIN_CHARGING_CURRENT, min(upper, amps))
        self.log("Info: wallbox: setting {} maximum charging current to {}A".format(charger.name, amps))
        await self.transport.set_max_charging_current(self.api_id(charger), amps)

    async def select_event_handler(self, entity_id, value):
        """Set the Eco-Smart mode on a charger that supports it."""
        if not entity_id.endswith("_eco_smart") or value not in ECO_SMART_OPTIONS:
            return
        charger = self.charger_for_entity(entity_id)
        if not charger or charger.eco_smart is None:
            return
        self.log("Info: wallbox: setting {} Eco-Smart to {}".format(charger.name, value))
        await self.transport.set_eco_smart(self.api_id(charger), value)

    def enable_control(self):
        """Decide whether Predbat-led charging can run, and say why when it cannot.

        Control needs automatic configuration because a charger is driven from its own
        car's plan, and it is automatic configuration that makes charger N car N.
        """
        self.control_active = False
        if not self.wallbox_control:
            return
        if not self.automatic:
            self.log("Warn: wallbox: wallbox_control needs wallbox_automatic to map each charger to a car, charge control is disabled")
            return
        self.control_active = True
        self.log("Info: wallbox: Predbat-led charge control enabled")

    async def load_control_state(self):
        """Restore the control switch and the list of chargers Predbat paused.

        The paused list is what lets a restart with control turned off still release a
        charger an earlier session left paused. Fails soft with no Storage component.
        """
        if self.control_state_loaded or self.storage is None:
            return
        try:
            saved = await self.storage.load(WALLBOX_STORAGE_MODULE, WALLBOX_CONTROL_STATE)
        except Exception as exc:
            self.log("Warn: wallbox: could not read the saved charge control state: {}".format(exc))
            return
        self.control_state_loaded = True
        if not isinstance(saved, dict):
            return
        if "control_enabled" in saved:
            self.control_enabled = bool(saved["control_enabled"])
        paused = saved.get("paused")
        if isinstance(paused, list):
            self.paused_by_predbat = {str(charger_id) for charger_id in paused}

    async def save_control_state(self):
        """Persist the control switch and the list of chargers Predbat paused."""
        if self.storage is None:
            return
        try:
            await self.storage.save(WALLBOX_STORAGE_MODULE, WALLBOX_CONTROL_STATE, {"control_enabled": self.control_enabled, "paused": sorted(self.paused_by_predbat)})
        except Exception as exc:
            self.log("Warn: wallbox: could not save the charge control state: {}".format(exc))

    def control_read_only_now(self):
        """Is Predbat in read only mode - the live attribute first, then the config argument."""
        read_only = getattr(self.base, "set_read_only", None)
        if read_only is None:
            read_only = self.get_arg("set_read_only", False)
        return bool(read_only)

    def refresh_car_windows(self, now):
        """Read each car's planned charging windows into control_windows.

        A car with no slot sensor yet is simply absent, so its charger is left alone.
        Returns True once at least one car's plan has been read.
        """
        windows = {}
        for car_n in range(len(self.car_order)):
            postfix = "" if car_n == 0 else "_{}".format(car_n)
            planned = self.get_state_wrapper("binary_sensor.{}_car_charging_slot{}".format(self.prefix, postfix), attribute="planned")
            if planned is None:
                continue
            windows[car_n] = parse_car_plan_windows(planned, now, self.local_tz)
        self.control_windows = windows
        return bool(windows)

    async def control_tick(self, now):
        """Run one cycle of charge control, releasing rather than just going quiet.

        Called on every poll whether or not control is available, because a charger a
        previous session paused must be released even when control has since been turned off.
        """
        reason = None
        if not self.control_active:
            reason = "charge control is not enabled"
        elif self.control_read_only_now():
            reason = "Predbat is in read only mode"
        elif not self.control_enabled:
            reason = "the charge control switch is off"
        if reason:
            if self.paused_by_predbat:
                self.log("Info: wallbox: releasing the chargers because {}".format(reason))
                await self.release_chargers()
            return
        await self.control_charge(now)

    async def release_chargers(self):
        """Resume every charger Predbat paused and hand each back to its own schedule.

        A charger that is no longer paused - resumed by hand, or unplugged - is only
        forgotten. The record is saved even if a call fails part way, so what was
        released stays released and the rest is retried on the next poll.
        """
        try:
            for charger_id in sorted(self.paused_by_predbat):
                charger = self.chargers.get(charger_id)
                if charger and charger_id in self.stale_ids:
                    continue
                if charger and charger.paused:
                    self.log("Info: wallbox: releasing {}".format(charger.name))
                    await self.transport.resume(self.api_id(charger))
                    await self.transport.resume_schedule(self.api_id(charger))
                self.paused_by_predbat.discard(charger_id)
        finally:
            await self.save_control_state()

    async def control_charge(self, now):
        """Drive each charger from its car's plan: resume a paused one inside a window, pause a charging one outside.

        Nothing else is sent. A Scheduled charger is following its own schedule and a
        Waiting one is waiting on the car, and resume does nothing for either.
        """
        if not self.refresh_car_windows(now):
            return
        before = set(self.paused_by_predbat)
        try:
            for car_n, charger_id in enumerate(self.car_order):
                charger = self.chargers.get(charger_id)
                if charger is None or car_n not in self.control_windows or charger_id in self.stale_ids:
                    continue
                wanted = in_car_plan_window(self.control_windows[car_n], now)
                if charger.status_id in UNPLUGGED_STATUS_IDS:
                    self.paused_by_predbat.discard(charger.charger_id)
                    continue
                if not charger.connected:
                    # Updating, Error or Unknown: say nothing, and keep the record until the charger reports again
                    continue
                if wanted and charger.locked:
                    if charger.charger_id not in self.lock_warned:
                        self.log("Warn: wallbox: {} is locked, so it will not charge in its planned window. Predbat does not unlock chargers".format(charger.name))
                        self.lock_warned.add(charger.charger_id)
                    continue
                self.lock_warned.discard(charger.charger_id)
                if wanted and charger.paused:
                    self.log("Info: wallbox: resuming {} for car {}".format(charger.name, car_n))
                    await self.transport.resume(self.api_id(charger))
                    self.paused_by_predbat.discard(charger.charger_id)
                elif not wanted and charger.charging:
                    self.log("Info: wallbox: pausing {} for car {}".format(charger.name, car_n))
                    # Recorded first: a pause whose reply is lost may still have been applied,
                    # and release ignores a recorded charger that turns out not to be paused
                    self.paused_by_predbat.add(charger.charger_id)
                    await self.transport.pause(self.api_id(charger))
        finally:
            if self.paused_by_predbat != before:
                await self.save_control_state()


async def run_wallbox_cli(args):  # pragma: no cover
    """Sign in, list the chargers, print each one's status and optionally send one control."""
    transport = WallboxTransport(print, args.username, args.password)
    charger_ids = await transport.list_chargers()
    if not charger_ids:
        print("No chargers found on this account")
        return
    print("Chargers: {}".format(charger_ids))
    for charger_id in charger_ids:
        payload = await transport.get_status(charger_id)
        if args.raw:
            print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        else:
            print(normalise_charger(charger_id, payload))

    target = args.charger if args.charger is not None else charger_ids[0]
    action = None
    if args.pause:
        action = ("pause", transport.pause(target))
    elif args.resume:
        action = ("resume", transport.resume(target))
    elif args.resume_schedule:
        action = ("resume schedule", transport.resume_schedule(target))
    elif args.lock:
        action = ("lock", transport.set_locked(target, True))
    elif args.unlock:
        action = ("unlock", transport.set_locked(target, False))
    elif args.max_current is not None:
        action = ("set max current {}A".format(args.max_current), transport.set_max_charging_current(target, args.max_current))
    elif args.eco_smart:
        action = ("set Eco-Smart {}".format(args.eco_smart), transport.set_eco_smart(target, args.eco_smart))
    if not action:
        return
    print("\nSending {} to {}...".format(action[0], target))
    await action[1]
    print("Waiting {}s for the charger to report the change...".format(COMMAND_SETTLE_SECONDS))
    await asyncio.sleep(COMMAND_SETTLE_SECONDS)
    print(normalise_charger(target, await transport.get_status(target)))


def main():  # pragma: no cover
    """Main function for command line execution."""
    parser = argparse.ArgumentParser(description="Test the Wallbox API")
    parser.add_argument("--username", required=True, help="Wallbox account email address")
    parser.add_argument("--password", required=True, help="Wallbox account password")
    parser.add_argument("--raw", action="store_true", help="Print each charger's full status payload as JSON")
    parser.add_argument("--charger", default=None, help="Charger id to control; defaults to the first one")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--pause", action="store_true", help="Pause the charging session")
    group.add_argument("--resume", action="store_true", help="Resume a paused charging session")
    group.add_argument("--resume-schedule", action="store_true", help="Hand the charger back to its own schedule")
    group.add_argument("--lock", action="store_true", help="Lock the charger")
    group.add_argument("--unlock", action="store_true", help="Unlock the charger")
    group.add_argument("--max-current", type=int, default=None, help="Set the maximum charging current in amps")
    group.add_argument("--eco-smart", choices=ECO_SMART_OPTIONS, default=None, help="Set the Eco-Smart mode")
    args = parser.parse_args()
    asyncio.run(run_wallbox_cli(args))


if __name__ == "__main__":
    main()
