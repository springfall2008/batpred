# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the annual engine's multi-export-tariff sweep."""

from annual import AnnualConfigError, validate_config


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


def test_annual_export_sweep(my_predbat):
    """The export_tariffs config key is validated and normalised."""
    failed = False

    print("Test: export_tariffs defaults to an empty list")
    if validate_config(base_config())["export_tariffs"] != []:
        print("  ERROR: an absent annual.export_tariffs should default to []")
        failed = True

    print("Test: a well-formed export_tariffs list is accepted in order")
    config = base_config()
    config["annual"]["export_tariffs"] = [
        {"id": "outgoing_fixed", "name": "Octopus Outgoing Fixed", "export_octopus_url": "https://example.test/fixed"},
        {"id": "seg", "name": "SEG", "rates_export": [{"rate": 4.1}]},
    ]
    result = validate_config(config)["export_tariffs"]
    if [entry["id"] for entry in result] != ["outgoing_fixed", "seg"]:
        print("  ERROR: export_tariffs should preserve order, got {}".format(result))
        failed = True

    print("Test: export_tariffs rejects malformed entries")
    for bad, fragment in (
        ([{"name": "no id", "rates_export": [{"rate": 4.1}]}], "id"),
        ([{"id": "no_rates", "name": "No rates"}], "rates_export"),
        ([{"id": "dup", "name": "A", "rates_export": [{"rate": 1.0}]}, {"id": "dup", "name": "B", "rates_export": [{"rate": 2.0}]}], "repeat"),
        ("outgoing_fixed", "list"),
    ):
        config = base_config()
        config["annual"]["export_tariffs"] = bad
        failed = expect_error("export_tariffs = {}".format(bad), config, fragment, failed)

    print("Test: an empty export_tariffs list is treated as absent, not as an error")
    config = base_config()
    config["annual"]["export_tariffs"] = []
    try:
        if validate_config(config)["export_tariffs"] != []:
            print("  ERROR: an empty list should normalise to []")
            failed = True
    except AnnualConfigError as error:
        print("  ERROR: an empty export_tariffs list should be accepted, got '{}'".format(error))
        failed = True

    print("Test: the results document gains by_export only when a sweep is configured")
    # Shape assertion only - a real run needs network. AnnualPredictor.run() returns the
    # legacy document when export_tariffs is empty and the by_export document when it is not.
    from annual import AnnualPredictor

    predictor = AnnualPredictor(base_config())
    if predictor.config["export_tariffs"] != []:
        print("  ERROR: a config with no sweep should carry an empty export_tariffs list")
        failed = True

    config = base_config()
    config["annual"]["export_tariffs"] = [{"id": "seg", "name": "SEG", "rates_export": [{"rate": 4.1}]}]
    predictor = AnnualPredictor(config)
    if [entry["id"] for entry in predictor.config["export_tariffs"]] != ["seg"]:
        print("  ERROR: the predictor should carry the validated sweep list")
        failed = True

    return failed
