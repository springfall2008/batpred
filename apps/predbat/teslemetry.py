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
        self.log("Info: TeslemetryAPI initialising site_id={}".format(self.site_id))

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
        """Fetch site info, publishing capacity and seeding control entity states."""
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
        """Main component loop: poll data at the configured cadences."""
        if not self.api_key or not self.site_id:
            self.log("Warn: Teslemetry key or site_id not configured, skipping run")
            return
        if first:
            await self.fetch_site_info()
        if self.api_auth_failed:
            # Do not hammer the API with a dead token; only live_status probes recovery
            if seconds - self.last_live_poll >= LIVE_POLL_SECONDS:
                self.last_live_poll = seconds
                await self.fetch_live_status()
            return
        if first or (seconds - self.last_live_poll >= LIVE_POLL_SECONDS):
            self.last_live_poll = seconds
            await self.fetch_live_status()
        if first or (seconds - self.last_energy_poll >= ENERGY_POLL_SECONDS):
            self.last_energy_poll = seconds
            await self.fetch_energy_today()

    async def final(self):
        """Cleanup on shutdown."""
        self.log("Info: TeslemetryAPI shutdown")
