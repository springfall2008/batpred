# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Growatt public OAuth OpenAPI component.

The public API supplies one-minute telemetry and UTC charge/discharge time slots. It does
not expose writable battery SoC limits. Predbat therefore keeps its target and reserve in
Home Assistant and removes a slot when the latest telemetry reaches that threshold. That
is deliberately visible in the logs and documentation because it is software enforcement,
not a limit stored in the inverter.
"""

# cspell:ignore echarge edischarge

import asyncio
import json
import time
import uuid
from datetime import datetime

import aiohttp

from component_base import ComponentBase
from coordinator import inverter_record
from oauth_mixin import OAuthMixin
from growatt_const import (
    GROWATT_BASE_URL,
    GROWATT_ENDPOINTS,
    GROWATT_RETURN_CODES,
    GROWATT_CODE_OK,
    GROWATT_CODE_TOKEN_INVALID,
    GROWATT_TIMEOUT,
    GROWATT_RETRIES,
    GROWATT_TTL_STATIC,
    GROWATT_TTL_LIVE,
    GROWATT_TELEMETRY_STALE,
    GROWATT_DISPATCH_INTERVAL,
    GROWATT_SET_TIME_SLOTS,
    GROWATT_SET_ENABLE_CONTROL,
    GROWATT_CAPABILITIES,
    clamp_percent,
    local_window_to_utc_slots,
    normalize_slots,
)


class GrowattAPI(ComponentBase, OAuthMixin):
    """Growatt cloud telemetry and schedule control."""

    api_debug = True
    WRITE_AND_POLL_SLEEP = GROWATT_DISPATCH_INTERVAL

    def _token_needs_refresh(self):
        """Refresh ten minutes before expiry, not OAuthMixin's generic two hours.

        Growatt access tokens themselves last about two hours. Using the mixin's two-hour
        threshold would therefore refresh before every API call, including every device in
        a multi-inverter account.
        """
        if self.auth_method != "oauth":
            return False
        if not self.token_expires_at:
            return True
        return time.time() > self.token_expires_at - 10 * 60

    def initialize(
        self,
        key="",
        auth_method="oauth",
        token_expires_at=None,
        token_hash="",
        inverter_sn=None,
        automatic=False,
        automatic_ignore_pv=False,
        control_enable=True,
        base_url=GROWATT_BASE_URL,
        battery_rate_max=None,
        api_delay=0.2,
        min_write_interval=GROWATT_DISPATCH_INTERVAL,
        **kwargs,
    ):
        """Initialise from the resolved growatt_* component arguments."""
        self.log("Info: GrowattAPI initialising")
        self.automatic = automatic
        self.automatic_ignore_pv = automatic_ignore_pv
        self.control_enable = control_enable
        self.base_url = str(base_url or GROWATT_BASE_URL).rstrip("/")
        self.inverter_sn_filter = inverter_sn if isinstance(inverter_sn, list) else ([inverter_sn] if inverter_sn else [])
        self.battery_rate_max_override = float(battery_rate_max) if battery_rate_max else 0.0
        self.api_delay = max(0.0, float(api_delay or 0))
        self.min_write_interval = max(GROWATT_DISPATCH_INTERVAL, float(min_write_interval or 0))

        self.device_list = []
        self.device_detail = {}
        self.device_values = {}
        self.telemetry_time = {}
        self.device_energy = {}
        self.local_schedule = {}
        self.applied_slots = {}
        self.last_write_time = {}
        self.control_active = set()
        self.control_enabled = set()
        self._tier_refreshed = {}
        self._write_lock = asyncio.Lock()
        self.last_api_error = ""
        self.discovery_ok = None
        self._soc_gate_state = {}

        self._init_oauth(auth_method=auth_method or "oauth", key=key, token_expires_at=token_expires_at, provider_name="growatt")
        self.token_hash = token_hash or ""
        if not self.control_enable:
            self.log("Info: Growatt control is disabled (growatt_control_enable is false); monitoring only")

    @staticmethod
    def _as_float(value, default=0.0):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def redact(payload):
        """Return a recursively redacted copy suitable for API debug logging."""
        if isinstance(payload, dict):
            return {key: ("********" if str(key).lower() in ("access_token", "refresh_token", "authorization") else GrowattAPI.redact(value)) for key, value in payload.items()}
        if isinstance(payload, list):
            return [GrowattAPI.redact(value) for value in payload]
        return payload

    def debug_api(self, direction, endpoint, payload=None):
        if self.api_debug:
            rendered = json.dumps(self.redact(payload), default=str) if payload is not None else ""
            self.log("Info: Growatt API {} {} {}".format(direction, endpoint, rendered[:4000]))

    def describe_code(self, code):
        return GROWATT_RETURN_CODES.get(code, "Unknown Growatt response code {}".format(code))

    async def _request(self, endpoint_key, body=None, _retry_after_refresh=False):
        """POST one OAuth request and return (code, data)."""
        if not _retry_after_refresh and not await self.check_and_refresh_oauth_token():
            self.last_api_error = "OAuth token refresh failed"
            return -1, None
        if not self.access_token:
            self.last_api_error = "No OAuth access token"
            return -1, None

        path = GROWATT_ENDPOINTS[endpoint_key]
        url = self.base_url + path
        payload = body if body is not None else None
        headers = {"Authorization": "Bearer {}".format(self.access_token), "Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        self.debug_api("request", path, payload)

        last_error = ""
        timeout = aiohttp.ClientTimeout(total=GROWATT_TIMEOUT)
        for attempt in range(GROWATT_RETRIES):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    request = session.post(url, headers=headers, json=payload) if payload is not None else session.post(url, headers=headers)
                    async with request as response:
                        if response.status == 401 and not _retry_after_refresh:
                            if await self.handle_oauth_401():
                                return await self._request(endpoint_key, body=body, _retry_after_refresh=True)
                        if response.status != 200:
                            last_error = "HTTP {}".format(response.status)
                        else:
                            result = await response.json()
                            self.debug_api("response", path, result)
                            try:
                                code = int(result.get("code", -1))
                            except (TypeError, ValueError):
                                code = -1
                            message = str(result.get("message") or "")
                            if code == GROWATT_CODE_OK:
                                self.last_api_error = ""
                                return code, result.get("data")
                            if code == GROWATT_CODE_TOKEN_INVALID and not _retry_after_refresh:
                                if await self.handle_oauth_401():
                                    return await self._request(endpoint_key, body=body, _retry_after_refresh=True)
                            self.last_api_error = message or self.describe_code(code)
                            self.log("Warn: Growatt {} failed: {} ({})".format(path, self.last_api_error, code))
                            return code, result.get("data")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                last_error = str(error)
            if attempt < GROWATT_RETRIES - 1:
                await asyncio.sleep(1 + attempt)
        self.last_api_error = last_error or "request failed"
        self.log("Warn: Growatt {} transport failure: {}".format(path, self.last_api_error))
        return -1, None

    async def _post(self, endpoint_key, body=None):
        return await self._request(endpoint_key, body=body)

    def tier_expired(self, tier, ttl_minutes):
        refreshed = self._tier_refreshed.get(tier)
        return refreshed is None or time.time() - refreshed >= ttl_minutes * 60

    def mark_refreshed(self, tier):
        self._tier_refreshed[tier] = time.time()

    async def get_device_list(self):
        """Return authorised battery inverter serials, respecting the configured filter."""
        code, data = await self._post("devices")
        self.discovery_ok = code == GROWATT_CODE_OK
        if code != GROWATT_CODE_OK or not isinstance(data, list):
            return []
        found = []
        filter_set = {str(value) for value in self.inverter_sn_filter}
        for item in data:
            if not isinstance(item, dict) or not item.get("deviceSn"):
                continue
            sn = str(item["deviceSn"])
            if filter_set and sn not in filter_set:
                continue
            self.device_detail[sn] = dict(self.device_detail.get(sn, {}), **item)
            found.append(sn)
        self.device_list = found
        return found

    async def refresh_static(self):
        previous = list(self.device_list)
        devices = await self.get_device_list()
        if self.discovery_ok is False:
            self.device_list = previous
            return bool(previous)
        if not devices:
            if self.inverter_sn_filter:
                self.log("Warn: Growatt configured serial filter {} matched no authorised battery inverter".format(self.inverter_sn_filter))
            else:
                self.log("Warn: Growatt found no authorised devices")
            return False

        usable = []
        for index, sn in enumerate(devices):
            if index and self.api_delay:
                await asyncio.sleep(self.api_delay)
            code, data = await self._post("device_info", {"deviceSn": sn})
            if code != GROWATT_CODE_OK or not isinstance(data, dict):
                # Static details rarely change, so a device that was already usable keeps
                # its cached details through a transient failure rather than dropping out.
                if sn in previous:
                    self.log("Warn: Growatt {} device info refresh failed, keeping its cached details".format(sn))
                    usable.append(sn)
                continue
            self.device_detail[sn] = dict(self.device_detail.get(sn, {}), **data)
            if data.get("existBattery") is False:
                self.log("Info: Growatt {} has no battery and will not be registered as a Predbat inverter".format(sn))
                continue
            usable.append(sn)
        self.device_list = usable
        if usable:
            self.mark_refreshed("static")
        return bool(usable)

    def _apply_telemetry(self, sn, data):
        """Map Growatt telemetry into Predbat's sign and unit conventions."""
        if not isinstance(data, dict) or data.get("soc") is None:
            return False
        values = {
            "soc": self._as_float(data.get("soc")),
            # Growatt: positive charge. Predbat: positive discharge.
            "battery_power": -self._as_float(data.get("batPower")),
            # Growatt: positive import. Predbat: negative import.
            "grid_power": -self._as_float(data.get("meterPower")),
            "pv_power": self._as_float(data.get("ppv")),
            "load_power": self._as_float(data.get("payLoadPower")),
        }
        self.device_values[sn] = values

        detail = self.device_detail.setdefault(sn, {})
        # Only fresh data advances the freshness clock: a payload repeating the previous
        # utcTime is the same sample served again, not a new reading.
        if data.get("utcTime") is None or data.get("utcTime") != detail.get("utcTime") or sn not in self.telemetry_time:
            self.telemetry_time[sn] = time.time()
        for key in ("maxChargePower", "maxDischargePower", "status", "batteryStatus", "utcTime"):
            if data.get(key) is not None:
                detail[key] = data[key]

        batteries = data.get("batteryList") if isinstance(data.get("batteryList"), list) else []
        charge_today = sum(self._as_float(item.get("echargeToday")) for item in batteries if isinstance(item, dict))
        discharge_today = sum(self._as_float(item.get("edischargeToday")) for item in batteries if isinstance(item, dict))
        energy = {}
        for leaf, field in (("import_today", "etoUserToday"), ("export_today", "etoGridToday"), ("pv_today", "epvToday")):
            if data.get(field) is not None:
                energy[leaf] = self._as_float(data[field])
        if batteries:
            energy["battery_charge_today"] = charge_today
            energy["battery_discharge_today"] = discharge_today
        if all(key in energy for key in ("import_today", "export_today", "pv_today", "battery_charge_today", "battery_discharge_today")):
            energy["load_today"] = max(0.0, energy["pv_today"] + energy["import_today"] + energy["battery_discharge_today"] - energy["export_today"] - energy["battery_charge_today"])
        self.device_energy[sn] = energy
        return True

    async def refresh_live(self):
        got_any = False
        for index, sn in enumerate(list(self.device_list)):
            if index and self.api_delay:
                await asyncio.sleep(self.api_delay)
            code, data = await self._post("device_data", {"deviceSn": sn})
            if code == GROWATT_CODE_OK and self._apply_telemetry(sn, data):
                got_any = True
            else:
                self.log("Warn: Growatt {} returned no usable telemetry".format(sn))
        if got_any:
            self.mark_refreshed("live")
        return got_any

    def battery_capacity(self, sn):
        detail = self.device_detail.get(sn, {})
        capacity_wh = self._as_float(detail.get("batteryCapacity"), 0.0)
        if capacity_wh <= 0:
            capacity_wh = sum(self._as_float(item.get("batteryCapacity")) for item in detail.get("batteryList", []) if isinstance(item, dict))
        return capacity_wh / 1000.0

    def inverter_limit(self, sn):
        return self._as_float(self.device_detail.get(sn, {}).get("nominalPower"), 0.0)

    def battery_rate_max(self, sn):
        if self.battery_rate_max_override > 0:
            return self.battery_rate_max_override
        detail = self.device_detail.get(sn, {})
        values = [self._as_float(detail.get("maxChargePower"), 0.0), self._as_float(detail.get("maxDischargePower"), 0.0), self._as_float(detail.get("batteryNominalPower"), 0.0)]
        values = [value for value in values if value > 0]
        return min(values) if values else self.inverter_limit(sn)

    def _sensor_name(self, sn, leaf):
        return "sensor.{}_growatt_{}_{}".format(self.prefix, sn.lower(), leaf)

    def _control_name(self, domain, sn, leaf):
        return "{}.{}_growatt_{}_{}".format(domain, self.prefix, sn.lower(), leaf)

    async def publish_data(self):
        units = {"soc": "%", "battery_power": "W", "grid_power": "W", "pv_power": "W", "load_power": "W"}
        for sn in self.device_list:
            for leaf, value in self.device_values.get(sn, {}).items():
                if leaf in units:
                    self.dashboard_item(self._sensor_name(sn, leaf), state=value, attributes={"unit_of_measurement": units[leaf], "friendly_name": "Growatt {} {}".format(sn, leaf.replace("_", " ").title())}, app="growatt")
            for leaf, value in self.device_energy.get(sn, {}).items():
                self.dashboard_item(
                    self._sensor_name(sn, leaf),
                    state=round(value, 3),
                    attributes={"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "measurement", "friendly_name": "Growatt {} {}".format(sn, leaf.replace("_", " ").title())},
                    app="growatt",
                )
            for leaf, value, unit in (
                ("battery_capacity", self.battery_capacity(sn), "kWh"),
                ("inverter_limit", self.inverter_limit(sn), "W"),
                ("battery_rate_max", self.battery_rate_max(sn), "W"),
            ):
                if value > 0:
                    self.dashboard_item(self._sensor_name(sn, leaf), state=round(value, 3), attributes={"unit_of_measurement": unit, "friendly_name": "Growatt {} {}".format(sn, leaf.replace("_", " ").title())}, app="growatt")
            detail = self.device_detail.get(sn, {})
            for leaf, field in (("inverter_model", "model"), ("battery_model", "batteryModel"), ("status", "status")):
                if detail.get(field) is not None:
                    self.dashboard_item(self._sensor_name(sn, leaf), state=detail[field], attributes={"friendly_name": "Growatt {} {}".format(sn, leaf.replace("_", " ").title())}, app="growatt")

    async def automatic_config(self):
        devices = list(self.device_list)
        if not devices:
            return
        self.set_arg_auto("inverter_type", ["GrowattCloud" for _ in devices])
        self.set_arg_auto("num_inverters", len(devices))
        for arg, leaf in (("soc_percent", "soc"), ("battery_power", "battery_power"), ("grid_power", "grid_power"), ("load_power", "load_power")):
            self.set_arg_auto(arg, [self._sensor_name(sn, leaf) for sn in devices])
        if not self.automatic_ignore_pv:
            self.set_arg_auto("pv_power", [self._sensor_name(sn, "pv_power") for sn in devices])
        for flag in ("grid_power_invert", "battery_power_invert", "load_power_invert"):
            self.set_arg_auto(flag, [False for _ in devices])
        for leaf in ("load_today", "import_today", "export_today", "pv_today"):
            if leaf == "pv_today" and self.automatic_ignore_pv:
                continue
            if all(leaf in self.device_energy.get(sn, {}) for sn in devices):
                self.set_arg_auto(leaf, [self._sensor_name(sn, leaf) for sn in devices])
        for arg, leaf, getter in (("soc_max", "battery_capacity", self.battery_capacity), ("inverter_limit", "inverter_limit", self.inverter_limit), ("battery_rate_max", "battery_rate_max", self.battery_rate_max)):
            if all(getter(sn) > 0 for sn in devices):
                self.set_arg_auto(arg, [self._sensor_name(sn, leaf) for sn in devices])
        self.log("Warn: Growatt does not report the site's grid export limit; set export_limit manually when the connection is capped below the inverter rating")

        self.set_arg_auto("reserve", [self._control_name("number", sn, "battery_schedule_reserve") for sn in devices])
        self.set_arg_auto("charge_start_time", [self._control_name("select", sn, "battery_schedule_charge_start_time") for sn in devices])
        self.set_arg_auto("charge_end_time", [self._control_name("select", sn, "battery_schedule_charge_end_time") for sn in devices])
        self.set_arg_auto("charge_limit", [self._control_name("number", sn, "battery_schedule_charge_soc") for sn in devices])
        self.set_arg_auto("charge_rate", [self._control_name("number", sn, "battery_schedule_charge_power") for sn in devices])
        self.set_arg_auto("scheduled_charge_enable", [self._control_name("switch", sn, "battery_schedule_charge_enable") for sn in devices])
        self.set_arg_auto("discharge_start_time", [self._control_name("select", sn, "battery_schedule_export_start_time") for sn in devices])
        self.set_arg_auto("discharge_end_time", [self._control_name("select", sn, "battery_schedule_export_end_time") for sn in devices])
        self.set_arg_auto("discharge_target_soc", [self._control_name("number", sn, "battery_schedule_export_soc") for sn in devices])
        self.set_arg_auto("discharge_rate", [self._control_name("number", sn, "battery_schedule_export_power") for sn in devices])
        self.set_arg_auto("scheduled_discharge_enable", [self._control_name("switch", sn, "battery_schedule_export_enable") for sn in devices])
        self.set_arg_auto("schedule_write_button", [self._control_name("switch", sn, "battery_schedule_charge_write") for sn in devices])

    @staticmethod
    def _empty_schedule():
        return {
            "reserve": 0,
            "charge": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"},
            "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"},
        }

    async def publish_schedule_settings_ha(self, sn):
        local = self.local_schedule.get(sn, self._empty_schedule())
        power_max = max(100, int(self.battery_rate_max(sn) or 10000))
        device_floor = int(self._as_float(self.device_detail.get(sn, {}).get("dischargeCutOffSOC"), 0))
        self.dashboard_item(
            self._control_name("number", sn, "battery_schedule_reserve"),
            state=int(local.get("reserve", 0)),
            attributes={"min": device_floor, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": "Growatt {} Battery Schedule Reserve".format(sn), "icon": "mdi:gauge"},
            app="growatt",
        )
        for direction in ("charge", "export"):
            window = local.get(direction, {})
            self.dashboard_item(
                self._control_name("select", sn, "battery_schedule_{}_start_time".format(direction)),
                state=window.get("start", "00:00:00"),
                attributes={"friendly_name": "Growatt {} {} Start".format(sn, direction.title()), "icon": "mdi:clock-outline"},
                app="growatt",
            )
            self.dashboard_item(
                self._control_name("select", sn, "battery_schedule_{}_end_time".format(direction)),
                state=window.get("end", "00:00:00"),
                attributes={"friendly_name": "Growatt {} {} End".format(sn, direction.title()), "icon": "mdi:clock-outline"},
                app="growatt",
            )
            self.dashboard_item(
                self._control_name("number", sn, "battery_schedule_{}_soc".format(direction)),
                state=int(window.get("soc", 0)),
                attributes={"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": "Growatt {} {} SoC".format(sn, direction.title()), "icon": "mdi:gauge"},
                app="growatt",
            )
            self.dashboard_item(
                self._control_name("number", sn, "battery_schedule_{}_power".format(direction)),
                state=int(window.get("power", 0)),
                attributes={"min": 0, "max": power_max, "step": 100, "unit_of_measurement": "W", "friendly_name": "Growatt {} {} Power".format(sn, direction.title()), "icon": "mdi:flash"},
                app="growatt",
            )
            self.dashboard_item(
                self._control_name("switch", sn, "battery_schedule_{}_enable".format(direction)),
                state="on" if window.get("enable") else "off",
                attributes={"friendly_name": "Growatt {} {} Enable".format(sn, direction.title()), "icon": "mdi:check-circle-outline"},
                app="growatt",
            )
        self.dashboard_item(self._control_name("switch", sn, "battery_schedule_charge_write"), state="off", attributes={"friendly_name": "Growatt {} Schedule Write".format(sn), "icon": "mdi:content-save"}, app="growatt")

    async def get_schedule_settings_ha(self, sn):
        schedule = {"reserve": int(self._as_float(self.get_state_wrapper(self._control_name("number", sn, "battery_schedule_reserve"), default=0), 0))}
        for direction in ("charge", "export"):
            schedule[direction] = {
                "enable": self.get_state_wrapper(self._control_name("switch", sn, "battery_schedule_{}_enable".format(direction)), default="off") == "on",
                "start": self.get_state_wrapper(self._control_name("select", sn, "battery_schedule_{}_start_time".format(direction)), default="00:00:00"),
                "end": self.get_state_wrapper(self._control_name("select", sn, "battery_schedule_{}_end_time".format(direction)), default="00:00:00"),
                "soc": int(self._as_float(self.get_state_wrapper(self._control_name("number", sn, "battery_schedule_{}_soc".format(direction)), default=0), 0)),
                "power": int(self._as_float(self.get_state_wrapper(self._control_name("number", sn, "battery_schedule_{}_power".format(direction)), default=0), 0)),
            }
        self.local_schedule[sn] = schedule
        return schedule

    def _sn_from_entity(self, entity_id):
        text = str(entity_id).lower()
        for sn in self.device_list:
            if "_growatt_{}_".format(str(sn).lower()) in text:
                return sn
        return None

    @staticmethod
    def _to_bool(value, current=False):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("turn_on", "on", "true", "1"):
            return True
        if text in ("turn_off", "off", "false", "0"):
            return False
        if text == "toggle":
            return not current
        return current

    def update_local_schedule(self, sn, entity_id, value):
        schedule = self.local_schedule.setdefault(sn, self._empty_schedule())
        leaf = str(entity_id).split("_growatt_{}_".format(str(sn).lower()), 1)[-1]
        if leaf == "battery_schedule_reserve":
            schedule["reserve"] = int(self._as_float(value, 0))
            return
        for direction in ("charge", "export"):
            prefix = "battery_schedule_{}_".format(direction)
            if not leaf.startswith(prefix):
                continue
            field = leaf[len(prefix) :]
            if field in ("start_time", "end_time"):
                schedule[direction][field.replace("_time", "")] = str(value)
            elif field in ("soc", "power"):
                schedule[direction][field] = int(self._as_float(value, 0))
            elif field == "enable":
                schedule[direction]["enable"] = self._to_bool(value, schedule[direction].get("enable", False))
            return

    async def select_event(self, entity_id, value):
        await self._handle_control_event(entity_id, value)

    async def number_event(self, entity_id, value):
        await self._handle_control_event(entity_id, value)

    async def switch_event(self, entity_id, service):
        await self._handle_control_event(entity_id, service)

    async def _handle_control_event(self, entity_id, value):
        sn = self._sn_from_entity(entity_id)
        if not sn:
            self.log("Warn: Growatt could not resolve a device for {}".format(entity_id))
            return
        if str(entity_id).endswith("battery_schedule_charge_write"):
            if self._to_bool(value):
                self.control_active.add(sn)
                await self.apply_schedule(sn)
            return
        self.update_local_schedule(sn, entity_id, value)
        await self.publish_schedule_settings_ha(sn)

    def telemetry_stale(self, sn):
        """Return True when a device's last fresh telemetry is older than GROWATT_TELEMETRY_STALE minutes."""
        received = self.telemetry_time.get(sn)
        return received is not None and time.time() - received > GROWATT_TELEMETRY_STALE * 60

    def _gate_reason(self, sn, direction, window, schedule):
        """Return why a slot must be withheld at the latest observed SoC, or None."""
        soc = self.device_values.get(sn, {}).get("soc")
        if soc is None:
            return None
        if self.telemetry_stale(sn):
            return "telemetry has not updated for over {} minutes, so the SoC gate cannot be enforced".format(GROWATT_TELEMETRY_STALE)
        if direction == "charge":
            target = self._as_float(window.get("soc"), 0)
            if target <= 0:
                return "charge target is not ready"
            if soc >= target:
                return "SoC {:.1f}% reached charge target {}%".format(soc, int(target))
            return None
        device_floor = self._as_float(self.device_detail.get(sn, {}).get("dischargeCutOffSOC"), 0)
        floor = max(self._as_float(schedule.get("reserve"), 0), self._as_float(window.get("soc"), 0), device_floor)
        if soc <= floor:
            return "SoC {:.1f}% reached discharge floor {}%".format(soc, int(floor))
        return None

    def build_time_slots(self, sn, schedule):
        """Build Growatt's UTC slot array, applying software SoC gates."""
        slots = []
        rate_max = self.battery_rate_max(sn)
        if rate_max <= 0:
            self.log("Warn: Growatt {} has no battery power rating, so no control slots can be built".format(sn))
            return []
        now_local = datetime.now(self.local_tz)
        for direction, sign in (("charge", 1), ("export", -1)):
            window = schedule.get(direction, {})
            if not window.get("enable"):
                continue
            reason = self._gate_reason(sn, direction, window, schedule)
            gate_key = (sn, direction)
            if reason:
                if self._soc_gate_state.get(gate_key) != reason:
                    self.log("Info: Growatt {} withholding {} slot because {}. The public API has no writable SoC limit, so Predbat enforces this from one-minute telemetry.".format(sn, direction, reason))
                self._soc_gate_state[gate_key] = reason
                continue
            if gate_key in self._soc_gate_state:
                self.log("Info: Growatt {} restoring {} slot after its software SoC gate cleared".format(sn, direction))
                self._soc_gate_state.pop(gate_key, None)
            percentage = sign * clamp_percent(self._as_float(window.get("power"), 0) / rate_max * 100.0)
            slots.extend(local_window_to_utc_slots(window.get("start"), window.get("end"), percentage, now_local))
        return normalize_slots(slots)[:16]

    @staticmethod
    def request_id():
        """Growatt requires a unique 32-character requestId."""
        return uuid.uuid4().hex

    async def dispatch(self, sn, set_type, value):
        body = {"deviceSn": sn, "setType": set_type, "value": value, "requestId": self.request_id()}
        code, _ = await self._post("dispatch", body)
        return code == GROWATT_CODE_OK

    def _is_read_only(self):
        return self.get_state_wrapper("switch.{}_set_read_only".format(self.prefix), default="off") == "on"

    async def apply_schedule(self, sn, force=False):
        """Write one complete slot table, with change detection and endpoint pacing."""
        if not self.control_enable or self._is_read_only():
            return False
        schedule = self.local_schedule.get(sn)
        if not schedule:
            return False
        slots = self.build_time_slots(sn, schedule)
        if not force and normalize_slots(self.applied_slots.get(sn)) == slots:
            return True

        async with self._write_lock:
            now = time.monotonic()
            elapsed = now - self.last_write_time.get(sn, 0)
            if elapsed < self.min_write_interval:
                self.log("Info: Growatt {} schedule update deferred for {:.1f}s by the documented dispatch rate limit".format(sn, self.min_write_interval - elapsed))
                return False
            if sn not in self.control_enabled:
                if not await self.dispatch(sn, GROWATT_SET_ENABLE_CONTROL, 1):
                    return False
                self.control_enabled.add(sn)
                self.last_write_time[sn] = time.monotonic()
                await asyncio.sleep(GROWATT_DISPATCH_INTERVAL)
            ok = await self.dispatch(sn, GROWATT_SET_TIME_SLOTS, slots)
            self.last_write_time[sn] = time.monotonic()
            if not ok:
                return False
            self.applied_slots[sn] = list(slots)
            self.log("Info: Growatt wrote {} UTC charge/discharge slot(s) for {}".format(len(slots), sn))
            return True

    async def _reconcile_control(self, sn):
        if sn not in self.control_active or self._is_read_only() or not self.control_enable:
            return
        try:
            await self.apply_schedule(sn)
        except Exception as error:
            self.log("Warn: Growatt schedule apply failed for {}: {}".format(sn, error))

    def _discovery_entities(self, sn):
        entities = {
            "soc_percent": {"entity_id": self._sensor_name(sn, "soc"), "access": "r", "unit": "%"},
            "battery_power": {"entity_id": self._sensor_name(sn, "battery_power"), "access": "r", "unit": "W"},
            "grid_power": {"entity_id": self._sensor_name(sn, "grid_power"), "access": "r", "unit": "W"},
            "load_power": {"entity_id": self._sensor_name(sn, "load_power"), "access": "r", "unit": "W"},
            "pv_power": {"entity_id": self._sensor_name(sn, "pv_power"), "access": "r", "unit": "W"},
            "reserve": {"entity_id": self._control_name("number", sn, "battery_schedule_reserve"), "access": "rw", "unit": "%"},
            "charge_start_time": {"entity_id": self._control_name("select", sn, "battery_schedule_charge_start_time"), "access": "rw", "domain": "select", "format": "HH:MM:SS"},
            "charge_end_time": {"entity_id": self._control_name("select", sn, "battery_schedule_charge_end_time"), "access": "rw", "domain": "select", "format": "HH:MM:SS"},
            "charge_limit": {"entity_id": self._control_name("number", sn, "battery_schedule_charge_soc"), "access": "rw", "unit": "%"},
            "charge_rate": {"entity_id": self._control_name("number", sn, "battery_schedule_charge_power"), "access": "rw", "unit": "W", "step": 100},
            "scheduled_charge_enable": {"entity_id": self._control_name("switch", sn, "battery_schedule_charge_enable"), "access": "rw", "domain": "switch"},
            "discharge_start_time": {"entity_id": self._control_name("select", sn, "battery_schedule_export_start_time"), "access": "rw", "domain": "select", "format": "HH:MM:SS"},
            "discharge_end_time": {"entity_id": self._control_name("select", sn, "battery_schedule_export_end_time"), "access": "rw", "domain": "select", "format": "HH:MM:SS"},
            "discharge_target_soc": {"entity_id": self._control_name("number", sn, "battery_schedule_export_soc"), "access": "rw", "unit": "%"},
            "discharge_rate": {"entity_id": self._control_name("number", sn, "battery_schedule_export_power"), "access": "rw", "unit": "W", "step": 100},
            "scheduled_discharge_enable": {"entity_id": self._control_name("switch", sn, "battery_schedule_export_enable"), "access": "rw", "domain": "switch"},
            "schedule_write_button": {"entity_id": self._control_name("switch", sn, "battery_schedule_charge_write"), "access": "rw", "domain": "switch"},
        }
        for leaf in ("load_today", "import_today", "export_today", "pv_today"):
            if leaf in self.device_energy.get(sn, {}):
                entities[leaf] = {"entity_id": self._sensor_name(sn, leaf), "access": "r", "unit": "kWh"}
        if self.battery_capacity(sn) > 0:
            entities["soc_max"] = {"entity_id": self._sensor_name(sn, "battery_capacity"), "access": "r", "unit": "kWh"}
        if self.inverter_limit(sn) > 0:
            entities["inverter_limit"] = {"entity_id": self._sensor_name(sn, "inverter_limit"), "access": "r", "unit": "W"}
        if self.battery_rate_max(sn) > 0:
            entities["battery_rate_max"] = {"entity_id": self._sensor_name(sn, "battery_rate_max"), "access": "r", "unit": "W"}
        return entities

    def build_discovery(self):
        if not self.device_list:
            return None
        inverters = []
        for sn in self.device_list:
            detail = self.device_detail.get(sn, {})
            ratings = {}
            for key, value in (("soc_max", self.battery_capacity(sn)), ("inverter_limit", self.inverter_limit(sn)), ("battery_rate_max", self.battery_rate_max(sn))):
                if value > 0:
                    ratings[key] = value
            info = {}
            for key, field in (("model", "model"), ("battery_model", "batteryModel"), ("firmware", "communicationVersion")):
                if detail.get(field):
                    info[key] = str(detail[field])
            inverters.append(
                inverter_record(
                    "growatt:{}".format(sn),
                    inverter_type="GrowattCloud",
                    control=self.control_enable,
                    composition="direct",
                    functions=["solar", "battery"],
                    capabilities=dict(GROWATT_CAPABILITIES),
                    hardware_ids={"serial": sn},
                    info=info,
                    ratings=ratings,
                    entities=self._discovery_entities(sn),
                )
            )
        return {"automatic": self.automatic, "inverters": inverters}

    def health_message(self):
        if not self.access_token:
            return "Growatt has no OAuth access token"
        if not self.device_list:
            return "Growatt found no authorised battery inverter"
        return None

    async def run(self, seconds, first):
        if not await self.check_and_refresh_oauth_token():
            return False
        if not self.access_token:
            self.log("Warn: Growatt needs an OAuth access token from Predbat.com")
            return False
        if self.tier_expired("static", GROWATT_TTL_STATIC) or not self.device_list:
            await self.refresh_static()
        if not self.device_list:
            return False
        live_ok = True
        if self.tier_expired("live", GROWATT_TTL_LIVE):
            live_ok = await self.refresh_live()

        for sn in list(self.device_list):
            try:
                await self.get_schedule_settings_ha(sn)
            except Exception as error:
                self.log("Warn: Growatt schedule read failed for {}: {}".format(sn, error))
            await self._reconcile_control(sn)
            await self.publish_schedule_settings_ha(sn)
        await self.publish_data()
        self.refresh_discovery()
        if first and not live_ok:
            self.log("Warn: Growatt first telemetry poll returned nothing, deferring startup")
            return False
        if first and self.automatic:
            await self.automatic_config()
        self.update_success_timestamp()
        return True

    async def final(self):
        return None
