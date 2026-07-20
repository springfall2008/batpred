# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# DEYE Cloud API Library
# -----------------------------------------------------------------------------

"""DEYE Cloud API integration for Predbat.

Registers each DEYE battery inverter as a ``DeyeCloud`` Predbat inverter,
publishing monitoring sensors and Fox-style schedule control entities. Predbat
drives those entities through the generic Inverter class; this module derives
the DEYE work mode internally and applies a combined ``strategy_dynamic_control``
payload. Supports HA add-on (self-managed token) and Predbat.com SaaS (injected
token) auth.
"""

import hashlib
import asyncio
import aiohttp
import argparse
import json
import os
from datetime import datetime
from component_base import ComponentBase
from oauth_mixin import OAuthMixin
from deye_const import DEYE_BASE_URLS, DEYE_ENDPOINTS, DEYE_TIMEOUT, DEYE_RETRIES, DEYE_TELEMETRY_KEYS, DEYE_LATEST_BODY_KEY, DEYE_WORKMODE, FREEZE_EXPORT_SOC, TOU_FIELD, TOU_SLOT_COUNT, TOU_FILLER_TIMES, DEYE_ORDER_MAX_POLLS, CONFIG_BATTERY_KEYS


class DeyeAPI(ComponentBase, OAuthMixin):
    """DEYE Cloud API component."""

    def initialize(self, app_id="", app_secret="", username="", password="", data_center="eu", company_id="", auth_method="app_credentials", token_expires_at=None, token_hash="", inverter_sn=None, automatic=False, automatic_ignore_pv=False, **kwargs):
        """Initialise the DEYE component from its resolved config args.

        ComponentBase.__init__ calls initialize(**kwargs); the Components
        registry has already resolved each arg from its deye_* config key and
        passes it BY ARG NAME (e.g. data_center <- deye_data_center), exactly
        like fox/enphase/solax/teslemetry. Consume the kwargs directly — do NOT
        re-derive with get_arg("data_center"): that bare name is not in
        apps.yaml (the key is deye_data_center), so it would always return the
        default and silently pin every setting.
        """
        self.log("Info: DeyeAPI initialising")
        self.app_id = app_id
        self.app_secret = app_secret
        self.username = username
        self.password = password
        self.data_center = data_center or "eu"
        self.company_id = company_id
        self.automatic = automatic
        self.automatic_ignore_pv = automatic_ignore_pv
        self.inverter_sn_filter = inverter_sn if isinstance(inverter_sn, list) else ([inverter_sn] if inverter_sn else [])
        self.device_list = []
        self.device_values = {}
        self.device_battery_config = {}
        self.local_schedule = {}
        self.pending_orders = {}
        self.order_poll_count = {}
        self.applied_payload = {}
        self.control_active = set()
        self.cached_values = {}
        self._init_oauth(
            auth_method=auth_method,
            key=app_secret or token_hash,
            token_expires_at=token_expires_at,
            provider_name="deye",
        )
        # _init_oauth() resets token_hash to "" (see oauth_mixin.py) so the configured value
        # must be applied AFTER it, exactly as fox.py does — otherwise a configured hash is
        # silently discarded and the Predbat.com SaaS dedup keyed on it breaks.
        self.token_hash = token_hash

    @property
    def base_url(self):
        """Return the OpenAPI base URL for the configured data centre."""
        return DEYE_BASE_URLS.get(self.data_center, DEYE_BASE_URLS["eu"])

    @staticmethod
    def _sha256(password):
        """Return the lower-case hex SHA-256 of a password."""
        return hashlib.sha256(password.encode("utf-8")).hexdigest().lower()

    @staticmethod
    def _login_payload(login):
        """Choose the DEYE login key: email if it looks like one, else username."""
        login = (login or "").strip()
        return {"email": login} if "@" in login else {"username": login}

    def _auth_headers(self):
        """Return JSON + Bearer auth headers for a DEYE request."""
        return {"Content-Type": "application/json", "Authorization": f"Bearer {self.access_token}"}

    async def fetch_token(self):
        """Fetch an access token using app credentials (app_credentials mode)."""
        url = f"{self.base_url}{DEYE_ENDPOINTS['token']}?appId={self.app_id}"
        body = {"appSecret": self.app_secret, "password": self._sha256(self.password), **self._login_payload(self.username)}
        if self.company_id:
            body["companyId"] = str(self.company_id)
        timeout = aiohttp.ClientTimeout(total=DEYE_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=body) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: DEYE token fetch failed: {e}")
            return False
        if not data.get("success"):
            self.log(f"Warn: DEYE token rejected: {data.get('msg', 'unknown')}")
            return False
        self.access_token = data.get("accessToken")
        return True

    async def _post(self, endpoint_key, body):
        """POST to a DEYE endpoint with retry and 401-refresh. Returns parsed JSON or raises."""
        url = f"{self.base_url}{DEYE_ENDPOINTS[endpoint_key]}"
        timeout = aiohttp.ClientTimeout(total=DEYE_TIMEOUT)
        last_err = None
        for attempt in range(DEYE_RETRIES):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, headers=self._auth_headers(), json=body) as resp:
                        if resp.status in (401, 403):
                            self.log(f"Warn: DEYE 401/403 on {endpoint_key}, attempt {attempt + 1}")
                            if await self.handle_oauth_401():
                                continue
                            if self.auth_method == "app_credentials":
                                self.access_token = None
                                if await self.fetch_token():
                                    continue
                            raise RuntimeError(f"DEYE auth failed on {endpoint_key}")
                        resp.raise_for_status()
                        return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = e
                self.log(f"Warn: DEYE network error on {endpoint_key} attempt {attempt + 1}: {e}")
                await asyncio.sleep(2**attempt)
        raise RuntimeError(f"DEYE POST failed after {DEYE_RETRIES} retries on {endpoint_key}: {last_err}")

    async def get_station_ids(self):
        """Return station ids visible to the account."""
        data = await self._post("station_list", {})
        if not data.get("success", True):
            self.log(f"Warn: DEYE station/list failed: {data.get('msg', 'unknown')}")
            return []
        stations = data.get("stationList") or []
        return [s.get("id") or s.get("stationId") for s in stations if s.get("id") or s.get("stationId")]

    async def get_device_list(self):
        """Discover battery inverter serials across the account's stations."""
        station_ids = await self.get_station_ids()
        if not station_ids:
            self.log("Warn: DEYE no stations found")
            self.device_list = []
            return []
        devices = []
        page, size = 1, 100
        while True:
            data = await self._post("station_device", {"page": page, "size": size, "stationIds": station_ids})
            if not data.get("success", True):
                self.log(f"Warn: DEYE station/device failed: {data.get('msg', 'unknown')}")
                break
            items = data.get("deviceListItems") or []
            devices.extend(items)
            total = data.get("total")
            if (total is not None and len(devices) >= int(total)) or len(items) < size:
                break
            page += 1
        serials = [x["deviceSn"] for x in devices if x.get("deviceType") == "INVERTER" and x.get("deviceSn")]
        if self.inverter_sn_filter:
            wanted = {s.lower() for s in self.inverter_sn_filter}
            serials = [s for s in serials if s.lower() in wanted]
        self.device_list = serials
        return serials

    @staticmethod
    def _datalist_to_dict(data_list):
        """Flatten a DEYE dataList of {key,value} pairs into a plain dict."""
        out = {}
        for item in data_list or []:
            key = item.get("key")
            if key is not None:
                out[key] = item.get("value")
        return out

    @staticmethod
    def _as_float(value, default=0.0):
        """Best-effort float coercion."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    async def fetch_device_data(self, sn):
        """Fetch and normalise the latest telemetry for one inverter."""
        data = await self._post("device_latest", {DEYE_LATEST_BODY_KEY: [sn]})
        if not data.get("success", True):
            self.log(f"Warn: DEYE device/latest failed for {sn}: {data.get('msg', 'unknown')}")
            return {}
        rows = data.get("deviceDataList") or []
        if not rows:
            return {}
        flat = self._datalist_to_dict(rows[0].get("dataList"))
        result = {name: self._as_float(flat.get(key)) for name, key in DEYE_TELEMETRY_KEYS.items()}
        self.device_values[sn] = result
        return result

    async def fetch_battery_config(self, sn):
        """Fetch and cache battery capability config for one inverter."""
        data = await self._post("config_battery", {"deviceSn": sn})
        if not data.get("success", True):
            self.log(f"Warn: DEYE config/battery failed for {sn}: {data.get('msg', 'unknown')}")
            return {}
        self.device_battery_config[sn] = data
        return data

    def _battery_config_value(self, sn, key, default=0.0):
        """Read a config_battery field for one inverter via CONFIG_BATTERY_KEYS (spike-confirmable), safely coerced."""
        raw = self.device_battery_config.get(sn, {}).get(CONFIG_BATTERY_KEYS[key])
        return self._as_float(raw, default)

    def derive_control_state(self, schedule, current_soc):
        """Map Predbat's schedule intent to a DEYE control state (see design spec table)."""
        reserve = int(schedule.get("reserve", 0))
        charge = schedule.get("charge", {})
        export = schedule.get("export", {})

        if export.get("enable"):
            export_soc = int(export.get("soc", FREEZE_EXPORT_SOC))
            if export_soc >= FREEZE_EXPORT_SOC:
                return {"behaviour": "freeze_export", "work_mode": DEYE_WORKMODE["selling_first"], "grid_charge": False, "solar_sell": True, "slot_soc": FREEZE_EXPORT_SOC, "power": int(export.get("power", 0))}
            return {"behaviour": "export", "work_mode": DEYE_WORKMODE["selling_first"], "grid_charge": False, "solar_sell": True, "slot_soc": export_soc, "power": int(export.get("power", 0))}

        if charge.get("enable"):
            charge_soc = int(charge.get("soc", 0))
            if charge_soc > current_soc and charge_soc > reserve:
                return {"behaviour": "charge", "work_mode": DEYE_WORKMODE["zero_export_load"], "grid_charge": True, "solar_sell": False, "slot_soc": charge_soc, "power": int(charge.get("power", 0))}
            if charge_soc == reserve:
                return {"behaviour": "freeze_charge", "work_mode": DEYE_WORKMODE["zero_export_load"], "grid_charge": True, "solar_sell": False, "slot_soc": reserve, "power": int(charge.get("power", 0))}
            return {"behaviour": "hold_charge", "work_mode": DEYE_WORKMODE["zero_export_load"], "grid_charge": False, "solar_sell": False, "slot_soc": reserve, "power": int(charge.get("power", 0))}

        return {"behaviour": "idle", "work_mode": DEYE_WORKMODE["zero_export_load"], "grid_charge": False, "solar_sell": False, "slot_soc": reserve, "power": 0}

    def _self_use_slot(self, start_time, reserve):
        """Build a self-use TOU slot holding at the reserve SoC."""
        return {TOU_FIELD["time"]: start_time, TOU_FIELD["power"]: 0, TOU_FIELD["soc"]: int(reserve), TOU_FIELD["grid_charge"]: False, TOU_FIELD["generate"]: True}

    def _action_slot(self, start_time, state):
        """Build a TOU slot realising a derived control state."""
        return {TOU_FIELD["time"]: start_time, TOU_FIELD["power"]: int(state["power"]), TOU_FIELD["soc"]: int(state["slot_soc"]), TOU_FIELD["grid_charge"]: bool(state["grid_charge"]), TOU_FIELD["generate"]: True}

    def build_tou_slots(self, schedule, current_soc):
        """Build exactly TOU_SLOT_COUNT ordered slots covering 24h from the schedule windows."""
        reserve = int(schedule.get("reserve", 0))
        # Collect (start_time, state) segment boundaries. Baseline self-use at 00:00.
        segments = {"00:00": {"behaviour": "idle", "power": 0, "slot_soc": reserve, "grid_charge": False, "solar_sell": False, "work_mode": None}}
        for direction in ("charge", "export"):
            window = schedule.get(direction, {})
            if window.get("enable") and window.get("start") and window.get("end"):
                intent = {"reserve": reserve, "charge": {"enable": False}, "export": {"enable": False}}
                intent[direction] = {"enable": True, "soc": window.get("soc", 0), "power": window.get("power", 0)}
                state = self.derive_control_state(intent, current_soc)
                segments[window["start"]] = state
                # After the window, return to self-use at reserve.
                segments.setdefault(window["end"], {"behaviour": "idle", "power": 0, "slot_soc": reserve, "grid_charge": False, "solar_sell": False, "work_mode": None})
        ordered = sorted(segments.items(), key=lambda kv: kv[0])
        slots = []
        for start_time, state in ordered:
            if state.get("grid_charge") or state.get("solar_sell") or state.get("power"):
                slots.append(self._action_slot(start_time, state))
            else:
                slots.append(self._self_use_slot(start_time, reserve))
        # Normalise to exactly TOU_SLOT_COUNT slots, each with a DISTINCT ascending
        # start time (DEYE rejects/mis-applies duplicate slot times). Pad with
        # self-use slots at filler times not already used by a window boundary,
        # then sort and trim keeping the earliest (imminent) slots.
        used = {slot[TOU_FIELD["time"]] for slot in slots}
        for filler_time in TOU_FILLER_TIMES:
            if len(slots) >= TOU_SLOT_COUNT:
                break
            if filler_time not in used:
                slots.append(self._self_use_slot(filler_time, reserve))
                used.add(filler_time)
        slots = sorted(slots, key=lambda slot: slot[TOU_FIELD["time"]])[:TOU_SLOT_COUNT]
        return slots

    def _now_minutes(self):
        """Return minutes since local midnight, for time-aware window selection."""
        try:
            return int(self.minutes_now)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _hm_to_minutes(hm):
        """Convert a HH:MM string to minutes since midnight (0 on bad input)."""
        try:
            parts = str(hm).split(":")
            return int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError):
            return 0

    def _window_active(self, window, now_minutes):
        """Return True if an enabled window covers now_minutes (handles a midnight wrap)."""
        if not window.get("enable") or not window.get("start") or not window.get("end"):
            return False
        start = self._hm_to_minutes(window["start"])
        end = self._hm_to_minutes(window["end"])
        if start == end:
            return False
        if start < end:
            return start <= now_minutes < end
        return now_minutes >= start or now_minutes < end  # window wraps past midnight

    def _active_state(self, schedule, current_soc, now_minutes):
        """Derive the control state for the window active at now_minutes, else idle.

        DEYE has a single global work mode per schedule, so the top-level mode must
        follow the window active RIGHT NOW rather than a static export-first
        precedence: otherwise an export window enabled elsewhere in the day would
        pin the mode to SELLING_FIRST and block the charge window's grid charging.
        """
        reserve = int(schedule.get("reserve", 0))
        charge = schedule.get("charge", {})
        export = schedule.get("export", {})
        intent = {"reserve": reserve, "charge": {"enable": False}, "export": {"enable": False}}
        if self._window_active(export, now_minutes):
            intent["export"] = {"enable": True, "soc": export.get("soc", 0), "power": export.get("power", 0)}
        elif self._window_active(charge, now_minutes):
            intent["charge"] = {"enable": True, "soc": charge.get("soc", 0), "power": charge.get("power", 0)}
        return self.derive_control_state(intent, current_soc)

    def build_dynamic_payload(self, sn, schedule, current_soc, now_minutes=None):
        """Build the strategy_dynamic_control body for one inverter.

        The top-level work mode / on-off flags follow the window active at
        now_minutes (defaults to the current local time); the 6 TOU slots still
        encode every window's per-slot config.
        """
        if now_minutes is None:
            now_minutes = self._now_minutes()
        slots = self.build_tou_slots(schedule, current_soc)
        active = self._active_state(schedule, current_soc, now_minutes)
        return {
            "deviceSn": sn,
            "workMode": active["work_mode"],
            "gridChargeAction": "on" if active["grid_charge"] else "off",
            "solarSellAction": "on" if active["solar_sell"] else "off",
            "touAction": "on",
            "timeUseSettingItems": slots,
        }

    def payloads_equal(self, a, b):
        """Compare two dynamic-control payloads ignoring deviceSn."""

        def strip(p):
            """Return payload dict p with the deviceSn key removed."""
            return {k: v for k, v in (p or {}).items() if k != "deviceSn"}

        return strip(a) == strip(b)

    async def _get(self, path):
        """GET an absolute DEYE path (used for order status). Returns parsed JSON or {}."""
        url = f"{self.base_url}{path}"
        timeout = aiohttp.ClientTimeout(total=DEYE_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=self._auth_headers()) as resp:
                    if resp.status in (401, 403):
                        if await self.handle_oauth_401():
                            async with session.get(url, headers=self._auth_headers()) as resp2:
                                resp2.raise_for_status()
                                return await resp2.json()
                        return {}
                    resp.raise_for_status()
                    return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: DEYE GET {path} failed: {e}")
            return {}

    async def apply_dynamic_control(self, sn, schedule, current_soc, force=False):
        """Write the combined control payload, suppressing no-op writes via the applied-payload cache. Returns True if written."""
        desired = self.build_dynamic_payload(sn, schedule, current_soc)
        if not force and self.payloads_equal(desired, self.applied_payload.get(sn)):
            self.log(f"Info: DEYE {sn} control unchanged, skipping write")
            return False
        resp = await self._post("dynamic_control", desired)
        if not resp.get("success", True):
            self.log(f"Warn: DEYE dynamic control failed for {sn}: {resp.get('msg', 'unknown')}")
            return False
        self.applied_payload[sn] = desired
        order_id = resp.get("orderId")
        if order_id:
            self.pending_orders[sn] = order_id
            self.log(f"Info: DEYE {sn} control submitted, orderId={order_id}")
        return True

    async def poll_order(self, sn):
        """Poll the pending control order via GET /order/{orderId}. Returns success/pending/failed."""
        order_id = self.pending_orders.get(sn)
        if not order_id:
            return "success"
        resp = await self._get(f"{DEYE_ENDPOINTS['order_result']}{order_id}")
        # _get() returns {} on a network/auth error; an empty or non-success
        # response is NOT a confirmation, so treat it as still pending (default
        # success to False, not True) rather than falsely clearing the order.
        if not resp.get("success", False):
            return "pending"
        self.pending_orders.pop(sn, None)
        return "success"

    def _sensor_name(self, sn, leaf):
        """Return a namespaced DEYE sensor entity id."""
        return f"sensor.{self.prefix}_deye_{sn.lower()}_{leaf}"

    def _control_name(self, domain, sn, leaf):
        """Return a namespaced DEYE control entity id."""
        return f"{domain}.{self.prefix}_deye_{sn.lower()}_{leaf}"

    async def publish_data(self):
        """Publish monitoring sensors for each inverter."""
        for sn in self.device_list:
            values = self.device_values.get(sn, {})
            units = {"soc": "%", "battery_power": "W", "grid_power": "W", "pv_power": "W", "load_power": "W", "temperature": "°C"}
            for leaf, unit in units.items():
                if leaf in values:
                    self.dashboard_item(self._sensor_name(sn, leaf), state=values[leaf], attributes={"unit_of_measurement": unit, "friendly_name": f"DEYE {sn} {leaf.replace('_', ' ').title()}"}, app="deye")

            if sn in self.device_battery_config:
                capability_units = {"battery_capacity": ("capacity", "kWh"), "battery_reserve_min": ("reserve_min", "%"), "max_charge_current": ("max_charge_current", "A"), "max_discharge_current": ("max_discharge_current", "A")}
                for leaf, (key, unit) in capability_units.items():
                    value = self._battery_config_value(sn, key)
                    self.dashboard_item(self._sensor_name(sn, leaf), state=value, attributes={"unit_of_measurement": unit, "friendly_name": f"DEYE {sn} {leaf.replace('_', ' ').title()}"}, app="deye")

    async def publish_schedule_settings_ha(self, sn):
        """Publish the charge/export schedule control entities for one inverter."""
        local = self.local_schedule.get(sn, {})
        reserve = int(local.get("reserve", 0))
        self.dashboard_item(
            self._control_name("number", sn, "battery_schedule_reserve"), state=reserve, attributes={"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": f"DEYE {sn} Battery Schedule Reserve", "icon": "mdi:gauge"}, app="deye"
        )
        for direction in ("charge", "export"):
            window = local.get(direction, {})
            self.dashboard_item(self._control_name("select", sn, f"battery_schedule_{direction}_start_time"), state=window.get("start", "00:00"), attributes={"friendly_name": f"DEYE {sn} {direction.title()} Start", "icon": "mdi:clock-outline"}, app="deye")
            self.dashboard_item(self._control_name("select", sn, f"battery_schedule_{direction}_end_time"), state=window.get("end", "00:00"), attributes={"friendly_name": f"DEYE {sn} {direction.title()} End", "icon": "mdi:clock-outline"}, app="deye")
            self.dashboard_item(
                self._control_name("number", sn, f"battery_schedule_{direction}_soc"),
                state=int(window.get("soc", 0)),
                attributes={"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": f"DEYE {sn} {direction.title()} SoC", "icon": "mdi:gauge"},
                app="deye",
            )
            self.dashboard_item(
                self._control_name("number", sn, f"battery_schedule_{direction}_power"),
                state=int(window.get("power", 0)),
                attributes={"min": 0, "max": 20000, "step": 100, "unit_of_measurement": "W", "friendly_name": f"DEYE {sn} {direction.title()} Power", "icon": "mdi:flash"},
                app="deye",
            )
            self.dashboard_item(
                self._control_name("switch", sn, f"battery_schedule_{direction}_enable"), state="on" if window.get("enable") else "off", attributes={"friendly_name": f"DEYE {sn} {direction.title()} Enable", "icon": "mdi:check-circle-outline"}, app="deye"
            )
        self.dashboard_item(self._control_name("switch", sn, "battery_schedule_charge_write"), state="off", attributes={"friendly_name": f"DEYE {sn} Schedule Write", "icon": "mdi:content-save"}, app="deye")

    async def get_schedule_settings_ha(self, sn):
        """Read the control entities into the schedule shape used by control derivation.

        Numeric casts route through ``_as_float`` (defaulting to 0) so a HA entity legitimately
        reporting the string ``"unknown"``/``"unavailable"`` - e.g. right after a HA restart,
        before Predbat republishes - falls back to 0 rather than raising and crashing the
        reconciliation loop (mirrors the Fox defensiveness).
        """
        schedule = {"reserve": int(self._as_float(self.get_state_wrapper(self._control_name("number", sn, "battery_schedule_reserve"), default=0), 0))}
        for direction in ("charge", "export"):
            schedule[direction] = {
                "enable": self.get_state_wrapper(self._control_name("switch", sn, f"battery_schedule_{direction}_enable"), default="off") == "on",
                "start": self.get_state_wrapper(self._control_name("select", sn, f"battery_schedule_{direction}_start_time"), default="00:00"),
                "end": self.get_state_wrapper(self._control_name("select", sn, f"battery_schedule_{direction}_end_time"), default="00:00"),
                "soc": int(self._as_float(self.get_state_wrapper(self._control_name("number", sn, f"battery_schedule_{direction}_soc"), default=0), 0)),
                "power": int(self._as_float(self.get_state_wrapper(self._control_name("number", sn, f"battery_schedule_{direction}_power"), default=0), 0)),
            }
        self.local_schedule[sn] = schedule
        return schedule

    def _sn_from_entity(self, entity_id):
        """Extract the inverter serial embedded in a DEYE entity id, or None if it can't be resolved.

        Entity ids are always ``{prefix}_deye_{sn}_{leaf}``, so the serial is always followed by
        ``_``. We match ``sn + "_"`` (not a bare prefix) so that prefix-colliding serials are
        disambiguated fully - e.g. an entity for ``INV11`` never mis-routes to ``INV1`` (which
        would otherwise send a forced control write to the wrong inverter).
        """
        marker = "_deye_"
        if marker not in entity_id:
            return None
        tail = entity_id.split(marker, 1)[1]
        for sn in self.device_list:
            if tail.startswith(sn.lower() + "_"):
                return sn
        return None

    async def apply_reserve_live(self, sn, reserve):
        """Write the reserve immediately via a forced control apply (freeze-charge relies on this taking effect at once)."""
        schedule = self.local_schedule.get(sn, {})
        schedule["reserve"] = int(reserve)
        self.local_schedule[sn] = schedule
        current_soc = self.device_values.get(sn, {}).get("soc", reserve)
        self.control_active.add(sn)  # Predbat is now actively controlling this inverter
        return await self.apply_dynamic_control(sn, schedule, current_soc, force=True)

    async def apply_schedule(self, sn, force=True):
        """Recompute the schedule from HA control entities and push it for one inverter."""
        schedule = await self.get_schedule_settings_ha(sn)
        current_soc = self.device_values.get(sn, {}).get("soc", schedule.get("reserve", 0))
        self.control_active.add(sn)  # Predbat is now actively controlling this inverter
        return await self.apply_dynamic_control(sn, schedule, current_soc, force=force)

    async def select_event(self, entity_id, value):
        """Handle a select (time) change from Home Assistant: refresh local schedule state."""
        sn = self._sn_from_entity(entity_id)
        if sn:
            await self.get_schedule_settings_ha(sn)

    async def number_event(self, entity_id, value):
        """Handle a number change from Home Assistant: the reserve is written live, others just refresh local state."""
        sn = self._sn_from_entity(entity_id)
        if not sn:
            return
        if entity_id.endswith("battery_schedule_reserve"):
            await self.apply_reserve_live(sn, int(float(value)))
        else:
            await self.get_schedule_settings_ha(sn)

    async def switch_event(self, entity_id, service):
        """Handle a switch change from Home Assistant: the write button applies the schedule, others just refresh local state."""
        sn = self._sn_from_entity(entity_id)
        if not sn:
            return
        if entity_id.endswith("_write") and service in ("turn_on", "on"):
            await self.apply_schedule(sn, force=True)
        else:
            await self.get_schedule_settings_ha(sn)

    async def automatic_config(self):
        """Register every discovered inverter as a DeyeCloud Predbat inverter."""
        devices = [sn.lower() for sn in self.device_list]
        n = len(devices)
        if not n:
            self.log("Warn: DEYE automatic_config found no inverters")
            return
        self.set_arg("inverter_type", ["DeyeCloud" for _ in devices])
        self.set_arg("num_inverters", n)
        self.set_arg("soc_percent", [self._sensor_name(sn, "soc") for sn in devices])
        self.set_arg("battery_power", [self._sensor_name(sn, "battery_power") for sn in devices])
        self.set_arg("grid_power", [self._sensor_name(sn, "grid_power") for sn in devices])
        self.set_arg("load_power", [self._sensor_name(sn, "load_power") for sn in devices])
        if not self.automatic_ignore_pv:
            self.set_arg("pv_power", [self._sensor_name(sn, "pv_power") for sn in devices])
        self.set_arg("battery_temperature", [self._sensor_name(sn, "temperature") for sn in devices])
        self.set_arg("soc_max", [self._sensor_name(sn, "battery_capacity") for sn in devices])
        self.set_arg("battery_min_soc", [self._sensor_name(sn, "battery_reserve_min") for sn in devices])
        self.set_arg("reserve", [self._control_name("number", sn, "battery_schedule_reserve") for sn in devices])
        self.set_arg("charge_start_time", [self._control_name("select", sn, "battery_schedule_charge_start_time") for sn in devices])
        self.set_arg("charge_end_time", [self._control_name("select", sn, "battery_schedule_charge_end_time") for sn in devices])
        self.set_arg("charge_limit", [self._control_name("number", sn, "battery_schedule_charge_soc") for sn in devices])
        self.set_arg("charge_rate", [self._control_name("number", sn, "battery_schedule_charge_power") for sn in devices])
        self.set_arg("scheduled_charge_enable", [self._control_name("switch", sn, "battery_schedule_charge_enable") for sn in devices])
        self.set_arg("discharge_start_time", [self._control_name("select", sn, "battery_schedule_export_start_time") for sn in devices])
        self.set_arg("discharge_end_time", [self._control_name("select", sn, "battery_schedule_export_end_time") for sn in devices])
        self.set_arg("discharge_target_soc", [self._control_name("number", sn, "battery_schedule_export_soc") for sn in devices])
        self.set_arg("discharge_rate", [self._control_name("number", sn, "battery_schedule_export_power") for sn in devices])
        self.set_arg("scheduled_discharge_enable", [self._control_name("switch", sn, "battery_schedule_export_enable") for sn in devices])
        self.set_arg("schedule_write_button", [self._control_name("switch", sn, "battery_schedule_charge_write") for sn in devices])

    async def run(self, seconds, first):
        """Main component loop: auth, discover, poll, publish, configure."""
        if self.auth_method == "app_credentials" and not getattr(self, "access_token", None):
            if not await self.fetch_token():
                self.log("Warn: DEYE token unavailable, skipping run")
                return False
        if not await self.check_and_refresh_oauth_token():
            self.log("Warn: DEYE OAuth token invalid, skipping run")
            return False

        if first or not self.device_list:
            await self.get_device_list()
        if not self.device_list:
            self.log("Error: DEYE no inverters found")
            return False

        for sn in self.device_list:
            try:
                await self.fetch_battery_config(sn)
                await self.fetch_device_data(sn)
                await self.get_schedule_settings_ha(sn)
            except Exception as e:
                self.log(f"Warn: DEYE poll failed for {sn}: {e}")

        await self.publish_data()
        for sn in self.device_list:
            await self.publish_schedule_settings_ha(sn)

        # Drain any control orders left pending by apply_dynamic_control() every cycle (not
        # just on first run) so a write that is HTTP-accepted but then fails to apply on the
        # device doesn't stay masked forever behind the applied-payload change-detection cache.
        for sn in list(self.pending_orders.keys()):
            try:
                status = await self.poll_order(sn)
            except Exception as e:
                self.log(f"Warn: DEYE order poll failed for {sn}: {e}")
                status = "pending"
            if status == "success":
                self.order_poll_count.pop(sn, None)
            else:
                self.order_poll_count[sn] = self.order_poll_count.get(sn, 0) + 1
                if self.order_poll_count[sn] >= DEYE_ORDER_MAX_POLLS:
                    self.log(f"Warn: DEYE {sn} control order unconfirmed after {DEYE_ORDER_MAX_POLLS} polls; forcing re-write")
                    self.pending_orders.pop(sn, None)
                    self.order_poll_count.pop(sn, None)
                    self.applied_payload.pop(sn, None)  # invalidate cache -> next apply re-writes

        await self._reconcile_control()

        if first and self.automatic:
            await self.automatic_config()
        return True

    async def _reconcile_control(self):
        """Re-apply the schedule of each inverter Predbat is already controlling.

        Keeps the time-aware top-level work mode in sync as the day advances (e.g.
        flips from a charge period to an export period). Change detection suppresses
        no-op writes, so this only actually writes at a genuine transition (or after
        an order-poll cache invalidation). Inverters Predbat has not yet driven via
        the write button (not in ``control_active``) are left untouched, so a startup
        cycle never clobbers an inverter before there is a plan.
        """
        for sn in self.device_list:
            if sn not in self.control_active:
                continue
            schedule = self.local_schedule.get(sn)
            if not schedule:
                continue
            current_soc = self.device_values.get(sn, {}).get("soc", schedule.get("reserve", 0))
            try:
                await self.apply_dynamic_control(sn, schedule, current_soc, force=False)
            except Exception as e:
                self.log(f"Warn: DEYE reconcile failed for {sn}: {e}")

    async def final(self):
        """Cleanup on shutdown."""
        self.log("Info: DeyeAPI shutdown")


class MockBase:  # pragma: no cover
    """Mock base object so DeyeAPI can be exercised from the command line."""

    def __init__(self):
        """Set up the minimal base surface DeyeAPI reads (tz, prefix, args, time, entities)."""
        self.local_tz = datetime.now().astimezone().tzinfo
        self.now_utc = datetime.now(self.local_tz)
        self.prefix = "predbat"
        self.args = {}
        self.midnight_utc = datetime.now(self.local_tz).replace(hour=0, minute=0, second=0, microsecond=0)
        self.minutes_now = self.now_utc.hour * 60 + self.now_utc.minute
        self.entities = {}

    def get_state_wrapper(self, entity_id, default=None, attribute=None, refresh=False, required_unit=None, raw=None):
        """Return a previously published entity state (or default)."""
        if raw:
            return self.entities.get(entity_id, {})
        return self.entities.get(entity_id, {}).get("state", default)

    def set_state_wrapper(self, entity_id, state, attributes=None, app=None):
        """Record an entity state locally."""
        self.entities[entity_id] = {"state": state, "attributes": attributes or {}}

    def log(self, message):
        """Print a timestamped log line."""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        """Print and record a published dashboard entity."""
        print(f"ENTITY: {entity_id} = {state}")
        self.set_state_wrapper(entity_id, state, attributes)

    def get_arg(self, arg, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
        """Return the caller's default (no configured args in CLI mode)."""
        return default

    def set_arg(self, key, value):
        """Print an inverter arg that automatic_config would set."""
        print(f"Set arg {key} = {value}")


def _build_deye(mock_base, args):  # pragma: no cover
    """Construct a DeyeAPI from parsed CLI args, choosing the auth mode."""
    if args.user_id:
        mock_base.args["user_id"] = args.user_id
    arg_dict = {
        "data_center": args.data_center,
        "company_id": args.company_id or "",
        "inverter_sn": args.serial,
        "automatic": True,
    }
    if args.token_hash:
        # Predbat.com SaaS: token injected/refreshed via OAuthMixin (Supabase edge function).
        if args.supabase_url:
            os.environ["SUPABASE_URL"] = args.supabase_url
        if args.supabase_key:
            os.environ["SUPABASE_KEY"] = args.supabase_key
        arg_dict.update({"auth_method": "oauth", "token_hash": args.token_hash, "token_expires_at": args.token_expires})
    else:
        # Self-hosted add-on: developer app (app_id/app_secret) + DeyeCloud account (username/password).
        arg_dict.update({"auth_method": "app_credentials", "app_id": args.app_id, "app_secret": args.app_secret, "username": args.username, "password": args.password})
    return DeyeAPI(mock_base, **arg_dict)


async def test_deye_api(args):  # pragma: no cover
    """Run one read-only DEYE cycle (discover, poll, publish); optionally write a test schedule.

    Default is read-only: run() discovers devices, polls telemetry/config and
    publishes entities but never writes to the inverter. --write-schedule opts in
    to a real charge-window write and polls the resulting order.
    """
    mock_base = MockBase()
    deye = _build_deye(mock_base, args)

    print("Calling run() once (read-only: discover, poll, publish)...")
    await deye.run(seconds=0, first=True)

    for sn in deye.device_list:
        print(f"Device {sn}: telemetry={json.dumps(deye.device_values.get(sn, {}))}")

    if args.write_schedule:
        serial = args.serial or (deye.device_list[0] if deye.device_list else None)
        if not serial:
            print("No inverter serial available for --write-schedule")
        else:
            current_soc = deye.device_values.get(serial, {}).get("soc", 0)
            schedule = {
                "reserve": 10,
                "charge": {"enable": True, "soc": 100, "power": 3000, "start": "00:10", "end": "00:20"},
                "export": {"enable": False, "soc": 0, "power": 0},
            }
            print(f"WRITING test schedule to {serial}:\n{json.dumps(schedule, indent=2)}")
            wrote = await deye.apply_dynamic_control(serial, schedule, current_soc, force=True)
            print(f"Write result: {wrote}, pending order: {deye.pending_orders.get(serial)}")
            if wrote:
                status = await deye.poll_order(serial)
                print(f"Order status: {status}")

    await deye.final()
    print("Done")


def main():  # pragma: no cover
    """Command-line entry point to test the DEYE Cloud API (read-only by default)."""
    parser = argparse.ArgumentParser(description="Test the DEYE Cloud API (read-only by default)")
    parser.add_argument("--serial", default=None, help="DEYE inverter serial (auto-discovered if omitted)")
    parser.add_argument("--data-center", default="eu", choices=["eu", "am", "india"], help="DEYE data centre (default eu)")
    parser.add_argument("--company-id", default=None, help="DeyeCloud companyId (business/installer accounts only)")
    # Self-hosted add-on auth: developer app credentials + DeyeCloud account login.
    parser.add_argument("--app-id", default=None, help="Developer App ID (from developer.deyecloud.com)")
    parser.add_argument("--app-secret", default=None, help="Developer App Secret (from developer.deyecloud.com)")
    parser.add_argument("--username", default=None, help="DeyeCloud account email/username")
    parser.add_argument("--password", default=None, help="DeyeCloud account password (sent SHA-256 hashed)")
    # Predbat.com SaaS auth: injected token.
    parser.add_argument("--token-hash", default=None, help="Injected OAuth token hash (Predbat.com SaaS mode)")
    parser.add_argument("--token-expires", default=None, help="OAuth token expiry timestamp")
    parser.add_argument("--supabase-url", default=None, help="Supabase URL for OAuth token refresh")
    parser.add_argument("--supabase-key", default=None, help="Supabase anon key for OAuth token refresh")
    parser.add_argument("--user-id", default=None, help="Supabase user ID for OAuth token refresh")
    parser.add_argument("--write-schedule", action="store_true", help="Opt in to writing a test charge window (default is read-only)")

    args = parser.parse_args()

    has_app_creds = all([args.app_id, args.app_secret, args.username, args.password])
    if not args.token_hash and not has_app_creds:
        parser.error("provide either --token-hash (SaaS) or all of --app-id/--app-secret/--username/--password (add-on)")

    asyncio.run(test_deye_api(args))


if __name__ == "__main__":  # pragma: no cover
    main()
