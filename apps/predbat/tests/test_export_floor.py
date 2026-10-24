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
stops there - and a replan shortens it - rather than draining to the 0% the optimiser trials. These
tests pin how a candidate's pv10 trial is floored, how the winner is pinned into the plan, and how the
resulting per-slot steps are merged back into one export at the end.
"""
from tests.test_infra import reset_inverter
from const import EXPORT_MODE_FREEZE, EXPORT_MODE_IDLE, EXPORT_MODE_TARGET, PV_SCENARIO_NOMINAL, PV_SCENARIO_PV10, PV_SCENARIO_PV90
from utils import export_mode_of, export_power_of, export_target_of, pack_export_limit


def run_export_floor_tests(my_predbat):
    """Run all export floor tests, returns True on failure"""
    failed = False
    failed |= test_floor_raises_target_to_nominal_end(my_predbat)
    failed |= test_floor_never_lowers_a_target(my_predbat)
    failed |= test_floor_only_applies_to_target_exports(my_predbat)
    failed |= test_floor_ignores_unknown_socs(my_predbat)
    failed |= test_floor_keeps_export_power(my_predbat)
    failed |= test_floor_without_battery_size(my_predbat)
    failed |= test_floor_shared_across_all_n(my_predbat)
    failed |= test_optimise_export_floors_pv10_and_pins_winner(my_predbat)
    failed |= test_optimise_export_all_n_uses_shared_floor(my_predbat)
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


def test_floor_raises_target_to_nominal_end(my_predbat):
    """A 0% target is raised to the whole percent nominal reaches at the window's end, rounded down"""
    print("**** test_floor_raises_target_to_nominal_end ****")
    setup(my_predbat)
    floored = my_predbat.export_limits_floored([target(0), target(0)], [6.66, 2.0])
    failed = check(False, export_target_of(floored[0]) == 66, "66.6% should floor to 66, got {}".format(floored[0]))
    failed = check(failed, export_target_of(floored[1]) == 20, "20.0% should floor to 20, got {}".format(floored[1]))
    if not failed:
        print("PASS")
    return failed


def test_floor_never_lowers_a_target(my_predbat):
    """A target already above where nominal ends is left alone - the floor only ever raises"""
    print("**** test_floor_never_lowers_a_target ****")
    setup(my_predbat)
    floored = my_predbat.export_limits_floored([target(50)], [3.0])
    failed = check(False, floored[0] == target(50), "a 50% target was lowered to {}".format(floored[0]))
    if not failed:
        print("PASS")
    return failed


def test_floor_only_applies_to_target_exports(my_predbat):
    """Freeze and idle windows carry no target, so they are passed through unchanged"""
    print("**** test_floor_only_applies_to_target_exports ****")
    setup(my_predbat)
    limits = [pack_export_limit(EXPORT_MODE_FREEZE), pack_export_limit(EXPORT_MODE_IDLE)]
    floored = my_predbat.export_limits_floored(limits, [6.0, 6.0])
    failed = check(False, floored == limits, "freeze/idle windows were changed to {}".format(floored))
    if not failed:
        print("PASS")
    return failed


def test_floor_ignores_unknown_socs(my_predbat):
    """A window whose end SoC is unknown (-1, None or missing) keeps its target"""
    print("**** test_floor_ignores_unknown_socs ****")
    setup(my_predbat)
    floored = my_predbat.export_limits_floored([target(0), target(0), target(0)], [-1.0, None])
    failed = check(False, floored == [target(0), target(0), target(0)], "unknown end SoCs changed the limits to {}".format(floored))
    if not failed:
        print("PASS")
    return failed


def test_floor_keeps_export_power(my_predbat):
    """Raising the target keeps the window's export power"""
    print("**** test_floor_keeps_export_power ****")
    setup(my_predbat)
    floored = my_predbat.export_limits_floored([target(0, 0.5)], [4.0])
    failed = check(False, export_target_of(floored[0]) == 40 and export_power_of(floored[0]) == 0.5, "expected 40% at half power, got {}".format(floored[0]))
    if not failed:
        print("PASS")
    return failed


def test_floor_without_battery_size(my_predbat):
    """With no battery size there is no percentage to floor at, so the limits are returned unchanged"""
    print("**** test_floor_without_battery_size ****")
    setup(my_predbat)
    my_predbat.soc_max = 0.0
    floored = my_predbat.export_limits_floored([target(0)], [4.0])
    failed = check(False, floored == [target(0)], "limits changed with soc_max 0: {}".format(floored))
    if not failed:
        print("PASS")
    return failed


def test_floor_shared_across_all_n(my_predbat):
    """Windows trialled together as all_n share one target, so they share one floor - the lowest end SoC"""
    print("**** test_floor_shared_across_all_n ****")
    setup(my_predbat)
    floored = my_predbat.export_limits_floored([target(0), target(0), target(0)], [6.0, 3.5, 8.0], shared=[0, 1])
    failed = check(False, export_target_of(floored[0]) == 35 and export_target_of(floored[1]) == 35, "shared windows should both floor at 35%, got {}".format(floored[:2]))
    failed = check(failed, export_target_of(floored[2]) == 80, "a window outside the shared set keeps its own floor, got {}".format(floored[2]))
    if not failed:
        print("PASS")
    return failed


class FakeHandle:
    """A stand-in prediction handle"""

    def __init__(self, result):
        """Hold the result get() will return"""
        self.result = result

    def get(self):
        """Return the canned result"""
        return self.result


def install_export_stubs(my_predbat, end_socs, cost_of):
    """Replace the export trial launcher and compute_metric with stubs, returning the recorded launches.

    cost_of(scenario, limit, start) gives each trial's cost; the nominal trial reports end_socs as the
    SoC at the end of each export window, as the batch does when asked.
    """
    calls = []

    def launch(this_export_limit, start, window_n, try_charge_limit, charge_window, try_export_window, try_export, pv_scenario, all_n, end_record, want_export_end_soc=False):
        """Record the trial and answer it with a canned result"""
        calls.append({"scenario": pv_scenario, "limit": this_export_limit, "others": list(try_export), "start": start, "want": want_export_end_soc})
        cost = cost_of(pv_scenario, this_export_limit, start)
        result = (cost, 0.0, 0.0, 0.0, 1.0, 5.0, 0, 0.0, 0.0, 0.0, 0.0)
        if want_export_end_soc:
            result = result + (list(end_socs),)
        return FakeHandle(result)

    def compute_metric(end_record, soc, soc10, cost, cost10, *args, **kwargs):
        """Blend nominal and pv10 cost the way the real metric weights them"""
        return round(0.85 * cost + 0.15 * cost10, 4), 0.0

    my_predbat.launch_run_prediction_export = launch
    my_predbat.compute_metric = compute_metric
    my_predbat.set_export_freeze = False
    my_predbat.set_export_freeze_only = False
    my_predbat.set_export_low_power = False
    my_predbat.pv_metric90_weight = 0.15
    my_predbat.metric_min_improvement_export = 0.0
    my_predbat.metric_min_improvement_export_freeze = 0.0
    my_predbat.isExporting = False
    my_predbat.export_window = []
    return calls


def restore_export_stubs(my_predbat):
    """Remove the instance-level stubs"""
    for name in ("launch_run_prediction_export", "compute_metric"):
        if name in my_predbat.__dict__:
            del my_predbat.__dict__[name]


def test_optimise_export_floors_pv10_and_pins_winner(my_predbat):
    """Each candidate's pv10 trial is floored at its own nominal window-end SoCs, pv90 is not, and the
    winning target export comes back pinned at its floor rather than at 0%"""
    print("**** test_optimise_export_floors_pv10_and_pins_winner ****")
    setup(my_predbat)
    export_window = [{"start": 780, "end": 810, "average": 20.0}, {"start": 900, "end": 930, "average": 18.0}]
    export_limit = [pack_export_limit(EXPORT_MODE_IDLE), target(0)]

    def cost_of(scenario, limit, start):
        """Exporting the full window is cheapest, idle the most expensive"""
        if export_mode_of(limit) == EXPORT_MODE_IDLE:
            return 100.0
        return 90.0 if start == 780 else 95.0

    calls = install_export_stubs(my_predbat, [5.0, 3.0], cost_of)
    try:
        result = my_predbat.optimise_export(0, 2, [], [], export_window, export_limit, end_record=my_predbat.end_record)
    finally:
        restore_export_stubs(my_predbat)

    failed = False
    nominal = [c for c in calls if c["scenario"] == PV_SCENARIO_NOMINAL]
    pv10 = [c for c in calls if c["scenario"] == PV_SCENARIO_PV10]
    pv90 = [c for c in calls if c["scenario"] == PV_SCENARIO_PV90]
    failed = check(failed, nominal and all(c["want"] for c in nominal), "every nominal trial should ask for window-end SoCs")
    failed = check(failed, len(pv10) == len(nominal) == len(pv90), "expected one pv10 and one pv90 trial per nominal trial, got {} {} {}".format(len(nominal), len(pv10), len(pv90)))
    for c in pv10:
        if export_mode_of(c["limit"]) == EXPORT_MODE_TARGET:
            failed = check(failed, export_target_of(c["limit"]) == 50, "a pv10 target trial should be floored at 50%, got {}".format(c["limit"]))
        failed = check(failed, export_target_of(c["others"][1]) == 30, "the other export window in a pv10 trial should be floored at 30%, got {}".format(c["others"][1]))
    for c in pv90:
        if export_mode_of(c["limit"]) == EXPORT_MODE_TARGET:
            failed = check(failed, export_target_of(c["limit"]) == 0, "pv90 trials take no floor, got {}".format(c["limit"]))
        failed = check(failed, export_target_of(c["others"][1]) == 0, "pv90 should see the other window without a floor, got {}".format(c["others"][1]))
    failed = check(failed, result[0] == target(50), "the winning export should be pinned at 50%, got {}".format(result[0]))
    failed = check(failed, result[1] == 780, "the full-window export should win, got start {}".format(result[1]))
    if not failed:
        print("PASS")
    return failed


def test_optimise_export_all_n_uses_shared_floor(my_predbat):
    """An all_n trial writes one target into every window it covers, so its pv10 trial and its pinned
    result use the lowest window-end SoC among them"""
    print("**** test_optimise_export_all_n_uses_shared_floor ****")
    setup(my_predbat)
    export_window = [{"start": 780, "end": 810, "average": 20.0}, {"start": 810, "end": 840, "average": 20.0}]
    export_limit = [target(0), target(0)]

    def cost_of(scenario, limit, start):
        """Exporting beats idle"""
        return 100.0 if export_mode_of(limit) == EXPORT_MODE_IDLE else 90.0

    calls = install_export_stubs(my_predbat, [6.0, 3.5], cost_of)
    try:
        result = my_predbat.optimise_export(0, 2, [], [], export_window, export_limit, all_n=[0, 1], end_record=my_predbat.end_record)
    finally:
        restore_export_stubs(my_predbat)

    failed = False
    pv10_targets = [c for c in calls if c["scenario"] == PV_SCENARIO_PV10 and export_mode_of(c["limit"]) == EXPORT_MODE_TARGET]
    failed = check(failed, pv10_targets and all(export_target_of(c["limit"]) == 35 for c in pv10_targets), "all_n pv10 trials should use the shared 35% floor, got {}".format([c["limit"] for c in pv10_targets]))
    failed = check(failed, result[0] == target(35), "the all_n winner should be pinned at 35%, got {}".format(result[0]))
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
