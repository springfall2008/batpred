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

The public API supplies telemetry (polled each minute, uploaded about every five minutes)
and UTC charge/discharge time slots. It does
not expose writable battery SoC limits. Predbat therefore keeps its charge target and export
floor in Home Assistant. When the latest telemetry reaches the charge target the charge slot
is kept at 0% power, so the battery holds; when it reaches the export floor the export slot
is removed. That is deliberately visible in the logs and documentation because it is software
enforcement, not a limit stored in the inverter. No reserve is claimed: outside a slot the
inverter's own self-consumption runs to its local minimum, which Predbat cannot hold above.

The slot table last written, and whether Predbat holds OpenAPI control, are saved through the
Storage component so a restart neither forgets slots it left on the device nor a release it
still owes. The device's own table is read back every GROWATT_TTL_READBACK minutes.

Writes are often answered with an error yet applied, and reads often fail while the device is
busy, so a write is judged by reading it back, and one whose read-back fails is treated as
unknown: the next attempt reads first. A table that could not be confirmed is retried with a
back-off (GROWATT_WRITE_BACKOFF_MAX), skipped as soon as the planned table changes.

Exporting depends on the site: a live SPH accepted an export slot and an export command, but
only covered the house load and never exported, while a charge slot charged as set. Its
export_limit setting (readDeviceDispatch) read {"percentage": 0, "exportLimitEnabled": 1}: zero
export, set on the inverter.
"""

# cspell:ignore echarge edischarge

import asyncio
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone

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
    GROWATT_CODE_TOO_MANY_REQUESTS,
    GROWATT_RATE_LIMIT_WAIT_MAX,
    GROWATT_TIMEOUT,
    GROWATT_RETRIES,
    GROWATT_WRITE_ATTEMPTS,
    GROWATT_READ_ATTEMPTS,
    GROWATT_RETRY_BACKOFF,
    GROWATT_WRITE_CONFIRMED,
    GROWATT_WRITE_MISMATCH,
    GROWATT_WRITE_UNKNOWN,
    GROWATT_WRITE_BACKOFF_MAX,
    GROWATT_TTL_STATIC,
    GROWATT_TTL_LIVE,
    GROWATT_TELEMETRY_STALE,
    GROWATT_HOLD_HYSTERESIS,
    GROWATT_DISPATCH_INTERVAL,
    GROWATT_TTL_READBACK,
    GROWATT_RELEASE_RETRY,
    GROWATT_LOOP_HANDOVER_TIMEOUT,
    GROWATT_SET_TIME_SLOTS,
    GROWATT_SET_ENABLE_CONTROL,
    GROWATT_CONTROL_ON,
    GROWATT_STORAGE_MODULE,
    GROWATT_CACHE_CONTROL,
    GROWATT_CAPABILITIES,
    clamp_percent,
    dispatch_slot_payload,
    local_window_to_utc_slots,
    normalize_slots,
    parse_dispatch_slots,
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
        # The slot table believed to be on each device: what was last written, replaced by
        # the device's own answer whenever a read-back succeeds. A serial absent from it is
        # unknown, which is not the same as an empty table.
        self.applied_slots = {}
        # The last slot table the device itself reported, for the published read-back sensor
        self.readback_slots = {}
        self.last_write_time = {}
        self.control_active = set()
        # control_enabled: enable_control was sent by this process, so a write can skip it.
        # control_claimed: persisted - Predbat holds OpenAPI control and must release it when
        # it goes read-only, even after a restart. A restart re-sends enable_control once
        # rather than trusting that the device still has it.
        self.control_enabled = set()
        self.control_claimed = set()
        # A slot table written but not confirmed because its read-back failed: the next attempt
        # reads before it writes again.
        self.unverified_slots = {}
        # Consecutive slot tables that could not be confirmed, the table they were for, and the
        # monotonic time before which that same table is not tried again.
        self.write_failures = {}
        self.failed_slots = {}
        self.write_retry_at = {}
        # The outcome of the last write_verified(): GROWATT_WRITE_CONFIRMED, _MISMATCH or _UNKNOWN
        self.last_write_outcome = None
        self._state_restored = False
        self._tier_refreshed = {}
        self._write_lock = asyncio.Lock()
        self.last_api_error = ""
        self.discovery_ok = None
        self._soc_gate_state = {}
        # Serials whose charge slot is holding at 0% power at the charge target
        self._charge_hold = set()

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

    @staticmethod
    def rate_limit_wait(message):
        """Seconds to wait after a 105 answer: the "retry after Nms" it gives, capped at GROWATT_RATE_LIMIT_WAIT_MAX.

        A live answer read "Endpoint rate limited for clientId=... deviceSn=..., retry after 3551ms".
        Without a parsable wait, one dispatch interval is used.
        """
        match = re.search(r"retry after\s+(\d+)\s*ms", str(message or ""), re.IGNORECASE)
        seconds = int(match.group(1)) / 1000.0 + 0.5 if match else float(GROWATT_DISPATCH_INTERVAL)
        return min(float(GROWATT_RATE_LIMIT_WAIT_MAX), seconds)

    async def _request(self, endpoint_key, body=None, _retry_after_refresh=False, _rate_limited=False):
        """POST one OAuth request and return (code, data).

        A TOO_MANY_REQUEST (105) answer is retried once after the wait it gives; a second 105 is returned.
        """
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
        # A dispatch write is sent once: a timed-out write may have been applied anyway, so
        # write_verified() reads it back rather than this loop re-sending it blind.
        attempts = 1 if endpoint_key == "dispatch" else GROWATT_RETRIES
        for attempt in range(attempts):
            try:
                async with self.http_session(timeout=timeout) as session:
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
                            if code == GROWATT_CODE_TOO_MANY_REQUESTS and not _rate_limited:
                                # Refused before it reached the device, so sending it again is
                                # safe - under a new requestId, which Growatt requires per request
                                wait = self.rate_limit_wait(message)
                                self.log("Info: Growatt {} rate limited, retrying once in {:.1f}s".format(path, wait))
                                await self._sleep(wait)
                                if isinstance(body, dict) and "requestId" in body:
                                    body = dict(body, requestId=self.request_id())
                                return await self._request(endpoint_key, body=body, _retry_after_refresh=_retry_after_refresh, _rate_limited=True)
                            self.last_api_error = message or self.describe_code(code)
                            self.log("Warn: Growatt {} failed: {} ({})".format(path, self.last_api_error, code))
                            return code, result.get("data")
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                last_error = str(error)
            if attempt < attempts - 1:
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
            if sn in self.readback_slots:
                slots = self.readback_slots[sn]
                self.dashboard_item(
                    self._sensor_name(sn, "dispatch_slots"),
                    state=len(slots),
                    attributes={"slots": slots, "time_zone": "UTC", "friendly_name": "Growatt {} Dispatch Slots (device read-back)".format(sn), "icon": "mdi:calendar-clock"},
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
        # A rating is bound only when every device reports it. A live SPH reports batteryCapacity 0
        # (top level and batteryList alike), so soc_max is then left unset and Predbat estimates
        # the battery size from charge history (battery_scaling_auto / find_battery_size).
        for arg, leaf, getter in (("soc_max", "battery_capacity", self.battery_capacity), ("inverter_limit", "inverter_limit", self.inverter_limit), ("battery_rate_max", "battery_rate_max", self.battery_rate_max)):
            if all(getter(sn) > 0 for sn in devices):
                self.set_arg_auto(arg, [self._sensor_name(sn, leaf) for sn in devices])
            elif arg == "soc_max":
                self.log("Info: Growatt does not report a battery capacity for {}; soc_max is left unset so Predbat estimates the battery size from charge history".format(", ".join(sn for sn in devices if getter(sn) <= 0)))
        self.log("Warn: Growatt does not report the site's grid export limit; set export_limit manually when the connection is capped below the inverter rating")

        # No reserve is bound: GrowattCloud does not claim has_reserve_soc, so inverter.py gives
        # it a Predbat-owned reserve entity instead (see INVERTER_DEF["GrowattCloud"]).
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
            "charge": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"},
            "export": {"enable": False, "soc": 0, "power": 0, "start": "00:00:00", "end": "00:00:00"},
        }

    async def publish_schedule_settings_ha(self, sn):
        local = self.local_schedule.get(sn, self._empty_schedule())
        power_max = max(100, int(self.battery_rate_max(sn) or 10000))
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
        schedule = {}
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
                if sn not in self.control_active:
                    self.control_active.add(sn)
                    await self.save_state()
                # Predbat presses the write button from its plan thread through ha.py's run_async(),
                # on a throwaway loop of its own; apply_schedule() takes _write_lock, which belongs to
                # the loop run() ticks on, so a press while run() held it failed with "is bound to a
                # different event loop". Hand the write over to the component's loop.
                await self.on_component_loop(self.apply_schedule(sn), timeout=GROWATT_LOOP_HANDOVER_TIMEOUT, what="Growatt write")
            return
        self.update_local_schedule(sn, entity_id, value)
        await self.publish_schedule_settings_ha(sn)

    def telemetry_stale(self, sn):
        """Return True when a device's last fresh telemetry is older than GROWATT_TELEMETRY_STALE minutes."""
        received = self.telemetry_time.get(sn)
        return received is not None and time.time() - received > GROWATT_TELEMETRY_STALE * 60

    def _gate_reason(self, sn, direction, window):
        """Return why a slot must be withheld at the latest observed SoC, or None.

        Reaching the charge target is not a reason to withhold: the charge slot is kept at 0%
        power instead (see _charge_at_target), so the battery holds rather than discharging.
        """
        soc = self.device_values.get(sn, {}).get("soc")
        if soc is None:
            return None
        if self.telemetry_stale(sn):
            return "telemetry has not updated for over {} minutes, so the SoC gate cannot be enforced".format(GROWATT_TELEMETRY_STALE)
        if direction == "charge":
            target = self._as_float(window.get("soc"), 0)
            if target <= 0:
                return "charge target is not ready"
            return None
        device_floor = self._as_float(self.device_detail.get(sn, {}).get("dischargeCutOffSOC"), 0)
        floor = max(self._as_float(window.get("soc"), 0), device_floor)
        if soc <= floor:
            return "SoC {:.1f}% reached discharge floor {}%".format(soc, int(floor))
        return None

    def _charge_at_target(self, sn, window):
        """Return True while the charge slot should hold at 0% power rather than charge.

        Holding starts when SoC reaches the charge target and lasts until SoC falls more than
        GROWATT_HOLD_HYSTERESIS below it, matching the planner's own 1% tolerance at the limit
        (prediction.py holds a charge window within 1% of its limit). Without the band the slot
        would flip between 0% and full power, and be rewritten, on every one-percent wobble.
        """
        soc = self.device_values.get(sn, {}).get("soc")
        target = self._as_float(window.get("soc"), 0)
        holding = sn in self._charge_hold
        at_target = soc is not None and (soc >= target or (holding and soc >= target - GROWATT_HOLD_HYSTERESIS))
        if at_target and not holding:
            self.log(
                "Info: Growatt {} SoC {:.1f}% reached charge target {}%, holding the charge slot at 0% power. The public API has no writable SoC limit, so Predbat enforces this from telemetry, which Growatt updates about every five minutes.".format(
                    sn, soc, int(target)
                )
            )
            self._charge_hold.add(sn)
        elif holding and not at_target:
            self.log("Info: Growatt {} SoC {} fell below charge target {}%, resuming the charge".format(sn, "unknown" if soc is None else "{:.1f}%".format(soc), int(target)))
            self._charge_hold.discard(sn)
        return at_target

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
                if direction == "charge":
                    self._charge_hold.discard(sn)
                continue
            reason = self._gate_reason(sn, direction, window)
            gate_key = (sn, direction)
            if reason:
                if self._soc_gate_state.get(gate_key) != reason:
                    self.log("Info: Growatt {} withholding {} slot because {}. The public API has no writable SoC limit, so Predbat enforces this from telemetry, which Growatt updates about every five minutes.".format(sn, direction, reason))
                self._soc_gate_state[gate_key] = reason
                if direction == "charge":
                    self._charge_hold.discard(sn)
                continue
            if gate_key in self._soc_gate_state:
                self.log("Info: Growatt {} restoring {} slot after its software SoC gate cleared".format(sn, direction))
                self._soc_gate_state.pop(gate_key, None)
            percentage = sign * clamp_percent(self._as_float(window.get("power"), 0) / rate_max * 100.0)
            # At the charge target the slot is kept, at 0% power, so the battery holds and the
            # house runs from the grid - as the plan expects - instead of self-consumption
            # draining it in the gap a removed slot would leave.
            if direction == "charge" and self._charge_at_target(sn, window):
                percentage = 0
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

    async def read_dispatch(self, sn, set_type):
        """Read one dispatch setting back from the device, returning (ok, data)."""
        body = {"deviceSn": sn, "setType": set_type, "requestId": self.request_id()}
        code, data = await self._post("read_dispatch", body)
        return code == GROWATT_CODE_OK, data

    def _is_read_only(self):
        return self.get_state_wrapper("switch.{}_set_read_only".format(self.prefix), default="off") == "on"

    def _pacing_wait(self, sn):
        """Seconds until the per-device dispatch limit allows another request, 0 when it does."""
        return max(0.0, self.min_write_interval - (time.monotonic() - self.last_write_time.get(sn, 0)))

    async def _sleep(self, seconds):
        """Wait, as a method so tests can skip the pacing and back-off waits."""
        await asyncio.sleep(seconds)

    async def _paced(self, sn, call):
        """Wait out the per-device dispatch limit, make one dispatch read or write, and stamp it."""
        wait = self._pacing_wait(sn)
        if wait > 0:
            await self._sleep(wait)
        try:
            return await call()
        finally:
            self.last_write_time[sn] = time.monotonic()

    async def read_dispatch_retry(self, sn, set_type, attempts=GROWATT_READ_ATTEMPTS):
        """Read one dispatch setting, retrying with back-off while the device is busy, offline or slow.

        Returns (ok, data). A live SPH answered READ_DEVICE_PARAM_FAIL (18) for a few minutes
        after a slow write, so one failed read is not taken as the answer.
        """
        for attempt in range(attempts):
            if attempt:
                await self._sleep(GROWATT_RETRY_BACKOFF * attempt)
            ok, data = await self._paced(sn, lambda: self.read_dispatch(sn, set_type))
            if ok:
                return True, data
        return False, None

    async def write_verified(self, sn, set_type, value, matches, attempts=GROWATT_WRITE_ATTEMPTS):
        """Write one dispatch setting and confirm it by reading it back, retrying until it reads as written.

        The read-back decides, not the write's own answer: Growatt's docs say to reconcile a
        timed-out write with readDeviceDispatch, and a live SPH answered PARAMETER_SETTING_FAILED
        to a table it did apply. matches(data) says whether a read shows the write took effect.
        Returns True once a read-back matches. last_write_outcome records how it ended:
        GROWATT_WRITE_CONFIRMED, GROWATT_WRITE_MISMATCH when the last read-back showed something
        else, or GROWATT_WRITE_UNKNOWN when it could not be read - the write may well have applied.
        """
        self.last_write_outcome = GROWATT_WRITE_UNKNOWN
        for attempt in range(attempts):
            if attempt:
                await self._sleep(GROWATT_RETRY_BACKOFF * attempt)
            wrote = await self._paced(sn, lambda: self.dispatch(sn, set_type, value))
            ok, data = await self.read_dispatch_retry(sn, set_type)
            if ok and matches(data):
                if not wrote:
                    self.log("Info: Growatt {} {} answered {}, but the read-back shows it applied".format(sn, set_type, self.last_api_error or "a failure"))
                self.last_write_outcome = GROWATT_WRITE_CONFIRMED
                return True
            self.last_write_outcome = GROWATT_WRITE_MISMATCH if ok else GROWATT_WRITE_UNKNOWN
            self.log("Warn: Growatt {} {} attempt {} of {} not confirmed by read-back ({})".format(sn, set_type, attempt + 1, attempts, "read failed" if not ok else "device holds {}".format(json.dumps(data, default=str)[:300])))
        return False

    def _write_backoff_minutes(self, sn):
        """Minutes to wait before retrying the same unconfirmed table: 1, 2, 4, 8, then GROWATT_WRITE_BACKOFF_MAX."""
        failures = self.write_failures.get(sn, 0)
        if failures <= 0:
            return 0
        return min(GROWATT_WRITE_BACKOFF_MAX, 2 ** (failures - 1))

    def _note_write_failure(self, sn, slots):
        """Count an unconfirmed table and schedule its retry."""
        self.write_failures[sn] = self.write_failures.get(sn, 0) + 1
        self.failed_slots[sn] = list(slots)
        minutes = self._write_backoff_minutes(sn)
        self.write_retry_at[sn] = time.monotonic() + minutes * 60
        return minutes

    def _clear_write_failure(self, sn):
        self.write_failures.pop(sn, None)
        self.failed_slots.pop(sn, None)
        self.write_retry_at.pop(sn, None)

    def _write_backing_off(self, sn, slots):
        """Return True while the same table that last failed is waiting out its back-off; a changed table never waits."""
        if sn not in self.write_retry_at or self.failed_slots.get(sn) != list(slots):
            return False
        return time.monotonic() < self.write_retry_at[sn]

    async def _confirm_unverified(self, sn, slots):
        """Read back a table written earlier whose read-back failed, returning True when the device holds it.

        Called with _write_lock held, before writing the same table again: the earlier write has
        often applied, and a read is cheaper than a write that then needs confirming too.
        """
        ok, data = await self.read_dispatch_retry(sn, GROWATT_SET_TIME_SLOTS)
        if not ok:
            return False
        held = parse_dispatch_slots(data)
        if held is None:
            return False
        self.readback_slots[sn] = held
        if held == slots:
            self.log("Info: Growatt {} holds the {} slot(s) written earlier, confirmed by read-back".format(sn, len(slots)))
            return True
        return False

    async def write_slots_verified(self, sn, slots):
        """Write a slot table (16 unused slots for an empty one) and confirm it by reading it back."""
        target = normalize_slots(slots)
        return await self.write_verified(sn, GROWATT_SET_TIME_SLOTS, dispatch_slot_payload(target), lambda data: parse_dispatch_slots(data) == target)

    async def set_control_verified(self, sn, value):
        """Set enable_control and confirm it by reading it back."""
        return await self.write_verified(sn, GROWATT_SET_ENABLE_CONTROL, value, lambda data: data == value)

    async def apply_schedule(self, sn, force=False):
        """Write one complete slot table, with change detection and endpoint pacing.

        The write is skipped only when the table matches the one the device is known to hold.
        A serial with no known table - a first run with nothing saved - is always written, so
        slots an earlier run left on the device are cleared even when Predbat now wants none.

        A table that could not be confirmed is retried after a back-off that doubles with each
        consecutive failure (see _write_backoff_minutes), unless the planned table has changed or
        force is set. When the last write's read-back failed, the retry reads first and writes
        only if the device does not already hold the table.
        """
        if not self.control_enable or self._is_read_only():
            return False
        schedule = self.local_schedule.get(sn)
        if not schedule:
            return False
        slots = self.build_time_slots(sn, schedule)
        if not force and sn in self.applied_slots and normalize_slots(self.applied_slots[sn]) == slots:
            return True
        if not force and self._write_backing_off(sn, slots):
            return False

        async with self._write_lock:
            wait = self._pacing_wait(sn)
            if wait > 0:
                self.log("Info: Growatt {} schedule update deferred for {:.1f}s by the documented dispatch rate limit".format(sn, wait))
                return False
            if self.unverified_slots.get(sn) == slots and await self._confirm_unverified(sn, slots):
                ok = True
            else:
                if sn not in self.control_enabled:
                    if not await self.set_control_verified(sn, GROWATT_CONTROL_ON):
                        minutes = self._note_write_failure(sn, slots)
                        self.log("Warn: Growatt {} could not confirm enable_control 1 ({}), retrying in {} minute(s)".format(sn, self.last_api_error or self.last_write_outcome, minutes))
                        return False
                    self.control_enabled.add(sn)
                    self.control_claimed.add(sn)
                ok = await self.write_slots_verified(sn, slots)
            if ok:
                self.applied_slots[sn] = list(slots)
                self.unverified_slots.pop(sn, None)
                self._clear_write_failure(sn)
                self.log("Info: Growatt wrote {} UTC charge/discharge slot(s) for {}, confirmed by read-back".format(len(slots), sn))
            else:
                # What the device holds is now unknown, so the known table is dropped
                self.applied_slots.pop(sn, None)
                outcome = self.last_write_outcome
                if outcome == GROWATT_WRITE_UNKNOWN:
                    # The write may well have applied: the retry reads before writing again
                    self.unverified_slots[sn] = list(slots)
                else:
                    # The device answered with a different table, so it may have dropped
                    # enable_control as well: the retry sends that again before the table
                    self.unverified_slots.pop(sn, None)
                    self.control_enabled.discard(sn)
                minutes = self._note_write_failure(sn, slots)
                self.log(
                    "Warn: Growatt {} slot table not confirmed after {} attempts ({}: {}), retrying in {} minute(s)".format(
                        sn, GROWATT_WRITE_ATTEMPTS, outcome, "read-back failed, it may have applied" if outcome == GROWATT_WRITE_UNKNOWN else "the device holds a different table", minutes
                    )
                )
        await self.save_state()
        return ok

    async def refresh_readback(self, sn):
        """Read the slot table the device actually holds and adopt it as the known table.

        The read shares the dispatch pacing, so it waits for the next run when a write has
        just gone out (which then confirms that write). An answer that cannot be parsed
        leaves the last written table in place, as the device's state is then still unknown.
        """
        async with self._write_lock:
            if self._pacing_wait(sn) > 0:
                return False
            ok, data = await self._paced(sn, lambda: self.read_dispatch(sn, GROWATT_SET_TIME_SLOTS))
        self.mark_refreshed("readback:{}".format(sn))
        if not ok:
            self.log("Warn: Growatt {} slot table read-back failed: {}".format(sn, self.last_api_error))
            return False
        slots = parse_dispatch_slots(data)
        if slots is None:
            self.log("Warn: Growatt {} slot table read-back was not understood, keeping the last written table: {}".format(sn, json.dumps(self.redact(data), default=str)[:1000]))
            return False
        known = self.applied_slots.get(sn)
        if known is not None and normalize_slots(known) != slots:
            self.log("Warn: Growatt {} holds {} slot(s) {} that differ from the {} last written {}; Predbat rewrites them on its next schedule apply".format(sn, len(slots), slots, len(known), known))
        self.readback_slots[sn] = slots
        self.applied_slots[sn] = slots
        await self.save_state()
        return True

    async def release_control(self, sn):
        """Hand the battery schedule back to the inverter's own settings.

        Called while Predbat is read-only, or growatt_control_enable is off, for a device it
        still holds OpenAPI control of. The release clears the slot table, confirmed by read-back:
        enable_control 0 is not sent, as a live SPH refused slot commands while it was 0 and
        reverted to 1 by itself within minutes, so it neither releases nor stays released. With no
        slots the inverter follows its own settings. Paced like any dispatch; a release that cannot
        be confirmed is retried after GROWATT_RELEASE_RETRY minutes rather than every run.
        """
        if sn not in self.control_claimed:
            return True
        if not self.tier_expired("release:{}".format(sn), GROWATT_RELEASE_RETRY):
            return False
        async with self._write_lock:
            if self._pacing_wait(sn) > 0:
                return False
            ok = await self.write_slots_verified(sn, [])
        if not ok:
            self.mark_refreshed("release:{}".format(sn))
            self.log("Warn: Growatt {} could not confirm its slot table was cleared to release control ({}), retrying in {} minutes".format(sn, self.last_write_outcome, GROWATT_RELEASE_RETRY))
            return False
        self.control_claimed.discard(sn)
        self.control_enabled.discard(sn)
        self.applied_slots[sn] = []
        self.unverified_slots.pop(sn, None)
        self._clear_write_failure(sn)
        self.log("Info: Growatt {} slot table cleared, OpenAPI control released; the inverter now follows its own settings".format(sn))
        await self.save_state()
        return True

    async def save_state(self):
        """Persist the control state a restart must not forget: the known slot tables and who holds control."""
        storage = self.storage
        if storage is None:
            return
        payload = {"applied_slots": self.applied_slots, "control_active": sorted(self.control_active), "control_claimed": sorted(self.control_claimed)}
        try:
            await storage.save(GROWATT_STORAGE_MODULE, GROWATT_CACHE_CONTROL, payload)
        except Exception as error:
            self.log("Warn: Growatt could not save control state: {}".format(error))

    async def restore_state(self):
        """Restore the saved control state once; a storage failure is retried on the next run."""
        storage = self.storage
        if storage is None:
            self._state_restored = True
            return
        try:
            data = await storage.load(GROWATT_STORAGE_MODULE, GROWATT_CACHE_CONTROL)
        except Exception as error:
            self.log("Warn: Growatt could not load control state: {}".format(error))
            return
        data = data if isinstance(data, dict) else {}
        for sn, slots in (data.get("applied_slots") or {}).items():
            if isinstance(slots, list) and sn not in self.applied_slots:
                self.applied_slots[str(sn)] = normalize_slots(slots)
        self.control_active |= {str(sn) for sn in data.get("control_active") or []}
        self.control_claimed |= {str(sn) for sn in data.get("control_claimed") or []}
        self._state_restored = True

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
        # ComponentBase.start() records this already; the CLI harness calls run() directly
        self.component_loop = asyncio.get_running_loop()
        if not await self.check_and_refresh_oauth_token():
            return False
        if not self.access_token:
            self.log("Warn: Growatt needs an OAuth access token from Predbat.com")
            return False
        if not self._state_restored:
            await self.restore_state()
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
            try:
                if self._is_read_only() or not self.control_enable:
                    await self.release_control(sn)
                else:
                    await self._reconcile_control(sn)
                # After the write, so a table just sent is confirmed on the next run rather
                # than the read pushing the write back by one pacing interval.
                if self.tier_expired("readback:{}".format(sn), GROWATT_TTL_READBACK):
                    await self.refresh_readback(sn)
            except Exception as error:
                self.log("Warn: Growatt control update failed for {}: {}".format(sn, error))
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


# ----------------------------------------------------------------------
# Command-line diagnostics
#
#   python3 growatt.py --config apps.yaml          read the growatt_* settings from apps.yaml
#   python3 growatt.py --key <token> ...           or give them individually
#
# Runs one read-only cycle of run() with automatic configuration on, then prints what the
# component found. Control is off, so nothing is written to the inverter; the slot table is
# read back. Run it from apps/predbat.
# ----------------------------------------------------------------------

# Command-line fields, mapped to the apps.yaml key --config fills each from. user_id is the shared
# Predbat.com instance id OAuthMixin's refresh sends, not a Growatt-specific setting.
GROWATT_CLI_CONFIG_KEYS = {
    "key": "growatt_key",
    "auth_method": "growatt_auth_method",
    "token_expires_at": "growatt_token_expires_at",
    "token_hash": "growatt_token_hash",
    "inverter_sn": "growatt_inverter_sn",
    "base_url": "growatt_base_url",
    "battery_rate_max": "growatt_battery_rate_max",
    "user_id": "user_id",
    # Present in a Predbat.com-generated apps.yaml; the token refresh needs both
    "supabase_url": "supabase_url",
    "supabase_key": "supabase_key",
}


def merge_growatt_cli_args(cli, config):
    """Fill any command-line field left empty from an apps.yaml pred_bat section.

    An explicit command-line value always wins, and an empty apps.yaml value fills nothing. An
    unquoted ISO timestamp loads from YAML as a datetime, which OAuthMixin cannot parse, so it is
    turned back into ISO text. Returns (merged, used): used names the apps.yaml keys taken.
    """
    merged = {field: cli.get(field) for field in GROWATT_CLI_CONFIG_KEYS}
    used = []
    for field, config_key in GROWATT_CLI_CONFIG_KEYS.items():
        if merged.get(field) not in (None, "", []):
            continue
        value = config.get(config_key)
        if value in (None, "", []):
            continue
        if isinstance(value, datetime):
            value = value.isoformat()
        merged[field] = value
        used.append(config_key)
    return merged, used


def _cli_slot_text(slots):
    """Format a UTC slot table as one short line, e.g. "01:00-03:00 +50%, 17:00-19:00 -20%"."""
    if not slots:
        return "empty"
    return ", ".join("{}-{} {:+d}%".format(slot["startTime"], slot["endTime"], slot["percentage"]) for slot in slots)


def _cli_print_device(client, sn):
    """Print what one inverter reported, for checking against the Growatt app."""
    detail = client.device_detail.get(sn, {})
    values = client.device_values.get(sn, {})
    energy = client.device_energy.get(sn, {})
    print("\n=== {} ===".format(sn))
    print("  model {}   battery {}   firmware {}   status {}".format(detail.get("model", "?"), detail.get("batteryModel", "?"), detail.get("communicationVersion", "?"), detail.get("status", "?")))
    if "soc" in values:
        print("  SoC {:.1f}%   battery {:.0f} W   grid {:.0f} W   PV {:.0f} W   load {:.0f} W".format(values["soc"], values.get("battery_power", 0), values.get("grid_power", 0), values.get("pv_power", 0), values.get("load_power", 0)))
        print("  (Predbat signs: battery positive = discharging, grid negative = importing)")
    else:
        print("  no telemetry")
    if energy:
        print("  today: " + "   ".join("{} {:.2f} kWh".format(leaf.replace("_today", "").replace("_", " "), value) for leaf, value in sorted(energy.items())))
    print("  battery capacity {:.2f} kWh   battery rate max {:.0f} W   inverter limit {:.0f} W   discharge cut-off {}%".format(client.battery_capacity(sn), client.battery_rate_max(sn), client.inverter_limit(sn), detail.get("dischargeCutOffSOC", "?")))
    if sn in client.readback_slots:
        print("  slot table on the device (UTC): {}".format(_cli_slot_text(client.readback_slots[sn])))
    else:
        print("  slot table on the device: not read back - see the Warn: lines above")


# Write tests (--write-test): what each should make the battery do, printed before it is written.
GROWATT_CLI_EXPECTATION = {
    "charge": "the battery CHARGES (Predbat battery power negative) at about the requested power",
    "export": "the battery DISCHARGES (Predbat battery power positive), covering the house and exporting the rest",
    "hold": "the battery neither charges nor discharges (a 0% charge slot); the house runs from the grid",
}
GROWATT_CLI_SCENARIOS = ("charge", "export", "hold")
GROWATT_CLI_DEFAULT_POWER_W = 1000
# Longest watch per test. A live SPH uploads telemetry about every five minutes, so this allows
# two fresh readings after the settle time; the watch ends early once it has them.
GROWATT_CLI_DEFAULT_MINUTES = 12
# Readings this soon after a write are shown but not judged: the cloud relays the slot table to
# the inverter and the inverter ramps, so the first reading can still show the old behaviour.
GROWATT_CLI_SETTLE_SECONDS = 60
# Fresh, settled readings that end a test's watch early.
GROWATT_CLI_FRESH_NEEDED = 2
# A write test refuses to start when the latest telemetry upload is older than this many minutes:
# uploads come about every five, so older means the inverter or datalogger is not reporting.
GROWATT_CLI_MAX_TELEMETRY_AGE = 10
# Battery power below this many watts counts as idle when judging.
GROWATT_CLI_THRESHOLD_W = 100
# getDeviceData's endpoint page allows one call a minute per device.
GROWATT_CLI_SAMPLE_SECONDS = 60


def _build_cli_client(key=None, auth_method="oauth", token_expires_at=None, token_hash=None, inverter_sn=None, base_url=None, battery_rate_max=None, user_id=None, supabase_url=None, supabase_key=None, trace=True):
    """Construct a GrowattAPI on a MockBase for a standalone run, with control off.

    supabase_url/supabase_key go into the environment, where OAuthMixin's refresh reads them;
    user_id goes into the base args, where it reads the instance id. Control stays off so run()
    and the reconcile loop never write: the write tests below write directly, after asking.
    """
    from mock_base import MockBase

    if supabase_url:
        os.environ["SUPABASE_URL"] = supabase_url
    if supabase_key:
        os.environ["SUPABASE_KEY"] = supabase_key

    kwargs = {"key": key or "", "auth_method": auth_method or "oauth", "token_expires_at": token_expires_at, "token_hash": token_hash or "", "inverter_sn": inverter_sn, "automatic": True, "control_enable": False}
    if base_url:
        kwargs["base_url"] = base_url
    if battery_rate_max:
        kwargs["battery_rate_max"] = battery_rate_max
    client = GrowattAPI(MockBase(user_id=user_id), **kwargs)
    client.api_debug = trace
    return client


async def test_growatt_api(
    key=None,
    auth_method="oauth",
    token_expires_at=None,
    token_hash=None,
    inverter_sn=None,
    base_url=None,
    battery_rate_max=None,
    user_id=None,
    supabase_url=None,
    supabase_key=None,
    trace=True,
    write_test=None,
    power=GROWATT_CLI_DEFAULT_POWER_W,
    minutes=GROWATT_CLI_DEFAULT_MINUTES,
    confirm=None,
    snapshot_dir=".",
    settle_seconds=GROWATT_CLI_SETTLE_SECONDS,
):
    """Run one read-only cycle of the Growatt component with automatic configuration on.

    run() discovers, polls, binds Predbat's settings (printed as "Set arg" lines) and reads the
    slot table back, but writes nothing. With write_test ("charge", "export", "hold" or "all")
    it then runs those live tests on the one inverter (see growatt_write_test()).

    Returns True only when the cycle, and any write test, passed.
    """
    client = _build_cli_client(key, auth_method, token_expires_at, token_hash, inverter_sn, base_url, battery_rate_max, user_id, supabase_url, supabase_key, trace)

    print("Calling run() once with automatic configuration on (read-only: control is off, nothing is written to the inverter)...")
    ok = await client.run(seconds=0, first=True)
    await client.final()
    if not ok:
        if client.oauth_failed:
            print("\nFAILED: the Growatt OAuth token needs re-authorising - reconnect Growatt in Predbat.com")
        elif not client.access_token:
            print("\nFAILED: no OAuth access token - pass --key (or GROWATT_KEY), or --token-hash with --user-id, --supabase-url and --supabase-key so it can be refreshed")
        elif client.discovery_ok is False:
            print("\nFAILED: the device list call failed{}".format(": {}".format(client.last_api_error) if client.last_api_error else ""))
        elif not client.device_list:
            print("\nFAILED: no authorised battery inverter found{}".format(" matching serial {}".format(inverter_sn) if inverter_sn else ""))
        else:
            print("\nFAILED: discovery found {}, but the first telemetry poll came back empty - see the Warn: lines above".format(", ".join(client.device_list)))
        return False
    for sn in client.device_list:
        _cli_print_device(client, sn)
    print("\nThe 'Set arg' lines above are what growatt_automatic: true would put in apps.yaml.")
    if not write_test:
        print("PASSED")
        return True
    if len(client.device_list) != 1:
        print("\nFAILED: the write tests drive one inverter, but this token reaches {} - choose one with --serial".format(", ".join(client.device_list)))
        return False
    scenarios = list(GROWATT_CLI_SCENARIOS) if write_test == "all" else [write_test]
    return await growatt_write_test(client, client.device_list[0], scenarios, power, minutes, confirm or (lambda prompt: True), snapshot_dir, settle_seconds=settle_seconds)


def build_cli_test_slots(scenario, now_utc, minutes, percentage):
    """Return the one-slot table a write test sends: from this UTC minute for minutes + 2.

    The extra two minutes keep the slot open while the last reading is taken. Charge uses
    +percentage, export -percentage and hold 0%. Slots cannot cross UTC midnight, so a window
    that would is cut at 23:59; with under five minutes left before midnight it returns None.
    """
    start = now_utc.replace(second=0, microsecond=0)
    end_minutes = start.hour * 60 + start.minute + minutes + 2
    if 24 * 60 - (start.hour * 60 + start.minute) < 5:
        return None
    end_text = "23:59" if end_minutes >= 24 * 60 else "{:02d}:{:02d}".format(end_minutes // 60, end_minutes % 60)
    signed = {"charge": clamp_percent(percentage), "export": -clamp_percent(percentage), "hold": 0}[scenario]
    return [{"startTime": start.strftime("%H:%M"), "endTime": end_text, "percentage": signed}]


def cli_slot_end(built_utc, slots):
    """Return the UTC datetime a write test's slot ends, from the time it was built and its endTime.

    build_cli_test_slots() never crosses UTC midnight, so the end is on the same UTC day - and it
    may be earlier than the watch would run, when the slot was cut at 23:59.
    """
    hour, minute = (int(part) for part in slots[-1]["endTime"].split(":"))
    return built_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)


def classify_cli_reading(power, sample_utc, written_utc, slot_end, seen):
    """Decide whether one telemetry reading is judged, returning (judged, label for the poll line).

    Only a new upload taken after the write and before the slot ends counts: an older one shows
    the battery before the write, and a later one the battery after the slot has stopped.
    """
    if power is None or sample_utc is None:
        return False, "no reading"
    if sample_utc <= written_utc:
        return False, "old reading from {}".format(sample_utc.strftime("%H:%M:%S"))
    if sample_utc in seen:
        return False, "same reading as the last poll"
    if sample_utc > slot_end:
        return False, "reading from {}, after the slot ended - not judged".format(sample_utc.strftime("%H:%M:%S"))
    return True, "new reading, taken {:.0f}s after the write".format((sample_utc - written_utc).total_seconds())


def judge_cli_window(scenario, samples, settle_seconds=GROWATT_CLI_SETTLE_SECONDS, threshold=GROWATT_CLI_THRESHOLD_W):
    """Judge a write test from its telemetry samples, returning (verdict, reason).

    samples are (seconds_since_write, battery_power) with Predbat's sign (positive = discharging).
    Only samples at or after settle_seconds count. verdict is "PASS", "FAIL" or "INCONCLUSIVE"
    when no sample counted.
    """
    judged = [power for elapsed, power in samples if elapsed >= settle_seconds]
    if not judged:
        return "INCONCLUSIVE", "no reading was taken after the {}s settle time".format(settle_seconds)
    checks = {
        "charge": (lambda power: power <= -threshold, "charging at {} W or more".format(threshold)),
        "export": (lambda power: power >= threshold, "discharging at {} W or more".format(threshold)),
        "hold": (lambda power: abs(power) < threshold, "within {} W of idle".format(threshold)),
    }
    check, wanted = checks[scenario]
    bad = [power for power in judged if not check(power)]
    readings = ", ".join("{:.0f} W".format(power) for power in judged)
    if bad:
        return "FAIL", "battery not {} in every settled reading ({})".format(wanted, readings)
    return "PASS", "battery {} in every settled reading ({})".format(wanted, readings)


def _parse_utc_time(value):
    """Parse getDeviceData's utcTime ("2026-11-11 21:06:37") as a UTC datetime, or None."""
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


async def _cli_sample(client, sn):
    """Poll telemetry for one device, returning (Predbat battery power, sample utcTime), either None when unknown."""
    code, data = await client._post("device_data", {"deviceSn": sn})
    if code != GROWATT_CODE_OK or not client._apply_telemetry(sn, data):
        return None, None
    return client.device_values[sn].get("battery_power"), _parse_utc_time(data.get("utcTime"))


async def _cli_read_baseline(client, sn):
    """Read the device's enable_control value and raw slot table, or (None, None) when either cannot be read."""
    ok, enable = await client.read_dispatch_retry(sn, GROWATT_SET_ENABLE_CONTROL)
    if not ok or not isinstance(enable, (int, float)):
        print("Could not read enable_control ({}): {}".format(client.last_api_error, enable))
        return None, None
    ok, slots = await client.read_dispatch_retry(sn, GROWATT_SET_TIME_SLOTS)
    if not ok or not isinstance(slots, list) or parse_dispatch_slots(slots) is None:
        print("Could not read the slot table in a shape Predbat can restore ({}): {}".format(client.last_api_error, slots))
        return None, None
    return int(enable), slots


async def _cli_restore_table(client, sn, slots):
    """Write back the saved slot table, confirmed by read-back with retries; return True once it reads as saved.

    An empty saved table goes out as 16 unused slots, since Growatt refuses an empty list.
    """
    table_ok = await client.write_slots_verified(sn, slots)
    print("  restore slot table: {} - {}".format("OK" if table_ok else "FAILED", "device holds {}".format(_cli_slot_text(normalize_slots(slots))) if table_ok else "see the Warn: lines above"))
    return table_ok


async def _cli_restore_enable(client, sn, enable):
    """Put enable_control back to its saved value if it differs, confirmed by read-back with retries."""
    ok, now_enable = await client.read_dispatch_retry(sn, GROWATT_SET_ENABLE_CONTROL)
    enable_ok = (ok and now_enable == enable) or await client.set_control_verified(sn, enable)
    print("  restore enable_control: {} - {}".format("OK" if enable_ok else "FAILED", "device reads {}".format(enable) if enable_ok else "see the Warn: lines above"))
    return enable_ok


async def _cli_restore(client, sn, enable, slots):
    """Restore both the slot table and enable_control, returning True when both read back as saved."""
    table_ok = await _cli_restore_table(client, sn, slots)
    enable_ok = await _cli_restore_enable(client, sn, enable)
    return table_ok and enable_ok


def save_cli_snapshot(folder, sn, enable, slots):
    """Save the pre-test enable_control value and raw slot table to a JSON file and return its path."""
    path = os.path.join(folder, "growatt_snapshot_{}_{}.json".format(sn, datetime.now().strftime("%Y%m%d_%H%M%S")))
    with open(path, "w") as handle:
        json.dump({"deviceSn": sn, "enable_control": enable, "time_slot_charge_discharge": slots}, handle, indent=2)
    return path


def load_cli_snapshot(path):
    """Load a snapshot written by save_cli_snapshot(), returning (sn, enable, slots); raises ValueError when malformed."""
    with open(path, "r") as handle:
        data = json.load(handle)
    sn, enable, slots = data.get("deviceSn"), data.get("enable_control"), data.get("time_slot_charge_discharge")
    if not sn or not isinstance(enable, int) or not isinstance(slots, list):
        raise ValueError("{} is not a Growatt snapshot".format(path))
    return sn, enable, slots


async def growatt_write_test(client, sn, scenarios, power, minutes, confirm, snapshot_dir, settle_seconds=GROWATT_CLI_SETTLE_SECONDS, sample_seconds=GROWATT_CLI_SAMPLE_SECONDS):
    """Write each scenario's slot table, read it back, watch the battery, then restore what the device held.

    The device's enable_control value and raw slot table are read first and saved to a snapshot
    file; refusing to start when they cannot be read, as there would be nothing to restore. Each
    scenario is restored before the next starts, and the final restore runs whatever happens.
    Returns True only when every scenario passed and every restore read back as saved.
    """
    rate_max = client.battery_rate_max(sn)
    if rate_max <= 0:
        print("\nFAILED: {} reports no battery power rating, so a power cannot be turned into a percentage - pass --battery-rate-max".format(sn))
        return False
    percentage = clamp_percent(power / rate_max * 100.0)
    print("\nWrite tests on {}: {} at {} W ({}% of the {:.0f} W battery rating), watching up to {} minutes each".format(sn, ", ".join(scenarios), power, percentage, rate_max, minutes))
    print("Make sure no running Predbat instance is controlling this inverter, or it will overwrite the test slots.")

    # Pre-flight: a device whose telemetry has stopped arriving cannot be judged, and in a live
    # run that went with settings reads failing for over ten minutes, so a restore could not be
    # confirmed either. Refuse rather than write to it.
    _, last_upload = await _cli_sample(client, sn)
    age_minutes = (datetime.now(timezone.utc) - last_upload).total_seconds() / 60 if last_upload else None
    if age_minutes is None or age_minutes > GROWATT_CLI_MAX_TELEMETRY_AGE:
        print(
            "\nFAILED: the latest telemetry is {} - the inverter or its datalogger is not reporting to Growatt, so nothing was written. Check it is online in the Growatt app and try again".format(
                "missing" if age_minutes is None else "{:.0f} minutes old (from {} UTC)".format(age_minutes, last_upload.strftime("%H:%M:%S"))
            )
        )
        return False
    enable, saved_slots = await _cli_read_baseline(client, sn)
    if enable is None:
        print("\nFAILED: the current settings could not be read, so they could not be restored - nothing was written")
        return False
    path = save_cli_snapshot(snapshot_dir, sn, enable, saved_slots)
    print("Current settings: enable_control {}, slot table {}".format(enable, _cli_slot_text(parse_dispatch_slots(saved_slots))))
    print("Saved them to {} - if this run is killed, put them back with --restore {}".format(path, path))
    if not confirm("Write test slots to {} now? [y/N] ".format(sn)):
        print("Cancelled - nothing was written")
        return True

    results = []
    # Set while the device holds something the test wrote and the restore has not yet undone, so
    # the finally block puts it back however the run ends (a failure, an exception or Ctrl-C).
    table_dirty = False
    enable_dirty = False
    polls_per_test = int(minutes * 60 // sample_seconds) + 1
    try:
        if enable != GROWATT_CONTROL_ON:
            enable_dirty = True
            if not await client.set_control_verified(sn, GROWATT_CONTROL_ON):
                print("\nFAILED: enable_control 1 could not be confirmed by read-back")
                results.append(("enable_control 1", "FAIL"))
                return False
        for scenario in scenarios:
            print("\n--- {} ---  expect: {}".format(scenario, GROWATT_CLI_EXPECTATION[scenario]))
            built_utc = datetime.now(timezone.utc)
            slots = build_cli_test_slots(scenario, built_utc, minutes, percentage)
            if slots is None:
                print("  skipped: too close to UTC midnight for a slot that does not cross it")
                results.append((scenario, "INCONCLUSIVE"))
                continue
            print("  writing {}".format(_cli_slot_text(slots)))
            table_dirty = True
            if not await client.write_slots_verified(sn, slots):
                print("  the write could not be confirmed by read-back after {} attempts".format(GROWATT_WRITE_ATTEMPTS))
                results.append((scenario, "FAIL"))
            else:
                written_utc = datetime.now(timezone.utc)
                slot_end = cli_slot_end(built_utc, slots)
                print("  confirmed by read-back. Growatt uploads telemetry about every 5 minutes, so only readings taken after the write and before the slot ends ({} UTC) are judged".format(slot_end.strftime("%H:%M")))
                samples = []
                seen = set()
                for index in range(polls_per_test):
                    if index:
                        if datetime.now(timezone.utc) >= slot_end:
                            print("  the slot has ended, so watching stops here")
                            break
                        await asyncio.sleep(sample_seconds)
                    power_now, sample_utc = await _cli_sample(client, sn)
                    values = client.device_values.get(sn, {})
                    fresh, age = classify_cli_reading(power_now, sample_utc, written_utc, slot_end, seen)
                    print(
                        "  poll {:2}  SoC {}%  battery {} W  grid {} W  load {} W  ({})".format(index + 1, values.get("soc", "?"), "?" if power_now is None else "{:.0f}".format(power_now), values.get("grid_power", "?"), values.get("load_power", "?"), age)
                    )
                    if fresh:
                        seen.add(sample_utc)
                        samples.append(((sample_utc - written_utc).total_seconds(), power_now))
                        if sum(1 for elapsed, _ in samples if elapsed >= settle_seconds) >= GROWATT_CLI_FRESH_NEEDED:
                            break
                verdict, reason = judge_cli_window(scenario, samples, settle_seconds=settle_seconds)
                print("  {}: {}".format(verdict, reason))
                results.append((scenario, verdict))
            print("  clearing: restoring the saved slot table")
            if await _cli_restore_table(client, sn, saved_slots):
                table_dirty = False
            else:
                results.append(("restore after " + scenario, "FAIL"))
                break
    finally:
        if table_dirty:
            print("\nStopped before the slot table was restored - restoring it now")
            if not await _cli_restore_table(client, sn, saved_slots):
                results.append(("final slot table restore", "FAIL"))
        if enable_dirty:
            print("\nRestoring enable_control to {}".format(enable))
            if not await _cli_restore_enable(client, sn, enable):
                results.append(("enable_control restore", "FAIL"))

    print("\nSummary:")
    for name, verdict in results:
        print("  {:22} {}".format(name, verdict))
    passed = all(verdict == "PASS" for _, verdict in results) and len(results) == len(scenarios)
    print("PASSED" if passed else "NOT PASSED - see above")
    return passed


async def restore_growatt_snapshot(client, path):
    """Write back a snapshot file a write test saved, and report whether it read back as saved."""
    sn, enable, slots = load_cli_snapshot(path)
    print("Restoring {} from {}: enable_control {}, slot table {}".format(sn, path, enable, _cli_slot_text(parse_dispatch_slots(slots))))
    ok = await _cli_restore(client, sn, enable, slots)
    print("PASSED" if ok else "FAILED")
    return ok


def main():  # pragma: no cover
    """Command-line entry point: one read-only Growatt cycle from apps.yaml or individual settings."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Growatt OpenAPI diagnostics: run one read-only cycle of the component with automatic configuration on, and print what it found.",
        epilog="Run from apps/predbat. The access token can also come from GROWATT_KEY or --config, which keep it out of shell history.",
    )
    parser.add_argument("--config", help="Read the growatt_* settings, user_id, supabase_url and supabase_key from an apps.yaml, resolving !secret as Predbat does; any option below overrides the file")
    parser.add_argument("--key", help="Growatt OAuth access token (growatt_key)")
    parser.add_argument("--auth-method", default=None, help="Authentication mode (growatt_auth_method), default oauth")
    parser.add_argument("--token-expires-at", help="Access token expiry as an ISO timestamp (growatt_token_expires_at)")
    parser.add_argument("--token-hash", help="Predbat.com refresh handle (growatt_token_hash)")
    parser.add_argument("--serial", action="append", help="Only this inverter serial (growatt_inverter_sn); repeat for several")
    parser.add_argument("--base-url", help="Regional OpenAPI base (growatt_base_url), default {}".format(GROWATT_BASE_URL))
    parser.add_argument("--battery-rate-max", type=float, help="Battery rate override in W (growatt_battery_rate_max)")
    parser.add_argument("--user-id", help="Predbat.com instance id, needed to refresh the token")
    parser.add_argument("--supabase-url", help="Supabase URL for the token refresh (or SUPABASE_URL)")
    parser.add_argument("--supabase-key", help="Supabase service key for the token refresh (or SUPABASE_KEY)")
    parser.add_argument("--no-trace", action="store_true", help="Stop printing every API request and response")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--write-test",
        choices=list(GROWATT_CLI_SCENARIOS) + ["all"],
        help="After the read-only cycle, write a slot starting now (charge, export, hold or all three in turn), read it back, watch the battery, then restore what the inverter held before",
    )
    mode.add_argument("--restore", metavar="SNAPSHOT", help="Write back a snapshot file a write test saved")
    parser.add_argument("--power", type=int, default=GROWATT_CLI_DEFAULT_POWER_W, help="Charge or export power for the write tests in W (default {})".format(GROWATT_CLI_DEFAULT_POWER_W))
    parser.add_argument("--minutes", type=int, default=GROWATT_CLI_DEFAULT_MINUTES, help="How long to watch each write test, in minutes (default {})".format(GROWATT_CLI_DEFAULT_MINUTES))
    parser.add_argument("--snapshot-dir", default=".", help="Where a write test saves the settings it will restore (default: the current directory)")
    parser.add_argument("--yes", action="store_true", help="Do not ask before writing")
    args = parser.parse_args()
    if args.minutes < 1:
        parser.error("--minutes must be at least 1")
    if args.power <= 0:
        parser.error("--power must be positive")

    cli = {
        "key": args.key,
        "auth_method": args.auth_method,
        "token_expires_at": args.token_expires_at,
        "token_hash": args.token_hash,
        "inverter_sn": args.serial,
        "base_url": args.base_url,
        "battery_rate_max": args.battery_rate_max,
        "user_id": args.user_id,
        "supabase_url": args.supabase_url,
        "supabase_key": args.supabase_key,
    }
    merged = dict(cli)
    if args.config:
        import yaml
        from utils import load_apps_yaml

        try:
            config, _ = load_apps_yaml(args.config)
        except (yaml.YAMLError, KeyError, OSError, ValueError) as error:
            parser.error("could not read Growatt settings from {}: {}".format(args.config, error))
        merged, used = merge_growatt_cli_args(cli, config or {})
        print("Read from {}: {}".format(args.config, ", ".join(used) if used else "nothing - no growatt_ keys found"))
    if not merged.get("key"):
        merged["key"] = os.environ.get("GROWATT_KEY")
    if not merged.get("key") and not merged.get("token_hash"):
        parser.error("no credential: pass --key (or set GROWATT_KEY), --token-hash, or --config pointing at an apps.yaml holding growatt_key")

    if args.restore:
        client = _build_cli_client(trace=not args.no_trace, **merged)
        try:
            ok = asyncio.run(restore_growatt_snapshot(client, args.restore))
        except (OSError, ValueError) as error:
            parser.error(str(error))
        sys.exit(0 if ok else 1)

    def confirm(prompt):
        """Ask before writing, unless --yes was given."""
        if args.yes:
            return True
        try:
            return input(prompt).strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    ok = asyncio.run(test_growatt_api(trace=not args.no_trace, write_test=args.write_test, power=args.power, minutes=args.minutes, confirm=confirm, snapshot_dir=args.snapshot_dir, **merged))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":  # pragma: no cover
    main()
