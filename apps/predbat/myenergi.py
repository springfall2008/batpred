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

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import aiohttp

from component_base import ComponentBase
from oauth_mixin import OAuthMixin
from predbat_metrics import record_api_call

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

    def _not_implemented(self, what):
        """Warn once that a control is not implemented in this release, and return False."""
        if what not in self._warned_stubs:
            self._warned_stubs.add(what)
            self.log("Warn: myenergi: {} is not implemented in this release".format(what))
        return False

    async def set_mode(self, device, mode):
        """Set the device supply mode. Not implemented in this release."""
        return self._not_implemented("set_mode")

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
                    self._update_asn(response.headers)
                    if response.status == 401:
                        record_api_call("myenergi", success=False, reason="auth_error")
                        raise MyEnergiAuthError("myenergi rejected the credentials for {}".format(path))
                    if response.status != 200:
                        self.needs_asn_refresh = True
                        reason = "server_error" if response.status >= 500 else "client_error"
                        record_api_call("myenergi", success=False, reason=reason)
                        raise MyEnergiApiError("HTTP {} from {}".format(response.status, path))
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

    async def send_boost(self, device, amount, target_time=None):
        """Start a boost, choosing the manual or smart command for a Zappi."""
        if device.kind == DEVICE_KIND_ZAPPI:
            energy = int(amount)
            if target_time:
                when = str(target_time).replace(":", "")
                await self._request("/cgi-zappi-mode-Z{}-0-11-{}-{}".format(device.serial, energy, when))
            else:
                await self._request("/cgi-zappi-mode-Z{}-0-10-{}-0000".format(device.serial, energy))
        else:
            target = EDDI_BOOST_TARGETS[EDDI_DEFAULT_BOOST_TARGET]
            await self._request("/cgi-eddi-boost-E{}-10-{}-{}".format(device.serial, target, int(amount)))
        return True

    async def cancel_boost(self, device):
        """Cancel an active boost."""
        if device.kind == DEVICE_KIND_ZAPPI:
            await self._request("/cgi-zappi-mode-Z{}-0-2-0-0000".format(device.serial))
        else:
            target = EDDI_BOOST_TARGETS[EDDI_DEFAULT_BOOST_TARGET]
            await self._request("/cgi-eddi-boost-E{}-1-{}-0".format(device.serial, target))
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
        self.meta_age_seconds = CLOUD_DEVICE_LIST_MAX_AGE

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
        self.meta_age_seconds = 0

    async def connect(self):
        """Load the device list, which also validates the access token."""
        await self._refresh_device_list()
        return True

    async def fetch_devices(self):
        """Poll status for every cached Zappi and Eddi, refreshing the list when stale."""
        if not self.device_meta or self.meta_age_seconds >= CLOUD_DEVICE_LIST_MAX_AGE:
            await self._refresh_device_list()
        devices = []
        for device_id, meta in self.device_meta.items():
            status = await self._request("GET", "/devices/{}/status".format(device_id))
            if status:
                devices.append(normalise_cloud_device(status, meta))
        return devices

    async def send_boost(self, device, amount, target_time=None):
        """Start a boost, selecting the request body shape by device class."""
        if device.kind == DEVICE_KIND_ZAPPI:
            body = {"mode": "normal", "parameters": {"energy": int(amount)}}
            if target_time:
                body = {"mode": "smart", "parameters": {"energy": int(amount), "targetTime": target_time}}
        else:
            body = {"durationMinutes": int(amount)}
        await self._request("POST", "/devices/{}/boost".format(device.device_id), body=body)
        return True

    async def cancel_boost(self, device):
        """Cancel an active boost."""
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
    "boost_energy": {"friendly_name": "myenergi Boost Energy", "icon": "mdi:rocket-launch", "unit_of_measurement": "kWh", "min": BOOST_ENERGY_MIN, "max": BOOST_ENERGY_MAX, "step": 1},
    "boost_minutes": {"friendly_name": "myenergi Boost Minutes", "icon": "mdi:rocket-launch", "unit_of_measurement": "minutes", "min": BOOST_MINUTES_MIN, "max": BOOST_MINUTES_MAX, "step": 5},
    "temp_1": {"friendly_name": "myenergi Temperature 1", "icon": "mdi:thermometer", "unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    "temp_2": {"friendly_name": "myenergi Temperature 2", "icon": "mdi:thermometer", "unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
}

DEFAULT_ZAPPI_BOOST_KWH = 10
DEFAULT_EDDI_BOOST_MINUTES = 60


class MyEnergiAPI(ComponentBase, OAuthMixin):
    """myenergi component providing Zappi and Eddi monitoring and boost control."""

    def initialize(self, auth_method=None, hub_serial=None, api_key=None, key=None, token_expires_at=None, token_hash=None, automatic=True, enable_controls=True, poll_seconds=60):
        """Select a transport from the configured credentials and set up component state."""
        configured_auth_method = (auth_method or "direct").lower()
        self.hub_serial = hub_serial
        self.api_key = api_key
        self.automatic = automatic
        self.enable_controls = enable_controls
        # ComponentBase.start() calls run() on a fixed 60 second cadence, so the poll
        # interval can only be a whole number of those intervals.
        self.poll_seconds = max(60, int(round(_to_float(poll_seconds, 60) / 60.0)) * 60)

        self.devices = {}
        self.boost_amounts = {}
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
            self.transport = MyEnergiDirectTransport(self.log, hub_serial, api_key)

    def entity_prefix(self, device):
        """Return the entity name prefix for a device, e.g. predbat_myenergi_zappi_12345678."""
        return "{}_myenergi_{}_{}".format(self.prefix, device.kind, device.serial)

    def automatic_config(self):
        """Wire the device energy sensors into Predbat's load inputs.

        Zappi charging energy is subtracted from house load as car charging, so it
        goes to car_charging_energy - as a list, because minute_data_import_export
        accepts one and sums the entities. Eddi diverted energy feeds the iboost
        model instead.

        Note that these sensors are session-scoped and reset to zero when a session
        ends. get_from_incrementing() clamps negative deltas to zero so the per-minute
        subtraction is unaffected, but the iboost_today total derived in fetch.py from
        the midnight-to-now difference will under-report after a mid-day Eddi reset.
        This is a known limitation, documented in docs/components.md.
        """
        zappi_entities = []
        eddi_entity = None
        for device in sorted(self.devices.values(), key=lambda item: item.serial):
            entity = "sensor.{}_session_energy".format(self.entity_prefix(device))
            if device.kind == DEVICE_KIND_ZAPPI:
                zappi_entities.append(entity)
            elif eddi_entity is None:
                eddi_entity = entity

        if zappi_entities:
            self.log("Info: myenergi: setting car_charging_energy to {}".format(zappi_entities))
            self.set_arg_auto("car_charging_energy", zappi_entities)
        if eddi_entity:
            self.log("Info: myenergi: setting iboost_energy_today to {}".format(eddi_entity))
            self.set_arg_auto("iboost_energy_today", eddi_entity)

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
            except MyEnergiError as exc:
                self.log("Warn: myenergi: poll failed: {}".format(exc))
                return False
            if devices:
                self.devices = {device.device_id: device for device in devices}
                await self.publish_data()
                if self.automatic and not self._auto_configured:
                    self.automatic_config()
                    self._auto_configured = True
            elif first:
                self.log("Warn: myenergi: connected but no Zappi or Eddi devices were found")

        self.update_success_timestamp()
        return True

    async def publish_data(self):
        """Publish every known device as Predbat entities."""
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
