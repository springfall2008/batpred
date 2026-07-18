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
Control path: exposes virtual select/number/switch entities driven by the
TESLA service hooks; handlers issue REST commands (operation mode, backup
reserve, grid import/export rules and the export tariff-trick).

Command dedupe: the TESLA service hooks carry `repeat: true`, so Predbat fires
each hook's loopback event every cycle (~5 minutes) even when nothing has
changed. Every control write is routed through `_apply_command`, which only
POSTs when the value actually differs from the last CONFIRMED-SENT value,
cutting Teslemetry command-credit spend without losing failure-retry (a
failed POST never updates the cache) or external-drift correction (see
`fetch_site_info`).
"""

import asyncio
import copy
import json
from datetime import datetime, timezone

import aiohttp

from component_base import ComponentBase

TESLEMETRY_DEFAULT_URL = "https://api.teslemetry.com"
TESLEMETRY_TIMEOUT = 30
TESLEMETRY_RETRIES = 3
LIVE_POLL_SECONDS = 120
ENERGY_POLL_SECONDS = 300

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
            site_id: Tesla energy site id to poll and control.
            base_url: REST API base URL (Teslemetry by default, swappable for a direct Fleet API connection).
            automatic: Automatically configure Predbat's inverter args to use this component (fox-style).
            kwargs: Reserved for future control-path configuration (accepted and ignored here).
        """
        self.api_key = key
        self.site_id = str(site_id) if site_id else ""
        self.base_url = base_url
        self.api_auth_failed = False
        self.last_live_poll = 0
        self.last_energy_poll = 0
        self.site_info_done = False
        self.reconcile_done = False
        self.last_soc = None
        self._last_sent = {}
        self.automatic = automatic
        self.automatic_done = False
        self.schedule = copy.deepcopy(DEFAULT_SCHEDULE)
        self.pending_schedule = copy.deepcopy(DEFAULT_SCHEDULE)
        self.schedule_loaded = False
        self.log("Info: TeslemetryAPI initialising site_id={}".format(self.site_id))
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
                        return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                self.log("Warn: Teslemetry network error on {} attempt {}: {}".format(path, attempt + 1, err))
                await asyncio.sleep(2**attempt)
        return None

    def publish_sensor(self, suffix, state, unit=None, state_class="measurement", friendly=None):
        """Publish one virtual sensor via the dashboard."""
        attributes = {"friendly_name": friendly or suffix.replace("_", " ").title(), "state_class": state_class}
        if unit:
            attributes["unit_of_measurement"] = unit
        self.dashboard_item(self.entity(suffix), state, attributes, app="teslemetry")

    async def fetch_live_status(self):
        """Fetch live power flows and SOC, publishing power sensors."""
        data = await self._request("GET", "/api/1/energy_sites/{}/live_status".format(self.site_id))
        if not data:
            return False
        response = data.get("response", {})
        self.last_soc = response.get("percentage_charged", self.last_soc)
        self.publish_sensor("soc", response.get("percentage_charged", 0), unit="%", friendly="Powerwall SOC")
        self.publish_sensor("battery_power", response.get("battery_power", 0), unit="W", friendly="Powerwall Battery Power")
        self.publish_sensor("grid_power", response.get("grid_power", 0), unit="W", friendly="Powerwall Grid Power")
        self.publish_sensor("load_power", response.get("load_power", 0), unit="W", friendly="Powerwall Load Power")
        self.publish_sensor("solar_power", response.get("solar_power", 0), unit="W", friendly="Powerwall Solar Power")
        self.update_success_timestamp()
        return True

    async def fetch_site_info(self):
        """Fetch site info, publishing the battery capacity as soc_max and seeding control entity states.

        Returns True only once soc_max has actually been published. If nameplate_energy is absent or
        zero, control-entity seeding still happens with whatever fields are present, but this returns
        False so the run()-level site_info_done latch stays False and the fetch is retried on a later
        cycle instead of permanently giving up on soc_max.
        """
        data = await self._request("GET", "/api/1/energy_sites/{}/site_info".format(self.site_id))
        if not data:
            return False
        response = data.get("response", {})
        nameplate_wh = response.get("nameplate_energy", 0)
        soc_max_published = False
        if nameplate_wh:
            self.publish_sensor("soc_max", round(nameplate_wh / 1000.0, 2), unit="kWh", state_class=None, friendly="Powerwall Capacity")
            soc_max_published = True
        nameplate_power = response.get("nameplate_power", 0)
        if nameplate_power:
            self.publish_sensor("battery_rate_max", nameplate_power, unit="W", state_class=None, friendly="Powerwall Max Rate")
        site_limit = response.get("max_site_meter_power_ac", 0) or nameplate_power
        if site_limit and site_limit < 100:
            # Some sites report this field in kW; normalise to W
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
        return soc_max_published

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
        """
        if not self.api_key or not self.site_id:
            self.log("Warn: Teslemetry key or site_id not configured, skipping run")
            return False
        if self.api_auth_failed:
            # Do not hammer the API with a dead token; only live_status probes recovery.
            # A successful probe clears the flag in _request; a failed one returns False
            # so ComponentBase keeps backing off.
            if seconds - self.last_live_poll >= LIVE_POLL_SECONDS:
                self.last_live_poll = seconds
                return await self.fetch_live_status()
            return False
        success = True
        if not self.site_info_done:
            self.site_info_done = await self.fetch_site_info()
        if not self.reconcile_done:
            self.reconcile_done = await self.reconcile_on_start()
        if first or (seconds - self.last_live_poll >= LIVE_POLL_SECONDS):
            self.last_live_poll = seconds
            success = await self.fetch_live_status()
        if first or (seconds - self.last_energy_poll >= ENERGY_POLL_SECONDS):
            self.last_energy_poll = seconds
            await self.fetch_energy_today()
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
        """Fetch the site's current tariff_rate from the device and return its tariff "code", or None on failure.

        Logs distinct warnings for the two failure modes: the read itself failing (network/HTTP error, empty
        body) versus a successful read whose response simply carries no "code" key anywhere.
        """
        data = await self._request("GET", "/api/1/energy_sites/{}/tariff_rate".format(self.site_id))
        if not data:
            self.log("Warn: Teslemetry tariff_rate read failed - no response from the device")
            return None
        code = self._find_tariff_code(data)
        if code is None:
            self.log("Warn: Teslemetry tariff_rate response contained no tariff code: {}".format(str(data)[:200]))
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
        return anything usable, so run() retries on a later cycle instead of latching a no-op.

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
