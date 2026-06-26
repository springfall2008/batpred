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
    charge_window_best=None,
    charge_limit_best=None,
    expect_export_start=None,
    car_charging_slots=None,
    car_charging_from_battery=False,
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

    # Car charging defaults to none, but a car charging slot can be supplied to test overlap handling
    car_charging_slots = car_charging_slots if car_charging_slots is not None else []
    my_predbat.num_cars = 1 if car_charging_slots else 0
    my_predbat.car_charging_slots = [car_charging_slots]
    my_predbat.car_charging_from_battery = car_charging_from_battery

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

    # Charge plan defaults to empty, but a charge slot can be supplied to test overlap handling
    charge_window_best = charge_window_best if charge_window_best is not None else []
    charge_limit_best = charge_limit_best if charge_limit_best is not None else []
    my_predbat.charge_window_best = charge_window_best
    my_predbat.charge_limit_best = charge_limit_best
    my_predbat.export_window_best = export_window_best
    my_predbat.export_limits_best = list(export_limits_best)

    # Baseline metric of the current plan
    best_metric, best_battery_value, best_cost, best_keep, best_cycle, best_carbon, best_import, best_export = my_predbat.run_prediction_metric(charge_limit_best, charge_window_best, export_window_best, my_predbat.export_limits_best, end_record=end_record)

    # Run the export more solar optimisation
    my_predbat.optimise_solar(best_metric, best_cost, best_keep, best_cycle, best_carbon, best_import, len(export_window_best))

    if len(expect_export_limit) != len(my_predbat.export_limits_best):
        print("ERROR: Expected {} export limits but got {}".format(len(expect_export_limit), len(my_predbat.export_limits_best)))
        failed = True
    else:
        for n in range(len(expect_export_limit)):
            # None means "don't care" - used where the re-optimised value is not deterministic
            if expect_export_limit[n] is None:
                continue
            if expect_export_limit[n] != my_predbat.export_limits_best[n]:
                print("ERROR: Expected export limit {} is {} but got {}".format(n, expect_export_limit[n], my_predbat.export_limits_best[n]))
                failed = True

    if expect_export_start is not None:
        for n in range(len(expect_export_start)):
            # None means "don't care" for this window's start
            if expect_export_start[n] is None:
                continue
            if expect_export_start[n] != my_predbat.export_window_best[n]["start"]:
                print("ERROR: Expected export window {} start {} but got {}".format(n, expect_export_start[n], my_predbat.export_window_best[n]["start"]))
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

    # Windows spanning two calendar days are each considered independently
    # minutes_now is midday so the plan horizon straddles two calendar days (day boundary at +720)
    multi_day_window_best = [
        {"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 15.0},
        {"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 60, "average": 15.0},
        {"start": my_predbat.minutes_now + 720, "end": my_predbat.minutes_now + 750, "average": 15.0},
        {"start": my_predbat.minutes_now + 750, "end": my_predbat.minutes_now + 780, "average": 15.0},
    ]
    failed |= run_optimise_solar(
        "multi_day_keep",
        my_predbat,
        export_window_best=multi_day_window_best,
        export_limits_best=[100.0, 100.0, 100.0, 100.0],
        expect_export_limit=[99.0, 99.0, 99.0, 99.0],
        pv_amount=3.0,
        threshold=100.0,
    )

    # A force export slot that starts after the first solar of the day is re-optimised once the
    # freeze export is added. The idle window becomes freeze export and the now-unprofitable force
    # export is removed (limit goes from 0 back to 100), confirming the re-optimisation step trims it.
    force_export_window_best = [
        {"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 15.0},
        {"start": my_predbat.minutes_now + 60, "end": my_predbat.minutes_now + 90, "average": 15.0},
    ]
    failed |= run_optimise_solar(
        "force_export_after_solar",
        my_predbat,
        export_window_best=force_export_window_best,
        export_limits_best=[100.0, 0.0],
        expect_export_limit=[99.0, 100.0],
        pv_amount=3.0,
        threshold=100.0,
        battery_soc=8.0,
    )

    # An idle export window that overlaps a planned charge slot must NOT be turned into freeze export
    # (we can't charge and freeze export at the same time). The second window is free of charge.
    overlap_window_best = [
        {"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 15.0},
        {"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 60, "average": 15.0},
    ]
    overlap_charge_window_best = [
        {"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 5.0},
    ]
    failed |= run_optimise_solar(
        "skip_charge_overlap",
        my_predbat,
        export_window_best=overlap_window_best,
        export_limits_best=[100.0, 100.0],
        expect_export_limit=[100.0, 99.0],
        pv_amount=3.0,
        threshold=100.0,
        charge_window_best=overlap_charge_window_best,
        charge_limit_best=[my_predbat.soc_max],
    )

    # An idle export window that overlaps a car charging slot must NOT be turned into freeze export
    # (freeze export disables charging so the battery can't be topped up for the car). The second
    # window is free of any car charging slot and is still converted.
    car_overlap_window_best = [
        {"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 15.0},
        {"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 60, "average": 15.0},
    ]
    car_charging_slots = [
        {"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "kwh": 5.0},
    ]
    failed |= run_optimise_solar(
        "skip_car_overlap",
        my_predbat,
        export_window_best=car_overlap_window_best,
        export_limits_best=[100.0, 100.0],
        expect_export_limit=[100.0, 99.0],
        pv_amount=3.0,
        threshold=100.0,
        car_charging_slots=car_charging_slots,
    )

    # When the car is allowed to charge from the battery the car slot does not block freeze export
    failed |= run_optimise_solar(
        "car_overlap_allowed_from_battery",
        my_predbat,
        export_window_best=car_overlap_window_best,
        export_limits_best=[100.0, 100.0],
        expect_export_limit=[99.0, 99.0],
        pv_amount=3.0,
        threshold=100.0,
        car_charging_slots=car_charging_slots,
        car_charging_from_battery=True,
    )

    # An export window that is already freeze export but was trimmed earlier (start moved later than
    # start_orig) is restored to its full original size to cover the whole solar period.
    trimmed_window_best = [
        {"start": my_predbat.minutes_now + 30, "start_orig": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 15.0},
    ]
    failed |= run_optimise_solar(
        "restore_trimmed_freeze",
        my_predbat,
        export_window_best=trimmed_window_best,
        export_limits_best=[99.0],
        expect_export_limit=[99.0],
        expect_export_start=[my_predbat.minutes_now],
        pv_amount=3.0,
        threshold=100.0,
    )

    return failed
