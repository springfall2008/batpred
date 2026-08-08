# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the pv90 upside forecast scenario."""

from const import PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10, PV_SCENARIO_PV90


def test_pv90_scenario_constants():
    """The three scenario ids must be distinct, and PV10 must stay == 1 for bool compatibility."""
    failed = False
    if PV_SCENARIO_NOMINAL != 0:
        print("ERROR: PV_SCENARIO_NOMINAL is {}, expected 0".format(PV_SCENARIO_NOMINAL))
        failed = True
    if PV_SCENARIO_PV10 != 1:
        print("ERROR: PV_SCENARIO_PV10 is {}, expected 1 (must stay truthy-compatible with the old bool)".format(PV_SCENARIO_PV10))
        failed = True
    if PV_SCENARIO_PV90 != 2:
        print("ERROR: PV_SCENARIO_PV90 is {}, expected 2".format(PV_SCENARIO_PV90))
        failed = True
    return failed


def test_pv90_config_items(my_predbat):
    """pv_metric90_weight and load_scaling90 must exist, be expert-gated and carry the documented defaults."""
    failed = False
    expected = {
        "pv_metric90_weight": {"default": 0.0, "min": 0, "max": 1.0, "step": 0.01},
        "load_scaling90": {"default": 0.9, "min": 0, "max": 2.0, "step": 0.01},
    }
    by_name = {item["name"]: item for item in my_predbat.CONFIG_ITEMS}
    for name, want in expected.items():
        item = by_name.get(name)
        if not item:
            print("ERROR: config item {} is missing from CONFIG_ITEMS".format(name))
            failed = True
            continue
        if item.get("type") != "input_number":
            print("ERROR: config item {} type is {}, expected input_number".format(name, item.get("type")))
            failed = True
        if item.get("enable") != "expert_mode":
            print("ERROR: config item {} enable is {}, expected expert_mode".format(name, item.get("enable")))
            failed = True
        for key, value in want.items():
            if item.get(key) != value:
                print("ERROR: config item {} {} is {}, expected {}".format(name, key, item.get(key), value))
                failed = True
    return failed


def test_pv90_config_read(my_predbat):
    """fetch_config_options must actually read the two new attributes via get_arg, not merely leave __init__'s defaults untouched.

    Both attributes are forced to a sentinel that matches neither the PredBat.__init__ default nor the CONFIG_ITEMS
    default before fetch_config_options() is called for real. If the get_arg reads were ever deleted from fetch.py,
    the sentinel would survive the call unchanged and this test would fail - unlike a test that only inspects the
    value left over from __init__, which would pass regardless of whether the read ever happened.

    The whole instance dict is snapshotted first and restored afterwards, so this test cannot leak state - such as
    the sentinel itself, or any of the many other attributes fetch_config_options() also populates - into tests
    that run after it.
    """
    failed = False
    sentinel = -999.0
    saved_state = my_predbat.__dict__.copy()
    try:
        my_predbat.pv_metric90_weight = sentinel
        my_predbat.load_scaling90 = sentinel
        my_predbat.fetch_config_options()
        for name, default in (("pv_metric90_weight", 0.0), ("load_scaling90", 0.9)):
            value = getattr(my_predbat, name, None)
            if value != default:
                print("ERROR: {} is {} after fetch_config_options, expected the default {} (sentinel {} was not overwritten - the get_arg read is missing)".format(name, value, default, sentinel))
                failed = True
    finally:
        my_predbat.__dict__.clear()
        my_predbat.__dict__.update(saved_state)
    return failed


def run_pv90_tests(my_predbat):
    """Run all pv90 tests, returning True if any failed."""
    failed = False
    print("**** Running pv90 tests ****")
    failed |= test_pv90_scenario_constants()
    failed |= test_pv90_config_items(my_predbat)
    failed |= test_pv90_config_read(my_predbat)
    return failed
