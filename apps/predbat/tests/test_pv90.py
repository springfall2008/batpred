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
from tests.test_infra import reset_inverter, reset_rates


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


def _force_python_engine(pred):
    """Force a Prediction onto the pure-Python engine, bypassing the C++ kernel.

    Setting prediction_kernel_enable = False alone is not sufficient here: fetch.py defaults the shared
    my_predbat fixture's prediction_kernel_enable to True, so Prediction.__init__ already built a
    kernel_handle before this function runs, and kernel_supported() gates on kernel_handle - not on the
    enable flag, so both must be cleared. The kernel now understands all three pv_scenario values and is
    held to them by the parity suite (tests/test_kernel_parity.py); these tests deliberately pin the
    Python engine's own behaviour, which is the reference the kernel is compared against.
    """
    pred.prediction_kernel_enable = False
    pred.kernel_handle = 0


def test_pv90_scenario_selects_arrays(my_predbat):
    """Each scenario must simulate against its own PV and load series.

    unit_test.py shares one PredBat instance across the whole TEST_REGISTRY loop, so every
    economically-relevant attribute Prediction.__init__ copies from my_predbat (inverter_limit,
    battery_rate_max_charge, reserve, io_adjusted, ...) can carry incidental values left behind by
    whichever test happened to run first. reset_inverter/reset_rates pin those to a known baseline
    (the same helpers every other prediction-array test in this suite relies on), soc_max=0 removes
    the battery as a confound so PV surplus/deficit must flow to export/import rather than being
    buffered, and io_adjusted is cleared so this test measures only the array selection, not the
    pv10 worst-case rate substitution covered separately below.
    """
    from prediction import Prediction

    failed = False
    reset_inverter(my_predbat)
    reset_rates(my_predbat, 10.0, 5.0)
    n = my_predbat.forecast_minutes + my_predbat.minutes_now
    pv50 = {minute: 0.02 for minute in range(0, n, 5)}
    pv10 = {minute: 0.01 for minute in range(0, n, 5)}
    pv90 = {minute: 0.03 for minute in range(0, n, 5)}
    load = {minute: 0.02 for minute in range(0, n, 5)}
    load10 = {minute: 0.03 for minute in range(0, n, 5)}
    load90 = {minute: 0.01 for minute in range(0, n, 5)}

    pred = Prediction(my_predbat, pv50, pv10, load, load10, pv90, load90, soc_kw=0, soc_max=0)
    _force_python_engine(pred)
    pred.io_adjusted = {}

    costs = {}
    for name, scenario in (("nominal", PV_SCENARIO_NOMINAL), ("pv10", PV_SCENARIO_PV10), ("pv90", PV_SCENARIO_PV90)):
        result = pred.run_prediction([], [], [], [], scenario, my_predbat.forecast_minutes)
        costs[name] = result[0]

    # pv10 is the worst case (least PV, most load), pv90 the best - so cost must order strictly
    if not (costs["pv10"] > costs["nominal"] > costs["pv90"]):
        print("ERROR: scenario costs are not ordered pv10 > nominal > pv90: {}".format(costs))
        failed = True
    return failed


def test_pv90_no_io_penalty_on_identical_series(my_predbat):
    """pv90 must not apply the pv10 io_adjusted worst-case import rate substitution.

    See test_pv90_scenario_selects_arrays for why reset_inverter/reset_rates and soc_max=0 are needed
    to keep this deterministic under the shared TEST_REGISTRY instance. soc_max=0 means no charging is
    possible here at all, so this test is deliberately silent on the charge-rate de-rate - that is
    covered separately by test_pv90_no_charge_derate. rate_max is overridden to a value distinct from
    the flat import rate set by reset_rates, since the io_adjusted worst-case substitution
    (import_rate = self.rate_max) would otherwise be a silent no-op when it happens to equal the rate
    that would have applied anyway.
    """
    from prediction import Prediction

    failed = False
    reset_inverter(my_predbat)
    reset_rates(my_predbat, 10.0, 5.0)
    n = my_predbat.forecast_minutes + my_predbat.minutes_now
    flat_pv = {minute: 0.0 for minute in range(0, n, 5)}
    flat_load = {minute: 0.01 for minute in range(0, n, 5)}

    # Identical series for every scenario: any remaining difference is the io penalty
    pred = Prediction(my_predbat, flat_pv, flat_pv, flat_load, flat_load, flat_pv, flat_load, soc_kw=0, soc_max=0)
    _force_python_engine(pred)
    pred.io_adjusted = {minute: 1 for minute in range(0, n)}
    pred.rate_max = 50.0

    nominal = pred.run_prediction([], [], [], [], PV_SCENARIO_NOMINAL, my_predbat.forecast_minutes)
    pv90 = pred.run_prediction([], [], [], [], PV_SCENARIO_PV90, my_predbat.forecast_minutes)
    pv10 = pred.run_prediction([], [], [], [], PV_SCENARIO_PV10, my_predbat.forecast_minutes)

    if abs(pv90[0] - nominal[0]) > 1e-6:
        print("ERROR: pv90 cost {} differs from nominal {} on identical series - a pv10-only penalty leaked into pv90".format(pv90[0], nominal[0]))
        failed = True
    if abs(pv10[0] - nominal[0]) < 1e-6:
        print("ERROR: pv10 cost matches nominal on identical series - the io_adjusted penalty is not being applied at all, so the pv90 check above is vacuous")
        failed = True
    return failed


def test_pv90_no_charge_derate(my_predbat):
    """pv90 must charge at the full rate; the pv10-only charge_scaling10 de-rate must not apply to it.

    test_pv90_no_io_penalty_on_identical_series above runs with soc_max=0, so it never exercises
    charging at all and cannot see the charge-rate de-rate applied at prediction.py's battery_rate_max_scaling
    site. This test targets that site directly: a real (non-zero) soc_max, an explicit charge window
    with no PV in it, and a target SoC well above what is reachable within the window at the nominal
    rate, so the amount actually charged is genuinely rate-limited - not target-limited or PV-diverted.
    With charge_scaling10 = 0.5, pv10 must reach roughly half of nominal's final_soc, while pv90's
    final_soc must equal nominal's exactly.
    """
    from prediction import Prediction

    failed = False
    reset_inverter(my_predbat)
    reset_rates(my_predbat, 10.0, 5.0)
    n = my_predbat.forecast_minutes + my_predbat.minutes_now
    flat_pv = {minute: 0.0 for minute in range(0, n, 5)}
    flat_load = {minute: 0.0 for minute in range(0, n, 5)}
    # battery_rate_max_charge is 1/60.0 kWh/minute (reset_inverter) = 1kW, so a 60-minute window can
    # deliver at most 1.0kWh at the nominal rate; the 5.0kWh target is well out of reach in that time.
    charge_window = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 0}]
    charge_limit = [5.0]

    pred = Prediction(my_predbat, flat_pv, flat_pv, flat_load, flat_load, flat_pv, flat_load, soc_kw=0.0, soc_max=10.0)
    _force_python_engine(pred)
    pred.set_charge_low_power = False  # keep the rate at the flat max instead of throttling to just meet the target
    pred.charge_scaling10 = 0.5

    nominal = pred.run_prediction(charge_limit, charge_window, [], [], PV_SCENARIO_NOMINAL, my_predbat.forecast_minutes)
    pv90 = pred.run_prediction(charge_limit, charge_window, [], [], PV_SCENARIO_PV90, my_predbat.forecast_minutes)
    pv10 = pred.run_prediction(charge_limit, charge_window, [], [], PV_SCENARIO_PV10, my_predbat.forecast_minutes)

    nominal_final_soc = nominal[5]
    pv90_final_soc = pv90[5]
    pv10_final_soc = pv10[5]

    if nominal_final_soc <= 0:
        print("ERROR: nominal final_soc is {}, test setup failed to charge the battery at all".format(nominal_final_soc))
        return True
    if abs(pv90_final_soc - nominal_final_soc) > 1e-6:
        print("ERROR: pv90 final_soc {} differs from nominal {} - the pv10-only charge de-rate leaked into pv90".format(pv90_final_soc, nominal_final_soc))
        failed = True
    if not (pv10_final_soc < nominal_final_soc - 1e-6):
        print("ERROR: pv10 final_soc {} is not below nominal {} - the charge de-rate is not being applied, so the pv90 check above is vacuous".format(pv10_final_soc, nominal_final_soc))
        failed = True
    return failed


METRIC_STATE_ITEMS = [
    "pv_metric10_weight",
    "pv_metric90_weight",
    "metric_battery_value_scaling",
    "carbon_enable",
    "metric_self_sufficiency",
    "metric_battery_cycle",
]


def save_metric_state(my_predbat):
    """Snapshot the weighting attributes the blend tests overwrite."""
    return {name: getattr(my_predbat, name) for name in METRIC_STATE_ITEMS}


def restore_metric_state(my_predbat, state):
    """Restore the weighting attributes saved by save_metric_state."""
    for name, value in state.items():
        setattr(my_predbat, name, value)


def test_pv90_metric_blend(my_predbat):
    """The blend must be a signed weighted average that sums to 1.0 across the three scenarios."""
    failed = False
    my_predbat.pv_metric10_weight = 0.1
    my_predbat.pv_metric90_weight = 0.2
    my_predbat.metric_battery_value_scaling = 0.0  # remove the residual credit so cost == metric
    my_predbat.carbon_enable = False
    my_predbat.metric_self_sufficiency = 0.0
    my_predbat.metric_battery_cycle = 0.0

    metric, _ = my_predbat.compute_metric(0, 0, 0, 100.0, 200.0, 0, 0, 0, 0, 0, 0, 0, 0, soc90=0, cost90=50.0)
    expected = 0.7 * 100.0 + 0.1 * 200.0 + 0.2 * 50.0
    if abs(metric - expected) > 1e-4:
        print("ERROR: blended metric is {}, expected {}".format(metric, expected))
        failed = True

    # cost90 omitted -> the pv90 term must drop out and the nominal weight absorb it
    metric, _ = my_predbat.compute_metric(0, 0, 0, 100.0, 200.0, 0, 0, 0, 0, 0, 0, 0, 0)
    expected = 0.9 * 100.0 + 0.1 * 200.0
    if abs(metric - expected) > 1e-4:
        print("ERROR: metric without cost90 is {}, expected {}".format(metric, expected))
        failed = True
    return failed


def test_pv90_metric_weight_renormalisation(my_predbat):
    """Weights summing above 1.0 must renormalise so the nominal weight never goes negative."""
    failed = False
    my_predbat.pv_metric10_weight = 0.8
    my_predbat.pv_metric90_weight = 0.8
    my_predbat.metric_battery_value_scaling = 0.0
    my_predbat.carbon_enable = False
    my_predbat.metric_self_sufficiency = 0.0
    my_predbat.metric_battery_cycle = 0.0

    metric, _ = my_predbat.compute_metric(0, 0, 0, 100.0, 200.0, 0, 0, 0, 0, 0, 0, 0, 0, soc90=0, cost90=50.0)
    expected = 0.5 * 200.0 + 0.5 * 50.0
    if abs(metric - expected) > 1e-4:
        print("ERROR: renormalised metric is {}, expected {} (nominal weight must clamp to 0)".format(metric, expected))
        failed = True
    return failed


def test_pv90_identical_scenarios_are_identity(my_predbat):
    """When every scenario has the same cost the blend must be a no-op at any weights."""
    failed = False
    my_predbat.pv_metric10_weight = 0.35
    my_predbat.pv_metric90_weight = 0.35
    my_predbat.metric_battery_value_scaling = 0.0
    my_predbat.carbon_enable = False
    my_predbat.metric_self_sufficiency = 0.0
    my_predbat.metric_battery_cycle = 0.0

    metric, _ = my_predbat.compute_metric(0, 0, 0, 123.0, 123.0, 0, 0, 0, 0, 0, 0, 0, 0, soc90=0, cost90=123.0)
    if abs(metric - 123.0) > 1e-4:
        print("ERROR: identity blend gave {}, expected 123.0".format(metric))
        failed = True
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
    failed |= test_pv90_scenario_selects_arrays(my_predbat)
    failed |= test_pv90_no_io_penalty_on_identical_series(my_predbat)
    failed |= test_pv90_no_charge_derate(my_predbat)

    metric_state = save_metric_state(my_predbat)
    try:
        failed |= test_pv90_metric_blend(my_predbat)
        failed |= test_pv90_metric_weight_renormalisation(my_predbat)
        failed |= test_pv90_identical_scenarios_are_identity(my_predbat)
    finally:
        restore_metric_state(my_predbat, metric_state)
    return failed
