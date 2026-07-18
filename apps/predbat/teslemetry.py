# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Tesla Powerwall integration via the Teslemetry API.

Teslemetry (https://api.teslemetry.com) proxies the Tesla Fleet API using the
same URI paths, authenticated with a static customer-supplied token (BYOK).
The base URL is configurable so a direct Fleet API connection can be swapped
in later without changing this component.

Data path:  polls live_status (power flows + SOC), site_info (capacity) and
calendar_history (daily energy) and publishes predbat_teslemetry_* sensors.
Control path: exposes fox-style virtual schedule entities (charge/discharge window
time selects, SoC numbers, enable switches, a reserve number and an atomic write
button) that inverter.py programs directly via the TESLA inverter type. Because the
Powerwall has no native scheduler, run() acts as one: each cycle it evaluates the
committed windows against the wall clock and live SOC and asserts the resulting
device tuple (tariff / export rule / grid charging / backup reserve / operation
mode) through deduped write-on-change commands, so unchanged cycles cost no
Teslemetry command credits and failed writes self-retry. All emulator writes are
gated on Predbat's set_read_only configuration.
"""

import argparse
import asyncio
import copy
import json
import sys
from datetime import datetime, timezone

import aiohttp

from component_base import ComponentBase

TESLEMETRY_DEFAULT_URL = "https://api.teslemetry.com"
TESLEMETRY_TIMEOUT = 30
TESLEMETRY_RETRIES = 3
LIVE_POLL_SECONDS = 120
# Daily energy totals are polled every 5 minutes: Predbat derives its 5-minute energy buckets from
# the change in these cumulative counters, so they must keep pace with the plan interval. The
# calendar_history response is large (there is no smaller "totals only" endpoint - the library's
# energy_history() just wraps calendar_history), so its payload is truncated in the debug log.
ENERGY_POLL_SECONDS = 300
RECONCILE_MAX_ATTEMPTS = 5
# Approximate usable capacity per Powerwall unit (kWh), used only to ESTIMATE soc_max when the API
# exposes no capacity field at all (observed on some Powerwall 3 firmware, whose site_info and
# live_status omit total_pack_energy/nameplate_energy/energy_left). Users can override soc_max in apps.yaml.
# Powerwall 1 (6.4 kWh) is told apart from Powerwall 2/3 (13.5 kWh) by its much lower inverter power
# (~3.3 kW vs ~5 kW / ~11.5 kW), since nameplate_power is exposed even when capacity is not.
POWERWALL_PACK_KWH = 13.5
POWERWALL_1_PACK_KWH = 6.4
POWERWALL_1_MAX_POWER = 3500  # Per-unit nameplate power at/below this is treated as a Powerwall 1.

OPERATION_MODES = ["self_consumption", "autonomous", "backup"]
EXPORT_RULES = ["never", "pv_only", "battery_ok"]
TARIFF_MODES = ["normal", "export_now"]

OPTIONS_TIME_FULL = ["{:02d}:{:02d}:00".format(hour, minute) for hour in range(24) for minute in range(60)]

DEFAULT_SCHEDULE = {
    "reserve": 20,
    "charge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 100, "enable": 0},
    "discharge": {"start_time": "00:00:00", "end_time": "00:00:00", "soc": 10, "enable": 0},
}


class TeslemetryAPI(ComponentBase):
    """Tesla Powerwall component using the Teslemetry (or Fleet) REST API."""

    EXPORT_SELL_RATE = 0.50  # GBP/kWh synthetic high sell price to force export now.
    DEFAULT_IMPORT_RATE = 0.28
    DEFAULT_EXPORT_RATE = 0.15

    def initialize(self, key="", site_id="", base_url=TESLEMETRY_DEFAULT_URL, automatic=False, **kwargs):
        """Initialise the Teslemetry component from configuration.

        Args:
            key: Teslemetry (or Fleet API) bearer token.
            site_id: Optional Tesla energy site id, or list of ids, used to FILTER the sites discovered
                from /api/1/products. Empty means "use whatever the account returns". The active site is
                resolved by discovery in run(); for now only the first matching site is used.
            base_url: REST API base URL (Teslemetry by default, swappable for a direct Fleet API connection).
            automatic: Automatically configure Predbat's inverter args to use this component (fox-style).
            kwargs: Reserved for future control-path configuration (accepted and ignored here).
        """
        self.api_key = key
        # site_id acts as a filter over the sites discovered from /api/1/products (see discover_site),
        # not a hardcoded target. It may be a single id, a list of ids, or empty (use all account sites).
        if isinstance(site_id, (list, tuple)):
            self.site_filter = [str(entry) for entry in site_id if entry not in (None, "")]
        elif site_id:
            self.site_filter = [str(site_id)]
        else:
            self.site_filter = []
        self.site_id = ""  # Resolved from discover_site() in run().
        self.base_url = base_url
        self.api_auth_failed = False
        self.last_live_poll = 0
        self.last_energy_poll = 0
        self.site_info_done = False
        self.reconcile_done = False
        self.reconcile_attempts = 0
        self.last_soc = None
        self.soc_max_real = False  # True once soc_max is published from a real device value (not the estimate).
        self._last_sent = {}
        self.automatic = automatic
        self.automatic_done = False
        self.schedule = copy.deepcopy(DEFAULT_SCHEDULE)
        self.pending_schedule = copy.deepcopy(DEFAULT_SCHEDULE)
        self.schedule_loaded = False
        self.log("Info: TeslemetryAPI initialising site filter={}".format(self.site_filter or "all account sites"))
        self.log("Info: Teslemetry control drift-correction is transition-based (self-heals when Predbat's own desired value changes, plus a one-off refresh at boot) - full periodic device-state reconciliation is a pilot follow-up")
        self.register_control_entities()

    def entity(self, suffix, domain="sensor"):
        """Build a prefixed virtual entity id for this component."""
        return "{}.{}_teslemetry_{}".format(domain, self.prefix, suffix)

    async def _request(self, method, path, json_body=None):
        """Make a Teslemetry REST request, returning parsed JSON or None on failure."""
        url = "{}{}".format(self.base_url, path)
        headers = {"Authorization": "Bearer {}".format(self.api_key), "Content-Type": "application/json"}
        timeout = aiohttp.ClientTimeout(total=TESLEMETRY_TIMEOUT)
        for attempt in range(TESLEMETRY_RETRIES):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.request(method, url, headers=headers, json=json_body) as resp:
                        if resp.status in (401, 403):
                            self.api_auth_failed = True
                            self.log("Warn: Teslemetry auth failed ({}) on {} - token revoked or subscription lapsed".format(resp.status, path))
                            return None
                        if resp.status == 429:
                            self.log("Warn: Teslemetry rate limited on {} attempt {}".format(path, attempt + 1))
                            await asyncio.sleep(2 ** (attempt + 1))
                            continue
                        if resp.status >= 400:
                            body = await resp.text()
                            self.log("Warn: Teslemetry HTTP {} on {}: {}".format(resp.status, path, body[:200]))
                            return None
                        self.api_auth_failed = False
                        result = await resp.json()
                        # Log the raw response so live output can be cross-checked against the parsing
                        # here (beta debugging aid), with the bulky history time_series and tariff/rate
                        # blocks summarised so the endpoints stay readable without a wall of data.
                        self.log("Info: Teslemetry API {} {} status {} response: {}".format(method, path, resp.status, json.dumps(self._summarize_for_log(result), default=str)))
                        return result
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                self.log("Warn: Teslemetry network error on {} attempt {}: {}".format(path, attempt + 1, err))
                await asyncio.sleep(2**attempt)
        return None

    async def discover_site(self):
        """Resolve the active energy site id from /api/1/products, filtered by the configured site_id(s).

        The account's products are fetched and reduced to energy sites (products carrying an
        `energy_site_id`; vehicles and other products are ignored). If a site_id filter is configured,
        only matching sites are kept. For now a single site is supported, so the first remaining
        candidate is used and any others are logged (full multi-site support is a follow-up).

        Sets self.site_id and returns True on success; returns False (leaving site_id empty) when the
        products read fails or no site matches, so run() retries on a later cycle.
        """
        data = await self._request("GET", "/api/1/products")
        if not data:
            return False
        candidates = []
        for product in data.get("response", []) or []:
            if not isinstance(product, dict):
                continue
            site = product.get("energy_site_id")
            if site is None:
                continue
            site = str(site)
            if self.site_filter and site not in self.site_filter:
                continue
            candidates.append(site)
        if not candidates:
            if self.site_filter:
                self.log("Warn: Teslemetry none of the configured site ids {} were found in the account products".format(self.site_filter))
            else:
                self.log("Warn: Teslemetry no energy sites found on the account")
            return False
        if len(candidates) > 1:
            self.log("Info: Teslemetry {} sites available {} - using the first ({}); multi-site is a follow-up".format(len(candidates), candidates, candidates[0]))
        self.site_id = candidates[0]
        self.log("Info: Teslemetry using energy site id {}".format(self.site_id))
        return True

    @staticmethod
    def _summarize_for_log(data):
        """Return a copy of an API response with bulky fields summarised for logging.

        The calendar_history time_series (and SmartBreakerEnergyLogs) and the site_info tariff blocks
        are huge; replace them with a short placeholder (entry count / tariff code) so the log shows
        which endpoint returned what without dumping ~150 KB of history or a full ToU rate table.
        """
        if not isinstance(data, dict) or not isinstance(data.get("response"), dict):
            return data
        response = dict(data["response"])
        for key in ("time_series", "SmartBreakerEnergyLogs"):
            if isinstance(response.get(key), list):
                response[key] = "[{} entries hidden]".format(len(response[key]))
        for key in ("tariff_content", "tariff_content_v2"):
            if isinstance(response.get(key), dict):
                response[key] = "[hidden, code={}]".format(response[key].get("code"))
        return {"response": response}

    def publish_sensor(self, suffix, state, unit=None, state_class="measurement", friendly=None):
        """Publish one virtual sensor via the dashboard."""
        attributes = {"friendly_name": friendly or suffix.replace("_", " ").title(), "state_class": state_class}
        if unit:
            attributes["unit_of_measurement"] = unit
        self.dashboard_item(self.entity(suffix), state, attributes, app="teslemetry")

    def publish_soc_max(self, kwh, estimate=False):
        """Publish the battery capacity (soc_max) in kWh, preferring a real device value over an estimate.

        Different Powerwall firmwares expose capacity in different places (total_pack_energy in
        live_status, nameplate_energy in site_info) or nowhere at all - in the last case this is
        estimated from battery_count. A later real value upgrades a previously published estimate,
        but an estimate never overwrites a real value.
        """
        if estimate and self.soc_max_real:
            return
        self.publish_sensor("soc_max", round(kwh, 2), unit="kWh", state_class=None, friendly="Powerwall Capacity")
        if not estimate:
            self.soc_max_real = True
        self.log("Info: Teslemetry soc_max = {} kWh ({})".format(round(kwh, 2), "estimated from battery_count - set soc_max manually if wrong" if estimate else "from device"))

    def estimate_pack_kwh(self, response):
        """Estimate total usable battery capacity (kWh) from a site_info response with no capacity field.

        Powerwall 1 (6.4 kWh) has a much lower inverter than Powerwall 2/3 (13.5 kWh), so the per-unit
        nameplate power (site nameplate_power / battery_count) tells them apart: at or below
        POWERWALL_1_MAX_POWER it is a Powerwall 1, otherwise a Powerwall 2/3. The gateway part name is
        logged for confirmation. The per-unit figure is multiplied by battery_count for the site total.
        """
        battery_count = response.get("battery_count") or 1
        nameplate_power = response.get("nameplate_power") or 0
        per_unit_power = (nameplate_power / battery_count) if battery_count else nameplate_power
        is_pw1 = 0 < per_unit_power <= POWERWALL_1_MAX_POWER
        per_unit_kwh = POWERWALL_1_PACK_KWH if is_pw1 else POWERWALL_PACK_KWH
        part_names = ", ".join(str(gateway.get("part_name", "")) for gateway in response.get("components", {}).get("gateways", []) or []) or "unknown"
        self.log("Info: Teslemetry capacity estimate: {} x {} kWh ({}, per-unit inverter ~{} W, part={})".format(battery_count, per_unit_kwh, "Powerwall 1" if is_pw1 else "Powerwall 2/3", int(per_unit_power), part_names))
        return battery_count * per_unit_kwh

    async def fetch_live_status(self):
        """Fetch live power flows and SOC, publishing power sensors."""
        data = await self._request("GET", "/api/1/energy_sites/{}/live_status".format(self.site_id))
        if not data:
            return False
        response = data.get("response", {})
        self.last_soc = response.get("percentage_charged", self.last_soc)
        self.publish_sensor("soc", round(float(response.get("percentage_charged", 0) or 0), 2), unit="%", friendly="Powerwall SOC")
        # Capacity (soc_max): prefer total_pack_energy (Wh); else derive from energy_left / percentage.
        # Both live in live_status per the HA integration, but some PW3 firmware omits them entirely,
        # in which case site_info's battery_count estimate (below) is the only source.
        total_pack_wh = response.get("total_pack_energy")
        energy_left_wh = response.get("energy_left")
        pct = response.get("percentage_charged")
        if total_pack_wh:
            self.publish_soc_max(total_pack_wh / 1000.0)
        elif energy_left_wh and pct:
            self.publish_soc_max((energy_left_wh / (pct / 100.0)) / 1000.0)
        self.publish_sensor("battery_power", response.get("battery_power", 0), unit="W", friendly="Powerwall Battery Power")
        self.publish_sensor("grid_power", response.get("grid_power", 0), unit="W", friendly="Powerwall Grid Power")
        self.publish_sensor("load_power", response.get("load_power", 0), unit="W", friendly="Powerwall Load Power")
        self.publish_sensor("solar_power", response.get("solar_power", 0), unit="W", friendly="Powerwall Solar Power")
        self.update_success_timestamp()
        return True

    async def fetch_site_info(self):
        """Fetch site info, publishing static config sensors and seeding control entity states.

        Returns True whenever a site_info response was received (regardless of whether soc_max could
        be published), so the run()-level site_info_done latch and automatic_config are not blocked on
        a capacity field this hardware may not expose. soc_max is published here from nameplate_energy
        when present, but the reliable source on PW3 is total_pack_energy from live_status.
        """
        data = await self._request("GET", "/api/1/energy_sites/{}/site_info".format(self.site_id))
        if not data:
            return False
        response = data.get("response", {})
        nameplate_wh = response.get("nameplate_energy", 0)
        battery_count = response.get("battery_count")
        if nameplate_wh:
            self.publish_soc_max(nameplate_wh / 1000.0)
        elif battery_count:
            # No capacity field on this firmware (e.g. PW3): estimate from battery_count and model. A
            # real value from live_status (total_pack_energy) will upgrade this if/when it appears.
            self.publish_soc_max(self.estimate_pack_kwh(response), estimate=True)
        nameplate_power = response.get("nameplate_power", 0)
        if nameplate_power:
            self.publish_sensor("battery_rate_max", nameplate_power, unit="W", state_class=None, friendly="Powerwall Max Rate")
        site_limit = response.get("max_site_meter_power_ac", 0)
        if not site_limit or site_limit <= 0 or site_limit > 1_000_000:
            # max_site_meter_power_ac is frequently an "unlimited" sentinel (e.g. 1e9) or absent;
            # fall back to the nameplate power rather than publishing an absurd inverter limit.
            site_limit = nameplate_power
        if site_limit and site_limit < 100:
            # Some sites report this field in kW; normalise to W.
            site_limit = site_limit * 1000
        if site_limit:
            self.publish_sensor("inverter_limit", int(site_limit), unit="W", state_class=None, friendly="Powerwall Site Limit")
        # Seed the control entity STATES (display only, no commands) from the device so they reflect
        # reality at boot instead of the hardcoded defaults set by register_control_entities().
        #
        # Known limitation (accepted, not an oversight): fetch_site_info is only ever driven to
        # completion ONCE per process - run()'s site_info_done latch stops calling it again once it
        # returns True - so the operation_mode/backup_reserve drift-refresh below corrects drift at
        # BOOT only. If the device drifts again mid-run (e.g. the customer changes mode/reserve via
        # the Tesla app after startup), that drift is not detected again until the process restarts.
        # In the meantime it self-heals only on a Predbat "transition" - i.e. whenever Predbat's OWN
        # desired value actually changes, since a changed target never matches the stale cache
        # regardless of the device's true state. A repeated assertion of the SAME target while the
        # device has silently drifted away stays stuck until the next boot. This is a ship-now,
        # close-in-pilot decision; continuous periodic device-state reconciliation is a follow-up.
        default_mode = response.get("default_real_mode")
        if default_mode in OPERATION_MODES:
            self.publish_control(self.entity("operation_mode", domain="select"), default_mode)
            # Drift correction: refresh the dedupe cache to the device's ACTUAL mode. If it matches what
            # Predbat last sent, the cache is unchanged (no spurious resend); if the user changed it
            # externally (e.g. the Tesla app), the cache now reflects that drift, so the next assertion
            # of Predbat's desired mode finds a mismatch and re-POSTs instead of being silently skipped.
            self._last_sent["operation_mode"] = default_mode
        backup_reserve = response.get("backup_reserve_percent")
        if isinstance(backup_reserve, (int, float)):
            self.publish_control(self.entity("backup_reserve", domain="number"), backup_reserve)
            self._last_sent["backup_reserve"] = int(backup_reserve)  # Same drift-correction rationale as operation_mode above.
        return True

    async def fetch_energy_today(self):
        """Fetch today's cumulative energy, publishing daily kWh sensors.

        The Fleet/Teslemetry calendar_history endpoint requires a "kind" and a "period" query
        parameter; without them it does not reliably return the daily energy time_series.
        """
        data = await self._request("GET", "/api/1/energy_sites/{}/calendar_history?kind=energy&period=day".format(self.site_id))
        if not data:
            return False
        series = data.get("response", {}).get("time_series", [])
        solar = grid_import = grid_export = load = 0.0
        for entry in series:
            solar += entry.get("solar_energy_exported", 0)
            grid_import += entry.get("grid_energy_imported", 0)
            grid_export += entry.get("grid_energy_exported_from_solar", 0) + entry.get("grid_energy_exported_from_battery", 0) + entry.get("grid_energy_exported_from_generator", 0)
            load += entry.get("consumer_energy_imported_from_grid", 0) + entry.get("consumer_energy_imported_from_solar", 0) + entry.get("consumer_energy_imported_from_battery", 0) + entry.get("consumer_energy_imported_from_generator", 0)
        self.publish_sensor("solar_today", round(solar / 1000.0, 3), unit="kWh", state_class="total_increasing", friendly="Powerwall Solar Today")
        self.publish_sensor("import_today", round(grid_import / 1000.0, 3), unit="kWh", state_class="total_increasing", friendly="Powerwall Import Today")
        self.publish_sensor("export_today", round(grid_export / 1000.0, 3), unit="kWh", state_class="total_increasing", friendly="Powerwall Export Today")
        self.publish_sensor("load_today", round(load / 1000.0, 3), unit="kWh", state_class="total_increasing", friendly="Powerwall Load Today")
        return True

    async def run(self, seconds=0, first=False):
        """Main component loop: poll data at the configured cadences.

        Returns True on a successful (or gated no-op) cycle and False on failure —
        ComponentBase.start() uses this to set api_started, clear the startup flag
        and drive its retry backoff, so a failing cycle must return False.

        Boot reconciliation is gated on the `reconcile_done` latch (mirroring the `site_info_done`
        pattern) rather than on `first`: if the very first cycle 401s, the auth-failed fast-path
        above returns before reconcile ever runs, but `first` is still consumed by that cycle. Gating
        on a latch instead means reconcile still runs on the first cycle that is NOT auth-failed,
        however many cycles that takes to arrive - a boot that 401s then recovers still reconciles.

        The retry is bounded: each failed `reconcile_on_start()` attempt increments
        `reconcile_attempts`, and once it reaches `RECONCILE_MAX_ATTEMPTS` the latch is force-set
        (with a one-time warning) so the emulator assert below is never permanently gated off by a
        device whose tariff_rate response never yields a usable code (an unvalidated shape on live
        hardware would otherwise wedge `reconcile_done` at False forever).
        """
        if not self.api_key:
            self.log("Warn: Teslemetry key not configured, skipping run")
            return False
        if self.api_auth_failed:
            # Do not hammer the API with a dead token; only one probe per cycle checks recovery.
            # A successful probe clears the flag in _request; a failed one returns False so
            # ComponentBase keeps backing off. Until a site is resolved the probe is discovery
            # itself (which also needs a valid token), otherwise a live_status poll.
            if seconds - self.last_live_poll >= LIVE_POLL_SECONDS:
                self.last_live_poll = seconds
                if not self.site_id:
                    return await self.discover_site()
                return await self.fetch_live_status()
            return False
        # Resolve the active site from /api/1/products (site_id filters the result); retry until it resolves.
        if not self.site_id and not await self.discover_site():
            return False
        success = True
        if not self.site_info_done:
            self.site_info_done = await self.fetch_site_info()
        if not self.reconcile_done:
            self.reconcile_done = await self.reconcile_on_start()
            if not self.reconcile_done:
                self.reconcile_attempts += 1
                if self.reconcile_attempts >= RECONCILE_MAX_ATTEMPTS:
                    self.reconcile_done = True
                    self.log("Warn: Teslemetry boot reconciliation abandoned after {} attempts - proceeding without crash-recovery check".format(self.reconcile_attempts))
        if not self.schedule_loaded:
            await self.load_schedule()
            self.schedule_loaded = True
            self.publish_schedule_entities()
        if first or (seconds - self.last_live_poll >= LIVE_POLL_SECONDS):
            self.last_live_poll = seconds
            success = await self.fetch_live_status()
        if first or (seconds - self.last_energy_poll >= ENERGY_POLL_SECONDS):
            self.last_energy_poll = seconds
            await self.fetch_energy_today()
        # Auto-config once site info is read AND a battery-capacity sensor (soc_max) exists. soc_max may
        # come from live_status (total_pack_energy) rather than site_info on some hardware (e.g. PW3), so
        # this is gated on the published sensor and runs after the live poll rather than on site_info alone.
        if self.automatic and not self.automatic_done and self.site_info_done and self.get_state_wrapper(self.entity("soc_max")) is not None:
            await self.automatic_config()
            self.automatic_done = True
        if self.schedule_loaded and self.reconcile_done and self.last_soc is not None and not self._is_read_only():
            # Scheduler emulator: the Powerwall has no native scheduler, so translate the committed
            # windows into device commands each cycle. Failures are logged and self-retry via the
            # dedupe cache; they do not fail the run() data path.
            await self.assert_device_state(self.evaluate_schedule(self.get_minutes_now(), self.last_soc))
        return success

    def register_control_entities(self):
        """Publish the virtual control entities with their initial states.

        Also records each entity's display attributes (options/min/max/friendly_name) in
        self.control_attrs, so publish_control can re-apply them on every subsequent write instead
        of a bare value-only write silently replacing them with an empty attributes dict.
        """
        controls = (
            ("operation_mode", "select", "self_consumption", {"friendly_name": "Powerwall Operation Mode", "options": OPERATION_MODES}),
            ("backup_reserve", "number", 0, {"friendly_name": "Powerwall Backup Reserve", "min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"}),
            ("allow_charging_from_grid", "switch", "on", {"friendly_name": "Powerwall Allow Grid Charging"}),
            ("allow_export", "select", "never", {"friendly_name": "Powerwall Allow Export", "options": EXPORT_RULES}),
            ("tariff_mode", "select", "normal", {"friendly_name": "Powerwall Tariff Mode", "options": TARIFF_MODES}),
        )
        self.control_attrs = {}
        for suffix, domain, initial_state, attributes in controls:
            entity_id = self.entity(suffix, domain=domain)
            self.control_attrs[entity_id] = attributes
            self.dashboard_item(entity_id, initial_state, attributes, app="teslemetry")
        self.publish_schedule_entities()

    def publish_control(self, entity_id, value):
        """Write a control entity's new state, re-applying the attributes register_control_entities
        recorded for it at init (options/min/max/friendly_name).

        A bare set_state_wrapper(entity, value) write defaults attributes to {}, which silently
        replaces the options/min/max/friendly_name published once at startup and never resent, so
        every select/number/switch write - and the reconcile/site_info writes that mirror device
        state into these same entities - must route through here instead of calling
        set_state_wrapper directly.
        """
        attrs = getattr(self, "control_attrs", {}).get(entity_id, {})
        self.dashboard_item(entity_id, value, attrs, app="teslemetry")

    @staticmethod
    def time_to_minutes(value):
        """Convert an HH:MM[:SS] time string to minutes since midnight, or 0 on garbage."""
        try:
            parts = str(value).split(":")
            return int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError):
            return 0

    @staticmethod
    def in_window(minutes_now, window):
        """Return True if minutes_now falls inside an enabled schedule window (inclusive start, exclusive end, midnight wrap supported; start == end means disabled)."""
        if not window.get("enable"):
            return False
        start = TeslemetryAPI.time_to_minutes(window.get("start_time", "00:00:00"))
        end = TeslemetryAPI.time_to_minutes(window.get("end_time", "00:00:00"))
        if start == end:
            return False
        if end > start:
            return start <= minutes_now < end
        return minutes_now >= start or minutes_now < end

    def evaluate_schedule(self, minutes_now, soc):
        """Map the committed schedule + wall clock + live SOC to the desired device tuple.

        Returns a dict with keys tariff_mode, export_rule, grid_charging, reserve and mode.
        Charge window wins over an overlapping discharge window (matches execute.py ordering).
        Charging uses backup mode (proven template semantics); the hold state (SOC at target,
        which is also how Predbat expresses charge freeze) is backup + grid charging off.
        Export uses the tariff-trick + autonomous with the device reserve as the discharge floor.
        Idle allows PV-only export so excess solar is never curtailed.
        """
        charge = self.schedule.get("charge", {})
        discharge = self.schedule.get("discharge", {})
        reserve = self.schedule.get("reserve", 20)
        if self.in_window(minutes_now, charge):
            target = int(charge.get("soc", 100))
            if soc >= target:
                return {"tariff_mode": "normal", "export_rule": "never", "grid_charging": False, "reserve": target, "mode": "backup"}
            return {"tariff_mode": "normal", "export_rule": "never", "grid_charging": True, "reserve": target, "mode": "backup"}
        if self.in_window(minutes_now, discharge):
            target = int(discharge.get("soc", 10))
            if soc > target:
                return {"tariff_mode": "export_now", "export_rule": "battery_ok", "grid_charging": False, "reserve": target, "mode": "autonomous"}
            return {"tariff_mode": "normal", "export_rule": "pv_only", "grid_charging": False, "reserve": target, "mode": "self_consumption"}
        return {"tariff_mode": "normal", "export_rule": "pv_only", "grid_charging": True, "reserve": int(reserve), "mode": "self_consumption"}

    def publish_schedule_entities(self):
        """Publish the schedule entities from the pending schedule (pending == committed after boot/apply).

        Entity states must track pending edits immediately so inverter.py's write-and-poll
        verification sees its own writes reflected back.
        """
        sched = self.pending_schedule
        self.dashboard_item(
            self.entity("schedule_reserve", domain="number"),
            sched.get("reserve", 20),
            {"friendly_name": "Powerwall Schedule Reserve", "min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "icon": "mdi:gauge"},
            app="teslemetry",
        )
        for direction in ["charge", "discharge"]:
            window = sched.get(direction, {})
            for attribute in ["start_time", "end_time"]:
                self.dashboard_item(
                    self.entity("schedule_{}_{}".format(direction, attribute), domain="select"),
                    window.get(attribute, "00:00:00"),
                    {"options": OPTIONS_TIME_FULL, "friendly_name": "Powerwall Schedule {} {}".format(direction.capitalize(), attribute.replace("_", " ").capitalize()), "icon": "mdi:clock-outline"},
                    app="teslemetry",
                )
            self.dashboard_item(
                self.entity("schedule_{}_soc".format(direction), domain="number"),
                window.get("soc", 100 if direction == "charge" else 10),
                {"friendly_name": "Powerwall Schedule {} Soc".format(direction.capitalize()), "min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "icon": "mdi:gauge"},
                app="teslemetry",
            )
            self.dashboard_item(
                self.entity("schedule_{}_enable".format(direction), domain="switch"),
                "on" if window.get("enable") else "off",
                {"friendly_name": "Powerwall Schedule {} Enable".format(direction.capitalize()), "icon": "mdi:check-circle-outline"},
                app="teslemetry",
            )
        self.dashboard_item(self.entity("schedule_write", domain="switch"), "off", {"friendly_name": "Powerwall Schedule Write", "icon": "mdi:content-save-outline"}, app="teslemetry")

    async def apply_schedule(self):
        """Commit the pending schedule atomically, persist it, and assert the device state immediately rather than waiting up to 60s for the next run cycle."""
        self.schedule = copy.deepcopy(self.pending_schedule)
        await self.save_schedule()
        self.publish_schedule_entities()
        if self.last_soc is not None and not self._is_read_only():
            await self.assert_device_state(self.evaluate_schedule(self.get_minutes_now(), self.last_soc))

    async def save_schedule(self):
        """Persist the committed schedule via the Storage component (no-op when storage is unavailable)."""
        storage = self.storage
        if storage:
            await storage.save("teslemetry", "schedule", self.schedule, format="json")

    async def load_schedule(self):
        """Restore the committed schedule from storage at boot; keep safe defaults when absent.

        The Powerwall has no native scheduler to read the plan back from (unlike Fox Cloud),
        so persistence is what makes a schedule survive a restart mid-plan.
        """
        storage = self.storage
        data = await storage.load("teslemetry", "schedule") if storage else None
        if isinstance(data, dict) and "charge" in data and "discharge" in data:
            self.schedule = data
        self.pending_schedule = copy.deepcopy(self.schedule)

    def get_minutes_now(self):
        """Return minutes since local midnight from the live wall clock in the site's local timezone.

        Deliberately not base.minutes_now: that value is only refreshed on Predbat's 5-minute
        update cycles, which would freeze the emulator's clock between cycles (and indefinitely
        if the main loop stalls), enforcing window boundaries late or never.
        """
        now = datetime.now(timezone.utc).astimezone(getattr(self, "local_tz", None) or timezone.utc)
        return now.hour * 60 + now.minute

    async def assert_device_state(self, desired):
        """Assert the desired device tuple, tariff first and mode last (the template-proven ordering).

        Each setter dedupes on write-on-change, so an unchanged assert costs zero command credits.
        Successful writes are mirrored into the diagnostic control entities; failures leave both
        the dedupe cache and the entity state untouched so the next cycle retries.
        """
        results = {}
        results["tariff_mode"] = await self.set_tariff(desired["tariff_mode"])
        results["export_rule"] = await self.set_export_rule(desired["export_rule"])
        results["grid_charging"] = await self.set_grid_charging(desired["grid_charging"])
        results["reserve"] = await self.set_backup_reserve(desired["reserve"])
        results["mode"] = await self.set_operation_mode(desired["mode"])
        if results["tariff_mode"]:
            self.publish_control(self.entity("tariff_mode", domain="select"), desired["tariff_mode"])
        if results["export_rule"]:
            self.publish_control(self.entity("allow_export", domain="select"), desired["export_rule"])
        if results["grid_charging"]:
            self.publish_control(self.entity("allow_charging_from_grid", domain="switch"), "on" if desired["grid_charging"] else "off")
        if results["reserve"]:
            self.publish_control(self.entity("backup_reserve", domain="number"), int(desired["reserve"]))
        if results["mode"]:
            self.publish_control(self.entity("operation_mode", domain="select"), desired["mode"])
        if not all(results.values()):
            self.log("Warn: Teslemetry device-state assert incomplete: {}".format({key: value for key, value in results.items() if not value}))
        return all(results.values())

    async def automatic_config(self):
        """Automatically wire Predbat's inverter args to this component's virtual entities (fox parity).

        With teslemetry_automatic enabled the user needs no manual inverter configuration in
        apps.yaml: the TESLA inverter type plus these args make inverter.py program the
        schedule entities directly and the emulator drive the device.

        battery_rate_max/inverter_limit are wired conditionally: fetch_site_info only publishes
        those two sensors when the site_info response actually carried nameplate_power/
        max_site_meter_power_ac, so wiring them unconditionally would point Predbat at entities
        that never exist on a site missing those fields. Predbat falls back to its own defaults
        for absent args, so skipping the wiring here is safe.
        """
        self.log("Info: Teslemetry automatic configuration - wiring Predbat to the TESLA inverter type")
        self.set_arg("inverter_type", ["TESLA"])
        self.set_arg("num_inverters", 1)
        self.set_arg("inverter_reserve_max", 80)
        self.set_arg("soc_percent", [self.entity("soc")])
        self.set_arg("soc_max", [self.entity("soc_max")])
        self.set_arg("battery_power", [self.entity("battery_power")])
        self.set_arg("battery_power_invert", [False])
        self.set_arg("grid_power", [self.entity("grid_power")])
        self.set_arg("grid_power_invert", [True])
        self.set_arg("load_power", [self.entity("load_power")])
        self.set_arg("pv_power", [self.entity("solar_power")])
        self.set_arg("load_today", [self.entity("load_today")])
        self.set_arg("import_today", [self.entity("import_today")])
        self.set_arg("export_today", [self.entity("export_today")])
        self.set_arg("pv_today", [self.entity("solar_today")])
        if self.get_state_wrapper(self.entity("battery_rate_max")) is not None:
            self.set_arg("battery_rate_max", [self.entity("battery_rate_max")])
        if self.get_state_wrapper(self.entity("inverter_limit")) is not None:
            self.set_arg("inverter_limit", [self.entity("inverter_limit")])
        self.set_arg("reserve", [self.entity("schedule_reserve", domain="number")])
        self.set_arg("charge_start_time", [self.entity("schedule_charge_start_time", domain="select")])
        self.set_arg("charge_end_time", [self.entity("schedule_charge_end_time", domain="select")])
        self.set_arg("charge_limit", [self.entity("schedule_charge_soc", domain="number")])
        self.set_arg("scheduled_charge_enable", [self.entity("schedule_charge_enable", domain="switch")])
        self.set_arg("discharge_start_time", [self.entity("schedule_discharge_start_time", domain="select")])
        self.set_arg("discharge_end_time", [self.entity("schedule_discharge_end_time", domain="select")])
        self.set_arg("discharge_target_soc", [self.entity("schedule_discharge_soc", domain="number")])
        self.set_arg("scheduled_discharge_enable", [self.entity("schedule_discharge_enable", domain="switch")])
        self.set_arg("schedule_write_button", [self.entity("schedule_write", domain="switch")])

    async def schedule_event(self, entity_id, value):
        """Stage a schedule entity write into pending_schedule; the write switch commits it.

        Reserve applies immediately (fox parity) since it is not part of window atomicity.
        Invalid times fall back to 00:00:00 and non-numeric SOC/reserve values are rejected.
        """
        if entity_id.endswith("_schedule_write"):
            if value in ("turn_on", "toggle"):
                await self.apply_schedule()
            self.publish_schedule_entities()
            return
        if entity_id.endswith("_schedule_reserve"):
            try:
                reserve = max(0, min(100, int(float(value))))
            except (ValueError, TypeError):
                self.log("Warn: Teslemetry invalid schedule reserve value {}".format(value))
                return
            self.pending_schedule["reserve"] = reserve
            self.schedule["reserve"] = reserve
            await self.save_schedule()
            self.publish_schedule_entities()
            return
        direction = None
        if "_schedule_charge_" in entity_id:
            direction = "charge"
        elif "_schedule_discharge_" in entity_id:
            direction = "discharge"
        if not direction:
            self.log("Warn: Teslemetry unhandled schedule event {} = {}".format(entity_id, value))
            return
        window = self.pending_schedule.setdefault(direction, {})
        if entity_id.endswith("_start_time") or entity_id.endswith("_end_time"):
            attribute = "start_time" if entity_id.endswith("_start_time") else "end_time"
            window[attribute] = value if value in OPTIONS_TIME_FULL else "00:00:00"
        elif entity_id.endswith("_soc"):
            try:
                window["soc"] = max(0, min(100, int(float(value))))
            except (ValueError, TypeError):
                self.log("Warn: Teslemetry invalid schedule soc value {}".format(value))
                return
        elif entity_id.endswith("_enable"):
            if value == "turn_on":
                window["enable"] = 1
            elif value == "turn_off":
                window["enable"] = 0
            elif value == "toggle":
                window["enable"] = 0 if window.get("enable") else 1
        else:
            self.log("Warn: Teslemetry unhandled schedule event {} = {}".format(entity_id, value))
            return
        self.publish_schedule_entities()

    async def _command(self, path, body):
        """POST a command; return True only if both the transport and the application layer succeeded.

        A 2xx HTTP status with a parsed JSON body is not sufficient on its own: Teslemetry/Fleet API can
        return HTTP 200 carrying an application-level failure, so the body is also checked for a truthy
        "error" key or a numeric "response.code" of 400 or above.
        """
        result = await self._request("POST", "/api/1/energy_sites/{}/{}".format(self.site_id, path), json_body=body)
        if result is None:
            return False
        if isinstance(result, dict):
            if result.get("error"):
                self.log("Warn: Teslemetry command to {} failed (application error): {}".format(path, str(result)[:200]))
                return False
            response = result.get("response")
            if isinstance(response, dict):
                code = response.get("code")
                if isinstance(code, (int, float)) and not isinstance(code, bool) and code >= 400:
                    self.log("Warn: Teslemetry command to {} failed (response code {}): {}".format(path, code, str(result)[:200]))
                    return False
        return True

    async def _apply_command(self, key, signature, sender, force=False):
        """Send a control command only when its value has actually changed, to cut Teslemetry command-credit spend.

        `key` identifies which control this is ("operation_mode", "backup_reserve", "grid_charging",
        "export_rule" or "tariff") and `signature` is a comparable representation of the value being
        asserted (e.g. the mode string, or a canonical JSON serialisation of a built tariff body).
        When `signature` already matches the last confirmed-sent value for `key`, `sender` is not
        called at all - this is the actual command-credit saving, since the TESLA service hooks fire
        their loopback event every cycle regardless of whether anything changed.

        Invariant (failure-retry): the dedupe cache is updated ONLY after a confirmed-successful send
        (a True return from `sender`), never on a skip or a failure. A dropped/failed command therefore
        leaves the cache stale, so the very next cycle's loopback event re-sends the same value instead
        of being silently skipped forever - failure-retry is preserved even though every-cycle sends of
        an unchanged value are deduped away.

        `force=True` bypasses the cache check and always calls `sender`, still refreshing the cache on
        success. This is used by `reconcile_on_start` so its corrective writes can never be silently
        skipped by a (present or future) pre-seeded cache entry that happens to already match what the
        recovery is about to send.

        Drift-refresh limitation (accepted, not an oversight): unlike "operation_mode" and
        "backup_reserve" (refreshed once per process from the device's actual state - see the NOTE in
        `fetch_site_info`), "grid_charging" and "export_rule" are NEVER device-drift-refreshed at all,
        because the site_info response this component reads does not expose either field's actual
        device state. If the device drifts on these two externally, the cache is never proactively
        corrected; they self-heal only when Predbat's own desired value changes (a transition), since a
        changed target never matches the stale cache irrespective of the device's true state.
        """
        if not force and self._last_sent.get(key) == signature:
            return True
        ok = await sender()
        if ok:
            self._last_sent[key] = signature
        return ok

    async def set_operation_mode(self, mode, force=False):
        """Set the Powerwall operation mode (self_consumption/autonomous/backup), deduped on write-on-change."""
        return await self._apply_command("operation_mode", mode, lambda: self._command("operation", {"default_real_mode": mode}), force=force)

    async def set_backup_reserve(self, percent, force=False):
        """Set the backup reserve percentage (cloud caps at 80; 80-100 treated as 100 by Predbat), deduped on write-on-change."""
        percent = int(percent)
        return await self._apply_command("backup_reserve", percent, lambda: self._command("backup", {"backup_reserve_percent": percent}), force=force)

    async def set_grid_charging(self, allow, force=False):
        """Allow or disallow charging the battery from the grid, deduped on write-on-change."""
        return await self._apply_command("grid_charging", bool(allow), lambda: self._command("grid_import_export", {"disallow_charge_from_grid_with_solar_installed": not allow}), force=force)

    async def set_export_rule(self, rule, force=False):
        """Set the grid export rule (never/pv_only/battery_ok), deduped on write-on-change."""
        return await self._apply_command("export_rule", rule, lambda: self._command("grid_import_export", {"customer_preferred_export_rule": rule}), force=force)

    def current_rates(self):
        """Return (import_rate, export_rate) in GBP/kWh from Predbat's rate data when available.

        Defaults to class constants if rates are unavailable. Rates from the base are in PENCE
        (pence per kWh), so they are divided by 100.0 to convert to GBP.
        """
        import_rate = self.DEFAULT_IMPORT_RATE
        export_rate = self.DEFAULT_EXPORT_RATE
        base = getattr(self, "base", None)
        if base is not None:
            minutes_now = getattr(base, "minutes_now", None)
            rate_import = getattr(base, "rate_import", None)
            rate_export = getattr(base, "rate_export", None)
            if minutes_now is not None and rate_import:
                import_rate = round(rate_import.get(minutes_now, self.DEFAULT_IMPORT_RATE * 100) / 100.0, 4)
            if minutes_now is not None and rate_export:
                export_rate = round(rate_export.get(minutes_now, self.DEFAULT_EXPORT_RATE * 100) / 100.0, 4)
        return import_rate, export_rate

    def build_tariff(self, mode, now=None):
        """Build a tariff_content_v2 dict for the requested mode.

        export_now: current 30-minute-aligned window becomes ON_PEAK with a
        high sell price so autonomous mode exports immediately; the window
        self-expires. normal: flat tariff from the customer's current rates.

        The Powerwall applies tou_periods windows in the SITE'S LOCAL wall-clock time (the same
        clock Predbat schedules against), not UTC, so when `now` is not injected (production use)
        it defaults to the base's local time - base.now_utc converted to base.local_tz - rather than
        a bare UTC clock read, which would misplace the window by the site's UTC offset. Falls back
        to system UTC only when no base is wired up (e.g. some unit tests), since there is then no
        site-local timezone to derive from.
        """
        if now is None:
            base = getattr(self, "base", None)
            base_now_utc = getattr(base, "now_utc", None) if base is not None else None
            if base_now_utc is not None:
                local_tz = getattr(base, "local_tz", None) or getattr(self, "local_tz", None) or timezone.utc
                now = base_now_utc.astimezone(local_tz)
            else:
                now = datetime.now(timezone.utc)
        import_rate, export_rate = self.current_rates()
        # Keep the synthetic ON_PEAK sell strictly above the live export rate, otherwise the
        # export trick silently inverts at the highest-value moments.
        sell_high = max(self.EXPORT_SELL_RATE, round(export_rate * 2, 4))

        def charges(off_peak_buy, on_peak_buy):
            """Build an energy_charges block."""
            rates = {"SUPER_OFF_PEAK": off_peak_buy}
            if mode == "export_now":
                rates["ON_PEAK"] = on_peak_buy
            return {"ALL": {"rates": {"ALL": 0}}, "AllYear": {"rates": rates}}

        tou_periods = {"SUPER_OFF_PEAK": {"periods": [{"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 0, "fromMinute": 0, "toHour": 0, "toMinute": 0}]}}
        if mode == "export_now":
            start = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
            end_minute_total = start.hour * 60 + start.minute + 60
            wrapped = end_minute_total >= 24 * 60
            end_hour, end_min = (end_minute_total // 60) % 24, end_minute_total % 60
            window = {"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": start.hour, "fromMinute": start.minute, "toHour": end_hour, "toMinute": end_min}
            off_periods = []
            if wrapped:
                # ON_PEAK crosses midnight, so its circle complement is a single segment from the wrapped end back to the start.
                off_periods.append({"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": end_hour, "fromMinute": end_min, "toHour": start.hour, "toMinute": start.minute})
            else:
                if start.hour > 0 or start.minute > 0:
                    off_periods.append({"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": 0, "fromMinute": 0, "toHour": start.hour, "toMinute": start.minute})
                if end_hour > 0 or end_min > 0:
                    off_periods.append({"fromDayOfWeek": 0, "toDayOfWeek": 6, "fromHour": end_hour, "fromMinute": end_min, "toHour": 0, "toMinute": 0})
            tou_periods = {"SUPER_OFF_PEAK": {"periods": off_periods}, "ON_PEAK": {"periods": [window]}}

        seasons = {"AllYear": {"fromMonth": 1, "fromDay": 1, "toMonth": 12, "toDay": 31, "tou_periods": tou_periods}}
        sell_rates = charges(export_rate, sell_high)
        # Buy-side ON_PEAK mirrors the high sell rate to discourage grid-charging during the export window.
        buy_rates = charges(import_rate, sell_high)
        common = {
            "min_applicable_demand": 0,
            "max_applicable_demand": 0,
            "monthly_minimum_bill": 0,
            "monthly_charges": 0,
            "daily_charges": [{"name": "Charge", "amount": 0}],
            "demand_charges": {"ALL": {"rates": {"ALL": 0}}, "AllYear": {"rates": {}}},
        }
        return {
            "version": 1,
            "utility": "Predbat",
            "code": "PREDBAT-{}".format(mode.upper().replace("_", "-")),
            "name": "Predbat ({})".format(mode),
            "currency": "GBP",
            "daily_demand_charges": {},
            "energy_charges": buy_rates,
            "seasons": seasons,
            "sell_tariff": {**common, "utility": "Predbat", "energy_charges": sell_rates, "seasons": seasons},
            **common,
        }

    async def set_tariff(self, mode, force=False):
        """Push the tariff for the requested mode via time_of_use_settings, deduped on write-on-change.

        The dedupe signature is a canonical (sort_keys) JSON serialisation of the BUILT tariff body
        rather than just `mode`: the export_now window and sell price are time-varying (the ON_PEAK
        window advances every 30 minutes and the sell price tracks the live export rate), so the same
        `mode` string can legitimately need re-sending when the underlying built tariff has changed,
        while an unchanged build (the common case every 5-minute cycle) is skipped.
        """
        tariff = self.build_tariff(mode)
        signature = json.dumps(tariff, sort_keys=True)
        return await self._apply_command("tariff", signature, lambda: self._command("time_of_use_settings", {"tou_settings": {"tariff_content_v2": tariff}}), force=force)

    @staticmethod
    def _find_tariff_code(node):
        """Recursively search a parsed JSON structure for the first string "code" key.

        The Teslemetry/Fleet API tariff_rate response shape is not guaranteed to nest the
        code at one fixed path (top-level, under tariff_content_v2, or elsewhere), so this
        walks dicts and lists looking for a "code" key rather than assuming a fixed path.
        """
        if isinstance(node, dict):
            code = node.get("code")
            if isinstance(code, str):
                return code
            for value in node.values():
                found = TeslemetryAPI._find_tariff_code(value)
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = TeslemetryAPI._find_tariff_code(item)
                if found is not None:
                    return found
        return None

    async def get_current_tariff_code(self):
        """Read the site's current tariff "code" from site_info, or None on failure.

        There is no dedicated tariff-read endpoint on the Fleet/Teslemetry API (a GET /tariff_rate
        returns 404); the active tariff is carried in the site_info response under tariff_content_v2
        (or tariff_content). Logs distinct warnings for the read itself failing versus a response that
        carries no tariff code.
        """
        data = await self._request("GET", "/api/1/energy_sites/{}/site_info".format(self.site_id))
        if not data:
            self.log("Warn: Teslemetry site_info read failed - cannot determine current tariff")
            return None
        response = data.get("response", {}) if isinstance(data, dict) else {}
        # Prefer tariff_content_v2 (where Predbat writes its PREDBAT-* marker); fall back to the v1
        # tariff_content (which carries the customer's real tariff code on observed PW3 hardware).
        tariff_v2 = response.get("tariff_content_v2")
        code = self._find_tariff_code(tariff_v2) if isinstance(tariff_v2, dict) else None
        if code is None:
            tariff_v1 = response.get("tariff_content")
            code = self._find_tariff_code(tariff_v1) if isinstance(tariff_v1, dict) else None
        if code is None:
            self.log("Warn: Teslemetry site_info carried no tariff code (tariff_content_v2/tariff_content absent or code-less)")
        return code

    def _is_read_only(self):
        """Return True if Predbat is configured read-only, so device-write commands must not be sent.

        Reads the configuration via get_arg rather than the base's set_read_only attribute: the attribute
        defaults to True in the PredBat constructor and is only refreshed from config during the fetch cycle,
        which runs AFTER phase-1 components start - so at reconcile_on_start time the attribute would always
        read True even for read-write instances. load_user_config(load_config=True) runs before phase-1
        component start, so get_arg returns the real configured value here.
        """
        if getattr(self, "base", None) is None:
            return False
        return bool(self.get_arg("set_read_only", False))

    async def reconcile_on_start(self):
        """Restore a safe state if a previous run died mid-export.

        Reads the Powerwall's ACTUAL current tariff from the device rather than the local
        entity mirror: register_control_entities() unconditionally reseeds the mirror to
        "normal" on every boot, so trusting it would make this recovery path unreachable.
        If the device's tariff still carries the PREDBAT-EXPORT-NOW marker written by
        build_tariff(), a previous run crashed mid-export, so normal tariff and disabled
        export are restored (unless Predbat is running read-only, in which case the need
        for recovery is logged but no write is sent). If no tariff code can be determined
        (read failure or code-less response, each logged distinctly by
        get_current_tariff_code), this skips rather than guessing at the device state.

        Returns True once this has actually executed against a live API (the tariff read
        returned a usable code, whether or not a restore was needed), so run() can latch
        reconcile_done and stop retrying. Returns False when the tariff read itself failed to
        return anything usable, so run() retries on a later cycle instead of latching a no-op -
        bounded to RECONCILE_MAX_ATTEMPTS retries by run(), which force-latches reconcile_done
        after that many consecutive failures so a device that never yields a usable tariff code
        cannot permanently gate the scheduler emulator off.

        The restore writes below pass force=True to `set_tariff`/`set_export_rule` so this recovery
        path can NEVER be silently skipped by the write-on-change dedupe cache - at boot the cache is
        empty so this is naturally a no-op today, but forcing makes that explicit and future-proof
        against a cache-preseed feature that might otherwise make recovery look like a no-op.
        """
        tariff_code = await self.get_current_tariff_code()
        if tariff_code is None:
            return False
        export_now_code = self.build_tariff("export_now").get("code")
        if tariff_code != export_now_code:
            return True
        if self._is_read_only():
            self.log("Warn: Teslemetry device tariff was left as {} - recovery needed but skipped (read-only mode)".format(tariff_code))
            return True
        self.log("Warn: Teslemetry device tariff was left as {} - restoring normal tariff and disabling export".format(tariff_code))
        if await self.set_tariff("normal", force=True):
            self.publish_control(self.entity("tariff_mode", domain="select"), "normal")
        if await self.set_export_rule("never", force=True):
            self.publish_control(self.entity("allow_export", domain="select"), "never")
        return True

    async def select_event(self, entity_id, value):
        """Handle select changes routed from the service-hook loopback."""
        if "_schedule_" in entity_id:
            await self.schedule_event(entity_id, value)
            return
        success = False
        if entity_id.endswith("_operation_mode") and value in OPERATION_MODES:
            success = await self.set_operation_mode(value)
        elif entity_id.endswith("_allow_export") and value in EXPORT_RULES:
            success = await self.set_export_rule(value)
        elif entity_id.endswith("_tariff_mode") and value in TARIFF_MODES:
            success = await self.set_tariff(value)
        else:
            self.log("Warn: Teslemetry unhandled select_event {} = {}".format(entity_id, value))
            return
        if success:
            self.publish_control(entity_id, value)
        else:
            self.log("Warn: Teslemetry command failed for {} = {} (state not updated)".format(entity_id, value))

    async def number_event(self, entity_id, value):
        """Handle number changes routed from the service-hook loopback.

        Guards the int(float(value)) conversion: a number.increment/decrement service call can
        forward value=None, which would otherwise raise a TypeError out of the dispatch loop.
        """
        if "_schedule_" in entity_id:
            await self.schedule_event(entity_id, value)
            return
        if entity_id.endswith("_backup_reserve"):
            try:
                percent = max(0, min(100, int(float(value))))
            except (ValueError, TypeError):
                self.log("Warn: Teslemetry invalid number_event value for {}: {}".format(entity_id, value))
                return
            if await self.set_backup_reserve(percent):
                self.publish_control(entity_id, percent)
            else:
                self.log("Warn: Teslemetry backup_reserve command failed (state not updated)")
        else:
            self.log("Warn: Teslemetry unhandled number_event {} = {}".format(entity_id, value))

    async def switch_event(self, entity_id, service):
        """Handle switch service calls routed from the service-hook loopback."""
        if "_schedule_" in entity_id:
            await self.schedule_event(entity_id, service)
            return
        if entity_id.endswith("_allow_charging_from_grid"):
            allow = service != "turn_off"
            if await self.set_grid_charging(allow):
                self.publish_control(entity_id, "on" if allow else "off")
            else:
                self.log("Warn: Teslemetry grid charging command failed (state not updated)")
        else:
            self.log("Warn: Teslemetry unhandled switch_event {} {}".format(entity_id, service))

    async def final(self):
        """Cleanup on shutdown."""
        self.log("Info: TeslemetryAPI shutdown")


class MockBase:  # pragma: no cover
    """Mock base object for standalone Teslemetry testing (stands in for the PredBat instance)."""

    def __init__(self):
        """Initialise the mock base with a local-time clock and empty entity/arg stores."""
        self.local_tz = datetime.now().astimezone().tzinfo
        self.now_utc = datetime.now(self.local_tz)
        self.prefix = "predbat"
        self.args = {}
        self.midnight_utc = datetime.now(self.local_tz).replace(hour=0, minute=0, second=0, microsecond=0)
        self.minutes_now = self.now_utc.hour * 60 + self.now_utc.minute
        self.entities = {}

    def get_state_wrapper(self, entity_id, default=None, attribute=None, refresh=False, required_unit=None, raw=None):
        """Return a previously published entity state (or its raw record)."""
        if raw:
            return self.entities.get(entity_id, {})
        return self.entities.get(entity_id, {}).get("state", default)

    def set_state_wrapper(self, entity_id, state, attributes=None, app=None):
        """Record an entity state update."""
        self.entities[entity_id] = {"state": state, "attributes": attributes or {}}

    def log(self, message):
        """Print a timestamped log line."""
        print("[{}] {}".format(datetime.now().strftime("%H:%M:%S"), message))

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        """Print and record a published dashboard entity."""
        print("ENTITY: {} = {}".format(entity_id, state))
        if attributes and "options" in attributes:
            attributes = {**attributes, "options": "..."}
        self.set_state_wrapper(entity_id, state, attributes)

    def get_arg(self, arg, default=None, indirect=True, combine=False, attribute=None, index=None, domain=None, can_override=True, required_unit=None):
        """Return a configured arg value; consulted so set_read_only actually gates control writes."""
        return self.args.get(arg, default)

    def set_arg(self, key, value):
        """Print an arg assignment made by automatic_config."""
        print("Set arg {} = {}".format(key, value))


async def test_teslemetry_api(key, site_id=None, base_url=None, control=False):
    """Run a standalone test of the Teslemetry component against the live API.

    site_id is optional and acts as a filter over the sites discovered from /api/1/products; when
    omitted the account's first site is used. By default this is READ-ONLY: it polls the Powerwall
    and prints/publishes the entities and status but sends no control commands - the scheduler
    emulator and any crash-recovery writes are suppressed via set_read_only. Pass control=True to
    let the component send commands.

    Returns True only if the run connected and completed successfully. A failed connection -
    an auth failure (401/403) or an unsuccessful run() - returns False so main() can exit
    non-zero instead of a broken connection looking like a pass.
    """
    mode = "READ-WRITE (controls may change)" if control else "READ-ONLY (status only, no controls changed)"
    print("Testing Teslemetry API for site {} - {}".format(site_id or "auto-discover", mode))

    mock_base = MockBase()
    # Read-only by default so a bare test run only reports status and never changes the Powerwall.
    mock_base.args["set_read_only"] = not control

    arg_dict = {"key": key, "site_id": site_id or "", "automatic": True}
    if base_url:
        arg_dict["base_url"] = base_url
    api = TeslemetryAPI(mock_base, **arg_dict)

    print("Calling run() once...")
    result = await api.run(seconds=0, first=True)
    await api.final()

    if api.api_auth_failed:
        print("FAILED: Teslemetry authentication failed (401/403) - check the --key token and --site-id are correct")
        return False
    if not result:
        print("FAILED: Teslemetry run did not complete successfully - see the warnings above")
        return False
    print("SUCCESS: Teslemetry run completed")
    return True


def main():  # pragma: no cover
    """Command line entry point to test the Teslemetry component against the live API."""
    parser = argparse.ArgumentParser(description="Test Teslemetry Tesla Powerwall API")
    parser.add_argument("--key", required=True, help="Teslemetry (or Fleet API) bearer token")
    parser.add_argument("--site-id", default=None, help="Optional Tesla energy site id to filter the sites discovered from /api/1/products (default: use the first site on the account)")
    parser.add_argument("--base-url", default=None, help="REST API base URL (default {})".format(TESLEMETRY_DEFAULT_URL))
    parser.add_argument("--control", action="store_true", help="Allow control commands to be sent (default is read-only: report status only, change nothing)")

    args = parser.parse_args()
    ok = asyncio.run(test_teslemetry_api(args.key, args.site_id, base_url=args.base_url, control=args.control))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
