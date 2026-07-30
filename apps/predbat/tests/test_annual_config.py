# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for annual prediction config validation."""

from datetime import date

from annual import AnnualConfigError, scrub_secrets, validate_config


def base_config():
    """Return a minimal valid annual config."""
    return {
        "annual": {
            "location": {"latitude": 51.5, "longitude": -0.1},
            "solar": [{"kwp": 5.6}],
            "battery": {"size_kwh": 9.5, "inverter_kw": 5.0},
            "load": {"annual_kwh": 3800},
            "tariff": {"rates_import": [{"rate": 25.0}]},
        }
    }


def expect_error(label, config, fragment, failed):
    """Assert that validate_config rejects the config with a message containing fragment."""
    try:
        validate_config(config)
    except AnnualConfigError as error:
        if fragment.lower() not in str(error).lower():
            print("  ERROR: {} raised '{}', expected it to mention '{}'".format(label, error, fragment))
            return True
        return failed
    print("  ERROR: {} should have raised AnnualConfigError".format(label))
    return True


def expect_config_error_type(label, config, failed):
    """Assert that validate_config raises AnnualConfigError specifically, not a bare ValueError/TypeError.

    A malformed numeric field must be caught and re-raised as AnnualConfigError so that a
    later CLI layer, which only catches that type, never sees a raw traceback.
    """
    try:
        validate_config(config)
    except AnnualConfigError:
        return failed
    except (ValueError, TypeError) as error:
        print("  ERROR: {} raised a bare {} ('{}') instead of AnnualConfigError".format(label, type(error).__name__, error))
        return True
    print("  ERROR: {} should have raised AnnualConfigError".format(label))
    return True


def test_annual_config(my_predbat):
    """Verify annual config defaulting, normalisation and rejection rules."""
    failed = False
    print("**** Testing annual config validation ****")

    print("Test: a minimal config validates and gains defaults")
    result = validate_config(base_config(), today=date(2026, 7, 25))
    if result["year"] != 2025:
        print("  ERROR: year should default to the most recent complete calendar year, got {}".format(result["year"]))
        failed = True
    if result["samples_per_month"] != 2:
        print("  ERROR: samples_per_month should default to 2, got {}".format(result["samples_per_month"]))
        failed = True
    if result["timezone"] != "Europe/London":
        print("  ERROR: timezone should default to Europe/London, got {}".format(result["timezone"]))
        failed = True
    if abs(result["pv10_derate_fallback"] - 0.7) > 1e-9:
        print("  ERROR: pv10_derate_fallback should default to 0.7, got {}".format(result["pv10_derate_fallback"]))
        failed = True
    if result["load"]["shape"] != "flat":
        print("  ERROR: load shape should default to flat, got {}".format(result["load"]["shape"]))
        failed = True
    if result["solar"][0]["declination"] != 35 or result["solar"][0]["azimuth"] != 180:
        print("  ERROR: solar defaults should be declination 35 azimuth 180, got {}".format(result["solar"][0]))
        failed = True
    if abs(result["solar"][0]["efficiency"] - 0.95) > 1e-9:
        print("  ERROR: solar efficiency should default to 0.95, got {}".format(result["solar"][0]["efficiency"]))
        failed = True
    if result["battery"]["charge_rate_kw"] != 5.0 or result["battery"]["discharge_rate_kw"] != 5.0:
        print("  ERROR: charge and discharge rates should default to inverter_kw, got {}".format(result["battery"]))
        failed = True
    if result["battery"]["export_limit_kw"] != 5.0:
        print("  ERROR: export_limit_kw should default to inverter_kw, got {}".format(result["battery"]))
        failed = True
    if result["battery"]["hybrid"] is not True:
        print("  ERROR: hybrid should default to True, got {}".format(result["battery"].get("hybrid")))
        failed = True

    print("Test: an unwrapped config without the 'annual' key is accepted")
    unwrapped = base_config()["annual"]
    result = validate_config(unwrapped, today=date(2026, 7, 25))
    if result["year"] != 2025:
        print("  ERROR: an unwrapped config should validate the same way")
        failed = True

    print("Test: Octopus load together with a manual figure is rejected")
    config = base_config()
    config["annual"]["load"]["octopus"] = {"api_key": "sk_x", "account_id": "A-1"}
    failed = expect_error("octopus plus annual_kwh", config, "mutually exclusive", failed)

    config = base_config()
    del config["annual"]["load"]["annual_kwh"]
    config["annual"]["load"]["car_charging_kwh"] = 2500
    config["annual"]["load"]["octopus"] = {"api_key": "sk_x", "account_id": "A-1"}
    failed = expect_error("octopus plus car_charging_kwh", config, "mutually exclusive", failed)

    print("Test: an empty octopus block alongside a manual figure is still rejected as mutually exclusive")
    config = base_config()
    config["annual"]["load"]["octopus"] = {}
    failed = expect_error("empty octopus plus annual_kwh", config, "mutually exclusive", failed)

    print("Test: an Octopus-only load block validates")
    config = base_config()
    del config["annual"]["load"]["annual_kwh"]
    config["annual"]["load"]["octopus"] = {"api_key": "sk_x", "account_id": "A-1"}
    result = validate_config(config, today=date(2026, 7, 25))
    if result["load"].get("octopus", {}).get("account_id") != "A-1":
        print("  ERROR: the Octopus load block should survive validation")
        failed = True

    print("Test: car_rate_kw is meaningless on an Octopus load (no separate car energy to rate) and is simply ignored")
    config = base_config()
    del config["annual"]["load"]["annual_kwh"]
    config["annual"]["load"]["octopus"] = {"api_key": "sk_x", "account_id": "A-1"}
    config["annual"]["load"]["car_rate_kw"] = 3.7
    result = validate_config(config, today=date(2026, 7, 25))
    if "car_rate_kw" in result["load"]:
        print("  ERROR: car_rate_kw should not appear in an Octopus load block, got {}".format(result["load"]))
        failed = True

    print("Test: a missing battery block yields a two-scenario run")
    config = base_config()
    del config["annual"]["battery"]
    result = validate_config(config, today=date(2026, 7, 25))
    if result["battery"] is not None:
        print("  ERROR: an omitted battery should normalise to None, got {}".format(result["battery"]))
        failed = True

    print("Test: a missing solar block is allowed for a battery-only run")
    config = base_config()
    del config["annual"]["solar"]
    result = validate_config(config, today=date(2026, 7, 25))
    if result["solar"] != []:
        print("  ERROR: an omitted solar block should normalise to an empty list, got {}".format(result["solar"]))
        failed = True

    print("Test: omitting both solar and battery is rejected as pointless")
    config = base_config()
    del config["annual"]["solar"]
    del config["annual"]["battery"]
    failed = expect_error("neither solar nor battery", config, "at least one of solar or battery", failed)

    print("Test: missing location is rejected")
    config = base_config()
    del config["annual"]["location"]
    failed = expect_error("no location", config, "annual.location is required", failed)

    print("Test: missing load is rejected")
    config = base_config()
    del config["annual"]["load"]
    failed = expect_error("no load", config, "annual.load is required", failed)

    print("Test: missing tariff is rejected")
    config = base_config()
    del config["annual"]["tariff"]
    failed = expect_error("no tariff", config, "annual.tariff is required", failed)

    print("Test: a broken baseline_tariff names the baseline block, not the main tariff")
    # Both blocks go through the same validator. Reporting every baseline problem against
    # "annual.tariff" sent the user to fix a block that was perfectly valid - the one
    # thing an error message must not do.
    for broken, fragment, label in [
        ({"rates_export": [{"rate": 4.1}]}, "annual.baseline_tariff requires either import_octopus_url or rates_import", "no import source"),
        ({"import_octopus_url": "https://example.com/{dno_region}/x/"}, "annual.baseline_tariff.import_octopus_url uses {dno_region}", "templated with no region"),
        ({"rates_import": [{"rate": 26.11}], "standing_charge_p_per_day": "abc"}, "annual.baseline_tariff.standing_charge_p_per_day", "non-numeric standing charge"),
        ("not-a-dict", "annual.baseline_tariff is required and must be a mapping", "not a mapping"),
    ]:
        config = base_config()
        config["annual"]["baseline_tariff"] = broken
        failed = expect_error("baseline {}".format(label), config, fragment, failed)

    print("Test: the main tariff's own messages still name the main tariff")
    # The parameterised path must not have shifted the ordinary case onto the baseline.
    config = base_config()
    config["annual"]["tariff"] = {"rates_export": [{"rate": 4.1}]}
    failed = expect_error("main tariff with no import source", config, "annual.tariff requires either import_octopus_url or rates_import", failed)
    config = base_config()
    config["annual"]["tariff"]["import_octopus_url"] = "https://example.com/{dno_region}/x/"
    config["annual"]["tariff"].pop("dno_region", None)
    failed = expect_error("main tariff templated with no region", config, "annual.tariff.import_octopus_url uses {dno_region}", failed)

    print("Test: car_rate_kw defaults to 7.4 kW when omitted")
    config = base_config()
    result = validate_config(config, today=date(2026, 7, 25))
    if result["load"]["car_rate_kw"] != 7.4:
        print("  ERROR: car_rate_kw should default to 7.4, got {}".format(result["load"]["car_rate_kw"]))
        failed = True

    print("Test: an explicit car_rate_kw survives validation")
    config = base_config()
    config["annual"]["load"]["car_rate_kw"] = 3.7
    result = validate_config(config, today=date(2026, 7, 25))
    if result["load"]["car_rate_kw"] != 3.7:
        print("  ERROR: car_rate_kw should survive validation as 3.7, got {}".format(result["load"]["car_rate_kw"]))
        failed = True

    print("Test: a zero car_rate_kw is rejected")
    config = base_config()
    config["annual"]["load"]["car_rate_kw"] = 0
    failed = expect_error("zero car_rate_kw", config, "car_rate_kw", failed)

    print("Test: a negative car_rate_kw is rejected")
    config = base_config()
    config["annual"]["load"]["car_rate_kw"] = -3.7
    failed = expect_error("negative car_rate_kw", config, "car_rate_kw", failed)

    print("Test: an unknown load shape is rejected")
    config = base_config()
    config["annual"]["load"]["shape"] = "sideways"
    failed = expect_error("bad shape", config, "annual.load.shape must be one of", failed)

    print("Test: a solar array without kwp is rejected")
    config = base_config()
    config["annual"]["solar"] = [{"declination": 30}]
    failed = expect_error("array without kwp", config, "is missing kwp", failed)

    print("Test: a panel count derives kwp at 400 W a panel")
    config = base_config()
    config["annual"]["solar"] = [{"panels": 13}]
    validated = validate_config(config)
    if abs(validated["solar"][0]["kwp"] - 5.2) > 0.001:
        print("  ERROR: 13 panels at 400 W should be 5.2 kWp, got {}".format(validated["solar"][0]["kwp"]))
        failed = True

    print("Test: a custom panel wattage is honoured")
    config = base_config()
    config["annual"]["solar"] = [{"panels": 10, "panel_watts": 450}]
    validated = validate_config(config)
    if abs(validated["solar"][0]["kwp"] - 4.5) > 0.001:
        print("  ERROR: 10 panels at 450 W should be 4.5 kWp, got {}".format(validated["solar"][0]["kwp"]))
        failed = True

    print("Test: the panel count and wattage survive validation for form round-tripping")
    if validated["solar"][0].get("panels") != 10 or validated["solar"][0].get("panel_watts") != 450:
        print("  ERROR: panels/panel_watts should be retained, got {}".format(validated["solar"][0]))
        failed = True

    print("Test: supplying both kwp and panels is rejected rather than silently preferring one")
    config = base_config()
    config["annual"]["solar"] = [{"kwp": 5.0, "panels": 13}]
    try:
        validate_config(config)
        print("  ERROR: supplying both kwp and panels should be rejected")
        failed = True
    except AnnualConfigError as error:
        if "panels" not in str(error) or "kwp" not in str(error):
            print("  ERROR: the error should name both fields, got {}".format(error))
            failed = True

    print("Test: an array with neither kwp nor panels is rejected")
    config = base_config()
    config["annual"]["solar"] = [{"declination": 35}]
    try:
        validate_config(config)
        print("  ERROR: an array with neither kwp nor panels should be rejected")
        failed = True
    except AnnualConfigError:
        pass

    print("Test: a fractional or zero panel count is rejected")
    for bad in [0, 2.5, -3]:
        config = base_config()
        config["annual"]["solar"] = [{"panels": bad}]
        try:
            validate_config(config)
            print("  ERROR: a panel count of {} should be rejected".format(bad))
            failed = True
        except AnnualConfigError:
            pass

    print("Test: samples_per_month below 1 is rejected")
    config = base_config()
    config["annual"]["samples_per_month"] = 0
    failed = expect_error("zero samples", config, "annual.samples_per_month must be at least", failed)

    print("Test: a postcode-only location validates")
    config = base_config()
    config["annual"]["location"] = {"postcode": "SW1A 1AA"}
    result = validate_config(config, today=date(2026, 7, 25))
    if result["location"].get("postcode") != "SW1A 1AA":
        print("  ERROR: a postcode location should survive validation")
        failed = True

    print("Test: a templated tariff URL without dno_region is rejected up front")
    config = base_config()
    config["annual"]["tariff"] = {"import_octopus_url": "https://api.octopus.energy/v1/products/AGILE/electricity-tariffs/E-1R-AGILE-{dno_region}/standard-unit-rates/"}
    failed = expect_error("templated url without region", config, "dno_region is not set", failed)

    print("Test: a templated tariff URL with dno_region validates and is carried through")
    config = base_config()
    config["annual"]["tariff"] = {"import_octopus_url": "https://api.octopus.energy/v1/products/AGILE/electricity-tariffs/E-1R-AGILE-{dno_region}/standard-unit-rates/", "dno_region": "A"}
    result = validate_config(config, today=date(2026, 7, 25))
    if result["tariff"].get("dno_region") != "A":
        print("  ERROR: dno_region should survive validation, got {}".format(result["tariff"].get("dno_region")))
        failed = True

    print("Test: both import and export templated URLs without dno_region are both named in the error")
    config = base_config()
    config["annual"]["tariff"] = {
        "import_octopus_url": "https://api.octopus.energy/v1/products/AGILE/electricity-tariffs/E-1R-AGILE-{dno_region}/standard-unit-rates/",
        "export_octopus_url": "https://api.octopus.energy/v1/products/AGILE-OUTGOING/electricity-tariffs/E-1R-AGILE-OUTGOING-{dno_region}/standard-unit-rates/",
    }
    try:
        validate_config(config, today=date(2026, 7, 25))
        print("  ERROR: both templated URLs without dno_region should have raised AnnualConfigError")
        failed = True
    except AnnualConfigError as error:
        message = str(error)
        if "import_octopus_url" not in message or "export_octopus_url" not in message:
            print("  ERROR: the dno_region error should name both offending fields, got '{}'".format(message))
            failed = True

    print("Test: a non-numeric kwp raises AnnualConfigError, not a bare ValueError")
    config = base_config()
    config["annual"]["solar"] = [{"kwp": "not-a-number"}]
    failed = expect_config_error_type("non-numeric kwp", config, failed)

    print("Test: a None kwp raises AnnualConfigError, not a bare TypeError")
    config = base_config()
    config["annual"]["solar"] = [{"kwp": None}]
    failed = expect_config_error_type("None kwp", config, failed)

    print("Test: a non-numeric size_kwh raises AnnualConfigError, not a bare ValueError")
    config = base_config()
    config["annual"]["battery"]["size_kwh"] = "lots"
    failed = expect_config_error_type("non-numeric size_kwh", config, failed)

    print("Test: a None size_kwh raises AnnualConfigError, not a bare TypeError")
    config = base_config()
    config["annual"]["battery"]["size_kwh"] = None
    failed = expect_config_error_type("None size_kwh", config, failed)

    print("Test: a negative size_kwh is rejected")
    config = base_config()
    config["annual"]["battery"]["size_kwh"] = -5
    failed = expect_error("negative size_kwh", config, "size_kwh", failed)

    print("Test: a zero kwp is rejected")
    config = base_config()
    config["annual"]["solar"] = [{"kwp": 0}]
    failed = expect_error("zero kwp", config, "kwp", failed)

    print("Test: an efficiency above 1 is rejected")
    config = base_config()
    config["annual"]["solar"] = [{"kwp": 5.6, "efficiency": 1.5}]
    failed = expect_error("efficiency 1.5", config, "efficiency", failed)

    print("Test: a year in the future is rejected")
    config = base_config()
    config["annual"]["year"] = 2027
    try:
        validate_config(config, today=date(2026, 7, 25))
        print("  ERROR: future year should have raised AnnualConfigError")
        failed = True
    except AnnualConfigError as error:
        if "annual.year must be at most" not in str(error):
            print("  ERROR: future year raised '{}', expected it to mention 'annual.year must be at most'".format(error))
            failed = True

    print("Test: a string declination and azimuth (as the web form always sends) are coerced to numbers")
    # The web form deliberately submits every field as a string (see web_annual.py's
    # config_from_post()); validate_config() is the single place that coerces and range
    # checks, so a string here must survive as a number, not crash deep inside
    # convert_azimuth() (solar_model.py) minutes into a run.
    config = base_config()
    config["annual"]["solar"] = [{"kwp": 5.6, "declination": "40", "azimuth": "170"}]
    result = validate_config(config, today=date(2026, 7, 25))
    declination = result["solar"][0]["declination"]
    azimuth = result["solar"][0]["azimuth"]
    if not isinstance(declination, (int, float)) or isinstance(declination, bool) or declination != 40:
        print("  ERROR: a string declination should coerce to the number 40, got {!r}".format(declination))
        failed = True
    if not isinstance(azimuth, (int, float)) or isinstance(azimuth, bool) or azimuth != 170:
        print("  ERROR: a string azimuth should coerce to the number 170, got {!r}".format(azimuth))
        failed = True

    print("Test: a negative azimuth within bounds validates (convert_azimuth's negative branch)")
    config = base_config()
    config["annual"]["solar"] = [{"kwp": 5.6, "azimuth": "-170"}]
    result = validate_config(config, today=date(2026, 7, 25))
    if result["solar"][0]["azimuth"] != -170:
        print("  ERROR: a negative azimuth should survive validation, got {}".format(result["solar"][0]["azimuth"]))
        failed = True

    print("Test: a declination outside 0-90 degrees is rejected")
    config = base_config()
    config["annual"]["solar"] = [{"kwp": 5.6, "declination": 95}]
    failed = expect_error("declination out of range", config, "declination", failed)

    print("Test: an azimuth outside -360 to 360 degrees is rejected")
    config = base_config()
    config["annual"]["solar"] = [{"kwp": 5.6, "azimuth": 400}]
    failed = expect_error("azimuth out of range", config, "azimuth", failed)

    print("Test: string latitude and longitude (as the web form always sends) are coerced to numbers")
    config = base_config()
    config["annual"]["location"] = {"latitude": "51.5", "longitude": "-0.1"}
    result = validate_config(config, today=date(2026, 7, 25))
    latitude = result["location"]["latitude"]
    longitude = result["location"]["longitude"]
    if not isinstance(latitude, (int, float)) or isinstance(latitude, bool) or abs(latitude - 51.5) > 1e-9:
        print("  ERROR: a string latitude should coerce to the number 51.5, got {!r}".format(latitude))
        failed = True
    if not isinstance(longitude, (int, float)) or isinstance(longitude, bool) or abs(longitude - (-0.1)) > 1e-9:
        print("  ERROR: a string longitude should coerce to the number -0.1, got {!r}".format(longitude))
        failed = True

    print("Test: a latitude outside -90 to 90 degrees is rejected")
    config = base_config()
    config["annual"]["location"] = {"latitude": 95, "longitude": 0}
    failed = expect_error("latitude out of range", config, "latitude", failed)

    print("Test: a longitude outside -180 to 180 degrees is rejected")
    config = base_config()
    config["annual"]["location"] = {"latitude": 0, "longitude": 200}
    failed = expect_error("longitude out of range", config, "longitude", failed)

    print("Test: a negative costs value raises AnnualConfigError, not a bare ValueError")
    # _validated_costs() must translate annual_costs.resolve_costs()'s ValueError into
    # AnnualConfigError - the CLI and web layer only catch the latter, so a config
    # problem inside the costs block must surface the same way every other config
    # mistake in this file does, not leak the pure module's own exception type.
    config = base_config()
    config["annual"]["costs"] = {"battery_install_gbp": -100}
    failed = expect_config_error_type("negative costs value", config, failed)

    print("Test: an unrecognised costs key raises AnnualConfigError, not a bare ValueError")
    config = base_config()
    config["annual"]["costs"] = {"not_a_real_setting": 100}
    failed = expect_config_error_type("unrecognised costs key", config, failed)

    print("Test: scrub_secrets removes API keys without mutating the original")
    config = base_config()
    config["annual"]["load"]["octopus"] = {"api_key": "sk_live_secret", "account_id": "A-1"}
    scrubbed = scrub_secrets(config)
    if scrubbed["annual"]["load"]["octopus"]["api_key"] != "xxx":
        print("  ERROR: api_key should be scrubbed, got {}".format(scrubbed["annual"]["load"]["octopus"]["api_key"]))
        failed = True
    if config["annual"]["load"]["octopus"]["api_key"] != "sk_live_secret":
        print("  ERROR: scrub_secrets must not mutate its input")
        failed = True
    if scrubbed["annual"]["load"]["octopus"]["account_id"] != "A-1":
        print("  ERROR: non-secret values should survive scrubbing")
        failed = True

    return failed
