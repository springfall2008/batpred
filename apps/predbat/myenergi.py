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

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

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

# Index tables used by the direct API's numeric status fields.
ZAPPI_CHARGE_MODES = ["None", "Fast", "Eco", "Eco+", "Stopped"]
ZAPPI_STATES = ["Unknown", "Paused", "Unknown", "Charging", "Boosting", "Completed"]
EDDI_STATES = ["Unknown", "Paused", "Unknown", "Diverting", "Boosting", "Max temp reached", "Stopped"]

ZAPPI_PLUG_STATES = {
    "A": "EV Disconnected",
    "B1": "EV Connected",
    "B2": "Waiting for EV",
    "C1": "EV ready to charge",
    "C2": "Charging",
    "D1": "EV ready to charge",
    "D2": "Charging",
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
    "charging": "Charging",
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
