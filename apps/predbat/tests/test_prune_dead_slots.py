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
from const import EXPORT_MODE_IDLE
from utils import export_mode_of, export_target_of, export_limit_sort_key


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
    failed |= test_prune_skips_clipping_window(my_predbat)
    failed |= test_prune_keeps_charge_hedge_when_blended_worsens(my_predbat)
    failed |= test_prune_keeps_export_hedge_when_blended_worsens(my_predbat)
    failed |= test_prune_drops_trade_off_slot_when_blended_improves(my_predbat)
    failed |= test_prune_drops_dead_slot_even_when_blended_worsens(my_predbat)
    failed |= test_prune_remeasures_blended_baseline_after_dead_removal(my_predbat)
    failed |= test_prune_reuses_blended_baseline_after_kept_hedge(my_predbat)
    failed |= test_prune_logs_trade_off_removal_distinctly(my_predbat)
    failed |= test_prune_keeps_charge_that_charges_in_its_window(my_predbat)
    failed |= test_prune_drops_hold_that_only_charges_in_pv10(my_predbat)
    failed |= test_prune_judges_slot_moving_soc_in_window_as_trade_off(my_predbat)
    failed |= test_prune_drops_slot_moving_trivial_soc_as_dead(my_predbat)
    failed |= test_prune_drops_hold_that_only_limits_discharge(my_predbat)
    failed |= test_prune_keeps_export_that_exports_in_its_window(my_predbat)
    failed |= test_prune_keeps_export_ending_at_forecast_horizon(my_predbat)
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


def install_metric_stub(my_predbat, table, default=100.0, blended=None, record_blended=None, soc=None):
    """Replace run_prediction_metric with a stub returning a metric looked up from `table`.

    The key is (tuple(charge_limits), tuple(export_limits)); values are the metric. Unknown
    shapes return `default`. Returns the list of keys queried, for call-count assertions.

    `table` answers the nominal-only calls. When `blended` is given it answers the full
    (pv10/pv90-blended) calls instead, so a test can make the two scenarios disagree; shapes
    missing from it fall back to `table`. When `record_blended` is a list, the key of every blended
    call is appended to it. When `soc` is given it maps a shape to the nominal SoC path
    ({minutes from now: kWh}) the call leaves in predict_soc, as the real one does; shapes missing
    from it leave an empty path.
    """
    calls = []
    blended_calls = record_blended if record_blended is not None else []

    def stub(charge_limit_best, charge_window_best, export_window_best, export_limits_best, end_record=None, save=None, nominal_only=False):
        """Stubbed run_prediction_metric keyed on the limit configuration"""
        key = (tuple(charge_limit_best), tuple(round(export_limit_sort_key(v), 2) for v in export_limits_best))
        calls.append(key)
        if not nominal_only:
            blended_calls.append(key)
        metric = table.get(key, default)
        if blended is not None and not nominal_only:
            metric = blended.get(key, metric)
        my_predbat.predict_soc = dict((soc or {}).get(key, {}))
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

    if export_mode_of(my_predbat.export_limits_best[0]) != EXPORT_MODE_IDLE:
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

    if export_target_of(my_predbat.export_limits_best[0]) != 0:
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

    if export_mode_of(my_predbat.export_limits_best[0]) != EXPORT_MODE_IDLE:
        print("ERROR: Improving removal was rejected, slot 0 limit is {}".format(my_predbat.export_limits_best[0]))
        failed = True
    if export_target_of(my_predbat.export_limits_best[1]) != 0:
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

    if export_mode_of(my_predbat.export_limits_best[0]) != EXPORT_MODE_IDLE:
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

    if export_target_of(my_predbat.export_limits_best[0]) != 0:
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

    if export_target_of(my_predbat.export_limits_best[0]) != 0:
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

    if export_target_of(my_predbat.export_limits_best[0]) != 0:
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

    if export_mode_of(my_predbat.export_limits_best[0]) != EXPORT_MODE_IDLE:
        print("ERROR: Neutral export freeze not pruned, limit is {}".format(my_predbat.export_limits_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_skips_clipping_window(my_predbat):
    """An anti-clipping window with clipping_target_soc_pct is never trialled or pruned"""
    print("**** test_prune_skips_clipping_window ****")
    failed = False
    setup(my_predbat)

    win = make_window(780, 810)
    win["clipping_target_soc_pct"] = 74.0
    my_predbat.export_window_best = [win]
    my_predbat.export_limits_best = [74.0]
    install_metric_stub(my_predbat, {}, default=100.0)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if my_predbat.export_limits_best[0] != 74.0:
        print("ERROR: Clipping export slot was pruned, limit is {}".format(my_predbat.export_limits_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_keeps_charge_hedge_when_blended_worsens(my_predbat):
    """A charge slot that buys a buffer against the pv10/load10 scenario is kept: removing it improves
    the nominal metric (the energy is not needed in the central forecast) but worsens the blended one,
    and the cheap window it sits in closes before the shortfall would arrive, so a later replan cannot
    put it back. Seen on a flat 7.6p overnight tariff where the prune cut a 50% charge to 34% and left
    the battery planned to reach the reserve exactly as the next cheap window opened."""
    print("**** test_prune_keeps_charge_hedge_when_blended_worsens ****")
    failed = False
    setup(my_predbat)

    my_predbat.charge_window_best = [make_window(780, 810)]
    my_predbat.charge_limit_best = [5.0]
    nominal = {
        ((5.0,), ()): 439.74,  # as planned
        ((0,), ()): 437.36,  # nominal is cheaper without the buffer
    }
    blended = {
        ((5.0,), ()): 436.15,  # as planned
        ((0,), ()): 442.51,  # but the blended plan is worse without it
    }
    install_metric_stub(my_predbat, nominal, blended=blended)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if my_predbat.charge_limit_best[0] != 5.0:
        print("ERROR: Charge hedge slot was pruned although the blended metric got worse, limit is {}".format(my_predbat.charge_limit_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_keeps_export_hedge_when_blended_worsens(my_predbat):
    """The export equivalent: an export that makes room for pv90 solar is nominally not worth it, but
    without it the pv90 branch clips, so the blended metric gets worse and the slot is kept"""
    print("**** test_prune_keeps_export_hedge_when_blended_worsens ****")
    failed = False
    setup(my_predbat)

    my_predbat.export_window_best = [make_window(780, 810)]
    my_predbat.export_limits_best = [0.0]
    nominal = {
        ((), (0.0,)): 100.0,  # as planned
        ((), (100.0,)): 98.0,  # nominal prefers to keep the energy
    }
    blended = {
        ((), (0.0,)): 100.0,  # as planned
        ((), (100.0,)): 103.0,  # pv90 clips without the export
    }
    install_metric_stub(my_predbat, nominal, blended=blended)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if export_target_of(my_predbat.export_limits_best[0]) != 0:
        print("ERROR: Export hedge slot was pruned although the blended metric got worse, limit is {}".format(my_predbat.export_limits_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_drops_trade_off_slot_when_blended_improves(my_predbat):
    """A slot whose removal changes the nominal outcome is still pruned when the blended metric does
    not get worse either - the guard only protects slots the blended plan is paying for"""
    print("**** test_prune_drops_trade_off_slot_when_blended_improves ****")
    failed = False
    setup(my_predbat)

    my_predbat.charge_window_best = [make_window(780, 810)]
    my_predbat.charge_limit_best = [5.0]
    nominal = {
        ((5.0,), ()): 100.0,
        ((0,), ()): 97.0,
    }
    blended = {
        ((5.0,), ()): 110.0,
        ((0,), ()): 109.0,
    }
    install_metric_stub(my_predbat, nominal, blended=blended)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if my_predbat.charge_limit_best[0] != 0:
        print("ERROR: Trade-off slot was kept although the blended metric also improved, limit is {}".format(my_predbat.charge_limit_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_drops_dead_slot_even_when_blended_worsens(my_predbat):
    """A slot that does nothing in the central forecast is pruned on the nominal metric alone, whatever
    the blended metric says - this is what keeps the #4453 phantom exports, whose apparent value comes
    from a pv10 modelling quirk, out of the plan"""
    print("**** test_prune_drops_dead_slot_even_when_blended_worsens ****")
    failed = False
    setup(my_predbat)

    my_predbat.export_window_best = [make_window(780, 810)]
    my_predbat.export_limits_best = [0.0]
    nominal = {}  # removal leaves the nominal metric unchanged - the slot is dead
    blended = {
        ((), (0.0,)): 100.0,
        ((), (100.0,)): 101.0,  # pv10 appears to value the phantom
    }
    install_metric_stub(my_predbat, nominal, default=100.0, blended=blended)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if export_mode_of(my_predbat.export_limits_best[0]) != EXPORT_MODE_IDLE:
        print("ERROR: Dead export slot was kept because of its blended metric, limit is {}".format(my_predbat.export_limits_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_remeasures_blended_baseline_after_dead_removal(my_predbat):
    """A dead removal leaves the nominal metric unchanged but can still move the blended one, so the
    blended baseline must be measured afresh before the next trade-off is judged.

    Slot 0 is a trade-off the blend accepts, slot 1 is dead in nominal but moves the blended metric,
    and slot 2 is a trade-off whose verdict depends on which baseline it is compared against: against
    the correct one (the plan without slots 0 and 1) it is pruned, against the stale one carried over
    from slot 0's acceptance it would be kept.
    """
    print("**** test_prune_remeasures_blended_baseline_after_dead_removal ****")
    failed = False
    setup(my_predbat)

    my_predbat.charge_window_best = [make_window(780, 810), make_window(810, 840), make_window(840, 870)]
    my_predbat.charge_limit_best = [5.0, 5.0, 5.0]
    nominal = {
        ((5.0, 5.0, 5.0), ()): 100.0,
        ((0, 5.0, 5.0), ()): 98.0,  # slot 0: trade-off
        ((0, 0, 5.0), ()): 98.0,  # slot 1: dead in nominal
        ((0, 0, 0), ()): 96.0,  # slot 2: trade-off
    }
    blended = {
        ((5.0, 5.0, 5.0), ()): 100.0,
        ((0, 5.0, 5.0), ()): 99.0,  # the blend accepts slot 0's removal
        ((0, 0, 5.0), ()): 104.0,  # slot 1's removal moves the blend although nominal does not move
        ((0, 0, 0), ()): 103.5,  # better than the plan it is trialled against (104), worse than the stale 99
    }
    install_metric_stub(my_predbat, nominal, blended=blended)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if my_predbat.charge_limit_best != [0, 0, 0]:
        print("ERROR: Expected all three slots pruned, got limits {} - slot 2 was judged against a stale blended baseline".format(my_predbat.charge_limit_best))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_reuses_blended_baseline_after_kept_hedge(my_predbat):
    """Keeping a hedge leaves the plan unchanged, so its blended baseline is still valid and the next
    trade-off reuses it rather than re-simulating the same plan"""
    print("**** test_prune_reuses_blended_baseline_after_kept_hedge ****")
    failed = False
    setup(my_predbat)

    my_predbat.charge_window_best = [make_window(780, 810), make_window(810, 840)]
    my_predbat.charge_limit_best = [5.0, 5.0]
    nominal = {
        ((5.0, 5.0), ()): 100.0,
        ((0, 5.0), ()): 98.0,  # slot 0: trade-off
        ((5.0, 0), ()): 98.0,  # slot 1: trade-off
    }
    blended = {
        ((5.0, 5.0), ()): 100.0,
        ((0, 5.0), ()): 102.0,  # slot 0 is a hedge the blend pays for - kept
        ((5.0, 0), ()): 99.0,  # slot 1 is not - pruned
    }
    blended_calls = []
    install_metric_stub(my_predbat, nominal, blended=blended, record_blended=blended_calls)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if my_predbat.charge_limit_best != [5.0, 0]:
        print("ERROR: Expected the hedge kept and the other slot pruned, got limits {}".format(my_predbat.charge_limit_best))
        failed = True
    full_plan_measures = blended_calls.count(((5.0, 5.0), ()))
    if full_plan_measures != 1:
        print("ERROR: The unchanged plan's blended metric was simulated {} times, expected once - the baseline was not reused after the kept hedge".format(full_plan_measures))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_logs_trade_off_removal_distinctly(my_predbat):
    """An accepted trade-off removal is not a dead slot, so its debug log must say so and carry the
    blended comparison, rather than reporting it as "Prune dead" """
    print("**** test_prune_logs_trade_off_removal_distinctly ****")
    failed = False
    setup(my_predbat)
    my_predbat.debug_enable = True

    my_predbat.charge_window_best = [make_window(780, 810)]
    my_predbat.charge_limit_best = [5.0]
    nominal = {((5.0,), ()): 100.0, ((0,), ()): 97.0}
    blended = {((5.0,), ()): 110.0, ((0,), ()): 109.0}
    install_metric_stub(my_predbat, nominal, blended=blended)

    lines = []
    real_log = my_predbat.log
    my_predbat.log = lambda message, *args, **kwargs: lines.append(message)
    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        my_predbat.log = real_log
        my_predbat.debug_enable = False
        restore_metric(my_predbat)

    prune_lines = [line for line in lines if line.startswith("Prune ")]
    if my_predbat.charge_limit_best[0] != 0:
        print("ERROR: Trade-off slot was not pruned, limit is {}".format(my_predbat.charge_limit_best[0]))
        failed = True
    if any(line.startswith("Prune dead") for line in prune_lines):
        print("ERROR: Accepted trade-off removal was logged as dead: {}".format(prune_lines))
        failed = True
    if not any(line.startswith("Prune trade-off") and "blended metric 109.0 vs baseline 110.0" in line for line in prune_lines):
        print("ERROR: Expected a 'Prune trade-off' line carrying the blended comparison, got {}".format(prune_lines))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_keeps_charge_that_charges_in_its_window(my_predbat):
    """A charge that charges in its own window in the central forecast is not dead, even when the
    nominal metric does not move.

    Seen on an Intelligent Octopus replay (2.6 kW into 19 kWh, cheap 23:30-05:30): the 23:30 slot
    charged 1.2 kWh in nominal, but the optimiser's 87% limit on the last hour cut the same energy off
    the end of the night, so the nominal metric was unchanged. In pv10 (slower charging) the battery
    never reached that limit, so the slot was a real hedge - pruning it made the blended plan 2.34p
    worse. A replan at 23:30 sees the same battery as now and would not put it back.
    """
    print("**** test_prune_keeps_charge_that_charges_in_its_window ****")
    failed = False
    setup(my_predbat)

    my_predbat.charge_window_best = [make_window(780, 810)]  # ends 90 minutes from now
    my_predbat.charge_limit_best = [5.0]
    blended = {((5.0,), ()): 346.22, ((0,), ()): 348.56}
    soc = {
        ((5.0,), ()): {60: 0.8, 90: 2.0, 600: 16.6},  # charges in its window
        ((0,), ()): {60: 0.8, 90: 0.8, 600: 16.6},  # the later limit makes it up by the end of the night
    }
    install_metric_stub(my_predbat, {}, default=329.84, blended=blended, soc=soc)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if my_predbat.charge_limit_best[0] != 5.0:
        print("ERROR: Charge that charges in its own window was pruned as dead although the blended metric got worse, limit is {}".format(my_predbat.charge_limit_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_drops_hold_that_only_charges_in_pv10(my_predbat):
    """A charge to 50% that does nothing in nominal (the battery is at 55% there) but tops up in pv10
    (49%) is dead, whatever the blend says - if the battery really is low at that time, the replan then
    puts the charge back, so there is nothing to hedge"""
    print("**** test_prune_drops_hold_that_only_charges_in_pv10 ****")
    failed = False
    setup(my_predbat)

    my_predbat.charge_window_best = [make_window(780, 810)]
    my_predbat.charge_limit_best = [5.0]
    blended = {((5.0,), ()): 100.0, ((0,), ()): 101.0}  # pv10 values the top-up
    soc = {
        ((5.0,), ()): {60: 5.5, 90: 5.5},  # nominal is above the limit - nothing happens
        ((0,), ()): {60: 5.5, 90: 5.5},
    }
    install_metric_stub(my_predbat, {}, default=100.0, blended=blended, soc=soc)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if my_predbat.charge_limit_best[0] != 0:
        print("ERROR: Hold that does nothing in nominal was kept because of its blended metric, limit is {}".format(my_predbat.charge_limit_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_judges_slot_moving_soc_in_window_as_trade_off(my_predbat):
    """A slot that moves the nominal SoC in its window but leaves the nominal metric unchanged is still
    pruned when the blended metric does not get worse - but as a trade-off judged on the blend, not as
    a dead slot"""
    print("**** test_prune_judges_slot_moving_soc_in_window_as_trade_off ****")
    failed = False
    setup(my_predbat)
    my_predbat.debug_enable = True

    my_predbat.charge_window_best = [make_window(780, 810)]
    my_predbat.charge_limit_best = [5.0]
    blended = {((5.0,), ()): 110.0, ((0,), ()): 109.0}
    soc = {((5.0,), ()): {60: 0.8, 90: 2.0}, ((0,), ()): {60: 0.8, 90: 0.8}}
    install_metric_stub(my_predbat, {}, blended=blended, soc=soc)

    lines = []
    real_log = my_predbat.log
    my_predbat.log = lambda message, *args, **kwargs: lines.append(message)
    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        my_predbat.log = real_log
        my_predbat.debug_enable = False
        restore_metric(my_predbat)

    prune_lines = [line for line in lines if line.startswith("Prune ")]
    if my_predbat.charge_limit_best[0] != 0:
        print("ERROR: Slot the blended plan does not pay for was kept, limit is {}".format(my_predbat.charge_limit_best[0]))
        failed = True
    if not any(line.startswith("Prune trade-off") and "blended metric 109.0 vs baseline 110.0" in line for line in prune_lines):
        print("ERROR: Expected the slot that moves SoC in its window to be judged as a trade-off on the blend, got {}".format(prune_lines))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_drops_slot_moving_trivial_soc_as_dead(my_predbat):
    """A slot whose removal moves the nominal SoC in its window by a trivial amount is still dead,
    whatever the blend says, so a phantom export that leaks a few watt-hours cannot buy its way back in
    through a pv10 quirk (#4453)"""
    print("**** test_prune_drops_slot_moving_trivial_soc_as_dead ****")
    failed = False
    setup(my_predbat)

    my_predbat.export_window_best = [make_window(780, 810)]
    my_predbat.export_limits_best = [0.0]
    blended = {((), (0.0,)): 100.0, ((), (100.0,)): 101.0}  # pv10 appears to value the phantom
    soc = {((), (0.0,)): {90: 5.0}, ((), (100.0,)): {90: 5.05}}
    install_metric_stub(my_predbat, {}, default=100.0, blended=blended, soc=soc)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if export_mode_of(my_predbat.export_limits_best[0]) != EXPORT_MODE_IDLE:
        print("ERROR: Export slot moving trivial SoC was kept because of its blended metric, limit is {}".format(my_predbat.export_limits_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_drops_hold_that_only_limits_discharge(my_predbat):
    """A charge slot whose limit only stops the battery discharging as far in its window (no charge -
    the SoC still falls, just less) is dead, whatever the blend says. It buys nothing a replan could
    not still buy when the slot comes round. Seen on the pre_saving1 debug case: daytime holds on a flat
    day rate that the blend valued at 0.04-0.26p each."""
    print("**** test_prune_drops_hold_that_only_limits_discharge ****")
    failed = False
    setup(my_predbat)

    my_predbat.charge_window_best = [make_window(780, 810)]
    my_predbat.charge_limit_best = [5.02]
    blended = {((5.02,), ()): 390.76, ((0,), ()): 390.82}
    soc = {
        ((5.02,), ()): {60: 5.57, 90: 5.13},  # the SoC falls across the window with the slot
        ((0,), ()): {60: 5.57, 90: 5.01},  # just a little further without it
    }
    install_metric_stub(my_predbat, {}, default=390.72, blended=blended, soc=soc)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if my_predbat.charge_limit_best[0] != 0:
        print("ERROR: Hold that only limits discharge was kept because of its blended metric, limit is {}".format(my_predbat.charge_limit_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_keeps_export_that_exports_in_its_window(my_predbat):
    """An export that lowers the nominal SoC across its own window is not dead even when the nominal
    metric does not move - it is judged on the blend like any other trade-off"""
    print("**** test_prune_keeps_export_that_exports_in_its_window ****")
    failed = False
    setup(my_predbat)

    my_predbat.export_window_best = [make_window(780, 810)]
    my_predbat.export_limits_best = [0.0]
    blended = {((), (0.0,)): 100.0, ((), (100.0,)): 101.0}
    soc = {
        ((), (0.0,)): {60: 5.0, 90: 4.0},  # exports 1 kWh in its window
        ((), (100.0,)): {60: 5.0, 90: 5.0},
    }
    install_metric_stub(my_predbat, {}, default=100.0, blended=blended, soc=soc)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if export_mode_of(my_predbat.export_limits_best[0]) == EXPORT_MODE_IDLE:
        print("ERROR: Export that exports in its own window was pruned as dead although the blended metric got worse")
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_prune_keeps_export_ending_at_forecast_horizon(my_predbat):
    """An export that reaches the end of the forecast is judged on its SoC like any other.

    Both engines record predict_soc from 0 to forecast_minutes - 5, so when end_record is the whole
    forecast (a plan with no charge windows) there is no key at the window's clipped end. The check
    must read the last simulated step instead of finding nothing and calling the slot dead.
    """
    print("**** test_prune_keeps_export_ending_at_forecast_horizon ****")
    failed = False
    setup(my_predbat)
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.end_record = 24 * 60

    my_predbat.export_window_best = [make_window(720 + 1380, 720 + 1440)]
    my_predbat.export_limits_best = [0.0]
    blended = {((), (0.0,)): 100.0, ((), (100.0,)): 101.0}
    soc = {
        ((), (0.0,)): {1380: 5.0, 1435: 4.1},  # exports in its window, up to the last simulated step
        ((), (100.0,)): {1380: 5.0, 1435: 5.0},
    }
    install_metric_stub(my_predbat, {}, default=100.0, blended=blended, soc=soc)

    try:
        my_predbat.prune_dead_plan_slots()
    finally:
        restore_metric(my_predbat)

    if export_mode_of(my_predbat.export_limits_best[0]) == EXPORT_MODE_IDLE:
        print("ERROR: Export ending at the forecast horizon was pruned as dead although it exports in its window and the blended metric got worse")
        failed = True

    if not failed:
        print("PASS")
    return failed
