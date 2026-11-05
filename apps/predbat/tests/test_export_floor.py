# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for the realistic pv10 export floor.

A target export is sent to the inverter at the SoC the nominal plan reaches, so in a worse scenario it
stops there - and a replan shortens it - rather than draining to the target the optimiser trials. Every
pv10 simulation floors each target export at where the same plan's nominal run ends. These tests pin the
floor rule, how run_prediction_metric feeds it the nominal run it has already made, and how the per-slot
steps clipping leaves are merged back into one export at the end. The prediction-layer behaviour (direct
runs, the batch and its fallbacks) is pinned in test_prediction_batch.
"""
from tests.test_infra import reset_inverter
from const import EXPORT_MODE_FREEZE, EXPORT_MODE_IDLE, EXPORT_MODE_TARGET, PREDICT_STEP, PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10, PV_SCENARIO_PV90
from prediction_batch import export_end_soc_from_series, floor_export_limits, prediction_cache_key
from utils import export_power_of, export_target_of, pack_export_limit


def run_export_floor_tests(my_predbat):
    """Run all export floor tests, returns True on failure"""
    failed = False
    failed |= test_floor_raises_target_to_nominal_end()
    failed |= test_floor_never_lowers_a_target()
    failed |= test_floor_only_applies_to_target_exports()
    failed |= test_floor_ignores_unknown_socs()
    failed |= test_floor_keeps_export_power()
    failed |= test_floor_without_battery_size()
    failed |= test_metric_runs_nominal_first_and_feeds_the_floor(my_predbat)
    failed |= test_merge_staircase_into_one_export(my_predbat)
    failed |= test_merge_keeps_a_binding_hold(my_predbat)
    failed |= test_merge_needs_contiguous_falling_same_power_steps(my_predbat)
    failed |= test_merge_skips_manual_windows(my_predbat)
    return failed


def setup(my_predbat):
    """Reset the shared instance to a small, known battery"""
    reset_inverter(my_predbat)
    my_predbat.soc_max = 10.0
    my_predbat.reserve = 0.5
    my_predbat.best_soc_min = 0.0
    my_predbat.debug_enable = False
    my_predbat.minutes_now = 720
    my_predbat.end_record = 24 * 60
    my_predbat.forecast_minutes = 48 * 60
    my_predbat.manual_all_times = []
    my_predbat.all_active_keep_max = {}
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []


def target(percent, power=1.0):
    """A target export instruction"""
    return pack_export_limit(EXPORT_MODE_TARGET, percent, power)


def check(failed, condition, message):
    """Print a failure message and fold it into the running result"""
    if not condition:
        print("ERROR: " + message)
        return True
    return failed


def test_floor_raises_target_to_nominal_end():
    """A 0% target is raised to the whole percent nominal reaches at the window's end, rounded down"""
    print("**** test_floor_raises_target_to_nominal_end ****")
    floored = floor_export_limits([target(0), target(0)], [6.66, 2.0], 10.0)
    failed = check(False, export_target_of(floored[0]) == 66, "66.6% should floor to 66, got {}".format(floored[0]))
    failed = check(failed, export_target_of(floored[1]) == 20, "20.0% should floor to 20, got {}".format(floored[1]))
    if not failed:
        print("PASS")
    return failed


def test_floor_never_lowers_a_target():
    """A target already above where nominal ends is left alone - the floor only ever raises"""
    print("**** test_floor_never_lowers_a_target ****")
    floored = floor_export_limits([target(50)], [3.0], 10.0)
    failed = check(False, floored[0] == target(50), "a 50% target was lowered to {}".format(floored[0]))
    if not failed:
        print("PASS")
    return failed


def test_floor_only_applies_to_target_exports():
    """Freeze and idle windows carry no target, so they are passed through unchanged"""
    print("**** test_floor_only_applies_to_target_exports ****")
    limits = [pack_export_limit(EXPORT_MODE_FREEZE), pack_export_limit(EXPORT_MODE_IDLE)]
    floored = floor_export_limits(limits, [6.0, 6.0], 10.0)
    failed = check(False, floored == limits, "freeze/idle windows were changed to {}".format(floored))
    if not failed:
        print("PASS")
    return failed


def test_floor_ignores_unknown_socs():
    """A window whose end SoC is unknown (-1, None or missing) keeps its target"""
    print("**** test_floor_ignores_unknown_socs ****")
    floored = floor_export_limits([target(0), target(0), target(0)], [-1.0, None], 10.0)
    failed = check(False, floored == [target(0), target(0), target(0)], "unknown end SoCs changed the limits to {}".format(floored))
    if not failed:
        print("PASS")
    return failed


def test_floor_keeps_export_power():
    """Raising the target keeps the window's export power"""
    print("**** test_floor_keeps_export_power ****")
    floored = floor_export_limits([target(0, 0.5)], [4.0], 10.0)
    failed = check(False, export_target_of(floored[0]) == 40 and export_power_of(floored[0]) == 0.5, "expected 40% at half power, got {}".format(floored[0]))
    if not failed:
        print("PASS")
    return failed


def test_floor_without_battery_size():
    """With no battery size there is no percentage to floor at, so the limits are returned unchanged"""
    print("**** test_floor_without_battery_size ****")
    floored = floor_export_limits([target(0)], [4.0], 0.0)
    failed = check(False, floored == [target(0)], "limits changed with soc_max 0: {}".format(floored))
    if not failed:
        print("PASS")
    return failed


class RecordingPrediction:
    """Just enough of a Prediction for run_prediction_metric: the window-end SoC memo"""

    def __init__(self):
        """Start with an empty memo"""
        self.prediction_cache_export_end = {}


def test_metric_runs_nominal_first_and_feeds_the_floor(my_predbat):
    """run_prediction_metric runs nominal first and records its window-end SoCs for the plan, so the pv10
    run floors from it instead of simulating nominal again - and the nominal run's predict_soc and
    friends are what the caller is left holding afterwards"""
    print("**** test_metric_runs_nominal_first_and_feeds_the_floor ****")
    setup(my_predbat)
    my_predbat.pv_metric90_weight = 0.15
    charge_limit = [5.0]
    charge_window = [{"start": 700, "end": 760}]
    export_window = [{"start": 780, "end": 810}, {"start": 900, "end": 930}]
    export_limits = [target(0), target(0)]
    nominal_soc = {0: 6.0, 90: 4.0, 210: 2.5}
    order = []
    held = {}

    def run_prediction(cl, cw, ew, el, pv_scenario, end_record, save=None, step=PREDICT_STEP):
        """Record the scenario order and leave a per-scenario SoC series behind, as the real one does"""
        order.append(pv_scenario)
        if pv_scenario == PV_SCENARIO_PV10:
            held["memo_at_pv10"] = dict(my_predbat.prediction.prediction_cache_export_end)
        my_predbat.predict_soc = dict(nominal_soc) if pv_scenario == PV_SCENARIO_NOMINAL else {0: float(pv_scenario)}
        my_predbat.iboost_next = pv_scenario
        return (10.0, 0.0, 0.0, 0.0, 1.0, 5.0, 0, 0.0, 0.0, 0.0, 0.0)

    real_prediction = getattr(my_predbat, "prediction", None)
    my_predbat.prediction = RecordingPrediction()
    my_predbat.run_prediction = run_prediction
    try:
        my_predbat.run_prediction_metric(charge_limit, charge_window, export_window, export_limits, end_record=my_predbat.end_record)
    finally:
        del my_predbat.__dict__["run_prediction"]
        my_predbat.prediction = real_prediction

    failed = check(False, order[:1] == [PV_SCENARIO_NOMINAL], "nominal should run first, ran {}".format(order))
    failed = check(failed, sorted(order) == sorted([PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10, PV_SCENARIO_PV90]), "expected one run per scenario, ran {}".format(order))
    key = prediction_cache_key(charge_limit, charge_window, export_limits, export_window, PV_SCENARIO_NOMINAL, my_predbat.end_record, PREDICT_STEP)
    expected = export_end_soc_from_series(nominal_soc, export_window, my_predbat.minutes_now, export_limits)
    failed = check(failed, held.get("memo_at_pv10", {}).get(key) == expected, "pv10 should find the nominal window-end SoCs {} in the memo, found {}".format(expected, held.get("memo_at_pv10")))
    failed = check(failed, my_predbat.predict_soc == nominal_soc and my_predbat.iboost_next == PV_SCENARIO_NOMINAL, "the nominal run's state should be left in place, got predict_soc {} iboost_next {}".format(my_predbat.predict_soc, my_predbat.iboost_next))
    my_predbat.pv_metric90_weight = 0.0
    if not failed:
        print("PASS")
    return failed


def install_nominal_stub(my_predbat, metric_of):
    """Replace run_prediction_metric with a stub scoring the plan's export shape with metric_of"""

    def stub(charge_limit_best, charge_window_best, export_window_best, export_limits_best, end_record=None, save=None, nominal_only=False):
        """Score the export windows as (start, end, limit) tuples"""
        metric = metric_of(tuple((w["start"], w["end"], l) for w, l in zip(export_window_best, export_limits_best)))
        return metric, 0.0, metric, 0.0, 0.0, 0.0, 0.0, 0.0

    my_predbat.run_prediction_metric = stub


def restore_nominal_stub(my_predbat):
    """Remove the run_prediction_metric stub"""
    if "run_prediction_metric" in my_predbat.__dict__:
        del my_predbat.__dict__["run_prediction_metric"]


def staircase(my_predbat, targets, power=1.0, start=1020):
    """Give the plan contiguous 30 minute target exports stepping down through targets"""
    my_predbat.export_window_best = [{"start": start + 30 * n, "end": start + 30 * (n + 1), "average": 16.0 + n, "target": t} for n, t in enumerate(targets)]
    my_predbat.export_limits_best = [target(t, power) for t in targets]


def test_merge_staircase_into_one_export(my_predbat):
    """Steps the nominal plan does not notice merging become one export at the last step's target"""
    print("**** test_merge_staircase_into_one_export ****")
    setup(my_predbat)
    staircase(my_predbat, [66, 59, 51])
    install_nominal_stub(my_predbat, lambda shape: 100.0)
    try:
        my_predbat.merge_export_staircase()
    finally:
        restore_nominal_stub(my_predbat)
    failed = check(False, len(my_predbat.export_window_best) == 1, "expected one merged export, got {}".format(my_predbat.export_window_best))
    if not failed:
        merged = my_predbat.export_window_best[0]
        failed = check(failed, (merged["start"], merged["end"]) == (1020, 1110), "merged export should run 1020-1110, got {}-{}".format(merged["start"], merged["end"]))
        failed = check(failed, my_predbat.export_limits_best[0] == target(51), "merged export should target the last step's 51%, got {}".format(my_predbat.export_limits_best[0]))
        failed = check(failed, merged.get("target") == 51, "merged window should display the 51% target, got {}".format(merged.get("target")))
    if not failed:
        print("PASS")
    return failed


def test_merge_keeps_a_binding_hold(my_predbat):
    """A step the nominal plan stops at on purpose - merging it changes the nominal metric - stays separate"""
    print("**** test_merge_keeps_a_binding_hold ****")
    setup(my_predbat)
    staircase(my_predbat, [50, 20])

    def metric_of(shape):
        """Exporting below 50% in the first slot costs money"""
        return 101.0 if len(shape) == 1 else 100.0

    install_nominal_stub(my_predbat, metric_of)
    try:
        my_predbat.merge_export_staircase()
    finally:
        restore_nominal_stub(my_predbat)
    failed = check(False, my_predbat.export_limits_best == [target(50), target(20)], "a binding hold was merged: {}".format(my_predbat.export_limits_best))
    if not failed:
        print("PASS")
    return failed


def test_merge_needs_contiguous_falling_same_power_steps(my_predbat):
    """A gap, a rising target or a change of export power each stop a merge"""
    print("**** test_merge_needs_contiguous_falling_same_power_steps ****")
    failed = False
    cases = []
    setup(my_predbat)
    staircase(my_predbat, [60, 40])
    my_predbat.export_window_best[1] = dict(my_predbat.export_window_best[1], start=1060, end=1090)
    cases.append(("gap", [w.copy() for w in my_predbat.export_window_best], list(my_predbat.export_limits_best)))
    staircase(my_predbat, [40, 60])
    cases.append(("rising", [w.copy() for w in my_predbat.export_window_best], list(my_predbat.export_limits_best)))
    staircase(my_predbat, [60, 40])
    my_predbat.export_limits_best[1] = target(40, 0.5)
    cases.append(("power", [w.copy() for w in my_predbat.export_window_best], list(my_predbat.export_limits_best)))
    for name, windows, limits in cases:
        setup(my_predbat)
        my_predbat.export_window_best = [w.copy() for w in windows]
        my_predbat.export_limits_best = list(limits)
        install_nominal_stub(my_predbat, lambda shape: 100.0)
        try:
            my_predbat.merge_export_staircase()
        finally:
            restore_nominal_stub(my_predbat)
        failed = check(failed, len(my_predbat.export_window_best) == 2, "{}: windows should not merge, got {}".format(name, my_predbat.export_window_best))
    if not failed:
        print("PASS")
    return failed


def test_merge_skips_manual_windows(my_predbat):
    """A manually set export keeps its own window"""
    print("**** test_merge_skips_manual_windows ****")
    setup(my_predbat)
    staircase(my_predbat, [60, 40])
    my_predbat.manual_all_times = [1050]
    install_nominal_stub(my_predbat, lambda shape: 100.0)
    try:
        my_predbat.merge_export_staircase()
    finally:
        restore_nominal_stub(my_predbat)
    failed = check(False, len(my_predbat.export_window_best) == 2, "a manual window was merged: {}".format(my_predbat.export_window_best))
    if not failed:
        print("PASS")
    return failed
