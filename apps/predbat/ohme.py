# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Ohme API library.
# Credits to: https://github.com/dan-r/ohmepy for the original code.
# -----------------------------------------------------------------------------

import asyncio
import json
from enum import Enum
from typing import Any, Optional, Self, Mapping
from dataclasses import dataclass
import datetime
import aiohttp
import traceback
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Union
from datetime import timedelta, timezone
from config import TIME_FORMAT_HA

GOOGLE_API_KEY = "AIzaSyC8ZeZngm33tpOXLpbXeKfwtyZ1WrkbdBY"  # cspell:disable-line
VERSION = "1.5.1"


JsonValueType = Union[Dict[str, "JsonValueType"], List["JsonValueType"], str, int, float, bool, None]

# Ohme attribute table for Home Assistant entities
ohme_attribute_table = {
    "mode": {"friendly_name": "Ohme Charge Mode", "icon": "mdi:ev-station"},
    "status": {"friendly_name": "Ohme Charger Status", "icon": "mdi:ev-station"},
    "power_watts": {"friendly_name": "Ohme Power", "icon": "mdi:lightning-bolt", "unit_of_measurement": "W", "device_class": "power"},
    "power_amps": {"friendly_name": "Ohme Current", "icon": "mdi:current-ac", "unit_of_measurement": "A", "device_class": "current"},
    "power_volts": {"friendly_name": "Ohme Voltage", "icon": "mdi:sine-wave", "unit_of_measurement": "V", "device_class": "voltage"},
    "ct_amps": {"friendly_name": "Ohme CT Clamp Current", "icon": "mdi:current-ac", "unit_of_measurement": "A", "device_class": "current"},
    "max_charge": {"friendly_name": "Ohme Max Charge Enabled", "icon": "mdi:battery-charging-100"},
    "available": {"friendly_name": "Ohme Available", "icon": "mdi:connection"},
    "target_soc": {"friendly_name": "Ohme Target SOC", "icon": "mdi:battery-charging", "unit_of_measurement": "%", "device_class": "battery", "min": 0, "max": 100, "step": 1},
    "target_time": {"friendly_name": "Ohme Target Time", "icon": "mdi:clock-outline"},
    "preconditioning": {"friendly_name": "Ohme Preconditioning", "icon": "mdi:air-conditioner", "unit_of_measurement": "mins", "min": 0, "max": 60, "step": 5},
    "slots": {"friendly_name": "Ohme Charge Slots", "icon": "mdi:calendar-clock"},
    "energy": {"friendly_name": "Ohme Session Energy", "icon": "mdi:lightning-bolt", "unit_of_measurement": "Wh", "device_class": "energy"},
    "battery_percent": {"friendly_name": "Ohme Battery Percent", "icon": "mdi:battery", "unit_of_measurement": "%", "device_class": "battery"},
    "current_vehicle": {"friendly_name": "Ohme Current Vehicle", "icon": "mdi:car"},
    "approve_charge": {"friendly_name": "Ohme Approve Charge", "icon": "mdi:check-circle-outline"},
}

BASE_TIME = datetime.datetime.strptime("00:00", "%H:%M")
OPTIONS_TIME = [((BASE_TIME + timedelta(seconds=minute * 60)).strftime("%H:%M")) for minute in range(0, 24 * 60, 1)]


def time_next_occurs(hour: int, minute: int) -> datetime.datetime:
    """Find when this time next occurs."""
    current = datetime.datetime.now()
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= datetime.datetime.now():
        target = target + datetime.timedelta(days=1)

    return target


@dataclass
class ChargeSlot:
    """Dataclass for reporting an individual charge slot."""

    start: datetime.datetime
    end: datetime.datetime
    energy: float

    def __str__(self):
        return f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')}"

    def to_dict(self) -> dict[str, JsonValueType]:
        """Convert to a JSON-serializable dictionary."""
        return {
            "start": str(self.start.isoformat()),
            "end": str(self.end.isoformat()),
            "energy": float(self.energy),
        }


def slot_list(data: Dict[str, Any]) -> List[ChargeSlot]:
    """Get list of charge slots with energy delta summed for merged slots."""
    session_slots = data.get("allSessionSlots", [])
    if not session_slots:
        return []

    slots: List[ChargeSlot] = []

    for slot in session_slots:
        start_time = datetime.datetime.fromtimestamp(slot["startTimeMs"] / 1000).replace(microsecond=0).astimezone()
        end_time = datetime.datetime.fromtimestamp(slot["endTimeMs"] / 1000).replace(microsecond=0).astimezone()

        hours = (end_time - start_time).total_seconds() / 3600
        energy = round((slot["watts"] * hours) / 1000, 2)

        slots.append(ChargeSlot(start_time, end_time, energy))

    # Merge adjacent slots
    merged_slots: List[ChargeSlot] = []
    for slot in slots:
        if merged_slots and merged_slots[-1].end == slot.start:
            # Merge slot by extending the end time and summing energy
            merged_slots[-1] = ChargeSlot(
                merged_slots[-1].start,
                slot.end,
                merged_slots[-1].energy + slot.energy,
            )
        else:
            merged_slots.append(slot)

    return merged_slots


def vehicle_to_name(vehicle: Dict[str, Any]) -> str:
    """Translate vehicle object to human readable name."""
    if vehicle.get("name") is not None:
        return vehicle["name"]

    model: Dict[str, Any] = vehicle.get("model") or {}
    brand: Dict[str, Any] = model.get("brand") or {}

    brand_name = brand.get("name") or model.get("make") or "Unknown"
    model_name = model.get("modelName") or "Unknown"
    year_from = model.get("availableFromYear")
    year_to = model.get("availableToYear") or ""

    if year_from is None:
        return f"{brand_name} {model_name}"

    return f"{brand_name} {model_name} ({year_from}-{year_to})"


class ChargerStatus(Enum):
    """Charger state enum."""

    UNPLUGGED = "unplugged"
    PENDING_APPROVAL = "pending_approval"
    CHARGING = "charging"
    PLUGGED_IN = "plugged_in"
    PAUSED = "paused"
    FINISHED = "finished"


class ChargerMode(Enum):
    """Charger mode enum."""

    SMART_CHARGE = "smart_charge"
    MAX_CHARGE = "max_charge"
    PAUSED = "paused"


@dataclass
class ChargerPower:
    """Dataclass for reporting power status of charger."""

    watts: float
    amps: float
    volts: int | None
    ct_amps: float


class OhmeAPI:
    """Ohme API exception."""

    def __init__(self, email, password, ohme_automatic_octopus_intelligent, base):
        self.email = email
        self.base = base
        self.log = base.log
        self.password = password
        self.client = OhmeApiClient(email, password, self.log)
        self.api_started = False
        self.stop_api = False
        self.count_errors = 0
        self.queued_events = []
        self.ohme_automatic_octopus_intelligent = ohme_automatic_octopus_intelligent

    def wait_api_started(self):
        """
        Wait for the API to start
        """
        self.log("Ohme API: Waiting for API to start")
        count = 0
        while not self.api_started and count < 240:
            time.sleep(1)
            count += 1
        if not self.api_started:
            self.log("Warn: Ohme API: Failed to start")
            return False
        return True

    def is_alive(self):
        """
        Check if the API is alive
        """
        return self.api_started

    def last_updated_time(self):
        """
        Get the last successful update time
        """
        if self.client:
            return self.client.last_success_timestamp
        return None

    async def start(self):
        """
        Main run loop
        """

        first = True
        count_seconds = 0
        while not self.stop_api:
            try:
                if not first and self.queued_events:
                    while self.queued_events:
                        event = self.queued_events.pop(0)
                        handler, *args = event
                        try:
                            await handler(*args)
                        except ApiException as e:
                            self.log("Warn: Ohme API: Event handler error: {}".format(e))
                    first = True  # Force an immediate update after handling events

                if first or (count_seconds % (30 * 60)) == 0:
                    await self.client.async_update_device_info()
                    await self.client.async_get_advanced_settings()

                if first or (count_seconds % (120)) == 0:
                    await self.client.async_get_charge_session()
                    await self.publish_data()

                if not self.api_started:
                    print("Ohme API: Started")
                    self.api_started = True

                    if self.ohme_automatic_octopus_intelligent and self.client.serial:
                        await self.automatic_config_octopus_intelligent()

                first = False

            except Exception as e:
                self.log("Error: Ohme API: {}".format(e))
                self.log("Error: " + traceback.format_exc())

            await asyncio.sleep(1)
            count_seconds += 1

        await self.client.close()
        print("Ohme API: Stopped")

    async def stop(self):
        self.stop_api = True

    async def automatic_config_octopus_intelligent(self):
        """
        Automatically set the predbat entities to use ohme via octopus
        """
        self.log("Info: Ohme API: Setting Predbat to use Ohme")
        self.base.args["octopus_intelligent_slot"] = "binary_sensor.predbat_ohme_slot_active"
        self.base.args["octopus_ready_time"] = "select.predbat_ohme_target_time"
        self.base.args["octopus_charge_limit"] = "number.predbat_ohme_target_percent"

    async def publish_data(self):
        """
        Publish data to HA using dashboard_item
        """
        mode = self.client.mode
        status = self.client.status
        power = self.client.power
        max_charge = self.client.max_charge
        available = self.client.available
        target_soc = self.client.target_soc
        target_time = self.client.target_time
        preconditioning = self.client.preconditioning
        slots = self.client.slots
        energy = self.client.energy
        battery = self.client.battery
        vehicle = self.client.current_vehicle

        # self.log("Info: Ohme API: Mode: %s, Status: %s, Power: %sW, %sA, %sV, CT: %sA, Max Charge: %s, Available: %s, Target SOC: %s%%, Target Time: %s, Preconditioning: %s mins, Vehicle: %s, Slots: %s" % (
        #         mode, status, power.watts, power.amps, power.volts, power.ct_amps, max_charge, available, target_soc,
        #         target_time, preconditioning, vehicle, slots)
        #        )

        # Create entity name prefix
        entity_name_sensor = "sensor.predbat_ohme"
        entity_name_number = "number.predbat_ohme"
        entity_name_select = "select.predbat_ohme"
        entity_name_switch = "switch.predbat_ohme"
        entity_name_binary_sensor = "binary_sensor.predbat_ohme"

        # Publish mode and status
        if mode is None:
            mode = "disconnected"
        else:
            mode = str(mode.value)
        self.base.dashboard_item(entity_name_sensor + "_mode", state=mode, attributes=ohme_attribute_table.get("mode", {}), app="ohme")

        if status is None:
            status = "unknown"
        else:
            status = str(status.value)
        self.base.dashboard_item(entity_name_sensor + "_status", state=status, attributes=ohme_attribute_table.get("status", {}), app="ohme")

        # Publish power data
        if power:
            self.base.dashboard_item(entity_name_sensor + "_power_watts", state=power.watts, attributes=ohme_attribute_table.get("power_watts", {}), app="ohme")
            self.base.dashboard_item(entity_name_sensor + "_power_amps", state=power.amps, attributes=ohme_attribute_table.get("power_amps", {}), app="ohme")
            self.base.dashboard_item(entity_name_sensor + "_power_volts", state=power.volts, attributes=ohme_attribute_table.get("power_volts", {}), app="ohme")
            self.base.dashboard_item(entity_name_sensor + "_ct_amps", state=power.ct_amps, attributes=ohme_attribute_table.get("ct_amps", {}), app="ohme")

        # Publish boolean states
        self.base.dashboard_item(entity_name_switch + "_max_charge", state=max_charge, attributes=ohme_attribute_table.get("max_charge", {}), app="ohme")
        self.base.dashboard_item(entity_name_binary_sensor + "_available", state="on" if available else "off", attributes=ohme_attribute_table.get("available", {}), app="ohme")

        # Publish target data
        self.base.dashboard_item(entity_name_number + "_target_percent", state=target_soc, attributes=ohme_attribute_table.get("target_soc", {}), app="ohme")

        # Target time
        target_time_str = "00:00"
        if target_time and len(target_time) == 2:
            target_time_str = f"{target_time[0]:02d}:{target_time[1]:02d}"
        target_attributes = ohme_attribute_table.get("target_time", {})
        target_attributes["options"] = OPTIONS_TIME
        self.base.dashboard_item(entity_name_select + "_target_time", state=target_time_str, attributes=target_attributes, app="ohme")

        # Publish preconditioning
        self.base.dashboard_item(entity_name_number + "_preconditioning", state=preconditioning, attributes=ohme_attribute_table.get("preconditioning", {}), app="ohme")

        # Publish slot information
        num_slots = len(slots) if slots else 0
        slot_attributes = ohme_attribute_table.get("slots", {}).copy()

        planned_dispatches = []
        completed_dispatches = []
        slot_active = False
        for slot in slots:
            start = slot.start
            end = slot.end
            energy = slot.energy
            is_completed = False
            if end < datetime.datetime.now().astimezone():
                is_completed = True
            if start <= datetime.datetime.now().astimezone() <= end:
                slot_active = True
            dispatch = {"start": start.strftime(TIME_FORMAT_HA), "end": end.strftime(TIME_FORMAT_HA), "energy": -energy, "location": "AT_HOME"}
            if is_completed:
                completed_dispatches.append(dispatch)
            else:
                planned_dispatches.append(dispatch)

        if slots:
            slot_attributes["planned_dispatches"] = planned_dispatches
            slot_attributes["completed_dispatches"] = completed_dispatches
        self.base.dashboard_item(entity_name_binary_sensor + "_slot_active", state=slot_active, attributes=slot_attributes, app="ohme")

        # Publish energy and battery data
        self.base.dashboard_item(entity_name_sensor + "_energy", state=energy, attributes=ohme_attribute_table.get("energy", {}), app="ohme")
        self.base.dashboard_item(entity_name_sensor + "_battery_percent", state=battery, attributes=ohme_attribute_table.get("battery_percent", {}), app="ohme")
        self.base.dashboard_item(entity_name_sensor + "_current_vehicle", state=vehicle, attributes=ohme_attribute_table.get("current_vehicle", {}), app="ohme")

        # Approve charge switch
        self.base.dashboard_item(entity_name_switch + "_approve_charge", state="off", attributes=ohme_attribute_table.get("approve_charge", {}), app="ohme")

    # Event stubs to queue for main thread
    async def select_event(self, entity_id, value):
        self.queued_events.append((self.select_event_handler, entity_id, value))

    async def number_event(self, entity_id, value):
        self.queued_events.append((self.number_event_handler, entity_id, value))

    async def switch_event(self, entity_id, service):
        self.queued_events.append((self.switch_event_handler, entity_id, service))

    # event handlers
    async def select_event_handler(self, entity_id, value):
        """
        Select event
        """
        if entity_id.endswith("_target_time"):
            if value in OPTIONS_TIME:
                hour, minute = map(int, value.split(":"))
                await self.client.async_apply_session_rule(target_time=(hour, minute))
                self.log(f"Info: Ohme API: Set target time to {hour:02d}:{minute:02d}")
            else:
                self.log(f"Warn: Ohme API: Invalid target time value: {value}")

    async def number_event_handler(self, entity_id, value):
        """
        Number event
        """
        if entity_id.endswith("_target_soc"):
            if (isinstance(value, float) or isinstance(value, int)) and 0 <= value <= 100:
                await self.client.async_apply_session_rule(target_percent=int(value))
            else:
                self.log(f"Warn: Ohme API: Invalid target SOC value: {value}")
        elif entity_id.endswith("_preconditioning"):
            try:
                value = int(value)
            except (ValueError, TypeError):
                self.log(f"Warn: Ohme API: Invalid preconditioning value: {value}")
                return
            if value == 0:
                self.log(f"Info: Ohme API: Set preconditioning to off")
                await self.client.async_apply_session_rule(pre_condition=True)
            else:
                self.log(f"Info: Ohme API: Set preconditioning length to {int(value)} mins")
                await self.client.async_apply_session_rule(pre_condition=True, pre_condition_length=int(value))

    async def switch_event_handler(self, entity_id, service):
        """
        Switch event
        """
        if entity_id.endswith("_max_charge"):
            if service == "turn_on":
                await self.client.async_max_charge(True)
            elif service == "turn_off":
                await self.client.async_max_charge(False)
        elif entity_id.endswith("_approve_charge"):
            if service == "turn_on":
                if self.client.status != ChargerStatus.PENDING_APPROVAL:
                    self.log("Warn: Ohme API: Charger not pending approval")
                    return
                await self.client.async_approve_charge()
                self.log("Info: Ohme API: Approved charge")


class OhmeApiClient:
    """API client for Ohme EV chargers."""

    def __init__(self, email: str, password: str, log):
        if email is None or password is None:
            raise AuthException("Credentials not provided")

        # Credentials from configuration
        self.email = email
        self._password = password
        self.log = log

        # Charger and its capabilities
        self.device_info: dict[str, Any] = {}
        self._charge_session: dict[str, Any] = {}
        self._advanced_settings: dict[str, Any] = {}
        self._next_session: dict[str, Any] = {}
        self._cars: list[Any] = []

        self.energy: float = 0.0
        self.battery: int = 0

        self._capabilities: dict[str, bool | str | list[str]] = {}
        self._configuration: dict[str, bool | str] = {}
        self.ct_connected: bool = False
        self.cap_available: bool = True
        self.cap_enabled: bool = False
        self.solar_capable: bool = False

        # Authentication
        self._token_birth: float = 0.0
        self._token: str | None = None
        self._refresh_token: str | None = None

        # User info
        self.serial = ""

        # Sessions
        self._session = None
        self._close_session = False
        self._timeout = 10
        self._last_rule: dict[str, Any] = {}
        self.last_success_timestamp = None

    # Auth methods

    async def async_login(self) -> bool:
        """Refresh the user auth token from the stored credentials."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._close_session = True

        async with asyncio.timeout(self._timeout):
            async with self._session.post(
                f"https://www.googleapis.com/identitytoolkit/v3/relyingparty/verifyPassword?key={GOOGLE_API_KEY}",
                data={
                    "email": self.email,
                    "password": self._password,
                    "returnSecureToken": True,
                },
            ) as resp:
                if resp.status != 200:
                    raise AuthException("Incorrect credentials")

                resp_json = await resp.json()
                self._token_birth = time.time()
                self._token = resp_json["idToken"]
                self._refresh_token = resp_json["refreshToken"]
                return True
        raise AuthException("Incorrect credentials")

    async def _async_refresh_session(self) -> bool:
        """Refresh auth token if needed."""
        if self._token is None:
            return await self.async_login()

        # Don't refresh token unless its over 45 mins old
        if time.time() - self._token_birth < 2700:
            return True

        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._close_session = True

        async with asyncio.timeout(self._timeout):
            async with self._session.post(
                f"https://securetoken.googleapis.com/v1/token?key={GOOGLE_API_KEY}",
                data={
                    "grantType": "refresh_token",
                    "refreshToken": self._refresh_token,
                },
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    msg = f"Warn: Ohme auth refresh: {text}"
                    self.log(msg)
                    raise AuthException(msg)

                resp_json = await resp.json()
                self._token_birth = time.time()
                self._token = resp_json["id_token"]
                self._refresh_token = resp_json["refresh_token"]
                return True

    # Internal methods

    async def _handle_api_error(self, url: str, resp: aiohttp.ClientResponse):
        """Raise an exception if API response failed."""
        if resp.status != 200:
            text = await resp.text()
            msg = f"Warn:Ohme API response error: {url}, {resp.status}; {text}"
            self.log(msg)
            raise ApiException(msg)

    async def _make_request(
        self,
        method: str,
        url: str,
        data: Optional[Mapping[str, str | bool]] = None,
        skip_json: bool = False,
    ):
        """Make an HTTP request."""
        await self._async_refresh_session()

        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._close_session = True

        async with asyncio.timeout(self._timeout):
            async with self._session.request(
                method=method,
                url=f"https://api.ohme.io{url}",
                data=json.dumps(data) if data and method in {"PUT", "POST"} else data,
                headers={
                    "Authorization": f"Firebase {self._token}",
                    "Content-Type": "application/json",
                    "User-Agent": f"ohmepy/{VERSION}",
                },
            ) as resp:
                # self.log("Info: %s request to %s, status code %s" % (method, url, resp.status))
                await self._handle_api_error(url, resp)

                if skip_json and method == "POST":
                    return await resp.text()

                return await resp.json() if method != "PUT" else True

    def _charge_in_progress(self) -> bool:
        """Is a charge in progress? Used to determine if schedule or session should be adjusted."""
        return self.status is not ChargerStatus.UNPLUGGED and self.status is not ChargerStatus.PENDING_APPROVAL

    # Simple getters

    def is_capable(self, capability: str) -> bool:
        """Return whether or not this model has a given capability."""
        return bool(self._capabilities[capability])

    def configuration_value(self, value: str) -> bool:
        """Return a boolean configuration value."""
        return bool(self._configuration.get(value))

    @property
    def status(self) -> ChargerStatus:
        """Return status from enum."""
        if self._charge_session["mode"] == "PENDING_APPROVAL":
            return ChargerStatus.PENDING_APPROVAL
        elif self._charge_session["mode"] == "DISCONNECTED":
            return ChargerStatus.UNPLUGGED
        elif self._charge_session["mode"] == "STOPPED":
            return ChargerStatus.PAUSED
        elif self._charge_session["mode"] == "FINISHED_CHARGE":
            return ChargerStatus.FINISHED
        elif self._charge_session.get("power") and self._charge_session["power"].get("watt", 0) > 0:
            return ChargerStatus.CHARGING
        else:
            return ChargerStatus.PLUGGED_IN

    @property
    def mode(self) -> Optional[ChargerMode]:
        """Return status from enum."""
        if self._charge_session["mode"] == "SMART_CHARGE":
            return ChargerMode.SMART_CHARGE
        elif self._charge_session["mode"] == "MAX_CHARGE":
            return ChargerMode.MAX_CHARGE
        elif self._charge_session["mode"] == "STOPPED":
            return ChargerMode.PAUSED

        return None

    @property
    def max_charge(self) -> bool:
        """Get if max charge is enabled."""
        return self._charge_session.get("mode") == "MAX_CHARGE"

    @property
    def available(self) -> bool:
        """CT reading."""
        return self._advanced_settings.get("online", False)

    @property
    def power(self) -> ChargerPower:
        """Return all power readings."""

        charge_power = self._charge_session.get("power") or {}
        return ChargerPower(
            watts=charge_power.get("watt", 0),
            amps=charge_power.get("amp", 0),
            volts=charge_power.get("volt", None),
            ct_amps=self._advanced_settings.get("clampAmps", 0),
        )

    @property
    def target_soc(self) -> int:
        """Target state of charge."""
        if self.status is ChargerStatus.PAUSED and self._charge_session.get("suspendedRule") is not None:
            return self._charge_session.get("suspendedRule", {}).get("targetPercent", 0)
        elif self._charge_in_progress():
            return int(self._charge_session["appliedRule"]["targetPercent"])

        return int(self._next_session.get("targetPercent", 0))

    @property
    def target_time(self) -> tuple[int, int]:
        """Target state of charge."""
        if self._charge_in_progress():
            target = int(self._charge_session["appliedRule"]["targetTime"])
        else:
            target = int(self._next_session.get("targetTime", 0))

        return (target // 3600, (target % 3600) // 60)

    @property
    def preconditioning(self) -> int:
        """Preconditioning time."""
        if self._charge_in_progress():
            if self._last_rule.get("preconditioningEnabled"):
                return int(self._last_rule.get("preconditionLengthMins", 0))
        else:
            if self._next_session.get("preconditioningEnabled"):
                return int(self._next_session.get("preconditionLengthMins", 0))

        return 0

    @property
    def slots(self) -> list[ChargeSlot]:
        """Slot list."""
        return slot_list(self._charge_session)

    @property
    def next_slot_start(self) -> datetime.datetime | None:
        """Next slot start."""
        return min(
            (slot.start for slot in self.slots if slot.start > datetime.datetime.now().astimezone()),
            default=None,
        )

    @property
    def next_slot_end(self) -> datetime.datetime | None:
        """Next slot start."""
        return min(
            (slot.end for slot in self.slots if slot.end > datetime.datetime.now().astimezone()),
            default=None,
        )

    @property
    def vehicles(self) -> list[str]:
        """Return a list of vehicle names."""
        output = []
        for vehicle in self._cars:
            output.append(vehicle_to_name(vehicle))
        return output

    @property
    def current_vehicle(self) -> Optional[str]:
        """Returns the name of the currently selected vehicle."""
        # The selected vehicle is the first one in this list
        if len(self._cars) > 0:
            return vehicle_to_name(self._cars[0])
        return None

    # Push methods

    async def async_pause_charge(self) -> bool:
        """Pause an ongoing charge"""
        result = await self._make_request("POST", f"/v1/chargeSessions/{self.serial}/stop", skip_json=True)
        return bool(result)

    async def async_resume_charge(self) -> bool:
        """Resume a paused charge"""
        result = await self._make_request("POST", f"/v1/chargeSessions/{self.serial}/resume", skip_json=True)
        return bool(result)

    async def async_approve_charge(self) -> bool:
        """Approve a charge"""
        result = await self._make_request("PUT", f"/v1/chargeSessions/{self.serial}/approve?approve=true")
        return bool(result)

    async def async_max_charge(self, state: bool = True) -> bool:
        """Enable max charge"""
        result = await self._make_request(
            "PUT",
            f"/v1/chargeSessions/{self.serial}/rule?maxCharge=" + str(state).lower(),
        )
        return bool(result)

    async def async_set_mode(self, mode: ChargerMode | str) -> None:
        """Set charger mode."""
        if isinstance(mode, str):
            mode = ChargerMode(mode)

        if mode is ChargerMode.MAX_CHARGE:
            await self.async_max_charge(True)
        elif mode is ChargerMode.SMART_CHARGE:
            await self.async_max_charge(False)
        elif mode is ChargerMode.PAUSED:
            await self.async_pause_charge()

    async def async_apply_session_rule(
        self,
        max_price: Optional[float] = None,
        target_time: Optional[tuple[int, int]] = None,
        target_percent: Optional[int] = None,
        pre_condition: Optional[bool] = None,
        pre_condition_length: Optional[int] = None,
    ) -> bool:
        """Apply rule to ongoing charge/stop max charge."""
        # Check every property. If we've provided it, use that. If not, use the existing.
        if max_price is None:
            if "settings" in self._last_rule and self._last_rule["settings"] is not None and len(self._last_rule["settings"]) > 1:
                max_price = self._last_rule["settings"][0]["enabled"]
            else:
                max_price = False

        if target_percent is None:
            target_percent = self._last_rule["targetPercent"] if "targetPercent" in self._last_rule else 80

        if pre_condition is None:
            pre_condition = self._last_rule["preconditioningEnabled"] if "preconditioningEnabled" in self._last_rule else False

        if not pre_condition_length:
            pre_condition_length = self._last_rule["preconditionLengthMins"] if ("preconditionLengthMins" in self._last_rule and self._last_rule["preconditionLengthMins"] is not None) else 30

        if target_time is None:
            # Default to 9am
            target_time_cache = self._last_rule["targetTime"] if "targetTime" in self._last_rule else 32400
            target_time = (target_time_cache // 3600, (target_time_cache % 3600) // 60)

        target_ts = int(time_next_occurs(target_time[0], target_time[1]).timestamp() * 1000)

        # Convert these to string form
        max_price_str = "true" if max_price else "false"
        pre_condition_str = "true" if pre_condition else "false"

        result = await self._make_request(
            "PUT",
            f"/v1/chargeSessions/{self.serial}/rule?enableMaxPrice={max_price_str}&targetTs={target_ts}&enablePreconditioning={pre_condition_str}&toPercent={target_percent}&preconditionLengthMins={pre_condition_length}",
        )
        return bool(result)

    async def async_change_price_cap(self, enabled: Optional[bool] = None, cap: Optional[float] = None) -> bool:
        """Change price cap settings."""
        settings = await self._make_request("GET", "/v1/users/me/settings")
        if enabled is not None:
            settings["chargeSettings"][0]["enabled"] = enabled

        if cap is not None:
            settings["chargeSettings"][0]["value"] = cap

        result = await self._make_request("PUT", "/v1/users/me/settings", data=settings)
        return bool(result)

    async def async_update_schedule(
        self,
        target_percent: Optional[int] = None,
        target_time: Optional[tuple[int, int]] = None,
        pre_condition: Optional[bool] = None,
        pre_condition_length: Optional[int] = None,
    ) -> bool:
        """Update the schedule for the next charge."""
        rule = self._next_session

        # Account for user having no rules
        if not rule:
            return False

        # Update percent and time if provided
        if target_percent is not None:
            rule["targetPercent"] = target_percent
        if target_time is not None:
            rule["targetTime"] = (target_time[0] * 3600) + (target_time[1] * 60)

        # Update pre-conditioning if provided
        if pre_condition is not None:
            rule["preconditioningEnabled"] = pre_condition
        if pre_condition_length:
            rule["preconditionLengthMins"] = pre_condition_length

        await self._make_request("PUT", f"/v1/chargeRules/{rule['id']}", data=rule)
        return True

    async def async_set_target(
        self,
        target_percent: Optional[int] = None,
        target_time: Optional[tuple[int, int]] = None,
        pre_condition_length: Optional[int] = None,
    ) -> bool:
        """Set a target time/percentage."""
        pre_condition: Optional[bool] = None
        if pre_condition_length is not None:
            pre_condition = bool(pre_condition_length)

        if self._charge_in_progress():
            await self.async_apply_session_rule(
                target_time=target_time,
                target_percent=target_percent,
                pre_condition=pre_condition,
                pre_condition_length=pre_condition_length,
            )
        else:
            await self.async_update_schedule(
                target_time=target_time,
                target_percent=target_percent,
                pre_condition=pre_condition,
                pre_condition_length=pre_condition_length,
            )
        return True

    async def async_set_configuration_value(self, values: Mapping[str, bool]) -> bool:
        """Set a configuration value or values."""
        result = await self._make_request("PUT", f"/v1/chargeDevices/{self.serial}/appSettings", data=values)
        await asyncio.sleep(1)  # The API is slow to update after this request

        return bool(result)

    async def async_set_vehicle(self, selected_name: str) -> bool:
        """Set the vehicle to be charged."""
        for vehicle in self._cars:
            if vehicle_to_name(vehicle) == selected_name:
                result = await self._make_request("PUT", f"/v1/car/{vehicle['id']}/select")

                return True
        return False

    # Pull methods

    async def async_get_charge_session(self) -> None:
        """Fetch charge sessions endpoint."""
        # Retry if state is CALCULATING or DELIVERING
        for attempt in range(3):
            resp = await self._make_request("GET", "/v1/chargeSessions")
            resp = resp[0]

            if resp.get("mode") != "CALCULATING" and resp.get("mode") != "DELIVERING":
                self.last_success_timestamp = datetime.datetime.now(timezone.utc)
                break

            if attempt < 2:  # Only sleep if there are more retries left
                await asyncio.sleep(1)

        self._charge_session = resp

        # Store last rule
        if resp["mode"] == "SMART_CHARGE" and "appliedRule" in resp:
            self._last_rule = resp["appliedRule"]

        # Get energy reading
        if self._charge_in_progress() and resp.get("batterySoc") is not None:
            self.energy = max(0, self.energy, resp["batterySoc"].get("wh") or 0)
        else:
            self.energy = 0

        self.battery = ((resp.get("car") or {}).get("batterySoc") or {}).get("percent") or (resp.get("batterySoc") or {}).get("percent") or 0

        resp = await self._make_request("GET", "/v1/chargeSessions/nextSessionInfo")
        self._next_session = resp.get("rule", {})

    async def async_get_advanced_settings(self) -> None:
        """Get advanced settings (mainly for CT clamp reading)"""
        resp = await self._make_request("GET", f"/v1/chargeDevices/{self.serial}/advancedSettings")

        self._advanced_settings = resp

        # clampConnected is not reliable, so check clampAmps being > 0 as an alternative
        if resp["clampConnected"] or (isinstance(resp.get("clampAmps"), float) and resp.get("clampAmps") > 0):
            self.ct_connected = True

    async def async_update_device_info(self) -> bool:
        """Update _device_info with our charger model."""
        resp = await self._make_request("GET", "/v1/users/me/account")
        self._cars = resp.get("cars") or []

        try:
            self.cap_enabled = resp["userSettings"]["chargeSettings"][0]["enabled"]
        except:
            pass

        device = resp["chargeDevices"][0]

        self._capabilities = device["modelCapabilities"]
        self._configuration = device["optionalSettings"]
        self.serial = device["id"]

        self.device_info = {
            "name": device["modelTypeDisplayName"],
            "model": device["modelTypeDisplayName"].replace("Ohme ", ""),
            "sw_version": device["firmwareVersionLabel"],
        }

        if resp["tariff"] is not None and resp["tariff"]["dsrTariff"]:
            self.cap_available = False

        solar_modes = device["modelCapabilities"]["solarModes"]
        if isinstance(solar_modes, list) and len(solar_modes) == 1:
            self.solar_capable = True

        return True

    async def close(self) -> None:
        """Close open client session."""
        if self._session and self._close_session:
            await self._session.close()

    async def __aenter__(self) -> Self:
        """Async enter."""
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        """Async exit."""
        await self.close()


# Exceptions
class ApiException(Exception):
    ...


class AuthException(ApiException):
    ...
