# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# Sunsynk Connect API Library
# -----------------------------------------------------------------------------

"""Sunsynk Connect Cloud API integration.

Injected-token Bearer client for Sunsynk Connect inverters. Predbat.com's SaaS edge
functions own the Sunsynk RSA publicKey -> login -> access-token exchange and refresh
chain server-side; the ``sunsynk_key`` config value handed to this component IS the
resulting Bearer access token, so ``SunsynkAPI`` never performs RSA signing or login
itself here - it only issues plain Bearer-authenticated requests and flags
``needs_reauth`` when the injected token is rejected, for the SaaS side to notice and
refresh. Device discovery and read-only telemetry are implemented here (Task 9), along with
entity publishing via ``publish_data()`` (Task 10); automatic scheduling and control are
added in later tasks.
"""

import asyncio
import json
import random
from datetime import datetime, timedelta, timezone

import aiohttp

from component_base import ComponentBase
from oauth_mixin import OAuthMixin
from predbat_metrics import record_api_call

SUNSYNK_DOMAIN = "https://api.sunsynk.net"
TIMEOUT = 60
SUNSYNK_RETRIES = 10

# Defined locally (rather than imported from const.py) to avoid dependency issues, matching
# fox.py's TIME_FORMAT_HA approach - components should stay standalone-testable via MockBase.
SUNSYNK_DATE_FORMAT = "%Y-%m-%d"

# Maximum age (minutes) of cached data before an API refresh is triggered, matching fox.py's
# FOX_REFRESH_* constants. STATIC and REALTIME are consumed by the read methods below (device
# list, and flow/battery/grid/input/output telemetry); SETTINGS is not yet used - device
# settings read/write land in a later task.
SUNSYNK_REFRESH_STATIC = 24 * 60  # Device/plant list rarely changes
SUNSYNK_REFRESH_SETTINGS = 60  # Device settings
SUNSYNK_REFRESH_REALTIME = 5  # Real time monitoring data

# Storage cache keys for device data persisted between reboots, matching fox.py's
# FOX_CACHE_KEYS. "device_settings" is not yet used - device settings land in a later task.
SUNSYNK_CACHE_KEYS = ["device_list", "device_settings", "device_values"]

# Sentinel default for _publish_battery_extras()'s `battery` parameter, distinct from a real
# `None` value. `None` means "already fetched this cycle and the fetch failed" (do NOT
# re-fetch - that would hit a failing endpoint twice per publish cycle); the sentinel means
# "not attempted yet" (safe to lazily fetch here). A plain `None` default can't tell these
# apart, since a failed get_battery(sn) call also returns None.
_UNFETCHED = object()


class SunsynkAPI(ComponentBase, OAuthMixin):
    """Sunsynk Connect API client."""

    def initialize(self, key, automatic, automatic_ignore_pv=False, inverter_sn=None, plant_id=None, auth_method=None, token_expires_at=None, token_hash=None, base_url=SUNSYNK_DOMAIN):
        """Initialise the Sunsynk API component."""
        self.key = key
        self.automatic = automatic
        self.automatic_ignore_pv = automatic_ignore_pv
        self.base_url = base_url
        self.failures_total = 0
        # Set on a 401/403 so callers (and the SaaS oauth-refresh edge function, which owns
        # the RSA/login chain) can notice the injected token was rejected; cleared again on
        # the next successful request. This component never attempts a re-login itself.
        self.needs_reauth = False

        # Scopes device discovery to a single plant (config key sunsynk_plant_id, wired by a
        # later task). Multi-plant Sunsynk accounts are a v1 non-goal - see get_device_list().
        self.plant_id = plant_id

        # Cache state. device_settings lands in a later task (empty/unused here).
        self.device_list = []
        self.device_values = {"flow": {}, "battery": {}, "grid": {}, "input": {}, "output": {}}
        self.device_settings = {}
        self.data_age = {}

        # Initialise OAuth bookkeeping (expiry tracking, provider_name etc.) for consistency
        # with fox.py/deye.py, even though Sunsynk's injected-token model means refreshing is
        # done entirely server-side rather than via check_and_refresh_oauth_token().
        self._init_oauth(auth_method=auth_method, key=key, token_expires_at=token_expires_at, provider_name="sunsynk")
        # _init_oauth() only sets self.access_token when auth_method == "oauth" (None
        # otherwise) - but 'key' IS the Bearer access token here regardless of auth_method,
        # so it must be (re)applied after _init_oauth or get_headers() would send "Bearer
        # None" whenever auth_method isn't "oauth".
        self.access_token = key
        # _init_oauth() also unconditionally resets token_hash to "" (see oauth_mixin.py) so
        # the configured value must be applied after it too, exactly as fox.py/deye.py do -
        # otherwise a configured hash is silently discarded and the Predbat.com SaaS dedup
        # keyed on it breaks.
        self.token_hash = token_hash or ""

        # Convert inverter_sn to list, same normalisation as fox.py/deye.py
        if inverter_sn is None:
            self.inverter_sn_filter = []
        elif isinstance(inverter_sn, str):
            self.inverter_sn_filter = [inverter_sn]
        else:
            self.inverter_sn_filter = inverter_sn

    def get_headers(self):
        """Return the plain Bearer authorisation headers used for every Sunsynk request.

        No request signing (unlike fox.py's MD5 signature) - Sunsynk reads use a plain
        Bearer token, and that token is injected/refreshed server-side rather than derived
        here.
        """
        return {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}

    def is_alive(self):
        """Check if the API is alive (component started and holding a usable token)."""
        return self.api_started and bool(self.access_token)

    async def request_get(self, path, post=False, datain=None):
        """
        Retry wrapper around request_get_func.
        """
        retries = 0
        result = None
        self.log("Sunsynk: API Requesting {} {} - data {}".format("POST" if post else "GET", path, datain))

        while retries < SUNSYNK_RETRIES:
            result, allow_retry = await self.request_get_func(path, post=post, datain=datain)
            if result is not None:
                return result
            if not allow_retry:
                break
            retries += 1
            await asyncio.sleep(retries * random.random())
        self.log("Sunsynk: API Response failed after {} retries for {}".format(SUNSYNK_RETRIES, path))
        return result

    async def request_get_func(self, path, post=False, datain=None):
        """
        Perform a single Sunsynk API request.

        Plain Bearer auth, no request signing. On 401/403 there is no re-login attempt -
        the access token is injected and refreshed server-side by the SaaS edge function, so
        a stale token here just sets self.needs_reauth for the caller to notice, rather than
        triggering a login flow this component doesn't implement.
        """
        headers = self.get_headers()
        url = self.base_url + path
        self.log("Sunsynk: API Request: path {} post {} datain {}".format(path, post, datain))

        timeout = aiohttp.ClientTimeout(total=TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if post:
                    async with session.post(url, headers=headers, json=datain) as response:
                        status_code = response.status
                        try:
                            data = await response.json()
                        except (aiohttp.ContentTypeError, json.JSONDecodeError):
                            self.log("Warn: Sunsynk: Failed to decode response from {} code {}".format(url, status_code))
                            data = None
                else:
                    async with session.get(url, headers=headers, params=datain) as response:
                        status_code = response.status
                        try:
                            data = await response.json()
                        except (aiohttp.ContentTypeError, json.JSONDecodeError):
                            self.log("Warn: Sunsynk: Failed to decode response from {} code {}".format(url, status_code))
                            data = None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            self.log(f"Warn: Sunsynk: Exception during request to {url}: {e}")
            self.failures_total += 1
            record_api_call("sunsynk", False, "connection_error")
            return None, False

        if status_code in (401, 403):
            # Injected-token model: no re-login here, just flag it for the caller/SaaS side.
            self.needs_reauth = True
            self.log("Warn: Sunsynk: Authentication error with status code {} from {}".format(status_code, url))
            self.failures_total += 1
            record_api_call("sunsynk", False, "auth_error")
            return None, False

        if status_code in (200, 201):
            if data is None:
                data = {}
            if isinstance(data, dict) and data.get("success") is False:
                # Sunsynk's API family returns HTTP 200 with {"success": false} on failure
                # (e.g. stale token, invalid request) rather than a non-2xx status - treat it
                # the same as a hard failure so a stale-token 200 doesn't silently become an
                # empty "data" payload that gets cached as if it were fresh. Mirrors deye.py's
                # `if not data.get("success", True)` discipline.
                self.failures_total += 1
                self.log("Warn: Sunsynk: API call to {} returned success=false (code {}): {}".format(url, data.get("code"), data.get("msg")))
                record_api_call("sunsynk", False, "api_success_false")
                return None, False
            self.needs_reauth = False
            self.update_success_timestamp()
            record_api_call("sunsynk")
            return data, False

        self.failures_total += 1
        if status_code == 429:
            # Rate limiting so wait up to 30 seconds and allow a retry
            self.log("Info: Sunsynk: Rate limiting detected, waiting...")
            record_api_call("sunsynk", False, "rate_limit")
            await asyncio.sleep(random.random() * 30 + 1)
            return None, True
        record_api_call("sunsynk", False, "server_error")
        return None, False

    async def get_device_list(self):
        """
        Discover the inverter device list for the account, scoped to a single plant.

        Predbat.com's SaaS connect handler already rejects multi-plant Sunsynk accounts at
        onboarding, so multi-plant support is a v1 non-goal here - this always resolves to
        ONE plant. If ``self.plant_id`` is configured, it is used directly; otherwise the
        account's plants are listed and the sole plant used (a warning is logged and the
        first plant used defensively if more than one comes back).

        Real inverters response shape (GET /api/v1/plant/{id}/inverters):
            {"code": 0, "success": true, "data": {"pageSize": 50, "total": 2, "infos": [
                {"id": 260047, "sn": "2504040106", "model": "SUN-50KW-SG01HP3-EU-BM4", "status": 1},
                {"id": 260046, "sn": "2504040164", "model": "SUN-50KW-SG01HP3-EU-BM4", "status": 1}
            ]}}

        Returns a list of ``{"sn": ..., "model": ..., "plant_id": ...}`` dicts, filtered by
        ``self.inverter_sn_filter`` when set. Returns None on API/discovery failure - any
        existing cached list is left untouched so callers can keep using it.
        """
        if self.device_list and not self._needs_refresh("device_list", SUNSYNK_REFRESH_STATIC):
            return self.device_list

        plant_id = self.plant_id
        if not plant_id:
            plant_id = await self._discover_plant_id()
            if plant_id is None:
                return None

        query = {"page": 1, "limit": 50, "status": -1, "type": -2}
        result = await self.request_get("/api/v1/plant/{}/inverters".format(plant_id), datain=query)
        if result is None:
            return None

        infos = (result.get("data") or {}).get("infos", []) or []
        devices = [{"sn": info.get("sn"), "model": info.get("model"), "plant_id": plant_id} for info in infos]
        if self.inverter_sn_filter:
            devices = [device for device in devices if device.get("sn") in self.inverter_sn_filter]

        self.device_list = devices
        await self._save_cache("device_list", devices)
        return devices

    async def _discover_plant_id(self):
        """
        Resolve the account's single plant id by listing plants.

        Real response shape (GET /api/v1/plants):
            {"code": 0, "success": true, "data": {"infos": [
                {"id": 505825, "name": "Virtual power station", "status": 1}
            ]}}

        Returns None on API failure or when the account has no plants. Logs a warning and
        uses the first plant when more than one comes back - the SaaS connect handler already
        rejects multi-plant accounts at onboarding, so this is a defensive fallback only.
        """
        result = await self.request_get("/api/v1/plants", datain={"page": 1, "limit": 100})
        if result is None:
            return None

        infos = (result.get("data") or {}).get("infos", []) or []
        if not infos:
            self.log("Warn: Sunsynk: No plants found for this account")
            return None
        if len(infos) > 1:
            self.log("Warn: Sunsynk: {} plants found for this account - multi-plant is not supported, using the first".format(len(infos)))
        return infos[0].get("id")

    async def get_plant_flow(self, plant_id):
        """
        Get the live power-flow snapshot for a plant (PV/battery/grid/load).

        Real response shape (GET /api/v1/plant/energy/{id}/flow):
            {"code": 0, "success": true, "data": {
                "pvPower": 0, "battPower": 400, "gridOrMeterPower": 3179, "loadOrEpsPower": 3164,
                "soc": 100, "batTo": true, "toBat": false, "gridTo": true, "toGrid": false,
                "toLoad": true, "pvTo": false
            }}

        Returns the parsed 'data' dict, or None on API failure.
        """
        date_str = self.midnight_utc.strftime(SUNSYNK_DATE_FORMAT)
        return await self._get_realtime("flow", plant_id, "/api/v1/plant/energy/{}/flow".format(plant_id), datain={"date": date_str})

    async def get_battery(self, sn):
        """
        Get real-time battery telemetry for an inverter.

        Real response shape (GET /api/v1/inverter/battery/{sn}/realtime):
            {"code": 0, "success": true, "data": {
                "power": 170, "capacity": "100.0", "soc": "100.0", "temp": "17.0", "voltage": "639.8"
            }}

        Returns the parsed 'data' dict, or None on API failure.
        """
        return await self._get_realtime("battery", sn, "/api/v1/inverter/battery/{}/realtime".format(sn), datain={"sn": sn, "lan": "en"})

    async def get_grid(self, sn):
        """
        Get real-time grid telemetry for an inverter.

        Returns the parsed 'data' dict, or None on API failure.
        """
        return await self._get_realtime("grid", sn, "/api/v1/inverter/grid/{}/realtime".format(sn), datain={"sn": sn, "lan": "en"})

    async def get_input(self, sn):
        """
        Get real-time PV/input telemetry for an inverter.

        Returns the parsed 'data' dict, or None on API failure.
        """
        return await self._get_realtime("input", sn, "/api/v1/inverter/{}/realtime/input".format(sn))

    async def get_output(self, sn):
        """
        Get real-time output telemetry for an inverter.

        Returns the parsed 'data' dict, or None on API failure.
        """
        return await self._get_realtime("output", sn, "/api/v1/inverter/{}/realtime/output".format(sn))

    async def _get_realtime(self, bucket, cache_key, path, datain=None):
        """
        Shared cache-and-fetch logic for the realtime telemetry endpoints (flow/battery/grid/
        input/output).

        Freshness is tracked PER bucket+cache_key (e.g. "device_values:battery:SN1"), NOT via
        one shared "device_values" clock - a single shared clock would mean fetching any
        bucket/sn resets the timestamp for every other bucket/sn, so a genuinely-stale entry
        for a DIFFERENT key would be served from cache without ever being fetched again. Returns
        the cached value for ``cache_key`` within ``device_values[bucket]`` when it is already
        populated and still within SUNSYNK_REFRESH_REALTIME for THIS bucket+cache_key pair;
        otherwise polls the API, updates the in-memory bucket and persists the whole
        device_values cache. Returns None on API failure, leaving any existing cached value
        untouched.
        """
        store = self.device_values[bucket]
        age_key = self._realtime_age_key(bucket, cache_key)
        if cache_key in store and not self._needs_refresh(age_key, SUNSYNK_REFRESH_REALTIME):
            return store[cache_key]

        result = await self.request_get(path, datain=datain)
        if result is None:
            return None

        data = result.get("data") or {}
        store[cache_key] = data
        self.data_age[age_key] = datetime.now(timezone.utc)
        # Persists the whole in-memory device_values struct (all buckets/sns together) so a
        # reboot restores everything at once; _save_cache() also stamps its own "device_values"
        # bookkeeping key, which is unrelated to (and must not be confused with) the per-entry
        # age_key freshness tracked above.
        await self._save_cache("device_values", self.device_values)
        return data

    @staticmethod
    def _realtime_age_key(bucket, cache_key):
        """Return the per-entry data_age key for a realtime telemetry bucket+cache_key pair."""
        return "device_values:{}:{}".format(bucket, cache_key)

    def _data_age_minutes(self, key):
        """
        Return the age in minutes of the in-memory data for a cache key, or None if unknown.
        """
        timestamp = self.data_age.get(key, None)
        if timestamp is None:
            return None
        return (datetime.now(timezone.utc) - timestamp).total_seconds() / 60.0

    def _needs_refresh(self, key, max_age_minutes):
        """
        Return True if the data for a cache key is missing or older than max_age_minutes.
        """
        age = self._data_age_minutes(key)
        return age is None or age >= max_age_minutes

    async def _save_cache(self, key, data):
        """
        Save data to storage under the sunsynk module and record its update time in memory.
        """
        now = datetime.now(timezone.utc)
        self.data_age[key] = now
        if self.storage:
            # Expire after a day so stale data doesn't linger in the cache forever
            await self.storage.save("sunsynk", key, data, format="json", expiry=now + timedelta(days=1))

    async def _load_cache(self, key):
        """
        Load cached data for a key from storage, recording its age. Returns None if absent.
        """
        if not self.storage:
            return None
        data = await self.storage.load("sunsynk", key)
        if data is None:
            return None
        age = await self.storage.age("sunsynk", key)
        if age is None:
            return None
        self.data_age[key] = datetime.now(timezone.utc) - timedelta(minutes=age)
        return data

    async def publish_data(self):
        """
        Publish Sunsynk telemetry as Predbat entities via dashboard_item(), fox.py-style.

        The plant-level flow endpoint (get_plant_flow) is PLANT-AGGREGATED - in a
        multi-inverter site it already sums every inverter's contribution. Publishing its
        values under EACH inverter's SN would double-count a multi-inverter site (Task 10
        brief, Codex Critical 3), so this method branches on the device count:

        - Single-inverter plant: plant flow IS that one inverter's data, so it is safe to
          derive battery_power/grid_power/battery_soc/pv_power/load_power directly from
          flow's VERIFIED direction-boolean sign mapping (batTo/toGrid - Risk 5, confirmed
          against live data at the plant level).
        - Multi-inverter plant: per-SN entities instead come from the per-SN battery/grid/
          input/output endpoints (see _publish_multi_inverter_sn()), including load_power
          (sourced from the per-SN output endpoint's AC output, NOT plant flow's
          loadOrEpsPower, which is plant-aggregate and would double-count here too) - and a
          single plant-level summary (sensor.{prefix}_sunsynk_site_*) is published from flow
          separately, since that one is plant-aggregate by design and isn't attributed to
          any single SN.

        battery_temperature and soc_max (from battery.capacity) always come from the per-SN
        battery/{sn}/realtime endpoint in both branches, since flow carries neither.
        """
        devices = self.device_list
        if not devices:
            self.log("Warn: Sunsynk: publish_data() called with no discovered devices - nothing to publish")
            return

        plant_id = devices[0].get("plant_id")
        flow = await self.get_plant_flow(plant_id) if plant_id else None

        entity_name_sensor = f"sensor.{self.prefix}_sunsynk"

        if len(devices) == 1:
            sn = devices[0].get("sn")
            await self._publish_single_inverter(entity_name_sensor, sn, flow)
        else:
            for device in devices:
                sn = device.get("sn")
                if sn:
                    await self._publish_multi_inverter_sn(entity_name_sensor, sn)
                else:
                    self.log("Warn: Sunsynk: skipping device with missing SN: {}".format(device))
            if flow is not None:
                self._publish_site_summary(entity_name_sensor, flow)
            else:
                self.log("Warn: Sunsynk: site - no plant flow data available, skipping site summary")

    async def _publish_single_inverter(self, entity_name_sensor, sn, flow):
        """
        Publish one SN's entities for a SINGLE-inverter plant.

        battery_power/grid_power/battery_soc/pv_power/load_power are derived from the plant
        flow endpoint using the VERIFIED sign mapping - safe here because the plant's flow
        total IS this one inverter's data (no double-counting risk with only one inverter).
        """
        if flow is not None:
            battery_power, grid_power = self._flow_signed_powers(flow)

            self._publish_entity(entity_name_sensor, sn, "battery_power", battery_power, "W", "power", "mdi:battery-charging")
            self._publish_entity(entity_name_sensor, sn, "grid_power", grid_power, "W", "power", "mdi:transmission-tower")
            self._publish_entity(entity_name_sensor, sn, "battery_soc", flow.get("soc"), "%", "battery", "mdi:battery-50")
            self._publish_entity(entity_name_sensor, sn, "pv_power", flow.get("pvPower"), "W", "power", "mdi:solar-power")
            self._publish_entity(entity_name_sensor, sn, "load_power", flow.get("loadOrEpsPower"), "W", "power", "mdi:home-lightning-bolt")
        else:
            self.log("Warn: Sunsynk: {} - no plant flow data available, skipping flow-derived entities".format(sn))

        await self._publish_battery_extras(entity_name_sensor, sn)

    async def _publish_multi_inverter_sn(self, entity_name_sensor, sn):
        """
        Publish one SN's entities for a MULTI-inverter plant, from the PER-SN battery/grid/
        input/output endpoints rather than plant flow (see publish_data() docstring for why).

        # TODO(risk5-perSN): the per-SN battery.power / grid power sign convention is
        # UNCONFIRMED. Risk 5 was only verified at the plant-flow level (the batTo/toGrid
        # direction booleans) - the demo plant used to develop this was near-idle, so these
        # per-SN fields' sign couldn't be cross-checked against a known charge/discharge or
        # import/export state. Confirm against a live, non-idle multi-inverter plant before
        # relying on these signs for control decisions.

        # TODO(risk6-perSN-load): per-SN load source unconfirmed. There is no per-SN "load"
        # endpoint - loadOrEpsPower on plant flow is plant-aggregate and would double-count
        # here (see publish_data() docstring), so load_power is instead derived from the
        # per-SN inverter/{sn}/realtime/output endpoint's AC output (pac/pInv), on the
        # assumption that an inverter's AC output IS its load contribution. That assumption,
        # and the endpoint's shape, are both unconfirmed against a live multi-inverter plant.
        """
        self.log("Warn: Sunsynk: {} - per-SN battery/grid power sign is UNCONFIRMED (only plant-level flow signs are verified) - treat with caution until confirmed against live multi-inverter data".format(sn))

        battery = await self.get_battery(sn)
        if battery is not None:
            self._publish_entity(entity_name_sensor, sn, "battery_power", self._to_float(battery.get("power")), "W", "power", "mdi:battery-charging")
            self._publish_entity(entity_name_sensor, sn, "battery_soc", self._to_float(battery.get("soc")), "%", "battery", "mdi:battery-50")

        await self._publish_battery_extras(entity_name_sensor, sn, battery)

        grid = await self.get_grid(sn)
        if grid is not None:
            self._publish_entity(entity_name_sensor, sn, "grid_power", self._sum_phase_power(grid, ("vip", "meters", "phases"), ("power", "pac", "gridPower")), "W", "power", "mdi:transmission-tower")

        pv_input = await self.get_input(sn)
        if pv_input is not None:
            self._publish_entity(entity_name_sensor, sn, "pv_power", self._sum_phase_power(pv_input, ("pv", "pvStrings", "strings"), ("power", "pac", "pvPower")), "W", "power", "mdi:solar-power")

        # TODO(risk6-perSN-load): per-SN load source unconfirmed - see docstring above.
        self.log("Warn: Sunsynk: {} - per-SN load_power source (inverter AC output) is UNCONFIRMED (not one of the plant-flow-verified quantities) - treat with caution until confirmed against live multi-inverter data".format(sn))
        output = await self.get_output(sn)
        if output is not None:
            self._publish_entity(entity_name_sensor, sn, "load_power", self._sum_phase_power(output, ("vip", "phases"), ("power", "pac", "pInv")), "W", "power", "mdi:home-lightning-bolt")

    async def _publish_battery_extras(self, entity_name_sensor, sn, battery=_UNFETCHED):
        """
        Publish battery_temperature and soc_max (from battery.capacity) for one SN.

        Always sourced from the per-SN battery/{sn}/realtime endpoint - plant flow carries
        neither field. Used by both the single- and multi-inverter publish paths; the
        multi-inverter path passes its already-fetched battery dict in (which may legitimately
        be None if that fetch already failed) to avoid a duplicate request, the single-inverter
        path fetches it here since it has no other need for it.

        ``battery`` defaults to the module-level ``_UNFETCHED`` sentinel rather than ``None``,
        so a genuinely-unfetched battery (lazy-fetch here) can be told apart from an
        already-fetched-and-failed one (``None``, passed explicitly by the multi-inverter
        path) - a plain ``None`` default can't distinguish the two, which meant a battery
        endpoint that had already failed this cycle was silently re-fetched a second time.
        """
        if battery is _UNFETCHED:
            battery = await self.get_battery(sn)
        if battery is None:
            return
        self._publish_entity(entity_name_sensor, sn, "battery_temperature", self._to_float(battery.get("temp")), "°C", "temperature", "mdi:thermometer")
        self._publish_entity(entity_name_sensor, sn, "soc_max", self._to_float(battery.get("capacity")), "Ah", None, "mdi:battery-high")

    def _publish_site_summary(self, entity_name_sensor, flow):
        """
        Publish a single plant-level summary (sensor.{prefix}_sunsynk_site_*) for a MULTI-
        inverter plant, from the plant flow endpoint using the VERIFIED sign mapping.

        This is the one place flow's plant-aggregate values are safe to publish directly in
        a multi-inverter plant - it is attributed to "site", not to any single inverter's SN,
        so there is no double-counting risk (unlike publishing it per-SN would be).
        """
        battery_power, grid_power = self._flow_signed_powers(flow)

        self._publish_entity(entity_name_sensor, "site", "battery_power", battery_power, "W", "power", "mdi:battery-charging")
        self._publish_entity(entity_name_sensor, "site", "grid_power", grid_power, "W", "power", "mdi:transmission-tower")
        self._publish_entity(entity_name_sensor, "site", "battery_soc", flow.get("soc"), "%", "battery", "mdi:battery-50")
        self._publish_entity(entity_name_sensor, "site", "pv_power", flow.get("pvPower"), "W", "power", "mdi:solar-power")
        self._publish_entity(entity_name_sensor, "site", "load_power", flow.get("loadOrEpsPower"), "W", "power", "mdi:home-lightning-bolt")

    def _publish_entity(self, entity_name_sensor, sn, leaf, state, unit, device_class, icon):
        """
        Publish one sensor.{prefix}_sunsynk_{sn}_{leaf} entity via dashboard_item(), using
        fox.py/solis.py-style attributes (friendly_name/unit_of_measurement/device_class/
        state_class/icon).
        """
        entity_id = "{}_{}_{}".format(entity_name_sensor, sn.lower(), leaf)
        attributes = {
            "friendly_name": "Sunsynk {} {}".format(sn, leaf.replace("_", " ").title()),
            "unit_of_measurement": unit,
            "icon": icon,
        }
        if device_class:
            attributes["device_class"] = device_class
            attributes["state_class"] = "measurement"
        self.dashboard_item(entity_id, state=state, attributes=attributes, app="sunsynk")

    @staticmethod
    def _flow_signed_powers(flow):
        """
        Apply the VERIFIED plant-flow sign mapping to battery_power/grid_power.

        battery_power = battPower if batTo else -battPower (discharge +ve, charge -ve);
        grid_power = gridOrMeterPower if toGrid else -gridOrMeterPower (predbat convention:
        export +ve, import -ve). This is Risk 5's verified direction-boolean mapping - the
        single source of truth for it, since a sign error here is the highest-stakes mistake
        in this module. Used by both _publish_single_inverter() (flow IS that one inverter's
        data) and _publish_site_summary() (flow attributed to "site", not any inverter's SN).

        Returns a (battery_power, grid_power) tuple.
        """
        batt_power_raw = flow.get("battPower", 0) or 0
        battery_power = batt_power_raw if flow.get("batTo") else -batt_power_raw
        grid_power_raw = flow.get("gridOrMeterPower", 0) or 0
        grid_power = grid_power_raw if flow.get("toGrid") else -grid_power_raw
        return battery_power, grid_power

    @staticmethod
    def _to_float(value):
        """Best-effort float conversion, returning None (rather than raising) on bad input."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _sum_phase_power(data, list_keys, flat_keys):
        """
        Sum a 'power' field across a per-phase/per-string breakdown, falling back to the
        first present flat field when no breakdown list is found.

        # TODO(risk5-perSN): shape AND sign are UNCONFIRMED for the per-SN grid/input
        # endpoints (see _publish_multi_inverter_sn's TODO) - this is defensive parsing
        # only, not verified against real API responses.
        """
        for key in list_keys:
            entries = data.get(key)
            if isinstance(entries, list) and entries:
                total = 0.0
                counted = False
                for entry in entries:
                    if isinstance(entry, dict) and "power" in entry:
                        power = SunsynkAPI._to_float(entry.get("power"))
                        if power is not None:
                            total += power
                            counted = True
                if counted:
                    return total
        for flat_key in flat_keys:
            if flat_key in data:
                value = SunsynkAPI._to_float(data.get(flat_key))
                if value is not None:
                    return value
        return None


class MockBase:  # pragma: no cover
    """Mock base class for standalone testing"""

    def __init__(self):
        """Initialise minimal base state (timezone, prefix, args, entities)."""
        self.local_tz = datetime.now().astimezone().tzinfo
        self.now_utc = datetime.now(self.local_tz)
        self.prefix = "predbat"
        self.args = {}
        self.midnight_utc = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.minutes_now = self.now_utc.hour * 60 + self.now_utc.minute
        self.entities = {}

    def get_state_wrapper(self, entity_id, default=None, attribute=None, refresh=False, required_unit=None, raw=None):
        """Return a stored entity's state (or the full record when raw=True)."""
        if raw:
            return self.entities.get(entity_id, {})
        else:
            return self.entities.get(entity_id, {}).get("state", default)

    def set_state_wrapper(self, entity_id, state, attributes=None, app=None):
        """Store an entity's state and attributes."""
        self.entities[entity_id] = {"state": state, "attributes": attributes or {}}

    def log(self, message):
        """Print a timestamped log message."""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        """Print and store a dashboard entity update."""
        print(f"ENTITY: {entity_id} = {state}")
        if attributes:
            if "options" in attributes:
                attributes["options"] = "..."
            print(f"  Attributes: {json.dumps(attributes, indent=2)}")
        self.set_state_wrapper(entity_id, state, attributes)

    def get_arg(self, key, default=None):
        """Return a config argument (always the default - no real config backing)."""
        return default

    def set_arg(self, key, value):
        """Print a config argument write for debugging."""
        state = None
        if isinstance(value, str) and "." in value:
            state = self.get_state_wrapper(value, default=None)
        elif isinstance(value, list):
            state = "n/a []"
            for v in value:
                if isinstance(v, str) and "." in v:
                    state = self.get_state_wrapper(v, default=None)
                    break
        else:
            state = "n/a"
        print(f"Set arg {key} = {value} (state={state})")
