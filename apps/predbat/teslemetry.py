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
"""

import asyncio
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


class TeslemetryAPI(ComponentBase):
    """Tesla Powerwall component using the Teslemetry (or Fleet) REST API."""

    EXPORT_SELL_RATE = 0.50  # GBP/kWh synthetic high sell price to force export now.
    DEFAULT_IMPORT_RATE = 0.28
    DEFAULT_EXPORT_RATE = 0.15

    def initialize(self, key="", site_id="", base_url=TESLEMETRY_DEFAULT_URL, **kwargs):
        """Initialise the Teslemetry component from configuration.

        Args:
            key: Teslemetry (or Fleet API) bearer token.
            site_id: Tesla energy site id to poll and control.
            base_url: REST API base URL (Teslemetry by default, swappable for a direct Fleet API connection).
            kwargs: Reserved for future control-path configuration (accepted and ignored here).
        """
        self.api_key = key
        self.site_id = str(site_id) if site_id else ""
        self.base_url = base_url
        self.api_auth_failed = False
        self.last_live_poll = 0
        self.last_energy_poll = 0
        self.site_info_done = False
        self.log("Info: TeslemetryAPI initialising site_id={}".format(self.site_id))
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
        self.publish_sensor("soc", response.get("percentage_charged", 0), unit="%", friendly="Powerwall SOC")
        self.publish_sensor("battery_power", response.get("battery_power", 0), unit="W", friendly="Powerwall Battery Power")
        self.publish_sensor("grid_power", response.get("grid_power", 0), unit="W", friendly="Powerwall Grid Power")
        self.publish_sensor("load_power", response.get("load_power", 0), unit="W", friendly="Powerwall Load Power")
        self.publish_sensor("solar_power", response.get("solar_power", 0), unit="W", friendly="Powerwall Solar Power")
        self.update_success_timestamp()
        return True

    async def fetch_site_info(self):
        """Fetch site info, publishing the battery capacity as soc_max and seeding control entity states."""
        data = await self._request("GET", "/api/1/energy_sites/{}/site_info".format(self.site_id))
        if not data:
            return False
        response = data.get("response", {})
        nameplate_wh = response.get("nameplate_energy", 0)
        if nameplate_wh:
            self.publish_sensor("soc_max", round(nameplate_wh / 1000.0, 2), unit="kWh", state_class=None, friendly="Powerwall Capacity")
        # Seed the control entity STATES (display only, no commands) from the device so they reflect
        # reality at boot instead of the hardcoded defaults set by register_control_entities().
        default_mode = response.get("default_real_mode")
        if default_mode in OPERATION_MODES:
            self.set_state_wrapper(self.entity("operation_mode", domain="select"), default_mode)
        backup_reserve = response.get("backup_reserve_percent")
        if isinstance(backup_reserve, (int, float)):
            self.set_state_wrapper(self.entity("backup_reserve", domain="number"), backup_reserve)
        return True

    async def fetch_energy_today(self):
        """Fetch today's cumulative energy, publishing daily kWh sensors."""
        data = await self._request("GET", "/api/1/energy_sites/{}/calendar_history".format(self.site_id))
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
        if first:
            await self.reconcile_on_start()
        if first or (seconds - self.last_live_poll >= LIVE_POLL_SECONDS):
            self.last_live_poll = seconds
            success = await self.fetch_live_status()
        if first or (seconds - self.last_energy_poll >= ENERGY_POLL_SECONDS):
            self.last_energy_poll = seconds
            await self.fetch_energy_today()
        return success

    def register_control_entities(self):
        """Publish the virtual control entities with their initial states."""
        self.dashboard_item(self.entity("operation_mode", domain="select"), "self_consumption", {"friendly_name": "Powerwall Operation Mode", "options": OPERATION_MODES}, app="teslemetry")
        self.dashboard_item(self.entity("backup_reserve", domain="number"), 0, {"friendly_name": "Powerwall Backup Reserve", "min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"}, app="teslemetry")
        self.dashboard_item(self.entity("allow_charging_from_grid", domain="switch"), "on", {"friendly_name": "Powerwall Allow Grid Charging"}, app="teslemetry")
        self.dashboard_item(self.entity("allow_export", domain="select"), "never", {"friendly_name": "Powerwall Allow Export", "options": EXPORT_RULES}, app="teslemetry")
        self.dashboard_item(self.entity("tariff_mode", domain="select"), "normal", {"friendly_name": "Powerwall Tariff Mode", "options": TARIFF_MODES}, app="teslemetry")

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

    async def set_operation_mode(self, mode):
        """Set the Powerwall operation mode (self_consumption/autonomous/backup)."""
        return await self._command("operation", {"default_real_mode": mode})

    async def set_backup_reserve(self, percent):
        """Set the backup reserve percentage (cloud caps at 80; 80-100 treated as 100 by Predbat)."""
        return await self._command("backup", {"backup_reserve_percent": int(percent)})

    async def set_grid_charging(self, allow):
        """Allow or disallow charging the battery from the grid."""
        return await self._command("grid_import_export", {"disallow_charge_from_grid_with_solar_installed": not allow})

    async def set_export_rule(self, rule):
        """Set the grid export rule (never/pv_only/battery_ok)."""
        return await self._command("grid_import_export", {"customer_preferred_export_rule": rule})

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
        """
        if now is None:
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

    async def set_tariff(self, mode):
        """Push the tariff for the requested mode via time_of_use_settings."""
        tariff = self.build_tariff(mode)
        return await self._command("time_of_use_settings", {"tou_settings": {"tariff_content_v2": tariff}})

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
        """Fetch the site's current tariff_rate from the device and return its tariff "code", or None on failure."""
        data = await self._request("GET", "/api/1/energy_sites/{}/tariff_rate".format(self.site_id))
        if not data:
            return None
        return self._find_tariff_code(data)

    def _is_read_only(self):
        """Return True if Predbat is configured read-only, so device-write commands must not be sent."""
        base = getattr(self, "base", None)
        return bool(getattr(base, "set_read_only", False)) if base is not None else False

    async def reconcile_on_start(self):
        """Restore a safe state if a previous run died mid-export.

        Reads the Powerwall's ACTUAL current tariff from the device rather than the local
        entity mirror: register_control_entities() unconditionally reseeds the mirror to
        "normal" on every boot, so trusting it would make this recovery path unreachable.
        If the device's tariff still carries the PREDBAT-EXPORT-NOW marker written by
        build_tariff(), a previous run crashed mid-export, so normal tariff and disabled
        export are restored (unless Predbat is running read-only, in which case the need
        for recovery is logged but no write is sent). If the tariff read itself fails,
        this skips silently rather than guessing at the device state.
        """
        tariff_code = await self.get_current_tariff_code()
        if tariff_code is None:
            self.log("Warn: Teslemetry could not read the current tariff from the device on boot - skipping reconcile")
            return
        export_now_code = self.build_tariff("export_now").get("code")
        if tariff_code != export_now_code:
            return
        if self._is_read_only():
            self.log("Warn: Teslemetry device tariff was left as {} - recovery needed but skipped (read-only mode)".format(tariff_code))
            return
        self.log("Warn: Teslemetry device tariff was left as {} - restoring normal tariff and disabling export".format(tariff_code))
        if await self.set_tariff("normal"):
            self.set_state_wrapper(self.entity("tariff_mode", domain="select"), "normal")
        if await self.set_export_rule("never"):
            self.set_state_wrapper(self.entity("allow_export", domain="select"), "never")

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
            self.set_state_wrapper(entity_id, value)
        else:
            self.log("Warn: Teslemetry command failed for {} = {} (state not updated)".format(entity_id, value))

    async def number_event(self, entity_id, value):
        """Handle number changes routed from the service-hook loopback."""
        if entity_id.endswith("_backup_reserve"):
            percent = max(0, min(100, int(float(value))))
            if await self.set_backup_reserve(percent):
                self.set_state_wrapper(entity_id, percent)
            else:
                self.log("Warn: Teslemetry backup_reserve command failed (state not updated)")
        else:
            self.log("Warn: Teslemetry unhandled number_event {} = {}".format(entity_id, value))

    async def switch_event(self, entity_id, service):
        """Handle switch service calls routed from the service-hook loopback."""
        if entity_id.endswith("_allow_charging_from_grid"):
            allow = service != "turn_off"
            if await self.set_grid_charging(allow):
                self.set_state_wrapper(entity_id, "on" if allow else "off")
            else:
                self.log("Warn: Teslemetry grid charging command failed (state not updated)")
        else:
            self.log("Warn: Teslemetry unhandled switch_event {} {}".format(entity_id, service))

    async def final(self):
        """Cleanup on shutdown."""
        self.log("Info: TeslemetryAPI shutdown")
