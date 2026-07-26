# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the Annual tab's prefill and configuration handling."""

import builtins
from unittest.mock import patch

from annual import validate_config
from web import WebInterface
from web_annual import DEFAULT_CONFIG, AnnualPage


def make_page(my_predbat):
    """Return an AnnualPage backed by a WebInterface over the test fixture."""
    return AnnualPage(WebInterface(my_predbat, web_port=5054))


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
