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
import time
import aiohttp
import argparse
import json
import os
from component_base import ComponentBase
from mock_base import MockBase
from oauth_mixin import OAuthMixin
from deye_const import (
    DEYE_BASE_URLS,
    DEYE_ENDPOINTS,
    DEYE_TIMEOUT,
    DEYE_RETRIES,
    DEYE_TELEMETRY_KEYS,
    DEYE_LATEST_BODY_KEY,
    DEYE_WORKMODE,
    FREEZE_EXPORT_SOC,
    TOU_FIELD,
    TOU_SLOT_COUNT,
    TOU_FILLER_TIMES,
    DEYE_ORDER_MAX_POLLS,
    DEYE_BUSY_CODES,
    DEYE_BUSY_MARKERS,
    CONFIG_BATTERY_KEYS,
    DEYE_AUTH_ERROR_MARKERS,
    DEYE_DEBUG_MAX_CHARS,
    DEYE_DEBUG_REDACT_KEYS,
    DEYE_TELEMETRY_NEGATE,
    DEYE_TELEMETRY_REQUIRED,
    DEYE_CAPACITY_KEYS,
    DEYE_ENERGY_KEYS,
    DEYE_RATED_POWER_KEY,
    LIFEPO4_CHARGE_VOLTS_PER_CELL,
    LIFEPO4_NOMINAL_VOLTS_PER_CELL,
    DEYE_STORAGE_MODULE,
    DEYE_TTL_STATIC,
    DEYE_TTL_CONFIG,
    DEYE_TTL_LIVE,
    DEYE_RESTORE_MAX_CONTROL,
    DEYE_CACHE_STATIC,
    DEYE_CACHE_CONFIG,
    DEYE_CACHE_RATINGS,
    DEYE_CACHE_CONTROL,
)


class DeyeAPI(ComponentBase, OAuthMixin):
    """DEYE Cloud API component."""

    # Trace every API request/response while the DEYE integration beds in; flip to
    # False once it is stable. A class attribute so it is always readable even when a
    # caller (tests, the CLI tool) builds the object without going through initialize().
    api_debug = True

    def initialize(
        self,
        app_id="",
        app_secret="",
        username="",
        password="",
        key="",
        data_center="eu",
        company_id="",
        auth_method="app_credentials",
        token_expires_at=None,
        token_hash="",
        inverter_sn=None,
        automatic=False,
        automatic_ignore_pv=False,
        battery_nominal_voltage=None,
        **kwargs,
    ):
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
        self.station_ids = []
        self.device_values = {}
        self.device_battery_config = {}
        self.device_capacity = {}
        self.device_pack_voltage = {}
        self.device_energy = {}
        self.device_rated_power = {}
        self.local_schedule = {}
        self.pending_orders = {}
        self.order_poll_count = {}
        self.applied_payload = {}
        self.control_active = set()
        self.cached_values = {}
        # tier name -> wall-clock time of last successful refresh; seeded from storage.age()
        # at startup so the refresh cadence survives a restart instead of restarting with it.
        self._tier_refreshed = {}
        self._cache_restored = False
        self._saved_ratings = None  # signature of the ratings last written, to skip no-op saves
        self._soc_floor_warned = set()
        self.battery_nominal_voltage = self._as_float(battery_nominal_voltage, 0.0)
        # auth_method defaults to app_credentials, but an injected access token with no
        # developer app credentials can only be oauth — and in app_credentials mode
        # _init_oauth() throws the key away, so a host that forgets deye_auth_method gets a
        # component that authenticates with nothing. Infer the mode from what was supplied.
        if key and not app_id and auth_method != "oauth":
            self.log("Info: DEYE inferring auth_method 'oauth' - an access token was injected without app credentials")
            auth_method = "oauth"
        # _init_oauth() assigns key straight to self.access_token in oauth mode, so it must
        # be the injected ACCESS TOKEN (deye_key) — NOT token_hash, which is only the dedup
        # handle echoed back to oauth-refresh. Passing the hash made DEYE reject every call
        # with "auth invalid token", and a token_expires_at far in the future meant the
        # refresh that would have replaced it never ran.
        self._init_oauth(
            auth_method=auth_method,
            key=key or app_secret,
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

    @staticmethod
    def redact(payload):
        """Return payload with credential-bearing keys masked, for safe logging."""
        if isinstance(payload, dict):
            return {k: ("***" if k in DEYE_DEBUG_REDACT_KEYS else DeyeAPI.redact(v)) for k, v in payload.items()}
        if isinstance(payload, list):
            return [DeyeAPI.redact(v) for v in payload]
        return payload

    def debug_api(self, direction, what, payload=None):
        """Log one side of a DEYE API exchange when api_debug is on.

        On by default while the integration beds in — DEYE's error bodies (bad
        token, unsupported config point) are the only way to tell what the cloud
        actually rejected. Bodies are truncated and credentials redacted.
        """
        if not self.api_debug:
            return
        if payload is None:
            self.log(f"Debug: DEYE API {direction} {what}")
            return
        try:
            text = json.dumps(self.redact(payload), default=str)
        except (TypeError, ValueError):
            text = str(payload)
        if len(text) > DEYE_DEBUG_MAX_CHARS:
            text = f"{text[:DEYE_DEBUG_MAX_CHARS]}... [truncated, {len(text)} chars]"
        self.log(f"Debug: DEYE API {direction} {what}: {text}")

    async def fetch_token(self):
        """Fetch an access token using app credentials (app_credentials mode)."""
        # Every app_credentials arg is optional in COMPONENT_LIST (the component also
        # accepts the SaaS token path), so a misconfigured instance can reach here with
        # them unset. Fail soft: both callers already treat False as "skip this run",
        # whereas hashing a None password raises AttributeError out of run() and buries
        # the real problem under a traceback on every cycle.
        missing = [name for name in ("app_id", "app_secret", "username", "password") if not getattr(self, name, None)]
        if missing:
            self.log(f"Warn: DEYE app_credentials login is missing {', '.join(missing)} - set deye_auth_method to 'oauth' if this is a Predbat.com instance")
            return False
        url = f"{self.base_url}{DEYE_ENDPOINTS['token']}?appId={self.app_id}"
        body = {"appSecret": self.app_secret, "password": self._sha256(self.password), **self._login_payload(self.username)}
        if self.company_id:
            body["companyId"] = str(self.company_id)
        timeout = aiohttp.ClientTimeout(total=DEYE_TIMEOUT)
        self.debug_api("POST", url, body)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=body) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: DEYE token fetch failed: {e}")
            return False
        self.debug_api("RESP", url, data)
        if not data.get("success"):
            self.log(f"Warn: DEYE token rejected: {data.get('msg', 'unknown')}")
            return False
        self.access_token = data.get("accessToken")
        return True

    @staticmethod
    def is_auth_error_body(data):
        """Return True if an HTTP 200 DEYE body is really an expired/invalid-token failure.

        DEYE answers a bad bearer token with 200 + {"success": false, "msg": "auth
        invalid token"} instead of a 401, so the body has to be inspected or no
        refresh is ever attempted (see DEYE_AUTH_ERROR_MARKERS).
        """
        if not isinstance(data, dict) or data.get("success", True):
            return False
        # Separators are normalised to spaces so a code in the RFC 6749 style
        # ("invalid_token") matches the same marker as the prose msg text.
        text = f"{data.get('msg', '')} {data.get('code', '')}".lower().replace("_", " ").replace("-", " ")
        return any(marker in text for marker in DEYE_AUTH_ERROR_MARKERS)

    async def reauthenticate(self, context):
        """Obtain a fresh access token after an auth failure. Returns True if the call is worth retrying."""
        if await self.handle_oauth_401():
            return True
        if self.auth_method == "app_credentials":
            self.access_token = None
            if await self.fetch_token():
                return True
        self.log(f"Warn: DEYE re-authentication failed on {context}")
        return False

    async def _post(self, endpoint_key, body):
        """POST to a DEYE endpoint with retry and token refresh. Returns parsed JSON or raises."""
        url = f"{self.base_url}{DEYE_ENDPOINTS[endpoint_key]}"
        timeout = aiohttp.ClientTimeout(total=DEYE_TIMEOUT)
        last_err = None
        refreshed = False
        for attempt in range(DEYE_RETRIES):
            try:
                self.debug_api("POST", url, body)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, headers=self._auth_headers(), json=body) as resp:
                        if resp.status in (401, 403):
                            self.log(f"Warn: DEYE 401/403 on {endpoint_key}, attempt {attempt + 1}")
                            if await self.reauthenticate(endpoint_key):
                                continue
                            raise RuntimeError(f"DEYE auth failed on {endpoint_key}")
                        resp.raise_for_status()
                        data = await resp.json()
                        self.debug_api("RESP", f"{url} status={resp.status}", data)
                # A 200 whose body reports an invalid/expired token means the same thing as a
                # 401 here: refresh once and retry. Bounded to a single refresh per call so a
                # server that keeps rejecting the new token cannot spin the loop.
                if not refreshed and self.is_auth_error_body(data):
                    refreshed = True
                    self.log(f"Warn: DEYE token rejected in response body on {endpoint_key}: {data.get('msg', 'unknown')}, refreshing")
                    if await self.reauthenticate(endpoint_key):
                        continue
                return data
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
        self.station_ids = [s.get("id") or s.get("stationId") for s in stations if s.get("id") or s.get("stationId")]
        return self.station_ids

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
        result = {}
        for name, key in DEYE_TELEMETRY_KEYS.items():
            value = self._as_float(flat.get(key))
            # DEYE's grid sign is the opposite of Predbat's (see DEYE_TELEMETRY_NEGATE).
            result[name] = -value if name in DEYE_TELEMETRY_NEGATE else value
        self.device_energy[sn] = {name: self._as_float(flat.get(key)) for name, key in DEYE_ENERGY_KEYS.items() if key in flat}
        # Only record when actually present. An unconditional write would coerce an absent
        # key to 0.0 and clobber a rating captured on an earlier cycle, which then stops
        # publishing the inverter_limit sensor and stops automatic_config mapping it —
        # the same reason the energy counters above and derive_battery_capacity() below
        # both leave their caches alone when the source key is missing.
        if DEYE_RATED_POWER_KEY in flat:
            self.device_rated_power[sn] = self._as_float(flat[DEYE_RATED_POWER_KEY])
        self.derive_battery_capacity(sn, flat)
        missing = [DEYE_TELEMETRY_KEYS[name] for name in DEYE_TELEMETRY_REQUIRED if DEYE_TELEMETRY_KEYS[name] not in flat]
        if missing:
            # A missing key silently reads 0.0, which previously hid a wholly wrong key
            # map, so say so loudly and name what the device did return.
            self.log(f"Warn: DEYE {sn} device/latest missing expected keys {missing} - telemetry will read zero. Available keys: {sorted(flat)}")
        self.device_values[sn] = result
        return result

    def nominal_pack_voltage(self, bms_charge_voltage):
        """Infer the LiFePO4 pack's nominal voltage from its BMS charge target.

        The cell count is what actually matters: a 57.6 V charge target is 16 cells, which
        is a 51.2 V nominal pack. Deriving it this way rather than assuming 48 V means
        DEYE's high-voltage stacks scale correctly with no configuration.

        The RESTING pack voltage must never be used for this — it climbs with SOC, so the
        same 16S pack reads ~53.8 V at 94% but ~58 V at 100%, which rounds to 18 cells and
        overstates capacity by 12.5%. An explicit deye_battery_nominal_voltage wins over
        everything; otherwise there is no safe inference and 0 is returned.
        """
        if self.battery_nominal_voltage > 0:
            return self.battery_nominal_voltage
        if bms_charge_voltage > 0:
            cells = round(bms_charge_voltage / LIFEPO4_CHARGE_VOLTS_PER_CELL)
            if cells > 0:
                return cells * LIFEPO4_NOMINAL_VOLTS_PER_CELL
        # Deliberately NO default. Guessing (say 51.2 V for a 48 V hybrid) would be right
        # for the common LV pack and several-fold wrong for an HV stack, and a wrong
        # soc_max is worse than none: it becomes nominal_capacity, which sets the
        # 0.65-1.20 plausibility band that filters find_battery_size(), so a bad value
        # also rejects every correct sample from Predbat's own estimator. Returning 0
        # leaves that band disabled and lets the estimator work from the soc_percent and
        # battery_power history instead.
        return 0.0

    def derive_battery_capacity(self, sn, flat):
        """Derive pack capacity in kWh from device/latest, caching it as a soc_max source.

        config/battery answers "config point not supported" on some models (code 2106001),
        leaving Predbat with no soc_max at all. device/latest does carry the pack's rated
        Ah and the BMS charge voltage on those same models, and Ah x nominal volts is the
        pack's kWh.
        """
        rated_ah = self._as_float(flat.get(DEYE_CAPACITY_KEYS["rated_capacity_ah"]))
        if rated_ah <= 0:
            return 0.0
        volts = self.nominal_pack_voltage(self._as_float(flat.get(DEYE_CAPACITY_KEYS["bms_charge_voltage"])))
        if volts <= 0:
            measured = self._as_float(flat.get(DEYE_CAPACITY_KEYS["battery_voltage"]))
            self.log(f"Warn: DEYE {sn} reports a {rated_ah}Ah pack but no BMS charge voltage, so kWh cannot be derived (pack currently reads {measured}V, which varies with SOC and cannot be used). Set deye_battery_nominal_voltage, or soc_max in apps.yaml")
            return 0.0
        self.device_pack_voltage[sn] = volts
        capacity = round(rated_ah * volts / 1000.0, 2)
        if self.device_capacity.get(sn) != capacity:
            self.log(f"Info: DEYE {sn} battery capacity derived as {capacity}kWh ({rated_ah}Ah at {volts}V nominal)")
        self.device_capacity[sn] = capacity
        return capacity

    def battery_capacity(self, sn):
        """Return the best available pack capacity in kWh: config/battery first, else derived.

        config/battery reports battCapacity in Ah (confirmed live: 1200, matching
        device/latest BatteryRatedCapacity 1200 Ah), so it is scaled by the nominal pack
        voltage exactly like the derived value — publishing it raw would claim a 1200 kWh
        battery.
        """
        volts = self.device_pack_voltage.get(sn, 0.0)
        if sn in self.device_battery_config and volts > 0:
            configured_ah = self._battery_config_value(sn, "capacity")
            if configured_ah > 0:
                return round(configured_ah * volts / 1000.0, 2)
        return self.device_capacity.get(sn, 0.0)

    def battery_reserve_min(self, sn):
        """Return the inverter's own minimum SOC floor in percent, or 0 when unknown.

        config/battery reports this as battLowCapacity (14 on the live 3-phase hybrid).
        It is the floor the installer configured on the inverter, so Predbat must never
        ask for a slot SOC below it.
        """
        if sn not in self.device_battery_config:
            return 0
        try:
            floor = int(self._battery_config_value(sn, "reserve_min", 0))
        except (TypeError, ValueError):
            return 0
        return floor if 0 < floor <= 100 else 0

    def battery_rate_max(self, sn):
        """Return the battery's maximum charge rate in W, for Predbat's battery_rate_max.

        config/battery reports maxChargeCurrent in amps (185 live), so it is scaled by the
        nominal pack voltage. This is the DC battery-side limit and can legitimately exceed
        the inverter's AC RatedPower — inverter_limit constrains the AC side separately.
        """
        volts = self.device_pack_voltage.get(sn, 0.0)
        if volts <= 0 or sn not in self.device_battery_config:
            return 0.0
        amps = self._battery_config_value(sn, "max_charge_current")
        return round(amps * volts) if amps > 0 else 0.0

    async def fetch_battery_config(self, sn):
        """Fetch and cache battery capability config for one inverter."""
        data = await self._post("config_battery", {"deviceSn": sn})
        if not data.get("success", True):
            # Not fatal: some models reject this config point entirely, and battery
            # capacity is then derived from device/latest instead.
            self.log(f"Warn: DEYE config/battery failed for {sn}: {data.get('msg', 'unknown')} - battery capacity will be derived from device/latest")
            return {}
        self.device_battery_config[sn] = data
        return data

    async def fetch_measure_points(self, sn):
        """Log the device's measure-point metadata (read-only, first cycle only).

        Names every metric the model can report, which is the reference for the
        device/latest key spellings and for any capability config not exposed elsewhere.
        """
        data = await self._post("device_measure_points", {"deviceSn": sn})
        if not data.get("success", True):
            self.log(f"Warn: DEYE device/measurePoints failed for {sn}: {data.get('msg', 'unknown')}")
            return {}
        return data

    async def fetch_station_latest(self, station_id):
        """Log a station's latest snapshot (read-only, first cycle only)."""
        data = await self._post("station_latest", {"stationId": station_id})
        if not data.get("success", True):
            self.log(f"Warn: DEYE station/latest failed for {station_id}: {data.get('msg', 'unknown')}")
            return {}
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
                # Normalised to HH:MM here: these strings become DEYE slot times, and the
                # entities they came from carry seconds.
                segments[self._to_slot_time(window["start"])] = state
                # After the window, return to self-use at reserve.
                segments.setdefault(self._to_slot_time(window["end"]), {"behaviour": "idle", "power": 0, "slot_soc": reserve, "grid_charge": False, "solar_sell": False, "work_mode": None})
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
    def _to_slot_time(value):
        """Normalise a schedule time to the HH:MM DEYE's TOU slots require.

        The control entities carry HH:MM:SS because that is the format Predbat writes (see
        INVERTER_DEF charge_time_format), but DEYE's timeUseSettingItems take HH:MM, so the
        seconds are dropped here — at the one point a schedule time becomes a slot time.
        """
        text = str(value or "00:00")
        parts = text.split(":")
        if len(parts) < 2:
            return "00:00"
        try:
            return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
        except ValueError:
            return "00:00"

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
        # Final guard: never ask the battery to go below the floor its own installer
        # settings declare (config/battery battLowCapacity). Predbat's control entities
        # start at 0 and only reach their real value once it has written them, so without
        # this the first control write of a cycle sends slot SOC 0 to a pack whose floor is
        # 14%. Applied here, at the last step, so no other caller can bypass it.
        floor = self.battery_reserve_min(sn)
        if floor > 0:
            lifted = False
            for slot in slots:
                if slot.get(TOU_FIELD["soc"], 0) < floor:
                    slot[TOU_FIELD["soc"]] = floor
                    lifted = True
            # Say so once per serial. Routine before Predbat has written the reserve, but a
            # persistent complaint means Predbat is planning below the inverter's floor —
            # usually battery_min_soc not being mapped, which is worth surfacing.
            if lifted and sn not in self._soc_floor_warned:
                self._soc_floor_warned.add(sn)
                self.log(f"Info: DEYE {sn} raising requested slot SOC to the inverter's {floor}% floor (config/battery battLowCapacity)")
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
                # Two passes at most: the original call, then one retry after a refresh.
                for attempt in range(2):
                    self.debug_api("GET", url)
                    async with session.get(url, headers=self._auth_headers()) as resp:
                        if resp.status in (401, 403):
                            data = None
                        else:
                            resp.raise_for_status()
                            data = await resp.json()
                            self.debug_api("RESP", f"{url} status={resp.status}", data)
                    # A 401/403 and a 200 body reporting an invalid token both mean the
                    # token needs refreshing before the call can succeed.
                    if attempt == 0 and (data is None or self.is_auth_error_body(data)):
                        if data is not None:
                            self.log(f"Warn: DEYE token rejected in response body on GET {path}: {data.get('msg', 'unknown')}, refreshing")
                        if await self.reauthenticate(f"GET {path}"):
                            continue
                        return {}
                    return data if data is not None else {}
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: DEYE GET {path} failed: {e}")
            return {}
        return {}

    @staticmethod
    def is_busy_response(data):
        """Return True when DEYE refused a command because one is already running.

        This is back-pressure, not a failure: DEYE runs a single control order per device
        and rejects anything sent while one is in flight.
        """
        if not isinstance(data, dict) or data.get("success", True):
            return False
        if str(data.get("code", "")) in DEYE_BUSY_CODES:
            return True
        msg = str(data.get("msg", "")).lower()
        return any(marker in msg for marker in DEYE_BUSY_MARKERS)

    async def apply_dynamic_control(self, sn, schedule, current_soc, force=False):
        """Write the combined control payload, suppressing no-op writes via the applied-payload cache. Returns True if written."""
        desired = self.build_dynamic_payload(sn, schedule, current_soc)
        if not force and self.payloads_equal(desired, self.applied_payload.get(sn)):
            self.log(f"Info: DEYE {sn} control unchanged, skipping write")
            return False
        # DEYE executes one order at a time per device, so a write sent while the previous
        # one is still running is rejected outright. Defer instead of burning the call:
        # applied_payload is left untouched, so the next cycle sees the same difference and
        # re-applies once the order has drained. force does not bypass this — the API would
        # reject it just the same.
        pending = self.pending_orders.get(sn)
        if pending:
            self.log(f"Info: DEYE {sn} order {pending} still in flight, deferring this control write")
            return False
        resp = await self._post("dynamic_control", desired)
        if not resp.get("success", True):
            if self.is_busy_response(resp):
                self.log(f"Info: DEYE {sn} is busy running a control order, will re-apply next cycle")
            else:
                self.log(f"Warn: DEYE dynamic control failed for {sn}: {resp.get('msg', 'unknown')}")
            return False
        self.applied_payload[sn] = desired
        order_id = resp.get("orderId")
        if order_id:
            self.pending_orders[sn] = order_id
            self.log(f"Info: DEYE {sn} control submitted, orderId={order_id}")
        # Persist immediately rather than at the end of the cycle: a restart between the
        # write and the next tick would otherwise lose track of the in-flight order and
        # re-write a payload the inverter already holds.
        await self.save_control()
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

            # Capacity is published whenever ANY source has it — config/battery when the
            # model serves it, otherwise the value derived from device/latest. Without
            # this, models that reject config/battery publish no soc_max sensor at all
            # while automatic_config still points Predbat at it.
            capacity = self.battery_capacity(sn)
            if capacity > 0:
                self.dashboard_item(self._sensor_name(sn, "battery_capacity"), state=capacity, attributes={"unit_of_measurement": "kWh", "friendly_name": f"DEYE {sn} Battery Capacity"}, app="deye")

            # Inverter/battery ratings, used by Predbat to model clipping and charge curves.
            rate_max = self.battery_rate_max(sn)
            if rate_max > 0:
                self.dashboard_item(self._sensor_name(sn, "battery_rate_max"), state=rate_max, attributes={"unit_of_measurement": "W", "friendly_name": f"DEYE {sn} Battery Rate Max"}, app="deye")
            rated_power = self.device_rated_power.get(sn, 0.0)
            if rated_power > 0:
                self.dashboard_item(self._sensor_name(sn, "inverter_limit"), state=rated_power, attributes={"unit_of_measurement": "W", "friendly_name": f"DEYE {sn} Inverter Limit"}, app="deye")

            # Daily energy counters feed Predbat's load/import/export history learning. They
            # reset at midnight; minute_data/clean_incrementing_reverse absorb that (see
            # DEYE_ENERGY_KEYS for why the Daily registers are used over the Total* ones).
            for leaf, value in self.device_energy.get(sn, {}).items():
                self.dashboard_item(
                    self._sensor_name(sn, leaf),
                    state=value,
                    attributes={"unit_of_measurement": "kWh", "device_class": "energy", "state_class": "measurement", "friendly_name": f"DEYE {sn} {leaf.replace('_', ' ').title()}"},
                    app="deye",
                )

            if sn in self.device_battery_config:
                capability_units = {"battery_reserve_min": ("reserve_min", "%"), "max_charge_current": ("max_charge_current", "A"), "max_discharge_current": ("max_discharge_current", "A")}
                for leaf, (key, unit) in capability_units.items():
                    value = self._battery_config_value(sn, key)
                    self.dashboard_item(self._sensor_name(sn, leaf), state=value, attributes={"unit_of_measurement": unit, "friendly_name": f"DEYE {sn} {leaf.replace('_', ' ').title()}"}, app="deye")

    async def publish_schedule_settings_ha(self, sn):
        """Publish the charge/export schedule control entities for one inverter."""
        local = self.local_schedule.get(sn, {})
        # Deliberately NOT clamped to the inverter floor. This entity is Predbat's control
        # surface: it writes a value then reads it back to confirm (write_and_poll_value),
        # so publishing anything other than what was written guarantees a mismatch and a
        # retry storm. The floor is enforced at the API boundary instead, in
        # build_dynamic_payload, which is what actually protects the battery.
        reserve = int(local.get("reserve", 0))
        self.dashboard_item(
            self._control_name("number", sn, "battery_schedule_reserve"),
            state=reserve,
            attributes={"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "friendly_name": f"DEYE {sn} Battery Schedule Reserve", "icon": "mdi:gauge"},
            app="deye",
        )
        for direction in ("charge", "export"):
            window = local.get(direction, {})
            # HH:MM:SS to match INVERTER_DEF charge_time_format. Any other value makes
            # Predbat replace these entities with its own dummies (inverter.py, the
            # inv_charge_time_format != "HH:MM:SS" branch) and the window never arrives.
            self.dashboard_item(
                self._control_name("select", sn, f"battery_schedule_{direction}_start_time"), state=window.get("start", "00:00:00"), attributes={"friendly_name": f"DEYE {sn} {direction.title()} Start", "icon": "mdi:clock-outline"}, app="deye"
            )
            self.dashboard_item(self._control_name("select", sn, f"battery_schedule_{direction}_end_time"), state=window.get("end", "00:00:00"), attributes={"friendly_name": f"DEYE {sn} {direction.title()} End", "icon": "mdi:clock-outline"}, app="deye")
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
                "start": self.get_state_wrapper(self._control_name("select", sn, f"battery_schedule_{direction}_start_time"), default="00:00:00"),
                "end": self.get_state_wrapper(self._control_name("select", sn, f"battery_schedule_{direction}_end_time"), default="00:00:00"),
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

    async def apply_schedule(self, sn, force=False):
        """Recompute the schedule from HA control entities and push it for one inverter.

        Not forced by default. Predbat presses the write button on every cycle as its normal
        "apply the schedule" action, so forcing here re-sent a byte-identical payload every
        few minutes — 36 orders in two hours on a live site with nothing actually changing.
        The applied-payload cache is the single source of truth for whether a write is
        needed; when it must be distrusted (an order left unconfirmed after
        DEYE_ORDER_MAX_POLLS) that path already pops the entry, which makes the next apply
        write naturally.
        """
        schedule = await self.get_schedule_settings_ha(sn)
        current_soc = self.device_values.get(sn, {}).get("soc", schedule.get("reserve", 0))
        self.control_active.add(sn)  # Predbat is now actively controlling this inverter
        return await self.apply_dynamic_control(sn, schedule, current_soc, force=force)

    @staticmethod
    def _to_bool(value, current=False):
        """Coerce an HA switch service name or entity state into a boolean."""
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text == "toggle":
            return not current
        return text in ("on", "turn_on", "true", "1", "enable", "enabled")

    def update_local_schedule(self, sn, entity_id, value):
        """Apply a control entity change into local_schedule. Returns True if it was applied.

        This is what makes a write stick. Predbat writes these entities with
        number/set_value, select/select_option or switch/turn_*, then reads the entity back
        to confirm (see inverter.write_and_poll_value / write_and_poll_switch). Nothing else
        republishes them, so if the incoming value is not applied here and the entity
        re-emitted, the read-back returns the old value and every write is reported failed.
        Mirrors fox.write_battery_schedule_event.
        """
        schedule = self.local_schedule.setdefault(sn, {})
        if entity_id.endswith("battery_schedule_reserve"):
            # Stored exactly as written; the floor is applied at the API boundary so the
            # read-back Predbat performs always matches what it wrote.
            schedule["reserve"] = int(self._as_float(value, 0))
            return True
        direction = "charge" if "_charge_" in entity_id else ("export" if "_export_" in entity_id else None)
        if not direction:
            return False
        window = schedule.setdefault(direction, {})
        if entity_id.endswith("_start_time"):
            window["start"] = str(value)
        elif entity_id.endswith("_end_time"):
            window["end"] = str(value)
        elif entity_id.endswith("_soc"):
            window["soc"] = int(self._as_float(value, 0))
        elif entity_id.endswith("_power"):
            window["power"] = int(self._as_float(value, 0))
        elif entity_id.endswith("_enable"):
            window["enable"] = self._to_bool(value, current=window.get("enable", False))
        else:
            return False
        return True

    async def _handle_control_event(self, entity_id, value):
        """Apply an entity change and republish it, so Predbat's read-back confirms the write."""
        sn = self._sn_from_entity(entity_id)
        if not sn:
            return
        if self.update_local_schedule(sn, entity_id, value):
            await self.publish_schedule_settings_ha(sn)
        else:
            await self.get_schedule_settings_ha(sn)

    async def select_event(self, entity_id, value):
        """Handle a select (time) change from Home Assistant."""
        await self._handle_control_event(entity_id, value)

    async def number_event(self, entity_id, value):
        """Handle a number change from Home Assistant; the reserve is additionally written live."""
        sn = self._sn_from_entity(entity_id)
        if not sn:
            return
        if entity_id.endswith("battery_schedule_reserve"):
            # Reserve goes to the inverter immediately (freeze-charge depends on it taking
            # effect at once), and is republished so Predbat's read-back sees the new value
            # whether or not the inverter write itself succeeded.
            await self.apply_reserve_live(sn, int(self._as_float(value, 0)))
            await self.publish_schedule_settings_ha(sn)
            return
        await self._handle_control_event(entity_id, value)

    async def switch_event(self, entity_id, service):
        """Handle a switch change from Home Assistant: the write button applies the schedule."""
        sn = self._sn_from_entity(entity_id)
        if not sn:
            return
        if entity_id.endswith("_write"):
            # A momentary button, not schedule state: it has nothing to store, and must not
            # fall through to update_local_schedule where "_charge_" would match a direction.
            if service in ("turn_on", "on", "toggle"):
                # Unforced: change detection decides. Predbat presses this every cycle, so
                # forcing would re-send an unchanged payload each time.
                await self.apply_schedule(sn)
            return
        await self._handle_control_event(entity_id, service)

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
        # Energy counters are only mapped when every inverter actually reports them, so a
        # model that omits one does not leave Predbat reading a sensor that is never published.
        for leaf in DEYE_ENERGY_KEYS:
            if leaf == "pv_today" and self.automatic_ignore_pv:
                continue
            if all(leaf in self.device_energy.get(sn, {}) for sn in self.device_list):
                self.set_arg(leaf, [self._sensor_name(sn, leaf) for sn in devices])
            else:
                self.log(f"Warn: DEYE not every inverter reports {leaf}, it must be set manually in apps.yaml")
        # Only point Predbat at capability sensors that are actually published: config/battery
        # is unsupported on some models, and an arg aimed at a non-existent sensor is worse
        # than an absent arg (which the user can fill in via apps.yaml).
        if all(self.battery_capacity(sn) > 0 for sn in self.device_list):
            self.set_arg("soc_max", [self._sensor_name(sn, "battery_capacity") for sn in devices])
        else:
            self.log("Warn: DEYE no battery capacity available for every inverter, soc_max must be set manually in apps.yaml")
        if all(sn in self.device_battery_config for sn in self.device_list):
            self.set_arg("battery_min_soc", [self._sensor_name(sn, "battery_reserve_min") for sn in devices])
        if all(self.battery_rate_max(sn) > 0 for sn in self.device_list):
            self.set_arg("battery_rate_max", [self._sensor_name(sn, "battery_rate_max") for sn in devices])
        else:
            self.log("Warn: DEYE no battery charge-current limit available, battery_rate_max must be set manually in apps.yaml")
        if all(self.device_rated_power.get(sn, 0.0) > 0 for sn in self.device_list):
            self.set_arg("inverter_limit", [self._sensor_name(sn, "inverter_limit") for sn in devices])
        else:
            self.log("Warn: DEYE no RatedPower reported, inverter_limit must be set manually in apps.yaml")
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

    @staticmethod
    def _age_text(age):
        """Render a cache age in minutes for logging, tolerating an unknown age."""
        return f"{age:.1f} minutes" if age is not None else "unknown"

    def tier_expired(self, tier, ttl_minutes):
        """Return True when a tier has never refreshed or its TTL has elapsed."""
        last = self._tier_refreshed.get(tier)
        if last is None:
            return True
        return (time.time() - last) / 60.0 >= ttl_minutes

    def mark_refreshed(self, tier, age_minutes=0.0):
        """Start (or seed) a tier's TTL clock.

        age_minutes lets a restored cache backdate the clock, so a tier cached 2 minutes
        before a restart still has 13 of its 15 minutes left rather than a fresh 15.
        """
        self._tier_refreshed[tier] = time.time() - (age_minutes or 0.0) * 60.0

    async def load_cache(self, name):
        """Return (data, age_minutes) for one cache file, or (None, None).

        A cache must never break a run, so storage being absent, unreadable or corrupt is
        reported and treated as a miss — the tier then simply refreshes.
        """
        storage = self.storage
        if not storage:
            return None, None
        try:
            data = await storage.load(DEYE_STORAGE_MODULE, name)
            if data is None:
                return None, None
            age = await storage.age(DEYE_STORAGE_MODULE, name)
        except Exception as e:
            self.log(f"Warn: DEYE could not read the {name} cache: {e}")
            return None, None
        return data, age

    async def save_cache(self, name, data):
        """Persist one cache file. No-ops when storage is unavailable."""
        storage = self.storage
        if not storage:
            return False
        try:
            return await storage.save(DEYE_STORAGE_MODULE, name, data, format="json", expiry=None)
        except Exception as e:
            self.log(f"Warn: DEYE could not write the {name} cache: {e}")
            return False

    async def save_static(self):
        """Cache discovery results."""
        return await self.save_cache(DEYE_CACHE_STATIC, {"station_ids": self.station_ids, "device_list": self.device_list})

    async def save_config(self):
        """Cache the per-device config/battery responses."""
        return await self.save_cache(DEYE_CACHE_CONFIG, self.device_battery_config)

    def _ratings_payload(self):
        """Return the static per-device ratings in their cached shape."""
        return {"capacity": self.device_capacity, "pack_voltage": self.device_pack_voltage, "rated_power": self.device_rated_power}

    async def save_ratings(self):
        """Cache the ratings, but only when they have actually changed.

        These are written by the once-a-minute live poll yet are static per install, so an
        unconditional save would rewrite an identical file 1440 times a day. Compared by
        serialised value rather than identity because the dicts are mutated in place.
        """
        payload = self._ratings_payload()
        signature = json.dumps(payload, sort_keys=True, default=str)
        if signature == getattr(self, "_saved_ratings", None):
            return False
        if await self.save_cache(DEYE_CACHE_RATINGS, payload):
            self._saved_ratings = signature
            return True
        return False

    async def save_control(self):
        """Cache control state: what was last written, and any order still in flight."""
        return await self.save_cache(DEYE_CACHE_CONTROL, {"applied_payload": self.applied_payload, "pending_orders": self.pending_orders, "order_poll_count": self.order_poll_count})

    async def restore_state(self):
        """Restore cached state at startup and seed each tier's refresh clock.

        Seeding the clocks from storage.age() rather than from process start is what makes
        the cadence survive a restart: a tier cached two minutes ago keeps the rest of its
        TTL, one cached nine hours ago re-polls immediately.
        """
        static, age = await self.load_cache(DEYE_CACHE_STATIC)
        if isinstance(static, dict):
            station_ids = static.get("station_ids")
            device_list = static.get("device_list")
            # An empty device_list is never restored as valid: caching an empty discovery
            # (an outage during the previous run) would otherwise pin the component dead
            # for the whole 8 hour TTL.
            if isinstance(device_list, list) and device_list:
                self.device_list = device_list
                self.station_ids = station_ids if isinstance(station_ids, list) else []
                self.mark_refreshed("static", age)
                self.log(f"Info: DEYE restored {len(device_list)} inverter(s) from cache (age {self._age_text(age)})")

        config, age = await self.load_cache(DEYE_CACHE_CONFIG)
        if isinstance(config, dict) and config:
            self.device_battery_config = config
            self.mark_refreshed("config", age)

        # Ratings are static per install and carry no TTL of their own — device/latest
        # rewrites them on every live refresh. Restoring them unconditionally is the main
        # win: automatic_config() can map soc_max/battery_rate_max/inverter_limit at
        # startup without waiting for a poll to complete.
        ratings, _ = await self.load_cache(DEYE_CACHE_RATINGS)
        if isinstance(ratings, dict):
            self.device_capacity = ratings.get("capacity") or {}
            self.device_pack_voltage = ratings.get("pack_voltage") or {}
            self.device_rated_power = ratings.get("rated_power") or {}
            # Record what is already on disk so the first poll does not rewrite an
            # identical file.
            self._saved_ratings = json.dumps(self._ratings_payload(), sort_keys=True, default=str)

        # Telemetry is deliberately not restored: the live tier polls every minute anyway,
        # and HA keeps the last published value of each entity, so there is nothing to gain
        # from caching it 1440 times a day. The live clock therefore starts unset and the
        # first tick polls immediately.

        control, age = await self.load_cache(DEYE_CACHE_CONTROL)
        if isinstance(control, dict):
            orders = control.get("pending_orders")
            counts = control.get("order_poll_count")
            # Orders restore unconditionally: an unpolled order is orphaned, and
            # DEYE_ORDER_MAX_POLLS still bounds how long it can stay unconfirmed.
            if isinstance(orders, dict):
                self.pending_orders = orders
                if orders:
                    self.log(f"Info: DEYE resuming {len(orders)} pending control order(s) from cache")
            if isinstance(counts, dict):
                self.order_poll_count = counts
            applied = control.get("applied_payload")
            if isinstance(applied, dict) and applied:
                if age is not None and age < DEYE_RESTORE_MAX_CONTROL:
                    self.applied_payload = applied
                else:
                    # Deliberately discarded. This cache asserts the inverter still holds
                    # what Predbat last wrote; after a long gap that may be false, and a
                    # wrongly SKIPPED write leaves the battery diverging from the plan. A
                    # redundant write is the cheaper mistake.
                    self.log(f"Info: DEYE applied-payload cache is stale (age {self._age_text(age)}), the next apply will re-write to the inverter")

    async def refresh_static(self):
        """Re-run discovery and the per-model capability reads, then cache them."""
        # get_device_list() clears device_list when discovery comes back empty. That was
        # harmless when discovery only ran at startup, but this tier re-runs every 8 hours
        # in a long-lived process, and one transient failure must not take a working
        # component down until the next success. Absence of a result is not a result.
        previous_devices, previous_stations = self.device_list, self.station_ids
        await self.get_device_list()
        if not self.device_list:
            if previous_devices:
                self.device_list, self.station_ids = previous_devices, previous_stations
                self.log(f"Warn: DEYE discovery returned no inverters, keeping the {len(previous_devices)} already known")
                return False
            # Neither marked nor saved: an empty discovery must be retried on the next
            # tick, not cached and left for 8 hours.
            return False
        for station_id in self.station_ids:
            try:
                await self.fetch_station_latest(station_id)
            except Exception as e:
                self.log(f"Warn: DEYE station/latest failed for {station_id}: {e}")
        for sn in self.device_list:
            try:
                await self.fetch_measure_points(sn)
            except Exception as e:
                self.log(f"Warn: DEYE device/measurePoints failed for {sn}: {e}")
        self.mark_refreshed("static")
        await self.save_static()
        return True

    async def refresh_config(self):
        """Re-read config/battery for every device and cache it when anything came back."""
        got_any = False
        for sn in self.device_list:
            try:
                if await self.fetch_battery_config(sn):
                    got_any = True
            except Exception as e:
                self.log(f"Warn: DEYE config/battery failed for {sn}: {e}")
        # The clock is started either way so a model that rejects this config point
        # entirely retries on the tier cadence rather than on every single tick, but the
        # cache is only written when there is something worth keeping.
        self.mark_refreshed("config")
        if got_any:
            await self.save_config()
        return got_any

    async def refresh_live(self):
        """Poll device/latest for every device, then cache ratings (telemetry is not persisted)."""
        got_any = False
        for sn in self.device_list:
            try:
                if await self.fetch_device_data(sn):
                    got_any = True
            except Exception as e:
                self.log(f"Warn: DEYE device/latest failed for {sn}: {e}")
        if got_any:
            self.mark_refreshed("live")
            # Only the ratings are cached, and only when they change — the telemetry itself
            # is not persisted (see DEYE_CACHE_* in deye_const.py).
            await self.save_ratings()
        return got_any

    async def run(self, seconds, first):
        """Main component loop: auth, restore, tiered refresh, publish, configure."""
        if self.auth_method == "app_credentials" and not getattr(self, "access_token", None):
            if not await self.fetch_token():
                self.log("Warn: DEYE token unavailable, skipping run")
                return False
        if not await self.check_and_refresh_oauth_token():
            self.log("Warn: DEYE OAuth token invalid, skipping run")
            return False

        if first and not self._cache_restored:
            # Once per process. Restoring before any polling is what lets the TTL checks
            # below skip work a previous run already did.
            self._cache_restored = True
            await self.restore_state()

        # Discovery also refreshes whenever there are no devices to work with: without
        # that guard a failed discovery would be retried only once the 8 hour TTL expired.
        if self.tier_expired("static", DEYE_TTL_STATIC) or not self.device_list:
            await self.refresh_static()
        if not self.device_list:
            self.log("Error: DEYE no inverters found")
            return False

        if self.tier_expired("config", DEYE_TTL_CONFIG):
            await self.refresh_config()

        live_ok = True
        if self.tier_expired("live", DEYE_TTL_LIVE):
            live_ok = await self.refresh_live()

        # Free: reads the HA control entities rather than the API, so it runs every tick
        # regardless of tier.
        for sn in self.device_list:
            try:
                await self.get_schedule_settings_ha(sn)
            except Exception as e:
                self.log(f"Warn: DEYE schedule read failed for {sn}: {e}")

        await self.publish_data()
        for sn in self.device_list:
            await self.publish_schedule_settings_ha(sn)

        # Drain any control orders left pending by apply_dynamic_control() every cycle (not
        # just on first run) so a write that is HTTP-accepted but then fails to apply on the
        # device doesn't stay masked forever behind the applied-payload change-detection cache.
        orders_changed = bool(self.pending_orders)
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
        if orders_changed:
            # Only when orders were actually drained, so an idle cycle does not rewrite the
            # cache and needlessly reset the applied-payload age used on restore.
            await self.save_control()

        await self._reconcile_control()

        if first and not live_ok:
            # Startup has not really succeeded without telemetry: automatic_config() would
            # map only the args backed by cached ratings and permanently skip the rest,
            # because it runs on the first cycle alone. Returning False leaves first set,
            # so ComponentBase retries the whole startup path on its backoff (60s doubling
            # to 128 minutes) until a poll comes back.
            self.log("Warn: DEYE first poll returned no telemetry, deferring startup; it will be retried after a backoff")
            return False

        if first and self.automatic:
            await self.automatic_config()

        # Report the cycle as successful. components.is_alive() requires a success within
        # the last 60 minutes, and with no timestamp ever recorded it returns False for the
        # whole session — so every Predbat run ended "Complete run status ... with component
        # errors: DEYE Cloud" despite nothing having failed.
        self.update_success_timestamp()
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
    if args.key or args.token_hash:
        # Predbat.com SaaS: token injected/refreshed via OAuthMixin (Supabase edge function).
        if args.supabase_url:
            os.environ["SUPABASE_URL"] = args.supabase_url
        if args.supabase_key:
            os.environ["SUPABASE_KEY"] = args.supabase_key
        # --key is the injected ACCESS TOKEN, the same value Predbat.com supplies as
        # deye_key. Without it the tool starts with an empty bearer, so the first call is
        # rejected and only the refresh rescues it — which does not exercise the path
        # production actually takes.
        arg_dict.update({"auth_method": "oauth", "key": args.key or "", "token_hash": args.token_hash or "", "token_expires_at": args.token_expires})
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
    parser.add_argument("--key", default=None, help="Injected OAuth access token (Predbat.com SaaS mode, the deye_key value)")
    parser.add_argument("--token-hash", default=None, help="Injected OAuth token hash, the refresh dedup handle (Predbat.com SaaS mode)")
    parser.add_argument("--token-expires", default=None, help="OAuth token expiry timestamp")
    parser.add_argument("--supabase-url", default=None, help="Supabase URL for OAuth token refresh")
    parser.add_argument("--supabase-key", default=None, help="Supabase anon key for OAuth token refresh")
    parser.add_argument("--user-id", default=None, help="Supabase user ID for OAuth token refresh")
    parser.add_argument("--write-schedule", action="store_true", help="Opt in to writing a test charge window (default is read-only)")

    args = parser.parse_args()

    has_app_creds = all([args.app_id, args.app_secret, args.username, args.password])
    if not args.key and not args.token_hash and not has_app_creds:
        parser.error("provide either --key (SaaS, optionally with --token-hash) or all of --app-id/--app-secret/--username/--password (add-on)")

    asyncio.run(test_deye_api(args))


if __name__ == "__main__":  # pragma: no cover
    main()
