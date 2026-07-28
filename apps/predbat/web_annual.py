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

import calendar
import copy
import datetime
import html
import json
import os
import re
import sys

import yaml
from aiohttp import web

from annual import AnnualConfigError, validate_config
from annual_costs import DEFAULT_COSTS
from annual_job import AnnualJob
from annual_store import list_runs, load_run, save_run
from tariff_catalogue import CUSTOM_ID, merged_catalogue
from web_helper import get_plan_css, get_plan_renderer_js

# Validated with the dataviz palette checker in BOTH light and dark mode, all pairs.
# Predbat's house chart trio (#2196F3/#FF9800/#4CAF50) FAILS here: green vs orange is
# only deltaE 3.6 under protanopia, so roughly one man in twelve could not tell
# "Without Predbat" from "With Predbat" - the exact comparison this chart exists to
# make. #9439ef was chosen for pv_only by sweeping 216 candidates through the same
# validator: the obvious Okabe-Ito picks (#E69F00, #56B4E9) fall outside the dark-mode
# lightness band, and #CC79A7/#AA4499 only reach deltaE 7.6/6.4 against a neighbour.
# Do not substitute without re-running the validator in both modes.
SCENARIO_COLOURS = {"no_pvbat": "#0072B2", "pv_only": "#9439ef", "without_predbat": "#D55E00", "with_predbat": "#009E73"}
SCENARIO_LABELS = {"no_pvbat": "No PV/Battery", "pv_only": "PV only", "without_predbat": "Without Predbat", "with_predbat": "With Predbat"}
SCENARIO_ORDER = ["no_pvbat", "pv_only", "without_predbat", "with_predbat"]

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


def _json_for_script(value):
    """Serialise a value for embedding inside an inline ``<script>`` block.

    Plain ``json.dumps`` does not escape ``</`` sequences, so a value containing the
    literal text ``</script>`` (for example a run id echoed back off the query
    string, or free text that ends up inside a chart payload) would close the
    surrounding script tag early and let the remainder be parsed as HTML/JS of the
    attacker's choosing. Escaping the forward slash after ``<`` is the standard,
    JSON-semantics-preserving fix (JSON has no meaning for a preceding backslash on
    ``/``, so this round-trips through ``JSON.parse`` unchanged).
    """
    return json.dumps(value).replace("</", "<\\/")


class AnnualPage:
    """Renders and drives the Annual prediction tab."""

    def __init__(self, web_interface):
        """Attach to the running web interface so args and Storage are reachable."""
        self.web = web_interface
        self.base = web_interface.base
        self.log = web_interface.log
        self.job = AnnualJob(log=self.log)
        # Deliberately NOT storing a "last validation error" on self: this page object
        # is shared by every request from every visitor, so an attribute here would
        # leak one person's failed validation onto everyone else's later, unrelated
        # page load (see html_annual's `error` parameter instead).
        #
        # _running_config IS safe to hold as instance state, unlike the error above:
        # AnnualJob itself only ever runs one job at a time (see AnnualJob.start()), so
        # there is only ever one "currently running" config to remember, not one per
        # visitor. It is set just before job.start() succeeds and cleared once the
        # finished run has been dealt with, one way or another (see
        # _consume_terminal_state).
        self._running_config = None

    def _arg(self, name, default=None, indirect=True, combine=False):
        """Read one configuration value from the in-memory args dictionary.

        Never reads apps.yaml from disk: the file may not exist at all in some
        deployments, which is exactly where the unconfigured case matters most.

        ``indirect`` defaults to True to match ``get_arg()``'s own default, but a
        caller reading a value that can itself contain a literal dot - a URL, most
        obviously - must pass False. With indirect left True, ``resolve_arg()``
        (``userinterface.py``) treats any dotted string as a Home Assistant entity
        id, fails to find one, and silently returns the default instead of the
        real value - see the call sites below for the field this bit precisely.
        """
        try:
            return self.base.get_arg(name, default, indirect=indirect, combine=combine)
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
        # combine=True sums a multi-inverter soc_max (a sensor_list in APPS_SCHEMA) into one
        # total usable capacity, which is what the annual model wants. Without it, get_arg()'s
        # float-default coercion raises on a list, is caught internally, and silently returns
        # the default - a multi-inverter system would otherwise read as unconfigured.
        battery_kwh = self._arg("soc_max", 0.0, combine=True) or 0
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

        # combine=True sums a multi-inverter soc_max (a sensor_list in APPS_SCHEMA) into one
        # total usable capacity, which is what the annual model wants. Without it, get_arg()'s
        # float-default coercion raises on a list, is caught internally, and silently returns
        # the default - a multi-inverter system would otherwise read as unconfigured.
        battery_kwh = self._arg("soc_max", 0.0, combine=True) or 0
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

        # indirect=False: these values are URLs, full of literal dots, which get_arg()'s
        # default indirect=True would otherwise treat as a Home Assistant entity id to look
        # up - failing to find one and silently returning None instead of the real URL. See
        # compare.py's and fetch.py's own Octopus URL reads for the same requirement. Do not
        # "simplify" this back to the _arg() default.
        import_url = self._arg("rates_import_octopus_url", None, indirect=False)
        export_url = self._arg("rates_export_octopus_url", None, indirect=False)
        if import_url:
            config["tariff"] = {"import_octopus_url": import_url, "standing_charge_p_per_day": DEFAULT_CONFIG["tariff"]["standing_charge_p_per_day"]}
            if export_url:
                config["tariff"]["export_octopus_url"] = export_url

        dno_region = self._arg("dno_region", None)
        if dno_region:
            config["tariff"]["dno_region"] = dno_region

        # indirect=False for the same reason as the URLs above: an Octopus API key is a
        # dotted string ("sk_live_..." keys are not, but account ids and keys are free
        # text and must not be resolved as entity ids), so leaving indirect at its True
        # default risks silently returning None instead of the configured value.
        api_key = self._arg("octopus_api_key", None, indirect=False)
        account_id = self._arg("octopus_api_account", None, indirect=False)
        if api_key and account_id:
            # Both are needed to download consumption - a key without an account (or the
            # reverse) cannot fetch anything, so only a complete pair is worth offering.
            # Selecting the Octopus source as well as filling the fields is the point:
            # this user has real metered consumption available, which models their year
            # far better than the synthetic annual-kWh profile the default falls back to.
            #
            # Replaces the load block rather than adding to it: _validate_load treats
            # octopus and annual_kwh/car_charging_kwh as mutually exclusive (the metered
            # series already includes car charging, so carrying both would double-count
            # it) and rejects a config holding both. render_form falls back to
            # DEFAULT_CONFIG for the manual inputs, so they still show sensible values if
            # the user switches the radio back to "Enter my usage".
            config["load"] = {"octopus": {"api_key": str(api_key), "account_id": str(account_id)}, "shape": config["load"].get("shape", "flat")}

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

    def _number_field(self, name, label, value, step="any", suffix=""):
        """Return one labelled numeric input row.

        ``value`` is HTML-escaped before interpolation: although the caller usually
        passes a plain number, several call sites pass a config value that ultimately
        traces back to a posted form field (e.g. a re-render after a validation
        error), so it must be treated as untrusted.
        """
        return '<div class="annual-field"><label for="{name}">{label}</label><input type="number" step="{step}" id="{name}" name="{name}" value="{value}">{suffix}</div>\n'.format(
            name=name, label=label, step=step, value=html.escape(str(value), quote=True) if value is not None else "", suffix=" {}".format(suffix) if suffix else ""
        )

    def _text_field(self, name, label, value, wide=False):
        """Return one labelled text input row.

        ``value`` is HTML-escaped before interpolation - it is user-controlled
        (a postcode, an Octopus API key or account id, a tariff URL, a DNO region).

        ``wide`` puts the input on its own full-width line beneath the label, for the
        long values - rates URLs and API keys - that are otherwise shown through a
        keyhole and cannot be read or checked without scrolling within the box.
        """
        return '<div class="annual-field{wide}"><label for="{name}">{label}</label><input type="text" id="{name}" name="{name}" value="{value}"></div>\n'.format(
            name=name, label=label, wide=" annual-field-wide" if wide else "", value=html.escape(str(value), quote=True) if value is not None else ""
        )

    def render_form(self, config, errors=None):
        """Return the configuration form as HTML, populated from ``config``.

        ``errors`` is displayed above the form with every field left as the user
        entered it - losing their input on a validation failure would be worse
        than the failure.
        """
        solar = config.get("solar") or [{}]
        battery = config.get("battery") or {}
        load = config.get("load") or {}
        tariff = config.get("tariff") or {}
        location = config.get("location") or {}

        text = '<div class="annual-form-wrap">\n'

        if errors:
            text += '<div class="annual-error"><strong>Could not run:</strong> {}</div>\n'.format(html.escape(str(errors), quote=True))

        if not self.is_configured():
            text += '<div class="annual-banner">Predbat isn\'t configured yet — these are <strong>example values</strong>, edit them to match your home.</div>\n'

        text += '<form action="./annual_run" method="post" id="annualform">\n'

        text += "<fieldset><legend>Location</legend>\n"
        text += self._text_field("postcode", "Postcode", location.get("postcode", ""))
        text += self._number_field("latitude", "Latitude (instead of postcode)", location.get("latitude"))
        text += self._number_field("longitude", "Longitude", location.get("longitude"))
        text += "</fieldset>\n"

        text += "<fieldset><legend>Solar</legend>\n"
        for index, array in enumerate(solar):
            mode = "panels" if array.get("panels") else "kwp"
            text += '<div class="annual-array"><strong>Array {}</strong>\n'.format(index + 1)
            text += '<div class="annual-field"><label for="solar_mode_{i}">Describe this array by</label><select id="solar_mode_{i}" name="solar_mode_{i}" onchange="annualSolarModeChanged({i})">\n'.format(i=index)
            for mode_value, caption in [("kwp", "Peak power (kWp)"), ("panels", "Number of panels")]:
                text += '<option value="{}" {}>{}</option>\n'.format(mode_value, "selected" if mode == mode_value else "", caption)
            text += "</select></div>\n"
            text += '<div id="solar-kwp-row-{i}" style="display:{d}">\n'.format(i=index, d="none" if mode == "panels" else "block")
            text += self._number_field("solar_kwp_{}".format(index), "Peak power", array.get("kwp"), suffix="kWp")
            text += "</div>\n"
            text += '<div id="solar-panels-row-{i}" style="display:{d}">\n'.format(i=index, d="block" if mode == "panels" else "none")
            text += self._number_field("solar_panels_{}".format(index), "Number of panels", array.get("panels", 0), step="1")
            text += self._number_field("solar_panel_watts_{}".format(index), "Watts per panel", array.get("panel_watts", 400), suffix="W")
            text += "</div>\n"
            text += self._number_field("solar_declination_{}".format(index), "Pitch", array.get("declination", 35), suffix="degrees")
            text += self._number_field("solar_azimuth_{}".format(index), "Azimuth (180 = south)", array.get("azimuth", 180), suffix="degrees")
            text += "</div>\n"
        text += '<p class="annual-note" id="annual-solar-total"></p>\n'
        text += "</fieldset>\n"

        text += "<fieldset><legend>Battery</legend>\n"
        text += self._number_field("battery_size_kwh", "Usable capacity", battery.get("size_kwh"), suffix="kWh")
        text += self._number_field("battery_inverter_kw", "Inverter size", battery.get("inverter_kw"), suffix="kW")
        text += self._number_field("battery_export_limit_kw", "Export limit", battery.get("export_limit_kw"), suffix="kW")
        text += '<div class="annual-field"><label for="battery_hybrid">Hybrid inverter</label><input type="checkbox" id="battery_hybrid" name="battery_hybrid" {}></div>\n'.format("checked" if battery.get("hybrid", True) else "")
        text += "</fieldset>\n"

        using_octopus = "octopus" in load
        text += "<fieldset><legend>Load</legend>\n"
        text += '<div class="annual-field"><label><input type="radio" name="load_source" value="manual" {}> Enter my usage</label></div>\n'.format("" if using_octopus else "checked")
        text += '<div class="annual-subgroup" id="load-manual">\n'
        text += self._number_field("load_annual_kwh", "Annual consumption", load.get("annual_kwh", DEFAULT_CONFIG["load"]["annual_kwh"]), suffix="kWh")
        shape = load.get("shape", "flat")
        text += '<div class="annual-field"><label for="load_shape">Usage pattern</label><select id="load_shape" name="load_shape">\n'
        for value, caption in [("flat", "About the same through the day"), ("night", "More at night"), ("day", "More during the day")]:
            text += '<option value="{}" {}>{}</option>\n'.format(value, "selected" if shape == value else "", caption)
        text += "</select></div>\n"
        text += self._number_field("load_car_charging_kwh", "Car charging per year (0 for none)", load.get("car_charging_kwh", 0), suffix="kWh")
        text += self._number_field("load_car_rate_kw", "Charger power", load.get("car_rate_kw", 7.4), suffix="kW")
        text += "</div>\n"
        text += '<div class="annual-field"><label><input type="radio" name="load_source" value="octopus" {}> Import from Octopus</label></div>\n'.format("checked" if using_octopus else "")
        text += '<div class="annual-subgroup" id="load-octopus">\n'
        text += self._text_field("load_octopus_api_key", "Octopus API key", (load.get("octopus") or {}).get("api_key", ""), wide=True)
        text += self._text_field("load_octopus_account_id", "Account ID", (load.get("octopus") or {}).get("account_id", ""), wide=True)
        text += '<p class="annual-note">Your meter readings already include any car charging, so the figures above are not used with this option.</p>\n'
        text += "</div>\n"
        text += "</fieldset>\n"

        text += "<fieldset><legend>Tariff</legend>\n"
        text += '<div class="annual-field"><label for="tariff_id">Tariff</label><select id="tariff_id" name="tariff_id" onchange="annualTariffChanged()">\n'
        catalogue = self.catalogue()
        current_import_url = tariff.get("import_octopus_url")
        # A hand-entered URL (no matching catalogue entry) is what Custom means, so it
        # is the fallback selection rather than leaving the dropdown on its first entry.
        selected_id = None
        if current_import_url:
            selected_id = CUSTOM_ID
            for entry in catalogue:
                if entry.get("import_octopus_url") == current_import_url:
                    selected_id = entry["id"]
                    break
        for entry in catalogue:
            text += '<option value="{}" data-import="{}" data-export="{}" {}>{}</option>\n'.format(
                html.escape(entry["id"], quote=True),
                html.escape(entry.get("import_octopus_url", ""), quote=True),
                html.escape(entry.get("export_octopus_url", ""), quote=True),
                "selected" if entry["id"] == selected_id else "",
                html.escape(entry["name"], quote=True),
            )
        text += "</select></div>\n"
        text += self._text_field("tariff_import_url", "Import rates URL", tariff.get("import_octopus_url", ""), wide=True)
        text += self._text_field("tariff_export_url", "Export rates URL", tariff.get("export_octopus_url", ""), wide=True)
        text += self._text_field("tariff_dno_region", "Octopus region letter", tariff.get("dno_region", ""))
        text += self._number_field("tariff_standing_charge", "Standing charge", tariff.get("standing_charge_p_per_day", 60.0), suffix="p/day")
        text += "</fieldset>\n"

        text += "<details><summary>Advanced</summary>\n"
        text += self._number_field("year", "Year to model (blank for the most recent complete year)", config.get("year"))
        text += self._number_field("samples_per_month", "Days sampled per month", config.get("samples_per_month", 2), step="1")
        text += '<div class="annual-field"><label for="debug">Save plans for debugging</label><input type="checkbox" id="debug" name="debug" {}></div>\n'.format("checked" if config.get("debug") else "")
        text += '<p class="annual-note">Keeps each sampled day\'s plan so you can inspect it below. Makes the saved run much larger.</p>\n'
        text += self._number_field("pv10_derate_fallback", "P10 fallback derate", config.get("pv10_derate_fallback", 0.7))
        for index, array in enumerate(solar):
            text += self._number_field("solar_efficiency_{}".format(index), "Array {} efficiency".format(index + 1), array.get("efficiency", 0.95))
        costs_config = config.get("costs") or {}
        for key, label in [
            ("battery_install_gbp", "Battery install cost"),
            ("battery_per_kwh_gbp", "Battery cost per kWh"),
            ("pv_minimum_gbp", "Minimum PV install cost"),
            ("pv_rate_small_gbp_per_kwp", "PV £/kWp for a small system (about 2 kWp)"),
            ("pv_rate_medium_gbp_per_kwp", "PV £/kWp for a medium system (about 7 kWp)"),
            ("pv_rate_large_gbp_per_kwp", "PV £/kWp for a large system (about 30 kWp)"),
            ("predbat_annual_gbp", "Predbat cost per year"),
        ]:
            text += self._number_field("cost_{}".format(key), label, costs_config.get(key, DEFAULT_COSTS[key]), suffix="£")
        text += "</details>\n"

        # Marks THIS tab as the one that started the run, so only it navigates to the
        # results when the run finishes (see annualPoll). The poll runs in every open
        # tab, and a tab sitting on a half-filled form must not be reloaded out from
        # under whoever is typing in it.
        text += '<button type="submit" id="annual-run-button" onclick="annualMarkStarted()">Run simulations</button>\n'
        # A second submit button pointed at the plain POST /annual handler via
        # formaction - the same fields, saved without starting a run.
        text += '<button type="submit" formaction="./annual" formmethod="post">Save settings</button>\n'
        text += "</form>\n</div>\n"
        return text

    def config_from_post(self, postdata):
        """Rebuild a config dict from submitted form fields.

        Numeric-context fields are coerced to int/float here; a value that fails to
        parse is left as the original string so validate_config() - the sole authority
        on what is IN RANGE - still sees exactly what the user typed and can raise its
        usual, actionable "must be a number, got '...'" error. This coercion never
        applies a minimum, a maximum, or any other opinion about validity: it exists
        only so the web side's own uses of the config before validate_config runs (the
        run label, the on-disk YAML, a re-render after a failure) are numerically
        sound, not so that config_from_post becomes a second, competing validator.
        """

        def value(name, default=None):
            """Return one posted field, or the default when absent or blank."""
            raw = postdata.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            return str(raw).strip()

        def numeric(name, default=None):
            """Return one posted field coerced to int/float, or the default when absent.

            A value that does not parse as a number is returned unchanged (as the
            posted string) rather than raising here - see the docstring above.
            """
            raw = value(name)
            if raw is None:
                return default
            if re.fullmatch(r"[+-]?\d+", raw):
                return int(raw)
            try:
                return float(raw)
            except ValueError:
                return raw

        config = {}

        location = {}
        if value("postcode"):
            location["postcode"] = value("postcode")
        if value("latitude") is not None and value("longitude") is not None:
            location["latitude"] = numeric("latitude")
            location["longitude"] = numeric("longitude")
        config["location"] = location

        arrays = []
        index = 0
        while value("solar_kwp_{}".format(index)) is not None or value("solar_panels_{}".format(index)) is not None:
            array = {
                "declination": numeric("solar_declination_{}".format(index), 35),
                "azimuth": numeric("solar_azimuth_{}".format(index), 180),
                "efficiency": numeric("solar_efficiency_{}".format(index), 0.95),
            }
            # Only one of kwp/panels is ever sent - validate_config rejects an array
            # carrying both, so the mode radio decides which single field goes out.
            if value("solar_mode_{}".format(index)) == "panels":
                array["panels"] = numeric("solar_panels_{}".format(index), 0)
                array["panel_watts"] = numeric("solar_panel_watts_{}".format(index), 400)
            else:
                array["kwp"] = numeric("solar_kwp_{}".format(index))
            arrays.append(array)
            index += 1
        if arrays:
            config["solar"] = arrays

        if value("battery_size_kwh") is not None:
            config["battery"] = {
                "size_kwh": numeric("battery_size_kwh"),
                "inverter_kw": numeric("battery_inverter_kw", 5.0),
                "export_limit_kw": numeric("battery_export_limit_kw", 5.0),
                "hybrid": bool(postdata.get("battery_hybrid")),
            }

        # The engine rejects an Octopus block alongside manual figures, because the
        # meter series already contains any car charging. Send one or the other.
        if value("load_source", "manual") == "octopus":
            config["load"] = {"octopus": {"api_key": value("load_octopus_api_key", ""), "account_id": value("load_octopus_account_id", "")}}
        else:
            config["load"] = {
                "annual_kwh": numeric("load_annual_kwh", 3800),
                "shape": value("load_shape", "flat"),
                "car_charging_kwh": numeric("load_car_charging_kwh", 0),
                "car_rate_kw": numeric("load_car_rate_kw", 7.4),
            }

        tariff = {"standing_charge_p_per_day": numeric("tariff_standing_charge", 0)}
        if value("tariff_import_url"):
            tariff["import_octopus_url"] = value("tariff_import_url")
        if value("tariff_export_url"):
            tariff["export_octopus_url"] = value("tariff_export_url")
        if value("tariff_dno_region"):
            tariff["dno_region"] = value("tariff_dno_region")
        if not tariff.get("import_octopus_url"):
            tariff["rates_import"] = DEFAULT_CONFIG["tariff"]["rates_import"]
            tariff["rates_export"] = DEFAULT_CONFIG["tariff"]["rates_export"]
        config["tariff"] = tariff

        if value("year"):
            config["year"] = numeric("year")
        config["samples_per_month"] = numeric("samples_per_month", 2)
        if value("pv10_derate_fallback"):
            config["pv10_derate_fallback"] = numeric("pv10_derate_fallback")
        # A checkbox absent from postdata means unchecked - matches how battery_hybrid
        # is read above; there is no "off" value to see, only "on" or nothing at all.
        config["debug"] = postdata.get("debug") is not None

        costs = {}
        for key in DEFAULT_COSTS:
            submitted = numeric("cost_{}".format(key), None)
            if submitted is not None:
                costs[key] = submitted
        if costs:
            config["costs"] = costs

        return config

    def cli_command(self, config_path):
        """Return the argv for the child process that performs the run."""
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "annual_cli.py")
        work_dir = os.path.join(self.base.config_root, "annual_work")
        return [sys.executable, script, "--config", config_path, "--work-dir", work_dir, "--machine"]

    def validation_error(self, config):
        """Return a message when the config is invalid, or None when it is fine.

        Validation runs here for immediate feedback, but the same validate_config
        runs again inside the child and remains the authority.
        """
        try:
            validate_config(config)
        except AnnualConfigError as error:
            return str(error)
        return None

    def _storage(self):
        """Return the Storage component, or None when it is unavailable."""
        components = getattr(self.base, "components", None)
        return components.get_component("storage") if components else None

    async def _consume_terminal_state(self):
        """Return a just-finished job's status once, atomically returning the job to idle.

        ``self.job.status()`` and the write back to ``self.job.state = "idle"`` happen
        with no ``await`` between them, so this coroutine either claims a terminal
        state entirely or finds none - never a partial claim that a concurrent poll
        could also observe. That is what stops two overlapping ``/annual_status``
        polls from both saving the same finished run (they would otherwise both see
        "complete" and both call save_run with different second-resolution ids), and
        what stops a "complete"/"failed"/"cancelled" state from being reported to
        every poll forever: a fresh page load - this tab after a refresh, or any
        other open tab - sees "idle" the moment someone has already claimed it.

        Returns None when the job is not currently in a terminal state, so callers
        fall back to a plain, side-effect-free ``self.job.status()``.

        A storage failure while saving the just-finished run is caught rather than
        left to propagate: letting it escape would 500 this poll, which the page's
        JS turns into a silent retry-as-idle (see annualPoll's ``.catch``) with the
        run's results gone for good - the user would see nothing at all. Instead the
        ``status`` already claimed as "complete" above is downgraded to "failed" with
        an explanatory message, so THIS poll - the one the page's JS is actually
        looking at, since polling stops the moment any terminal state is reported -
        carries the failure visibly.
        """
        status = self.job.status()
        if status["state"] not in ("complete", "failed", "cancelled"):
            return None
        results = self.job.results
        self.job.results = None
        self.job.state = "idle"
        self.job.error = None
        if status["state"] == "complete" and results is not None:
            config = self._running_config or self.load_config()
            try:
                await self._store_completed_run(config, results)
            except Exception as exception:  # noqa: BLE001 - a storage failure must be visible, never silent - see the docstring above
                message = "The run finished but its results could not be saved: {}".format(exception)
                self.log("Warn: Annual: {}".format(message))
                self.job.state = "failed"
                self.job.error = message
                status = dict(status, state="failed", error=message)
            self._running_config = None
        return status

    async def _store_completed_run(self, config, results):
        """Save a finished run's results into the ring.

        ``results`` arrives already claimed by the caller (see
        _consume_terminal_state): by the time this coroutine's first ``await`` runs,
        nothing else can still be holding the same results to save a second time.
        """
        run_id = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        await save_run(self._storage(), results, config, run_id)

    async def status_payload(self):
        """Return the polling payload: job state plus the stored run list.

        The state reported here is either live progress or a one-shot "just
        finished" snapshot claimed by _consume_terminal_state - never a stale
        terminal state repeated on every subsequent poll.
        """
        status = await self._consume_terminal_state() or self.job.status()
        status["runs"] = await list_runs(self._storage())
        return status

    async def html_annual(self, request, error=None, config=None):
        """Render the Annual tab: the form, then the selected run's results.

        ``error`` is a validation message threaded through from a POST handler in
        the same request-response cycle, not read off ``self`` - the page object is
        shared by every visitor, so storing it as instance state would leak one
        person's failed validation onto everyone else's later, unrelated page load.

        ``config`` is likewise threaded through from a POST handler rather than
        always re-read from disk: on a validation failure (or a refused second run)
        nothing was ever saved, so re-reading ``self.load_config()`` here would
        silently discard whatever the visitor had actually typed and re-render the
        previous, unrelated saved configuration instead - the opposite of the "form
        still populated with what was entered" behaviour ``render_form`` promises.
        """
        self.web.default_page = "./annual"
        if config is None:
            config = self.load_config()

        text = self.web.get_header("Predbat Annual")
        text += "<body>\n"
        text += self.render_css()
        text += self.render_form(config, errors=error)
        text += self.render_progress()

        storage = self._storage()
        runs = await list_runs(storage)
        selected = request.query.get("run") or (runs[0]["id"] if runs else None)
        results = await load_run(storage, selected) if selected else None
        text += self.render_results(results, runs, selected)
        text += self.render_script()
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    async def html_annual_post(self, request):
        """Save the configuration without running."""
        postdata = await request.post()
        config = self.config_from_post(postdata)
        error = self.validation_error(config)
        if not error:
            self.save_config(config)
        return await self.html_annual(request, error=error, config=config)

    async def html_annual_run(self, request):
        """Validate, save, and spawn the run."""
        postdata = await request.post()
        config = self.config_from_post(postdata)
        error = self.validation_error(config)
        if error:
            return await self.html_annual(request, error=error, config=config)

        # Checked BEFORE anything is saved or _running_config is touched: doing this
        # after job.start()'s own refusal (it also checks) would have already
        # overwritten the in-flight run's config and state with this second run's,
        # so the run that was actually working would finish labelled with the
        # wrong battery size, array size and tariff - and nothing on screen would
        # say so, since job.start()'s refusal on its own leaves `error` at None.
        if self.job.state == "running":
            error = "A run is already in progress. Wait for it to finish, or cancel it, before starting another."
            return await self.html_annual(request, error=error, config=config)

        self.save_config(config)
        self._running_config = config
        started = await self.job.start(self.cli_command(self._config_path()))
        if not started and self.job.state != "running":
            error = self.job.status().get("error") or "The run could not be started"
        return await self.html_annual(request, error=error, config=config)

    async def html_annual_status(self, request):
        """Return the job status as JSON for the page to poll."""
        return web.json_response(await self.status_payload())

    async def html_annual_cancel(self, request):
        """Cancel a running job."""
        await self.job.cancel()
        return web.json_response(self.job.status())

    async def html_annual_download(self, request):
        """Return one stored run's raw results document as a JSON download."""
        run_id = request.query.get("run")
        results = await load_run(self._storage(), run_id)
        if results is None:
            return web.json_response({"error": "No stored run with id {}".format(run_id)}, status=404)
        return web.json_response(results, headers={"Content-Disposition": 'attachment; filename="annual-{}.json"'.format(run_id)})

    async def html_annual_plan(self, request):
        """Return one captured plan as JSON, for the results page's plan viewer to render.

        Only present at all under a debug run (see ``"plans"`` in Task 1's month-row
        addition); a non-debug run's stored results carry no ``"plans"`` key on any
        month, so ``_find_plan`` falls through to None and this 404s, same as any
        other query it cannot resolve.
        """
        run_id = request.query.get("run", "")
        results = await load_run(self._storage(), run_id) if run_id else None
        if not results:
            return web.json_response({"error": "run not found"}, status=404)
        plan = self._find_plan(results, request.query.get("month"), request.query.get("index"), request.query.get("scenario"))
        if plan is None:
            return web.json_response({"error": "plan not found"}, status=404)
        return web.json_response(plan)

    @staticmethod
    def _find_plan(results, month, index, scenario):
        """Return one captured plan's raw_plan dict, or None when it cannot be resolved.

        ``month``, ``index`` and ``scenario`` arrive straight off the query string,
        which is attacker-controlled - this must coerce defensively and never raise
        out of the handler above; any failure to resolve simply becomes a None the
        handler turns into a 404, not a 500.
        """
        if not isinstance(results, dict) or not scenario:
            return None
        try:
            month = int(month)
            index = int(index)
        except (TypeError, ValueError):
            return None
        for entry in results.get("months", []) or []:
            if not isinstance(entry, dict) or entry.get("month") != month:
                continue
            plans = entry.get("plans")
            # A corrupt stored run could have "plans" as anything at all (a dict, a
            # string, ...) - indexing a non-list with an int would raise KeyError or
            # TypeError instead of the None this method's docstring promises never to
            # let escape, so a non-list is treated the same as no plans at all.
            if not isinstance(plans, list) or index < 0 or index >= len(plans):
                return None
            plan_entry = plans[index]
            if not isinstance(plan_entry, dict):
                return None
            scenarios = plan_entry.get("scenarios")
            if not isinstance(scenarios, dict):
                return None
            return scenarios.get(scenario)
        return None

    @staticmethod
    def _pounds(pence):
        """Return a pence value formatted as pounds with an explicit unit."""
        try:
            return "£{:.2f}".format(float(pence) / 100.0)
        except (TypeError, ValueError):
            return "n/a"

    def render_results(self, results, runs, selected_id):
        """Return the results view: selector, totals, chart, monthly table, caveats."""
        text = '<div class="annual-results">\n'
        text += self._render_selector(runs, selected_id)

        if not results:
            text += "<p>No results yet — fill in the form above and press Run simulations.</p>\n</div>\n"
            return text

        if not isinstance(results, dict):
            text += "<p>This run's results file is missing or could not be read.</p>\n</div>\n"
            return text

        annual = results.get("annual", {}) or {}
        scenarios = annual.get("scenarios")
        included = annual.get("months_included", 0)

        text += "<h2>Annual totals for {}</h2>\n".format(html.escape(str(results.get("year", "")), quote=True))
        if not scenarios:
            text += "<p>No month produced a usable result, so there is no annual figure.</p>\n"
        else:
            text += "<table class='comparison-table'><tr><th>Scenario</th><th>Cost</th><th>Import</th><th>Export</th></tr>\n"
            for key in SCENARIO_ORDER:
                entry = scenarios.get(key, {})
                text += "<tr><td>{}</td><td>{}</td><td>{} kWh</td><td>{} kWh</td></tr>\n".format(SCENARIO_LABELS[key], self._pounds(entry.get("cost_p")), round(entry.get("import_kwh", 0), 1), round(entry.get("export_kwh", 0), 1))
            text += "</table>\n"
            savings = annual.get("savings", {}) or {}
            text += "<p><strong>PV and battery save {}</strong> against no system.</p>\n".format(self._pounds(savings.get("pv_battery_vs_none_p", 0)))
            text += "<p><strong>Predbat saves a further {}</strong> against a timer-controlled battery.</p>\n".format(self._pounds(savings.get("predbat_vs_baseline_p", 0)))
            text += "<p>Standing charge (identical in every scenario): {}</p>\n".format(self._pounds(annual.get("standing_charge_p", 0)))

        text += "<p>Based on {} of 12 months.".format(included)
        excluded = annual.get("months_excluded") or []
        if excluded:
            text += " Excluded: {}.".format(", ".join(calendar.month_abbr[month] for month in excluded))
        text += "</p>\n"

        text += self._render_payback(results)
        text += self._render_chart(results)
        text += self._render_month_table(results)
        text += self._render_plan_viewer(results, selected_id)
        text += self._render_caveats(results)
        if selected_id:
            text += '<p><a href="./annual_download?run={}">Download this run as JSON</a></p>\n'.format(html.escape(str(selected_id), quote=True))
        text += "</div>\n"
        return text

    def _render_selector(self, runs, selected_id):
        """Return the run selector, or nothing when there are no stored runs."""
        if not runs:
            return ""
        text = '<form action="./annual" method="get" class="annual-selector"><label for="run">Run</label><select id="run" name="run" onchange="this.form.submit()">\n'
        for run in runs:
            run_id = html.escape(str(run["id"]), quote=True)
            label = html.escape(str(run.get("label", run["id"])), quote=True)
            text += '<option value="{}" {}>{} — {}</option>\n'.format(run_id, "selected" if run["id"] == selected_id else "", label, run_id)
        text += "</select></form>\n"
        return text

    def _render_payback(self, results):
        """Return the install cost and payback table, or the reason there is none."""
        annual = results.get("annual") or {}
        costs = annual.get("costs") or {}
        payback = annual.get("payback") or {}
        if not costs:
            return ""

        text = "<h2>Cost and payback</h2>\n"
        text += "<p class='annual-note'>Estimated install cost for a {} kWp array and a {} kWh battery: <strong>£{:,.0f}</strong> (PV £{:,.0f} at £{:,.0f}/kWp, battery £{:,.0f}).</p>\n".format(
            costs.get("total_kwp", 0), costs.get("battery_kwh", 0), costs.get("total_gbp", 0), costs.get("pv_gbp", 0), costs.get("pv_rate_gbp_per_kwp", 0), costs.get("battery_gbp", 0)
        )

        if not payback.get("available"):
            reason = html.escape(str(payback.get("reason", "Payback could not be calculated.")), quote=True)
            text += "<p class='annual-unavailable'>{}</p>\n".format(reason)
            return text

        has_pv = costs.get("total_kwp", 0) > 0
        has_battery = costs.get("battery_kwh", 0) > 0
        # A row prices hardware the system does not have: with no battery configured,
        # "PV + battery"/"With Predbat" would show the PV-only capital against a
        # battery-and-PV saving, reading as though the battery cost nothing (finding
        # #1); with no PV, "PV only" would show a solar system that was never modelled
        # (finding #7). Suppress rather than render a plausible-looking wrong number.
        rows = []
        if has_pv:
            rows.append(("pv_only", "PV only"))
        if has_battery:
            rows.append(("pv_battery", "PV + battery"))
            rows.append(("pv_battery_predbat", "With Predbat"))

        if not rows:
            text += "<p class='annual-unavailable'>No PV or battery is configured, so there is nothing to price a payback for.</p>\n"
            return text

        text += "<table class='comparison-table'>\n<tr><th>Option</th><th>Capital</th><th>Saving a year</th><th>Pays back in</th></tr>\n"
        for key, label in rows:
            row = payback.get(key) or {}
            if row.get("pays_back") and row.get("years") is not None:
                years = "{:.1f} years".format(row["years"])
            else:
                years = "<span class='annual-unavailable'>does not pay back</span>"
            saving = "£{:,.0f}".format(row.get("annual_saving_gbp", 0))
            if row.get("predbat_annual_gbp"):
                saving += " <span class='annual-note'>(after £{:,.0f}/year for Predbat)</span>".format(row["predbat_annual_gbp"])
            text += "<tr><td>{}</td><td>£{:,.0f}</td><td>{}</td><td>{}</td></tr>\n".format(label, row.get("capital_gbp", 0), saving, years)
        text += "</table>\n"
        text += "<p class='annual-note'>Simple payback: capital divided by the modelled annual saving. It ignores panel degradation, price inflation, battery replacement and finance costs.</p>\n"
        return text

    def _render_chart(self, results):
        """Return the grouped monthly bar chart.

        Only months with a usable result contribute a bar. An unavailable month is
        left out of the series entirely rather than plotted as zero - a zero-height
        bar reads as free electricity, which is the opposite of what happened.

        The same rule applies to a scenario missing from a month's own ``scenarios``
        dict (a run stored before this scenario existed, e.g. a pre-``pv_only`` legacy
        document): that point is plotted as a gap (``None``, which ApexCharts skips),
        never as a fabricated zero, and a scenario absent from every included month is
        dropped from the payload entirely rather than shipped as an all-gap series.
        """
        categories = []
        series = {key: [] for key in SCENARIO_ORDER}
        for entry in results.get("months", []):
            if entry.get("status") not in ("ok", "degraded"):
                continue
            categories.append(calendar.month_abbr[entry["month"]])
            for key in SCENARIO_ORDER:
                cost_p = entry.get("scenarios", {}).get(key, {}).get("cost_p")
                series[key].append(round(cost_p / 100.0, 2) if isinstance(cost_p, (int, float)) else None)

        if not categories:
            return "<p>No month produced a usable result, so there is nothing to chart.</p>\n"

        chart_keys = [key for key in SCENARIO_ORDER if any(value is not None for value in series[key])]
        payload = {
            "chart": {"type": "bar", "height": 400, "toolbar": {"show": False}},
            "series": [{"name": SCENARIO_LABELS[key], "data": series[key]} for key in chart_keys],
            "colors": [SCENARIO_COLOURS[key] for key in chart_keys],
            "xaxis": {"categories": categories},
            "yaxis": {"title": {"text": "Cost (£)"}},
            "plotOptions": {"bar": {"columnWidth": "70%", "borderRadius": 4, "borderRadiusApplication": "end"}},
            "stroke": {"show": True, "width": 2, "colors": ["transparent"]},
            "dataLabels": {"enabled": False},
            "legend": {"position": "top"},
            "tooltip": {"y": {"formatter": None}},
        }
        # ApexCharts itself is already loaded by get_header_html() (web_helper.py); a
        # second <script src> tag here would just be a redundant, wasted fetch.
        text = '<div id="annual-chart"></div>\n'
        synthesised_months = [calendar.month_abbr[entry["month"]] for entry in results.get("months", []) if entry.get("rates_synthesised")]
        if synthesised_months:
            # The bar itself carries no visual marker - it is real Predbat plan output,
            # just costed against a fallback rate pattern - so a reader would otherwise
            # have no way to tell these months apart from any other bar in the chart.
            text += "<p class='annual-note'>Rates for {} were synthesised from today's rates because no historical rates were available (see caveats below) - treat those bars as indicative, not historical.</p>\n".format(", ".join(synthesised_months))
        text += "<script>\nvar annualOptions = {};\n".format(_json_for_script(payload))
        text += "annualOptions.tooltip.y = {formatter: function (v) { return '£' + v.toFixed(2); }};\n"
        text += "new ApexCharts(document.querySelector('#annual-chart'), annualOptions).render();\n</script>\n"
        return text

    def _render_month_table(self, results):
        """Return the per-month energy breakdown, marking degraded, unavailable and rate-synthesised months.

        A scenario missing from a month's own ``scenarios`` dict (e.g. ``pv_only`` on a
        run stored before that scenario existed) gets no row at all here - not a row of
        fabricated ``0 kWh`` cells, which would read as "this scenario really used zero
        energy" rather than "this scenario was never modelled for this month".
        """
        text = "<h2>By month</h2>\n<table class='comparison-table'>\n"
        text += "<tr><th>Month</th><th>Scenario</th><th>Cost</th><th>Import</th><th>Export</th><th>PV</th><th>Battery</th></tr>\n"
        for entry in results.get("months", []):
            name = calendar.month_abbr[entry["month"]]
            if entry.get("status") not in ("ok", "degraded"):
                reason = html.escape(str(entry.get("reason", "no result")), quote=True)
                text += "<tr class='annual-unavailable'><td>{}</td><td colspan='6'>unavailable — {}</td></tr>\n".format(name, reason)
                continue
            suffix = " (degraded — {} sampled day(s) failed)".format(len(entry.get("failed_days", []))) if entry.get("status") == "degraded" else ""
            synthesised = entry.get("rates_synthesised") or []
            if synthesised:
                # A synthesised month is still "ok"/"degraded" - it produced a real,
                # usable plan - so it is not caught by the unavailable branch above.
                # Without this, it renders identically to a month priced from real
                # historical rates, which is exactly the ambiguity being fixed here.
                suffix += " <span class='annual-synthesised-tag' title='Rates for this month came from today&#39;s rates, not what was actually charged then'>rates synthesised: {}</span>".format(html.escape(", ".join(synthesised), quote=True))
            month_scenarios = entry.get("scenarios", {}) or {}
            first_row = True
            for key in SCENARIO_ORDER:
                if key not in month_scenarios:
                    continue
                scenario = month_scenarios[key]
                text += "<tr><td>{}{}</td><td>{}</td><td>{}</td><td>{} kWh</td><td>{} kWh</td><td>{} kWh</td><td>{} kWh</td></tr>\n".format(
                    name if first_row else "",
                    suffix if first_row else "",
                    SCENARIO_LABELS[key],
                    self._pounds(scenario.get("cost_p")),
                    round(scenario.get("import_kwh", 0), 1),
                    round(scenario.get("export_kwh", 0), 1),
                    round(scenario.get("pv_generated_kwh", 0), 1),
                    round(scenario.get("battery_throughput_kwh", 0), 1),
                )
                first_row = False
        text += "</table>\n"
        return text

    def _render_plan_viewer(self, results, selected_id):
        """Return the captured-plan viewer, or nothing when this run captured no plans.

        A debug run's month rows carry a non-empty ``"plans"`` list (see Task 1); a
        non-debug run has no ``"plans"`` key on any month at all, so ``options``
        stays empty and this returns "" - no empty viewer shell must appear for an
        ordinary run.

        The day options are built in the results document's own order - months
        1..12, each month's plans in the order the engine sampled them - which is
        already chronological, so no separate sort is needed.
        """
        options = []
        for entry in results.get("months", []) or []:
            plans = entry.get("plans") or []
            for index, plan in enumerate(plans):
                day = plan.get("day", "")
                leg = plan.get("leg", "single")
                label = day if leg == "single" else "{} ({})".format(day, leg)
                value = "{}:{}".format(entry.get("month"), index)
                options.append((value, label))

        if not options:
            return ""

        text = "<h2>Captured plans</h2>\n"
        text += '<div class="annual-plan-viewer">\n'
        text += '<div class="annual-field"><label for="annual-plan-day">Day</label><select id="annual-plan-day" onchange="annualLoadPlan()">\n'
        for value, label in options:
            text += '<option value="{}">{}</option>\n'.format(html.escape(value, quote=True), html.escape(label, quote=True))
        text += "</select></div>\n"
        text += '<div class="annual-field"><label for="annual-plan-scenario">Scenario</label><select id="annual-plan-scenario" onchange="annualLoadPlan()">\n'
        for key in SCENARIO_ORDER:
            text += '<option value="{}">{}</option>\n'.format(html.escape(key, quote=True), html.escape(SCENARIO_LABELS[key], quote=True))
        text += "</select></div>\n"
        text += '<div class="annual-field"><label for="annual-plan-debug">Show debug columns</label><input type="checkbox" id="annual-plan-debug" onchange="annualLoadPlan()"></div>\n'
        text += "<div id='annual-plan-container'></div>\n"
        text += get_plan_css()
        text += get_plan_renderer_js()
        text += "<script>\n"
        text += "const ANNUAL_RUN_ID = {};\n".format(_json_for_script(selected_id))
        text += """const ANNUAL_EMPTY_OVERRIDES = {
    manual_charge_times: [], manual_export_times: [], manual_freeze_charge_times: [],
    manual_freeze_export_times: [], manual_demand_times: [],
    manual_import_rates: [], manual_export_rates: [], manual_load_adjust: [], manual_soc: []
};
async function annualLoadPlan() {
    const [month, index] = document.getElementById('annual-plan-day').value.split(':');
    const scenario = document.getElementById('annual-plan-scenario').value;
    const showDebug = document.getElementById('annual-plan-debug').checked;
    const container = document.getElementById('annual-plan-container');
    const params = new URLSearchParams({run: ANNUAL_RUN_ID, month: month, index: index, scenario: scenario});
    try {
        const response = await fetch('./annual_plan?' + params.toString());
        if (!response.ok) { container.innerHTML = '<p>That plan is not available.</p>'; return; }
        container.innerHTML = renderPlanTable(await response.json(), ANNUAL_EMPTY_OVERRIDES, showDebug, false);
    } catch (error) {
        container.innerHTML = '<p>Could not load that plan.</p>';
    }
}
annualLoadPlan();
</script>
"""
        text += "</div>\n"
        return text

    def _render_caveats(self, results):
        """Return the caveats the engine attached to this run."""
        caveats = results.get("caveats") or []
        if not caveats:
            return ""
        text = "<h2>Caveats</h2>\n<ul class='annual-caveats'>\n"
        for caveat in caveats:
            text += "<li>{}</li>\n".format(html.escape(str(caveat), quote=True))
        text += "</ul>\n"
        return text

    def render_css(self):
        """Return the scoped styles for the tab."""
        return """<style>
.annual-form-wrap fieldset { border: 1px solid var(--md-border, #cbd5e1); margin-bottom: 1rem; padding: 0.75rem; }
.annual-form-wrap legend { font-weight: 600; }
.annual-field { margin: 0.35rem 0; }
.annual-field label { display: inline-block; min-width: 20rem; }
/* An Octopus rates URL runs to ~130 characters and an API key to ~32, so the default
   input width shows a keyhole view of both. These sit on their own line under the label
   rather than beside it, so they can use the full width without pushing the form wide;
   max-width keeps them inside the column on a narrow screen. */
.annual-field.annual-field-wide label { display: block; min-width: 0; margin-bottom: 0.15rem; }
.annual-field-wide input[type="text"] { width: 100%; max-width: 60rem; box-sizing: border-box; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.9rem; }
.annual-subgroup { margin-left: 1.5rem; }
.annual-note { font-size: 0.85rem; opacity: 0.8; }
.annual-banner { border-left: 4px solid #D55E00; padding: 0.5rem 0.75rem; margin-bottom: 1rem; }
.annual-error { border-left: 4px solid #b00020; padding: 0.5rem 0.75rem; margin-bottom: 1rem; }
.annual-progress { margin: 1rem 0; }
.annual-bar { height: 1.25rem; border: 1px solid var(--md-border, #cbd5e1); }
.annual-bar-fill { height: 100%; background: #0072B2; width: 0%; }
.annual-caveats li { margin-bottom: 0.35rem; }
.annual-unavailable { opacity: 0.6; font-style: italic; }
.annual-synthesised-tag { font-size: 0.75rem; font-weight: 600; color: #D55E00; border: 1px solid #D55E00; border-radius: 3px; padding: 0.05rem 0.35rem; margin-left: 0.35rem; }
</style>
"""

    def render_progress(self):
        """Return the progress area, hidden until a run starts."""
        return """<div class="annual-progress" id="annual-progress" style="display:none">
  <div class="annual-bar"><div class="annual-bar-fill" id="annual-bar-fill"></div></div>
  <p id="annual-progress-text"></p>
  <button type="button" onclick="annualCancel()">Cancel</button>
</div>
"""

    def render_script(self):
        """Return the polling and tariff-picker script."""
        return """<script>
function annualTariffChanged() {
  var select = document.getElementById('tariff_id');
  var option = select.options[select.selectedIndex];
  document.getElementById('tariff_import_url').value = option.getAttribute('data-import') || '';
  document.getElementById('tariff_export_url').value = option.getAttribute('data-export') || '';
}
function annualCancel() { fetch('./annual_cancel', {method: 'POST'}); }
function annualSolarModeChanged(index) {
  var mode = document.getElementById('solar_mode_' + index).value;
  document.getElementById('solar-kwp-row-' + index).style.display = (mode === 'panels') ? 'none' : 'block';
  document.getElementById('solar-panels-row-' + index).style.display = (mode === 'panels') ? 'block' : 'none';
  annualUpdateSolarTotal();
}
function annualUpdateSolarTotal() {
  var totalKwp = 0, totalPanels = 0, allPanels = true, index = 0;
  while (document.getElementById('solar_mode_' + index)) {
    var mode = document.getElementById('solar_mode_' + index).value;
    if (mode === 'panels') {
      var count = parseFloat(document.getElementById('solar_panels_' + index).value) || 0;
      var watts = parseFloat(document.getElementById('solar_panel_watts_' + index).value) || 0;
      totalPanels += count;
      totalKwp += count * watts / 1000;
    } else {
      allPanels = false;
      totalKwp += parseFloat(document.getElementById('solar_kwp_' + index).value) || 0;
    }
    index += 1;
  }
  var note = document.getElementById('annual-solar-total');
  if (!note) { return; }
  // The panel count is shown only when EVERY array was entered as panels. A partial
  // count would read as the whole system's, which is worse than not showing one.
  note.textContent = allPanels && totalPanels > 0
    ? 'Total: ' + totalKwp.toFixed(2) + ' kWp across ' + totalPanels + ' panels'
    : 'Total: ' + totalKwp.toFixed(2) + ' kWp';
}
document.addEventListener('input', function (event) {
  if (event.target && /^solar_(kwp|panels|panel_watts)_/.test(event.target.id)) { annualUpdateSolarTotal(); }
});
annualUpdateSolarTotal();
// sessionStorage is per-tab, which is exactly the scope wanted: the tab that pressed
// Run remembers it across the POST re-render, and no other tab sees the flag.
function annualMarkStarted() {
  try { sessionStorage.setItem('annualRunStarted', '1'); } catch (error) { /* private mode - fall back to the link */ }
}
function annualStartedHere() {
  try {
    if (sessionStorage.getItem('annualRunStarted')) { sessionStorage.removeItem('annualRunStarted'); return true; }
  } catch (error) { /* private mode - fall back to the link */ }
  return false;
}
function annualPoll() {
  fetch('./annual_status').then(function (r) { return r.json(); }).then(function (s) {
    var box = document.getElementById('annual-progress');
    var button = document.getElementById('annual-run-button');
    if (s.state === 'running') {
      box.style.display = 'block';
      if (button) { button.disabled = true; }
      var pct = s.total ? Math.round((s.completed / s.total) * 100) : 0;
      document.getElementById('annual-bar-fill').style.width = pct + '%';
      document.getElementById('annual-progress-text').textContent = s.message + ' — ' + pct + '% (' + s.elapsed + 's)';
    } else {
      if (button) { button.disabled = false; }
      if (s.state === 'complete') {
        box.style.display = 'block';
        // The tab that started the run goes straight to the results - ./annual with no
        // run parameter selects the newest stored run, which is the one that just
        // finished. Every OTHER tab only gets the link: this poll fires in all of them,
        // including ones mid-edit on a form for a run they never started, and a forced
        // reload there would silently drop whatever the user had typed.
        //
        // Navigating is safe from the redirect loop this once had because the server
        // hands out 'complete' exactly once (_consume_terminal_state), so the reloaded
        // page's own first poll sees idle rather than 'complete' again.
        if (annualStartedHere()) {
          document.getElementById('annual-progress-text').textContent = 'Run complete — loading results…';
          window.location.href = './annual';
          return;
        }
        document.getElementById('annual-progress-text').innerHTML = 'Run complete — <a href="./annual">view results</a>';
        return;
      }
      if (s.state === 'failed' || s.state === 'cancelled') {
        box.style.display = 'block';
        // Clear the started-here flag too: a run that failed has no results to show, and
        // leaving it set would send this tab off to the results the next time ANY run
        // completed, which it did not start.
        annualStartedHere();
        document.getElementById('annual-progress-text').textContent = s.state + (s.error ? ': ' + s.error : '');
        return;
      }
      box.style.display = 'none';
    }
    setTimeout(annualPoll, 1000);
  }).catch(function () { setTimeout(annualPoll, 5000); });
}
annualPoll();
</script>
"""
