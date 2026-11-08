# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
A fake GivEnergy cloud for the GE Cloud API budget harness.

It stands in for aiohttp.ClientSession, so it sees every request gecloud.py makes - retries included -
at the HTTP boundary, and records each one as a CallRecord: when (simulated time), what (endpoint and
register), and why (the harness's CALL_CONTEXT plus the gecloud functions on the call stack).

Anything it does not model raises AssertionError, so a request can never silently succeed or reach
the real api.givenergy.cloud.
"""

import contextvars
import inspect
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from gecloud import (
    GE_API_ACCOUNT,
    GE_API_DEVICES,
    GE_API_EVC_DEVICES,
    GE_API_INVERTER_METER,
    GE_API_INVERTER_READ_SETTING,
    GE_API_INVERTER_SETTINGS,
    GE_API_INVERTER_STATUS,
    GE_API_INVERTER_WRITE_SETTING,
    GE_API_SITE,
    GE_API_SMART_DEVICES,
    GE_API_URL,
)

# Anonymised identities - never the serials of the account the register list was captured from
HARNESS_SERIAL = "SA0000H001"
HARNESS_DONGLE_SERIAL = "WA0000H001"
HARNESS_SITE_ID = 10001
HARNESS_ACCOUNT_ID = 1

# The 81 control registers a real GivEnergy (Gateway) site reported, as (name, kind, initial value).
# Only names, kinds and values were copied from the capture; it also holds an API key and personal
# details, which must never be copied. kind is "time", "number", "switch", "option" or "writeonly".
CAPTURED_REGISTERS = [
    ("Restart Inverter", "writeonly", False),
    ("Enable AC Charge Upper % Limit", "switch", True),
    ("Enable Eco Mode", "switch", True),
    ("DC Discharge 2 Start Time", "time", "00:00"),
    ("DC Discharge 2 End Time", "time", "00:00"),
    ("Inverter Max Output Active Power Percent", "number", 100),
    ("DC Discharge 1 Start Time", "time", "17:00"),
    ("DC Discharge 1 End Time", "time", "19:01"),
    ("Enable DC Discharge", "switch", False),
    ("AC Charge 1 Start Time", "time", "14:30"),
    ("AC Charge 1 End Time", "time", "15:00"),
    ("AC Charge Enable", "switch", False),
    ("Battery Reserve % Limit", "number", 4),
    ("Battery Charge Power", "number", 0),
    ("Battery Discharge Power", "number", 0),
    ("Battery Cutoff % Limit", "number", 4),
    ("AC Charge Upper % Limit", "number", 100),
    ("AC Charge 1 Upper SOC % Limit", "number", 100),
]
for _slot in range(2, 11):
    CAPTURED_REGISTERS += [
        ("AC Charge {} Start Time".format(_slot), "time", "00:00"),
        ("AC Charge {} End Time".format(_slot), "time", "00:00"),
        ("AC Charge {} Upper SOC % Limit".format(_slot), "number", 100),
    ]
CAPTURED_REGISTERS += [
    ("DC Discharge 1 Lower SOC % Limit", "number", 4),
    ("DC Discharge 2 Lower SOC % Limit", "number", 4),
]
for _slot in range(3, 11):
    CAPTURED_REGISTERS += [
        ("DC Discharge {} Start Time".format(_slot), "time", "00:00"),
        ("DC Discharge {} End Time".format(_slot), "time", "00:00"),
        ("DC Discharge {} Lower SOC % Limit".format(_slot), "number", 4),
    ]
CAPTURED_REGISTERS += [
    ("Export Power Priority", "option", "Load First"),
    ("Inverter Charge Power Percentage", "number", 100),
    ("Inverter Discharge Power Percentage", "number", 100),
    ("Enable EPS", "switch", False),
    ("Pause Battery", "option", "Not Paused"),
    ("Pause Battery Start Time", "time", "00:00"),
    ("Pause Battery End Time", "time", "23:59"),
    ("Force Off Grid", "switch", False),
    ("Restart Hardware", "option", "None"),
    ("Enable Calculated Load", "switch", False),
]

# Option labels for the "option" registers, in GivEnergy's "Value must be one of: (...)" form
OPTION_LABELS = {
    "Export Power Priority": ["Load First", "Battery First", "Grid First"],
    "Pause Battery": ["Not Paused", "Pause Charge", "Pause Discharge", "Pause Charge & Discharge"],
    "Restart Hardware": ["None", "Restart Inverter", "Restart Dongle"],
}

# Why calls are being made; set by the harness. A ContextVar so tasks gecloud spawns inherit it.
CALL_CONTEXT = contextvars.ContextVar("gecloud_call_context", default="unlabelled")

# gecloud.py's HTTP layer, left out of the caller chain because every call passes through it
HTTP_LAYER_FUNCTIONS = ("async_get_inverter_data", "async_get_inverter_data_retry")


@dataclass
class CallRecord:
    """One HTTP request gecloud made to the fake cloud."""

    sim_time: datetime
    scenario_id: int
    method: str
    endpoint: str
    path: str
    serial: str
    setting_id: str
    setting_name: str
    category: str
    context: str
    caller: str
    status: int
    retry: bool

    def to_dict(self):
        """The record as a JSON-friendly dict, with the simulated time as ISO text."""
        data = asdict(self)
        data["sim_time"] = self.sim_time.isoformat()
        return data


def build_register_list(registers, rate_w, rate_discharge_w=None):
    """The inverter/{serial}/settings response body: one entry per register, with ids 1..N in order."""
    rate_discharge_w = rate_w if rate_discharge_w is None else rate_discharge_w
    result = []
    for index, (name, kind, _value) in enumerate(registers):
        validation = ""
        if kind == "time":
            rules = ["date_format:H:i"]
        elif kind == "number":
            if name == "Battery Charge Power":
                rules = ["between:0,{}".format(rate_w)]
            elif name == "Battery Discharge Power":
                rules = ["between:0,{}".format(rate_discharge_w)]
            else:
                rules = ["between:0,100"]
        elif kind == "switch":
            rules = ["boolean"]
        elif kind == "option":
            labels = OPTION_LABELS[name]
            rules = ["in:{}".format(",".join(str(option) for option in range(len(labels))))]
            validation = "Value must be one of: ({})".format(", ".join(labels))
        else:
            rules = ["writeonly"]
        result.append({"id": index + 1, "name": name, "validation_rules": rules, "validation": validation})
    return result


def _template_regex(template):
    """A regex matching a GE_API_* path template, with each {placeholder} captured by name."""
    path = template.split("?")[0]
    return re.compile("^" + re.sub(r"\\{(\w+)\\}", r"(?P<\1>[^/?]+)", re.escape(path)) + "$")


# Most specific first: settings/{id}/read must not be taken for settings
ROUTES = [
    (GE_API_INVERTER_READ_SETTING, "POST"),
    (GE_API_INVERTER_WRITE_SETTING, "POST"),
    (GE_API_INVERTER_SETTINGS, "GET"),
    (GE_API_INVERTER_STATUS, "GET"),
    (GE_API_INVERTER_METER, "GET"),
    (GE_API_DEVICES, "GET"),
    (GE_API_SITE, "GET"),
    (GE_API_ACCOUNT, "GET"),
    (GE_API_EVC_DEVICES, "GET"),
    (GE_API_SMART_DEVICES, "GET"),
]
ROUTE_REGEXES = [(template, method, _template_regex(template)) for template, method in ROUTES]


class FakeResponse:
    """The aiohttp response stand-in: an async context manager with .status and async .json()."""

    def __init__(self, status, body):
        """Hold the status and JSON body."""
        self.status = status
        self.body = body

    async def __aenter__(self):
        """Enter the response context."""
        return self

    async def __aexit__(self, *args):
        """Leave the response context."""
        return False

    async def json(self):
        """The decoded JSON body."""
        return self.body


class FakeGECloud:
    """A single GivEnergy hybrid inverter on one site, behind a fake GE Cloud API that records every call."""

    def __init__(self, battery_kwh, rate_w, clock, registers=None, model="GIV-HY5.0", rate_discharge_w=None, flags=None):
        """battery_kwh, rate_w (charge) and rate_discharge_w (defaults to rate_w) describe the battery; clock returns the simulated time for each record."""
        self.battery_kwh = battery_kwh
        self.rate_w = rate_w
        self.rate_discharge_w = rate_w if rate_discharge_w is None else rate_discharge_w
        # Every GivEnergy device seen in captures and samples reports full power discharge in eco mode
        self.flags = ["full-power-discharge-in-eco-mode"] if flags is None else list(flags)
        self.clock = clock
        self.model = model
        self.registers = registers if registers is not None else CAPTURED_REGISTERS
        self.register_list = build_register_list(self.registers, rate_w, self.rate_discharge_w)
        self.register_names = {entry["id"]: entry["name"] for entry in self.register_list}
        self.register_values = {index + 1: value for index, (_name, _kind, value) in enumerate(self.registers)}
        self.soc_percent = 50.0
        self.meter_today = {"solar": 0.0, "grid": {"import": 0.0, "export": 0.0}, "battery": {"charge": 0.0, "discharge": 0.0}, "consumption": 0.0, "ac_charge": 0.0}
        self.meter_total = {"solar": 1000.0, "grid": {"import": 2000.0, "export": 500.0}, "battery": {"charge": 800.0, "discharge": 800.0}, "consumption": 2500.0, "ac_charge": 600.0}
        self.calls = []
        self.scenario_id = 0
        # Setting ids whose reads time out on the device (GivEnergy code -1), on every attempt
        self.read_timeouts = set()

    def register_count(self):
        """The number of registers gecloud reads on each sweep (write-only ones are never read)."""
        return sum(1 for entry in self.register_list if "writeonly" not in entry["validation_rules"])

    def session_factory(self):
        """A drop-in for aiohttp.ClientSession whose requests are answered and recorded by this fake."""
        fake = self

        class FakeSession:
            """The aiohttp.ClientSession stand-in."""

            def __init__(self, *args, **kwargs):
                """Accept and ignore ClientSession's arguments (timeout and so on)."""

            async def __aenter__(self):
                """Enter the session context."""
                return self

            async def __aexit__(self, *args):
                """Leave the session context."""
                return False

            def get(self, url, headers=None):
                """Answer a GET."""
                return FakeResponse(*fake.handle("GET", url, None))

            def post(self, url, headers=None, json=None):
                """Answer a POST."""
                return FakeResponse(*fake.handle("POST", url, json))

        return FakeSession

    def caller_chain(self):
        """The gecloud.py functions on the current call stack, outermost first, excluding the HTTP layer."""
        names = [frame.function for frame in inspect.stack() if frame.filename.endswith("gecloud.py") and frame.function not in HTTP_LAYER_FUNCTIONS]
        return " > ".join(reversed(names))

    def categorise(self, method, endpoint, caller=""):
        """The budget category of a request: communication-device is the device list or device info, by caller."""
        if endpoint == GE_API_DEVICES:
            return "device_info" if "async_get_device_info" in caller else "device_list"
        return {
            GE_API_ACCOUNT: "account",
            GE_API_SITE: "site",
            GE_API_INVERTER_STATUS: "status",
            GE_API_INVERTER_METER: "meter",
            GE_API_INVERTER_SETTINGS: "register_list",
            GE_API_INVERTER_READ_SETTING: "register_read",
            GE_API_INVERTER_WRITE_SETTING: "register_write",
            GE_API_EVC_DEVICES: "evc",
            GE_API_SMART_DEVICES: "smart_device",
        }[endpoint]

    def handle(self, method, url, body):
        """Route one request, record it, and return (status, JSON body)."""
        assert url.startswith(GE_API_URL), "Request to {} is outside the fake GE Cloud".format(url)
        path = url[len(GE_API_URL) :]
        bare_path = path.split("?")[0]
        for endpoint, route_method, regex in ROUTE_REGEXES:
            match = regex.match(bare_path)
            if match and route_method == method:
                break
        else:
            raise AssertionError("Fake GE Cloud does not model {} {}".format(method, path))

        params = match.groupdict()
        setting_id = params.get("setting_id", "")
        status, data = self.respond(endpoint, params, body)
        caller = self.caller_chain()
        context = CALL_CONTEXT.get()
        previous = next((record for record in reversed(self.calls) if record.method == method and record.path == path and record.context == context), None)
        retry = previous is not None and previous.status not in (200, 201)
        self.calls.append(
            CallRecord(
                sim_time=self.clock(),
                scenario_id=self.scenario_id,
                method=method,
                endpoint=endpoint,
                path=path,
                serial=params.get("inverter_serial_number", ""),
                setting_id=str(setting_id),
                setting_name=self.register_names.get(int(setting_id), "") if setting_id else "",
                category=self.categorise(method, endpoint, caller),
                context=context,
                caller=caller,
                status=status,
                retry=retry,
            )
        )
        return status, {"data": data}

    def respond(self, endpoint, params, body):
        """The (status, data) for a matched request."""
        if endpoint == GE_API_INVERTER_READ_SETTING:
            if int(params["setting_id"]) in self.read_timeouts:
                return 200, {"value": -1, "message": "Device timeout"}
            return 200, {"value": self.register_values[int(params["setting_id"])]}
        if endpoint == GE_API_INVERTER_WRITE_SETTING:
            setting_id = int(params["setting_id"])
            value = self.typed_value(setting_id, body.get("value") if body else None)
            self.register_values[setting_id] = value
            return 200, {"value": value, "success": True}
        if endpoint == GE_API_INVERTER_SETTINGS:
            return 200, self.register_list
        if endpoint == GE_API_INVERTER_STATUS:
            return 200, self.status_data()
        if endpoint == GE_API_INVERTER_METER:
            return 200, {"time": self.iso_now(), "today": self.meter_today, "total": self.meter_total, "is_metered": True}
        if endpoint == GE_API_DEVICES:
            return 200, [self.device_data()]
        if endpoint == GE_API_SITE:
            return 200, {"id": HARNESS_SITE_ID}
        if endpoint == GE_API_ACCOUNT:
            return 200, {"id": HARNESS_ACCOUNT_ID, "first_name": "Harness", "surname": "User", "timezone": "GMT", "standard_timezone": "Europe/London", "flags": []}
        return 200, []

    def typed_value(self, setting_id, value):
        """A written value in its register's own type: gecloud sends every value as a string, but GE Cloud reads numbers and switches back typed."""
        kind = self.registers[setting_id - 1][1]
        if kind == "number":
            number = float(value)
            return int(number) if number.is_integer() else number
        if kind in ("switch", "writeonly"):
            return str(value).lower() in ("true", "on", "1")
        return value

    def iso_now(self):
        """The simulated time in GE Cloud's ISO form."""
        return self.clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def status_data(self):
        """The system-data/latest body, carrying the current SOC."""
        return {
            "time": self.iso_now(),
            "status": "Normal",
            "solar": {"power": 0, "arrays": []},
            "grid": {"voltage": 240.0, "current": 0.0, "power": 0, "frequency": 50.0},
            "battery": {"percent": self.soc_percent, "power": 0, "temperature": 20},
            "inverter": {"temperature": 30.0, "power": 0, "output_voltage": 240.0, "output_frequency": 50.0, "eps_power": 0},
            "consumption": 0,
        }

    def device_data(self):
        """The communication-device entry. last_updated is the real time: gecloud skips devices more than 5 real days stale."""
        real_now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        capacity_ah = round(self.battery_kwh * 1000 / 51.2, 2)
        return {
            "serial_number": HARNESS_DONGLE_SERIAL,
            "firmware_version": 1,
            "type": "WIFI",
            "commission_date": "2024-01-01T00:00:00Z",
            "inverter": {
                "serial": HARNESS_SERIAL,
                "status": "NORMAL",
                "last_online": real_now,
                "last_updated": real_now,
                "commission_date": "2024-01-01T00:00:00Z",
                "info": {
                    "battery_type": "LITHIUM",
                    "battery": {"nominal_capacity": capacity_ah, "nominal_voltage": 51.2, "depth_of_discharge": 1},
                    "model": self.model,
                    "max_charge_rate": self.rate_w,
                    "max_discharge_rate": self.rate_discharge_w,
                },
                "warranty": {"type": "Standard", "expiry_date": "2034-01-01T00:00:00Z"},
                "firmware_version": {"ARM": 616, "DSP": 616},
                "connections": {"batteries": [{"module_number": 1, "serial": "BA0000H001", "firmware_version": 1, "capacity": {"full": capacity_ah, "design": capacity_ah}, "cell_count": 16, "has_usb": True, "nominal_voltage": 51.2}], "meters": []},
                "flags": list(self.flags),
            },
            "site_id": HARNESS_SITE_ID,
        }
