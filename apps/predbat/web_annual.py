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
from annual_costs import DEFAULT_COSTS, build_costs, resolve_costs
from annual_job import AnnualJob
from annual_store import backfill_summaries, delete_run, list_runs, load_plan, load_run, save_run
from tariff_catalogue import (
    BASELINE_DEFAULT_EXPORT_ID,
    BASELINE_DEFAULT_IMPORT_ID,
    CUSTOM_ID,
    NO_EXPORT_ID,
    PRICE_CAP_IMPORT_P,
    PRICE_CAP_STANDING_CHARGE_P,
    SEG_EXPORT_P,
    match_entry,
    merged_export_catalogue,
    merged_import_catalogue,
)
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
    "tariff": {"rates_import": [{"rate": PRICE_CAP_IMPORT_P}], "rates_export": [{"rate": SEG_EXPORT_P}], "standing_charge_p_per_day": PRICE_CAP_STANDING_CHARGE_P},
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

    # Ordered, because the arrows step through it. Not wrapped: "next" from the last page
    # landing back on the first reads as a misclick rather than a choice.
    NAV_PAGES = [("config", "Configure", "./annual"), ("view", "Results", "./annual_view"), ("compare", "Compare", "./annual_compare")]

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

        # combine=True for the same reason as soc_max above, and it bites even harder here:
        # inverter_limit and export_limit are sensor_lists in APPS_SCHEMA, so a value is a
        # LIST even on a single-inverter system - a plain 3.6 kW inverter reads as [3600].
        # Without combine, get_arg()'s float-default coercion fails on the list, is caught
        # internally, and returns the 0.0 default, so every real system silently fell back
        # to the example 5 kW rather than showing its own limits. Summing is what the model
        # wants: two 3.6 kW inverters can move 7.2 kW between them.
        for arg_name, field, divisor in [("inverter_limit", "inverter_kw", 1000.0), ("export_limit", "export_limit_kw", 1000.0)]:
            watts = self._arg(arg_name, 0.0, combine=True) or 0
            try:
                watts = float(watts)
            except (TypeError, ValueError):
                watts = 0
            if watts > 0:
                config["battery"][field] = round(watts / divisor, 2)

        # Read the real setting rather than inferring it. This used to set hybrid whenever
        # an inverter_type was present at all, which is true of every configured system -
        # so an AC-coupled setup was modelled as hybrid, letting the plan charge the
        # battery straight from DC PV that in reality has to go out through the inverter
        # and back in.
        #
        # Taken off the instance attribute rather than through _arg(): inverter_hybrid is
        # a CONFIG_ITEMS switch whose value lives in an entity, not in apps.yaml, so
        # get_arg() ignores args entirely for it and always returns the default. This is
        # the same attribute the planner itself runs on (set in fetch.py). Defaults to
        # True for an instance that has not fetched its config yet, matching the switch's
        # own default.
        config["battery"]["hybrid"] = bool(getattr(self.base, "inverter_hybrid", True))

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

        # The load source is deliberately NOT prefilled from the Octopus credentials, even
        # when a complete pair is configured. Octopus gives us the IMPORT meter - what was
        # bought from the grid - and on a home that already has solar or a battery, that
        # system's self-consumption and discharge have already been subtracted from every
        # reading; modelling a system on top counts the same saving twice. The form says so
        # in its own banner. Anyone whose apps.yaml holds Octopus credentials is by
        # definition a configured Predbat with a battery or an array, so auto-selecting it
        # picked the one source that is wrong for them. Predicted consumption stays the
        # default and the choice is left to the user; render_form still fills the key and
        # account boxes from these args, so switching is a click rather than a paste.

        return config

    def _octopus_from_args(self):
        """Return the live instance's (api_key, account_id), or (None, None) if either is unset.

        ``indirect=False`` because an API key and an account id are free text that can
        contain a literal dot. Left at ``get_arg``'s ``indirect=True`` default,
        ``resolve_arg`` (``userinterface.py``) would treat a dotted value as a Home
        Assistant entity id, fail to find one, and silently return the default instead
        of the real credential.
        """
        return self._arg("octopus_api_key", None, indirect=False), self._arg("octopus_api_account", None, indirect=False)

    def import_catalogue(self):
        """Return the import tariff dropdown entries: built-ins merged with the user's own."""
        return merged_import_catalogue(self._arg("compare_list", None))

    def export_catalogue(self):
        """Return the export tariff dropdown entries: built-ins merged with the user's own."""
        return merged_export_catalogue(self._arg("compare_list", None))

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

    @staticmethod
    def _selected_side_id(tariff, catalogue, url_key, rates_key):
        """Return the catalogue id matching one side of this tariff, Custom, or None if unset.

        The matching itself is ``tariff_catalogue.match_entry``, shared with the compare
        table so the dropdown and the table cannot describe the same tariff differently.
        What is added here is the dropdown's own two special cases: None for a side that
        was never set, so the caller can leave the select alone, and Custom for one that
        matches no entry - a hand-entered URL is exactly what Custom means, and falling
        back to it beats leaving the dropdown showing the first entry in the list.
        """
        tariff = tariff or {}
        if not tariff.get(url_key) and not tariff.get(rates_key):
            return None
        entry = match_entry(catalogue, tariff, url_key, rates_key)
        return entry["id"] if entry else CUSTOM_ID

    @staticmethod
    def _tariff_select(name, label, catalogue, selected_id, url_key):
        """Return one tariff dropdown as HTML.

        The URL each entry carries rides along in ``data-url`` so picking Custom can
        start from the tariff that was on screen rather than from an empty box.
        """
        text = '<div class="annual-field"><label for="{name}">{label}</label><select id="{name}" name="{name}" onchange="annualTariffChanged(\'{name}\')">\n'.format(name=name, label=label)
        for entry in catalogue:
            text += '<option value="{}" data-url="{}" {}>{}</option>\n'.format(
                html.escape(entry["id"], quote=True),
                html.escape(entry.get(url_key, ""), quote=True),
                "selected" if entry["id"] == selected_id else "",
                html.escape(entry["name"], quote=True),
            )
        return text + "</select></div>\n"

    @classmethod
    def _selected_import_id(cls, tariff, catalogue):
        """Return the import catalogue id matching this tariff, or Custom, or None if unset."""
        return cls._selected_side_id(tariff, catalogue, "import_octopus_url", "rates_import")

    @classmethod
    def _selected_export_id(cls, tariff, catalogue):
        """Return the export catalogue id matching this tariff, or Custom, or None if unset."""
        return cls._selected_side_id(tariff, catalogue, "export_octopus_url", "rates_export")

    def render_form(self, config, errors=None):
        """Return the configuration form as HTML, populated from ``config``.

        ``errors`` is displayed above the form with every field left as the user
        entered it - losing their input on a validation failure would be worse
        than the failure.
        """
        # An ABSENT solar key means "not configured yet", so offer one blank array to fill
        # in. An explicitly EMPTY list means the user removed them all, which is a valid
        # battery-only run - collapsing the two with `or [{}]` made it impossible to get
        # to zero arrays through the form.
        solar = config.get("solar")
        if solar is None:
            solar = [{}]
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
        # The actions sit at the TOP of the form, directly under the tabs, so Run is
        # reachable without scrolling past every fieldset - and so the progress bar that
        # appears on pressing it is in view rather than below the fold. The progress area
        # is emitted here, inside the form, purely so it sits immediately under the
        # buttons that start it.
        text += '<div class="annual-actions">\n'
        text += '<button type="submit" id="annual-run-button" onclick="annualMarkStarted()">Run simulations</button>\n'
        # A second submit button pointed at the plain POST /annual handler via
        # formaction - the same fields, saved without starting a run.
        text += '<button type="submit" formaction="./annual" formmethod="post">Save settings</button>\n'
        # Discards the saved configuration and rebuilds it from the live instance, so it
        # confirms first - there is no undo, and someone who has spent a while tuning a
        # setup would not thank us for wiping it on a stray click.
        text += '<button type="submit" formaction="./annual_reset" formmethod="post" class="annual-secondary" onclick="return confirm(\'Reset the configuration to your Predbat settings and the standard defaults? Anything you have changed here will be lost.\');">Reset to defaults</button>\n'
        text += "</div>\n"
        text += self.render_progress()

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
            # Add and remove go through the server rather than cloning markup in JS: an
            # array block is a mode select, two toggled rows and a matching efficiency
            # field over in Advanced, and keeping a JS clone in step with all of that
            # would rot. Submitting re-renders from config_from_post, so everything
            # already typed survives and the arrays renumber themselves.
            text += '<button type="submit" formaction="./annual_array" formmethod="post" name="array_op" value="remove:{}" class="annual-secondary">Remove array {}</button>\n'.format(index, index + 1)
            text += "</div>\n"
        if not solar:
            text += '<p class="annual-note">No solar arrays — the run will model the battery on its own.</p>\n'
        text += '<p class="annual-note" id="annual-solar-total"></p>\n'
        text += '<button type="submit" formaction="./annual_array" formmethod="post" name="array_op" value="add" class="annual-secondary">Add another array</button>\n'
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
        # Fall back to the live instance's credentials when the config being rendered
        # carries none. prefill_config() only runs on a FIRST visit - load_config()
        # returns the saved annual.yaml once one exists - so without this, a user who
        # saved a config before this prefill was added (or who saved one while on the
        # manual source, which stores no octopus block at all) would see these two boxes
        # permanently empty despite having Octopus configured, and would have to paste
        # their key in by hand every time.
        #
        # Only the FIELD VALUES fall back, never the selected source: the saved config's
        # manual/Octopus choice is the user's and is left alone. Injecting the octopus
        # block into a saved manual config instead would make it invalid outright, since
        # _validate_load treats the two as mutually exclusive.
        saved_octopus = load.get("octopus") or {}
        args_key, args_account = self._octopus_from_args()
        text += self._text_field("load_octopus_api_key", "Octopus API key", saved_octopus.get("api_key") or (args_key if args_key and args_account else "") or "", wide=True)
        text += self._text_field("load_octopus_account_id", "Account ID", saved_octopus.get("account_id") or (args_account if args_key and args_account else "") or "", wide=True)
        text += '<p class="annual-note">Your meter readings already include any car charging, so the figures above are not used with this option.</p>\n'
        # An import meter records what was BOUGHT, not what the house used. On a home that
        # already has solar or a battery, the existing system's self-consumption and
        # discharge have already been subtracted from every reading. Modelling solar and a
        # battery on top of that applies the saving twice and overstates the result - so
        # this option is only sound for a house with neither fitted yet, which is also the
        # case the whole tool is aimed at.
        text += '<p class="annual-banner"><strong>Only use this if you do not already have solar or a battery.</strong> '
        text += "Octopus gives us what you imported from the grid, not what your home used. If you already generate or store your own, "
        text += "that has already been taken off these readings, and adding a modelled system on top would count the same saving twice "
        text += "and overstate what it is worth. With an existing system, enter your total annual consumption above instead.</p>\n"
        if self.is_configured():
            # The live instance already reports a battery or an array, so this is not a
            # hypothetical for this particular user.
            text += '<p class="annual-banner"><strong>Your Predbat setup already has solar or a battery configured</strong>, so meter readings from this account will '
            text += "understate what your home actually uses. Enter your annual consumption above rather than importing it.</p>\n"
        text += "</div>\n"
        text += "</fieldset>\n"

        text += "<fieldset><legend>Tariff</legend>\n"
        # Import and export are picked separately so any combination can be modelled -
        # including an import tariff paired with no export payment at all, which is what
        # a household without an export agreement is actually on.
        import_catalogue = self.import_catalogue()
        export_catalogue = self.export_catalogue()
        selected_import = self._selected_import_id(tariff, import_catalogue)
        # A tariff carrying no export source at all means export is not paid for, which is
        # a real selection rather than an absent one. Stated explicitly so the default does
        # not rest on "No export payment" happening to be first in the list.
        selected_export = self._selected_export_id(tariff, export_catalogue) or NO_EXPORT_ID
        text += self._tariff_select("tariff_import_id", "Import tariff", import_catalogue, selected_import, "import_octopus_url")
        # The URL box belongs to Custom alone. The dropdown is authoritative for every
        # other entry (see config_from_post), so leaving it on screen for a built-in
        # tariff invites someone to edit a value that is then ignored - and worse, used
        # to look like the selection when it no longer matches it.
        text += '<div id="tariff-custom-import" style="display:{}">\n'.format("block" if selected_import == CUSTOM_ID else "none")
        text += self._text_field("tariff_import_url", "Import rates URL", tariff.get("import_octopus_url", ""), wide=True)
        text += "</div>\n"
        text += self._tariff_select("tariff_export_id", "Export tariff", export_catalogue, selected_export, "export_octopus_url")
        text += '<div id="tariff-custom-export" style="display:{}">\n'.format("block" if selected_export == CUSTOM_ID else "none")
        text += self._text_field("tariff_export_url", "Export rates URL", tariff.get("export_octopus_url", ""), wide=True)
        text += "</div>\n"
        text += self._text_field("tariff_dno_region", "Octopus region letter", tariff.get("dno_region", ""))
        baseline = config.get("baseline_tariff") or {}
        # Falls back to the price cap when the config carries no baseline - a config
        # saved before this existed, or a fresh prefill. Matches validate_config's own
        # default, so what the form shows is what a run would actually use.
        #
        # CUSTOM_ID counts as "no baseline" here, because this dropdown deliberately has
        # no Custom option (the baseline answers "what would they otherwise be on", so a
        # hand-entered URL has no place in it - see config_from_post). A hand-edited YAML
        # baseline matching no entry therefore left NOTHING marked selected, and the form
        # relied on the browser falling back to the first option to show anything at all.
        # Naming the default explicitly is what makes the rendered form state its
        # selection rather than imply it.
        baseline_id = self._selected_import_id(baseline, import_catalogue)
        if not baseline_id or baseline_id == CUSTOM_ID:
            baseline_id = BASELINE_DEFAULT_IMPORT_ID
        text += '<div class="annual-field"><label for="baseline_tariff_id">Import tariff without PV or a battery</label><select id="baseline_tariff_id" name="baseline_tariff_id">\n'
        for entry in import_catalogue:
            if entry["id"] == CUSTOM_ID:
                continue
            text += '<option value="{}" {}>{}</option>\n'.format(html.escape(entry["id"], quote=True), "selected" if entry["id"] == baseline_id else "", html.escape(entry["name"], quote=True))
        text += "</select></div>\n"
        # Import only, deliberately: with no PV and no battery there is nothing to export,
        # so an export tariff for this scenario would be a control that changes no figure.
        text += '<p class="annual-note">Used only for the no-PV/battery comparison, which has nothing to export - so only the import side applies. Someone with no system would not be on a battery tariff, so pricing that scenario on one credits it with a saving it could never have had. The price cap is the sensible default.</p>\n'
        text += self._number_field("tariff_standing_charge", "Standing charge", tariff.get("standing_charge_p_per_day", PRICE_CAP_STANDING_CHARGE_P), suffix="p/day")
        text += "</fieldset>\n"

        # The estimate is filled in by JavaScript from ./annual_cost_preview rather than
        # rendered here, so it tracks the solar and battery fields as they are typed. The
        # figure comes from the same annual_costs model the run itself uses - see
        # html_annual_cost_preview for why it is fetched rather than recomputed in JS.
        costs_config = config.get("costs") or {}
        text += "<fieldset><legend>System cost</legend>\n"
        text += '<p class="annual-note" id="annual-cost-estimate">Estimating…</p>\n'
        text += '<p class="annual-note">Estimated from typical UK install prices. If you have a real quote, enter it below and it will be used instead — leave a box at 0 to keep the estimate.</p>\n'
        text += '<p class="annual-note">Most quotes cover the whole installation, so the second box is usually the one you want. The battery price is taken as the difference between the two, and the solar-only figure is what the PV-only payback is worked out from — leave it at 0 and that row uses the estimate instead.</p>\n'
        text += self._number_field("cost_quoted_pv_gbp", "Quoted price for solar only", costs_config.get("quoted_pv_gbp", 0), suffix="£")
        text += self._number_field("cost_quoted_total_gbp", "Quoted price for solar &amp; battery", costs_config.get("quoted_total_gbp", 0), suffix="£")
        text += "</fieldset>\n"

        text += "<details><summary>Advanced</summary>\n"
        text += self._number_field("year", "Year to model (blank for the most recent complete year)", config.get("year"))
        text += self._number_field("samples_per_month", "Days sampled per month", config.get("samples_per_month", 2), step="1")
        text += '<div class="annual-field"><label for="debug">Save plans for debugging</label><input type="checkbox" id="debug" name="debug" {}></div>\n'.format("checked" if config.get("debug") else "")
        text += '<p class="annual-note">Keeps each sampled day\'s plan so you can inspect it below. Makes the saved run much larger.</p>\n'
        text += self._number_field("pv10_derate_fallback", "P10 fallback derate", config.get("pv10_derate_fallback", 0.7))
        for index, array in enumerate(solar):
            text += self._number_field("solar_efficiency_{}".format(index), "Array {} efficiency".format(index + 1), array.get("efficiency", 0.95))
        # The two quote fields are deliberately NOT here - a real quote is the most useful
        # thing a user can tell us, so it belongs on the page rather than behind Advanced.
        # These are the model's own parameters, which most people never touch.
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

        # The dropdown is authoritative for every entry except Custom, and the URL boxes
        # are only shown for Custom. Reading the boxes regardless (as this used to) meant
        # the two could disagree: picking a BASIC-RATES entry such as "Eon Next Drive"
        # or "Price cap import / SEG export" cleared both boxes, and the no-URL fallback
        # below then silently ran the run at the price-cap default instead of the tariff
        # named in the dropdown - a different set of rates to the one on screen.
        tariff = {"standing_charge_p_per_day": numeric("tariff_standing_charge", 0)}
        for field, catalogue, url_key, rates_key, url_field in [
            ("tariff_import_id", self.import_catalogue(), "import_octopus_url", "rates_import", "tariff_import_url"),
            ("tariff_export_id", self.export_catalogue(), "export_octopus_url", "rates_export", "tariff_export_url"),
        ]:
            selected = value(field) or CUSTOM_ID
            if selected == CUSTOM_ID:
                if value(url_field):
                    tariff[url_key] = value(url_field)
                continue
            for entry in catalogue:
                if entry.get("id") != selected:
                    continue
                for key in [url_key, rates_key]:
                    # `is not None` rather than truthiness: "No export payment" is
                    # rates_export [{"rate": 0.0}], a perfectly real selection, and a
                    # rate of zero must not be dropped here as if nothing were chosen.
                    if entry.get(key) is not None:
                        tariff[key] = copy.deepcopy(entry[key])
                break

        if value("tariff_dno_region"):
            tariff["dno_region"] = value("tariff_dno_region")
        # Only reached by Custom with nothing filled in, or an id that is no longer in the
        # catalogue (a compare_list entry the user has since removed).
        if not tariff.get("import_octopus_url") and not tariff.get("rates_import"):
            tariff["rates_import"] = DEFAULT_CONFIG["tariff"]["rates_import"]
            tariff["rates_export"] = DEFAULT_CONFIG["tariff"]["rates_export"]
        config["tariff"] = tariff

        # The baseline is chosen from the catalogue only - it answers "what would they
        # otherwise be on", so a hand-entered URL has no place in it. An unrecognised id
        # is left out entirely, and validate_config then supplies the price-cap default
        # rather than this silently inventing rates.
        baseline_id = value("baseline_tariff_id")
        if baseline_id and baseline_id != CUSTOM_ID:
            for entry in self.import_catalogue():
                if entry.get("id") == baseline_id:
                    baseline = {key: copy.deepcopy(entry[key]) for key in ["import_octopus_url", "rates_import"] if entry.get(key)}
                    # The scenario exports nothing, so this export side is inert - it is
                    # carried only so the baseline tariff has the same shape as any other
                    # and AnnualTariff has a defined export source rather than none.
                    for export_entry in self.export_catalogue():
                        if export_entry["id"] == BASELINE_DEFAULT_EXPORT_ID:
                            baseline.update({key: copy.deepcopy(export_entry[key]) for key in ["export_octopus_url", "rates_export"] if export_entry.get(key)})
                            break
                    config["baseline_tariff"] = baseline
                    break

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
        """Render the Annual configuration page: the form and the run progress, nothing else.

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

        Deliberately does not render any stored run's results: showing the form and a
        past run's totals on the same page let the form's current values read as
        though they described the run below them, which is often untrue - the run
        selector can show a run made on a completely different system. The results
        live on their own page, ``html_annual_view``.
        """
        self.web.default_page = "./annual"
        if config is None:
            config = self.load_config()

        text = self.web.get_header("Predbat What If")
        text += "<body>\n"
        text += self.render_css()
        text += self.render_nav("config")
        text += self.render_form(config, errors=error)
        text += self.render_script()
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    async def html_annual_view(self, request):
        """Render the results viewer for one stored run."""
        self.web.default_page = "./annual_view"
        text = self.web.get_header("Predbat What If")
        text += "<body>\n"
        text += self.render_css()
        text += self.render_nav("view")
        text += self.render_progress()
        storage = self._storage()
        runs = await list_runs(storage)
        selected = request.query.get("run") or (runs[0]["id"] if runs else None)
        results = await load_run(storage, selected) if selected else None
        text += self.render_results(results, runs, selected)
        text += self.render_script()
        text += "</body></html>\n"
        return web.Response(content_type="text/html", text=text)

    async def html_annual_compare(self, request):
        """Render the run comparison table."""
        self.web.default_page = "./annual_compare"
        storage = self._storage()
        runs = await backfill_summaries(storage, await list_runs(storage))
        text = self.web.get_header("Predbat What If")
        text += "<body>\n"
        text += self.render_css()
        text += self.render_nav("compare")
        text += self.render_progress()
        text += self.render_compare(runs, request.query.get("run"))
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

    async def html_annual_cost_preview(self, request):
        """Return the estimated install cost for a system size, as JSON for the live form.

        Exists so the form can show a running cost without the cost model being
        reimplemented in JavaScript. Duplicating it there would put the band
        interpolation, the minimum install price and the quote overrides in two places
        that would drift apart - the same hazard the prediction kernel carries a written
        parity rule about. This keeps ``annual_costs`` the only implementation.

        Every parameter is optional and anything unparseable is treated as absent, since
        the query string arrives straight off a keystroke: a half-typed "3." must produce
        a sensible answer rather than a 500.
        """

        def number(name):
            """Return one query parameter as a float, or 0.0 if it is absent or malformed."""
            try:
                return float(request.query.get(name, "") or 0)
            except (TypeError, ValueError):
                return 0.0

        overrides = {}
        for name in ["quoted_pv_gbp", "quoted_total_gbp"]:
            value = number(name)
            if value > 0:
                overrides[name] = value
        try:
            settings = resolve_costs(overrides)
        except ValueError:
            settings = resolve_costs(None)
        costs = build_costs(number("total_kwp"), number("battery_kwh"), settings)
        return web.json_response(costs)

    async def html_annual_delete(self, request):
        """Delete one stored run and return to the comparison table.

        Redirects rather than rendering in place so a refresh cannot re-post the
        deletion - by then the run is gone, and the second attempt would report a
        failure for something that already succeeded.
        """
        postdata = await request.post()
        await delete_run(self._storage(), str(postdata.get("run", "")))
        raise web.HTTPFound("./annual_compare")

    async def html_annual_array(self, request):
        """Add or remove a solar array and re-render the form.

        Goes through the server rather than cloning markup in JavaScript: an array block
        is a mode select, two toggled rows and a matching efficiency field in Advanced,
        and a JS clone would have to be kept in step with all of it. Rebuilding from
        ``config_from_post`` keeps everything already typed and renumbers the arrays for
        free.

        Deliberately does NOT save. Adding a row is a half-finished edit like any other -
        the new array has no size yet - and writing it to disk would persist a
        configuration that cannot be run. Save settings and Run simulations are both
        right there.
        """
        postdata = await request.post()
        config = self.config_from_post(postdata)
        operation = str(postdata.get("array_op", ""))
        arrays = list(config.get("solar") or [])

        if operation == "add":
            # Orientation defaults only - size is left blank so it is obvious which array
            # still needs filling in, rather than quietly running at a made-up 5 kWp.
            arrays.append({"declination": 35, "azimuth": 180, "efficiency": 0.95})
        elif operation.startswith("remove:"):
            try:
                index = int(operation.split(":", 1)[1])
            except (TypeError, ValueError):
                index = -1
            if 0 <= index < len(arrays):
                arrays.pop(index)

        config["solar"] = arrays
        return await self.html_annual(request, config=config)

    async def html_annual_reset(self, request):
        """Discard the saved configuration and rebuild it from the live instance.

        "Default" here means ``prefill_config()``, not the bare example: it reads
        whatever this Predbat knows - solar arrays, battery and inverter sizes, tariff
        URLs, region - and fills only the gaps with the example UK system. So on a
        configured instance this resets to *your* setup rather than to a stranger's,
        which is the point of offering it.

        The load source is the exception: a reset always returns it to predicted
        consumption rather than the Octopus import meter, which is only sound for a home
        with no solar or battery fitted. See ``prefill_config()``.

        The result is saved, not merely displayed: a reset the user has to remember to
        confirm with Save would leave the old configuration on disk and running, which is
        the opposite of what the button says it does.
        """
        config = self.prefill_config()
        self.save_config(config)
        return await self.html_annual(request, config=config)

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
        """Return one stored run's raw results document as a JSON download.

        The document is exactly as stored: a debug run's captured plans are held
        separately under their own storage keys (see ``annual_store.save_run``) and
        are not included here.
        """
        run_id = request.query.get("run")
        results = await load_run(self._storage(), run_id)
        if results is None:
            return web.json_response({"error": "No stored run with id {}".format(run_id)}, status=404)
        return web.json_response(results, headers={"Content-Disposition": 'attachment; filename="annual-{}.json"'.format(run_id)})

    async def html_annual_plan(self, request):
        """Return one captured plan as JSON, for the results page's plan viewer to render.

        Only present at all under a debug run; a non-debug run wrote no plan keys, so
        ``load_plan`` falls through to None and this 404s, same as any other query it
        cannot resolve.
        """
        plan = await load_plan(self._storage(), request.query.get("run", ""), request.query.get("month"), request.query.get("index"), request.query.get("scenario"))
        if plan is None:
            return web.json_response({"error": "plan not found"}, status=404)
        return web.json_response(plan)

    @staticmethod
    def _describe_tariff_side(tariff, url_key, rates_key):
        """Return a readable description of one side of a tariff, import or export.

        A rates URL is ~130 characters of mostly boilerplate, so the Octopus product
        code is pulled out of it - that is the part a user recognises. The full URL is
        kept as the cell's title attribute for anyone who needs to check the exact
        endpoint.
        """
        url = tariff.get(url_key)
        if url:
            match = re.search(r"/products/([^/]+)/", str(url))
            product = match.group(1) if match else str(url)
            return html.escape(product, quote=True), html.escape(str(url), quote=True)
        rates = tariff.get(rates_key)
        if isinstance(rates, list) and rates:
            values = [entry.get("rate") for entry in rates if isinstance(entry, dict) and entry.get("rate") is not None]
            # "flat 0p" is accurate but reads like a missing figure. Naming the choice
            # keeps a deliberate no-export run distinguishable from a broken one.
            if values and set(values) == {0} and rates_key == "rates_export":
                return "no export payment", ""
            if values and len(set(values)) == 1:
                return "flat {}p".format(values[0]), ""
            if values:
                return "{} rates, {}p to {}p".format(len(values), min(values), max(values)), ""
        return "not set", ""

    @classmethod
    def _name_tariff_side(cls, catalogue, tariff, url_key, rates_key):
        """Return (display text, title) naming one side of a tariff from the catalogue.

        The catalogue's own name first - "Octopus Agile", "Price cap", "No export
        payment" - because that is the wording the user picked on the form, and it is
        the only thing that can describe an entry carrying no URL to parse. The user's
        own ``compare_list`` tariffs are named too, which is precisely why this happens
        here rather than at save time: only the web layer can read ``compare_list``.

        A side matching nothing - a hand-entered URL, or a ``compare_list`` entry since
        deleted - falls through to ``_describe_tariff_side``, which digs out whatever is
        readable rather than leaving the cell blank.

        The full URL is still carried as the title in both cases, so naming a tariff
        never hides which endpoint it actually priced against.
        """
        tariff = tariff or {}
        entry = match_entry(catalogue, tariff, url_key, rates_key)
        if entry:
            return html.escape(entry["name"], quote=True), html.escape(str(tariff.get(url_key) or ""), quote=True)
        return cls._describe_tariff_side(tariff, url_key, rates_key)

    @staticmethod
    def _tariff_cell(value, title):
        """Return one tariff cell's HTML, wrapped in a title only when there is one to show."""
        return '<span title="{}">{}</span>'.format(title, value) if title else value

    def _render_run_details(self, results):
        """Return a small table of the key settings this run actually used.

        Read from the run's OWN stored config, never from the form above or the live
        instance: the selector can show a run made with a different system entirely, and
        labelling it with today's settings would misattribute every figure below it.

        Secrets are already redacted - ``scrub_secrets`` replaced anything key-like with
        "xxx" before the results document was written - so nothing sensitive is rendered
        here even though the Octopus account is shown.
        """
        config = results.get("config")
        if not isinstance(config, dict) or not config:
            return "<p class='annual-note'>This run did not record the settings it used.</p>\n"

        rows = []
        rows.append(("Year modelled", html.escape(str(results.get("year", "unknown")), quote=True)))

        solar = config.get("solar") or []
        if isinstance(solar, dict):
            solar = [solar]
        if isinstance(solar, list) and solar:
            total_kwp = sum(float(array.get("kwp", 0) or 0) for array in solar if isinstance(array, dict))
            panels = [int(array.get("panels", 0) or 0) for array in solar if isinstance(array, dict)]
            # Only show a panel count when EVERY array was given as panels; a partial
            # count would read as the whole system's.
            if all(panels) and panels:
                summary = "{:g} kWp across {} panels".format(round(total_kwp, 2), sum(panels))
            else:
                summary = "{:g} kWp".format(round(total_kwp, 2))
            if len(solar) > 1:
                summary += " in {} arrays".format(len(solar))
            rows.append(("Solar", html.escape(summary, quote=True)))
        else:
            rows.append(("Solar", "none"))

        battery = config.get("battery") or {}
        if isinstance(battery, dict) and battery.get("size_kwh"):
            summary = "{:g} kWh, {:g} kW inverter".format(float(battery["size_kwh"]), float(battery.get("inverter_kw", 0) or 0))
            if battery.get("export_limit_kw"):
                summary += ", {:g} kW export limit".format(float(battery["export_limit_kw"]))
            summary += ", hybrid" if battery.get("hybrid", True) else ", AC coupled"
            rows.append(("Battery", html.escape(summary, quote=True)))
        else:
            rows.append(("Battery", "none"))

        load = config.get("load") or {}
        if isinstance(load, dict) and load.get("octopus"):
            account = (load.get("octopus") or {}).get("account_id", "")
            rows.append(("Household load", html.escape("Octopus consumption history{}".format(" for {}".format(account) if account else ""), quote=True)))
            rows.append(("Car charging", "included in the meter readings"))
        elif isinstance(load, dict):
            shape = {"night": "more at night", "day": "more during the day", "flat": "about the same through the day"}.get(load.get("shape", "flat"), str(load.get("shape", "")))
            rows.append(("Household load", html.escape("{:,.0f} kWh a year, {}".format(float(load.get("annual_kwh", 0) or 0), shape), quote=True)))
            car_kwh = float(load.get("car_charging_kwh", 0) or 0)
            car = "{:,.0f} kWh a year at {:g} kW".format(car_kwh, float(load.get("car_rate_kw", 7.4) or 7.4)) if car_kwh > 0 else "none"
            rows.append(("Car charging", html.escape(car, quote=True)))

        # The baseline is what the no-PV/battery scenario was priced on, and therefore
        # what every saving on this page is measured FROM. Stating the saving without it
        # leaves a figure nobody can check. Only shown when the run recorded one - an
        # older run did not, and inventing today's default for it would be a guess about
        # what that run actually did.
        baseline = config.get("baseline_tariff")
        if isinstance(baseline, dict) and baseline:
            rows.append(("Baseline tariff", self._tariff_cell(*self._name_tariff_side(self.import_catalogue(), baseline, "import_octopus_url", "rates_import"))))

        tariff = config.get("tariff") or {}
        if isinstance(tariff, dict):
            for label, catalogue, url_key, rates_key in [
                ("Import tariff", self.import_catalogue(), "import_octopus_url", "rates_import"),
                ("Export tariff", self.export_catalogue(), "export_octopus_url", "rates_export"),
            ]:
                rows.append((label, self._tariff_cell(*self._name_tariff_side(catalogue, tariff, url_key, rates_key))))
            if tariff.get("standing_charge_p_per_day") is not None:
                rows.append(("Standing charge", html.escape("{:g}p a day".format(float(tariff["standing_charge_p_per_day"])), quote=True)))

        if config.get("samples_per_month"):
            rows.append(("Days sampled", html.escape("{} a month".format(config["samples_per_month"]), quote=True)))

        text = "<h2>What this run used</h2>\n<table class='comparison-table annual-details'>\n"
        for label, value in rows:
            text += "<tr><th>{}</th><td>{}</td></tr>\n".format(label, value)
        text += "</table>\n"
        return text

    @staticmethod
    def _pounds(pence):
        """Return a pence value formatted as pounds with an explicit unit."""
        try:
            return "£{:.2f}".format(float(pence) / 100.0)
        except (TypeError, ValueError):
            return "n/a"

    def render_results(self, results, runs, selected_id):
        """Return the results view: selector, run details, totals, chart, monthly table, caveats.

        The configuration form and these results now live on separate pages (see
        ``html_annual`` and ``html_annual_view``), so no divider or heading is needed
        here to keep them visually apart - the nav strip above already tells the
        visitor which page, and therefore which of the two, they are looking at.
        """
        text = '<div class="annual-results">\n'
        text += self._render_selector(runs, selected_id)

        if not results:
            text += "<p>No results yet — fill in the form above and press Run simulations.</p>\n</div>\n"
            return text

        if not isinstance(results, dict):
            text += "<p>This run's results file is missing or could not be read.</p>\n</div>\n"
            return text

        text += self._render_run_details(results)

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
        selected_entry = next((run for run in (runs or []) if run.get("id") == selected_id), None)
        # None when the index entry carries no "plan_index" key at all - a run stored
        # before this existed - so _render_plan_viewer falls back to reading plans out
        # of the document itself; [] for a found entry that simply captured none.
        plan_index = selected_entry.get("plan_index") if selected_entry is not None else None
        text += self._render_plan_viewer(results, selected_id, plan_index)
        text += self._render_caveats(results)
        if selected_id:
            text += '<p><a href="./annual_download?run={}">Download this run as JSON</a></p>\n'.format(html.escape(str(selected_id), quote=True))
        text += "</div>\n"
        return text

    def _render_selector(self, runs, selected_id):
        """Return the run selector, or nothing when there are no stored runs."""
        if not runs:
            return ""
        text = '<form action="./annual_view" method="get" class="annual-selector"><label for="run">Run</label><select id="run" name="run" onchange="this.form.submit()">\n'
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

    def _render_plan_viewer(self, results, selected_id, plan_index):
        """Return the captured-plan viewer, or nothing when this run captured no plans.

        ``annual_store.save_run`` strips a debug run's captured plans out of the
        results document and records their labels (month, index, day, leg - no
        payload) as ``plan_index`` on the run's index entry instead, so this reads
        from ``plan_index`` rather than from ``results`` - the document ``save_run``
        stores no longer carries the plans at all.

        ``plan_index`` is ``None`` for a run stored before it existed (an index
        entry with no ``"plan_index"`` key), which falls back to reading the plans
        out of the document itself, exactly as this used to work for every run -
        that document still has them embedded, since it predates the split. An
        empty list, by contrast, means the entry was found and simply captured no
        plans (an ordinary, non-debug run), and must not trigger that fallback.

        The day options are built in the same order the plans were recorded in -
        months 1..12, each month's legs in the order the engine sampled them -
        which is already chronological, so no separate sort is needed either way.
        """
        options = []
        if plan_index is not None:
            for item in plan_index:
                if not isinstance(item, dict):
                    continue
                day = item.get("day") or ""
                leg = item.get("leg", "single")
                label = day if leg == "single" else "{} ({})".format(day, leg)
                value = "{}:{}".format(item.get("month"), item.get("index"))
                options.append((value, label))
        else:
            for entry in results.get("months", []) or []:
                plans = entry.get("plans") or []
                for index, plan in enumerate(plans):
                    if not isinstance(plan, dict):
                        continue
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

    def render_compare(self, runs, selected_id):
        """Return the run comparison table, or an explanation when there are none.

        Each row is built from that run's OWN ``summary`` alone (see
        ``_render_compare_row``) - never another run's, and never the live form -
        so twenty rows correctly show twenty different systems rather than one
        run's figures echoed down every row.
        """
        if not runs:
            return '<div class="annual-compare-scroll"><p>No runs have been stored yet — run some simulations to compare them here.</p></div>\n'

        # Built once and passed down rather than per row: each merge walks the built-ins
        # and the user's compare_list, and twenty rows would repeat that work twenty times
        # for an answer that cannot change between them.
        catalogues = (self.import_catalogue(), self.export_catalogue())

        text = '<div class="annual-compare-scroll">\n<table class="comparison-table annual-compare">\n'
        text += (
            "<tr><th>Run</th><th>Solar</th><th>Battery</th><th>System cost</th><th>Baseline</th><th>Import</th><th>Export</th>"
            "<th>Cost with Predbat</th><th>Saving vs no system</th>"
            "<th>PV only pays back in</th><th>PV + battery pays back in</th><th>With Predbat pays back in</th><th></th></tr>\n"
        )
        for run in runs:
            text += self._render_compare_row(run, selected_id, catalogues)
        text += "</table>\n</div>\n"
        return text

    def _render_compare_row(self, run, selected_id, catalogues):
        """Return one comparison row, reading entirely from ``run``'s own summary.

        Reading anything other than ``run["summary"]`` here - another run in the
        list, or the live configuration form - has already been a defect once on
        this branch: it misattributes every figure on the row to the wrong system.

        ``catalogues`` is the (import, export) pair used to name the three tariffs; it
        describes the tariffs on offer, not any one run, so sharing it across rows cannot
        leak one run's figures into another's.
        """
        summary = run.get("summary") or {}
        run_id = str(run.get("id", ""))
        label = str(run.get("label", run_id))
        row_class = ' class="annual-compare-current"' if run.get("id") == selected_id else ""

        text = "<tr{}>\n".format(row_class)
        text += '<td><a href="./annual_view?run={}">{}</a></td>\n'.format(html.escape(run_id, quote=True), html.escape(label, quote=True))
        text += "<td>{}</td>\n".format(self._compare_number(summary.get("total_kwp"), " kWp"))
        text += "<td>{}</td>\n".format(self._compare_number(summary.get("battery_kwh"), " kWh"))
        # A payback period means little without the outlay it is repaying, so the price
        # sits beside it. Marked when it came from a real quote rather than the model.
        total_gbp = summary.get("total_gbp")
        if isinstance(total_gbp, (int, float)):
            price = "£{:,.0f}".format(total_gbp)
            if summary.get("quoted"):
                price += ' <span class="annual-note" title="From a quote you entered, not the cost model">quoted</span>'
        else:
            price = "—"
        text += "<td>{}</td>\n".format(price)
        import_catalogue, export_catalogue = catalogues
        # Baseline first: it is what the other two are being judged against, so reading
        # left to right gives "instead of this, on these, it costs...".
        for catalogue, tariff, url_key, rates_key in [
            (import_catalogue, summary.get("baseline_tariff"), "import_octopus_url", "rates_import"),
            (import_catalogue, summary.get("tariff"), "import_octopus_url", "rates_import"),
            (export_catalogue, summary.get("tariff"), "export_octopus_url", "rates_export"),
        ]:
            # A summary written before these fields existed holds a plain string here
            # rather than a dict. backfill_summaries normally refreshes those, but it
            # skips a run whose document has gone, so the table must still draw a row for
            # one rather than failing the whole page over a value it cannot describe.
            if not isinstance(tariff, dict) or not tariff:
                text += "<td>—</td>\n"
                continue
            text += "<td>{}</td>\n".format(self._tariff_cell(*self._name_tariff_side(catalogue, tariff, url_key, rates_key)))
        text += "<td>{}</td>\n".format(self._compare_money(summary.get("cost_with_predbat_p")))
        text += "<td>{}</td>\n".format(self._compare_money(summary.get("saving_vs_none_p")))
        payback_years = summary.get("payback_years") or {}
        payback_reason = summary.get("payback_reason")
        for key in ["pv_only", "pv_battery", "pv_battery_predbat"]:
            text += self._render_payback_cell(payback_years, payback_reason, key)
        # Its own little form: a button cannot post on its own, and wrapping the whole
        # table in one form would make every row's button submit the same run id.
        # Confirms first - a deleted run cannot be recovered, only re-run.
        text += '<td><form action="./annual_delete" method="post" class="annual-delete">'
        text += '<input type="hidden" name="run" value="{}">'.format(html.escape(run_id, quote=True))
        text += '<button type="submit" class="annual-secondary" onclick="return confirm(\'Delete this run? It cannot be recovered - you would have to run the simulation again.\');">Delete</button>'
        text += "</form></td>\n"
        text += "</tr>\n"
        return text

    @staticmethod
    def _compare_number(value, suffix):
        """Return a formatted numeric cell, or an em dash when the value is unknown.

        ``None`` means the figure could not be computed - rendered as a dash, never
        as ``0``, which would read as "a system with no panels" rather than "unknown".
        """
        if value is None:
            return "—"
        try:
            return html.escape("{:g}{}".format(float(value), suffix), quote=True)
        except (TypeError, ValueError):
            return "—"

    def _compare_money(self, pence):
        """Return a pence value formatted as pounds, or an em dash when it is unknown.

        A summary figure of ``None`` means the value is unknown, not that it is
        zero: rendering it via ``self._pounds`` would print "n/a" rather than the
        dash the rest of this table uses for "unknown", so ``None`` is intercepted
        here first.
        """
        if pence is None:
            return "—"
        return html.escape(self._pounds(pence), quote=True)

    @staticmethod
    def _render_payback_cell(payback_years, payback_reason, key):
        """Return one payback cell for ``key``, distinguishing "unavailable" from "never pays back".

        An empty ``payback_years`` means payback was never computed at all - the run
        covered less than a full year - rendered as a dash carrying ``payback_reason``
        as its title. A populated dict with this key set to ``None`` means the option
        WAS costed and genuinely never pays back: a materially different fact from
        "unavailable", so it is rendered in words rather than as the same dash.
        """
        if not payback_years:
            reason = html.escape(str(payback_reason or "Payback could not be calculated."), quote=True)
            return '<td class="annual-unavailable" title="{}">—</td>\n'.format(reason)
        years = payback_years.get(key)
        if years is None:
            return '<td class="annual-unavailable">does not pay back</td>\n'
        # A non-numeric years figure (a corrupt stored document, say) is unknown, not
        # "never pays back" - render the same dash used for a value that was never
        # computed, rather than letting "{:.1f}".format() raise and 500 the whole
        # compare page.
        if not isinstance(years, (int, float)):
            return '<td class="annual-unavailable">—</td>\n'
        return "<td>{}</td>\n".format(html.escape("{:.1f} years".format(years), quote=True))

    def render_nav(self, current):
        """Return the page title and tab strip, marking the current page and disabling the end arrows.

        The title lives here rather than in each of the three handlers so all three carry
        the same one - the nav is already the piece every page shares.
        """
        names = [name for name, _, _ in self.NAV_PAGES]
        position = names.index(current) if current in names else 0
        text = "<h1>What If Annual Prediction</h1>\n"
        text += '<div class="annual-nav">\n'
        previous = self.NAV_PAGES[position - 1][2] if position > 0 else None
        text += '<a class="annual-nav-arrow{}" href="{}">&#9664;</a>\n'.format("" if previous else " annual-nav-disabled", previous or "#")
        for name, label, href in self.NAV_PAGES:
            text += '<a class="annual-nav-tab{}" href="{}">{}</a>\n'.format(" annual-nav-current" if name == current else "", href, label)
        following = self.NAV_PAGES[position + 1][2] if position < len(self.NAV_PAGES) - 1 else None
        text += '<a class="annual-nav-arrow{}" href="{}">&#9654;</a>\n'.format("" if following else " annual-nav-disabled", following or "#")
        text += "</div>\n"
        return text

    def render_css(self):
        """Return the scoped styles for the tab."""
        return """<style>
.annual-form-wrap fieldset { border: 1px solid var(--md-border, #cbd5e1); margin-bottom: 1rem; padding: 0.75rem; }
.annual-form-wrap legend { font-weight: 600; }
/* Every fieldset insets its content by its own border plus 0.75rem of padding, but the
   Advanced block and the buttons are bare children of the form - so without this they
   hang to the left of everything above them, and the buttons sit flush against the
   bottom of the page with nothing under them. */
.annual-form-wrap > details, .annual-actions { padding: 0 0.75rem; }
/* The actions sit directly under the tabs, so the margin is below them, separating them
   from the fieldsets - not above, where it would push them away from the nav. */
.annual-actions { margin-bottom: 1rem; }
.annual-actions button { margin-right: 0.5rem; padding: 0.4rem 0.9rem; }
/* PADDING, not a margin on the buttons: a bottom margin on the last child collapses
   straight out through the container and produces no visible gap at all, which is why
   the buttons still sat hard against the bottom of the page. */
.annual-form-wrap, .annual-results, .annual-compare-scroll { padding-bottom: 3rem; }
.annual-field { margin: 0.35rem 0; }
.annual-field label { display: inline-block; min-width: 20rem; }
/* An Octopus rates URL runs to ~130 characters and an API key to ~32, so the default
   input width shows a keyhole view of both. These sit on their own line under the label
   rather than beside it, so they can use the full width without pushing the form wide;
   max-width keeps them inside the column on a narrow screen. */
.annual-field.annual-field-wide label { display: block; min-width: 0; margin-bottom: 0.15rem; }
.annual-field-wide input[type="text"] { width: 100%; max-width: 60rem; box-sizing: border-box; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.9rem; }
.annual-subgroup { margin-left: 1.5rem; }
/* Each array is its own block. Without this the Remove button of one array sat directly
   against the "Array N" heading of the next, reading as though it belonged to the array
   below it rather than the one above. */
.annual-array { padding-bottom: 0.75rem; margin-bottom: 1rem; border-bottom: 1px dashed var(--md-border, #cbd5e1); }
.annual-array:last-of-type { border-bottom: 0; margin-bottom: 0.5rem; }
.annual-array > strong { display: block; margin-bottom: 0.5rem; }
.annual-array button { margin-top: 0.75rem; }
.annual-note { font-size: 0.85rem; opacity: 0.8; }
/* Predbat's global stylesheet sets `p { white-space: nowrap }` (web_helper.py), which
   suits the short single-line paragraphs elsewhere but runs every sentence of prose on
   this tab straight off the right of the page. Re-enable wrapping for the tab's own
   paragraphs only - deliberately not by changing the global rule, which every other
   page depends on. Scoped to paragraphs and list items, so the compare table's own
   nowrap (which keeps its columns intact while it scrolls) is untouched.
   The max-width is for readability: prose set across an ultra-wide monitor is hard to
   track from the end of one line back to the start of the next. */
.annual-form-wrap p, .annual-results p, .annual-compare-scroll p, .annual-banner, .annual-note, .annual-error, .annual-caveats li { white-space: normal; }
.annual-banner, .annual-error, .annual-caveats li, .annual-form-wrap > p, .annual-results > p { max-width: 80ch; }
.annual-banner { border-left: 4px solid #D55E00; padding: 0.5rem 0.75rem; margin-bottom: 1rem; }
.annual-error { border-left: 4px solid #b00020; padding: 0.5rem 0.75rem; margin-bottom: 1rem; }
.annual-progress { margin: 1rem 0; }
.annual-bar { height: 1.25rem; border: 1px solid var(--md-border, #cbd5e1); }
.annual-bar-fill { height: 100%; background: #0072B2; width: 0%; }
/* The tab strip between the header and the page body. The current tab is visually
   distinct (bold, underlined) so a visitor can tell which of the three pages they are
   on without reading the URL. A disabled arrow - at either end of the strip, never in
   the middle - is faded AND pointer-events:none, so it cannot be clicked at all: merely
   fading it would still let "next" from the last page wrap back to the first, which
   reads as a misclick rather than a deliberate choice. */
.annual-nav { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 1rem; border-bottom: 1px solid var(--md-border, #cbd5e1); padding-bottom: 0.5rem; }
.annual-nav-tab { padding: 0.25rem 0.75rem; text-decoration: none; border-radius: 3px; }
.annual-nav-tab.annual-nav-current { font-weight: 600; text-decoration: underline; }
.annual-nav-arrow { text-decoration: none; padding: 0 0.5rem; }
.annual-nav-arrow.annual-nav-disabled { opacity: 0.35; pointer-events: none; }
/* Label column narrow and left-aligned, so the values line up and read as a list of
   settings rather than a data grid. */
.annual-details th { text-align: left; white-space: nowrap; padding-right: 1rem; font-weight: 600; }
.annual-caveats li { margin-bottom: 0.35rem; }
.annual-unavailable { opacity: 0.6; font-style: italic; }
.annual-synthesised-tag { font-size: 0.75rem; font-weight: 600; color: #D55E00; border: 1px solid #D55E00; border-radius: 3px; padding: 0.05rem 0.35rem; margin-left: 0.35rem; }
/* Thirteen columns is wide - the table scrolls within its own container instead of
   forcing the whole page wider, which would otherwise push the nav strip and
   everything else sideways on anything narrower than a desktop monitor. */
.annual-compare-scroll { overflow-x: auto; }
/* nowrap on the DATA only. Applied to the whole table it caught the headers too, and
   since a column can be no narrower than its widest unbreakable content, every column
   was held open at its heading set on one line - "PV + battery pays back in" is ~24
   characters propping open a column whose data reads "4.2 years". Letting the headings
   wrap onto two or three short lines lets each column shrink to the figures it holds,
   which is what keeps three tariff columns affordable. */
table.annual-compare td { white-space: nowrap; }
table.annual-compare th { white-space: normal; vertical-align: bottom; }
/* The headings are now the tall part of the table, so the padding that suited a
   single-line row is more than they need between columns. */
table.annual-compare th, table.annual-compare td { padding: 4px 8px 4px 4px; }
.annual-compare-current { font-weight: 600; }
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
function annualTariffChanged(field) {
  // Import and export are independent, so each select drives only its own URL box.
  var sides = {
    'tariff_import_id': ['tariff_import_url', 'tariff-custom-import'],
    'tariff_export_id': ['tariff_export_url', 'tariff-custom-export']
  };
  var side = sides[field];
  if (!side) { return; }
  var select = document.getElementById(field);
  var option = select.options[select.selectedIndex];
  // The URL box belongs to Custom alone - every other entry is taken from the catalogue
  // server-side, so showing an editable box for one would offer an edit that is then
  // ignored. It is still populated, so switching to Custom starts from the tariff you
  // were looking at rather than from nothing.
  document.getElementById(side[0]).value = option.getAttribute('data-url') || '';
  var custom = document.getElementById(side[1]);
  if (custom) { custom.style.display = (select.value === 'custom') ? 'block' : 'none'; }
}
function annualCancel() { fetch('./annual_cancel', {method: 'POST'}); }
function annualSolarModeChanged(index) {
  var mode = document.getElementById('solar_mode_' + index).value;
  document.getElementById('solar-kwp-row-' + index).style.display = (mode === 'panels') ? 'none' : 'block';
  document.getElementById('solar-panels-row-' + index).style.display = (mode === 'panels') ? 'block' : 'none';
  annualUpdateSolarTotal();
  // Switching kWp/panels changes the total, so the cost estimate has to follow.
  annualCostPreviewSoon();
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
function annualSolarTotalKwp() {
  var total = 0, index = 0;
  while (document.getElementById('solar_mode_' + index)) {
    if (document.getElementById('solar_mode_' + index).value === 'panels') {
      total += (parseFloat(document.getElementById('solar_panels_' + index).value) || 0) * (parseFloat(document.getElementById('solar_panel_watts_' + index).value) || 0) / 1000;
    } else {
      total += parseFloat(document.getElementById('solar_kwp_' + index).value) || 0;
    }
    index += 1;
  }
  return total;
}
function annualFieldValue(id) {
  var field = document.getElementById(id);
  return field ? (parseFloat(field.value) || 0) : 0;
}
// Asks the server for the estimate rather than recomputing the cost model here: the band
// interpolation, the minimum install price and the quote overrides live in annual_costs
// and must not be reimplemented in JavaScript, where the two copies would drift.
var annualCostTimer = null;
function annualCostPreview() {
  var note = document.getElementById('annual-cost-estimate');
  if (!note) { return; }
  var params = new URLSearchParams({
    total_kwp: annualSolarTotalKwp(),
    battery_kwh: annualFieldValue('battery_size_kwh'),
    quoted_pv_gbp: annualFieldValue('cost_quoted_pv_gbp'),
    quoted_total_gbp: annualFieldValue('cost_quoted_total_gbp')
  });
  fetch('./annual_cost_preview?' + params.toString()).then(function (r) {
    if (!r.ok) { throw new Error('preview failed'); }
    return r.json();
  }).then(function (costs) {
    var money = function (value) { return '£' + Math.round(value).toLocaleString(); };
    var pv = costs.pv_gbp > 0 ? ('solar ' + money(costs.pv_gbp) + (costs.pv_quoted ? ' (quoted)' : (costs.pv_rate_gbp_per_kwp > 0 ? ' at ' + money(costs.pv_rate_gbp_per_kwp) + '/kWp' : ''))) : '';
    var bat = costs.battery_gbp > 0 ? ('battery ' + money(costs.battery_gbp) + (costs.battery_quoted ? ' (quoted)' : '')) : '';
    var parts = [pv, bat].filter(function (part) { return part; });
    note.textContent = costs.total_gbp > 0
      ? 'Estimated install cost ' + money(costs.total_gbp) + (parts.length ? ' — ' + parts.join(', ') : '')
      : 'No solar or battery configured, so there is nothing to cost.';
  }).catch(function () {
    note.textContent = 'Could not work out the install cost.';
  });
}
function annualCostPreviewSoon() {
  // Debounced: this fires on every keystroke in a size or quote field, and one request
  // per character would be wasteful for a figure nobody reads mid-word.
  if (annualCostTimer) { clearTimeout(annualCostTimer); }
  annualCostTimer = setTimeout(annualCostPreview, 300);
}
document.addEventListener('input', function (event) {
  if (!event.target || !event.target.id) { return; }
  if (/^solar_(kwp|panels|panel_watts)_/.test(event.target.id)) {
    annualUpdateSolarTotal();
    annualCostPreviewSoon();
  }
  if (event.target.id === 'battery_size_kwh' || event.target.id === 'cost_quoted_pv_gbp' || event.target.id === 'cost_quoted_total_gbp') {
    annualCostPreviewSoon();
  }
});
annualUpdateSolarTotal();
annualCostPreview();
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
        // The tab that started the run goes straight to the viewer - ./annual_view with
        // no run parameter selects the newest stored run, which is the one that just
        // finished. Every OTHER tab only gets the link: this poll fires in all of them,
        // including ones mid-edit on a form for a run they never started, and a forced
        // reload there would silently drop whatever the user had typed.
        //
        // Navigating is safe from the redirect loop this once had because the server
        // hands out 'complete' exactly once (_consume_terminal_state), so the reloaded
        // page's own first poll sees idle rather than 'complete' again.
        if (annualStartedHere()) {
          document.getElementById('annual-progress-text').textContent = 'Run complete — loading results…';
          window.location.href = './annual_view';
          return;
        }
        document.getElementById('annual-progress-text').innerHTML = 'Run complete — <a href="./annual_view">view results</a>';
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
