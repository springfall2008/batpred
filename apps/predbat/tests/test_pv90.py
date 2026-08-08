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
from plan import DummyThread
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


def test_pv90_load_scaling90_composes_relatively(my_predbat):
    """load_scaling90 must compose relatively with load_scaling, not replace it as an independent absolute multiplier.

    This pins fix-round-2 Item 2. If load_scaling90 were an absolute multiplier (scale_fixed=self.load_scaling90
    alone, the pre-fix code), a user's own load_scaling would not cancel out of it: on
    coverage/cases/predbat_debug_agile1.yaml, load_scaling=0.5 with load_scaling90 at its default 0.9 gave
    0.9/0.5 = 1.8x - a same-PV, HIGHER-load scenario, the opposite of the "upside" pv90 is meant to model.
    load_scaling=1.05 (this file's earlier test_pv90_step_arrays_built, and most default configs) is too
    close to 1.0 for that inversion to show up in a totals comparison, so this test deliberately sets
    load_scaling well away from 1.0 (0.5, matching the debug case that surfaced the bug) - under the
    absolute convention this assertion fails (total90 > total), which is exactly the regression being
    pinned; under the relative convention (self.load_scaling * self.load_scaling90) it always holds,
    because the two load_scaling factors are the same on both sides and only load_scaling90's 0.9 discount
    remains.
    """
    failed = False
    saved_load_scaling = my_predbat.load_scaling
    saved_load_minutes = my_predbat.load_minutes
    saved_load_forecast_only = my_predbat.load_forecast_only
    try:
        my_predbat.load_scaling = 0.5
        # The historical-load path (step_data_history's type_load and not forward branch) reads
        # load_minutes as a HA-style incrementing/cumulative series via get_filtered_load_minute ->
        # get_from_incrementing (a plain per-minute dict, as most other array-based tests in this file
        # seed, is read by the *forward* PV path only and is not what the load path consults). Index 0
        # is "now", larger indices are further back in time with a *lower* cumulative value, and
        # load_forecast_only must be False or the historical path is skipped entirely and returns zero
        # regardless of what load_minutes contains.
        my_predbat.load_forecast_only = False
        rate = 0.02  # kWh/minute
        my_predbat.load_minutes = {minute: (1440 - minute) * rate for minute in range(0, 1441)}
        my_predbat.calculate_plan(recompute=True)
        total = sum(my_predbat.load_minutes_step.values())
        total90 = sum(my_predbat.load_minutes_step90.values())
        if total <= 0:
            print("ERROR: load_minutes_step is all zero despite seeding a synthetic load series - the comparison would be vacuous")
            return True
        if total90 >= total:
            print("ERROR: load_minutes_step90 total {} is not below the nominal {} at load_scaling=0.5 - load_scaling90 is not composing relatively with load_scaling".format(total90, total))
            failed = True
    finally:
        my_predbat.load_scaling = saved_load_scaling
        my_predbat.load_minutes = saved_load_minutes
        my_predbat.load_forecast_only = saved_load_forecast_only
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


def _setup_calculate_plan_with_real_windows(my_predbat):
    """Give calculate_plan() an actual charge and export window to optimise, and snapshot what it touches.

    reset_inverter() alone leaves self.low_rates and self.high_export_rates empty (they are normally
    populated by fetch.py's rate scan, which these launch-counting tests never run), and with both empty
    calculate_plan() completes having found nothing to optimise - so optimise_charge_limit/optimise_export/
    optimise_charge_limit_price_threads are never even called, and a pv90-launch count of zero would be
    vacuously true regardless of whether the pv90 wiring is correct. Populating low_rates/high_export_rates
    directly (calculate_best_charge/calculate_best_export are True by default) gives calculate_plan() a real
    window in each direction to search, so the counting assertions below are actually exercised.

    threads is forced to 0 - the same idiom already used by test_execute.py, test_random_scenarios.py and
    test_single_debug.py - so calculate_plan() takes the synchronous DummyThread fallback instead of
    spinning up a real multiprocessing Pool, keeping the test fast and deterministic.

    Returns a snapshot dict for _restore_calculate_plan_with_real_windows.
    """
    snapshot = {
        "low_rates": my_predbat.low_rates,
        "high_export_rates": my_predbat.high_export_rates,
        "threads": my_predbat.args.get("threads"),
        "charge_window_best": my_predbat.charge_window_best,
        "export_window_best": my_predbat.export_window_best,
        "charge_limit_best": my_predbat.charge_limit_best,
        "export_limits_best": my_predbat.export_limits_best,
        "plan_valid": my_predbat.plan_valid,
    }
    reset_inverter(my_predbat)
    reset_rates(my_predbat, 10.0, 5.0)
    my_predbat.args["threads"] = 0
    my_predbat.low_rates = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 5.0}]
    my_predbat.high_export_rates = [{"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 90, "average": 20.0}]
    my_predbat.plan_valid = False
    return snapshot


def _restore_calculate_plan_with_real_windows(my_predbat, snapshot):
    """Undo the mutations made by _setup_calculate_plan_with_real_windows."""
    my_predbat.low_rates = snapshot["low_rates"]
    my_predbat.high_export_rates = snapshot["high_export_rates"]
    if snapshot["threads"] is None:
        my_predbat.args.pop("threads", None)
    else:
        my_predbat.args["threads"] = snapshot["threads"]
    my_predbat.charge_window_best = snapshot["charge_window_best"]
    my_predbat.export_window_best = snapshot["export_window_best"]
    my_predbat.charge_limit_best = snapshot["charge_limit_best"]
    my_predbat.export_limits_best = snapshot["export_limits_best"]
    my_predbat.plan_valid = snapshot["plan_valid"]


def test_pv90_weight_zero_skips_simulation(my_predbat):
    """With the weight at 0 no pv90 prediction may be run, so plan time is unaffected."""
    failed = False
    my_predbat.pv_metric90_weight = 0.0
    calls = {"count": 0}
    original = my_predbat.launch_run_prediction_charge

    def counting(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Count pv90 launches so the skip can be asserted."""
        if pv_scenario == PV_SCENARIO_PV90:
            calls["count"] += 1
        return original(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record)

    my_predbat.launch_run_prediction_charge = counting
    snapshot = _setup_calculate_plan_with_real_windows(my_predbat)
    try:
        my_predbat.calculate_plan(recompute=True)
    finally:
        my_predbat.launch_run_prediction_charge = original
        _restore_calculate_plan_with_real_windows(my_predbat, snapshot)
    if calls["count"] != 0:
        print("ERROR: {} pv90 predictions were launched with pv_metric90_weight=0".format(calls["count"]))
        failed = True
    return failed


def test_pv90_weight_nonzero_runs_simulation(my_predbat):
    """With a non-zero weight pv90 predictions must actually run, each one paired with the try_soc that launched it.

    Counting alone cannot catch a desync between the launch list and the pop list in optimise_charge_limit
    (the two could go out of step in a way that still leaves the counts matching by coincidence), so this
    test additionally records the try_soc that was active at every pv90 launch and checks, from the outer
    result, that the search still considered more than one candidate SoC - the desync failure mode this
    guards against is results being silently attributed to the wrong try_soc, which a bare count cannot
    distinguish from correct pairing.
    """
    failed = False
    my_predbat.pv_metric90_weight = 0.1
    calls = {"count": 0}
    launched_socs = []
    original = my_predbat.launch_run_prediction_charge

    def counting(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Count pv90 launches, and record the try_soc each one was launched for, so pairing can be checked."""
        if pv_scenario == PV_SCENARIO_PV90:
            calls["count"] += 1
            launched_socs.append(loop_soc)
        return original(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record)

    my_predbat.launch_run_prediction_charge = counting
    snapshot = _setup_calculate_plan_with_real_windows(my_predbat)
    try:
        my_predbat.calculate_plan(recompute=True)
    finally:
        my_predbat.launch_run_prediction_charge = original
        my_predbat.pv_metric90_weight = 0.0
        _restore_calculate_plan_with_real_windows(my_predbat, snapshot)
    if calls["count"] == 0:
        print("ERROR: no pv90 predictions were launched with pv_metric90_weight=0.1")
        failed = True
    elif len(set(launched_socs)) < 2:
        # A desynced results/results90 pop would still often produce a non-zero count, but the search
        # explores several distinct try_soc candidates per window - seeing only one (or none) here is a
        # sign the pv90 launches were not actually keyed to the candidates being searched.
        print("ERROR: pv90 predictions were only launched for {} distinct try_soc value(s): {} - expected the search to try several".format(len(set(launched_socs)), launched_socs))
        failed = True
    return failed


def test_pv90_charge_limit_results_paired_with_try_soc(my_predbat):
    """optimise_charge_limit's pv90 results must stay paired with the try_soc that produced them.

    Neither a launch count nor a count of distinct try_soc values launched (see
    test_pv90_weight_nonzero_runs_simulation) can catch a pop-side desync: a wrong index into
    results90, a pop gated differently from its launch, or a wrong dict key would still often launch
    and pop the right *number* of pv90 predictions for the right *set* of try_soc values - it would
    just hand one candidate's pv90 result to a different candidate's nominal result when both are
    scored together in compute_metric.

    This test makes mispairing observable directly. Both launch_run_prediction_charge (the dynamic
    results/results10/results90 lists) and launch_run_prediction_charge_min_max (the loop_soc/
    best_soc_min pre-fill candidates that seed resultmid/result10/result90 directly) are monkeypatched
    so both the nominal (PV_SCENARIO_NOMINAL) and pv90 (PV_SCENARIO_PV90) cost returned for a given
    try_soc are replaced with try_soc itself - an invertible encoding shared by both scenarios and
    both launch paths, so every candidate in optimise_charge_limit's ranking loop is covered, not just
    the dynamically-launched ones.

    compute_metric is also called by optimise_charge_limit_price_threads (optimise_levels) and
    optimise_export within the same calculate_plan() run, via launch functions this test does not
    encode - so the check below is scoped to only fire while execution is genuinely inside
    optimise_charge_limit (tracked via a depth counter around a wrapped optimise_charge_limit, which
    also catches the "levelling on best_all_n" call optimise_charge_limit_price_threads makes back
    into optimise_charge_limit). Without that scoping this test would false-positive on
    optimise_levels'/optimise_export's own, unrelated, unencoded compute_metric calls.

    A wrapped compute_metric then asserts, on every in-scope call that carries a pv90 term, that the
    nominal cost and the pv90 cost decode back to the *same* try_soc (cost == cost90). That equality
    can only hold if the pv90 result reaching compute_metric was actually produced for the same
    try_soc as the nominal result sitting next to it - exactly the property a positional desync would
    break.
    """
    failed = False
    my_predbat.pv_metric90_weight = 0.1
    mismatches = []
    encoded_scenarios = (PV_SCENARIO_NOMINAL, PV_SCENARIO_PV90)
    in_scope = {"depth": 0}

    original_launch = my_predbat.launch_run_prediction_charge

    def encoding_launch(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Launch for real, then replace the returned cost with try_soc (loop_soc) for nominal/pv90 scenarios only."""
        handle = original_launch(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record)
        if pv_scenario in encoded_scenarios:
            encoded_result = list(handle.get())
            encoded_result[0] = loop_soc
            handle = DummyThread(tuple(encoded_result))
        return handle

    original_launch_min_max = my_predbat.launch_run_prediction_charge_min_max

    def encoding_launch_min_max(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record):
        """Same encoding as encoding_launch, for the min/max pre-fill launches (11 fields plus min_soc/max_soc)."""
        handle = original_launch_min_max(loop_soc, window_n, charge_limit, charge_window, export_window, export_limits, pv_scenario, all_n, end_record)
        if pv_scenario in encoded_scenarios:
            encoded_result = list(handle.get())
            encoded_result[0] = loop_soc
            handle = DummyThread(tuple(encoded_result))
        return handle

    original_optimise_charge_limit = my_predbat.optimise_charge_limit

    def scoped_optimise_charge_limit(*args, **kwargs):
        """Mark every compute_metric call made while inside optimise_charge_limit as in-scope for the pairing check."""
        in_scope["depth"] += 1
        try:
            return original_optimise_charge_limit(*args, **kwargs)
        finally:
            in_scope["depth"] -= 1

    original_compute_metric = my_predbat.compute_metric

    def checking_compute_metric(*args, **kwargs):
        """Assert the nominal cost (args[3]) and cost90 decode back to the same try_soc, only while inside optimise_charge_limit."""
        cost90 = kwargs.get("cost90")
        if cost90 is not None and in_scope["depth"] > 0:
            cost = args[3]
            if cost != cost90:
                mismatches.append((cost, cost90))
        return original_compute_metric(*args, **kwargs)

    my_predbat.launch_run_prediction_charge = encoding_launch
    my_predbat.launch_run_prediction_charge_min_max = encoding_launch_min_max
    my_predbat.optimise_charge_limit = scoped_optimise_charge_limit
    my_predbat.compute_metric = checking_compute_metric
    snapshot = _setup_calculate_plan_with_real_windows(my_predbat)
    try:
        my_predbat.calculate_plan(recompute=True)
    finally:
        my_predbat.launch_run_prediction_charge = original_launch
        my_predbat.launch_run_prediction_charge_min_max = original_launch_min_max
        my_predbat.optimise_charge_limit = original_optimise_charge_limit
        my_predbat.compute_metric = original_compute_metric
        my_predbat.pv_metric90_weight = 0.0
        _restore_calculate_plan_with_real_windows(my_predbat, snapshot)

    if mismatches:
        print("ERROR: {} candidate(s) were scored with a pv90 cost decoding to a different try_soc than the nominal cost sitting next to it (desync): {}".format(len(mismatches), mismatches))
        failed = True
    return failed


def test_pv90_weight_nonzero_runs_export_simulation(my_predbat):
    """optimise_export must also launch pv90 predictions when the weight is active.

    test_pv90_weight_nonzero_runs_simulation only patches launch_run_prediction_charge, so it cannot
    tell whether optimise_export's results90 wiring is present at all - deleting the results90 lines
    from optimise_export would leave every other pv90 test in this module green. This test patches
    launch_run_prediction_export instead and asserts the same launch behaviour for the export path.
    """
    failed = False
    my_predbat.pv_metric90_weight = 0.1
    calls = {"count": 0}
    original = my_predbat.launch_run_prediction_export

    def counting(this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, pv_scenario, all_n, end_record):
        """Count pv90 launches from optimise_export so the wiring can be asserted."""
        if pv_scenario == PV_SCENARIO_PV90:
            calls["count"] += 1
        return original(this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, pv_scenario, all_n, end_record)

    my_predbat.launch_run_prediction_export = counting
    snapshot = _setup_calculate_plan_with_real_windows(my_predbat)
    try:
        my_predbat.calculate_plan(recompute=True)
    finally:
        my_predbat.launch_run_prediction_export = original
        my_predbat.pv_metric90_weight = 0.0
        _restore_calculate_plan_with_real_windows(my_predbat, snapshot)
    if calls["count"] == 0:
        print("ERROR: no pv90 predictions were launched from optimise_export with pv_metric90_weight=0.1")
        failed = True
    return failed


def test_pv90_weight_nonzero_runs_levels_simulation(my_predbat):
    """optimise_charge_limit_price_threads (optimise_levels) must also launch pv90 predictions when the weight is active.

    Same coverage gap as test_pv90_weight_nonzero_runs_export_simulation above but for the levels
    path: deleting the handle90 line from optimise_charge_limit_price_threads would leave every other
    pv90 test in this module green, since none of them patch launch_run_prediction_single.
    """
    failed = False
    my_predbat.pv_metric90_weight = 0.1
    calls = {"count": 0}
    original = my_predbat.launch_run_prediction_single

    def counting(charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step):
        """Count pv90 launches from optimise_levels so the wiring can be asserted."""
        if pv_scenario == PV_SCENARIO_PV90:
            calls["count"] += 1
        return original(charge_limit, charge_window, export_window, export_limits, pv_scenario, end_record, step=step)

    my_predbat.launch_run_prediction_single = counting
    snapshot = _setup_calculate_plan_with_real_windows(my_predbat)
    try:
        my_predbat.calculate_plan(recompute=True)
    finally:
        my_predbat.launch_run_prediction_single = original
        my_predbat.pv_metric90_weight = 0.0
        _restore_calculate_plan_with_real_windows(my_predbat, snapshot)
    if calls["count"] == 0:
        print("ERROR: no pv90 predictions were launched from optimise_levels with pv_metric90_weight=0.1")
        failed = True
    return failed


def test_pv90_run_prediction_metric_carries_cost90(my_predbat):
    """run_prediction_metric must include a pv90 term at a non-zero weight, or every comparison against its result is on the wrong scale.

    This pins fix-round-1 Finding 1 (Critical): run_prediction_metric seeds/re-seeds the best_metric that
    optimise_charge_limit_price_threads, optimise_charge_limit and optimise_export's pv90-inclusive
    candidate metrics are compared against. Before that fix, none of the other pv90 tests in this module
    caught the gap - replacing `if self.pv_metric90_weight > 0:` inside run_prediction_metric with `if
    False:` left `--test pv90 --test optimise_levels --test compute_metric` and the whole `--quick` suite
    green, because every other test either checks that pv90 predictions were *launched* (not that the
    resulting metric ever reached compute_metric with cost90 populated) or checks compute_metric's blend
    arithmetic directly with hand-supplied costs (never through run_prediction_metric itself).

    This test closes that gap by scoping a wrapped compute_metric to only record calls made while
    execution is genuinely inside run_prediction_metric (a depth counter around a wrapped
    run_prediction_metric, the same technique test_pv90_charge_limit_results_paired_with_try_soc uses to
    scope its own check to optimise_charge_limit), and asserting every such call carries a non-None
    cost90 at pv_metric90_weight=0.1.
    """
    failed = False
    my_predbat.pv_metric90_weight = 0.1
    calls = {"count": 0, "missing_cost90": 0}
    in_scope = {"depth": 0}

    original_rpm = my_predbat.run_prediction_metric

    def scoped_run_prediction_metric(*args, **kwargs):
        """Mark every compute_metric call made while inside run_prediction_metric as in-scope for the cost90 check."""
        in_scope["depth"] += 1
        try:
            return original_rpm(*args, **kwargs)
        finally:
            in_scope["depth"] -= 1

    original_compute_metric = my_predbat.compute_metric

    def checking_compute_metric(*args, **kwargs):
        """Record whether cost90 was supplied, only while inside run_prediction_metric."""
        if in_scope["depth"] > 0:
            calls["count"] += 1
            if kwargs.get("cost90") is None:
                calls["missing_cost90"] += 1
        return original_compute_metric(*args, **kwargs)

    my_predbat.run_prediction_metric = scoped_run_prediction_metric
    my_predbat.compute_metric = checking_compute_metric
    snapshot = _setup_calculate_plan_with_real_windows(my_predbat)
    try:
        my_predbat.calculate_plan(recompute=True)
    finally:
        my_predbat.run_prediction_metric = original_rpm
        my_predbat.compute_metric = original_compute_metric
        my_predbat.pv_metric90_weight = 0.0
        _restore_calculate_plan_with_real_windows(my_predbat, snapshot)

    if calls["count"] == 0:
        print("ERROR: run_prediction_metric never reached compute_metric during calculate_plan - test setup problem")
        failed = True
    elif calls["missing_cost90"] > 0:
        print("ERROR: {} of {} run_prediction_metric -> compute_metric call(s) had cost90=None at pv_metric90_weight=0.1".format(calls["missing_cost90"], calls["count"]))
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
    failed |= test_pv90_load_scaling90_composes_relatively(my_predbat)
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

    launch_state = save_metric_state(my_predbat)
    original_launch_run_prediction_charge = my_predbat.launch_run_prediction_charge
    original_launch_run_prediction_charge_min_max = my_predbat.launch_run_prediction_charge_min_max
    original_launch_run_prediction_export = my_predbat.launch_run_prediction_export
    original_launch_run_prediction_single = my_predbat.launch_run_prediction_single
    original_optimise_charge_limit = my_predbat.optimise_charge_limit
    original_run_prediction_metric = my_predbat.run_prediction_metric
    original_compute_metric = my_predbat.compute_metric
    try:
        failed |= test_pv90_weight_zero_skips_simulation(my_predbat)
        failed |= test_pv90_weight_nonzero_runs_simulation(my_predbat)
        failed |= test_pv90_charge_limit_results_paired_with_try_soc(my_predbat)
        failed |= test_pv90_weight_nonzero_runs_export_simulation(my_predbat)
        failed |= test_pv90_weight_nonzero_runs_levels_simulation(my_predbat)
        failed |= test_pv90_run_prediction_metric_carries_cost90(my_predbat)
    finally:
        my_predbat.launch_run_prediction_charge = original_launch_run_prediction_charge
        my_predbat.launch_run_prediction_charge_min_max = original_launch_run_prediction_charge_min_max
        my_predbat.launch_run_prediction_export = original_launch_run_prediction_export
        my_predbat.launch_run_prediction_single = original_launch_run_prediction_single
        my_predbat.optimise_charge_limit = original_optimise_charge_limit
        my_predbat.run_prediction_metric = original_run_prediction_metric
        my_predbat.compute_metric = original_compute_metric
        restore_metric_state(my_predbat, launch_state)
    return failed
