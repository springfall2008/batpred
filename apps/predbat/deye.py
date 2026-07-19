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
from component_base import ComponentBase
from oauth_mixin import OAuthMixin
from deye_const import DEYE_BASE_URLS, DEYE_ENDPOINTS, DEYE_TIMEOUT, DEYE_RETRIES, DEYE_TELEMETRY_KEYS, DEYE_LATEST_BODY_KEY, DEYE_WORKMODE, FREEZE_EXPORT_SOC, TOU_FIELD, TOU_SLOT_COUNT


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
        self.token_hash = token_hash
        self.automatic = automatic
        self.automatic_ignore_pv = automatic_ignore_pv
        self.inverter_sn_filter = inverter_sn if isinstance(inverter_sn, list) else ([inverter_sn] if inverter_sn else [])
        self.device_list = []
        self.device_values = {}
        self.device_battery_config = {}
        self.local_schedule = {}
        self.pending_orders = {}
        self.applied_payload = {}
        self.cached_values = {}
        self._init_oauth(
            auth_method=auth_method,
            key=app_secret or token_hash,
            token_expires_at=token_expires_at,
            provider_name="deye",
        )

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
        # Normalise to exactly TOU_SLOT_COUNT: pad by repeating the last slot's SoC at spread times, or trim keeping the imminent windows.
        while len(slots) < TOU_SLOT_COUNT:
            filler_time = "23:59" if not slots else slots[-1][TOU_FIELD["time"]]
            slots.append(self._self_use_slot(filler_time, reserve))
        if len(slots) > TOU_SLOT_COUNT:
            slots = slots[:TOU_SLOT_COUNT]
        return slots
