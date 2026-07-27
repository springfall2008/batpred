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
import re
from unittest.mock import patch

from aiohttp import web as aiohttp_web

from annual import validate_config
from tariff_catalogue import CUSTOM_ID
from web import WebInterface
from web_annual import DEFAULT_CONFIG, AnnualPage


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
    page._running_config = page.prefill_config()

    async def poll_twice_concurrently():
        """Run two status_payload() polls concurrently on the same event loop."""
        return await asyncio.gather(page.status_payload(), page.status_payload())

    print("Test: two concurrent polls save the finished run exactly once")
    first, second = asyncio.run(poll_twice_concurrently())
    run_saves = [call for call in storage.save_calls if call[1].startswith("run_")]
    if len(run_saves) != 1:
        print("  ERROR: two concurrent polls should save the finished run exactly once, got {} save() calls: {}".format(len(run_saves), storage.save_calls))
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


def sample_run_results():
    """Return a results document covering an ok, a degraded and an unavailable month."""
    scenarios = {
        "no_pvbat": {"cost_p": 18000.0, "import_kwh": 400.0, "export_kwh": 0.0, "pv_generated_kwh": 0.0, "battery_throughput_kwh": 0.0, "export_credit_p_estimate": 0.0, "self_consumed_kwh": 0.0, "self_consumed_kwh_meaningful": True},
        "without_predbat": {"cost_p": 9000.0, "import_kwh": 300.0, "export_kwh": 20.0, "pv_generated_kwh": 120.0, "battery_throughput_kwh": 90.0, "export_credit_p_estimate": 300.0, "self_consumed_kwh": 100.0, "self_consumed_kwh_meaningful": True},
        "with_predbat": {"cost_p": 6600.0, "import_kwh": 280.0, "export_kwh": 145.0, "pv_generated_kwh": 120.0, "battery_throughput_kwh": 140.0, "export_credit_p_estimate": 675.0, "self_consumed_kwh": 0.0, "self_consumed_kwh_meaningful": False},
    }
    return {
        "year": 2025,
        "months": [
            {"month": 1, "status": "ok", "days": 31, "sampled_days": ["2025-01-08", "2025-01-24"], "standing_charge_p": 1860.0, "scenarios": scenarios},
            {"month": 2, "status": "degraded", "days": 28, "failed_days": ["2025-02-14"], "standing_charge_p": 1680.0, "scenarios": scenarios},
            {"month": 3, "status": "unavailable", "reason": "no rate data available", "days": 31, "standing_charge_p": 1860.0},
        ],
        "annual": {"scenarios": scenarios, "standing_charge_p": 3540.0, "savings": {"pv_battery_vs_none_p": 9000.0, "predbat_vs_baseline_p": 2400.0}, "months_included": 2, "months_excluded": [3]},
        "caveats": ["An example caveat about the P10 fallback."],
    }


def test_web_annual_results(my_predbat):
    """Verify the results view: totals, chart series, month statuses, caveats, selector."""
    failed = False
    print("**** Testing web_annual results ****")

    page = make_page(my_predbat)
    runs = [{"id": "20260726-101500", "label": "9.5kWh battery · 5.6kWp · Agile", "months_included": 12}, {"id": "20260725-090000", "label": "no battery · 5.6kWp · Agile", "months_included": 12}]
    html = page.render_results(sample_run_results(), runs, "20260726-101500")

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

    print("Test: self_consumed_kwh is qualified when it is not meaningful")
    if "not meaningful" not in html.lower():
        print("  ERROR: a non-meaningful self-consumption figure should be qualified, not shown bare")
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

    print("Test: with no runs at all the view says so rather than rendering an empty chart")
    empty = page.render_results(None, [], None)
    if "apexcharts" in empty.lower() and "series" in empty.lower():
        print("  ERROR: no chart should be drawn when there are no results")
        failed = True
    if "no results" not in empty.lower():
        print("  ERROR: the empty state should say there are no results yet")
        failed = True

    return failed


def test_web_annual_routes_registered(my_predbat):
    """Verify all six Annual routes are registered, so a typo'd path cannot ship green."""
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
    }
    missing = expected - registered
    if missing:
        print("  ERROR: missing route registrations: {}".format(missing))
        failed = True

    return failed
