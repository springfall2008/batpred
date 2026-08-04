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

from annual import AnnualConfigError, validate_config
from annual_store import list_runs, load_run, save_run
from tariff_catalogue import BASELINE_DEFAULT_IMPORT_ID, CUSTOM_ID, EXPORT_TARIFFS, IMPORT_TARIFFS, NO_EXPORT_ID, PRICE_CAP_IMPORT_P
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
        "tariff_import_id": CUSTOM_ID,
        "tariff_export_id": CUSTOM_ID,
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

        print("Test: a single inverter's limits are read, not silently replaced by the example 5 kW")
        # inverter_limit and export_limit are sensor_lists too, so even ONE inverter stores
        # a list: a 3.6 kW inverter is [3600]. Without combine=True the float coercion
        # failed, get_arg returned its 0.0 default, and every real system fell back to the
        # example 5 kW while appearing to have been read from the user's own setup.
        my_predbat.args["inverter_limit"] = [3600]
        my_predbat.args["export_limit"] = [3600]
        config = make_page(my_predbat).prefill_config()
        if config["battery"]["inverter_kw"] != 3.6:
            print("  ERROR: a [3600] inverter_limit should read as 3.6 kW, got {}".format(config["battery"]["inverter_kw"]))
            failed = True
        if config["battery"]["export_limit_kw"] != 3.6:
            print("  ERROR: a [3600] export_limit should read as 3.6 kW, got {}".format(config["battery"]["export_limit_kw"]))
            failed = True

        print("Test: an AC-coupled system is not prefilled as hybrid")
        # This used to be inferred from inverter_type being present, which is true of every
        # configured system - so an AC-coupled setup was modelled as hybrid, letting the
        # plan charge the battery straight from DC PV that really has to make a round trip
        # through the inverter.
        # Set on the instance, not in args: inverter_hybrid is a CONFIG_ITEMS switch whose
        # value lives in an entity, so get_arg() ignores args for it entirely and would
        # return the default whatever we put there - a test written against args would
        # pass without testing anything.
        saved_hybrid = my_predbat.inverter_hybrid
        try:
            my_predbat.inverter_hybrid = False
            if make_page(my_predbat).prefill_config()["battery"]["hybrid"] is not False:
                print("  ERROR: an AC-coupled inverter should prefill as AC coupled, not hybrid")
                failed = True
            my_predbat.inverter_hybrid = True
            if make_page(my_predbat).prefill_config()["battery"]["hybrid"] is not True:
                print("  ERROR: a hybrid inverter should prefill as hybrid")
                failed = True
        finally:
            my_predbat.inverter_hybrid = saved_hybrid

        print("Test: multiple inverters' limits are summed")
        my_predbat.args["inverter_limit"] = [3600, 3600]
        config = make_page(my_predbat).prefill_config()
        if config["battery"]["inverter_kw"] != 7.2:
            print("  ERROR: two 3.6 kW inverters should total 7.2 kW, got {}".format(config["battery"]["inverter_kw"]))
            failed = True
        my_predbat.args.pop("inverter_limit", None)
        my_predbat.args.pop("export_limit", None)
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

        print("Test: the catalogues merge the user's compare_list, each side taking what it can use")
        my_predbat.args["compare_list"] = [{"id": "mine", "name": "My tariff", "rates_import_octopus_url": "https://example.com/x"}]
        import_ids = [entry["id"] for entry in make_page(my_predbat).import_catalogue()]
        export_ids = [entry["id"] for entry in make_page(my_predbat).export_catalogue()]
        if "mine" not in import_ids:
            print("  ERROR: a user compare_list entry should appear in the import catalogue, got {}".format(import_ids))
            failed = True
        # This entry supplies an import URL only, so offering it as an export tariff would
        # price export against a source it does not have.
        if "mine" in export_ids:
            print("  ERROR: an import-only compare_list entry should not appear in the export catalogue, got {}".format(export_ids))
            failed = True
        if "agile" not in import_ids or "outgoing_prime" not in export_ids:
            print("  ERROR: built-in entries should still be present, got {} / {}".format(import_ids, export_ids))
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

        print("Test: the tariff dropdowns list their catalogues and a Custom entry each")
        if CUSTOM_ID not in html:
            print("  ERROR: the dropdowns should offer a Custom entry")
            failed = True
        for select_id, expected in [("tariff_import_id", "Octopus Agile"), ("tariff_export_id", "Octopus Agile Outgoing")]:
            block = re.search(r'<select id="{}".*?</select>'.format(select_id), html, re.S)
            if not block:
                print("  ERROR: the {} dropdown should render".format(select_id))
                failed = True
            elif expected not in block.group(0):
                print("  ERROR: the {} dropdown should list the built-in tariffs, expected {}".format(select_id, expected))
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

        print("Test: a configured Octopus key and account fill the fields but do NOT select the Octopus source")
        # The Octopus source reads the IMPORT meter, and on a home that already has solar
        # or a battery that meter has had the existing system's self-consumption and
        # discharge subtracted from every reading - which is why the form carries a banner
        # warning against using it there. Selecting it automatically for anyone with
        # credentials in apps.yaml therefore picked exactly the wrong source for a
        # configured Predbat, which by definition has a battery or an array. Predicted
        # consumption is always the default; the credentials still prefill so switching to
        # Octopus is one click rather than a paste.
        my_predbat.args["octopus_api_key"] = "sk_live_exampleKey123"
        my_predbat.args["octopus_api_account"] = "A-1234ABCD"
        octopus_page = make_page(my_predbat)
        octopus_config = octopus_page.prefill_config()
        if "octopus" in (octopus_config.get("load") or {}):
            print("  ERROR: configured Octopus credentials must not select the Octopus load source, got {}".format(octopus_config.get("load")))
            failed = True
        if (octopus_config.get("load") or {}).get("annual_kwh") != DEFAULT_CONFIG["load"]["annual_kwh"]:
            print("  ERROR: the prefill should keep the predicted-consumption load, got {}".format(octopus_config.get("load")))
            failed = True
        try:
            validate_config(octopus_config)
        except Exception as error:
            print("  ERROR: the prefilled config must validate, got {}".format(error))
            failed = True
        octopus_form = octopus_page.render_form(octopus_config)
        if re.search(r'value="octopus"[^>]*checked', octopus_form):
            print("  ERROR: the Octopus radio must not be selected just because credentials are configured")
            failed = True
        if not re.search(r'value="manual"[^>]*checked', octopus_form):
            print("  ERROR: the predicted-consumption radio should be the selected default")
            failed = True
        # The credentials still reach the boxes, so choosing Octopus does not mean pasting
        # a key in by hand.
        if "sk_live_exampleKey123" not in octopus_form or "A-1234ABCD" not in octopus_form:
            print("  ERROR: configured Octopus credentials should still prefill the fields")
            failed = True
        if 'id="load_annual_kwh"' not in octopus_form or 'value=""' in octopus_form.split('id="load_annual_kwh"')[1][:80]:
            print("  ERROR: the manual consumption field should still show a default value")
            failed = True

        print("Test: a key without an account (or the reverse) does not prefill the credential fields")
        # An incomplete pair cannot download anything, so showing half of it would only
        # produce a run that fails partway through.
        my_predbat.args["octopus_api_key"] = "sk_live_exampleKey123"
        my_predbat.args.pop("octopus_api_account", None)
        partial_page = make_page(my_predbat)
        partial_form = partial_page.render_form(partial_page.prefill_config())
        if "sk_live_exampleKey123" in partial_form:
            print("  ERROR: an API key with no account must not prefill the credential fields")
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

        print("Test: selecting a basic-rates tariff uses THAT tariff, not the price-cap default")
        # The bug this pins: the dropdown only ever populated the URL boxes, and
        # config_from_post read the boxes. A basic-rates entry has no URL, so both boxes
        # were cleared and the no-URL fallback silently substituted the price-cap rates -
        # a different tariff to the one named on screen, and different money out.
        eon_post = valid_postdata()
        eon_post["tariff_import_id"] = "eon_next_drive"
        eon_post["tariff_import_url"] = ""
        eon_post["tariff_export_url"] = ""
        eon_config = make_page(my_predbat).config_from_post(eon_post)
        eon_rates = eon_config["tariff"].get("rates_import") or []
        if not any(abs(float(entry.get("rate", 0)) - 6.7) < 0.001 for entry in eon_rates):
            print("  ERROR: selecting Eon Next Drive should use its 6.7p night rate, got {}".format(eon_config["tariff"]))
            failed = True
        if any(abs(float(entry.get("rate", 0)) - 24.86) < 0.001 for entry in eon_rates) and len(eon_rates) == 1:
            print("  ERROR: selecting Eon Next Drive fell back to the flat price-cap default, got {}".format(eon_config["tariff"]))
            failed = True
        validate_config(eon_config)

        print("Test: selecting a URL tariff takes the URL from the catalogue, not the text box")
        # The box is hidden for a built-in entry, so a stale value left in it from an
        # earlier selection must not be what actually runs.
        stale_post = valid_postdata()
        stale_post["tariff_import_id"] = "agile"
        stale_post["tariff_import_url"] = "https://example.com/STALE-LEFTOVER/"
        stale_config = make_page(my_predbat).config_from_post(stale_post)
        if "STALE-LEFTOVER" in str(stale_config["tariff"]):
            print("  ERROR: a built-in tariff must ignore the hidden URL box, got {}".format(stale_config["tariff"]))
            failed = True
        if "AGILE" not in str(stale_config["tariff"].get("import_octopus_url", "")).upper():
            print("  ERROR: selecting Agile should use the Agile URL, got {}".format(stale_config["tariff"]))
            failed = True

        print("Test: Custom still honours the hand-entered URLs")
        custom_post = valid_postdata()
        custom_post["tariff_import_id"] = CUSTOM_ID
        custom_post["tariff_import_url"] = "https://example.com/my-own/"
        # A catalogue export alongside the custom import, so the next test can prove the
        # two sides toggle their URL boxes independently.
        custom_post["tariff_export_id"] = "seg"
        custom_post["tariff_export_url"] = ""
        custom_config = make_page(my_predbat).config_from_post(custom_post)
        if custom_config["tariff"].get("import_octopus_url") != "https://example.com/my-own/":
            print("  ERROR: Custom should use the typed URL, got {}".format(custom_config["tariff"]))
            failed = True

        print("Test: each URL box is only shown for its own side's Custom")
        builtin_form = make_page(my_predbat).render_form(stale_config)
        custom_form = make_page(my_predbat).render_form(custom_config)
        builtin_row = re.search(r'<div id="tariff-custom-import" style="display:([^"]*)"', builtin_form)
        custom_row = re.search(r'<div id="tariff-custom-import" style="display:([^"]*)"', custom_form)
        if not builtin_row or builtin_row.group(1) != "none":
            print("  ERROR: the import URL box should be hidden for a built-in tariff, got {}".format(builtin_row and builtin_row.group(1)))
            failed = True
        if not custom_row or custom_row.group(1) != "block":
            print("  ERROR: the import URL box should be shown for a custom import, got {}".format(custom_row and custom_row.group(1)))
            failed = True
        # The sides are independent, so a custom import must not drag the export box open
        # with it - that was the whole failure mode of the single paired dropdown.
        custom_export_row = re.search(r'<div id="tariff-custom-export" style="display:([^"]*)"', custom_form)
        if not custom_export_row or custom_export_row.group(1) != "none":
            print("  ERROR: a custom import with a catalogue export should leave the export URL box hidden, got {}".format(custom_export_row and custom_export_row.group(1)))
            failed = True

        print("Test: import and export can be combined freely, and each round-trips through the form")
        # The point of splitting the dropdowns: a pairing nobody enumerated in advance
        # must survive save-and-reload with both sides intact.
        for import_id, export_id, import_marker, export_marker in [
            ("price_cap", "outgoing_prime", "rates_import", "export_octopus_url"),
            ("agile", "seg", "import_octopus_url", "rates_export"),
            ("intelligent_go", "agile_outgoing", "import_octopus_url", "export_octopus_url"),
        ]:
            combo_post = valid_postdata()
            combo_post["tariff_import_id"] = import_id
            combo_post["tariff_export_id"] = export_id
            combo_post["tariff_import_url"] = ""
            combo_post["tariff_export_url"] = ""
            # Octopus product URLs are region-templated, and validate_config rightly
            # refuses one without a region letter to substitute into it.
            combo_post["tariff_dno_region"] = "A"
            combo_config = make_page(my_predbat).config_from_post(combo_post)
            combo_tariff = combo_config["tariff"]
            if not combo_tariff.get(import_marker):
                print("  ERROR: {}+{} should set {}, got {}".format(import_id, export_id, import_marker, combo_tariff))
                failed = True
            if not combo_tariff.get(export_marker):
                print("  ERROR: {}+{} should set {}, got {}".format(import_id, export_id, export_marker, combo_tariff))
                failed = True
            validate_config(combo_config)
            combo_form = make_page(my_predbat).render_form(combo_config)
            import_select = re.search(r'<select id="tariff_import_id".*?</select>', combo_form, re.S)
            export_select = re.search(r'<select id="tariff_export_id".*?</select>', combo_form, re.S)
            if not import_select or not re.search(r'value="{}"[^>]*selected'.format(import_id), import_select.group(0)):
                print("  ERROR: import {} should re-select itself on reload".format(import_id))
                failed = True
            if not export_select or not re.search(r'value="{}"[^>]*selected'.format(export_id), export_select.group(0)):
                print("  ERROR: export {} should re-select itself on reload".format(export_id))
                failed = True

        print("Test: two export tariffs sharing an import are told apart on reload")
        # The single paired dropdown matched on the import URL alone, so "Agile / Prime"
        # and "Agile / Fixed" were indistinguishable and a saved config came back as
        # whichever appeared first in the list. Pin that it no longer can.
        prime_post = valid_postdata()
        prime_post.update({"tariff_import_id": "agile", "tariff_export_id": "outgoing_prime", "tariff_import_url": "", "tariff_export_url": ""})
        prime_form = make_page(my_predbat).render_form(make_page(my_predbat).config_from_post(prime_post))
        prime_select = re.search(r'<select id="tariff_export_id".*?</select>', prime_form, re.S)
        if not prime_select or not re.search(r'value="outgoing_prime"[^>]*selected', prime_select.group(0)):
            print("  ERROR: Agile+Prime should reload as Prime, not as the first export sharing that import")
            failed = True

        print("Test: 'no export payment' is a real selection, priced at zero rather than left unset")
        no_export_post = valid_postdata()
        no_export_post.update({"tariff_import_id": "agile", "tariff_export_id": NO_EXPORT_ID, "tariff_import_url": "", "tariff_export_url": "", "tariff_dno_region": "A"})
        no_export_config = make_page(my_predbat).config_from_post(no_export_post)
        no_export_tariff = no_export_config["tariff"]
        if no_export_tariff.get("rates_export") != [{"rate": 0.0}]:
            print("  ERROR: no export should set a flat 0p export rate, got {}".format(no_export_tariff))
            failed = True
        if no_export_tariff.get("export_octopus_url"):
            print("  ERROR: no export must not also carry an export URL, got {}".format(no_export_tariff))
            failed = True
        validate_config(no_export_config)
        no_export_form = make_page(my_predbat).render_form(no_export_config)
        no_export_select = re.search(r'<select id="tariff_export_id".*?</select>', no_export_form, re.S)
        if not no_export_select or not re.search(r'value="{}"[^>]*selected'.format(NO_EXPORT_ID), no_export_select.group(0)):
            print("  ERROR: no export should re-select itself on reload")
            failed = True

        print("Test: a config with no export source at all shows as 'no export' rather than Custom")
        # A prefill from an Octopus account with an import tariff but no export agreement
        # lands here. Falling through to Custom would open an empty URL box and read as a
        # broken config rather than the ordinary situation it is.
        bare_form = make_page(my_predbat).render_form({"tariff": {"import_octopus_url": "https://example.com/import/", "standing_charge_p_per_day": 60.0}})
        bare_select = re.search(r'<select id="tariff_export_id".*?</select>', bare_form, re.S)
        if not bare_select or not re.search(r'value="{}"[^>]*selected'.format(NO_EXPORT_ID), bare_select.group(0)):
            print("  ERROR: a tariff with no export source should default the dropdown to no export")
            failed = True

        print("Test: a basic-rates tariff re-selects its own dropdown entry, not the first one")
        if not re.search(r'<option value="eon_next_drive"[^>]*selected', make_page(my_predbat).render_form(eon_config)):
            print("  ERROR: a basic-rates config should re-select its own catalogue entry")
            failed = True

        print("Test: arrays can be added and removed, keeping what was already typed")
        array_page = make_page(my_predbat)
        array_base = valid_postdata()
        added = asyncio.run(array_page.html_annual_array(FakeRequest(dict(array_base, array_op="add")))).text
        if added.count("Remove array") != 2:
            print("  ERROR: adding should give two arrays, got {}".format(added.count("Remove array")))
            failed = True

        two_arrays = dict(array_base)
        two_arrays.update({"solar_kwp_1": "3.0", "solar_declination_1": "35", "solar_azimuth_1": "90", "solar_mode_1": "kwp", "solar_efficiency_1": "0.95"})
        removed = asyncio.run(array_page.html_annual_array(FakeRequest(dict(two_arrays, array_op="remove:0")))).text
        if removed.count("Remove array") != 1:
            print("  ERROR: removing should leave one array, got {}".format(removed.count("Remove array")))
            failed = True
        # The RIGHT one goes, and the survivor keeps its own values - an off-by-one here
        # would silently delete the wrong array and look like it worked.
        if 'value="3.0"' not in removed or 'value="5.6"' in removed:
            print("  ERROR: removing array 1 should drop 5.6 and keep 3.0, got neither")
            failed = True

        print("Test: every array can be removed, which is a valid battery-only run")
        emptied = asyncio.run(array_page.html_annual_array(FakeRequest(dict(array_base, array_op="remove:0")))).text
        if "Remove array" in emptied or "model the battery on its own" not in emptied:
            print("  ERROR: removing the last array should leave none and say so")
            failed = True

        print("Test: a malformed array operation is ignored rather than raising")
        # The value comes off a form post, so it is not to be trusted.
        for bad_op in ["remove:abc", "remove:99", "remove:-1", "nonsense", ""]:
            try:
                asyncio.run(array_page.html_annual_array(FakeRequest(dict(array_base, array_op=bad_op))))
            except Exception as error:
                print("  ERROR: array_op={!r} raised {}".format(bad_op, type(error).__name__))
                failed = True

        print("Test: the buttons and progress bar sit above the fields, not below them")
        # Run should be reachable without scrolling past every fieldset, and the progress
        # bar it reveals has to be in view rather than below the fold.
        top_form = make_page(my_predbat).render_form(make_page(my_predbat).prefill_config())
        above_fields = top_form.split("<fieldset>")[0]
        for expected in ['class="annual-actions"', 'id="annual-progress"']:
            if expected not in above_fields:
                print("  ERROR: {} should appear before the first fieldset, not after it".format(expected))
                failed = True
        # Ordered buttons-then-progress: the bar belongs directly under the control that
        # starts it.
        if above_fields.index('class="annual-actions"') > above_fields.index('id="annual-progress"'):
            print("  ERROR: the progress bar should sit under the buttons, not above them")
            failed = True
        # Still inside the form, or the submit buttons would no longer submit it.
        if above_fields.index("<form") > above_fields.index('class="annual-actions"'):
            print("  ERROR: the buttons must stay inside the form to submit it")
            failed = True

        print("Test: every page carries the What If title")
        # In render_nav rather than the three handlers, so they cannot drift apart - a
        # title on two pages out of three is the failure this guards.
        for page_name in ["config", "view", "compare"]:
            if "What If Annual Prediction" not in make_page(my_predbat).render_nav(page_name):
                print("  ERROR: the {} page should carry the What If title".format(page_name))
                failed = True

        print("Test: the no-PV/battery scenario gets its own tariff, defaulting to the price cap")
        # A household with no system is not on a battery tariff, so pricing the
        # counterfactual on one credits it with a saving it could never have had.
        baseline_form = make_page(my_predbat).render_form(make_page(my_predbat).prefill_config())
        if 'name="baseline_tariff_id"' not in baseline_form:
            print("  ERROR: the form should offer a separate baseline tariff")
            failed = True
        baseline_select = re.search(r'<select id="baseline_tariff_id".*?</select>', baseline_form, re.S)
        if not baseline_select:
            print("  ERROR: the baseline tariff dropdown should render")
            failed = True
        else:
            # Custom is a hand-entered URL, which makes no sense for "what would they
            # otherwise be on".
            if 'value="{}"'.format(CUSTOM_ID) in baseline_select.group(0):
                print("  ERROR: Custom should not be offered as a baseline tariff")
                failed = True
            if not re.search(r'value="price_cap"[^>]*selected', baseline_select.group(0)):
                print("  ERROR: the baseline should default to the price cap, got {}".format(baseline_select.group(0)[:200]))
                failed = True

        print("Test: a chosen baseline tariff reaches the config and validates")
        baseline_post = valid_postdata()
        baseline_post["baseline_tariff_id"] = "price_cap"
        baseline_config = make_page(my_predbat).config_from_post(baseline_post)
        if not (baseline_config.get("baseline_tariff") or {}).get("rates_import"):
            print("  ERROR: the chosen baseline tariff should reach the config, got {}".format(baseline_config.get("baseline_tariff")))
            failed = True
        validated = validate_config(baseline_config)
        if not (validated.get("baseline_tariff") or {}).get("rates_import"):
            print("  ERROR: the baseline tariff should survive validation, got {}".format(validated.get("baseline_tariff")))
            failed = True

        print("Test: a baseline matching no catalogue entry still marks an option selected")
        # A baseline written by hand in YAML resolves to CUSTOM_ID, which this dropdown
        # deliberately does not offer - so nothing was marked selected and the form
        # depended on the browser showing the first option to display anything at all.
        # Anything reading the HTML rather than rendering it saw a dropdown with no
        # selection, and the page stated no baseline where it means the price cap.
        custom_baseline = make_page(my_predbat).prefill_config()
        custom_baseline["baseline_tariff"] = {"import_octopus_url": "https://api.octopus.energy/v1/products/MY-OWN-DEAL/x/", "rates_export": [{"rate": 4.1}]}
        custom_select = re.search(r'<select id="baseline_tariff_id".*?</select>', make_page(my_predbat).render_form(custom_baseline), re.S)
        if not custom_select or len(re.findall(r"selected", custom_select.group(0))) != 1:
            print("  ERROR: exactly one baseline option should be selected, got {}".format(custom_select.group(0) if custom_select else None))
            failed = True
        elif not re.search(r'value="{}"[^>]*selected'.format(BASELINE_DEFAULT_IMPORT_ID), custom_select.group(0)):
            print("  ERROR: an unmatched baseline should fall back to the price cap, got {}".format(custom_select.group(0)[:300]))
            failed = True

        print("Test: an unrecognised baseline id falls back to the default rather than inventing rates")
        stale_post = valid_postdata()
        stale_post["baseline_tariff_id"] = "no-such-tariff"
        stale_validated = validate_config(make_page(my_predbat).config_from_post(stale_post))
        cap_rate = (stale_validated["baseline_tariff"].get("rates_import") or [{}])[0].get("rate")
        if cap_rate != PRICE_CAP_IMPORT_P:
            print("  ERROR: an unknown baseline id should fall back to the price cap, got {}".format(cap_rate))
            failed = True

        print("Test: the form shows a live cost estimate and offers quote overrides")
        cost_form = make_page(my_predbat).render_form(make_page(my_predbat).prefill_config())
        if 'id="annual-cost-estimate"' not in cost_form:
            print("  ERROR: the form should carry a live install-cost readout")
            failed = True
        for field in ['name="cost_quoted_pv_gbp"', 'name="cost_quoted_total_gbp"']:
            if field not in cost_form:
                print("  ERROR: the form should offer {}".format(field))
                failed = True
        # A real quote is the most useful thing a user can give us, so it belongs on the
        # page rather than behind the Advanced toggle with the model's own parameters.
        before_advanced = cost_form.split("<details><summary>Advanced")[0]
        if "cost_quoted_pv_gbp" not in before_advanced:
            print("  ERROR: the quote fields should be on the page, not hidden under Advanced")
            failed = True

        print("Test: a submitted quote reaches the config")
        quote_post = valid_postdata()
        quote_post["cost_quoted_pv_gbp"] = "7000"
        quote_post["cost_quoted_total_gbp"] = "11200"
        quote_config = make_page(my_predbat).config_from_post(quote_post)
        if quote_config.get("costs", {}).get("quoted_pv_gbp") != 7000 or quote_config.get("costs", {}).get("quoted_total_gbp") != 11200:
            print("  ERROR: submitted quotes should reach the config, got {}".format(quote_config.get("costs")))
            failed = True
        validate_config(quote_config)

        print("Test: the cost preview endpoint uses the real model and honours a quote")
        preview = asyncio.run(make_page(my_predbat).html_annual_cost_preview(FakeRequest(query={"total_kwp": "5.0", "battery_kwh": "9.5"})))
        modelled = json.loads(preview.text)
        if abs(modelled["battery_gbp"] - 3350.0) > 0.01 or modelled["pv_quoted"]:
            print("  ERROR: the preview should use the cost model, got {}".format(modelled))
            failed = True
        quoted_preview = asyncio.run(make_page(my_predbat).html_annual_cost_preview(FakeRequest(query={"total_kwp": "5.0", "battery_kwh": "9.5", "quoted_pv_gbp": "7000"})))
        quoted_body = json.loads(quoted_preview.text)
        if abs(quoted_body["pv_gbp"] - 7000.0) > 0.01 or not quoted_body["pv_quoted"]:
            print("  ERROR: the preview should honour a quote, got {}".format(quoted_body))
            failed = True

        print("Test: the preview survives a half-typed number rather than 500ing")
        # It fires on every keystroke, so "3." and "" both arrive in the normal course of
        # someone typing a size.
        for query in [{"total_kwp": "3."}, {"total_kwp": ""}, {"total_kwp": "abc"}, {"battery_kwh": "-"}, {}]:
            try:
                asyncio.run(make_page(my_predbat).html_annual_cost_preview(FakeRequest(query=query)))
            except Exception as error:
                print("  ERROR: preview raised {} for {}".format(type(error).__name__, query))
                failed = True

        print("Test: reset restores the live instance's settings, not a stranger's")
        # "Default" means prefill_config(), which reads this Predbat's own solar, battery
        # and tariff before filling gaps with the example - so on a configured instance
        # resetting must land on the user's system, not the generic UK one.
        saved_args = dict(my_predbat.args)
        try:
            my_predbat.args["soc_max"] = 12.5
            reset_page = make_page(my_predbat)
            reset_page.save_config({"battery": {"size_kwh": 99.0}, "solar": [{"kwp": 99.0}], "load": {"annual_kwh": 99}, "tariff": {"rates_import": [{"rate": 99.0}]}})
            response = asyncio.run(reset_page.html_annual_reset(FakeRequest()))
            # Assert on the FIELDS, not a substring of the page: searching the whole
            # document for "99" matches `z-index: 9999` in Predbat's own header CSS and
            # passes regardless of what reset did.
            battery_field = re.search(r'id="battery_size_kwh"[^>]*value="([^"]*)"', response.text)
            solar_field = re.search(r'id="solar_kwp_0"[^>]*value="([^"]*)"', response.text)
            if not battery_field or float(battery_field.group(1)) != 12.5:
                print("  ERROR: reset should restore the live instance's battery size, got {}".format(battery_field and battery_field.group(1)))
                failed = True
            if not solar_field or float(solar_field.group(1)) == 99.0:
                print("  ERROR: reset should discard the saved solar size, got {}".format(solar_field and solar_field.group(1)))
                failed = True
            # Saved, not just shown: a reset the user must remember to confirm with Save
            # would leave the old configuration on disk and still running.
            if float((reset_page.load_config().get("battery") or {}).get("size_kwh", 0)) != 12.5:
                print("  ERROR: reset should persist, got {}".format(reset_page.load_config().get("battery")))
                failed = True
        finally:
            my_predbat.args.clear()
            my_predbat.args.update(saved_args)

        print("Test: the reset button confirms before discarding, and is not the run button")
        reset_form = make_page(my_predbat).render_form(make_page(my_predbat).prefill_config())
        reset_button = re.search(r"<button[^>]*annual_reset[^>]*>.*?</button>", reset_form, re.S)
        if not reset_button:
            print("  ERROR: the form should offer a reset button")
            failed = True
        elif "confirm(" not in reset_button.group(0):
            print("  ERROR: reset discards the saved config with no undo, so it must confirm first")
            failed = True
        elif "annualMarkStarted" in reset_button.group(0):
            print("  ERROR: reset must not mark the tab as having started a run")
            failed = True

        print("Test: the Advanced block and the buttons line up with the fieldsets above")
        # Both are bare children of the form, while every fieldset insets its content by
        # its own border plus padding - so without an explicit inset they drift left of
        # everything above them, and the buttons sit flush against the page bottom.
        layout_form = make_page(my_predbat).render_form(make_page(my_predbat).prefill_config())
        if 'class="annual-actions"' not in layout_form:
            print("  ERROR: the submit buttons should be wrapped so they can be aligned and spaced")
            failed = True
        layout_css = make_page(my_predbat).render_css()
        if ".annual-actions" not in layout_css or ".annual-form-wrap > details" not in layout_css:
            print("  ERROR: the Advanced block and the button row both need the fieldset inset")
            failed = True

        print("Test: the tab's prose wraps, overriding Predbat's global nowrap on paragraphs")
        # web_helper.py's page stylesheet sets `p { white-space: nowrap }`, which suits the
        # short single-line paragraphs elsewhere in Predbat but ran every sentence on this
        # tab off the right of the page - the warning below among them. Nothing about the
        # markup reveals this, so it needs pinning here.
        css = make_page(my_predbat).render_css()
        wrap_rule = re.search(r"([^\n{]*)\{[^}]*white-space:\s*normal", css)
        if not wrap_rule:
            print("  ERROR: the tab must re-enable wrapping for its own paragraphs")
            failed = True
        else:
            for needed in [".annual-banner", ".annual-note", ".annual-results p"]:
                if needed not in wrap_rule.group(1):
                    print("  ERROR: {} should be included in the wrapping override, got {!r}".format(needed, wrap_rule.group(1)))
                    failed = True
        # The compare table still relies on nowrap to keep its columns intact while it
        # scrolls sideways, so the override must not have swept that away.
        if "white-space: nowrap" not in css:
            print("  ERROR: the compare table's own nowrap should survive the wrapping override")
            failed = True

        print("Test: the Octopus import option warns against using it with an existing system")
        octopus_form = make_page(my_predbat).render_form(make_page(my_predbat).prefill_config())
        if "do not already have solar or a battery" not in octopus_form:
            print("  ERROR: the Octopus option must warn that meter readings understate an existing system's load")
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

        print("Test: a saved config with no octopus block still shows the live credentials")
        # The reported bug: prefill_config() only runs on a FIRST visit, because
        # load_config() returns the saved annual.yaml once one exists. A user who had
        # saved a config on the manual source - which stores no octopus block at all -
        # saw both boxes permanently empty despite having Octopus configured.
        my_predbat.args["octopus_api_key"] = "sk_live_savedCase"
        my_predbat.args["octopus_api_account"] = "A-SAVED01"
        saved_manual = {
            "location": {"postcode": "SW1A 1AA"},
            "solar": [{"kwp": 5.0}],
            "battery": {"size_kwh": 9.5, "inverter_kw": 5.0},
            "load": {"annual_kwh": 3800, "shape": "night", "car_charging_kwh": 3000, "car_rate_kw": 7.4},
            "tariff": {"rates_import": [{"rate": 24.86}], "standing_charge_p_per_day": 60.0},
        }
        saved_form = make_page(my_predbat).render_form(saved_manual)
        if "sk_live_savedCase" not in saved_form or "A-SAVED01" not in saved_form:
            print("  ERROR: the live Octopus credentials should fill the boxes even when the saved config has no octopus block")
            failed = True
        # ...but the saved config's own source choice is the user's, and is left alone.
        if re.search(r'value="octopus"[^>]*checked', saved_form):
            print("  ERROR: falling back to the live credentials must not switch the saved manual source to Octopus")
            failed = True
        if not re.search(r'value="manual"[^>]*checked', saved_form):
            print("  ERROR: the saved manual source should stay selected")
            failed = True

        print("Test: an incomplete credential pair does not half-fill the boxes")
        my_predbat.args.pop("octopus_api_account", None)
        partial_form = make_page(my_predbat).render_form(saved_manual)
        if "sk_live_savedCase" in partial_form:
            print("  ERROR: a key with no account cannot download anything, so it must not be offered")
            failed = True
        my_predbat.args.pop("octopus_api_key", None)

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
            "tariff_import_id": CUSTOM_ID,
            "tariff_export_id": CUSTOM_ID,
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
        catalogue_entries = page.import_catalogue()
        built_in = next(entry for entry in catalogue_entries if entry["id"] != CUSTOM_ID and entry.get("import_octopus_url"))

        def import_select(html_text):
            """Return just the import dropdown's markup - Custom appears in both selects."""
            block = re.search(r'<select id="tariff_import_id".*?</select>', html_text, re.S)
            return block.group(0) if block else ""

        matched_config = copy.deepcopy(config)
        matched_config["tariff"] = {"import_octopus_url": built_in["import_octopus_url"], "standing_charge_p_per_day": 60.0}
        matched_html = import_select(page.render_form(matched_config))
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
        custom_html = import_select(page.render_form(custom_config))
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

        print("Test: a non-numeric panel count reaches validate_config as the posted string, and raises AnnualConfigError, not a bare ValueError")
        # The kwp branch (numeric("solar_kwp_0")) already passes an unparsed value
        # straight through so validate_config can raise its actionable error; the
        # panels branch used to wrap it in int(...), which raised a bare ValueError
        # from inside config_from_post itself - before validate_config ever ran, and
        # uncaught by anything in the request handler.
        postdata = valid_postdata()
        postdata["solar_mode_0"] = "panels"
        postdata["solar_panels_0"] = "abc"
        postdata["solar_panel_watts_0"] = "400"
        bad_panels_config = make_page(my_predbat).config_from_post(postdata)
        if bad_panels_config["solar"][0].get("panels") != "abc":
            print("  ERROR: a non-numeric panel count should survive config_from_post unchanged, got {!r}".format(bad_panels_config["solar"][0].get("panels")))
            failed = True
        try:
            validate_config(bad_panels_config)
            print("  ERROR: a non-numeric panel count should be rejected by validate_config, it validated cleanly")
            failed = True
        except AnnualConfigError:
            pass
        except Exception as error:  # noqa: BLE001 - the point of this test is that nothing but AnnualConfigError escapes
            print("  ERROR: a non-numeric panel count should raise AnnualConfigError, got a bare {}: {}".format(type(error).__name__, error))
            failed = True

        print("Test: a fractional panel count is rejected by validate_config, not silently truncated")
        # int(13.7) == 13 would have quietly cost 0.7 of a panel (~£30) less than the
        # user typed, defeating _require_number's integer-float guard.
        postdata = valid_postdata()
        postdata["solar_mode_0"] = "panels"
        postdata["solar_panels_0"] = "13.7"
        postdata["solar_panel_watts_0"] = "400"
        fractional_config = make_page(my_predbat).config_from_post(postdata)
        if fractional_config["solar"][0].get("panels") != 13.7:
            print("  ERROR: a fractional panel count should survive config_from_post as 13.7, not truncated, got {!r}".format(fractional_config["solar"][0].get("panels")))
            failed = True
        try:
            validate_config(fractional_config)
            print("  ERROR: a fractional panel count (13.7) should be rejected, it validated cleanly (silently truncated)")
            failed = True
        except AnnualConfigError:
            pass

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

        print("Test: the Predbat annual cost defaults to zero in the rendered form")
        # prefill_config() itself has no "costs" key at all when unconfigured (see
        # DEFAULT_CONFIG), so asserting against prefill_config().get("costs", {})...
        # reduces to "0 != 0" and would pass even with the whole cost feature deleted.
        # Assert against what the visitor actually sees instead: the rendered
        # cost_predbat_annual_gbp field.
        default_form = make_page(my_predbat).render_form(make_page(my_predbat).prefill_config())
        default_field = re.search(r'id="cost_predbat_annual_gbp"[^>]*value="([^"]*)"', default_form)
        if not default_field or float(default_field.group(1)) != 0:
            print("  ERROR: predbat_annual_gbp must default to 0 - Predbat is free when self-hosted, got {!r}".format(default_field.group(1) if default_field else None))
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
        # The run's OWN settings, as scrub_secrets left them - what the details table
        # must describe, rather than whatever the live form happens to hold.
        "config": {
            "solar": [{"kwp": 5.6, "declination": 35, "azimuth": 180}],
            "battery": {"size_kwh": 9.5, "inverter_kw": 5.0, "export_limit_kw": 5.0, "hybrid": True},
            "load": {"annual_kwh": 3800, "shape": "night", "car_charging_kwh": 3000, "car_rate_kw": 7.4},
            # Templated, with dno_region beside them: results["config"] is the RAW config
            # (annual.py stores self.config["raw"]), and AnnualTariff substitutes the
            # region at fetch time without writing it back. A pre-substituted URL here
            # would not be what a real run stores, and would quietly stop matching the
            # catalogue that names it.
            "tariff": {
                "import_octopus_url": "https://api.octopus.energy/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-{dno_region}/standard-unit-rates/",
                "export_octopus_url": "https://api.octopus.energy/v1/products/OUTGOING-PRIME-FIX-12M-26-06-23/electricity-tariffs/E-1R-OUTGOING-PRIME-FIX-12M-26-06-23-{dno_region}/standard-unit-rates/",
                "dno_region": "A",
                "standing_charge_p_per_day": 60.0,
            },
            "samples_per_month": 2,
        },
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

    print("Test: the nav marks the viewer as the current page, so it is clear which page this is")
    nav = page.render_nav("view")
    if "annual-nav-current" not in nav:
        print("  ERROR: the nav should mark the viewer page as current")
        failed = True

    print("Test: a run states the key settings it actually used")
    details = page._render_run_details(results)
    for expected in ["5.6 kWp", "9.5 kWh", "Octopus Agile", "Octopus Outgoing Prime", "3,800 kWh a year", "more at night", "3,000 kWh a year", "60p a day"]:
        if expected not in details:
            print("  ERROR: the run details should state {}, got {}".format(expected, details))
            failed = True
    # The full URL stays as the cell's title for anyone checking the exact endpoint - the
    # catalogue name replaces the product code on screen, it does not hide the source.
    for expected in ["AGILE-24-10-01", "OUTGOING-PRIME-FIX-12M-26-06-23"]:
        if expected not in details:
            print("  ERROR: the run details should keep {} as the cell's title, got {}".format(expected, details))
            failed = True

    print("Test: the run details name the baseline tariff the saving is measured against")
    # Without it the page states a saving but not what it is a saving FROM, which cannot
    # be checked - and the compare table beside it does show the baseline.
    baseline_run = copy.deepcopy(results)
    baseline_run["config"]["baseline_tariff"] = {"rates_import": [{"rate": PRICE_CAP_IMPORT_P}]}
    baseline_details = page._render_run_details(baseline_run)
    if "Baseline tariff" not in baseline_details or "Price cap" not in baseline_details:
        print("  ERROR: the run details should name the baseline tariff, got {}".format(baseline_details))
        failed = True

    print("Test: a run that recorded no baseline omits the row rather than inventing one")
    no_baseline = copy.deepcopy(results)
    no_baseline["config"].pop("baseline_tariff", None)
    if "Baseline tariff" in page._render_run_details(no_baseline):
        print("  ERROR: a run with no stored baseline should not show a baseline row")
        failed = True

    print("Test: the details describe the RUN's own config, not the live form")
    # The selector can show a run from a completely different system; labelling it with
    # today's settings would misattribute every figure below it.
    other = copy.deepcopy(results)
    other["config"]["solar"] = [{"kwp": 12.0}]
    other["config"]["battery"] = {"size_kwh": 20.0, "inverter_kw": 8.0, "hybrid": False}
    other_details = page._render_run_details(other)
    if "12 kWp" not in other_details or "20 kWh" not in other_details or "AC coupled" not in other_details:
        print("  ERROR: the details must come from the stored run, got {}".format(other_details))
        failed = True
    if "5.6 kWp" in other_details:
        print("  ERROR: the details are showing another run's system")
        failed = True

    print("Test: an Octopus-sourced run says so rather than inventing a kWh figure")
    octopus_run = copy.deepcopy(results)
    octopus_run["config"]["load"] = {"octopus": {"api_key": "xxx", "account_id": "A-1234ABCD"}, "shape": "flat"}
    octopus_details = page._render_run_details(octopus_run)
    if "A-1234ABCD" not in octopus_details or "Octopus consumption history" not in octopus_details:
        print("  ERROR: an Octopus run should name its account, got {}".format(octopus_details))
        failed = True
    if "3,800 kWh a year" in octopus_details:
        print("  ERROR: an Octopus run has no synthetic annual figure to show")
        failed = True

    print("Test: a run that recorded no settings says so rather than rendering an empty table")
    bare = copy.deepcopy(results)
    bare.pop("config", None)
    if "did not record the settings" not in page._render_run_details(bare):
        print("  ERROR: a run with no stored config should say so")
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

    print("Test: a no-battery run does not price a PV + battery or With Predbat row")
    # A user who blanks the battery field gets a config with no battery configured
    # (a supported, first-class configuration - see build_label's "no battery" runs).
    # Before the fix, these two rows still rendered at the PV-only capital, reading
    # as though the battery were free.
    no_battery = copy.deepcopy(results)
    no_battery["annual"]["costs"]["battery_kwh"] = 0
    no_battery_html = page._render_payback(no_battery)
    if "PV + battery" in no_battery_html or "With Predbat" in no_battery_html:
        print("  ERROR: a no-battery run must not price a battery row, got {!r}".format(no_battery_html))
        failed = True
    if "PV only" not in no_battery_html:
        print("  ERROR: a no-battery run should still show the PV-only row, got {!r}".format(no_battery_html))
        failed = True

    print("Test: a no-PV run does not price a PV only row")
    no_pv = copy.deepcopy(results)
    no_pv["annual"]["costs"]["total_kwp"] = 0
    no_pv_html = page._render_payback(no_pv)
    if "PV only" in no_pv_html:
        print("  ERROR: a no-PV run must not price a PV-only row, got {!r}".format(no_pv_html))
        failed = True
    if "PV + battery" not in no_pv_html or "With Predbat" not in no_pv_html:
        print("  ERROR: a no-PV run should still show the battery rows, got {!r}".format(no_pv_html))
        failed = True

    print("Test: a run with neither PV nor battery says so instead of rendering an empty table")
    neither = copy.deepcopy(results)
    neither["annual"]["costs"]["battery_kwh"] = 0
    neither["annual"]["costs"]["total_kwp"] = 0
    neither_html = page._render_payback(neither)
    if "<table" in neither_html:
        print("  ERROR: a run with no PV and no battery must not render a payback table, got {!r}".format(neither_html))
        failed = True

    print("Test: a payback row with pays_back True but years None does not raise, and falls back safely")
    # build_payback never produces this combination today, but a future engine
    # change or a hand-edited stored document could - "{:.1f} years".format(None)
    # raises TypeError, which would 500 the results page.
    malformed = copy.deepcopy(results)
    malformed["annual"]["payback"]["pv_only"] = {"pays_back": True, "years": None, "capital_gbp": 8000.0, "annual_saving_gbp": 100.0, "gross_annual_saving_gbp": 100.0, "predbat_annual_gbp": 0.0}
    try:
        malformed_html = page._render_payback(malformed)
    except TypeError as error:
        print("  ERROR: a row with pays_back True and years None must not raise, got {}".format(error))
        failed = True
    else:
        if "does not pay back" not in malformed_html.lower():
            print("  ERROR: a row with years=None should fall back to the 'does not pay back' message, got {!r}".format(malformed_html))
            failed = True

    print("Test: a scenario missing from every included month (a run stored before the scenario existed) is dropped from the chart, not drawn as a fabricated zero bar")
    legacy_results = copy.deepcopy(sample_run_results())
    for month_entry in legacy_results["months"]:
        (month_entry.get("scenarios") or {}).pop("pv_only", None)
    legacy_chart_html = page._render_chart(legacy_results)
    if '"PV only"' in legacy_chart_html:
        print("  ERROR: a scenario absent from every included month must be dropped from the chart series entirely, got it still named in the payload: {!r}".format(legacy_chart_html))
        failed = True
    if "#9439ef" in legacy_chart_html:
        print("  ERROR: the PV-only colour must not appear once the scenario itself was dropped from the chart")
        failed = True
    if "No PV/Battery" not in legacy_chart_html:
        print("  ERROR: the other, still-present scenarios must still render normally in the chart")
        failed = True

    print("Test: the same missing scenario is omitted from the month table, not shown as a fabricated 0 kWh row")
    legacy_month_table = page._render_month_table(legacy_results)
    if "PV only" in legacy_month_table:
        print("  ERROR: a scenario missing from a month must not get a row in the month table, got {!r}".format(legacy_month_table))
        failed = True
    if "No PV/Battery" not in legacy_month_table:
        print("  ERROR: the other, still-present scenarios must still get a row in the month table")
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

    print("Test: the selector submits to the viewer, not the configure page")
    # Before the page split, ./annual rendered the results and read ?run=; now it is
    # the configuration form and ignores the parameter entirely. A selector that still
    # posts to ./annual strands the visitor on the config page with the run dropped -
    # the critical regression this test exists to catch.
    selector_html = page._render_selector(runs, runs[0]["id"])
    if 'action="./annual_view"' not in selector_html:
        print("  ERROR: the run selector should submit to ./annual_view, got {!r}".format(selector_html))
        failed = True
    if 'action="./annual"' in selector_html:
        print("  ERROR: the run selector must not submit to ./annual (the configure page), got {!r}".format(selector_html))
        failed = True

    print("Test: a download link is offered for the selected run")
    if "annual_download?run=20260726-101500" not in html:
        print("  ERROR: the selected run should be downloadable as JSON")
        failed = True

    print("Test: a run with captured plans, saved and reloaded through storage, still offers a plan viewer")
    # Round-tripped through save_run/load_run/list_runs rather than handed a dict that
    # still has "plans" embedded: save_run strips plans out of the document it stores
    # and records their labels as plan_index on the index entry instead, so a test built
    # on a hand-built embedded-plans dict would stay green even if the viewer were still
    # reading the (now-empty) document instead of plan_index - which is exactly the bug
    # this replaces (the viewer was permanently empty for every run saved after the
    # split, because it read entry.get("plans") from a document that no longer has one).
    plan_storage = RaceStorage()
    debug_results = copy.deepcopy(sample_run_results())
    debug_results["months"][0]["plans"] = [{"day": "2025-01-15", "leg": "single", "scenarios": {"with_predbat": {"rows": [], "soc_max": 9.5}}}]
    asyncio.run(save_run(plan_storage, debug_results, {}, "20260727-plan-viewer"))
    stored_results = asyncio.run(load_run(plan_storage, "20260727-plan-viewer"))
    stored_runs = asyncio.run(list_runs(plan_storage))
    html_text = page.render_results(stored_results, stored_runs, "20260727-plan-viewer")
    if "annual-plan-viewer" not in html_text:
        print("  ERROR: a run whose plans were split into their own storage keys should still render the plan viewer")
        failed = True
    if "renderPlanTable" not in html_text:
        print("  ERROR: the viewer must use the existing plan renderer")
        failed = True
    if "2025-01-15" not in html_text:
        print("  ERROR: the day option should come from the index's plan_index, got {!r}".format(html_text))
        failed = True

    print("Test: a legacy run (plans still embedded in the document, no plan_index recorded) still offers a plan viewer")
    legacy_runs = [{"id": "legacy-plan-run", "label": "legacy", "months_included": 2}]
    legacy_debug_results = copy.deepcopy(sample_run_results())
    legacy_debug_results["months"][0]["plans"] = [{"day": "2025-01-15", "leg": "single", "scenarios": {"with_predbat": {"rows": [], "soc_max": 9.5}}}]
    legacy_html = page.render_results(legacy_debug_results, legacy_runs, "legacy-plan-run")
    if "annual-plan-viewer" not in legacy_html:
        print("  ERROR: a legacy run with embedded plans and no plan_index should still render the plan viewer")
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


def test_web_annual_pages(my_predbat):
    """Verify the config, viewer and compare pages are separate, and the nav between them."""
    failed = False
    print("**** Testing web_annual pages and nav ****")

    print("Test: the config page shows the form and no results")
    page = make_page(my_predbat)
    config_html = asyncio.run(page.html_annual(FakeRequest())).text
    if 'name="solar_kwp_0"' not in config_html:
        print("  ERROR: the config page should show the form")
        failed = True
    if "Annual totals for" in config_html or "What this run used" in config_html:
        print("  ERROR: the config page must not also render the results")
        failed = True

    print("Test: the viewer shows results and no form")
    view_html = asyncio.run(page.html_annual_view(FakeRequest())).text
    if 'name="solar_kwp_0"' in view_html:
        print("  ERROR: the viewer page must not render the configuration form")
        failed = True

    print("Test: the viewer renders a stored run's own figures, selector and plan viewer")
    # The negative assertion above (no form) would still pass if html_annual_view
    # stopped calling render_results altogether - that is precisely the hole finding
    # #1 (the selector navigating to the wrong page) fell through: nothing drove the
    # viewer against a real stored run. Round-tripped through save_run/load_run/
    # list_runs, not a hand-built dict - a hand-built dict would stay green even if
    # the viewer read plans straight off the document instead of the index's
    # plan_index, a mistake already made twice on this branch.
    view_storage = RaceStorage()
    page._storage = lambda: view_storage
    stored_view_results = copy.deepcopy(sample_run_results())
    stored_view_results["months"][0]["plans"] = [{"day": "2025-01-15", "leg": "single", "scenarios": {"with_predbat": {"rows": [], "soc_max": 9.5}}}]
    asyncio.run(save_run(view_storage, stored_view_results, {}, "20260728-view-check"))
    stored_view_html = asyncio.run(page.html_annual_view(FakeRequest(query={"run": "20260728-view-check"}))).text
    page._storage = lambda: None  # restore the default so later tests in this function are unaffected
    if "Annual totals for" not in stored_view_html:
        print("  ERROR: the viewer should render the stored run's own totals heading")
        failed = True
    if "90.00" not in stored_view_html:
        print("  ERROR: the viewer should render the stored run's own PV/battery saving (9000p = £90.00)")
        failed = True
    if "24.00" not in stored_view_html:
        print("  ERROR: the viewer should render the stored run's own Predbat saving (2400p = £24.00)")
        failed = True
    if "annual-selector" not in stored_view_html:
        print("  ERROR: the viewer should show the run selector for a stored run")
        failed = True
    if "20260728-view-check" not in stored_view_html:
        print("  ERROR: the viewer should identify the selected run in the selector")
        failed = True
    if "annual-plan-viewer" not in stored_view_html:
        print("  ERROR: a stored run with a plan_index should still render the plan viewer")
        failed = True
    if "2025-01-15" not in stored_view_html:
        print("  ERROR: the plan viewer's day option should come from the stored run's plan_index")
        failed = True
    if "renderPlanTable" not in stored_view_html:
        print("  ERROR: the plan viewer must use the existing plan renderer")
        failed = True

    print("Test: the nav marks the current page and disables the end arrows")
    nav = page.render_nav("config")
    if "annual-nav-current" not in nav:
        print("  ERROR: the nav should mark the current page")
        failed = True
    if "annual-nav-disabled" not in nav:
        print("  ERROR: the arrow at the first page should be disabled, not wrap")
        failed = True
    middle = page.render_nav("view")
    if "annual-nav-disabled" in middle:
        print("  ERROR: neither arrow should be disabled on the middle page")
        failed = True
    for target in ["./annual", "./annual_view", "./annual_compare"]:
        if target not in nav:
            print("  ERROR: the nav should link to {}".format(target))
            failed = True

    print("Test: the progress area is on every page, so a run stays visible when you navigate")
    for name, rendered in [("config", config_html), ("view", view_html)]:
        if "annual-progress" not in rendered:
            print("  ERROR: the {} page should carry the progress area".format(name))
            failed = True

    print("Test: a finished run sends the tab that started it to the viewer, not the form")
    if "'./annual_view'" not in page.render_script():
        print("  ERROR: run completion should navigate to the viewer page")
        failed = True

    print("Test: the compare table lists every run with its own figures")

    def catalogue_entry(entries, entry_id):
        """Return one built-in catalogue entry by id, so fixtures cannot drift from it."""
        return [entry for entry in entries if entry["id"] == entry_id][0]

    agile = catalogue_entry(IMPORT_TARIFFS, "agile")
    cosy = catalogue_entry(IMPORT_TARIFFS, "cosy")
    price_cap = catalogue_entry(IMPORT_TARIFFS, "price_cap")
    outgoing_fixed = catalogue_entry(EXPORT_TARIFFS, "outgoing_fixed")
    no_export = catalogue_entry(EXPORT_TARIFFS, NO_EXPORT_ID)
    runs = [
        {
            "id": "20260728-0900",
            # Deliberately does not repeat "5.6"/"9.5" in the label: earlier versions of
            # this test used a label like "9.5 kWh battery, 5.6 kWp, Agile", which meant
            # the Solar/Battery assertions below were satisfied by the label text alone -
            # deleting the Solar/Battery <td> cells outright would still have passed.
            # It carries no tariff name either, for the same reason: the three tariff
            # cells must be doing the work, not the label.
            "label": "System Alpha",
            "summary": {
                "total_kwp": 5.6,
                "battery_kwh": 9.5,
                "total_gbp": 12250.0,
                "tariff": {"import_octopus_url": agile["import_octopus_url"], "export_octopus_url": outgoing_fixed["export_octopus_url"]},
                "baseline_tariff": {"rates_import": price_cap["rates_import"]},
                "cost_with_predbat_p": 66000.0,
                "saving_vs_none_p": 114000.0,
                "payback_years": {"pv_only": 17.8, "pv_battery": 13.61, "pv_battery_predbat": 11.78},
                "payback_reason": None,
                "months_included": 12,
            },
        },
        {
            "id": "20260728-0800",
            "label": "System Beta",
            "summary": {
                "total_kwp": 12.0,
                "battery_kwh": 20.0,
                "total_gbp": 25400.0,
                "quoted": True,
                "tariff": {"import_octopus_url": cosy["import_octopus_url"], "rates_export": no_export["rates_export"]},
                "baseline_tariff": {"rates_import": price_cap["rates_import"]},
                "cost_with_predbat_p": 40000.0,
                "saving_vs_none_p": 140000.0,
                "payback_years": {"pv_only": 9.1, "pv_battery": 8.2, "pv_battery_predbat": 7.0},
                "payback_reason": None,
                "months_included": 12,
            },
        },
    ]
    table = page.render_compare(runs, "20260728-0900")
    for expected in ["5.6 kWp", "9.5 kWh", "Octopus Agile", "13.6", "12 kWp", "20 kWh", "Octopus Cosy", "8.2"]:
        if expected not in table:
            print("  ERROR: the compare table should show {}, got {}".format(expected, table))
            failed = True

    print("Test: the compare table has a column each for the baseline, import and export tariffs")
    # One "Tariff" column could not describe a run once import and export became
    # independent choices: two runs differing only in their export deal looked
    # identical, and the baseline the saving is measured against was invisible.
    header = re.search(r"<tr[^>]*>.*?</tr>", table, re.S).group(0)
    for heading in ["Baseline", "Import", "Export"]:
        if "<th>{}</th>".format(heading) not in header:
            print("  ERROR: the compare table should have a {} column, got {}".format(heading, header))
            failed = True
    if "<th>Tariff</th>" in header:
        print("  ERROR: the single Tariff column should have been replaced by the three, got {}".format(header))
        failed = True

    print("Test: the tariff cells show the catalogue's own names, on the run that used them")
    alpha_row, beta_row = [row for row in re.findall(r"<tr[^>]*>.*?</tr>", table, re.S) if "<th>" not in row]
    for row, label, expected_names, unexpected_names in [
        (alpha_row, "System Alpha", ["Price cap", "Octopus Agile", "Octopus Outgoing Fixed"], ["Octopus Cosy", "No export payment"]),
        (beta_row, "System Beta", ["Price cap", "Octopus Cosy", "No export payment"], ["Octopus Agile", "Octopus Outgoing Fixed"]),
    ]:
        for name in expected_names:
            if name not in row:
                print("  ERROR: the {} row should name {}, got {}".format(label, name, row))
                failed = True
        for name in unexpected_names:
            if name in row:
                print("  ERROR: the {} row should not carry {}, got {}".format(label, name, row))
                failed = True

    print("Test: a tariff that matches no catalogue entry falls back to something readable, not a blank")
    # A hand-entered URL, or a compare_list entry the user has since deleted. The
    # product code is the part of a 130-character URL anyone recognises.
    # The export rate has to be one no EXPORT_TARIFFS entry uses, or it matches the
    # catalogue and is named rather than falling back - 15.0p stopped being unmatched
    # once EDF Export was added, which is what this rate is dodging.
    odd = [
        {
            "id": "odd",
            "label": "System Delta",
            "summary": {
                "tariff": {"import_octopus_url": "https://api.octopus.energy/v1/products/MY-OWN-DEAL-24/electricity-tariffs/x/", "rates_export": [{"rate": 21.5}]},
                "baseline_tariff": {},
                "payback_years": {},
            },
        }
    ]
    odd_table = page.render_compare(odd, "odd")
    if "MY-OWN-DEAL-24" not in odd_table:
        print("  ERROR: an unmatched import URL should fall back to its product code, got {}".format(odd_table))
        failed = True
    if "flat 21.5p" not in odd_table:
        print("  ERROR: an unmatched export rate should be described rather than blanked, got {}".format(odd_table))
        failed = True

    print("Test: a run stored before the tariff fields renders as dashes rather than raising")
    # backfill_summaries normally refreshes these, but it skips a run whose document has
    # gone; the table must still draw rather than 500 the whole compare page.
    legacy = [{"id": "legacy", "label": "System Epsilon", "summary": {"total_kwp": 4.0, "tariff": "Agile", "payback_years": {}}}]
    legacy_table = page.render_compare(legacy, "legacy")
    if "System Epsilon" not in legacy_table:
        print("  ERROR: a legacy summary should still render its row, got {}".format(legacy_table))
        failed = True

    print("Test: the compare table's own compare_list tariffs are named, not reduced to a rate")
    # The reason naming happens here rather than in annual_store: only the web layer can
    # read compare_list, so a name computed at save time would be blind to the user's
    # own tariffs.
    my_predbat.args["compare_list"] = [{"id": "mine", "name": "My works deal", "rates_import": [{"rate": 11.11}]}]
    mine = [{"id": "mine-run", "label": "System Zeta", "summary": {"tariff": {"rates_import": [{"rate": 11.11}]}, "baseline_tariff": {}, "payback_years": {}}}]
    if "My works deal" not in make_page(my_predbat).render_compare(mine, "mine-run"):
        print("  ERROR: a compare_list tariff should be named in the compare table")
        failed = True
    my_predbat.args.pop("compare_list", None)

    print("Test: only the data cells are held on one line, so the headers can wrap and the columns shrink")
    # nowrap on the whole table forced every column to at least its header width on one
    # line - "PV + battery pays back in" is ~24 characters holding open a column whose
    # data is "4.2 years". Thirteen columns of that scrolled far further than they needed.
    css = page.render_css()
    if "table.annual-compare th" not in css.replace("\n", " "):
        print("  ERROR: the compare table's headers should be allowed to wrap")
        failed = True
    if "table.annual-compare { white-space: nowrap; }" in css:
        print("  ERROR: nowrap must not apply to the whole compare table, or the headers cannot wrap")
        failed = True

    print("Test: each run's figures land in its OWN row, not merely somewhere in the table")
    # A per-table substring check (above) cannot tell "each run got its own row" apart
    # from "the rows are swapped/shifted but every value still appears somewhere" - a
    # transposed row would pass it just as cleanly as a correct table. Splitting into
    # <tr> blocks and checking each run's label sits next to ITS OWN figures - and not
    # the other run's - is what actually catches a row/summary mismatch, which is
    # exactly the defect class the global constraint calls out as having happened once
    # on this branch already.
    data_rows = [row for row in re.findall(r"<tr[^>]*>.*?</tr>", table, re.S) if "<th>" not in row]
    if len(data_rows) != len(runs):
        print("  ERROR: expected {} data rows, got {}".format(len(runs), len(data_rows)))
        failed = True
    else:
        row_expectations = [
            ("System Alpha", ["5.6 kWp", "9.5 kWh", "Octopus Agile", "13.6"], ["Octopus Cosy", "8.2", "12 kWp", "20 kWh"]),
            ("System Beta", ["12 kWp", "20 kWh", "Octopus Cosy", "8.2"], ["Octopus Agile", "13.6", "5.6 kWp", "9.5 kWh"]),
        ]
        for row, (label_fragment, must_contain, must_not_contain) in zip(data_rows, row_expectations):
            if label_fragment not in row:
                print("  ERROR: expected a row for {}, got {}".format(label_fragment, row))
                failed = True
            for expected in must_contain:
                if expected not in row:
                    print("  ERROR: the {} row should show {}, got {}".format(label_fragment, expected, row))
                    failed = True
            for unexpected in must_not_contain:
                if unexpected in row:
                    print("  ERROR: the {} row should not show {}'s figures, got {}".format(label_fragment, unexpected, row))
                    failed = True

        print("Test: the system price is shown beside the payback, and a quote is labelled")
        # A payback period means little without the outlay it repays, which is why the
        # price column exists at all.
        if "£12,250" not in data_rows[0]:
            print("  ERROR: the Agile row should show its £12,250 system cost, got {}".format(data_rows[0]))
            failed = True
        if "£25,400" not in data_rows[1] or "quoted" not in data_rows[1]:
            print("  ERROR: the Cosy row should show £25,400 marked as quoted, got {}".format(data_rows[1]))
            failed = True
        if "quoted" in data_rows[0]:
            print("  ERROR: a modelled cost must not be labelled as a quote, got {}".format(data_rows[0]))
            failed = True

        print("Test: cost and saving render as pounds, not raw pence (an off-by-100 would ship green otherwise)")
        if "£660.00" not in data_rows[0]:
            print("  ERROR: cost_with_predbat_p=66000.0p should render as £660.00 on the Agile row, got {}".format(data_rows[0]))
            failed = True
        if "£1140.00" not in data_rows[0]:
            print("  ERROR: saving_vs_none_p=114000.0p should render as £1140.00 on the Agile row, got {}".format(data_rows[0]))
            failed = True
        if "£400.00" not in data_rows[1]:
            print("  ERROR: cost_with_predbat_p=40000.0p should render as £400.00 on the Cosy row, got {}".format(data_rows[1]))
            failed = True
        if "£1400.00" not in data_rows[1]:
            print("  ERROR: saving_vs_none_p=140000.0p should render as £1400.00 on the Cosy row, got {}".format(data_rows[1]))
            failed = True

    print("Test: unknown Solar/Battery/Cost/Saving figures render as a dash, never as 0 or n/a")
    # _compare_number and _compare_money exist solely to turn a None summary figure
    # into "-" rather than "0 kWp"/"n/a" - a None kWp reading as "a system with no
    # panels" or a None saving reading as "n/a" are both worse than an honest "unknown".
    # No other fixture in this file supplies None for these four fields, so replacing
    # either helper with a plain formatter would have stayed green.
    unknown_run = [
        {
            "id": "unknown-figures",
            "label": "System Gamma, unknown figures",
            "summary": {
                "total_kwp": None,
                "battery_kwh": None,
                "tariff": "Agile",
                "cost_with_predbat_p": None,
                "saving_vs_none_p": None,
                "payback_years": {"pv_only": 5.0, "pv_battery": 4.0, "pv_battery_predbat": 3.0},
                "payback_reason": None,
                "months_included": 12,
            },
        }
    ]
    unknown_table = page.render_compare(unknown_run, "unknown-figures")
    unknown_row = [row for row in re.findall(r"<tr[^>]*>.*?</tr>", unknown_table, re.S) if "<th>" not in row][0]
    unknown_cells = re.findall(r"<td[^>]*>(.*?)</td>", unknown_row, re.S)
    # Cells are located by their COLUMN HEADING rather than a hard-coded index: adding a
    # column (the system price was added after this test was written) silently shifts
    # every index along, and the assertions then check the wrong cells while still
    # passing or failing for the wrong reason.
    headings = re.findall(r"<th[^>]*>(.*?)</th>", unknown_table, re.S)
    for heading, field in [("Solar", "total_kwp"), ("Battery", "battery_kwh"), ("Cost with Predbat", "cost_with_predbat_p"), ("Saving vs no system", "saving_vs_none_p")]:
        if heading not in headings:
            print("  ERROR: the compare table should have a {} column, got {}".format(heading, headings))
            failed = True
            continue
        cell = unknown_cells[headings.index(heading)].strip()
        if cell != "—":
            print("  ERROR: an unknown {} should render as a dash, got {!r} in cell {}".format(field, cell, unknown_cells))
            failed = True
        if cell in ("0", "0 kWp", "0 kWh", "£0.00", "n/a"):
            print("  ERROR: an unknown {} must not render as zero/n-a, got {!r}".format(field, cell))
            failed = True

    print("Test: a run whose payback was unavailable shows a dash and its reason, not a number")
    unavailable = [
        {
            "id": "x",
            "label": "partial",
            "summary": {
                "total_kwp": 5.0,
                "battery_kwh": 9.0,
                "tariff": "Agile",
                "cost_with_predbat_p": 100.0,
                "saving_vs_none_p": 50.0,
                "payback_years": {},
                "payback_reason": "Payback needs a full year, but only 11 of 12 months could be modelled.",
                "months_included": 11,
            },
        }
    ]
    text = page.render_compare(unavailable, "x")
    if "11 of 12" not in text:
        print("  ERROR: the reason payback is unavailable should be available to the user")
        failed = True
    if "0.0 years" in text or "None" in text:
        print("  ERROR: an unavailable payback must not render as a number, got {}".format(text))
        failed = True

    print("Test: a run that does not pay back says so rather than showing a number")
    never = [
        {
            "id": "y",
            "label": "never",
            "summary": {
                "total_kwp": 5.0,
                "battery_kwh": 9.0,
                "tariff": "Agile",
                "cost_with_predbat_p": 100.0,
                "saving_vs_none_p": -50.0,
                "payback_years": {"pv_only": None, "pv_battery": None, "pv_battery_predbat": None},
                "payback_reason": None,
                "months_included": 12,
            },
        }
    ]
    if "does not pay back" not in page.render_compare(never, "y"):
        print("  ERROR: a non-paying-back run should say so")
        failed = True

    print("Test: a non-numeric payback 'years' renders as a dash instead of 500ing the whole compare page")
    # build_summary type-guards cost_p but passes years through unchecked (finding #4);
    # a hand-edited or corrupted stored document with years as a string must not raise
    # out of "{:.1f} years".format(years) at render time - backfill_summaries' own
    # try/except cannot catch a render-time raise, so one bad document would take down
    # /annual_compare for every run.
    corrupt_years = [
        {
            "id": "z",
            "label": "corrupt",
            "summary": {
                "total_kwp": 5.0,
                "battery_kwh": 9.0,
                "tariff": "Agile",
                "cost_with_predbat_p": 100.0,
                "saving_vs_none_p": 50.0,
                "payback_years": {"pv_only": "17.8", "pv_battery": None, "pv_battery_predbat": 9.0},
                "payback_reason": None,
                "months_included": 12,
            },
        }
    ]
    try:
        corrupt_html = page.render_compare(corrupt_years, "z")
    except ValueError as error:
        print("  ERROR: a non-numeric payback 'years' must not raise, got {}".format(error))
        failed = True
    else:
        corrupt_row = [row for row in re.findall(r"<tr[^>]*>.*?</tr>", corrupt_html, re.S) if "<th>" not in row][0]
        corrupt_cells = re.findall(r"<td[^>]*>(.*?)</td>", corrupt_row, re.S)
        # Located by heading, not a fixed index - adding a column shifts every index and
        # would leave this checking the wrong cell.
        corrupt_headings = re.findall(r"<th[^>]*>(.*?)</th>", corrupt_html, re.S)
        pv_only_cell = corrupt_cells[corrupt_headings.index("PV only pays back in")].strip() if "PV only pays back in" in corrupt_headings else ""
        if pv_only_cell != "—":
            print("  ERROR: a non-numeric pv_only years should render as a dash, got {!r}".format(pv_only_cell))
            failed = True
        if "17.8" in corrupt_cells[6]:
            print("  ERROR: a non-numeric years must not be formatted as though it were a float, got {!r}".format(corrupt_cells[6]))
            failed = True

    print("Test: the compare table is horizontally scrollable rather than widening the page")
    if "overflow-x" not in page.render_css():
        print("  ERROR: a nine-column table needs its own scroll container")
        failed = True

    print("Test: with no stored runs the compare page says so rather than showing an empty table")
    if "No runs" not in page.render_compare([], None):
        print("  ERROR: an empty compare page should explain itself")
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
    """Verify all nine Annual routes are registered, so a typo'd path cannot ship green."""
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
        ("GET", "/annual_view"),
        ("GET", "/annual_compare"),
    }
    missing = expected - registered
    if missing:
        print("  ERROR: missing route registrations: {}".format(missing))
        failed = True

    return failed


def test_web_annual_plan_route(my_predbat):
    """Verify the plan viewer's route: resolves a captured plan, and 404s (never 500s) otherwise.

    ``./annual_plan``'s query string is attacker-controlled, so the route's underlying
    ``annual_store.load_plan`` must coerce defensively rather than let a malformed
    ``month``/``index`` raise out of the handler - a 500 here would be a defect, per the
    plan doc's own caution. ``load_plan`` itself is covered directly in
    ``test_annual_store.py``; this test covers the route that sits on top of it.
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

    print("Test: the route resolves a valid month/index/scenario to its captured plan")
    response = asyncio.run(page.html_annual_plan(FakeRequest(query={"run": "20250108-plans", "month": "1", "index": "1", "scenario": "with_predbat"})))
    if response.status != 200 or json.loads(response.body) != {"rows": [], "soc_max": 9.5}:
        print("  ERROR: expected the second plan's with_predbat scenario, got status={} body={!r}".format(response.status, response.body))
        failed = True

    print("Test: the route 404s rather than raising on a non-numeric month/index")
    for month, index in [("not-a-number", "0"), ("1", "also-not-a-number"), (None, "0"), ("1", None)]:
        try:
            result = asyncio.run(page.html_annual_plan(FakeRequest(query={"run": "20250108-plans", "month": month, "index": index, "scenario": "with_predbat"})))
        except Exception as error:  # noqa: BLE001 - the point of this test is that nothing escapes
            print("  ERROR: the route must never raise, got {} for month={!r} index={!r}".format(error, month, index))
            failed = True
            continue
        if result.status != 404:
            print("  ERROR: a malformed query should 404, got {!r}".format(result.status))
            failed = True

    print("Test: the route 404s for an out-of-range index and an unknown scenario")
    if asyncio.run(page.html_annual_plan(FakeRequest(query={"run": "20250108-plans", "month": "1", "index": "99", "scenario": "with_predbat"}))).status != 404:
        print("  ERROR: an out-of-range index should 404")
        failed = True
    if asyncio.run(page.html_annual_plan(FakeRequest(query={"run": "20250108-plans", "month": "1", "index": "0", "scenario": "not_a_scenario"}))).status != 404:
        print("  ERROR: an unknown scenario should 404")
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

    return failed
