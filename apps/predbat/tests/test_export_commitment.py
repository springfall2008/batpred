# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from tests.test_infra import reset_rates, update_rates_export, reset_inverter
from prediction import Prediction


def setup_single_export_window(my_predbat, rate_import=10.0, rate_export=30.0, battery_size=10.0, battery_soc=10.0, window_minutes=120):
    """
    Build a Predbat instance with a single export window covering the current time, a full battery and no load/PV.

    The export rate is set well above the import rate so that exporting is metric-favourable (keeping the battery
    is worth less than exporting it), isolating the behaviour to the cost gate in optimise_export.

    Returns (export_window_best, record_export_windows, end_record).
    """
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    reset_inverter(my_predbat)
    my_predbat.pool = None

    my_predbat.forecast_minutes = 24 * 60
    end_record = my_predbat.forecast_minutes
    my_predbat.end_record = end_record

    # No load and no solar keeps the simulation deterministic
    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = 0.0
        load_step[minute] = 0.0
    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)

    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.soc_max = battery_size
    my_predbat.soc_kw = battery_soc
    my_predbat.inverter_hybrid = False
    my_predbat.inverter_loss = 1.0
    my_predbat.battery_loss = 1.0
    my_predbat.battery_loss_discharge = 1.0
    my_predbat.best_soc_keep = 0.0
    my_predbat.best_soc_min = 0.0
    my_predbat.reserve = 0.0
    my_predbat.num_inverters = 1
    my_predbat.metric_battery_cycle = 0.0
    my_predbat.debug_enable = False

    # A single two hour export window starting at the current time
    start = my_predbat.minutes_now
    end = my_predbat.minutes_now + window_minutes
    export_window_best = [{"start": start, "end": end, "average": rate_export}]

    reset_rates(my_predbat, rate_import, rate_export)
    update_rates_export(my_predbat, export_window_best)

    record_export_windows = max(my_predbat.max_charge_windows(end_record + my_predbat.minutes_now, export_window_best), 1)
    return export_window_best, record_export_windows, end_record


def run_in_progress_start_test(my_predbat):
    """
    An export that is already running must not have its start pushed past the current minute.

    optimise_export varies the window start, so "stop now and restart later inside this window" is always a
    candidate. Selecting it puts a gap in a running forced export, which is the flapping the commitment exists
    to prevent, so a retained in-progress export must still cover the current minute.
    """
    failed = False

    orig_set_export_freeze = my_predbat.set_export_freeze
    orig_set_export_freeze_only = my_predbat.set_export_freeze_only
    orig_set_export_low_power = my_predbat.set_export_low_power
    orig_metric_min_improvement_export = my_predbat.metric_min_improvement_export

    export_window_best, record_export_windows, end_record = setup_single_export_window(my_predbat)

    my_predbat.set_export_freeze = False
    my_predbat.set_export_freeze_only = False
    my_predbat.set_export_low_power = False
    my_predbat.metric_min_improvement_export = 100.0

    # The window is already under way: it started half an hour ago and runs for another 90 minutes
    export_window_best[0]["start"] = my_predbat.minutes_now - 30
    export_window_best[0]["end"] = my_predbat.minutes_now + 90
    update_rates_export(my_predbat, export_window_best)

    my_predbat.isExporting = True
    my_predbat.export_window = [{"start": my_predbat.minutes_now - 30, "end": my_predbat.minutes_now + 90, "average": 30.0}]

    best_export, best_start = my_predbat.optimise_export(0, record_export_windows, [], [], export_window_best, [100.0], end_record=end_record)[:2]
    if best_export < 100.0 and best_start > my_predbat.minutes_now:
        print("ERROR: in-progress export was delayed to {} which is after minutes_now {}".format(best_start, my_predbat.minutes_now))
        failed = True

    my_predbat.isExporting = False
    my_predbat.export_window = []
    my_predbat.set_export_freeze = orig_set_export_freeze
    my_predbat.set_export_freeze_only = orig_set_export_freeze_only
    my_predbat.set_export_low_power = orig_set_export_low_power
    my_predbat.metric_min_improvement_export = orig_metric_min_improvement_export
    return failed


def run_tweak_monotonic_test(my_predbat):
    """
    tweak_plan must never write back a window change that makes the whole plan worse.

    optimise_export scores its candidates against the window being turned off rather than against the setting
    the plan already holds, so it can return something worse than what an earlier pass chose. Here it is
    stubbed to return exactly that (export off, start delayed) and tweak_plan is expected to revert it.
    """
    failed = False

    export_window_best, record_export_windows, end_record = setup_single_export_window(my_predbat)

    start = my_predbat.minutes_now - 30
    end = my_predbat.minutes_now + 90
    export_window_best[0]["start"] = start
    export_window_best[0]["end"] = end
    update_rates_export(my_predbat, export_window_best)

    # A profitable export already selected by an earlier pass
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.export_window_best = export_window_best
    my_predbat.export_limits_best = [0.0]
    my_predbat.end_record = end_record

    metric_before = my_predbat.plan_metric_current(end_record)[0]

    orig_optimise_export = my_predbat.optimise_export

    def bad_optimise_export(*args, **kwargs):
        """Stand in for optimise_export returning a worse option than the plan already holds."""
        return 100.0, my_predbat.minutes_now + 60, 0, 0, 0, 0, 0, 0, 0, 0

    my_predbat.optimise_export = bad_optimise_export
    try:
        my_predbat.tweak_plan(end_record)
    finally:
        my_predbat.optimise_export = orig_optimise_export

    if my_predbat.export_limits_best[0] != 0.0:
        print("ERROR: tweak_plan kept a worse export limit {} instead of reverting to 0.0".format(my_predbat.export_limits_best[0]))
        failed = True
    if my_predbat.export_window_best[0]["start"] != start:
        print("ERROR: tweak_plan kept a delayed export start {} instead of reverting to {}".format(my_predbat.export_window_best[0]["start"], start))
        failed = True
    if "start_orig" in my_predbat.export_window_best[0]:
        print("ERROR: tweak_plan left a start_orig behind after reverting the change")
        failed = True

    metric_after = my_predbat.plan_metric_current(end_record)[0]
    if metric_after > metric_before:
        print("ERROR: tweak_plan made the plan worse, metric {} was {}".format(metric_after, metric_before))
        failed = True

    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    return failed


def run_export_commitment_tests(my_predbat):
    """
    Regression tests for the forced export commitment in optimise_export (plan.py).

    When Predbat is already exporting within a window the cost gate is relaxed so the export is sustained
    across planning cycles. This stops the export "flapping" on near-flat multi-slot price peaks where
    exporting now versus holding and exporting the adjacent equal-priced slot is a cost coin toss each cycle.
    """
    print("**** Running Export commitment tests ****")
    failed = False

    export_window_best, record_export_windows, end_record = setup_single_export_window(my_predbat)

    # Restrict the export options to [100, 0] so the freeze (99) option cannot interfere
    orig_set_export_freeze = my_predbat.set_export_freeze
    orig_set_export_freeze_only = my_predbat.set_export_freeze_only
    orig_set_export_low_power = my_predbat.set_export_low_power
    orig_metric_min_improvement_export = my_predbat.metric_min_improvement_export
    orig_metric_min_improvement_export_freeze = my_predbat.metric_min_improvement_export_freeze

    my_predbat.set_export_freeze = False
    my_predbat.set_export_freeze_only = False
    my_predbat.set_export_low_power = False

    # A deliberately huge min improvement so a genuinely beneficial export is rejected by the cost gate on a
    # fresh plan, but is small enough to be overcome by the commitment relaxation.
    my_predbat.metric_min_improvement_export = 100.0
    my_predbat.metric_min_improvement_export_freeze = 0.1
    charge_window_best = []
    charge_limit_best = []
    export_limits_best = [100.0]

    # 1) Fresh plan, not currently exporting -> the export is gated out and the window is held at 100%
    my_predbat.isExporting = False
    my_predbat.export_window = []
    best_export_fresh = my_predbat.optimise_export(0, record_export_windows, charge_limit_best, charge_window_best, export_window_best, export_limits_best, end_record=end_record)[0]
    if best_export_fresh != 100.0:
        print("ERROR: expected export gated out (100%) on a fresh plan with huge min_improvement, got {}".format(best_export_fresh))
        failed = True

    # 2) Same scenario but we are already exporting within this window -> the commitment relaxes the cost gate
    #    and the in-progress export is retained (limit below 100%)
    my_predbat.isExporting = True
    my_predbat.export_window = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 120, "average": 30.0}]
    best_export_keep = my_predbat.optimise_export(0, record_export_windows, charge_limit_best, charge_window_best, export_window_best, export_limits_best, end_record=end_record)[0]
    if best_export_keep >= 100.0:
        print("ERROR: expected an in-progress export to be retained (<100%) under commitment, got {}".format(best_export_keep))
        failed = True

    # 3) Guard: the commitment must not fire when the prior export window does not cover the current time, so a
    #    stale export flag cannot force a fresh export. The window is gated out again.
    my_predbat.isExporting = True
    my_predbat.export_window = [{"start": my_predbat.minutes_now - 300, "end": my_predbat.minutes_now - 180, "average": 30.0}]
    best_export_outside = my_predbat.optimise_export(0, record_export_windows, charge_limit_best, charge_window_best, export_window_best, export_limits_best, end_record=end_record)[0]
    if best_export_outside != 100.0:
        print("ERROR: expected export gated out (100%) when the prior export window does not cover now, got {}".format(best_export_outside))
        failed = True

    # Restore the fields mutated above so we do not make later tests in the shared suite order-dependent
    my_predbat.isExporting = False
    my_predbat.export_window = []
    my_predbat.set_export_freeze = orig_set_export_freeze
    my_predbat.set_export_freeze_only = orig_set_export_freeze_only
    my_predbat.set_export_low_power = orig_set_export_low_power
    my_predbat.metric_min_improvement_export = orig_metric_min_improvement_export
    my_predbat.metric_min_improvement_export_freeze = orig_metric_min_improvement_export_freeze

    # 4) An in-progress export must not be restarted later inside its own window
    failed |= run_in_progress_start_test(my_predbat)

    # 5) tweak_plan must revert a window change that makes the whole plan worse
    failed |= run_tweak_monotonic_test(my_predbat)

    if failed:
        print("**** Export commitment tests FAILED ****")
    else:
        print("**** Export commitment tests passed ****")
    return failed
