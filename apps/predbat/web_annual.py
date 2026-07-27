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
import datetime
import html
import os
import sys

import yaml
from aiohttp import web

from annual import AnnualConfigError, validate_config
from annual_job import AnnualJob
from annual_store import list_runs, load_run, save_run
from tariff_catalogue import CUSTOM_ID, merged_catalogue

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
        self.job = AnnualJob(log=self.log)
        # Deliberately NOT storing a "last validation error" on self: this page object
        # is shared by every request from every visitor, so an attribute here would
        # leak one person's failed validation onto everyone else's later, unrelated
        # page load (see html_annual's `error` parameter instead).

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

    def _text_field(self, name, label, value):
        """Return one labelled text input row.

        ``value`` is HTML-escaped before interpolation - it is user-controlled
        (a postcode, an Octopus API key or account id, a tariff URL, a DNO region).
        """
        return '<div class="annual-field"><label for="{name}">{label}</label><input type="text" id="{name}" name="{name}" value="{value}"></div>\n'.format(name=name, label=label, value=html.escape(str(value), quote=True) if value is not None else "")

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
            text += '<div class="annual-array"><strong>Array {}</strong>\n'.format(index + 1)
            text += self._number_field("solar_kwp_{}".format(index), "Peak power", array.get("kwp"), suffix="kWp")
            text += self._number_field("solar_declination_{}".format(index), "Pitch", array.get("declination", 35), suffix="degrees")
            text += self._number_field("solar_azimuth_{}".format(index), "Azimuth (180 = south)", array.get("azimuth", 180), suffix="degrees")
            text += "</div>\n"
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
        text += self._text_field("load_octopus_api_key", "Octopus API key", (load.get("octopus") or {}).get("api_key", ""))
        text += self._text_field("load_octopus_account_id", "Account ID", (load.get("octopus") or {}).get("account_id", ""))
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
        text += self._text_field("tariff_import_url", "Import rates URL", tariff.get("import_octopus_url", ""))
        text += self._text_field("tariff_export_url", "Export rates URL", tariff.get("export_octopus_url", ""))
        text += self._text_field("tariff_dno_region", "Octopus region letter", tariff.get("dno_region", ""))
        text += self._number_field("tariff_standing_charge", "Standing charge", tariff.get("standing_charge_p_per_day", 60.0), suffix="p/day")
        text += "</fieldset>\n"

        text += "<details><summary>Advanced</summary>\n"
        text += self._number_field("year", "Year to model (blank for the most recent complete year)", config.get("year"))
        text += self._number_field("samples_per_month", "Days sampled per month", config.get("samples_per_month", 2), step="1")
        text += self._number_field("pv10_derate_fallback", "P10 fallback derate", config.get("pv10_derate_fallback", 0.7))
        for index, array in enumerate(solar):
            text += self._number_field("solar_efficiency_{}".format(index), "Array {} efficiency".format(index + 1), array.get("efficiency", 0.95))
        text += "</details>\n"

        text += '<button type="submit" id="annual-run-button">Run</button>\n'
        # A second submit button pointed at the plain POST /annual handler via
        # formaction - the same fields, saved without starting a run.
        text += '<button type="submit" formaction="./annual" formmethod="post">Save</button>\n'
        text += "</form>\n</div>\n"
        return text

    def config_from_post(self, postdata):
        """Rebuild a config dict from submitted form fields.

        Values are left as the strings the browser sent; validate_config() in the
        engine does the coercion and range checking, so there is exactly one place
        that decides what a valid number is.
        """

        def value(name, default=None):
            """Return one posted field, or the default when absent or blank."""
            raw = postdata.get(name)
            if raw is None or str(raw).strip() == "":
                return default
            return str(raw).strip()

        config = {}

        location = {}
        if value("postcode"):
            location["postcode"] = value("postcode")
        if value("latitude") is not None and value("longitude") is not None:
            location["latitude"] = value("latitude")
            location["longitude"] = value("longitude")
        config["location"] = location

        arrays = []
        index = 0
        while value("solar_kwp_{}".format(index)) is not None:
            arrays.append(
                {
                    "kwp": value("solar_kwp_{}".format(index)),
                    "declination": value("solar_declination_{}".format(index), 35),
                    "azimuth": value("solar_azimuth_{}".format(index), 180),
                    "efficiency": value("solar_efficiency_{}".format(index), 0.95),
                }
            )
            index += 1
        if arrays:
            config["solar"] = arrays

        if value("battery_size_kwh") is not None:
            config["battery"] = {
                "size_kwh": value("battery_size_kwh"),
                "inverter_kw": value("battery_inverter_kw", 5.0),
                "export_limit_kw": value("battery_export_limit_kw", 5.0),
                "hybrid": bool(postdata.get("battery_hybrid")),
            }

        # The engine rejects an Octopus block alongside manual figures, because the
        # meter series already contains any car charging. Send one or the other.
        if value("load_source", "manual") == "octopus":
            config["load"] = {"octopus": {"api_key": value("load_octopus_api_key", ""), "account_id": value("load_octopus_account_id", "")}}
        else:
            config["load"] = {
                "annual_kwh": value("load_annual_kwh", 3800),
                "shape": value("load_shape", "flat"),
                "car_charging_kwh": value("load_car_charging_kwh", 0),
                "car_rate_kw": value("load_car_rate_kw", 7.4),
            }

        tariff = {"standing_charge_p_per_day": value("tariff_standing_charge", 0)}
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
            config["year"] = value("year")
        config["samples_per_month"] = value("samples_per_month", 2)
        if value("pv10_derate_fallback"):
            config["pv10_derate_fallback"] = value("pv10_derate_fallback")

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
        """
        status = self.job.status()
        if status["state"] not in ("complete", "failed", "cancelled"):
            return None
        results = self.job.results
        self.job.results = None
        self.job.state = "idle"
        self.job.error = None
        if status["state"] == "complete" and results is not None:
            config = getattr(self, "_running_config", None) or self.load_config()
            await self._store_completed_run(config, results)
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

    async def html_annual(self, request, error=None):
        """Render the Annual tab: the form, then the selected run's results.

        ``error`` is a validation message threaded through from a POST handler in
        the same request-response cycle, not read off ``self`` - the page object is
        shared by every visitor, so storing it as instance state would leak one
        person's failed validation onto everyone else's later, unrelated page load.
        """
        self.web.default_page = "./annual"
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
        return await self.html_annual(request, error=error)

    async def html_annual_run(self, request):
        """Validate, save, and spawn the run."""
        postdata = await request.post()
        config = self.config_from_post(postdata)
        error = self.validation_error(config)
        if error:
            return await self.html_annual(request, error=error)

        self.save_config(config)
        self._running_config = config
        started = await self.job.start(self.cli_command(self._config_path()))
        if not started and self.job.state != "running":
            error = self.job.status().get("error") or "The run could not be started"
        return await self.html_annual(request, error=error)

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

    def render_results(self, results, runs, selected_id):
        """Placeholder replaced in full by the results task; keeps the page renderable."""
        return "<p>No results yet — fill in the form above and press Run.</p>\n"

    def render_css(self):
        """Return the scoped styles for the tab."""
        return """<style>
.annual-form-wrap fieldset { border: 1px solid var(--md-border, #cbd5e1); margin-bottom: 1rem; padding: 0.75rem; }
.annual-form-wrap legend { font-weight: 600; }
.annual-field { margin: 0.35rem 0; }
.annual-field label { display: inline-block; min-width: 20rem; }
.annual-subgroup { margin-left: 1.5rem; }
.annual-note { font-size: 0.85rem; opacity: 0.8; }
.annual-banner { border-left: 4px solid #D55E00; padding: 0.5rem 0.75rem; margin-bottom: 1rem; }
.annual-error { border-left: 4px solid #b00020; padding: 0.5rem 0.75rem; margin-bottom: 1rem; }
.annual-progress { margin: 1rem 0; }
.annual-bar { height: 1.25rem; border: 1px solid var(--md-border, #cbd5e1); }
.annual-bar-fill { height: 100%; background: #0072B2; width: 0%; }
.annual-caveats li { margin-bottom: 0.35rem; }
.annual-unavailable { opacity: 0.6; font-style: italic; }
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
        // Deliberately not navigating: this poll fires on every open tab, including
        // ones mid-edit on the form for a run they never started, and a forced
        // reload would silently drop whatever they had typed. The user chooses
        // when to give up their own edits by following the link themselves.
        box.style.display = 'block';
        document.getElementById('annual-progress-text').innerHTML = 'Run complete — <a href="./annual">view results</a>';
        return;
      }
      if (s.state === 'failed' || s.state === 'cancelled') {
        box.style.display = 'block';
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
