# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Unit tests for the pairwise export-window swap optimisation pass.

optimise_swap_export moves an export later in the day when doing so costs no more than
metric_min_improvement_swap (default -0.25p, i.e. a small regression is accepted to defer the
export). Holding charge for longer hedges against the forecast being wrong, so a later export of
equal value is always preferred.

The pass can only defer exports that exist when it runs, so these tests also pin its position in
optimise_all_windows: it has to run after every pass that can turn an export on, otherwise the
exports those passes add are never considered for deferral (#4478). The budgeted plan pass in particular
only reaches the near-term windows, so it can only ever add exports at the front - exactly the ones this
pass exists to push back.
"""

from tests.test_infra import reset_rates, reset_inverter, update_rates_export, clear_manual_times
from const import EXPORT_MODE_IDLE, EXPORT_MODE_TARGET
from utils import export_mode_of, export_target_of, pack_export_limit
from prediction import Prediction

# Export limits in the planner's own (mode, target, power) form: export down to 0%, or window off
EXPORT_FULL = pack_export_limit(EXPORT_MODE_TARGET, 0)
EXPORT_OFF = pack_export_limit(EXPORT_MODE_IDLE)


def setup_swap_export(
    my_predbat,
    export_window_best,
    export_limits_best,
    rate_import=30.0,
    rate_export=15.0,
    battery_size=10.0,
    battery_soc=10.0,
    export_rate=10.0,
):
    """Configure my_predbat for an export-swap test and return the starting metric and cost."""
    end_record = my_predbat.forecast_minutes
    my_predbat.end_record = end_record
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.calculate_export_oncharge = True
    my_predbat.set_export_freeze = True
    my_predbat.soc_max = battery_size
    my_predbat.soc_kw = battery_soc
    my_predbat.reserve = 0.0
    my_predbat.best_soc_keep = 0.0
    clear_manual_times(my_predbat)
    my_predbat.iboost_enable = False
    my_predbat.iboost_plan = []
    my_predbat.iboost_on_export = False
    my_predbat.car_charging_from_battery = True
    my_predbat.num_cars = 0
    my_predbat.car_charging_slots = [[]]
    my_predbat.metric_min_improvement_swap = -0.25

    # A generous battery export rate so a 30 minute window can move a meaningful amount of energy
    my_predbat.battery_rate_max_discharge = export_rate / 60.0
    my_predbat.battery_rate_max_export = export_rate / 60.0

    reset_rates(my_predbat, rate_import, rate_export)
    update_rates_export(my_predbat, export_window_best)

    # No solar and a small flat load
    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = 0.0
        load_step[minute] = 0.2 / (60 / 5)
    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
    my_predbat.debug_enable = False

    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.export_window_best = export_window_best
    my_predbat.export_limits_best = list(export_limits_best)

    best_metric, _, best_cost, _, _, _, _, _ = my_predbat.run_prediction_metric(my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, end_record=end_record)
    return best_metric, best_cost


def _record_pass_order(my_predbat):
    """Stub out every optimisation pass so optimise_all_windows just records the call order.

    Returns (order, restore): order is the list the stubs append to, filled in when
    optimise_all_windows is called, and restore() puts the real methods back. my_predbat is shared
    across the whole test run, so leaving the stubs in place would break every later test.
    """
    order = []
    stubbed = (
        "optimise_levels_pass",
        "optimise_detailed_pass",
        "optimise_plan_pass",
        "optimise_solar",
        "optimise_swap_export",
        "optimise_swap_charge",
        "optimise_charge_windows_reset",
        "optimise_charge_windows_manual",
        "plan_write_debug",
    )
    saved = {name: my_predbat.__dict__.get(name, None) for name in stubbed}

    def restore():
        """Remove the stubs so the class methods are visible again."""
        for name, value in saved.items():
            if value is None:
                my_predbat.__dict__.pop(name, None)
            else:
                my_predbat.__dict__[name] = value

    def stub(name, result):
        """Build a recording stub returning a fixed result."""

        def _call(*args, **kwargs):
            """Record the call and return the canned result."""
            order.append(name)
            return result

        return _call

    zeros12 = tuple([0.0] * 12)
    zeros8 = tuple([0.0] * 8)
    zeros6 = tuple([0.0] * 6)

    my_predbat.optimise_levels_pass = stub("levels", zeros12)
    my_predbat.optimise_detailed_pass = stub("detailed", zeros8)
    my_predbat.optimise_plan_pass = stub("plan_pass", zeros6)
    my_predbat.optimise_solar = stub("solar", zeros6)
    my_predbat.optimise_swap_export = stub("swap_export", None)
    my_predbat.optimise_swap_charge = stub("swap_charge", None)

    # Passes that are not part of the ordering under test but would otherwise touch the plan
    my_predbat.optimise_charge_windows_reset = lambda reset_all: None
    my_predbat.optimise_charge_windows_manual = lambda: None
    my_predbat.plan_write_debug = lambda *args, **kwargs: None

    return order, restore


class _RecordingLimits(list):
    """An export_limits_best that logs every element write, so a test can see when the plan changes"""

    def __init__(self, values, events):
        super().__init__(values)
        self.events = events

    def __setitem__(self, index, value):
        self.events.append(("set", index))
        super().__setitem__(index, value)


def _test_partner_swaps_batched_first_pass_kept(my_predbat):
    """A target's partner swaps are priced as one batch, so the plan must not change while trials are
    queued, each trial must change only its partner and the target, and of several passing swaps the
    earliest partner must be kept - the swap a one-at-a-time search stopped at.

    Four equally priced windows exporting in the first two: for target 3 both partners 0 and 1 pass,
    and partner 0 must be the one applied. The partners are 20 minutes with a gap before each, against
    the target's 30, so every trial moves its partner's start back to its end less 30: a start applied
    to the live plan while trials are still queued then shows up as a plan change."""
    failed = False
    now = my_predbat.minutes_now
    export_window_best = [
        {"start": now + 10, "end": now + 30, "average": 15.0},
        {"start": now + 40, "end": now + 60, "average": 15.0},
        {"start": now + 60, "end": now + 90, "average": 15.0},
        {"start": now + 90, "end": now + 120, "average": 15.0},
    ]
    setup_swap_export(my_predbat, export_window_best, export_limits_best=[EXPORT_FULL, EXPORT_FULL, EXPORT_OFF, EXPORT_OFF])

    events = []
    my_predbat.export_limits_best = _RecordingLimits(my_predbat.export_limits_best, events)
    real_launch = my_predbat.launch_run_prediction_metric
    real_get = my_predbat.get_run_prediction_metric
    # Window starts as they stood when the current batch began. A start moved while trials are queued is
    # as much a plan change as a limit write: an earlier trial's untouched windows share their dicts with
    # the live plan, so it would be priced against a start that changed after it was queued
    batch_starts = []

    def record_moved_starts():
        """Log a plan write for every window whose start has moved since the current batch began"""
        for n, window in enumerate(my_predbat.export_window_best):
            if window["start"] != batch_starts[n]:
                events.append(("set_start", n))

    def launch(charge_limit, charge_window, export_window, export_limits, end_record):
        """Record which windows this trial changes relative to the plan as it stands"""
        if not batch_starts:
            batch_starts.extend(window["start"] for window in my_predbat.export_window_best)
        record_moved_starts()
        changed = [n for n in range(len(export_limits)) if export_limits[n] != my_predbat.export_limits_best[n] or export_window[n]["start"] != my_predbat.export_window_best[n]["start"]]
        events.append(("launch", changed))
        return real_launch(charge_limit, charge_window, export_window, export_limits, end_record)

    def get(handles):
        """Check no start moved while the batch was queued, then read the results"""
        if batch_starts:
            record_moved_starts()
            batch_starts.clear()
        events.append(("get",))
        return real_get(handles)

    my_predbat.launch_run_prediction_metric = launch
    my_predbat.get_run_prediction_metric = get
    try:
        my_predbat.optimise_swap_export(0, len(export_window_best))
    finally:
        del my_predbat.launch_run_prediction_metric
        del my_predbat.get_run_prediction_metric
        my_predbat.export_limits_best = list(my_predbat.export_limits_best)

    # The plan may only change once a batch has been read
    queued = False
    for event in events:
        if event[0] == "launch":
            queued = True
        elif event[0] == "get":
            queued = False
        elif queued:
            print("ERROR: the plan changed (window {}) while a batch of swap trials was still queued, events {}".format(event[1], events))
            failed = True
            break

    launches = [event[1] for event in events if event[0] == "launch"]
    if launches[:2] != [[0, 3], [1, 3]]:
        print("ERROR: target 3 should queue partners 0 and 1 as one batch, each changing only itself and the target, got {}".format(launches))
        failed = True
    for changed in launches:
        if len(changed) != 2:
            print("ERROR: a swap trial should change exactly two windows, got {}".format(changed))
            failed = True

    sets = [event[1] for event in events if event[0] == "set"]
    if sets[:2] != [0, 3]:
        print("ERROR: the first kept swap should be partner 0 into target 3 (the earliest passing partner), got writes {}".format(sets))
        failed = True
    if [export_mode_of(limit) for limit in my_predbat.export_limits_best] != [EXPORT_MODE_IDLE, EXPORT_MODE_IDLE, EXPORT_MODE_TARGET, EXPORT_MODE_TARGET]:
        print("ERROR: both exports should end up deferred to windows 2 and 3, got {}".format(my_predbat.export_limits_best))
        failed = True
    return failed


def _test_full_combine(my_predbat):
    """A trimmed target that still has room for the whole partner absorbs it (Full combine): the partner
    turns off and goes back to its original start, and the target takes the partner's limit and grows
    back from its own end by both lengths.

    The target is trimmed to 10 minutes from a 60 minute original (start_orig), and the partner exports
    for 30 minutes, so 10 + 30 <= 60 and neither the Partial combine nor the Swap branch applies."""
    failed = False
    now = my_predbat.minutes_now
    export_window_best = [
        {"start": now, "end": now + 30, "average": 15.0},
        {"start": now + 80, "end": now + 90, "average": 15.0, "start_orig": now + 30},
    ]
    setup_swap_export(my_predbat, export_window_best, export_limits_best=[EXPORT_FULL, EXPORT_FULL])
    my_predbat.optimise_swap_export(0, len(export_window_best))

    partner, target = my_predbat.export_window_best
    if export_mode_of(my_predbat.export_limits_best[0]) != EXPORT_MODE_IDLE or partner["start"] != now:
        print("ERROR: full combine should turn the partner off at its original start {}, got limit {} start {}".format(now, my_predbat.export_limits_best[0], partner["start"]))
        failed = True
    if my_predbat.export_limits_best[1] != EXPORT_FULL or target["start"] != now + 90 - 40:
        print("ERROR: full combine should give the target the partner's limit {} from {} (its end less both lengths), got limit {} start {}".format(EXPORT_FULL, now + 50, my_predbat.export_limits_best[1], target["start"]))
        failed = True
    return failed


def _run_swap_with_penalties(my_predbat, later_price, blended_penalty, central_penalty, pv90_penalty=0.0):
    """Run the swap pass on an export in window 0 with an idle later window at later_price, adding
    blended_penalty to every trial's blended metric, central_penalty to its central gate metric and
    pv90_penalty to its PV90 gate metric. Returns the final export limits.

    The penalties stand in for a scenario mix where the move is worse under the PV10 forecast than
    under the central one (#4292), without having to build a load profile that produces it. PV90 is
    simulated (pv_metric90_weight > 0) so that its gate is read."""
    now = my_predbat.minutes_now
    export_window_best = [
        {"start": now, "end": now + 30, "average": 15.0},
        {"start": now + 30, "end": now + 60, "average": later_price},
    ]
    saved_pv90_weight = my_predbat.pv_metric90_weight
    my_predbat.pv_metric90_weight = 0.1
    setup_swap_export(my_predbat, export_window_best, export_limits_best=[EXPORT_FULL, EXPORT_OFF])
    real_get = my_predbat.get_run_prediction_metric
    real_gates = my_predbat.get_run_prediction_gates

    def get(handles):
        """Read a trial's metric, made worse by blended_penalty"""
        result = list(real_get(handles))
        result[0] += blended_penalty
        return tuple(result)

    def gates(handles):
        """Read the central and PV90 gate metrics, a trial's (it has a PV10 handle) made worse by the penalties"""
        gate, gate90 = real_gates(handles)
        if handles[1] is not None:
            gate += central_penalty
            gate90 += pv90_penalty
        return gate, gate90

    my_predbat.get_run_prediction_metric = get
    my_predbat.get_run_prediction_gates = gates
    try:
        my_predbat.optimise_swap_export(0, len(export_window_best))
    finally:
        del my_predbat.get_run_prediction_metric
        del my_predbat.get_run_prediction_gates
        my_predbat.pv_metric90_weight = saved_pv90_weight
    return my_predbat.export_limits_best


def _test_equal_price_judged_on_central(my_predbat):
    """Moving an export to a later window at the same price is judged on the central forecast and PV90,
    not PV10 (#4292).

    The PV10 scenario runs the plan with no replanning, so it charges a later export for running the
    battery dry under high load although the next replan would stop it. A move that is no worse on the
    central forecast and PV90 must be kept however much worse the blended metric says it is; a move to a
    differently priced window, or one that is worse on the central forecast or PV90, still is not."""
    failed = False
    limits = _run_swap_with_penalties(my_predbat, later_price=15.0, blended_penalty=1.0, central_penalty=0.0)
    if [export_mode_of(limit) for limit in limits] != [EXPORT_MODE_IDLE, EXPORT_MODE_TARGET]:
        print("ERROR: an equal-price move no worse on the central forecast should be kept despite a worse blended metric, got limits {}".format(limits))
        failed = True

    limits = _run_swap_with_penalties(my_predbat, later_price=15.0, blended_penalty=1.0, central_penalty=1.0)
    if [export_mode_of(limit) for limit in limits] != [EXPORT_MODE_TARGET, EXPORT_MODE_IDLE]:
        print("ERROR: an equal-price move worse on the central forecast should be refused, got limits {}".format(limits))
        failed = True

    limits = _run_swap_with_penalties(my_predbat, later_price=15.0, blended_penalty=1.0, central_penalty=0.0, pv90_penalty=1.0)
    if [export_mode_of(limit) for limit in limits] != [EXPORT_MODE_TARGET, EXPORT_MODE_IDLE]:
        print("ERROR: an equal-price move worse in the PV90 scenario (clipping replanning cannot recover) should be refused, got limits {}".format(limits))
        failed = True

    limits = _run_swap_with_penalties(my_predbat, later_price=14.0, blended_penalty=1.0, central_penalty=0.0)
    if [export_mode_of(limit) for limit in limits] != [EXPORT_MODE_TARGET, EXPORT_MODE_IDLE]:
        print("ERROR: a move to a differently priced window should still be judged on the blended metric, got limits {}".format(limits))
        failed = True
    return failed


def _test_equal_price_move_refused_when_it_clips_in_pv90(my_predbat):
    """A same-price later move that leaves the battery full through a PV90 solar surplus must be refused (#4292).

    Replanning can stop a later export when load runs high, but it cannot recover solar clipped against the
    export limit while the battery is held full. With no solar in the central forecast the move costs nothing
    there; in PV90 the morning export makes room for the midday surplus and the afternoon one does not."""
    failed = False
    now = my_predbat.minutes_now
    export_window_best = [
        {"start": now, "end": now + 30, "average": 15.0},
        {"start": now + 300, "end": now + 330, "average": 15.0},
    ]
    reset_inverter(my_predbat)
    changed = ("pv_metric90_weight", "export_limit", "inverter_limit", "pv_forecast_minute90_step", "load_minutes_step90", "prediction")
    saved = {name: my_predbat.__dict__[name] for name in changed if name in my_predbat.__dict__}
    my_predbat.pv_metric90_weight = 0.5
    try:
        setup_swap_export(my_predbat, export_window_best, export_limits_best=[EXPORT_FULL, EXPORT_OFF])
        my_predbat.export_limit = 6.0 / 60.0
        my_predbat.inverter_limit = 20.0 / 60.0
        pv_step = my_predbat.pv_forecast_minute_step
        pv90_step = {}
        for minute in range(0, my_predbat.forecast_minutes, 5):
            pv90_step[minute] = 10.0 / (60 / 5) if 60 <= minute < 240 else 0.0
        my_predbat.pv_forecast_minute90_step = pv90_step
        my_predbat.load_minutes_step90 = my_predbat.load_minutes_step
        my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, my_predbat.load_minutes_step, my_predbat.load_minutes_step, pv90_step, my_predbat.load_minutes_step)
        my_predbat.optimise_swap_export(0, len(export_window_best))
        limits = my_predbat.export_limits_best
        if [export_mode_of(limit) for limit in limits] != [EXPORT_MODE_TARGET, EXPORT_MODE_IDLE]:
            print("ERROR: a same-price later move that clips solar in PV90 should be refused, got limits {}".format(limits))
            failed = True
    finally:
        for name in changed:
            if name in saved:
                setattr(my_predbat, name, saved[name])
            else:
                my_predbat.__dict__.pop(name, None)
    return failed


def _test_equal_price_move_does_not_preempt_a_better_partner(my_predbat):
    """A same-price later move judged on the central forecast must not take a target from a partner the
    blended metric keeps (#4292).

    The pass keeps the first partner that passes for each target. Window 0 (15p) moving into the idle
    window 2 (15p) is refused on the blended metric but passes on the central forecast; window 1 (14p)
    moving there gains on the blended metric. Window 1's move must win although window 0 is read first."""
    now = my_predbat.minutes_now
    export_window_best = [
        {"start": now, "end": now + 30, "average": 15.0},
        {"start": now + 30, "end": now + 60, "average": 14.0},
        {"start": now + 60, "end": now + 90, "average": 15.0},
    ]
    saved_pv90_weight = my_predbat.pv_metric90_weight
    my_predbat.pv_metric90_weight = 0.1
    setup_swap_export(my_predbat, export_window_best, export_limits_best=[EXPORT_FULL, EXPORT_FULL, EXPORT_OFF])
    real_launch = my_predbat.launch_run_prediction_metric
    real_get = my_predbat.get_run_prediction_metric
    window0_idle = set()

    def launch(charge_limit, charge_window, export_window, export_limits, end_record):
        """Queue a trial, noting the ones that turn window 0 off"""
        handles = real_launch(charge_limit, charge_window, export_window, export_limits, end_record)
        if export_mode_of(export_limits[0]) == EXPORT_MODE_IDLE:
            window0_idle.add(id(handles[0]))
        return handles

    def get(handles):
        """Read a trial's metric, made much worse when it turns window 0 off"""
        result = list(real_get(handles))
        if id(handles[0]) in window0_idle:
            result[0] += 10.0
        return tuple(result)

    my_predbat.launch_run_prediction_metric = launch
    my_predbat.get_run_prediction_metric = get
    try:
        my_predbat.optimise_swap_export(0, len(export_window_best))
    finally:
        del my_predbat.launch_run_prediction_metric
        del my_predbat.get_run_prediction_metric
        my_predbat.pv_metric90_weight = saved_pv90_weight
    modes = [export_mode_of(limit) for limit in my_predbat.export_limits_best]
    if modes != [EXPORT_MODE_TARGET, EXPORT_MODE_IDLE, EXPORT_MODE_TARGET]:
        print("ERROR: the 14p export should move into the later 15p window ahead of the same-price one, got limits {}".format(my_predbat.export_limits_best))
        return True
    return False


def _test_gates_include_metric_keep(my_predbat):
    """The equal-price swap check counts the best_soc_keep penalty, which export_gate_metric leaves out (#4292).

    Without it a same-price later move that takes the battery below keep would look free on the central forecast."""

    class _Handle:
        """A queued prediction whose result is already known."""

        def __init__(self, cost, metric_keep):
            """Hold a prediction result with the given cost and keep penalty."""
            self.result = (cost, 0.0, 0.0, 0.0, 0.0, 5.0, 0.0, 0.0, metric_keep, 0.0, 0.0)

        def get(self):
            """Return the held prediction result."""
            return self.result

    without_keep = my_predbat.get_run_prediction_gates((_Handle(100.0, 0.0), None, _Handle(50.0, 0.0)))
    with_keep = my_predbat.get_run_prediction_gates((_Handle(100.0, 3.0), None, _Handle(50.0, 2.0)))
    if abs(with_keep[0] - without_keep[0] - 3.0) > 1e-6 or abs(with_keep[1] - without_keep[1] - 2.0) > 1e-6:
        print("ERROR: get_run_prediction_gates should add each scenario's metric_keep, got {} without keep and {} with".format(without_keep, with_keep))
        return True
    if my_predbat.get_run_prediction_gates((_Handle(100.0, 0.0), None, None))[1] is not None:
        print("ERROR: get_run_prediction_gates should return None for PV90 when it is not simulated")
        return True
    return False


def run_optimise_swap_export_tests(my_predbat):
    """Run the pairwise export-window swap optimisation tests and return True on failure."""
    print("**** Running Optimise swap export tests ****")
    reset_inverter(my_predbat)
    failed = False

    # ---------------------------------------------------------------------------------------------
    # Beneficial swap: the plan exports in the first window while an identically priced later window
    # sits idle. Deferring the export holds the charge for longer at no extra cost, so the pass must
    # move it to the later window.
    # ---------------------------------------------------------------------------------------------
    export_window_best = [
        {"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 15.0},
        {"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 60, "average": 15.0},
    ]
    setup_swap_export(my_predbat, export_window_best, export_limits_best=[EXPORT_FULL, EXPORT_OFF])
    my_predbat.optimise_swap_export(0, len(export_window_best))
    if not (export_mode_of(my_predbat.export_limits_best[0]) == EXPORT_MODE_IDLE and export_target_of(my_predbat.export_limits_best[1]) == 0):
        print("ERROR: equal priced windows should defer the export to the later window, got limits {}".format(my_predbat.export_limits_best))
        failed = True

    # ---------------------------------------------------------------------------------------------
    # Safety invariant: an export already in the latest window has nowhere better to go and must be
    # left alone.
    # ---------------------------------------------------------------------------------------------
    export_window_best = [
        {"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 15.0},
        {"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 60, "average": 15.0},
    ]
    setup_swap_export(my_predbat, export_window_best, export_limits_best=[EXPORT_OFF, EXPORT_FULL])
    my_predbat.optimise_swap_export(0, len(export_window_best))
    if not (export_mode_of(my_predbat.export_limits_best[0]) == EXPORT_MODE_IDLE and export_target_of(my_predbat.export_limits_best[1]) == 0):
        print("ERROR: an export already in the last window should be unchanged, got limits {}".format(my_predbat.export_limits_best))
        failed = True

    # ---------------------------------------------------------------------------------------------
    # Guard: with export optimisation disabled the pass returns immediately without changes.
    # ---------------------------------------------------------------------------------------------
    export_window_best = [
        {"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 15.0},
        {"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 60, "average": 15.0},
    ]
    setup_swap_export(my_predbat, export_window_best, export_limits_best=[EXPORT_FULL, EXPORT_OFF])
    my_predbat.calculate_best_export = False
    my_predbat.optimise_swap_export(0, len(export_window_best))
    if my_predbat.export_limits_best != [EXPORT_FULL, EXPORT_OFF]:
        print("ERROR: disabled export optimisation should be unchanged, got {}".format(my_predbat.export_limits_best))
        failed = True
    my_predbat.calculate_best_export = True

    # ---------------------------------------------------------------------------------------------
    # Partial combine regression (#5423): moving export time from an earlier window into a later,
    # trimmed one must shrink the earlier window, never grow it backwards into the slot before it.
    # Here the slot before is a manual demand override, so growing into it exports through it.
    # ---------------------------------------------------------------------------------------------
    now = my_predbat.minutes_now
    export_window_best = [
        {"start": now, "end": now + 30, "average": 50.0},
        {"start": now + 30, "end": now + 60, "average": 50.0},
        {"start": now + 80, "end": now + 90, "average": 50.0, "start_orig": now + 60},
    ]
    setup_swap_export(my_predbat, export_window_best, export_limits_best=[EXPORT_OFF, EXPORT_FULL, EXPORT_FULL], rate_import=30.0, rate_export=50.0)
    saved_manual_export_times = my_predbat.manual_export_times
    my_predbat.manual_export_times = [now]
    try:
        my_predbat.optimise_swap_export(0, len(export_window_best))
    finally:
        my_predbat.manual_export_times = saved_manual_export_times
    middle = my_predbat.export_window_best[1]
    if middle["start"] < now + 30:
        print("ERROR: partial combine grew window 1 back to {} into the manual slot ending {}, windows {}".format(middle["start"], now + 30, my_predbat.export_window_best))
        failed = True

    # ---------------------------------------------------------------------------------------------
    # Batched partners: priced against an unchanged plan, earliest passing partner kept
    # ---------------------------------------------------------------------------------------------
    failed |= _test_partner_swaps_batched_first_pass_kept(my_predbat)

    # ---------------------------------------------------------------------------------------------
    # Full combine: a trimmed target with room for the whole partner absorbs it
    # ---------------------------------------------------------------------------------------------
    failed |= _test_full_combine(my_predbat)

    # ---------------------------------------------------------------------------------------------
    # Equal-price later moves are judged on the central forecast and PV90, leaving out the no-replan PV10 scenario (#4292)
    # ---------------------------------------------------------------------------------------------
    failed |= _test_equal_price_judged_on_central(my_predbat)
    failed |= _test_equal_price_move_refused_when_it_clips_in_pv90(my_predbat)
    failed |= _test_gates_include_metric_keep(my_predbat)
    failed |= _test_equal_price_move_does_not_preempt_a_better_partner(my_predbat)

    # ---------------------------------------------------------------------------------------------
    # Ordering regression (#4478): the export swap must run after every pass that can enable an
    # export, otherwise the exports the plan pass / optimise_solar add are never
    # considered for deferral and stay pinned at the front of the plan.
    # ---------------------------------------------------------------------------------------------
    for second_pass in (False, True):
        my_predbat.calculate_second_pass = second_pass
        my_predbat.export_more_solar = True
        my_predbat.charge_window_best = []
        my_predbat.charge_limit_best = []
        my_predbat.export_window_best = []
        my_predbat.export_limits_best = []
        order, restore = _record_pass_order(my_predbat)
        try:
            my_predbat.optimise_all_windows(0.0, 0.0)
        finally:
            restore()

        adding_pass = "plan_pass"
        if "swap_export" not in order:
            print("ERROR: optimise_swap_export was not called at all, order {}".format(order))
            failed = True
        elif order.count("swap_export") != 1:
            print("ERROR: optimise_swap_export should be called exactly once, order {}".format(order))
            failed = True
        else:
            for earlier in (adding_pass, "solar"):
                if earlier in order and order.index("swap_export") < order.index(earlier):
                    print("ERROR: optimise_swap_export runs before {} so exports it adds are never deferred, order {}".format(earlier, order))
                    failed = True

    if failed:
        print("**** Optimise swap export tests FAILED ****")
    else:
        print("**** Optimise swap export tests passed ****")
    return failed
