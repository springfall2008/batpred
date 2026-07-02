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
        """Fetch site info, publishing the battery capacity as soc_max."""
        data = await self._request("GET", "/api/1/energy_sites/{}/site_info".format(self.site_id))
        if not data:
            return False
        response = data.get("response", {})
        nameplate_wh = response.get("nameplate_energy", 0)
        if nameplate_wh:
            self.publish_sensor("soc_max", round(nameplate_wh / 1000.0, 2), unit="kWh", state_class=None, friendly="Powerwall Capacity")
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
        """POST a command; return True on success."""
        result = await self._request("POST", "/api/1/energy_sites/{}/{}".format(self.site_id, path), json_body=body)
        return result is not None

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

    async def set_tariff(self, mode):
        """Push the tariff for the requested mode (implemented in the tariff task)."""
        self.log("Info: Teslemetry set_tariff({}) stub".format(mode))
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
