# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from tests.test_infra import reset_rates, reset_inverter, update_rates_export
from prediction import Prediction


def run_optimise_solar(
    name,
    my_predbat,
    export_window_best,
    export_limits_best,
    expect_export_limit,
    pv_amount=0.0,
    load_amount=0.2,
    threshold=1.0,
    set_export_freeze=True,
    calculate_best_export=True,
    rate_import=10.0,
    rate_export=15.0,
    battery_size=10.0,
    battery_soc=5.0,
):
    print("Starting optimise solar test {}".format(name))
    failed = False
    end_record = my_predbat.forecast_minutes
    my_predbat.end_record = end_record
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = calculate_best_export
    my_predbat.set_export_freeze = set_export_freeze
    my_predbat.set_charge_freeze = True
    my_predbat.export_more_solar_threshold = threshold
    my_predbat.soc_max = battery_size
    my_predbat.soc_kw = battery_soc
    my_predbat.reserve = 0.5
    my_predbat.manual_all_times = set()

    reset_rates(my_predbat, rate_import, rate_export)
    update_rates_export(my_predbat, export_window_best)

    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = pv_amount / (60 / 5)
        load_step[minute] = load_amount / (60 / 5)
    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
    my_predbat.debug_enable = True

    # Empty charge plan, the export windows we are testing
    charge_window_best = []
    charge_limit_best = []
    my_predbat.charge_window_best = charge_window_best
    my_predbat.charge_limit_best = charge_limit_best
    my_predbat.export_window_best = export_window_best
    my_predbat.export_limits_best = list(export_limits_best)

    # Baseline metric of the current plan
    best_metric = my_predbat.run_prediction_metric(charge_limit_best, charge_window_best, export_window_best, my_predbat.export_limits_best, end_record=end_record)[0]

    # Run the export more solar optimisation
    my_predbat.optimise_solar(best_metric, len(export_window_best))

    if len(expect_export_limit) != len(my_predbat.export_limits_best):
        print("ERROR: Expected {} export limits but got {}".format(len(expect_export_limit), len(my_predbat.export_limits_best)))
        failed = True
    else:
        for n in range(len(expect_export_limit)):
            if expect_export_limit[n] != my_predbat.export_limits_best[n]:
                print("ERROR: Expected export limit {} is {} but got {}".format(n, expect_export_limit[n], my_predbat.export_limits_best[n]))
                failed = True

    return failed


def run_optimise_solar_tests(my_predbat):
    print("**** Running Optimise solar tests ****")
    reset_inverter(my_predbat)
    failed = False

    # Three back to back idle export windows
    export_window_best = []
    for n in range(0, 3):
        export_window_best.append({"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": 15.0})

    # With PV and a generous threshold all idle windows become freeze export (99)
    failed |= run_optimise_solar(
        "keep_with_pv",
        my_predbat,
        export_window_best=export_window_best,
        export_limits_best=[100.0, 100.0, 100.0],
        expect_export_limit=[99.0, 99.0, 99.0],
        pv_amount=3.0,
        threshold=100.0,
    )

    # Without PV there is nothing to export so windows stay idle
    failed |= run_optimise_solar(
        "no_pv",
        my_predbat,
        export_window_best=export_window_best,
        export_limits_best=[100.0, 100.0, 100.0],
        expect_export_limit=[100.0, 100.0, 100.0],
        pv_amount=0.0,
        threshold=100.0,
    )

    # With PV but a negative threshold the candidate plan is always rejected and reverted
    failed |= run_optimise_solar(
        "revert_when_too_costly",
        my_predbat,
        export_window_best=export_window_best,
        export_limits_best=[100.0, 100.0, 100.0],
        expect_export_limit=[100.0, 100.0, 100.0],
        pv_amount=3.0,
        threshold=-100.0,
    )

    # Export freeze disabled - feature is a no-op
    failed |= run_optimise_solar(
        "freeze_disabled",
        my_predbat,
        export_window_best=export_window_best,
        export_limits_best=[100.0, 100.0, 100.0],
        expect_export_limit=[100.0, 100.0, 100.0],
        pv_amount=3.0,
        threshold=100.0,
        set_export_freeze=False,
    )

    # An already active export window (limit 0) is left untouched, only idle ones are converted
    failed |= run_optimise_solar(
        "skip_active_window",
        my_predbat,
        export_window_best=export_window_best,
        export_limits_best=[0.0, 100.0, 100.0],
        expect_export_limit=[0.0, 99.0, 99.0],
        pv_amount=3.0,
        threshold=100.0,
    )

    return failed
