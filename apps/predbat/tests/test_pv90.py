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


def _snapshot_pv_forecast_raw_sensor(my_predbat):
    """Capture the pre-test state of every place dashboard_item() mutates for the raw PV forecast sensor.

    dashboard_item() (output.py) touches three separate stores, not one: it writes state+attributes into the
    mocked HA state layer (my_predbat.ha_interface.dummy_items via set_state_wrapper), appends the entity id to
    self.dashboard_index if not already present, and unconditionally overwrites self.dashboard_values[entity_id].
    All three must be captured here and restored by _restore_pv_forecast_raw_sensor, or a test that publishes to
    this entity leaks its fixture into every test that runs afterwards in the shared-instance TEST_REGISTRY loop
    - including web.py surfaces that read dashboard_values for this entity directly.
    """
    entity_id = "sensor." + my_predbat.prefix + "_pv_forecast_raw"
    return {
        "entity_id": entity_id,
        "had_dummy_item": entity_id in my_predbat.ha_interface.dummy_items,
        "dummy_item": my_predbat.ha_interface.dummy_items.get(entity_id),
        "had_index_entry": entity_id in my_predbat.dashboard_index,
        "had_dashboard_value": entity_id in my_predbat.dashboard_values,
        "dashboard_value": my_predbat.dashboard_values.get(entity_id),
    }


def _restore_pv_forecast_raw_sensor(my_predbat, snapshot):
    """Undo every mutation dashboard_item() made to the raw PV forecast sensor, from a snapshot taken beforehand.

    Restores all three stores dashboard_item() can touch: ha_interface.dummy_items, dashboard_index membership
    and dashboard_values. See _snapshot_pv_forecast_raw_sensor for why all three matter.
    """
    entity_id = snapshot["entity_id"]
    if snapshot["had_dummy_item"]:
        my_predbat.ha_interface.dummy_items[entity_id] = snapshot["dummy_item"]
    else:
        my_predbat.ha_interface.dummy_items.pop(entity_id, None)
    if not snapshot["had_index_entry"] and entity_id in my_predbat.dashboard_index:
        my_predbat.dashboard_index.remove(entity_id)
    if snapshot["had_dashboard_value"]:
        my_predbat.dashboard_values[entity_id] = snapshot["dashboard_value"]
    else:
        my_predbat.dashboard_values.pop(entity_id, None)


def test_pv90_forecast_fallback_to_p50(my_predbat):
    """With no forecast90 attribute published, pv_forecast_minute90 must be a copy of the p50 series.

    Publishing the raw forecast sensor mutates three shared stores on my_predbat (see
    _snapshot_pv_forecast_raw_sensor), which persist for the rest of the test run since every test in
    TEST_REGISTRY shares one PredBat instance. All three are snapshotted and restored afterwards so this test
    cannot leak the tiny two-point forecast used here into any test that runs later and happens to read the same
    sensor's state, dashboard_index membership or dashboard_values entry.
    """
    failed = False
    entity_id = "sensor." + my_predbat.prefix + "_pv_forecast_raw"
    snapshot = _snapshot_pv_forecast_raw_sensor(my_predbat)
    try:
        my_predbat.dashboard_item(
            entity_id,
            state=0,
            attributes={
                "relative_time": my_predbat.midnight_utc.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "forecast": {"0": 0.01, "60": 0.02},
                "forecast10": {"0": 0.005, "60": 0.01},
            },
        )
        pv50, pv10, pv90 = my_predbat.fetch_pv_forecast()
        if not pv50:
            print("ERROR: p50 series is empty, test setup failed")
            return True
        for minute, value in pv50.items():
            if pv90.get(minute) != value:
                print("ERROR: pv90[{}] is {}, expected the p50 value {} (fallback must copy p50)".format(minute, pv90.get(minute), value))
                failed = True
                break
        if pv90 is pv50:
            print("ERROR: pv90 is the same object as pv50 - must be a copy so later scaling cannot alias")
            failed = True
    finally:
        _restore_pv_forecast_raw_sensor(my_predbat, snapshot)
    return failed


def test_pv90_forecast_uses_published_p90(my_predbat):
    """When forecast90 is published it must be used verbatim, not the p50 fallback.

    Same shared-state hazard as test_pv90_forecast_fallback_to_p50 above: all three stores dashboard_item()
    mutates for the raw forecast sensor (mock HA state, dashboard_index, dashboard_values) are snapshotted and
    restored so this test cannot leak its published forecast90 attribute into later tests.
    """
    failed = False
    entity_id = "sensor." + my_predbat.prefix + "_pv_forecast_raw"
    snapshot = _snapshot_pv_forecast_raw_sensor(my_predbat)
    try:
        my_predbat.dashboard_item(
            entity_id,
            state=0,
            attributes={
                "relative_time": my_predbat.midnight_utc.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "forecast": {"0": 0.01, "60": 0.02},
                "forecast10": {"0": 0.005, "60": 0.01},
                "forecast90": {"0": 0.03, "60": 0.04},
            },
        )
        pv50, pv10, pv90 = my_predbat.fetch_pv_forecast()
        if pv90.get(0) != 0.03:
            print("ERROR: pv90[0] is {}, expected the published 0.03".format(pv90.get(0)))
            failed = True
        if pv90.get(60) != 0.04:
            print("ERROR: pv90[60] is {}, expected the published 0.04".format(pv90.get(60)))
            failed = True
        if pv90.get(0) == pv50.get(0):
            print("ERROR: pv90 fell back to p50 despite forecast90 being published")
            failed = True
    finally:
        _restore_pv_forecast_raw_sensor(my_predbat, snapshot)
    return failed


def test_pv90_step_arrays_built(my_predbat):
    """calculate_plan must build p90 step arrays with the same keys as the nominal ones."""
    failed = False
    if not hasattr(my_predbat, "pv_forecast_minute90_step"):
        print("ERROR: pv_forecast_minute90_step was not built")
        return True
    if not hasattr(my_predbat, "load_minutes_step90"):
        print("ERROR: load_minutes_step90 was not built")
        return True
    if set(my_predbat.pv_forecast_minute90_step.keys()) != set(my_predbat.pv_forecast_minute_step.keys()):
        print("ERROR: pv_forecast_minute90_step keys differ from pv_forecast_minute_step keys")
        failed = True
    if set(my_predbat.load_minutes_step90.keys()) != set(my_predbat.load_minutes_step.keys()):
        print("ERROR: load_minutes_step90 keys differ from load_minutes_step keys")
        failed = True
    total = sum(my_predbat.load_minutes_step.values())
    total90 = sum(my_predbat.load_minutes_step90.values())
    if total > 0 and total90 >= total:
        print("ERROR: load_minutes_step90 total {} is not below the nominal {} - load_scaling90 was not applied".format(total90, total))
        failed = True
    return failed


def test_pv90_missing_series_falls_back_to_p50(my_predbat):
    """A replayed debug dump has no pv_forecast_minute90; the plan must fall back to the p50 series, not silently zero it out.

    step_data_history's forward-mode read is `item.get(minute + minutes_now + offset, 0.0)` - a dict .get() with a
    default, so an empty pv_forecast_minute90 can never raise KeyError there; without the guard it would instead
    silently produce an all-zero step array, which inverts pv90's meaning (a "high PV" scenario predicting zero
    PV). Asserting only that no exception is raised cannot catch that failure mode, so this test compares the
    actual values: after blanking pv_forecast_minute90 and recomputing, pv_forecast_minute90_step must equal
    pv_forecast_minute_step exactly (the guard copies p50 verbatim, and step_data_history is deterministic given
    identical inputs).

    The shared my_predbat fixture carries an empty pv_forecast_minute by default (nothing in run_pv90_tests'
    earlier calls populates it), which would make that comparison vacuous - both sides would be all-zero
    regardless of whether the guard runs. This test therefore seeds a synthetic non-zero pv_forecast_minute of
    its own before recomputing, so the comparison is meaningful independent of fixture state, and restores it
    (alongside pv_forecast_minute90) in the finally block. A sanity check also confirms the resulting nominal
    step array is not itself all zero, so the comparison cannot pass vacuously even if the seeding is broken.
    """
    failed = False
    saved_pv90 = my_predbat.pv_forecast_minute90
    saved_pv50 = my_predbat.pv_forecast_minute
    minutes_now = my_predbat.minutes_now
    horizon = my_predbat.forecast_minutes + my_predbat.plan_interval_minutes + 10
    my_predbat.pv_forecast_minute = {minutes_now + offset: 0.01 for offset in range(horizon)}
    my_predbat.pv_forecast_minute90 = {}
    try:
        my_predbat.calculate_plan(recompute=True)
        nominal_total = sum(my_predbat.pv_forecast_minute_step.values())
        if nominal_total <= 0:
            print("ERROR: pv_forecast_minute_step is all zero despite seeding a synthetic PV series - the fallback comparison would be vacuous")
            return True
        if my_predbat.pv_forecast_minute90_step != my_predbat.pv_forecast_minute_step:
            print("ERROR: pv_forecast_minute90_step does not match the p50 fallback pv_forecast_minute_step - the guard is missing or broken (falling back to silent zeros, not p50)")
            failed = True
    except KeyError as error:
        print("ERROR: calculate_plan raised KeyError {} with an empty pv_forecast_minute90 - the p50 guard is missing".format(error))
        failed = True
    finally:
        my_predbat.pv_forecast_minute90 = saved_pv90
        my_predbat.pv_forecast_minute = saved_pv50
    return failed


def run_pv90_tests(my_predbat):
    """Run all pv90 tests, returning True if any failed."""
    failed = False
    print("**** Running pv90 tests ****")
    failed |= test_pv90_scenario_constants()
    failed |= test_pv90_config_items(my_predbat)
    failed |= test_pv90_config_read(my_predbat)
    failed |= test_pv90_forecast_fallback_to_p50(my_predbat)
    failed |= test_pv90_forecast_uses_published_p90(my_predbat)
    my_predbat.calculate_plan(recompute=True)
    failed |= test_pv90_step_arrays_built(my_predbat)
    failed |= test_pv90_missing_series_falls_back_to_p50(my_predbat)
    return failed
