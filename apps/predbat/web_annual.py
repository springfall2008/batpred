# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

"""The Annual prediction tab.

Renders the configuration form, drives the subprocess that runs the annual
prediction engine, and presents the results. Prefills from whatever the live
Predbat instance knows and falls back to a typical UK system for the rest, so
the tab is usable by someone who has not configured Predbat at all - which is
the prospective-buyer path the tool exists to serve.
"""

import copy
import os

import yaml

from tariff_catalogue import merged_catalogue

# A plausible UK home, used for any field the live instance cannot supply. These
# are an EXAMPLE, not a recommendation - the form says so, because a visitor
# could otherwise mistake them for a reading of their own system.
DEFAULT_CONFIG = {
    "location": {"postcode": "SW1A 1AA"},
    "solar": [{"kwp": 5.0, "declination": 35, "azimuth": 180, "efficiency": 0.95}],
    "battery": {"size_kwh": 9.5, "inverter_kw": 5.0, "export_limit_kw": 5.0, "hybrid": True},
    "load": {"annual_kwh": 3800, "shape": "flat", "car_charging_kwh": 0, "car_rate_kw": 7.4},
    "tariff": {"rates_import": [{"rate": 24.86}], "rates_export": [{"rate": 4.1}], "standing_charge_p_per_day": 60.0},
    "samples_per_month": 2,
}

CONFIG_FILENAME = "annual.yaml"


class AnnualPage:
    """Renders and drives the Annual prediction tab."""

    def __init__(self, web_interface):
        """Attach to the running web interface so args and Storage are reachable."""
        self.web = web_interface
        self.base = web_interface.base
        self.log = web_interface.log

    def _arg(self, name, default=None):
        """Read one configuration value from the in-memory args dictionary.

        Never reads apps.yaml from disk: the file may not exist at all in some
        deployments, which is exactly where the unconfigured case matters most.
        """
        try:
            return self.base.get_arg(name, default)
        except Exception:
            return default

    def _solar_from_args(self):
        """Return the configured solar arrays, or an empty list.

        open_meteo_forecast and forecast_solar are already lists of
        {kwp, declination, azimuth, efficiency}, which is the annual engine's own
        shape, so no translation is needed.
        """
        for name in ["open_meteo_forecast", "forecast_solar"]:
            configured = self._arg(name, None)
            if isinstance(configured, dict):
                configured = [configured]
            if isinstance(configured, list) and configured:
                arrays = []
                for entry in configured:
                    if not isinstance(entry, dict) or not entry.get("kwp"):
                        continue
                    arrays.append(
                        {
                            "kwp": entry.get("kwp"),
                            "declination": entry.get("declination", DEFAULT_CONFIG["solar"][0]["declination"]),
                            "azimuth": entry.get("azimuth", DEFAULT_CONFIG["solar"][0]["azimuth"]),
                            "efficiency": entry.get("efficiency", DEFAULT_CONFIG["solar"][0]["efficiency"]),
                        }
                    )
                if arrays:
                    return arrays
        return []

    def _location_from_args(self):
        """Return the configured location, taken from the solar entries if present."""
        for name in ["open_meteo_forecast", "forecast_solar"]:
            configured = self._arg(name, None)
            if isinstance(configured, dict):
                configured = [configured]
            for entry in configured or []:
                if not isinstance(entry, dict):
                    continue
                if entry.get("postcode"):
                    return {"postcode": entry["postcode"]}
                if entry.get("latitude") is not None and entry.get("longitude") is not None:
                    return {"latitude": entry["latitude"], "longitude": entry["longitude"]}
        return None

    def is_configured(self):
        """Return True when the live instance has a battery or a solar array.

        Those two are what signal a configured system; with neither, the form
        shows a banner saying the values on screen are examples.
        """
        battery_kwh = self._arg("soc_max", 0.0) or 0
        try:
            battery_kwh = float(battery_kwh)
        except (TypeError, ValueError):
            battery_kwh = 0
        return battery_kwh > 0 or bool(self._solar_from_args())

    def prefill_config(self):
        """Build a complete config from the live instance, filling gaps with the example.

        Every field falls back independently, so a half-configured Predbat gets its
        real values alongside example ones rather than all-or-nothing.
        """
        config = copy.deepcopy(DEFAULT_CONFIG)

        location = self._location_from_args()
        if location:
            config["location"] = location

        arrays = self._solar_from_args()
        if arrays:
            config["solar"] = arrays

        battery_kwh = self._arg("soc_max", 0.0) or 0
        try:
            battery_kwh = float(battery_kwh)
        except (TypeError, ValueError):
            battery_kwh = 0
        # A zero or absent soc_max means it is not set - fall back so the user can adjust
        if battery_kwh > 0:
            config["battery"]["size_kwh"] = battery_kwh

        for arg_name, field, divisor in [("inverter_limit", "inverter_kw", 1000.0), ("export_limit", "export_limit_kw", 1000.0)]:
            watts = self._arg(arg_name, 0.0) or 0
            try:
                watts = float(watts)
            except (TypeError, ValueError):
                watts = 0
            if watts > 0:
                config["battery"][field] = round(watts / divisor, 2)

        inverter_type = self._arg("inverter_type", None)
        if inverter_type:
            config["battery"]["hybrid"] = True

        import_url = self._arg("rates_import_octopus_url", None)
        export_url = self._arg("rates_export_octopus_url", None)
        if import_url:
            config["tariff"] = {"import_octopus_url": import_url, "standing_charge_p_per_day": DEFAULT_CONFIG["tariff"]["standing_charge_p_per_day"]}
            if export_url:
                config["tariff"]["export_octopus_url"] = export_url

        dno_region = self._arg("dno_region", None)
        if dno_region:
            config["tariff"]["dno_region"] = dno_region

        return config

    def catalogue(self):
        """Return the tariff dropdown entries: built-ins merged with the user's own."""
        return merged_catalogue(self._arg("compare_list", None))

    def _config_path(self):
        """Return the path of the saved annual configuration."""
        return os.path.join(self.base.config_root, CONFIG_FILENAME)

    def load_config(self):
        """Return the saved configuration, or a fresh prefill when none exists."""
        path = self._config_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    saved = yaml.safe_load(handle)
                if isinstance(saved, dict) and saved:
                    return saved.get("annual", saved)
        except (OSError, yaml.YAMLError) as error:
            self.log("Warn: Annual: could not read {}: {}".format(path, error))
        return self.prefill_config()

    def save_config(self, config):
        """Write the configuration so the CLI subprocess can consume it directly."""
        path = self._config_path()
        try:
            with open(path, "w", encoding="utf-8") as handle:
                yaml.safe_dump({"annual": config}, handle, default_flow_style=False, allow_unicode=True)
        except OSError as error:
            self.log("Warn: Annual: could not write {}: {}".format(path, error))
            raise
