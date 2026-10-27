# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from tests.test_infra import update_rates_export
from tests.test_export_commitment import setup_single_export_window
from const import EXPORT_MODE_TARGET, EXPORT_MODE_FREEZE, EXPORT_MODE_IDLE
from utils import pack_export_limit, export_mode_of


def setup_held_export(my_predbat, start_offset, threshold):
    """
    Hold a forced export to 0% in a single two hour window starting start_offset minutes from now.

    This stands in for an export the levels pass seeded: exporting a full battery at 30p against a 10p import
    rate improves the plan metric, but a large metric_min_improvement_export rejects it in optimise_export.
    Freeze and low power export are turned off so the only options are the forced export and idle.

    Returns end_record.
    """
    export_window_best, record_export_windows, end_record = setup_single_export_window(my_predbat)
    export_window_best[0]["start"] = my_predbat.minutes_now + start_offset
    export_window_best[0]["end"] = my_predbat.minutes_now + start_offset + 120
    update_rates_export(my_predbat, export_window_best)

    my_predbat.set_export_freeze = False
    my_predbat.set_export_freeze_only = False
    my_predbat.set_export_low_power = False
    my_predbat.isExporting = False
    my_predbat.export_window = []
    my_predbat.metric_min_improvement_export = threshold

    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.export_window_best = export_window_best
    my_predbat.export_limits_best = [pack_export_limit(EXPORT_MODE_TARGET, 0)]
    my_predbat.end_record = end_record
    return end_record


def run_detailed_pass(my_predbat, end_record):
    """Run optimise_detailed_pass over the plan currently held, as optimise_all_windows would after the levels pass."""
    best_metric, best_battery_value, best_cost, best_keep, best_cycle, best_carbon, best_import, best_export = my_predbat.run_prediction_metric(
        my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, end_record=end_record
    )
    record_export_windows = max(my_predbat.max_charge_windows(end_record + my_predbat.minutes_now, my_predbat.export_window_best), 1)
    my_predbat.optimise_detailed_pass(10.0, 30.0, 10.0, 0.0, best_metric, best_cost, best_keep, 0, best_cycle, best_carbon, best_import, best_battery_value, 1, record_export_windows)


def check_export_mode(my_predbat, label, expected_mode):
    """Return True (failed) when the single export window is not left in expected_mode."""
    mode = export_mode_of(my_predbat.export_limits_best[0])
    if mode != expected_mode:
        print("ERROR: {} - expected export mode {} but the window holds {}".format(label, expected_mode, my_predbat.export_limits_best[0]))
        return True
    return False


def run_export_threshold_removal_tests(my_predbat):
    """
    metric_min_improvement_export must be able to remove an export an earlier pass seeded (issue #3376).

    The levels pass places exports without the threshold. optimise_export then rejects a low-margin one, but
    both write-back sites used to undo that rejection because turning the export off makes the plan metric
    slightly worse, so no threshold could ever take the export out.
    """
    print("**** Running export threshold removal tests ****")
    failed = False

    # setup_single_export_window and setup_held_export reshape the shared fixture, so save the fields they set directly
    saved_names = (
        "set_export_freeze",
        "set_export_freeze_only",
        "set_export_low_power",
        "metric_min_improvement_export",
        "metric_min_improvement_export_freeze",
        "isExporting",
        "export_window",
        "forecast_minutes",
        "end_record",
        "load_minutes_step",
        "load_minutes_step10",
        "pv_forecast_minute_step",
        "pv_forecast_minute10_step",
        "calculate_best_charge",
        "calculate_best_export",
        "soc_max",
        "soc_kw",
        "inverter_hybrid",
        "inverter_loss",
        "battery_loss",
        "battery_loss_discharge",
        "best_soc_keep",
        "best_soc_min",
        "reserve",
        "num_inverters",
        "metric_battery_cycle",
        "debug_enable",
        "prediction",
        "rate_import",
        "rate_export",
    )
    missing = object()
    saved = {name: getattr(my_predbat, name, missing) for name in saved_names}
    # reset_rates and update_rates_export change the rate tables in place, so keep copies of their contents
    saved_rates = {name: dict(getattr(my_predbat, name)) for name in ("rate_import", "rate_export") if isinstance(getattr(my_predbat, name, None), dict)}
    try:
        # 1) A future export that fails a 100p threshold is turned off by the plan pass
        end_record = setup_held_export(my_predbat, 60, 100.0)
        my_predbat.optimise_plan_pass(end_record, budget=8)
        failed |= check_export_mode(my_predbat, "plan pass with a 100p threshold", EXPORT_MODE_IDLE)

        # 2) ... and by the detailed pass
        end_record = setup_held_export(my_predbat, 60, 100.0)
        run_detailed_pass(my_predbat, end_record)
        failed |= check_export_mode(my_predbat, "detailed pass with a 100p threshold", EXPORT_MODE_IDLE)

        # 3) At the default threshold the profitable export stays in both passes, as removing it costs far more
        #    than 0.1p per slot
        end_record = setup_held_export(my_predbat, 60, 0.1)
        my_predbat.optimise_plan_pass(end_record, budget=8)
        failed |= check_export_mode(my_predbat, "plan pass with the default threshold", EXPORT_MODE_TARGET)
        end_record = setup_held_export(my_predbat, 60, 0.1)
        run_detailed_pass(my_predbat, end_record)
        failed |= check_export_mode(my_predbat, "detailed pass with the default threshold", EXPORT_MODE_TARGET)

        # 4) An export the inverter is running is not stopped part way through on the threshold
        end_record = setup_held_export(my_predbat, -30, 100.0)
        my_predbat.isExporting = True
        my_predbat.export_window = [dict(my_predbat.export_window_best[0])]
        my_predbat.optimise_plan_pass(end_record, budget=8)
        failed |= check_export_mode(my_predbat, "plan pass on an export in progress", EXPORT_MODE_TARGET)

        # 5) ... but a slot that has started without exporting is still removed. The plan is rebuilt every cycle
        #    and the levels pass seeds the export again, so protecting any window covering the current time
        #    would let the rejected export run as soon as its slot began.
        end_record = setup_held_export(my_predbat, -30, 100.0)
        my_predbat.optimise_plan_pass(end_record, budget=8)
        failed |= check_export_mode(my_predbat, "plan pass on a started slot that is not exporting", EXPORT_MODE_IDLE)

        # 6) A threshold of zero never removes a profitable export
        end_record = setup_held_export(my_predbat, 60, 0.0)
        my_predbat.optimise_plan_pass(end_record, budget=8)
        failed |= check_export_mode(my_predbat, "plan pass with a zero threshold", EXPORT_MODE_TARGET)

        # 7) Which step downs are kept: (label, held limit, held window, new limit, metric loss, isExporting, expected).
        #    With a 10p threshold a full power forced export over a two hour window must gain 10p per slot.
        my_predbat.metric_min_improvement_export = 10.0
        my_predbat.metric_min_improvement_export_freeze = 3.0
        my_predbat.set_export_freeze = True
        my_predbat.set_export_freeze_only = False
        future = {"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 180}
        started = {"start": my_predbat.minutes_now - 30, "end": my_predbat.minutes_now + 90}
        trimmed = {"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 90, "start_orig": my_predbat.minutes_now - 30}
        target = pack_export_limit(EXPORT_MODE_TARGET, 0)
        freeze = pack_export_limit(EXPORT_MODE_FREEZE)
        idle = pack_export_limit(EXPORT_MODE_IDLE)
        target_threshold = my_predbat.export_min_improvement(target, 120)
        expected_threshold = 10.0 * 120 / my_predbat.plan_interval_minutes
        if abs(target_threshold - expected_threshold) > 1e-9:
            print("ERROR: export_min_improvement for a two hour full power export returned {}, expected {}".format(target_threshold, expected_threshold))
            failed = True
        cases = [
            ("target to idle within the threshold", target, future, idle, target_threshold - 0.01, False, True),
            ("target to idle at the threshold", target, future, idle, target_threshold, False, True),
            ("target to idle beyond the threshold", target, future, idle, target_threshold + 0.01, False, False),
            ("target to freeze within the threshold", target, future, freeze, target_threshold - 0.01, False, True),
            ("freeze to idle within the freeze threshold", freeze, future, idle, 2.99, False, True),
            ("freeze to idle beyond the freeze threshold", freeze, future, idle, 3.01, False, False),
            ("idle to target", idle, future, target, 0.0, False, False),
            ("target to a shallower target", target, future, pack_export_limit(EXPORT_MODE_TARGET, 50), 0.0, False, False),
            ("target to idle while exporting in the window", target, started, idle, 0.0, True, False),
            ("target to idle while exporting from a start since trimmed past now", target, trimmed, idle, 0.0, True, False),
            ("target to idle in a started window, not exporting", target, started, idle, 0.0, False, True),
            ("target to idle in a future window while exporting elsewhere", target, future, idle, 0.0, True, True),
        ]
        for label, held_limit, held_window, new_limit, metric_loss, is_exporting, expected in cases:
            my_predbat.isExporting = is_exporting
            result = my_predbat.export_threshold_rejected(held_limit, held_window, new_limit, metric_loss)
            if result != expected:
                print("ERROR: export_threshold_rejected for {} returned {}, expected {}".format(label, result, expected))
                failed = True

        # 8) A step down only counts when optimise_export tried the held mode
        my_predbat.isExporting = False
        if my_predbat.export_threshold_rejected(target, future, idle, 0.0, freeze_only=True):
            print("ERROR: export_threshold_rejected kept a forced export removed by a freeze-only search")
            failed = True
        my_predbat.set_export_freeze_only = True
        if my_predbat.export_threshold_rejected(target, future, idle, 0.0):
            print("ERROR: export_threshold_rejected kept a forced export removed with set_export_freeze_only")
            failed = True
        my_predbat.set_export_freeze_only = False
        if not my_predbat.export_threshold_rejected(freeze, future, idle, 0.0, freeze_only=True):
            print("ERROR: export_threshold_rejected refused a freeze removed by a freeze-only search")
            failed = True
        my_predbat.set_export_freeze = False
        if my_predbat.export_threshold_rejected(freeze, future, idle, 0.0):
            print("ERROR: export_threshold_rejected kept a freeze removed while freeze export is disabled")
            failed = True

        # 9) With the threshold at zero nothing is kept this way, not even a tie
        my_predbat.metric_min_improvement_export = 0.0
        for metric_loss in (0.0, 0.01):
            if my_predbat.export_threshold_rejected(target, future, idle, metric_loss):
                print("ERROR: export_threshold_rejected kept a step down losing {} with a zero threshold".format(metric_loss))
                failed = True
    finally:
        # Restore the fields mutated above so later tests in the shared suite are not order-dependent
        for name, value in saved.items():
            if value is missing:
                if hasattr(my_predbat, name):
                    delattr(my_predbat, name)
            else:
                setattr(my_predbat, name, value)
        for name, contents in saved_rates.items():
            getattr(my_predbat, name).clear()
            getattr(my_predbat, name).update(contents)
        my_predbat.charge_window_best = []
        my_predbat.charge_limit_best = []
        my_predbat.export_window_best = []
        my_predbat.export_limits_best = []

    if failed:
        print("**** Export threshold removal tests FAILED ****")
    else:
        print("**** Export threshold removal tests passed ****")
    return failed
