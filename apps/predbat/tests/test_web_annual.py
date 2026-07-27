# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the Annual tab's prefill and configuration handling."""

import asyncio
import builtins
import copy
import json
import re
from unittest.mock import patch

from aiohttp import web as aiohttp_web

from annual import validate_config
from annual_store import list_runs, save_run
from tariff_catalogue import CUSTOM_ID
from web import WebInterface
from web_annual import DEFAULT_CONFIG, AnnualPage, _json_for_script


class FakeRequest:
    """A minimal aiohttp-request stand-in exposing only what the handlers read."""

    def __init__(self, postdata=None, query=None):
        """Store the posted fields and query string a handler will read."""
        self._postdata = postdata or {}
        self.query = query or {}

    async def post(self):
        """Return the posted fields, mimicking aiohttp's async ``Request.post()``."""
        return self._postdata


class RaceStorage:
    """A Storage stand-in whose ``save()`` yields once, forcing concurrent callers to interleave.

    A fake with no yield point at all would hide a race that only manifests once two
    callers are genuinely interleaved by the event loop - real storage backends do
    real I/O and therefore always yield somewhere.
    """

    def __init__(self):
        """Start with nothing stored and no calls recorded."""
        self.store = {}
        self.save_calls = []

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Record the call, yield control once, then write."""
        self.save_calls.append((module, filename))
        await asyncio.sleep(0)
        self.store[(module, filename)] = data

    async def load(self, module, filename):
        """Return a stored value, or None."""
        return self.store.get((module, filename))


def make_page(my_predbat):
    """Return an AnnualPage backed by a WebInterface over the test fixture."""
    return AnnualPage(WebInterface(my_predbat, web_port=5054))


def option_tag(html_text, value):
    """Return the ``<option>`` tag whose ``value`` attribute equals ``value``, or None."""
    match = re.search(r'<option value="{}"[^>]*>'.format(re.escape(value)), html_text)
    return match.group(0) if match else None


class RaisingStorage:
    """A Storage stand-in whose ``save()`` always raises, for exercising failure handling.

    Real storage backends do real I/O and can genuinely fail - a full disk, a
    Predbat.com backend outage - so the web layer that sits on top of Storage must
    survive that, not just the happy path every other fake in this module models.
    """

    def __init__(self):
        """Start with nothing stored."""
        self.store = {}

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Always fail, as if the backend were unavailable."""
        raise RuntimeError("storage backend unavailable")

    async def load(self, module, filename):
        """Return a stored value, or None. Never actually populated, since save() always raises."""
        return self.store.get((module, filename))


def valid_postdata():
    """Return a complete, valid postdata dict, exactly as a browser would submit it.

    Every value is a plain string - as aiohttp's ``request.post()`` always returns -
    so a test built on this dict exercises the same string-only shape ``config_from_post``
    sees in production, not a hand-built config that happens to already be numeric.
    """
    return {
        "postcode": "SW1A 1AA",
        "solar_kwp_0": "5.6",
        "solar_declination_0": "35",
        "solar_azimuth_0": "180",
        "solar_efficiency_0": "0.95",
        "battery_size_kwh": "9.5",
        "battery_inverter_kw": "5.0",
        "battery_export_limit_kw": "5.0",
        "battery_hybrid": "on",
        "load_source": "manual",
        "load_annual_kwh": "3800",
        "load_shape": "flat",
        "load_car_charging_kwh": "2500",
        "load_car_rate_kw": "7.4",
        "tariff_id": CUSTOM_ID,
        "tariff_import_url": "https://example.com/import/",
        "tariff_export_url": "https://example.com/export/",
        "tariff_standing_charge": "60.0",
        "samples_per_month": "2",
    }


def test_web_annual(my_predbat):
    """Verify prefill against a configured and an unconfigured instance."""
    failed = False
    print("**** Testing web_annual prefill ****")

    saved_args = dict(my_predbat.args)
    try:
        print("Test: an unconfigured instance still produces a complete, valid config")
        # This is the acceptance criterion for "must work with Predbat unconfigured":
        # a prospective buyer, and eventually an unregistered Predbat.com visitor,
        # arrives with none of this set.
        for key in ["soc_max", "inverter_limit", "export_limit", "open_meteo_forecast", "forecast_solar", "compare_list", "dno_region"]:
            my_predbat.args.pop(key, None)
        page = make_page(my_predbat)
        config = page.prefill_config()
        try:
            validate_config(config)
        except Exception as error:
            print("  ERROR: an unconfigured prefill must still validate, got {}".format(error))
            failed = True
        if page.is_configured():
            print("  ERROR: with no battery and no solar the page should report unconfigured")
            failed = True
        if config["battery"]["size_kwh"] != DEFAULT_CONFIG["battery"]["size_kwh"]:
            print("  ERROR: battery should fall back to the default, got {}".format(config["battery"]))
            failed = True
        if not config["solar"]:
            print("  ERROR: solar should fall back to the default array")
            failed = True

        print("Test: prefill_config() never reads apps.yaml (or anything else) from disk")
        # The unconfigured case above is exactly where this matters: apps.yaml may not
        # exist at all. Tracks every open() call made during prefill_config() and asserts
        # none of them targets apps.yaml, rather than relying on inspection alone.
        with patch("builtins.open", wraps=builtins.open) as mock_open:
            make_page(my_predbat).prefill_config()
        opened_paths = [call.args[0] for call in mock_open.call_args_list]
        if any(str(path).endswith("apps.yaml") for path in opened_paths):
            print("  ERROR: prefill_config() must never read apps.yaml from disk, opened {}".format(opened_paths))
            failed = True

        print("Test: a zero soc_max counts as unset and falls back to the default")
        my_predbat.args["soc_max"] = 0
        config = make_page(my_predbat).prefill_config()
        if config["battery"]["size_kwh"] != DEFAULT_CONFIG["battery"]["size_kwh"]:
            print("  ERROR: a zero soc_max should fall back, got {}".format(config["battery"]["size_kwh"]))
            failed = True

        print("Test: a multi-inverter soc_max list is summed rather than treated as absent")
        # soc_max is a sensor_list in APPS_SCHEMA - a real multi-inverter system holds a
        # list here, not a scalar, and the annual model wants one total usable capacity.
        my_predbat.args["soc_max"] = [6.0, 6.5]
        page = make_page(my_predbat)
        config = page.prefill_config()
        if config["battery"]["size_kwh"] != 12.5:
            print("  ERROR: a multi-inverter soc_max should be summed to a total capacity, got {}".format(config["battery"]["size_kwh"]))
            failed = True
        if not page.is_configured():
            print("  ERROR: a multi-inverter battery should count as configured")
            failed = True

        print("Test: configured values are read from args and used")
        my_predbat.args["soc_max"] = 12.5
        my_predbat.args["open_meteo_forecast"] = [{"kwp": 7.2, "declination": 30, "azimuth": 170, "efficiency": 0.9}]
        config = make_page(my_predbat).prefill_config()
        if config["battery"]["size_kwh"] != 12.5:
            print("  ERROR: soc_max from args should be used, got {}".format(config["battery"]["size_kwh"]))
            failed = True
        if config["solar"][0]["kwp"] != 7.2 or config["solar"][0]["azimuth"] != 170:
            print("  ERROR: the solar array should come from args, got {}".format(config["solar"]))
            failed = True

        print("Test: prefill is per-field, not all-or-nothing")
        # Solar configured but no battery: the real array must survive alongside the
        # default battery rather than the whole prefill collapsing to defaults.
        my_predbat.args.pop("soc_max", None)
        config = make_page(my_predbat).prefill_config()
        if config["solar"][0]["kwp"] != 7.2:
            print("  ERROR: configured solar should survive an absent battery, got {}".format(config["solar"]))
            failed = True
        if config["battery"]["size_kwh"] != DEFAULT_CONFIG["battery"]["size_kwh"]:
            print("  ERROR: the battery should still fall back, got {}".format(config["battery"]))
            failed = True
        if not make_page(my_predbat).is_configured():
            print("  ERROR: a configured solar array alone should count as configured")
            failed = True

        print("Test: an Octopus tariff URL survives prefill (a dotted URL must not be read as an entity id)")
        # get_arg()'s default indirect=True treats any dotted string as a Home Assistant
        # entity id to resolve; a URL is full of dots, so this only passes if the URL
        # fields are read with indirect=False.
        import_url = "https://api.octopus.energy/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-A/standard-unit-rates/"
        export_url = "https://api.octopus.energy/v1/products/AGILE-OUTGOING-19-05-13/electricity-tariffs/E-1R-AGILE-OUTGOING-19-05-13-A/standard-unit-rates/"
        my_predbat.args["rates_import_octopus_url"] = import_url
        my_predbat.args["rates_export_octopus_url"] = export_url
        config = make_page(my_predbat).prefill_config()
        if config["tariff"].get("import_octopus_url") != import_url:
            print("  ERROR: the Octopus import URL should survive into tariff.import_octopus_url, got {}".format(config["tariff"]))
            failed = True
        if config["tariff"].get("export_octopus_url") != export_url:
            print("  ERROR: the Octopus export URL should survive into tariff.export_octopus_url, got {}".format(config["tariff"]))
            failed = True
        my_predbat.args.pop("rates_import_octopus_url", None)
        my_predbat.args.pop("rates_export_octopus_url", None)

        print("Test: the catalogue merges the user's compare_list")
        my_predbat.args["compare_list"] = [{"id": "mine", "name": "My tariff", "rates_import_octopus_url": "https://example.com/x"}]
        ids = [entry["id"] for entry in make_page(my_predbat).catalogue()]
        if "mine" not in ids:
            print("  ERROR: a user compare_list entry should appear in the catalogue, got {}".format(ids))
            failed = True
        if "agile_agile" not in ids:
            print("  ERROR: built-in entries should still be present, got {}".format(ids))
            failed = True

        print("Test: the default config validates on its own")
        try:
            validate_config(DEFAULT_CONFIG)
        except Exception as error:
            print("  ERROR: DEFAULT_CONFIG must be valid, got {}".format(error))
            failed = True

    finally:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)

    return failed


def test_web_annual_routes(my_predbat):
    """Verify the run command, validation gating and the status payload."""
    import asyncio

    failed = False
    print("**** Testing web_annual routes ****")

    page = make_page(my_predbat)

    print("Test: the CLI command targets annual_cli.py in machine mode")
    command = page.cli_command("/tmp/annual.yaml")
    if "--machine" not in command:
        print("  ERROR: the child must be run in machine mode, got {}".format(command))
        failed = True
    if not any("annual_cli.py" in part for part in command):
        print("  ERROR: the command should invoke annual_cli.py, got {}".format(command))
        failed = True
    if "--config" not in command or "/tmp/annual.yaml" not in command:
        print("  ERROR: the config path should be passed, got {}".format(command))
        failed = True

    print("Test: the status payload is JSON-serialisable and names its state")
    status = asyncio.run(page.status_payload())
    for key in ["state", "completed", "total", "message", "elapsed"]:
        if key not in status:
            print("  ERROR: status is missing '{}', got {}".format(key, status))
            failed = True
    if status.get("state") != "idle":
        print("  ERROR: a fresh page should report idle, got {}".format(status.get("state")))
        failed = True

    print("Test: an invalid config is rejected before anything is spawned")
    bad = {"location": {}, "load": {"annual_kwh": 1}, "tariff": {"rates_import": [{"rate": 5}]}}
    error = page.validation_error(bad)
    if not error:
        print("  ERROR: an invalid config should produce an error message")
        failed = True
    if page.job.state != "idle":
        print("  ERROR: validation must not start a job, state was {}".format(page.job.state))
        failed = True

    print("Test: a valid config produces no error")
    if page.validation_error(page.prefill_config()):
        print("  ERROR: the prefill config should validate, got {}".format(page.validation_error(page.prefill_config())))
        failed = True

    return failed


def test_web_annual_form(my_predbat):
    """Verify the form renders every group, reflects config, and round-trips a post."""
    failed = False
    print("**** Testing web_annual form ****")

    saved_args = dict(my_predbat.args)
    try:
        # compare_list is also cleared: the coverage/apps.yaml test fixture ships an
        # active demo compare_list whose entries share ids with several built-ins (see
        # test_web_annual's own "catalogue merges the user's compare_list" case below)
        # and, by the documented "a user entry replaces a built-in of the same id" rule,
        # would otherwise silently substitute their names for the ones this test checks.
        for key in ["soc_max", "open_meteo_forecast", "forecast_solar", "compare_list"]:
            my_predbat.args.pop(key, None)
        page = make_page(my_predbat)
        config = page.prefill_config()
        html = page.render_form(config)

        print("Test: every configuration group is present")
        for heading in ["Location", "Solar", "Battery", "Load", "Tariff", "Advanced"]:
            if heading not in html:
                print("  ERROR: the form is missing the '{}' group".format(heading))
                failed = True

        print("Test: an unconfigured instance gets the example-values banner")
        if "example values" not in html.lower():
            print("  ERROR: an unconfigured instance should be told these are examples")
            failed = True

        print("Test: a configured instance does NOT get the banner")
        my_predbat.args["soc_max"] = 10.0
        configured_html = make_page(my_predbat).render_form(make_page(my_predbat).prefill_config())
        if "example values" in configured_html.lower():
            print("  ERROR: a configured instance should not be told its values are examples")
            failed = True
        my_predbat.args.pop("soc_max", None)

        print("Test: the tariff dropdown lists the catalogue and a Custom entry")
        if CUSTOM_ID not in html:
            print("  ERROR: the dropdown should offer a Custom entry")
            failed = True
        if "Agile import / Agile export" not in html:
            print("  ERROR: the dropdown should list the built-in tariffs")
            failed = True

        print("Test: the load source is a radio pair, not two independent sections")
        if html.count('type="radio"') < 2:
            print("  ERROR: expected a radio pair for the load source")
            failed = True
        if "octopus" not in html.lower():
            print("  ERROR: the Octopus load option should be offered")
            failed = True

        print("Test: current values are rendered into the inputs")
        if 'value="3800"' not in html.replace("'", '"'):
            print("  ERROR: the annual kWh value should appear in the form")
            failed = True

        print("Test: a configured Octopus key and account prefill, select the Octopus source, and still validate")
        # _validate_load treats octopus and annual_kwh as mutually exclusive, so a prefill
        # that merely ADDED the octopus block to the default load would render fine and
        # then fail the moment it was run. Validating here is the assertion that matters.
        my_predbat.args["octopus_api_key"] = "sk_live_exampleKey123"
        my_predbat.args["octopus_api_account"] = "A-1234ABCD"
        octopus_page = make_page(my_predbat)
        octopus_config = octopus_page.prefill_config()
        if (octopus_config.get("load") or {}).get("octopus", {}).get("api_key") != "sk_live_exampleKey123":
            print("  ERROR: a configured Octopus API key should prefill, got {}".format(octopus_config.get("load")))
            failed = True
        if (octopus_config.get("load") or {}).get("octopus", {}).get("account_id") != "A-1234ABCD":
            print("  ERROR: a configured Octopus account should prefill, got {}".format(octopus_config.get("load")))
            failed = True
        if "annual_kwh" in (octopus_config.get("load") or {}):
            print("  ERROR: the prefilled octopus load must not also carry annual_kwh - they are mutually exclusive")
            failed = True
        try:
            validate_config(octopus_config)
        except Exception as error:
            print("  ERROR: an Octopus-prefilled config must validate, got {}".format(error))
            failed = True
        octopus_form = octopus_page.render_form(octopus_config)
        if not re.search(r'value="octopus"[^>]*checked', octopus_form):
            print("  ERROR: the Octopus radio should be selected when a key and account are configured")
            failed = True
        # The manual inputs must still show usable values, so switching the radio back
        # does not present an empty form.
        if 'id="load_annual_kwh"' not in octopus_form or 'value=""' in octopus_form.split('id="load_annual_kwh"')[1][:80]:
            print("  ERROR: the manual consumption field should still show a default value")
            failed = True

        print("Test: a key without an account (or the reverse) does not select the Octopus source")
        # An incomplete pair cannot download anything, so offering it would only produce
        # a run that fails partway through.
        my_predbat.args["octopus_api_key"] = "sk_live_exampleKey123"
        my_predbat.args.pop("octopus_api_account", None)
        partial_config = make_page(my_predbat).prefill_config()
        if "octopus" in (partial_config.get("load") or {}):
            print("  ERROR: an API key with no account must not select the Octopus source")
            failed = True
        my_predbat.args.pop("octopus_api_key", None)

        print("Test: the long free-text fields are rendered wide")
        # An Octopus rates URL is ~130 characters; at the default input width it is
        # unreadable without scrolling inside the box.
        wide_form = make_page(my_predbat).render_form(make_page(my_predbat).prefill_config())
        for field in ["load_octopus_api_key", "load_octopus_account_id", "tariff_import_url", "tariff_export_url"]:
            row = re.search(r'<div class="annual-field[^"]*">\s*<label for="{}".*?</div>'.format(field), wide_form, re.S)
            if not row or "annual-field-wide" not in row.group(0):
                print("  ERROR: {} should be rendered as a wide field".format(field))
                failed = True

        print("Test: the two submit buttons say what they do, and only the run button marks the tab")
        # "Save" next to results that save themselves reads as though it saves the run;
        # it only ever saved the form. The labels have to distinguish the two actions.
        form = page.render_form(page.prefill_config())
        if "Run simulations" not in form or "Save settings" not in form:
            print("  ERROR: expected 'Run simulations' and 'Save settings' buttons")
            failed = True
        if "annualMarkStarted()" not in form:
            print("  ERROR: the run button must mark this tab as the one that started the run")
            failed = True
        # The save button must NOT mark the tab: it starts no run, so a later completion
        # elsewhere must not drag this tab off its form.
        save_button = re.search(r"<button[^>]*formaction[^>]*>Save settings</button>", form)
        if not save_button or "annualMarkStarted" in save_button.group(0):
            print("  ERROR: the save button must not mark the tab as having started a run")
            failed = True

        print("Test: only the tab that started a run auto-navigates to the results")
        # This previously shipped as an unconditional redirect on every 'complete' poll,
        # which turned every open tab into a reload loop. The guard is what keeps a tab
        # that is mid-edit on the form from being reloaded out from under the user.
        page_js = page.render_script()
        # Assert on the guarded navigation as a single unit. Checking merely that
        # "annualStartedHere()" appears somewhere would pass against an unconditional
        # redirect, since the helper is also defined and called in the failure branch -
        # verified by mutating the guard to `if (true)` and watching a looser version of
        # this test still pass.
        guarded_navigation = re.search(r"if \(annualStartedHere\(\)\) \{[^}]*window\.location\.href", page_js, re.S)
        if not guarded_navigation:
            print("  ERROR: navigation to the results must sit inside an annualStartedHere() guard, not fire unconditionally")
            failed = True
        # The failure branch must clear the flag, or a failed run leaves this tab primed
        # to jump to results it did not produce the next time any run completes.
        if not re.search(r"'failed'.*?annualStartedHere\(\)", page_js, re.S):
            print("  ERROR: a failed or cancelled run must clear the started-here flag")
            failed = True

        print("Test: the form offers a debug checkbox, defaulting off")
        form = page.render_form(page.prefill_config())
        if 'name="debug"' not in form:
            print("  ERROR: the form should offer a debug checkbox")
            failed = True
        if re.search(r'name="debug"[^>]*checked', form):
            print("  ERROR: debug must default to off")
            failed = True

        print("Test: a submitted debug checkbox becomes a true config flag")
        debug_postdata = valid_postdata()
        debug_postdata["debug"] = "on"
        debug_config = page.config_from_post(debug_postdata)
        if debug_config.get("debug") is not True:
            print("  ERROR: debug should be True when checked, got {!r}".format(debug_config.get("debug")))
            failed = True

        print("Test: an unchecked debug box is False, not absent")
        debug_config = page.config_from_post(valid_postdata())
        if debug_config.get("debug") is not False:
            print("  ERROR: debug should be False when unchecked, got {!r}".format(debug_config.get("debug")))
            failed = True

        print("Test: validation errors are shown with the form still populated")
        html_with_error = page.render_form(config, errors="annual.solar[0] is missing kwp")
        if "annual.solar[0] is missing kwp" not in html_with_error:
            print("  ERROR: the error message should be displayed")
            failed = True
        if 'value="3800"' not in html_with_error.replace("'", '"'):
            print("  ERROR: the form should stay populated when an error is shown")
            failed = True

        print("Test: config_from_post rebuilds a config the engine accepts")
        postdata = {
            "postcode": "SW1A 1AA",
            "solar_kwp_0": "5.6",
            "solar_declination_0": "35",
            "solar_azimuth_0": "180",
            "solar_efficiency_0": "0.95",
            "battery_size_kwh": "9.5",
            "battery_inverter_kw": "5.0",
            "battery_export_limit_kw": "5.0",
            "battery_hybrid": "on",
            "load_source": "manual",
            "load_annual_kwh": "3800",
            "load_shape": "flat",
            "load_car_charging_kwh": "2500",
            "load_car_rate_kw": "7.4",
            "tariff_id": CUSTOM_ID,
            "tariff_import_url": "https://example.com/import/",
            "tariff_export_url": "https://example.com/export/",
            "tariff_standing_charge": "60.0",
            "samples_per_month": "2",
        }
        rebuilt = page.config_from_post(postdata)
        try:
            # config_from_post deliberately leaves posted values as the strings the
            # browser sent - validate_config() is the one place that coerces and range
            # checks them - so the round trip is only meaningful once validated.
            validated = validate_config(rebuilt)
        except Exception as error:
            print("  ERROR: a posted form should rebuild into a valid config, got {}".format(error))
            failed = True
            validated = None
        if validated is not None and validated.get("load", {}).get("car_charging_kwh") != 2500:
            print("  ERROR: car charging should survive the round trip, got {}".format(rebuilt["load"]))
            failed = True

        print("Test: choosing the Octopus load source drops the manual figures")
        postdata["load_source"] = "octopus"
        postdata["load_octopus_api_key"] = "sk_test"
        postdata["load_octopus_account_id"] = "A-1234ABCD"
        rebuilt = page.config_from_post(postdata)
        if "annual_kwh" in rebuilt["load"] or "car_charging_kwh" in rebuilt["load"]:
            print("  ERROR: the manual figures must not be sent alongside Octopus, got {}".format(rebuilt["load"]))
            failed = True
        try:
            validate_config(rebuilt)
        except Exception as error:
            print("  ERROR: the Octopus form should rebuild into a valid config, got {}".format(error))
            failed = True

        print("Test: user-controlled values are HTML-escaped, not interpolated raw")
        # A postcode is free text a visitor types in; a stray '"' or '<' must not be able
        # to break out of the value="..." attribute and inject markup/JS into the page.
        hostile = 'SW1A" onload="x'
        hostile_config = copy.deepcopy(config)
        hostile_config["location"]["postcode"] = hostile
        hostile_html = page.render_form(hostile_config)
        if hostile in hostile_html:
            print("  ERROR: the raw, unescaped postcode must not appear in the rendered form")
            failed = True
        if "SW1A&quot; onload=&quot;x" not in hostile_html:
            print("  ERROR: the postcode should appear HTML-escaped in the rendered form")
            failed = True

        print("Test: the tariff dropdown keeps the current selection across a re-render")
        catalogue_entries = page.catalogue()
        built_in = next(entry for entry in catalogue_entries if entry["id"] != CUSTOM_ID and entry.get("import_octopus_url"))

        matched_config = copy.deepcopy(config)
        matched_config["tariff"] = {"import_octopus_url": built_in["import_octopus_url"], "standing_charge_p_per_day": 60.0}
        matched_html = page.render_form(matched_config)
        matched_tag = option_tag(matched_html, built_in["id"])
        if matched_tag is None or "selected" not in matched_tag:
            print("  ERROR: the option matching the config's import URL should be selected, got {}".format(matched_tag))
            failed = True
        custom_tag_when_matched = option_tag(matched_html, CUSTOM_ID)
        if custom_tag_when_matched is not None and "selected" in custom_tag_when_matched:
            print("  ERROR: Custom should not be selected when a built-in tariff matches")
            failed = True

        custom_config = copy.deepcopy(config)
        custom_config["tariff"] = {"import_octopus_url": "https://example.com/not-in-the-catalogue/", "standing_charge_p_per_day": 60.0}
        custom_html = page.render_form(custom_config)
        custom_tag = option_tag(custom_html, CUSTOM_ID)
        if custom_tag is None or "selected" not in custom_tag:
            print("  ERROR: a hand-entered URL with no catalogue match should select Custom, got {}".format(custom_tag))
            failed = True
        built_in_tag_when_custom = option_tag(custom_html, built_in["id"])
        if built_in_tag_when_custom is not None and "selected" in built_in_tag_when_custom:
            print("  ERROR: the built-in tariff should not stay selected once its URL no longer matches")
            failed = True

        print("Test: each array offers a kWp/panels mode toggle and a panel count")
        form = make_page(my_predbat).render_form(make_page(my_predbat).prefill_config())
        for field in ['name="solar_mode_0"', 'name="solar_panels_0"', 'name="solar_panel_watts_0"']:
            if field not in form:
                print("  ERROR: the solar fieldset should offer {}".format(field))
                failed = True

        print("Test: a running total is shown under the roof aspects")
        if 'id="annual-solar-total"' not in form:
            print("  ERROR: the solar fieldset should show a running kWp/panel total")
            failed = True

        print("Test: submitting panels produces a panels config, not a kwp one")
        postdata = valid_postdata()
        postdata["solar_mode_0"] = "panels"
        postdata["solar_panels_0"] = "13"
        postdata["solar_panel_watts_0"] = "400"
        config = make_page(my_predbat).config_from_post(postdata)
        array = config["solar"][0]
        if array.get("panels") != 13 or "kwp" in array:
            print("  ERROR: panel mode should send panels and NOT kwp - validate_config rejects both together. Got {}".format(array))
            failed = True
        validate_config(config)

        print("Test: submitting kWp produces a kwp config, not a panels one")
        postdata = valid_postdata()
        postdata["solar_mode_0"] = "kwp"
        config = make_page(my_predbat).config_from_post(postdata)
        array = config["solar"][0]
        if "panels" in array or not array.get("kwp"):
            print("  ERROR: kWp mode should send kwp and NOT panels. Got {}".format(array))
            failed = True
        validate_config(config)

        print("Test: the cost settings appear and round-trip")
        for key in ["cost_battery_install_gbp", "cost_battery_per_kwh_gbp", "cost_pv_minimum_gbp", "cost_predbat_annual_gbp"]:
            if 'name="{}"'.format(key) not in form:
                print("  ERROR: the form should offer {}".format(key))
                failed = True
        postdata = valid_postdata()
        postdata["cost_battery_per_kwh_gbp"] = "250"
        postdata["cost_predbat_annual_gbp"] = "100"
        config = make_page(my_predbat).config_from_post(postdata)
        if config.get("costs", {}).get("battery_per_kwh_gbp") != 250 or config.get("costs", {}).get("predbat_annual_gbp") != 100:
            print("  ERROR: submitted cost settings should reach the config, got {}".format(config.get("costs")))
            failed = True

        print("Test: the Predbat annual cost defaults to zero")
        if make_page(my_predbat).prefill_config().get("costs", {}).get("predbat_annual_gbp", 0) != 0:
            print("  ERROR: predbat_annual_gbp must default to 0 - Predbat is free when self-hosted")
            failed = True

    finally:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)

    return failed


def test_web_annual_terminal_state(my_predbat):
    """Verify a finished job is claimed exactly once and never re-reports as complete.

    Covers two review findings against the first cut of the routes: (1) a completed
    job stuck reporting "complete" forever, which drove the page's poll into
    redirecting on every subsequent load; (3) two overlapping polls both saving the
    same finished run because the results were read, then awaited on, then only
    cleared afterwards - leaving a window where a second poll could still see them.
    """
    failed = False
    print("**** Testing web_annual terminal-state handling ****")

    page = make_page(my_predbat)
    storage = RaceStorage()
    page._storage = lambda: storage
    page.job.state = "complete"
    page.job.results = {"year": 2025, "annual": {"months_included": 12}}
    # Seeded from config_from_post(), not prefill_config(): prefill_config() already
    # returns a fully numeric config, which is exactly why the original cut of this
    # test passed while every REAL web-initiated run - which always goes through
    # config_from_post() and therefore carries string values - crashed build_label()
    # with a TypeError before the run's index entry was ever written (see FIX 1).
    page._running_config = page.config_from_post(valid_postdata())

    async def poll_twice_concurrently():
        """Run two status_payload() polls concurrently on the same event loop."""
        return await asyncio.gather(page.status_payload(), page.status_payload())

    print("Test: two concurrent polls save the finished run exactly once")
    first, second = asyncio.run(poll_twice_concurrently())
    run_saves = [call for call in storage.save_calls if call[1].startswith("run_")]
    if len(run_saves) != 1:
        print("  ERROR: two concurrent polls should save the finished run exactly once, got {} save() calls: {}".format(len(run_saves), storage.save_calls))
        failed = True

    print("Test: the run built from a string-only, web-posted config stores successfully and lands in the index")
    # Before FIX 1, build_label() raised on the string 'kwp' here, which happened
    # AFTER save_run() had already written the run_<id> blob but BEFORE the index
    # entry referencing it - so the blob was orphaned and the page kept saying "No
    # results yet" no matter how long the run had taken.
    if first.get("state") == "failed" or second.get("state") == "failed":
        print("  ERROR: a web-posted config must not fail to store, got states {} / {}".format(first.get("state"), second.get("state")))
        failed = True
    index = asyncio.run(list_runs(storage))
    if not index:
        print("  ERROR: the finished run should have landed in the index, got an empty index")
        failed = True
    elif "9.5kWh battery" not in index[0].get("label", "") or "5.6kWp" not in index[0].get("label", ""):
        print("  ERROR: the indexed run's label should reflect the posted battery and solar size, got {!r}".format(index[0].get("label")))
        failed = True

    print("Test: exactly one of the two concurrent polls is told the run is 'complete'")
    states = sorted([first["state"], second["state"]])
    if states != ["complete", "idle"]:
        print("  ERROR: expected one 'complete' and one 'idle' from the two concurrent polls, got {}".format(states))
        failed = True

    print("Test: a later poll never re-reports 'complete', so the page cannot loop on it")
    third = asyncio.run(page.status_payload())
    if third["state"] == "complete":
        print("  ERROR: a subsequent poll must not still report 'complete', got {}".format(third))
        failed = True
    if page.job.state == "complete":
        print("  ERROR: the job itself must not remain 'complete' once claimed, got {}".format(page.job.state))
        failed = True

    return failed


def test_web_annual_error_isolation(my_predbat):
    """Verify a validation error from one POST does not leak into a later, unrelated GET.

    ``AnnualPage`` is one long-lived object shared by every request from every
    visitor; storing the error as an instance attribute would let one person's
    failed validation show up on someone else's later, unrelated page load.
    """
    failed = False
    print("**** Testing web_annual error isolation ****")

    page = make_page(my_predbat)

    print("Test: an invalid POST (no location) renders its own validation error")
    bad_postdata = {"battery_size_kwh": "9.5", "battery_inverter_kw": "5.0", "battery_export_limit_kw": "5.0"}
    response = asyncio.run(page.html_annual_post(FakeRequest(bad_postdata)))
    if "Could not run" not in response.text:
        print("  ERROR: an invalid POST should render its own validation error, got no banner in the response")
        failed = True

    print("Test: a later, unrelated GET does not show the previous request's error")
    later = asyncio.run(page.html_annual(FakeRequest()))
    if "Could not run" in later.text:
        print("  ERROR: a later GET must not show a previous request's validation error")
        failed = True

    return failed


def test_web_annual_validation_error_preserves_input(my_predbat):
    """Verify a validation failure re-renders what was POSTED, not the config on disk.

    Both html_annual_post and html_annual_run used to render their error via
    html_annual(request, error=...), which called self.load_config() and re-rendered
    from disk rather than from anything on this request - so a validation failure
    silently discarded everything the visitor had just typed and showed them the
    previous saved config (or the prefill) instead. render_form's own docstring, and
    the spec's failure table, both promise the form stays populated with what was
    entered.
    """
    failed = False
    print("**** Testing web_annual validation error preserves posted input ****")

    page = make_page(my_predbat)

    # No postcode/latitude/longitude, so validation fails on location - and a load
    # figure far from both DEFAULT_CONFIG's 3800 and anything already on disk, so
    # its presence in the response can only be explained by the posted value having
    # survived, not a coincidental match with some other config.
    bad_postdata = dict(valid_postdata())
    del bad_postdata["postcode"]
    bad_postdata["load_annual_kwh"] = "4321"

    print("Test: html_annual_post keeps the posted value on a validation failure")
    response = asyncio.run(page.html_annual_post(FakeRequest(bad_postdata)))
    if "Could not run" not in response.text:
        print("  ERROR: an invalid POST should render its own validation error")
        failed = True
    if 'value="4321"' not in response.text.replace("'", '"'):
        print("  ERROR: the posted load figure (4321) should still be in the form after a validation failure, it was discarded instead")
        failed = True

    print("Test: html_annual_run keeps the posted value on a validation failure too")
    response = asyncio.run(page.html_annual_run(FakeRequest(bad_postdata)))
    if "Could not run" not in response.text:
        print("  ERROR: an invalid run request should render its own validation error")
        failed = True
    if 'value="4321"' not in response.text.replace("'", '"'):
        print("  ERROR: the posted load figure (4321) should still be in the form after a failed run request, it was discarded instead")
        failed = True

    return failed


def test_web_annual_run_refuses_while_running(my_predbat):
    """Verify a second Run while one is in flight is refused BEFORE it can clobber the first.

    html_annual_run used to call save_config() and set self._running_config = config
    before job.start()'s own "already running" check could refuse it - so a second
    Run submitted while the first was still executing silently relabelled the
    in-flight run under the second request's battery size, array size and tariff
    once it finished, with `error` left at None so the page showed no indication
    anything had been refused.
    """
    failed = False
    print("**** Testing web_annual second-run refusal ****")

    page = make_page(my_predbat)
    page.job.state = "running"  # a run already in flight, without spawning a real child
    first_running_config = {"battery": {"size_kwh": 9.5}, "solar": [{"kwp": 5.6}], "tariff": {}}
    page._running_config = first_running_config

    save_calls = []
    page.save_config = lambda config: save_calls.append(config)

    other_postdata = dict(valid_postdata())
    other_postdata["battery_size_kwh"] = "99"
    other_postdata["solar_kwp_0"] = "77"

    response = asyncio.run(page.html_annual_run(FakeRequest(other_postdata)))

    print("Test: the second run is refused with a clear, visible message")
    if "already in progress" not in response.text.lower():
        print("  ERROR: refusing a second run should say so clearly, got no such message in the response")
        failed = True

    print("Test: the refused request's config is never saved")
    if save_calls:
        print("  ERROR: a refused second run must not save its config, got save_config() called with {}".format(save_calls))
        failed = True

    print("Test: the in-flight run's config is untouched by the refused second request")
    if page._running_config != first_running_config:
        print("  ERROR: a refused second run must not overwrite the in-flight run's config, got {}".format(page._running_config))
        failed = True

    print("Test: the job itself is left exactly as it was (still 'running')")
    if page.job.state != "running":
        print("  ERROR: a refused second run must not touch the in-flight job's state, got {}".format(page.job.state))
        failed = True

    return failed


def test_web_annual_store_failure_surfaces(my_predbat):
    """Verify a Storage failure while saving a finished run is caught and shown, not silent.

    _store_completed_run() used to be awaited AFTER the terminal state had already
    been claimed and job.results cleared, so any exception from it propagated
    straight out of status_payload(), 500ing the poller - which the page's own JS
    '.catch' turns into a silent 5-second retry that then reports 'idle', with the
    run's results gone for good and nothing on screen to say so.
    """
    failed = False
    print("**** Testing web_annual store-failure visibility ****")

    page = make_page(my_predbat)
    storage = RaisingStorage()
    page._storage = lambda: storage
    page.job.state = "complete"
    page.job.results = {"year": 2025, "annual": {"months_included": 12}}
    page._running_config = page.config_from_post(valid_postdata())

    print("Test: status_payload() does not raise when storage.save() fails")
    try:
        status = asyncio.run(page.status_payload())
    except Exception as error:
        print("  ERROR: a storage failure must not propagate out of status_payload(), got {}".format(error))
        return True

    print("Test: the failure is reported as 'failed' with an explanatory error, not silently as 'idle' or 'complete'")
    if status.get("state") != "failed":
        print("  ERROR: a storage failure while saving should be reported as 'failed', got {}".format(status.get("state")))
        failed = True
    if not status.get("error"):
        print("  ERROR: a storage failure should carry an explanatory error message, got none")
        failed = True

    print("Test: the job itself carries the same error, so a fresh page load also sees it once")
    if page.job.error != status.get("error"):
        print("  ERROR: the job's own error should match what this poll reported, got job.error={!r} vs status.error={!r}".format(page.job.error, status.get("error")))
        failed = True

    return failed


def sample_run_results():
    """Return a results document covering an ok, a degraded and an unavailable month."""
    scenarios = {
        "no_pvbat": {"cost_p": 18000.0, "import_kwh": 400.0, "export_kwh": 0.0, "pv_generated_kwh": 0.0, "battery_throughput_kwh": 0.0, "export_credit_p_estimate": 0.0},
        "pv_only": {"cost_p": 13000.0, "import_kwh": 340.0, "export_kwh": 70.0, "pv_generated_kwh": 120.0, "battery_throughput_kwh": 0.0, "export_credit_p_estimate": 210.0},
        "without_predbat": {"cost_p": 9000.0, "import_kwh": 300.0, "export_kwh": 20.0, "pv_generated_kwh": 120.0, "battery_throughput_kwh": 90.0, "export_credit_p_estimate": 300.0},
        "with_predbat": {"cost_p": 6600.0, "import_kwh": 280.0, "export_kwh": 145.0, "pv_generated_kwh": 120.0, "battery_throughput_kwh": 140.0, "export_credit_p_estimate": 675.0},
    }
    return {
        "year": 2025,
        "months": [
            {"month": 1, "status": "ok", "days": 31, "sampled_days": ["2025-01-08", "2025-01-24"], "standing_charge_p": 1860.0, "scenarios": scenarios},
            {"month": 2, "status": "degraded", "days": 28, "failed_days": ["2025-02-14"], "standing_charge_p": 1680.0, "scenarios": scenarios},
            {"month": 3, "status": "unavailable", "reason": "no rate data available", "days": 31, "standing_charge_p": 1860.0},
        ],
        "annual": {
            "scenarios": scenarios,
            "standing_charge_p": 3540.0,
            "savings": {"pv_battery_vs_none_p": 9000.0, "predbat_vs_baseline_p": 2400.0},
            "months_included": 2,
            "months_excluded": [3],
            "costs": {"pv_gbp": 8900.0, "battery_gbp": 3350.0, "total_gbp": 12250.0, "pv_rate_gbp_per_kwp": 1589.29, "total_kwp": 5.6, "battery_kwh": 9.5},
            "payback": {
                "available": True,
                "pv_only": {"capital_gbp": 8900.0, "gross_annual_saving_gbp": 500.0, "annual_saving_gbp": 500.0, "predbat_annual_gbp": 0.0, "pays_back": True, "years": 17.8},
                "pv_battery": {"capital_gbp": 12250.0, "gross_annual_saving_gbp": 900.0, "annual_saving_gbp": 900.0, "predbat_annual_gbp": 0.0, "pays_back": True, "years": 13.61},
                "pv_battery_predbat": {"capital_gbp": 12250.0, "gross_annual_saving_gbp": 1140.0, "annual_saving_gbp": 1040.0, "predbat_annual_gbp": 100.0, "pays_back": True, "years": 11.78},
            },
        },
        "caveats": ["An example caveat about the P10 fallback."],
    }


def test_web_annual_results(my_predbat):
    """Verify the results view: totals, chart series, month statuses, caveats, selector."""
    failed = False
    print("**** Testing web_annual results ****")

    page = make_page(my_predbat)
    runs = [{"id": "20260726-101500", "label": "9.5kWh battery · 5.6kWp · Agile", "months_included": 12}, {"id": "20260725-090000", "label": "no battery · 5.6kWp · Agile", "months_included": 12}]
    results = sample_run_results()
    html = page.render_results(results, runs, "20260726-101500")

    print("Test: the annual savings figures are shown")
    if "90.00" not in html:
        print("  ERROR: the PV/battery saving (9000p = £90.00) should be shown")
        failed = True
    if "24.00" not in html:
        print("  ERROR: the Predbat saving (2400p = £24.00) should be shown")
        failed = True

    print("Test: the validated colourblind-safe palette is used, not the house trio")
    for colour in ["#0072B2", "#D55E00", "#009E73"]:
        if colour not in html:
            print("  ERROR: expected the validated colour {} in the chart".format(colour))
            failed = True
    for banned in ["#4CAF50", "#FF9800", "#2196F3"]:
        if banned in html:
            print("  ERROR: {} fails CVD separation for this chart and must not be used".format(banned))
            failed = True

    print("Test: the PV-only scenario appears in the chart and the month table")
    # Scoped to _render_chart()/_render_month_table() directly, not the whole page: a
    # substring check over `html` would also be satisfied by the payback table's own
    # "PV only" label (see _render_payback), so it would stay green even if "pv_only"
    # were dropped from SCENARIO_ORDER and vanished from both the chart and the table.
    chart_html = page._render_chart(results)
    month_table_html = page._render_month_table(results)
    if "#9439ef" not in chart_html:
        print("  ERROR: the validated PV-only colour should be present in the chart")
        failed = True
    if "PV only" not in chart_html:
        print("  ERROR: the PV-only scenario should be labelled in the chart")
        failed = True
    if "PV only" not in month_table_html:
        print("  ERROR: the PV-only scenario should be labelled in the month table")
        failed = True

    print("Test: the payback table shows all three purchase options")
    # Scoped to _render_payback() directly: "PV only" and "With Predbat" are also
    # SCENARIO_LABELS values that appear in the chart/month table regardless of what
    # the payback table renders, so a whole-page substring check would stay green even
    # with a payback row missing. Only "PV + battery" happens to be unique to this
    # table, which is exactly the gap that let a dropped row ship silently.
    payback_html = page._render_payback(results)
    for label in ["PV only", "PV + battery", "With Predbat"]:
        if label not in payback_html:
            print("  ERROR: the payback table should include {}".format(label))
            failed = True

    print("Test: a non-paying-back option says so rather than showing a number")
    no_payback = copy.deepcopy(results)
    no_payback["annual"]["payback"]["pv_only"] = {"pays_back": False, "years": None, "capital_gbp": 8000.0, "annual_saving_gbp": -10.0, "gross_annual_saving_gbp": -10.0, "predbat_annual_gbp": 0.0}
    text = page.render_results(no_payback, runs, runs[0]["id"])
    if "does not pay back" not in text.lower():
        print("  ERROR: an option that never pays back should say so")
        failed = True

    print("Test: an unavailable payback shows its reason instead of a blank table")
    unavailable = copy.deepcopy(results)
    unavailable["annual"]["payback"] = {"available": False, "reason": "Payback needs a full year, but only 11 of 12 months could be modelled."}
    text = page.render_results(unavailable, runs, runs[0]["id"])
    if "11 of 12" not in text:
        print("  ERROR: the reason payback is unavailable should be shown")
        failed = True

    print("Test: an unavailable month is marked, never drawn as zero")
    if "unavailable" not in html.lower():
        print("  ERROR: the unavailable month should be marked as such")
        failed = True
    if "no rate data available" not in html:
        print("  ERROR: the reason for exclusion should be shown")
        failed = True

    print("Test: a degraded month is shown with its cost and flagged as partial")
    if "degraded" not in html.lower():
        print("  ERROR: the degraded month should be flagged")
        failed = True

    print("Test: months_included is stated so the annual figure's coverage is clear")
    if "2 of 12" not in html:
        print("  ERROR: the annual figure should say how many months it covers")
        failed = True

    print("Test: caveats are displayed, not buried in the JSON")
    if "An example caveat about the P10 fallback." not in html:
        print("  ERROR: caveats must be shown to the user")
        failed = True

    print("Test: self-consumption is not reported (the figure was unreliable under battery arbitrage, so it was dropped)")
    if "self-consumed" in html.lower() or "self_consumed" in html.lower():
        print("  ERROR: self-consumption must not be rendered")
        failed = True

    print("Test: the run selector lists every stored run and marks the selected one")
    for run in runs:
        if run["label"] not in html:
            print("  ERROR: run {} should appear in the selector".format(run["id"]))
            failed = True
    if "selected" not in html:
        print("  ERROR: the selected run should be marked in the dropdown")
        failed = True

    print("Test: a download link is offered for the selected run")
    if "annual_download?run=20260726-101500" not in html:
        print("  ERROR: the selected run should be downloadable as JSON")
        failed = True

    print("Test: a run with captured plans offers a plan viewer")
    debug_results = copy.deepcopy(sample_run_results())
    debug_results["months"][0]["plans"] = [{"day": "2025-01-15", "leg": "single", "scenarios": {"with_predbat": {"rows": [], "soc_max": 9.5}}}]
    html_text = page.render_results(debug_results, runs, runs[0]["id"])
    if "annual-plan-viewer" not in html_text:
        print("  ERROR: a run with plans should render the plan viewer")
        failed = True
    if "renderPlanTable" not in html_text:
        print("  ERROR: the viewer must use the existing plan renderer")
        failed = True

    print("Test: a run with no captured plans renders no viewer")
    html_text = page.render_results(sample_run_results(), runs, runs[0]["id"])
    if "annual-plan-viewer" in html_text:
        print("  ERROR: a non-debug run must not show an empty plan viewer")
        failed = True

    print("Test: with no runs at all the view says so rather than rendering an empty chart")
    empty = page.render_results(None, [], None)
    if "apexcharts" in empty.lower() and "series" in empty.lower():
        print("  ERROR: no chart should be drawn when there are no results")
        failed = True
    if "no results" not in empty.lower():
        print("  ERROR: the empty state should say there are no results yet")
        failed = True

    print("Test: a corrupt, non-dict run blob says so rather than 500ing on results.get(...)")
    corrupt = page.render_results("not actually a results document", runs, "20260726-101500")
    if "could not be read" not in corrupt.lower() and "missing" not in corrupt.lower():
        print("  ERROR: a corrupt (non-dict) run blob should render a readable-failure message, got {!r}".format(corrupt))
        failed = True

    print("Test: apexcharts is loaded once by get_header_html, not duplicated by the chart")
    # Checked narrowly for a <script src=...> tag, not any mention of "apexcharts" -
    # the chart's own `new ApexCharts(...)` constructor call is legitimate and must
    # stay; it is the second CDN <script> fetch that was the duplicate.
    if "<script src=" in page._render_chart(sample_run_results()).lower():
        print("  ERROR: _render_chart should not emit its own <script src=...apexcharts...> tag; get_header_html already loads it")
        failed = True

    print("Test: a month whose rates were synthesised from the current-rates fallback is marked in the table and the chart, not indistinguishable from a real month")
    synthesised_results = copy.deepcopy(sample_run_results())
    synthesised_results["months"][0]["rates_synthesised"] = ["export", "import"]
    synthesised_html = page.render_results(synthesised_results, runs, "20260726-101500")
    if "rates synthesised" not in synthesised_html.lower():
        print("  ERROR: expected a visible marker on the month row whose rates were synthesised")
        failed = True
    plain_chart = page._render_chart(sample_run_results())
    synthesised_chart = page._render_chart(synthesised_results)
    # "jan" alone is not a valid probe here: month 1 is "ok" in sample_run_results() and
    # so is always a chart category regardless of rates_synthesised, so a test that only
    # checked for "jan" would stay green even with the whole synthesised-note block
    # deleted. Assert the note's own wording instead, and that switching it on is what
    # actually changes the chart output (the plain chart must not already contain it).
    if "rates for jan were synthesised" not in synthesised_chart.lower():
        print("  ERROR: expected the chart's synthesised-rates note to name the affected month by its own wording, got {!r}".format(synthesised_chart))
        failed = True
    if "rates for jan were synthesised" in plain_chart.lower():
        print("  ERROR: the unmodified sample results must not already carry a synthesised-rates note (the positive assertion above would be vacuous otherwise)")
        failed = True

    print("Test: a month with no rates_synthesised entry (the normal case) shows no synthesised marker")
    if "rates synthesised" in page._render_month_table(sample_run_results()).lower():
        print("  ERROR: a month with real historical rates must not be marked as synthesised")
        failed = True
    if "annual-note" in plain_chart and "synthesised" in plain_chart.lower():
        print("  ERROR: the chart must not show a synthesised-rates note when no month used the fallback")
        failed = True

    print("Test: _json_for_script neutralises a literal </script> so it cannot close the surrounding tag early")
    dangerous = "run-id-</script><script>alert(1)</script>"
    escaped = _json_for_script(dangerous)
    if "</script>" in escaped:
        print("  ERROR: expected every '</script>' to be escaped, got {!r}".format(escaped))
        failed = True
    if "<\\/script>" not in escaped:
        print("  ERROR: expected the standard '<\\/script>' escape, got {!r}".format(escaped))
        failed = True
    if json.loads(escaped) != dangerous:
        print("  ERROR: escaping must not change the decoded value once parsed back out of the page, got {!r}".format(json.loads(escaped)))
        failed = True
    if _json_for_script({"a": 1}) != '{"a": 1}':
        print("  ERROR: expected an ordinary payload with no '</' sequence to serialise unchanged, got {!r}".format(_json_for_script({"a": 1})))
        failed = True

    return failed


def _collect_stringy_numbers(node, path, failures):
    """Recursively record every path in ``node`` whose value is a string that parses as a number.

    After ``validate_config()`` every numeric-context field should have been coerced
    through ``_require_number`` to an ``int``/``float``; a leftover string that still
    parses cleanly as a number is exactly what a missed coercion looks like (a legitimate
    string field - a postcode, a tariff URL, an API key, "flat"/"night"/"day" - never
    happens to parse as a plain number). Walking the whole structure rather than
    spot-checking a few known fields means a numeric field added later that forgets to
    route through ``_require_number`` fails this test too, instead of only surfacing as a
    crash deep in a live run.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            _collect_stringy_numbers(value, "{}.{}".format(path, key), failures)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _collect_stringy_numbers(value, "{}[{}]".format(path, index), failures)
    elif isinstance(node, str):
        try:
            float(node)
        except ValueError:
            return
        failures.append((path, node))


def test_web_annual_post_numeric_coercion(my_predbat):
    """Verify every numeric field the web form posts as a string survives validate_config() as a number.

    Drives the real seam between the browser and the engine, which nothing else
    exercised end to end: aiohttp's ``request.post()`` always returns strings, so this
    builds a postdata dict of strings exactly as a browser would submit, runs it through
    ``AnnualPage.config_from_post()`` and then the real ``validate_config()``, and walks
    the whole validated result asserting no numeric-context field was left as a string.
    Before the fix this reproduced the reported crash's root cause: declination and
    azimuth (and latitude/longitude) survived as strings all the way to ``solar_model.
    convert_azimuth()``, which fails a ``str``/``int`` comparison minutes into a live run.
    """
    failed = False
    print("**** Testing web_annual POST numeric coercion ****")

    page = make_page(my_predbat)
    postdata = {
        "latitude": "51.5",
        "longitude": "-0.1",
        "solar_kwp_0": "5.6",
        "solar_declination_0": "40",
        "solar_azimuth_0": "170",
        "solar_efficiency_0": "0.9",
        "battery_size_kwh": "9.5",
        "battery_inverter_kw": "5.0",
        "battery_export_limit_kw": "5.0",
        "battery_hybrid": "on",
        "load_source": "manual",
        "load_annual_kwh": "3800",
        "load_shape": "flat",
        "load_car_charging_kwh": "2500",
        "load_car_rate_kw": "7.4",
        "tariff_standing_charge": "60.0",
        "year": "2025",
        "samples_per_month": "3",
        "pv10_derate_fallback": "0.6",
    }

    config = page.config_from_post(postdata)
    try:
        validated = validate_config(config)
    except Exception as error:
        print("  ERROR: a posted form built entirely of strings should validate cleanly, got {}".format(error))
        return True

    print("Test: no numeric-looking string survives anywhere in the validated config")
    # 'raw' deliberately holds the original, unvalidated config exactly as posted (see
    # validate_config()'s scrub_secrets(raw) call) so the CLI subprocess and any debug
    # dump can reproduce what the user actually submitted - it is expected to still be
    # full of strings and is excluded from this walk for that reason.
    walked = copy.deepcopy(validated)
    walked.pop("raw", None)
    failures = []
    _collect_stringy_numbers(walked, "config", failures)
    if failures:
        print("  ERROR: numeric-looking strings survived validate_config() at: {}".format(failures))
        failed = True

    print("Test: declination, azimuth, latitude and longitude specifically are numbers")
    for path, value, expected in [
        ("solar[0].declination", validated["solar"][0]["declination"], 40),
        ("solar[0].azimuth", validated["solar"][0]["azimuth"], 170),
        ("location.latitude", validated["location"]["latitude"], 51.5),
        ("location.longitude", validated["location"]["longitude"], -0.1),
    ]:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            print("  ERROR: {} should be a number, got {!r}".format(path, value))
            failed = True
        elif abs(value - expected) > 1e-9:
            print("  ERROR: {} should be {}, got {}".format(path, expected, value))
            failed = True

    return failed


def test_web_annual_routes_registered(my_predbat):
    """Verify all seven Annual routes are registered, so a typo'd path cannot ship green."""
    failed = False
    print("**** Testing web_annual route registration ****")

    web_interface = WebInterface(my_predbat, web_port=5056)
    app = aiohttp_web.Application()
    web_interface._register_annual_routes(app)

    registered = set()
    for route in app.router.routes():
        registered.add((route.method, route.resource.canonical))

    expected = {
        ("GET", "/annual"),
        ("POST", "/annual"),
        ("POST", "/annual_run"),
        ("GET", "/annual_status"),
        ("POST", "/annual_cancel"),
        ("GET", "/annual_download"),
        ("GET", "/annual_plan"),
    }
    missing = expected - registered
    if missing:
        print("  ERROR: missing route registrations: {}".format(missing))
        failed = True

    return failed


def test_web_annual_plan_route(my_predbat):
    """Verify the plan viewer's route: resolves a captured plan, and 404s (never 500s) otherwise.

    ``./annual_plan``'s query string is attacker-controlled, so ``_find_plan`` must
    coerce defensively rather than let a malformed ``month``/``index`` raise out of
    the handler - a 500 here would be a defect, per the plan doc's own caution.
    """
    failed = False
    print("**** Testing web_annual plan route ****")

    page = make_page(my_predbat)
    storage = RaceStorage()
    page._storage = lambda: storage

    debug_results = copy.deepcopy(sample_run_results())
    debug_results["months"][0]["plans"] = [
        {"day": "2025-01-08", "leg": "single", "scenarios": {"with_predbat": {"rows": [], "soc_max": 9.5}}},
        {"day": "2025-01-24", "leg": "single", "scenarios": {"with_predbat": {"rows": [], "soc_max": 9.5}}},
    ]
    asyncio.run(save_run(storage, debug_results, page.config_from_post(valid_postdata()), "20250108-plans"))

    print("Test: _find_plan resolves a valid month/index/scenario to its raw_plan dict")
    plan = page._find_plan(debug_results, "1", "1", "with_predbat")
    if plan != {"rows": [], "soc_max": 9.5}:
        print("  ERROR: expected the second plan's with_predbat scenario, got {!r}".format(plan))
        failed = True

    print("Test: _find_plan returns None rather than raising on a non-numeric month/index")
    for month, index in [("not-a-number", "0"), ("1", "also-not-a-number"), (None, "0"), ("1", None)]:
        try:
            result = page._find_plan(debug_results, month, index, "with_predbat")
        except Exception as error:  # noqa: BLE001 - the point of this test is that nothing escapes
            print("  ERROR: _find_plan must never raise, got {} for month={!r} index={!r}".format(error, month, index))
            failed = True
            continue
        if result is not None:
            print("  ERROR: a malformed query should resolve to None, got {!r}".format(result))
            failed = True

    print("Test: _find_plan returns None for an out-of-range index and an unknown scenario")
    if page._find_plan(debug_results, "1", "99", "with_predbat") is not None:
        print("  ERROR: an out-of-range index should resolve to None")
        failed = True
    if page._find_plan(debug_results, "1", "0", "not_a_scenario") is not None:
        print("  ERROR: an unknown scenario should resolve to None")
        failed = True

    print("Test: the route returns the plan JSON for a valid query")
    response = asyncio.run(page.html_annual_plan(FakeRequest(query={"run": "20250108-plans", "month": "1", "index": "0", "scenario": "with_predbat"})))
    if response.status != 200:
        print("  ERROR: a valid query should return 200, got {}".format(response.status))
        failed = True

    print("Test: an unknown run id 404s rather than 500ing")
    response = asyncio.run(page.html_annual_plan(FakeRequest(query={"run": "no-such-run", "month": "1", "index": "0", "scenario": "with_predbat"})))
    if response.status != 404:
        print("  ERROR: an unknown run should 404, got {}".format(response.status))
        failed = True

    print("Test: a hostile, non-numeric month/index 404s rather than 500ing")
    response = asyncio.run(page.html_annual_plan(FakeRequest(query={"run": "20250108-plans", "month": "'; DROP TABLE", "index": "0", "scenario": "with_predbat"})))
    if response.status != 404:
        print("  ERROR: a malformed query should 404, got {}".format(response.status))
        failed = True

    print("Test: a missing run query parameter 404s rather than 500ing")
    response = asyncio.run(page.html_annual_plan(FakeRequest(query={})))
    if response.status != 404:
        print("  ERROR: an absent run parameter should 404, got {}".format(response.status))
        failed = True

    print("Test: a corrupt stored 'plans' structure (not a list) resolves to None rather than raising")
    # A dict would previously reach `plans[index]`, which raises KeyError for an int key
    # that is not present - escaping the None-on-any-failure contract the docstring
    # promises. Every shape here must resolve to None without raising.
    corrupt_results = copy.deepcopy(sample_run_results())
    for corrupt_plans in [{"0": "not a list"}, "also not a list", 42, None]:
        corrupt_results["months"][0]["plans"] = corrupt_plans
        try:
            result = page._find_plan(corrupt_results, "1", "0", "with_predbat")
        except Exception as error:  # noqa: BLE001 - the point of this test is that nothing escapes
            print("  ERROR: _find_plan must never raise on a corrupt 'plans' structure, got {} for plans={!r}".format(error, corrupt_plans))
            failed = True
            continue
        if result is not None:
            print("  ERROR: a corrupt 'plans' structure should resolve to None, got {!r} for plans={!r}".format(result, corrupt_plans))
            failed = True

    print("Test: a corrupt 'scenarios' entry (not a dict) inside an otherwise-valid plan resolves to None rather than raising")
    corrupt_results = copy.deepcopy(sample_run_results())
    corrupt_results["months"][0]["plans"] = [{"day": "2025-01-08", "leg": "single", "scenarios": ["not", "a", "dict"]}]
    try:
        result = page._find_plan(corrupt_results, "1", "0", "with_predbat")
    except Exception as error:  # noqa: BLE001
        print("  ERROR: _find_plan must never raise on a corrupt 'scenarios' entry, got {}".format(error))
        failed = True
    else:
        if result is not None:
            print("  ERROR: a corrupt 'scenarios' entry should resolve to None, got {!r}".format(result))
            failed = True

    return failed
