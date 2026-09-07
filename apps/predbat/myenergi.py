# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# myenergi API library.
# Supports both the direct "director" API (digest auth, /cgi-* endpoints) that
# pymyenergi and the ha-myenergi integration use, and the official 3rd party API
# documented at https://api-docs.s18.myenergi.net/
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init


"""myenergi Zappi and Eddi integration.

Provides monitoring of myenergi Zappi EV chargers and Eddi hot water diverters,
automatic wiring of their energy sensors into Predbat's car charging and iboost
inputs, and send/cancel boost controls. Two interchangeable transports cover the
two myenergi APIs: a direct digest-authenticated transport that any myenergi owner
can configure today, and a bearer-token transport for the official 3rd party API.
"""

import argparse
import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import aiohttp

from component_base import ComponentBase
from mock_base import MockBase
from oauth_mixin import OAuthMixin
from predbat_metrics import record_api_call
from utils import parse_car_plan_windows, in_car_plan_window

MYENERGI_DIRECTOR_URL = "https://director.myenergi.net"
MYENERGI_CLOUD_URL = "https://api.s18.myenergi.net"

API_TIMEOUT = 30
USER_AGENT = "Wget/1.14 (linux-gnu)"

DEVICE_KIND_ZAPPI = "zappi"
DEVICE_KIND_EDDI = "eddi"
SUPPORTED_KINDS = (DEVICE_KIND_ZAPPI, DEVICE_KIND_EDDI)

# Device id prefixes. The direct API uses a single letter, the cloud API two letters.
DIRECT_PREFIX = {DEVICE_KIND_ZAPPI: "Z", DEVICE_KIND_EDDI: "E"}
CLOUD_PREFIX = {DEVICE_KIND_ZAPPI: "ZA", DEVICE_KIND_EDDI: "ED"}

# The "Charging" label appears in three unrelated tables below (a Zappi's numeric
# status, its plug status, and the cloud status translation) plus the component's
# charging binary sensor, all of which must agree on the exact string.
STATUS_CHARGING = "Charging"

# Index tables used by the direct API's numeric status fields.
ZAPPI_CHARGE_MODES = ["None", "Fast", "Eco", "Eco+", "Stopped"]
ZAPPI_STATES = ["Unknown", "Paused", "Unknown", STATUS_CHARGING, "Boosting", "Completed"]
EDDI_STATES = ["Unknown", "Paused", "Unknown", "Diverting", "Boosting", "Max temp reached", "Stopped"]

ZAPPI_PLUG_STATES = {
    "A": "EV Disconnected",
    "B1": "EV Connected",
    "B2": "Waiting for EV",
    "C1": "EV ready to charge",
    "C2": STATUS_CHARGING,
    "D1": "EV ready to charge",
    "D2": STATUS_CHARGING,
    "F": "Fault",
}

EDDI_BOOST_TARGETS = {"heater1": 1, "heater2": 2, "relay1": 11, "relay2": 12}
EDDI_DEFAULT_BOOST_TARGET = "heater1"

# The cloud API reports modes and statuses as strings. These maps translate them into
# the same vocabulary the direct API's index tables produce, so both transports emit
# identical MyEnergiDevice values for equivalent device states.
CLOUD_MODE_TO_NAME = {"fast": "Fast", "eco": "Eco", "eco+": "Eco+", "stop": "Stopped"}

# The supply modes that can be set, and their cloud-API spelling. Derived from the map
# above so the two vocabularies cannot drift apart. ZAPPI_CHARGE_MODES[0] ("None") is a
# state the device reports, not a mode either API accepts.
ZAPPI_MODE_TO_CLOUD = {name: cloud for cloud, name in CLOUD_MODE_TO_NAME.items()}

CLOUD_STATUS_TO_NAME = {
    "ev_not_connected": "Paused",
    "waiting_for_surplus": "Paused",
    "waiting_for_ev": "Paused",
    "charge_delayed": "Paused",
    "smart_charge_delay": "Paused",
    "charge_complete": "Completed",
    "charging": STATUS_CHARGING,
    "boosting": "Boosting",
    "stopped": "Stopped",
    "diverting": "Diverting",
    "hot": "Max temp reached",
    "starting": "Paused",
    "dsr": "Paused",
}

# Zappi boost energy limits, from the 3rd party API schema. The direct API accepts the
# same range in practice, so both transports validate against these.
BOOST_ENERGY_MIN = 1
BOOST_ENERGY_MAX = 99
BOOST_MINUTES_MIN = 0
BOOST_MINUTES_MAX = 240

# Boosting a Zappi is only accepted while it is in one of the green-energy modes.
ZAPPI_BOOSTABLE_MODES = ("Eco", "Eco+")

# The two modes Predbat-led charge control drives a Zappi between, and the mode a
# released Zappi falls back to when nothing was saved to restore.
ZAPPI_MODE_CHARGING = "Fast"
ZAPPI_MODE_STOPPED = "Stopped"
ZAPPI_MODE_RELEASE = "Eco+"

# Where the control switch state is persisted, so turning control off survives a restart.
MYENERGI_STORAGE_MODULE = "myenergi"
MYENERGI_CONTROL_STATE = "control_state"

# aiohttp only grew DigestAuthMiddleware and ClientSession(middlewares=...) in 3.12, and
# the direct transport cannot authenticate without them. requirements.txt pins the floor,
# but a hand-managed install can still be older, so say what to do rather than failing
# with a bare AttributeError from deep inside the first request.
AIOHTTP_DIGEST_REQUIRED = "the direct myenergi transport needs aiohttp 3.12 or newer for digest authentication (installed: {}) - upgrade aiohttp, or set myenergi_auth_method to oauth"


class MyEnergiError(Exception):
    """Base class for every myenergi transport failure."""


class MyEnergiAuthError(MyEnergiError):
    """Raised when myenergi rejects the supplied credentials."""


class MyEnergiApiError(MyEnergiError):
    """Raised when a myenergi request fails for a non-authentication reason."""


@dataclass
class MyEnergiDevice:
    """One normalised myenergi device, identical in shape across both transports."""

    device_id: str
    kind: str
    serial: str
    name: str
    online: bool
    status: str
    mode: str
    plug_status: str
    power_w: float
    grid_power_w: float
    generation_w: float
    voltage: float
    session_energy_kwh: float
    boost_active: bool
    boost_remaining_mins: int
    temp_1: Optional[float]
    temp_2: Optional[float]


def _to_float(value, default=0.0):
    """Coerce a raw API value to float, returning default for None or junk."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _index_lookup(table, index, default="Unknown"):
    """Look a numeric status code up in one of the direct API's index tables."""
    try:
        position = int(index)
    except (TypeError, ValueError):
        return default
    if 0 <= position < len(table):
        return table[position]
    return default


def _boost_units(amount):
    """Round a boost amount to the whole kWh or whole minutes both APIs expect.

    int() alone truncates toward zero, so a 9.8 kWh selection would be sent as 9.
    """
    return max(0, int(round(_to_float(amount))))


def validate_zappi_mode(device, mode):
    """Check a supply mode change is one this device can accept.

    Raises rather than defaulting, because the Eddi mode command is a different endpoint
    with its own vocabulary - falling through would silently address the wrong device.
    """
    if device.kind != DEVICE_KIND_ZAPPI:
        raise MyEnergiApiError("cannot set a supply mode on device kind '{}'".format(device.kind))
    if mode not in ZAPPI_MODE_TO_CLOUD:
        raise MyEnergiApiError("unknown Zappi supply mode '{}'".format(mode))


def digest_auth_available():
    """Return True when the installed aiohttp provides the digest auth middleware."""
    return hasattr(aiohttp, "DigestAuthMiddleware")


def _optional_temp(value):
    """Return an Eddi probe temperature, or None when no probe is connected.

    myenergi reports 127 for an unconnected probe and a negative value when the
    reading is unknown; publishing either as a temperature would be misleading.
    """
    if value is None:
        return None
    try:
        temperature = float(value)
    except (TypeError, ValueError):
        return None
    if temperature >= 127 or temperature < 0:
        return None
    return temperature


def normalise_direct_device(raw, kind):
    """Convert one direct API device record into a MyEnergiDevice.

    Args:
        raw: A single device dict from a /cgi-jstatus-* response.
        kind: Either DEVICE_KIND_ZAPPI or DEVICE_KIND_EDDI.
    """
    serial = str(raw.get("sno", "") or "")
    if kind == DEVICE_KIND_ZAPPI:
        status = _index_lookup(ZAPPI_STATES, raw.get("sta"))
        mode = _index_lookup(ZAPPI_CHARGE_MODES, raw.get("zmo"))
        plug_status = ZAPPI_PLUG_STATES.get(str(raw.get("pst", "") or ""), "")
        boost_active = status == "Boosting"
        boost_remaining_mins = 0
        temp_1 = None
        temp_2 = None
    else:
        status = _index_lookup(EDDI_STATES, raw.get("sta"))
        mode = "Stopped" if status == "Stopped" else "Normal"
        plug_status = ""
        boost_active = int(_to_float(raw.get("bsm"))) == 1
        boost_remaining_mins = int(round(_to_float(raw.get("rbt")) / 60.0))
        temp_1 = _optional_temp(raw.get("tp1"))
        temp_2 = _optional_temp(raw.get("tp2"))

    return MyEnergiDevice(
        device_id=DIRECT_PREFIX[kind] + serial,
        kind=kind,
        serial=serial,
        name="{}-{}".format(kind, serial),
        online=True,
        status=status,
        mode=mode,
        plug_status=plug_status,
        power_w=_to_float(raw.get("div")),
        grid_power_w=_to_float(raw.get("grd")),
        generation_w=_to_float(raw.get("gen")),
        voltage=_to_float(raw.get("vol")) / 10.0,
        session_energy_kwh=_to_float(raw.get("che")),
        boost_active=boost_active,
        boost_remaining_mins=boost_remaining_mins,
        temp_1=temp_1,
        temp_2=temp_2,
    )


def normalise_cloud_device(raw, meta):
    """Convert one cloud API status response into a MyEnergiDevice.

    Args:
        raw: The body of GET /devices/{id}/status.
        meta: The matching device entry from GET /devices, used for the id, alias
              and online flag that the status response does not carry.
    """
    kind = DEVICE_KIND_ZAPPI if str(raw.get("deviceClass", "")).upper() == "ZAPPI" else DEVICE_KIND_EDDI
    serial = str(meta.get("serialNumber", "") or "")
    device_id = str(meta.get("deviceId", "") or "")
    if not serial and device_id:
        serial = device_id[2:]

    status = CLOUD_STATUS_TO_NAME.get(str(raw.get("deviceStatus", "") or "").lower(), "Unknown")
    if kind == DEVICE_KIND_ZAPPI:
        mode = CLOUD_MODE_TO_NAME.get(str(raw.get("supplyMode", "") or "").lower(), "Unknown")
        plug_status = ZAPPI_PLUG_STATES.get(str(raw.get("pilotState", "") or ""), "")
        boost_active = bool(raw.get("boostCharge", False))
    else:
        mode = "Stopped" if status == "Stopped" else "Normal"
        plug_status = ""
        boost_active = bool(raw.get("boostActive", False))

    return MyEnergiDevice(
        device_id=device_id or (CLOUD_PREFIX[kind] + serial),
        kind=kind,
        serial=serial,
        name=meta.get("alias") or "{}-{}".format(kind, serial),
        online=bool(meta.get("online", True)),
        status=status,
        mode=mode,
        plug_status=plug_status,
        # The cloud API reports power in kW, the direct API in W. Normalise to W.
        power_w=_to_float(raw.get("actualPower")) * 1000.0,
        grid_power_w=_to_float(raw.get("gridPower")) * 1000.0,
        generation_w=_to_float(raw.get("genPower")) * 1000.0,
        voltage=0.0,
        session_energy_kwh=_to_float(raw.get("sessionEnergy")),
        boost_active=boost_active,
        boost_remaining_mins=0,
        # The cloud API does not expose Eddi probe temperatures
        temp_1=None,
        temp_2=None,
    )


class MyEnergiTransport(ABC):
    """Wire-format adapter for one of the two myenergi APIs.

    This is the only layer that knows how a myenergi request is shaped. Everything
    above it works in terms of MyEnergiDevice, so adding or changing a transport
    never touches publishing, auto-configuration or the controls.
    """

    def __init__(self, log):
        """Store the logging function and initialise the one-shot warning set."""
        self.log = log
        self._warned_stubs = set()

    @abstractmethod
    async def connect(self):
        """Establish and validate the connection. Returns True on success."""

    @abstractmethod
    async def fetch_devices(self):
        """Return a list of MyEnergiDevice for every supported device found."""

    @abstractmethod
    async def send_boost(self, device, amount, target_time=None):
        """Start a boost on a device.

        Args:
            device: The MyEnergiDevice to boost.
            amount: kWh for a Zappi, minutes for an Eddi.
            target_time: Optional "HH:MM" completion time, Zappi smart boost only.
        """

    @abstractmethod
    async def cancel_boost(self, device):
        """Cancel an active boost on a device."""

    @abstractmethod
    async def set_mode(self, device, mode):
        """Set a device's supply mode, named as ZAPPI_CHARGE_MODES spells it."""

    def _not_implemented(self, what):
        """Warn once that a control is not implemented in this release, and return False."""
        if what not in self._warned_stubs:
            self._warned_stubs.add(what)
            self.log("Warn: myenergi: {} is not implemented in this release".format(what))
        return False

    async def set_priority(self, device, priority):
        """Set the device diversion priority. Not implemented in this release."""
        return self._not_implemented("set_priority")

    async def set_min_green_level(self, device, level):
        """Set the Zappi minimum green level. Not implemented in this release."""
        return self._not_implemented("set_min_green_level")

    async def set_phase_setting(self, device, phase):
        """Set the Zappi phase setting. Not implemented in this release."""
        return self._not_implemented("set_phase_setting")

    async def get_schedule(self, device):
        """Read the device charging schedule. Not implemented in this release."""
        return self._not_implemented("get_schedule")

    async def set_schedule(self, device, schedule):
        """Write the device charging schedule. Not implemented in this release."""
        return self._not_implemented("set_schedule")


class MyEnergiDirectTransport(MyEnergiTransport):
    """Transport for the direct myenergi API used by pymyenergi and ha-myenergi.

    Authenticates with HTTP digest, using the hub serial as the username and the
    API key generated at myaccount.myenergi.com as the password. myenergi shards
    accounts across servers, so the first request goes to director.myenergi.net,
    whose X_MYENERGI-asn response header names the host to use from then on.
    """

    def __init__(self, log, hub_serial, api_key):
        """Store credentials and start with an unresolved active server."""
        super().__init__(log)
        self.hub_serial = str(hub_serial)
        self.api_key = api_key
        self.base_url = None
        self.needs_asn_refresh = True

    def _new_session(self):
        """Create an aiohttp session carrying the digest auth middleware."""
        if not digest_auth_available():
            raise MyEnergiApiError(AIOHTTP_DIGEST_REQUIRED.format(getattr(aiohttp, "__version__", "unknown")))
        digest = aiohttp.DigestAuthMiddleware(self.hub_serial, self.api_key)
        return aiohttp.ClientSession(middlewares=(digest,), headers={"User-Agent": USER_AGENT})

    def _update_asn(self, headers):
        """Follow the active server named by the X_MYENERGI-asn response header."""
        asn = headers.get("X_MYENERGI-asn")
        if not asn:
            raise MyEnergiAuthError("no X_MYENERGI-asn header returned - check the hub serial and API key")
        new_url = "https://" + asn
        if new_url != self.base_url:
            self.log("Info: myenergi: active server is {}".format(new_url))
            self.base_url = new_url

    async def _resolve_asn(self):
        """Ask director.myenergi.net which server this account lives on.

        Wrapped in the same status/timeout handling as _request, since this
        cold-start call targets a different host (the shared director, not the
        account's own server) and is the request most likely to hit a network
        failure. A non-200 response is reported as MyEnergiApiError rather than
        diagnosed as bad credentials - an outage or error page from the director
        has no reason to carry the ASN header, so the missing-header check only
        applies once the request itself actually succeeded.
        """
        path = "/cgi-jstatus-E"
        try:
            async with self._new_session() as session:
                async with session.get(MYENERGI_DIRECTOR_URL + path, timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)) as response:
                    if response.status == 401:
                        record_api_call("myenergi", success=False, reason="auth_error")
                        raise MyEnergiAuthError("myenergi rejected the credentials for {}".format(path))
                    if response.status != 200:
                        self.needs_asn_refresh = True
                        reason = "server_error" if response.status >= 500 else "client_error"
                        record_api_call("myenergi", success=False, reason=reason)
                        raise MyEnergiApiError("HTTP {} from {}".format(response.status, path))
                    self._update_asn(response.headers)
                    record_api_call("myenergi", success=True)
        except asyncio.TimeoutError as exc:
            self.needs_asn_refresh = True
            record_api_call("myenergi", success=False, reason="connection_error")
            raise MyEnergiApiError("timed out calling {}".format(path)) from exc
        except aiohttp.ClientError as exc:
            self.needs_asn_refresh = True
            record_api_call("myenergi", success=False, reason="connection_error")
            raise MyEnergiApiError("request to {} failed: {}".format(path, exc)) from exc
        self.needs_asn_refresh = False

    async def _request(self, path):
        """Perform one GET against the active server, resolving the ASN if needed."""
        if self.base_url is None or self.needs_asn_refresh:
            await self._resolve_asn()
        url = self.base_url + path
        try:
            async with self._new_session() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)) as response:
                    if response.status == 401:
                        record_api_call("myenergi", success=False, reason="auth_error")
                        raise MyEnergiAuthError("myenergi rejected the credentials for {}".format(path))
                    if response.status != 200:
                        self.needs_asn_refresh = True
                        reason = "server_error" if response.status >= 500 else "client_error"
                        record_api_call("myenergi", success=False, reason=reason)
                        raise MyEnergiApiError("HTTP {} from {}".format(response.status, path))
                    # The missing-header check runs only once the request itself succeeded,
                    # for the same reason _resolve_asn() orders it this way: an outage or an
                    # error page has no reason to carry the ASN header, and reporting that as
                    # bad credentials sends the user off to regenerate a perfectly good key.
                    self._update_asn(response.headers)
                    try:
                        payload = await response.json(content_type=None)
                    except (ValueError, TypeError) as exc:
                        record_api_call("myenergi", success=False, reason="decode_error")
                        raise MyEnergiApiError("could not decode the response from {}".format(path)) from exc
                    record_api_call("myenergi", success=True)
                    return payload
        except asyncio.TimeoutError as exc:
            self.needs_asn_refresh = True
            record_api_call("myenergi", success=False, reason="connection_error")
            raise MyEnergiApiError("timed out calling {}".format(path)) from exc
        except aiohttp.ClientError as exc:
            self.needs_asn_refresh = True
            record_api_call("myenergi", success=False, reason="connection_error")
            raise MyEnergiApiError("request to {} failed: {}".format(path, exc)) from exc

    async def connect(self):
        """Resolve the active server, which also validates the credentials."""
        await self._resolve_asn()
        return True

    async def fetch_devices(self):
        """Fetch every device in one /cgi-jstatus-* call and normalise the supported ones.

        The response is a list of single-key dicts, one per device family, plus
        housekeeping entries such as {"asn": ...} and {"fwv": ...} that are skipped.
        """
        payload = await self._request("/cgi-jstatus-*")
        devices = []
        if not isinstance(payload, list):
            return devices
        for group in payload:
            if not isinstance(group, dict):
                continue
            for kind, records in group.items():
                if kind not in SUPPORTED_KINDS or not isinstance(records, list):
                    continue
                for raw in records:
                    if isinstance(raw, dict):
                        devices.append(normalise_direct_device(raw, kind))
        return devices

    def _check_command_status(self, payload, path):
        """Raise when a /cgi-* command response reports a failure status.

        The command endpoints answer HTTP 200 whether or not they acted, carrying the
        real outcome in a {"status": N} body - a non-zero N means myenergi refused the
        command (an Eddi already at maximum temperature, a Zappi that will not accept
        the mode, an unknown device). Without this the caller would log a success and
        the switch would quietly flip back on the next poll. Only a numeric non-zero
        status counts as a failure: some endpoints answer with no body at all, and an
        unparseable body is left to the caller rather than invented into an error.
        """
        if not isinstance(payload, dict):
            return
        status = payload.get("status")
        if status is None:
            return
        try:
            code = int(status)
        except (TypeError, ValueError):
            return
        if code != 0:
            raise MyEnergiApiError("myenergi refused {} with status {}".format(path, code))

    async def set_mode(self, device, mode):
        """Set a Zappi's supply mode, e.g. Fast to charge now or Stopped to hold off."""
        validate_zappi_mode(device, mode)
        path = "/cgi-zappi-mode-Z{}-{}-0-0-0000".format(device.serial, ZAPPI_CHARGE_MODES.index(mode))
        self._check_command_status(await self._request(path), path)
        return True

    async def send_boost(self, device, amount, target_time=None):
        """Start a boost, choosing the manual or smart command for a Zappi."""
        if device.kind == DEVICE_KIND_ZAPPI:
            energy = _boost_units(amount)
            if target_time:
                # The command wants HHMM, so "7:30" has to become "0730" and not "730",
                # which myenergi would read as 07:30 shifted by a digit.
                when = str(target_time).replace(":", "").zfill(4)
                path = "/cgi-zappi-mode-Z{}-0-11-{}-{}".format(device.serial, energy, when)
            else:
                path = "/cgi-zappi-mode-Z{}-0-10-{}-0000".format(device.serial, energy)
        elif device.kind == DEVICE_KIND_EDDI:
            target = EDDI_BOOST_TARGETS[EDDI_DEFAULT_BOOST_TARGET]
            path = "/cgi-eddi-boost-E{}-10-{}-{}".format(device.serial, target, _boost_units(amount))
        else:
            raise MyEnergiApiError("cannot boost unsupported device kind '{}'".format(device.kind))
        self._check_command_status(await self._request(path), path)
        return True

    async def cancel_boost(self, device):
        """Cancel an active boost."""
        if device.kind == DEVICE_KIND_ZAPPI:
            path = "/cgi-zappi-mode-Z{}-0-2-0-0000".format(device.serial)
        elif device.kind == DEVICE_KIND_EDDI:
            target = EDDI_BOOST_TARGETS[EDDI_DEFAULT_BOOST_TARGET]
            path = "/cgi-eddi-boost-E{}-1-{}-0".format(device.serial, target)
        else:
            raise MyEnergiApiError("cannot cancel a boost on unsupported device kind '{}'".format(device.kind))
        self._check_command_status(await self._request(path), path)
        return True


# The cloud device list changes rarely, so it is cached between polls.
CLOUD_DEVICE_LIST_MAX_AGE = 30 * 60

# Model names in GET /devices that map onto the kinds this release supports.
CLOUD_MODEL_TO_KIND = {"zappi": DEVICE_KIND_ZAPPI, "eddi": DEVICE_KIND_EDDI}


class MyEnergiCloudTransport(MyEnergiTransport):
    """Transport for the official myenergi 3rd party API.

    Authenticates with a bearer JWT obtained through the OAuth2 authorization code
    flow. The token is read through a callable on every request so that a refresh
    performed by OAuthMixin on the component takes effect immediately.
    """

    def __init__(self, log, access_token_getter):
        """Store the token accessor and initialise the device list cache."""
        super().__init__(log)
        self.access_token_getter = access_token_getter
        self.device_meta = {}
        # Wall-clock stamp of the last GET /devices. A counter was tried first and never
        # advanced, so the cache never expired and a device added, removed or renamed in
        # the myenergi app stayed invisible until Predbat restarted.
        self.meta_fetched_at = 0.0

    def _headers(self):
        """Build the request headers, including the current bearer token."""
        return {
            "Authorization": "Bearer {}".format(self.access_token_getter() or ""),
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    async def _request(self, method, path, body=None):
        """Perform one cloud API request and return the decoded JSON body."""
        url = MYENERGI_CLOUD_URL + path
        try:
            async with aiohttp.ClientSession(headers=self._headers()) as session:
                async with session.request(method, url, json=body, timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)) as response:
                    if response.status == 401:
                        record_api_call("myenergi", success=False, reason="auth_error")
                        raise MyEnergiAuthError("myenergi rejected the access token for {}".format(path))
                    if response.status not in (200, 201, 202, 204):
                        reason = "server_error" if response.status >= 500 else "client_error"
                        record_api_call("myenergi", success=False, reason=reason)
                        raise MyEnergiApiError("HTTP {} from {} {}".format(response.status, method, path))
                    if response.status == 204:
                        record_api_call("myenergi", success=True)
                        return {}
                    try:
                        payload = await response.json(content_type=None)
                    except (ValueError, TypeError) as exc:
                        record_api_call("myenergi", success=False, reason="decode_error")
                        raise MyEnergiApiError("could not decode the response from {} {}".format(method, path)) from exc
                    record_api_call("myenergi", success=True)
                    return payload
        except asyncio.TimeoutError as exc:
            record_api_call("myenergi", success=False, reason="connection_error")
            raise MyEnergiApiError("timed out calling {} {}".format(method, path)) from exc
        except aiohttp.ClientError as exc:
            record_api_call("myenergi", success=False, reason="connection_error")
            raise MyEnergiApiError("request to {} {} failed: {}".format(method, path, exc)) from exc

    async def _refresh_device_list(self):
        """Reload GET /devices, keeping only the Zappi and Eddi entries."""
        payload = await self._request("GET", "/devices")
        if not isinstance(payload, dict):
            raise MyEnergiApiError("unexpected device list response shape from GET /devices")
        meta = {}
        for site in payload.get("sites", []) or []:
            for entry in site.get("devices", []) or []:
                kind = CLOUD_MODEL_TO_KIND.get(str(entry.get("model", "")).lower())
                device_id = entry.get("deviceId")
                if kind and device_id:
                    meta[device_id] = entry
        self.device_meta = meta
        self.meta_fetched_at = time.time()

    async def connect(self):
        """Load the device list, which also validates the access token."""
        await self._refresh_device_list()
        return True

    async def fetch_devices(self):
        """Poll status for every cached Zappi and Eddi, refreshing the list when stale.

        One device failing is tolerated so the rest stay visible, but a poll that reads
        none of the devices it knows about is a failed poll rather than an empty site,
        and raises. Returning [] there would let run() keep the previous readings and
        still stamp success, so a site whose every device was erroring would report
        healthy for as long as it kept failing.
        """
        if not self.device_meta or (time.time() - self.meta_fetched_at) >= CLOUD_DEVICE_LIST_MAX_AGE:
            await self._refresh_device_list()
        devices = []
        skipped = 0
        for device_id, meta in self.device_meta.items():
            # One device failing its status call must not cost the whole poll: the
            # remaining devices are still readable, and dropping them all would blank
            # every published entity over a single device being briefly unreachable.
            try:
                status = await self._request("GET", "/devices/{}/status".format(device_id))
            except MyEnergiApiError as exc:
                self.log("Warn: myenergi: skipping {} this poll: {}".format(device_id, exc))
                skipped += 1
                continue
            if not status:
                self.log("Warn: myenergi: no status returned for {}".format(device_id))
                skipped += 1
                continue
            devices.append(normalise_cloud_device(status, meta))
        # An account with no Zappi or Eddi at all leaves both counts at zero and is not
        # an error - only having devices and reading none of them is.
        if skipped and not devices:
            raise MyEnergiApiError("no myenergi device could be read this poll, {} skipped".format(skipped))
        return devices

    async def set_mode(self, device, mode):
        """Set a Zappi's supply mode, translating the name into the API's own spelling."""
        validate_zappi_mode(device, mode)
        body = {"supplyMode": ZAPPI_MODE_TO_CLOUD[mode]}
        await self._request("POST", "/devices/{}/mode".format(device.device_id), body=body)
        return True

    async def send_boost(self, device, amount, target_time=None):
        """Start a boost, selecting the request body shape by device class.

        Sending a Zappi body to an Eddi (or the reverse) is a documented 400, so an
        unrecognised kind is refused here rather than defaulted into the Eddi shape -
        matching MyEnergiDirectTransport.send_boost.
        """
        if device.kind == DEVICE_KIND_ZAPPI:
            body = {"mode": "normal", "parameters": {"energy": _boost_units(amount)}}
            if target_time:
                body = {"mode": "smart", "parameters": {"energy": _boost_units(amount), "targetTime": target_time}}
        elif device.kind == DEVICE_KIND_EDDI:
            body = {"durationMinutes": _boost_units(amount)}
        else:
            raise MyEnergiApiError("cannot boost unsupported device kind '{}'".format(device.kind))
        await self._request("POST", "/devices/{}/boost".format(device.device_id), body=body)
        return True

    async def cancel_boost(self, device):
        """Cancel an active boost."""
        if device.kind not in SUPPORTED_KINDS:
            raise MyEnergiApiError("cannot cancel a boost on unsupported device kind '{}'".format(device.kind))
        await self._request("DELETE", "/devices/{}/boost".format(device.device_id))
        return True


# Attribute table for the published Home Assistant entities, in the style of ohme.py
myenergi_attribute_table = {
    "status": {"friendly_name": "myenergi Status", "icon": "mdi:information-outline"},
    "mode": {"friendly_name": "myenergi Mode", "icon": "mdi:ev-station"},
    "plug_status": {"friendly_name": "myenergi Plug Status", "icon": "mdi:ev-plug-type2"},
    "power": {"friendly_name": "myenergi Power", "icon": "mdi:lightning-bolt", "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
    "session_energy": {"friendly_name": "myenergi Session Energy", "icon": "mdi:lightning-bolt", "unit_of_measurement": "kWh", "device_class": "energy", "state_class": "total_increasing"},
    "charging": {"friendly_name": "myenergi Charging", "icon": "mdi:battery-charging"},
    "boost": {"friendly_name": "myenergi Boost", "icon": "mdi:rocket-launch"},
    "zappi_control": {"friendly_name": "myenergi Zappi Charge Control", "icon": "mdi:ev-station"},
    "boost_energy": {"friendly_name": "myenergi Boost Energy", "icon": "mdi:rocket-launch", "unit_of_measurement": "kWh", "min": BOOST_ENERGY_MIN, "max": BOOST_ENERGY_MAX, "step": 1},
    "boost_minutes": {"friendly_name": "myenergi Boost Minutes", "icon": "mdi:rocket-launch", "unit_of_measurement": "minutes", "min": BOOST_MINUTES_MIN, "max": BOOST_MINUTES_MAX, "step": 5},
    "temp_1": {"friendly_name": "myenergi Temperature 1", "icon": "mdi:thermometer", "unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    "temp_2": {"friendly_name": "myenergi Temperature 2", "icon": "mdi:thermometer", "unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
}

DEFAULT_ZAPPI_BOOST_KWH = 10
DEFAULT_EDDI_BOOST_MINUTES = 60

# components.py's is_alive() marks a component failed once its last successful update is
# more than 60 minutes old, and the success timestamp is only stamped by a poll that ran.
# Capping the interval at half that window keeps a slow poll from being reported as a
# component error, while still being far slower than anyone realistically wants.
MIN_POLL_SECONDS = 60
MAX_POLL_SECONDS = 30 * 60


class MyEnergiAPI(ComponentBase, OAuthMixin):
    """myenergi component providing Zappi and Eddi monitoring and boost control."""

    def initialize(self, auth_method=None, hub_serial=None, api_key=None, key=None, token_expires_at=None, token_hash=None, automatic=True, enable_controls=True, poll_seconds=60, zappi_control=False):
        """Select a transport from the configured credentials and set up component state."""
        configured_auth_method = (auth_method or "direct").lower()
        self.hub_serial = hub_serial
        self.api_key = api_key
        self.automatic = automatic
        self.enable_controls = enable_controls
        self.zappi_control = bool(zappi_control)
        # ComponentBase.start() calls run() on a fixed 60 second cadence, so the poll
        # interval can only be a whole number of those intervals.
        self.poll_seconds = min(MAX_POLL_SECONDS, max(MIN_POLL_SECONDS, int(round(_to_float(poll_seconds, MIN_POLL_SECONDS) / 60.0)) * 60))

        self.devices = {}
        self.boost_amounts = {}
        self.control_windows = {}
        self.control_modes = {}
        self.control_saved_modes = {}
        self.control_active = False
        # The runtime switch, on unless the user turns it off. Restored from storage at startup.
        self.control_enabled = True
        self.control_released = False
        self.queued_events = []
        self._auto_configured = False
        self.transport = None

        if configured_auth_method == "oauth":
            self._init_oauth("oauth", key, token_expires_at, "myenergi")
            # _init_oauth() sets self.auth_method to its own "oauth"/"api_key" vocabulary,
            # overwriting whatever was assigned above - keep the user-facing "direct"/"oauth"
            # value (used for logging and for tests asserting the selection) in its own
            # attribute so it never depends on a name oauth_mixin.py owns.
            self.auth_method_config = configured_auth_method
            self.token_hash = token_hash or ""
            if not key and not token_hash:
                self.log("Error: myenergi: auth_method is 'oauth' but neither myenergi_key nor myenergi_token_hash is set")
                return
            self.transport = MyEnergiCloudTransport(self.log, lambda: self.access_token)
        else:
            self._init_oauth("api_key", None, None, "myenergi")
            self.auth_method_config = configured_auth_method
            if not hub_serial or not api_key:
                self.log("Error: myenergi: auth_method is 'direct' but myenergi_hub_serial and myenergi_api_key are not both set")
                return
            if not digest_auth_available():
                self.log("Error: myenergi: " + AIOHTTP_DIGEST_REQUIRED.format(getattr(aiohttp, "__version__", "unknown")))
                return
            self.transport = MyEnergiDirectTransport(self.log, hub_serial, api_key)

    def entity_prefix(self, device):
        """Return the entity name prefix for a device, e.g. predbat_myenergi_zappi_12345678."""
        return "{}_myenergi_{}_{}".format(self.prefix, device.kind, device.serial)

    def automatic_config(self):
        """Wire the device sensors into Predbat's load and car planning inputs.

        Zappi charging energy is subtracted from house load as car charging, so it
        goes to car_charging_energy - as a list, because minute_data_import_export
        accepts one and sums the entities. The matching plug status sensors go to
        car_charging_planned, which is indexed per car, so entry N is the Nth Zappi;
        without it Predbat falls back to the car_charging_threshold heuristic, because
        the regex the apps.yaml templates ship targets the third-party ha-myenergi
        integration's entity names rather than the ones this component publishes. Eddi
        diverted energy feeds iboost_energy_today.

        These sensors are session-scoped and reset to zero when a session ends. That is
        handled: get_from_incrementing() clamps negative deltas to zero for the
        per-minute subtraction, and the daily totals go through minute_data_load()'s
        clean_incrementing_reverse(), which rebases the series on a reset. The residual
        limitation is narrower - minute_data() smooths a fall of less than 1 kWh as a dip
        in the data (utils.py:565) before clean_incrementing_reverse() ever looks for a
        reset (utils.py:740), so a session ending below roughly 1 kWh is under-counted.
        An intervening zero reading does not rescue it, because the dip is smoothed away
        first. That loss is in the shared cumulative series, so it affects
        car_charging_energy and iboost_today alike. Documented in docs/components.md.

        The Zappi live power sensors go to car_charging_power, which is display-only: it feeds
        the web power flow diagram and the predbat.car_charging_power sensor, never the plan.
        """
        zappi_energy_entities = []
        zappi_power_entities = []
        zappi_plug_entities = []
        eddi_entity = None
        for device in sorted(self.devices.values(), key=lambda item: item.serial):
            prefix = self.entity_prefix(device)
            if device.kind == DEVICE_KIND_ZAPPI:
                zappi_energy_entities.append("sensor.{}_session_energy".format(prefix))
                zappi_power_entities.append("sensor.{}_power".format(prefix))
                zappi_plug_entities.append("sensor.{}_plug_status".format(prefix))
            elif device.kind == DEVICE_KIND_EDDI and eddi_entity is None:
                eddi_entity = "sensor.{}_session_energy".format(prefix)

        if zappi_energy_entities:
            self.log("Info: myenergi: setting car_charging_energy to {}".format(zappi_energy_entities))
            self.set_arg_auto("car_charging_energy", zappi_energy_entities)
            self.log("Info: myenergi: setting car_charging_planned to {}".format(zappi_plug_entities))
            self.set_arg_auto("car_charging_planned", zappi_plug_entities)
            self.log("Info: myenergi: setting car_charging_power to {}".format(zappi_power_entities))
            self.set_arg_auto("car_charging_power", zappi_power_entities)
        if eddi_entity:
            self.log("Info: myenergi: setting iboost_energy_today to {}".format(eddi_entity))
            self.set_arg_auto("iboost_energy_today", eddi_entity)

    def refresh_car_windows(self, now):
        """Read Predbat's planned car charging windows for every car into control_windows.

        Returns True once at least one car's plan has been read, False while no slot sensor
        has ever been published - which is what stops the loop stopping a car on startup,
        before Predbat has decided anything.

        The caller passes now so every car is judged against the same instant, and so the
        parsing stays a pure function of the plan and the clock.
        """
        windows = {}
        found = False
        for car_n in range(self.num_cars):
            postfix = "" if car_n == 0 else "_{}".format(car_n)
            planned = self.get_state_wrapper("binary_sensor.{}_car_charging_slot{}".format(self.prefix, postfix), attribute="planned")
            if planned is None:
                continue
            found = True
            windows[car_n] = self._parse_plan_windows(planned, now)
        self.control_windows = windows
        return found

    def _parse_plan_windows(self, planned, now):
        """Turn one car's published plan into a list of localised (start, end) pairs."""
        return parse_car_plan_windows(planned, now, self.local_tz)

    def should_charge_now(self, car_n, now):
        """Is now inside one of the planned charging windows for this car."""
        return in_car_plan_window(self.control_windows.get(car_n, []), now)

    def enable_control(self):
        """Decide whether Predbat-led Zappi control should run, and say why when it will not.

        Control needs automatic configuration because a Zappi is driven from its own car's
        plan, and it is auto-config that establishes which Zappi is which car.
        """
        if not self.zappi_control:
            return
        if not self.automatic:
            self.log("Warn: myenergi: myenergi_zappi_control needs myenergi_automatic to map each Zappi to a car, Zappi control is disabled")
            return
        if not self.enable_controls:
            self.log("Warn: myenergi: myenergi_zappi_control is ignored while myenergi_enable_controls is off")
            return
        self.control_active = True
        self.log("Info: myenergi: Predbat-led Zappi charge control enabled")

    async def save_control_enabled(self):
        """Persist the control switch so an off survives a restart.

        Without this a restart would silently hand Predbat back a Zappi the user had
        deliberately released, which they would only notice when the car charged at the
        wrong time. Fails soft: no Storage component just means the switch is not sticky.
        """
        if self.storage is None:
            return
        try:
            await self.storage.save(MYENERGI_STORAGE_MODULE, MYENERGI_CONTROL_STATE, {"control_enabled": self.control_enabled})
        except Exception as exc:
            self.log("Warn: myenergi: could not save the Zappi control switch state: {}".format(exc))

    async def load_control_enabled(self):
        """Restore the control switch from storage, leaving it on when nothing is saved."""
        if self.storage is None:
            return
        try:
            saved = await self.storage.load(MYENERGI_STORAGE_MODULE, MYENERGI_CONTROL_STATE)
        except Exception as exc:
            self.log("Warn: myenergi: could not read the Zappi control switch state: {}".format(exc))
            return
        if isinstance(saved, dict) and "control_enabled" in saved:
            self.control_enabled = bool(saved["control_enabled"])
            if not self.control_enabled:
                self.log("Info: myenergi: Zappi charge control is switched off from the last session")

    def control_read_only_now(self):
        """Is Predbat in read only mode - the live attribute rather than just the config arg.

        axle_control forces read only by setting the attribute without touching the arg, so
        read the attribute first and fall back to the arg for the window before it is set.
        """
        read_only = getattr(self.base, "set_read_only", None)
        if read_only is None:
            read_only = self.get_arg("set_read_only", False)
        return bool(read_only)

    async def control_tick(self, now):
        """Run one cycle of Zappi control, releasing rather than just going quiet.

        Read only mode and the control switch are both releases: Predbat may have left a
        Zappi Stopped, and walking away from that would strand the car unable to charge.
        """
        if not self.control_active:
            return
        reason = None
        if self.control_read_only_now():
            reason = "Predbat is in read only mode"
        elif not self.control_enabled:
            reason = "the Zappi control switch is off"
        if reason:
            if not self.control_released:
                self.log("Info: myenergi: releasing the Zappis because {}".format(reason))
                await self.release_zappis()
                self.control_released = True
            return
        if self.control_released:
            self.log("Info: myenergi: resuming Zappi charge control")
            self.control_released = False
        await self.control_charge(now)

    async def release_zappis(self):
        """Hand every held Zappi back, restoring the mode it had before Predbat took over.

        Falls back to Eco+ when nothing was saved - a restart, or a device that reported a
        mode neither API accepts back - so a released Zappi always lands somewhere useful
        rather than being left Stopped.
        """
        for device in self.controlled_zappis():
            if device.device_id not in self.control_modes:
                continue
            mode = self.control_saved_modes.get(device.device_id)
            if mode not in ZAPPI_MODE_TO_CLOUD:
                mode = ZAPPI_MODE_RELEASE
            self.log("Info: myenergi: releasing {} back to {}".format(device.name, mode))
            await self.transport.set_mode(device, mode)
        self.control_modes = {}
        self.control_saved_modes = {}

    def controlled_zappis(self):
        """The Zappis to drive, in serial order, so Zappi N is the same car as auto-config's Nth.

        automatic_config() wires car_charging_energy and car_charging_planned as per-car
        lists in this same order, so the two cannot disagree about which Zappi is which car.
        """
        return [device for device in sorted(self.devices.values(), key=lambda item: item.serial) if device.kind == DEVICE_KIND_ZAPPI]

    async def control_charge(self, now):
        """Drive every controlled Zappi from its car's charge plan.

        Predbat holds the charger for as long as it is in control: Fast inside a planned
        window, Stopped outside one. Fast is the only mode that draws what the plan assumed,
        since the window was chosen for its rate rather than for sunshine.

        The caller passes now so every Zappi is judged against one instant.
        """
        if not self.refresh_car_windows(now):
            return
        for car_n, device in enumerate(self.controlled_zappis()):
            wanted = ZAPPI_MODE_CHARGING if self.should_charge_now(car_n, now) else ZAPPI_MODE_STOPPED
            asked = self.control_modes.get(device.device_id)
            if asked == wanted and device.mode == wanted:
                continue
            if asked == wanted:
                self.log("Info: myenergi: {} was changed away from {}, re-applying".format(device.name, wanted))
            # Remember where the charger was before Predbat first moved it, so release can put it back
            if device.device_id not in self.control_saved_modes:
                self.control_saved_modes[device.device_id] = device.mode
            self.log("Info: myenergi: setting {} to {} for car {}".format(device.name, wanted, car_n))
            await self.transport.set_mode(device, wanted)
            self.control_modes[device.device_id] = wanted

    def boost_amount_for(self, device):
        """Return the currently selected boost amount for a device."""
        default = DEFAULT_ZAPPI_BOOST_KWH if device.kind == DEVICE_KIND_ZAPPI else DEFAULT_EDDI_BOOST_MINUTES
        return self.boost_amounts.get(device.device_id, default)

    async def run(self, seconds, first):
        """Process queued control events, then poll and publish."""
        if first:
            self.log("Info: myenergi: starting with the {} transport".format(self.auth_method_config))
        if not self.transport:
            return False

        if self.auth_method == "oauth":
            if not await self.check_and_refresh_oauth_token():
                return False

        refresh = False
        while self.queued_events:
            handler, *event_args = self.queued_events.pop(0)
            try:
                await handler(*event_args)
            except MyEnergiError as exc:
                self.log("Warn: myenergi: control failed: {}".format(exc))
            refresh = True

        if first or refresh or (seconds % self.poll_seconds) == 0:
            try:
                devices = await self.transport.fetch_devices()
            except MyEnergiAuthError as exc:
                # The proactive refresh above only covers a token that has reached its
                # stated expiry. A token revoked before then wedges the component until
                # restart unless the 401 itself triggers a refresh, so retry the poll
                # once behind one, as fox.py, deye.py and solis.py do.
                devices = await self._retry_poll_after_refresh(exc)
                if devices is None:
                    return False
            except MyEnergiError as exc:
                self.log("Warn: myenergi: poll failed: {}".format(exc))
                return False
            if devices:
                self.devices = {device.device_id: device for device in devices}
                if first:
                    # Before the first publish: the switch has to carry its restored state
                    # from the start, or a restart with control switched off would show it
                    # on for a cycle and then flip, looking like Predbat taking control back
                    await self.load_control_enabled()
                    self.enable_control()
                await self.publish_data()
                if self.automatic and not self._auto_configured:
                    self.automatic_config()
                    self._auto_configured = True
                if self.control_active:
                    try:
                        await self.control_tick(self.now_utc_exact)
                    except MyEnergiError as exc:
                        # myenergi can refuse a mode for reasons Predbat cannot see - nothing
                        # plugged in, a fault on the charger. Monitoring still succeeded, so
                        # this is a warning rather than a failed cycle. Nothing is recorded as
                        # set, so the next cycle tries again.
                        self.log("Warn: myenergi: Zappi charge control failed: {}".format(exc))
            elif first:
                self.log("Warn: myenergi: connected but no Zappi or Eddi devices were found")
            # Stamped only by a cycle that actually polled, so the health check reflects
            # real API contact. poll_seconds is capped at MAX_POLL_SECONDS for exactly
            # this reason - see the comment there.
            self.update_success_timestamp()
        return True

    async def _retry_poll_after_refresh(self, exc):
        """Refresh the OAuth token after a 401 and poll once more. Returns devices, or None on failure."""
        if self.auth_method != "oauth" or not await self.handle_oauth_401():
            self.log("Warn: myenergi: poll failed: {}".format(exc))
            return None
        try:
            return await self.transport.fetch_devices()
        except MyEnergiError as retry_exc:
            self.log("Warn: myenergi: poll failed again after refreshing the token: {}".format(retry_exc))
            return None

    def device_for_entity(self, entity_id):
        """Find the device an entity belongs to, or None when it is not known.

        The trailing underscore anchors the match to a whole prefix, so a serial that
        is a prefix of another device's serial cannot claim the other one's entities.
        """
        for device in self.devices.values():
            if "{}_".format(self.entity_prefix(device)) in entity_id:
                return device
        return None

    async def switch_event(self, entity_id, service):
        """Queue a switch service call for the run loop."""
        if not self.enable_controls:
            return
        self.queued_events.append((self.switch_event_handler, entity_id, service))

    async def number_event(self, entity_id, value):
        """Queue a number change for the run loop."""
        if not self.enable_controls:
            return
        self.queued_events.append((self.number_event_handler, entity_id, value))

    async def number_event_handler(self, entity_id, value):
        """Record a new boost amount for the device the entity belongs to.

        Guarded on the entity suffix, symmetrically with switch_event_handler: without
        it any future number.{prefix}_* entity would be read as a boost amount and
        clamped into boost_amounts purely because it belongs to a known device.
        """
        if not entity_id.endswith(("_boost_energy", "_boost_minutes")):
            return
        device = self.device_for_entity(entity_id)
        if not device:
            return
        if device.kind == DEVICE_KIND_ZAPPI:
            amount = int(_to_float(value, DEFAULT_ZAPPI_BOOST_KWH))
            amount = max(BOOST_ENERGY_MIN, min(BOOST_ENERGY_MAX, amount))
        else:
            amount = int(_to_float(value, DEFAULT_EDDI_BOOST_MINUTES))
            amount = max(BOOST_MINUTES_MIN, min(BOOST_MINUTES_MAX, amount))
        self.boost_amounts[device.device_id] = amount

    async def switch_event_handler(self, entity_id, service):
        """Send or cancel a boost in response to the boost switch.

        Returns whether the command was actually issued and accepted, so a rejection
        surfaces as the run loop's "control failed" warning instead of being logged as
        a success that the next poll silently contradicts.
        """
        if not self.enable_controls:
            return False
        if entity_id.endswith("_myenergi_zappi_control"):
            self.control_enabled = service == "turn_on"
            self.log("Info: myenergi: Zappi charge control switched {}".format("on" if self.control_enabled else "off"))
            await self.save_control_enabled()
            return True
        if not entity_id.endswith("_boost"):
            return False
        device = self.device_for_entity(entity_id)
        if not device:
            self.log("Warn: myenergi: no known device for {}".format(entity_id))
            return False

        if service == "turn_on":
            # myenergi rejects a boost unless the Zappi is in one of the green modes
            if device.kind == DEVICE_KIND_ZAPPI and device.mode not in ZAPPI_BOOSTABLE_MODES:
                self.log("Warn: myenergi: cannot boost {} while it is in {} mode - boost needs Eco or Eco+".format(device.name, device.mode))
                return False
            amount = self.boost_amount_for(device)
            self.log("Info: myenergi: boosting {} by {}".format(device.name, amount))
            return await self.transport.send_boost(device, amount)
        if service == "turn_off":
            self.log("Info: myenergi: cancelling boost on {}".format(device.name))
            return await self.transport.cancel_boost(device)
        return False

    async def publish_data(self):
        """Publish every known device as Predbat entities."""
        if self.control_active:
            # Published only when control could actually act on it. Gating on the config
            # key alone would leave a switch reading "on" for a feature that cannot run -
            # monitor-only mode, or automatic configuration off - and making that switch
            # merely respond to a toggle would keep it live without making it honest.
            self.dashboard_item(
                "switch.{}_myenergi_zappi_control".format(self.prefix),
                state="on" if self.control_enabled else "off",
                attributes=myenergi_attribute_table["zappi_control"],
                app="myenergi",
            )
        for device in self.devices.values():
            prefix = self.entity_prefix(device)
            self.dashboard_item("sensor.{}_status".format(prefix), state=device.status, attributes=myenergi_attribute_table["status"], app="myenergi")
            self.dashboard_item("sensor.{}_power".format(prefix), state=device.power_w, attributes=myenergi_attribute_table["power"], app="myenergi")
            self.dashboard_item("sensor.{}_session_energy".format(prefix), state=device.session_energy_kwh, attributes=myenergi_attribute_table["session_energy"], app="myenergi")
            self.dashboard_item("switch.{}_boost".format(prefix), state="on" if device.boost_active else "off", attributes=myenergi_attribute_table["boost"], app="myenergi")

            if device.kind == DEVICE_KIND_ZAPPI:
                self.dashboard_item("sensor.{}_mode".format(prefix), state=device.mode, attributes=myenergi_attribute_table["mode"], app="myenergi")
                self.dashboard_item("sensor.{}_plug_status".format(prefix), state=device.plug_status, attributes=myenergi_attribute_table["plug_status"], app="myenergi")
                self.dashboard_item("binary_sensor.{}_charging".format(prefix), state="on" if device.status == STATUS_CHARGING else "off", attributes=myenergi_attribute_table["charging"], app="myenergi")
                self.dashboard_item("number.{}_boost_energy".format(prefix), state=self.boost_amount_for(device), attributes=myenergi_attribute_table["boost_energy"], app="myenergi")
            else:
                self.dashboard_item("number.{}_boost_minutes".format(prefix), state=self.boost_amount_for(device), attributes=myenergi_attribute_table["boost_minutes"], app="myenergi")
                if device.temp_1 is not None:
                    self.dashboard_item("sensor.{}_temp_1".format(prefix), state=device.temp_1, attributes=myenergi_attribute_table["temp_1"], app="myenergi")
                if device.temp_2 is not None:
                    self.dashboard_item("sensor.{}_temp_2".format(prefix), state=device.temp_2, attributes=myenergi_attribute_table["temp_2"], app="myenergi")


# myenergi accepts a command before the device reports it, so a read-back fired straight
# after one still shows the old state. Long enough for the change to land, short enough
# that a boost/cancel test is not a chore to sit through.
COMMAND_SETTLE_SECONDS = 8


def print_device_table(devices):  # pragma: no cover
    """Print the device summary table, for the poll and for a command read-back alike."""
    print("{:<12} {:<10} {:<16} {:<10} {:>10} {:>12}".format("DEVICE", "KIND", "STATUS", "MODE", "POWER W", "SESSION kWh"))
    for device in devices:
        print("{:<12} {:<10} {:<16} {:<10} {:>10.0f} {:>12.2f}".format(device.device_id, device.kind, device.status, device.mode, device.power_w, device.session_energy_kwh))


async def confirm_command(component, device_id):  # pragma: no cover
    """Re-poll after a command and show the device's new state, so one run proves the effect.

    Reads through the transport rather than run(), which would republish every entity and
    bury the two lines that matter.
    """
    print("\nWaiting {}s for the device to report the change...".format(COMMAND_SETTLE_SECONDS))
    await asyncio.sleep(COMMAND_SETTLE_SECONDS)
    devices = await component.transport.fetch_devices()
    device = next((item for item in devices if item.device_id == device_id), None)
    if not device:
        print("{} is no longer in the poll response".format(device_id))
        return
    print_device_table([device])
    remaining = ", {} minutes remaining".format(device.boost_remaining_mins) if device.boost_remaining_mins else ""
    print("Boost active: {}{}".format(device.boost_active, remaining))


async def run_myenergi_cli(args):  # pragma: no cover
    """Run one myenergi poll, and optionally a boost command, against the live API."""
    mock_base = MockBase()
    arg_dict = {
        "auth_method": "oauth" if (args.token or args.token_hash) else "direct",
        "hub_serial": args.hub_serial,
        "api_key": args.api_key,
        "key": args.token,
        "token_hash": args.token_hash,
        # On by default, as it is in apps.yaml, so a harness run shows the car_charging_energy,
        # car_charging_planned and iboost_energy_today wiring a real run would set up - that
        # mapping is most of what there is to check before trusting the component with a car.
        "automatic": not args.no_automatic,
        "enable_controls": True,
    }
    component = MyEnergiAPI(mock_base, **arg_dict)
    if not component.transport:
        print("No usable credentials - pass --hub-serial and --api-key, or --token/--token-hash")
        return

    print("Connecting with the {} transport...".format(component.auth_method_config))
    await component.transport.connect()

    # One whole component cycle, as the other harnesses do, rather than a bare
    # fetch_devices(): publishing is part of run(), so a raw fetch shows the readings but
    # none of the sensors, switches and numbers a real run creates - which is exactly what
    # this harness is used to check before wiring the component into apps.yaml.
    print("Running one poll cycle...")
    if not await component.run(0, True):
        print("The poll cycle failed - see the messages above")
        return
    devices = sorted(component.devices.values(), key=lambda item: item.serial)
    if not devices:
        print("No Zappi or Eddi devices found")
        return

    print("")

    if args.raw:
        for device in devices:
            print(device)
    else:
        print_device_table(devices)

    # The three supply modes Predbat-led charge control drives a Zappi between, so the
    # same commands the component issues can be exercised by hand against a live charger.
    mode = None
    if args.start_charge:
        mode = ZAPPI_MODE_CHARGING
    elif args.stop_charge:
        mode = ZAPPI_MODE_STOPPED
    elif args.release:
        mode = ZAPPI_MODE_RELEASE
    if mode:
        zappi = next((item for item in devices if item.kind == DEVICE_KIND_ZAPPI), None)
        if not zappi:
            print("No Zappi found to control")
            return
        print("\nSetting {} to {}...".format(zappi.name, mode))
        await component.transport.set_mode(zappi, mode)
        await confirm_command(component, zappi.device_id)
        return

    target_kind = args.boost or args.cancel_boost
    if target_kind:
        device = next((item for item in devices if item.kind == target_kind), None)
        if not device:
            print("No {} device found to control".format(target_kind))
            return
        if args.boost:
            print("\nBoosting {} by {}...".format(device.name, args.amount))
            await component.transport.send_boost(device, args.amount)
        else:
            print("\nCancelling boost on {}...".format(device.name))
            await component.transport.cancel_boost(device)
        await confirm_command(component, device.device_id)


def main():  # pragma: no cover
    """Main function for command line execution."""
    parser = argparse.ArgumentParser(description="Test the myenergi API")
    parser.add_argument("--hub-serial", action="store", default=None, help="myenergi hub serial number (direct transport)")
    parser.add_argument("--api-key", action="store", default=None, help="myenergi API key from myaccount.myenergi.com (direct transport)")
    parser.add_argument("--token", action="store", default=None, help="myenergi OAuth access token (cloud transport)")
    parser.add_argument("--token-hash", action="store", default=None, help="myenergi OAuth token hash for refresh (cloud transport)")
    parser.add_argument("--boost", choices=SUPPORTED_KINDS, default=None, help="Send a boost to the first matching device")
    parser.add_argument("--cancel-boost", choices=SUPPORTED_KINDS, default=None, help="Cancel a boost on the first matching device")
    parser.add_argument("--amount", type=int, default=DEFAULT_ZAPPI_BOOST_KWH, help="Boost amount: kWh for a Zappi, minutes for an Eddi")
    # The mode actions are mutually exclusive, and are the same three commands Predbat-led
    # charge control issues - --release is what a hand-back does, and is how to undo --stop-charge
    charge_group = parser.add_mutually_exclusive_group()
    charge_group.add_argument("--start-charge", action="store_true", help="Put the first Zappi in {} to charge now, as a planned window does".format(ZAPPI_MODE_CHARGING))
    charge_group.add_argument("--stop-charge", action="store_true", help="Put the first Zappi in {}, as being outside a planned window does".format(ZAPPI_MODE_STOPPED))
    charge_group.add_argument("--release", action="store_true", help="Put the first Zappi back in {}, as releasing it does".format(ZAPPI_MODE_RELEASE))
    parser.add_argument("--no-automatic", action="store_true", help="Skip the automatic configuration of car_charging_energy, car_charging_planned and iboost_energy_today")
    parser.add_argument("--raw", action="store_true", help="Print the full normalised device records")

    args = parser.parse_args()
    asyncio.run(run_myenergi_cli(args))


if __name__ == "__main__":
    main()
