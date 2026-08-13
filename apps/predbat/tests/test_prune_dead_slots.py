# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
"""Tests for run_prediction_metric(nominal_only=...) and prune_dead_plan_slots.

The prune trials use a stubbed run_prediction_metric so each test controls exactly what the
simulation reports for every candidate plan shape, keeping the tests about the prune logic
(criterion, baseline update, guards) rather than the simulator.
"""
from tests.test_infra import reset_inverter


def run_prune_dead_slots_tests(my_predbat):
    """Run all prune-dead-slots and nominal-only-metric tests"""
    failed = False
    failed |= test_nominal_only_runs_single_scenario(my_predbat)
    failed |= test_nominal_only_skips_pv90_even_when_weighted(my_predbat)
    failed |= test_prune_drops_neutral_slot(my_predbat)
    failed |= test_prune_keeps_slot_when_removal_worsens(my_predbat)
    failed |= test_prune_allows_improvement_and_updates_baseline(my_predbat)
    failed |= test_prune_drops_dead_in_progress_window(my_predbat)
    failed |= test_prune_keeps_valuable_in_progress_window(my_predbat)
    failed |= test_prune_skips_manual_window(my_predbat)
    failed |= test_prune_drops_neutral_charge_freeze(my_predbat)
    failed |= test_prune_ignores_windows_outside_record(my_predbat)
    failed |= test_prune_drops_neutral_export_freeze(my_predbat)
    return failed


def setup(my_predbat):
    """Reset the shared predbat instance and give it a minimal plan-free state"""
    reset_inverter(my_predbat)
    my_predbat.soc_max = 10.0
    my_predbat.reserve = 0.5
    my_predbat.debug_enable = False
    my_predbat.minutes_now = 720
    my_predbat.end_record = 24 * 60
    my_predbat.forecast_minutes = 48 * 60
    my_predbat.manual_all_times = []
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []


def make_window(start, end, average=10.0):
    """Build a minimal charge/export window dict"""
    return {"start": start, "end": end, "average": average}


def install_metric_stub(my_predbat, table, default=100.0):
    """Replace run_prediction_metric with a stub returning a metric looked up from `table`.

    The key is (tuple(charge_limits), tuple(export_limits)); values are the metric. Unknown
    shapes return `default`. Returns the list of keys queried, for call-count assertions.
    """
    calls = []

    def stub(charge_limit_best, charge_window_best, export_window_best, export_limits_best, end_record=None, save=None, nominal_only=False):
        """Stubbed run_prediction_metric keyed on the limit configuration"""
        key = (tuple(charge_limit_best), tuple(round(v, 2) for v in export_limits_best))
        calls.append(key)
        metric = table.get(key, default)
        return metric, 0.0, metric, 0.0, 0.0, 0.0, 0.0, 0.0

    my_predbat.run_prediction_metric = stub
    return calls


def restore_metric(my_predbat):
    """Remove any instance-level run_prediction_metric stub"""
    if "run_prediction_metric" in my_predbat.__dict__:
        del my_predbat.__dict__["run_prediction_metric"]


def test_nominal_only_runs_single_scenario(my_predbat):
    """nominal_only=True must run exactly one (nominal) prediction, even though pv10 is normally simulated"""
    print("**** test_nominal_only_runs_single_scenario ****")
    failed = False
    setup(my_predbat)
    my_predbat.pv_metric90_weight = 0

    scenarios = []
    real_run_prediction = my_predbat.run_prediction

    def fake_run_prediction(charge_limit, charge_window, export_window, export_limits, pv10, end_record=None, save=None, step=5):
        """Record which scenario was simulated and return a canned result"""
        scenarios.append(pv10)
        return (5.0, 1.0, 1.0, 2.0, 3.0, 4.0, 0, 0.5, 0.25, 0.0, 0.0)

    my_predbat.run_prediction = fake_run_prediction
    try:
        metric, battery_value, cost, keep, cycle, carbon, import_kwh, export_kwh = my_predbat.run_prediction_metric([], [], [], [], end_record=my_predbat.end_record, nominal_only=True)
    finally:
        my_predbat.run_prediction = real_run_prediction

    if scenarios != [False]:
        print("ERROR: Expected exactly one nominal simulation, got scenarios {}".format(scenarios))
        failed = True
    if cost != 5.0:
        print("ERROR: Expected nominal cost 5.0 returned, got {}".format(cost))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_nominal_only_skips_pv90_even_when_weighted(my_predbat):
    """nominal_only=True must not run the pv90 scenario even when pv_metric90_weight is set"""
    print("**** test_nominal_only_skips_pv90_even_when_weighted ****")
    failed = False
    setup(my_predbat)
    my_predbat.pv_metric90_weight = 0.2

    scenarios = []
    real_run_prediction = my_predbat.run_prediction

    def fake_run_prediction(charge_limit, charge_window, export_window, export_limits, pv10, end_record=None, save=None, step=5):
        """Record which scenario was simulated and return a canned result"""
        scenarios.append(pv10)
        return (5.0, 1.0, 1.0, 2.0, 3.0, 4.0, 0, 0.5, 0.25, 0.0, 0.0)

    my_predbat.run_prediction = fake_run_prediction
    try:
        my_predbat.run_prediction_metric([], [], [], [], end_record=my_predbat.end_record, nominal_only=True)
    finally:
        my_predbat.run_prediction = real_run_prediction
        my_predbat.pv_metric90_weight = 0

    if scenarios != [False]:
        print("ERROR: Expected exactly one nominal simulation with pv90 weighted, got scenarios {}".format(scenarios))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_drops_neutral_slot(my_predbat):
    """An active export slot whose removal leaves the nominal metric unchanged is pruned"""
    print("**** test_prune_drops_neutral_slot ****")
    failed = False
    setup(my_predbat)

    my_predbat.export_window_best = [make_window(780, 810)]
    my_predbat.export_limits_best = [0.0]
    install_metric_stub(my_predbat, {}, default=100.0)  # every shape scores the same

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if my_predbat.export_limits_best[0] != 100.0:
        print("ERROR: Neutral export slot not pruned, limit is {}".format(my_predbat.export_limits_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_keeps_slot_when_removal_worsens(my_predbat):
    """An export slot whose removal makes the nominal metric worse is kept"""
    print("**** test_prune_keeps_slot_when_removal_worsens ****")
    failed = False
    setup(my_predbat)

    my_predbat.export_window_best = [make_window(780, 810)]
    my_predbat.export_limits_best = [0.0]
    table = {
        ((), (0.0,)): 100.0,  # as planned
        ((), (100.0,)): 105.0,  # removal loses export revenue
    }
    install_metric_stub(my_predbat, table)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if my_predbat.export_limits_best[0] != 0.0:
        print("ERROR: Genuine export slot was pruned, limit is {}".format(my_predbat.export_limits_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_allows_improvement_and_updates_baseline(my_predbat):
    """A removal that improves the nominal metric is accepted, and later trials compare against the
    improved baseline rather than the original one"""
    print("**** test_prune_allows_improvement_and_updates_baseline ****")
    failed = False
    setup(my_predbat)

    my_predbat.export_window_best = [make_window(780, 810), make_window(840, 870)]
    my_predbat.export_limits_best = [0.0, 0.0]
    table = {
        ((), (0.0, 0.0)): 100.0,  # as planned
        ((), (100.0, 0.0)): 95.0,  # removing slot 0 improves (e.g. phantom pinning charge)
        ((), (100.0, 100.0)): 95.5,  # then removing slot 1 regresses vs the improved baseline
        ((), (0.0, 100.0)): 99.9,  # (not visited once slot 0 is removed first)
    }
    install_metric_stub(my_predbat, table)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if my_predbat.export_limits_best[0] != 100.0:
        print("ERROR: Improving removal was rejected, slot 0 limit is {}".format(my_predbat.export_limits_best[0]))
        failed = True
    if my_predbat.export_limits_best[1] != 0.0:
        print("ERROR: Slot 1 was pruned against a stale baseline, limit is {}".format(my_predbat.export_limits_best[1]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_drops_dead_in_progress_window(my_predbat):
    """A window covering minutes_now IS trialled: it is the slot being executed right now, so a dead one
    must not be left commanding the inverter. Removal is still gated on the whole-plan metric, which is
    the yardstick #4402 requires - a genuinely valuable in-progress export is protected by the metric
    itself, not by being exempt from the trial."""
    print("**** test_prune_drops_dead_in_progress_window ****")
    failed = False
    setup(my_predbat)

    my_predbat.export_window_best = [make_window(700, 750)]  # covers minutes_now=720
    my_predbat.export_limits_best = [0.0]
    install_metric_stub(my_predbat, {}, default=100.0)  # removal is neutral - the slot is dead

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if my_predbat.export_limits_best[0] != 100.0:
        print("ERROR: Dead in-progress export slot was not pruned, limit is {}".format(my_predbat.export_limits_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_keeps_valuable_in_progress_window(my_predbat):
    """An in-progress export that is genuinely worth something is kept - the metric gate protects it, so
    an export in flight at a good price is never cancelled (the #4402 regression)"""
    print("**** test_prune_keeps_valuable_in_progress_window ****")
    failed = False
    setup(my_predbat)

    my_predbat.export_window_best = [make_window(700, 750)]  # covers minutes_now=720
    my_predbat.export_limits_best = [0.0]
    table = {
        ((), (0.0,)): 100.0,  # as planned
        ((), (100.0,)): 104.0,  # cancelling it mid-flight loses real export revenue
    }
    install_metric_stub(my_predbat, table)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if my_predbat.export_limits_best[0] != 0.0:
        print("ERROR: Valuable in-progress export was cancelled mid-flight, limit is {}".format(my_predbat.export_limits_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_skips_manual_window(my_predbat):
    """A manually-set window is never trialled by the prune"""
    print("**** test_prune_skips_manual_window ****")
    failed = False
    setup(my_predbat)

    my_predbat.export_window_best = [make_window(780, 810)]
    my_predbat.export_limits_best = [0.0]
    my_predbat.manual_all_times = [780]
    install_metric_stub(my_predbat, {}, default=100.0)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if my_predbat.export_limits_best[0] != 0.0:
        print("ERROR: Manual export slot was pruned, limit is {}".format(my_predbat.export_limits_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_drops_neutral_charge_freeze(my_predbat):
    """A charge freeze slot (limit == reserve) whose removal is nominal-neutral is pruned to 0"""
    print("**** test_prune_drops_neutral_charge_freeze ****")
    failed = False
    setup(my_predbat)

    my_predbat.charge_window_best = [make_window(780, 810)]
    my_predbat.charge_limit_best = [my_predbat.reserve]  # freeze charge marker
    install_metric_stub(my_predbat, {}, default=100.0)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if my_predbat.charge_limit_best[0] != 0:
        print("ERROR: Neutral charge freeze not pruned, limit is {}".format(my_predbat.charge_limit_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_ignores_windows_outside_record(my_predbat):
    """Windows beyond end_record are not trialled - they are never executed so sims are wasted on them"""
    print("**** test_prune_ignores_windows_outside_record ****")
    failed = False
    setup(my_predbat)

    beyond = my_predbat.minutes_now + my_predbat.end_record + 60
    my_predbat.export_window_best = [make_window(beyond, beyond + 30)]
    my_predbat.export_limits_best = [0.0]
    calls = install_metric_stub(my_predbat, {}, default=100.0)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if my_predbat.export_limits_best[0] != 0.0:
        print("ERROR: Out-of-record export slot was modified, limit is {}".format(my_predbat.export_limits_best[0]))
        failed = True
    if len(calls) > 1:
        print("ERROR: Out-of-record slot was trialled ({} metric calls)".format(len(calls)))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_drops_neutral_export_freeze(my_predbat):
    """An export freeze slot (limit 99) whose removal is nominal-neutral is pruned - this subsumes the
    freeze-at-100%-SoC heuristic with a simulation-based test"""
    print("**** test_prune_drops_neutral_export_freeze ****")
    failed = False
    setup(my_predbat)

    my_predbat.export_window_best = [make_window(780, 810)]
    my_predbat.export_limits_best = [99.0]
    install_metric_stub(my_predbat, {}, default=100.0)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if my_predbat.export_limits_best[0] != 100.0:
        print("ERROR: Neutral export freeze not pruned, limit is {}".format(my_predbat.export_limits_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed
