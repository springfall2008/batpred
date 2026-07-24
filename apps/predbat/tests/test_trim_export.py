# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from prediction import Prediction
from tests.test_infra import reset_inverter, reset_rates, update_rates_import, update_rates_export


def _setup_buffer_scenario(my_predbat, load=0.1, battery=3.0, charge_hours=8, rate_import=40.0, peak_rate=30.0, cheap_rate=15.0):
    """Build a scenario where the levels pass over-selects export and a self-consumption buffer must be retained.

    A short high-priced peak export slot is followed by three cheaper slots. Import is expensive and the
    next (cheap) charge is several hours away, so the battery must keep some charge to cover house load in
    the gap. Because the export windows are power limited, fully exporting all four slots would over-discharge
    and force expensive house import, so the optimiser must claw back part of the export. The economically
    correct claw-back is taken from the *cheapest* slot, leaving the high-priced peak fully exported.
    """
    reset_inverter(my_predbat)
    my_predbat.prediction_kernel_enable = False
    my_predbat.rate_min_forward = {}
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.soc_max = battery
    my_predbat.soc_kw = battery
    my_predbat.reserve = 0.5
    my_predbat.set_charge_freeze = True
    my_predbat.calculate_second_pass = False
    my_predbat.inverter_loss = 1.0
    my_predbat.battery_loss = 1.0
    my_predbat.battery_loss_discharge = 1.0
    my_predbat.best_soc_keep = 0.0
    my_predbat.debug_enable = False

    mn = my_predbat.minutes_now
    export_window_best = [
        {"start": mn, "end": mn + 30, "average": peak_rate},
        {"start": mn + 30, "end": mn + 60, "average": cheap_rate},
        {"start": mn + 60, "end": mn + 90, "average": cheap_rate},
        {"start": mn + 90, "end": mn + 120, "average": cheap_rate},
    ]
    charge_window_best = [{"start": mn + charge_hours * 60, "end": mn + (charge_hours + 2) * 60, "average": 5.0}]

    reset_rates(my_predbat, rate_import, 5.0)
    update_rates_import(my_predbat, charge_window_best)
    update_rates_export(my_predbat, export_window_best)

    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = 0.0
        load_step[minute] = load / (60 / 5)
    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)

    return charge_window_best, export_window_best


def run_trim_export_tests(my_predbat):
    """The self-consumption buffer must be retained from the cheapest export slot, not by clipping the peak.

    Regression test for the export-trim ordering: coming out of the levels pass all four export windows are
    switched fully on, which over-exports and forces house import. The detailed pass must claw the excess
    back from the lowest-priced slot(s), leaving the highest-priced peak window fully exported.
    """
    print("**** Running trim export priority tests ****")
    failed = False

    charge_window_best, export_window_best = _setup_buffer_scenario(my_predbat)
    end_record = my_predbat.forecast_minutes
    mn = my_predbat.minutes_now

    charge_limit_best = [0 for _ in charge_window_best]
    export_limits_best = [100 for _ in export_window_best]
    metric, _, _, _, _, _, _, _, metric_keep, _, _ = my_predbat.run_prediction(charge_limit_best, charge_window_best, export_window_best, export_limits_best, False, end_record=end_record)
    my_predbat.charge_limit_best = charge_limit_best
    my_predbat.export_limits_best = export_limits_best
    my_predbat.charge_window_best = charge_window_best
    my_predbat.export_window_best = export_window_best

    my_predbat.optimise_all_windows(metric, metric_keep)

    peak = my_predbat.export_window_best[0]
    peak_limit = my_predbat.export_limits_best[0]

    # The highest-priced peak slot must be exported for its full duration (start not clipped) and actually
    # discharging the battery - a limit of 100 (off) or 99 (freeze export) both mean it is not exporting.
    if peak["start"] != mn:
        print("ERROR: peak export window was clipped to {} (expected full window starting at {})".format(peak["start"], mn))
        failed = True
    if peak_limit >= 99:
        print("ERROR: peak export window is not exporting (limit {}, off/freeze) - it is the highest-priced slot".format(peak_limit))
        failed = True

    # The self-consumption buffer must genuinely be retained somewhere cheaper - at least one of the lower
    # priced windows must be reduced (clipped start, frozen, or turned off), otherwise nothing was clawed back.
    cheaper_reduced = False
    for n in range(1, len(my_predbat.export_window_best)):
        window = my_predbat.export_window_best[n]
        if my_predbat.export_limits_best[n] >= 99 or window["start"] > (mn + 30 * n):
            cheaper_reduced = True
            break
    if not cheaper_reduced:
        print("ERROR: no cheaper export window was reduced - buffer was not retained from the cheap end")
        failed = True

    if failed:
        print("Trim export plan: starts {} limits {}".format([w["start"] - mn for w in my_predbat.export_window_best], my_predbat.export_limits_best))

    return failed
