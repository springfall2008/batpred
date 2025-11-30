# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from utils import calc_percent_limit
from tests.test_infra import reset_rates, reset_inverter, update_rates_import, update_rates_export
from prediction import Prediction
from compare import Compare


def run_optimise_all_windows(
    name,
    my_predbat,
    charge_window_best=[],
    export_window_best=[],
    pv_amount=0,
    load_amount=0,
    expect_charge_limit=[],
    expect_export_limit=[],
    expect_best_price=0.0,
    rate_import=10.0,
    rate_export=5.5,
    battery_size=100.0,
    battery_soc=0.0,
    hybrid=False,
    inverter_loss=1.0,
    best_soc_keep=0.0,
    best_soc_keep_weight=0.5,
    second_pass=False,
):
    print("Starting optimise all windows test {}".format(name))
    end_record = my_predbat.forecast_minutes
    failed = False
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.soc_max = battery_size
    my_predbat.soc_kw = battery_soc
    my_predbat.inverter_hybrid = hybrid
    my_predbat.inverter_loss = inverter_loss
    my_predbat.best_soc_keep = best_soc_keep
    my_predbat.best_soc_keep_weight = best_soc_keep_weight
    my_predbat.reserve = 0.5
    my_predbat.set_charge_freeze = True
    my_predbat.calculate_second_pass = second_pass

    reset_rates(my_predbat, rate_import, rate_export)
    update_rates_import(my_predbat, charge_window_best)
    update_rates_export(my_predbat, export_window_best)

    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = pv_amount / (60 / 5)
        load_step[minute] = load_amount / (60 / 5)
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
    my_predbat.debug_enable = True

    charge_limit_best = [0 for n in range(len(charge_window_best))]
    export_limits_best = [100 for n in range(len(export_window_best))]

    failed = False
    metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = my_predbat.run_prediction(
        charge_limit_best, charge_window_best, export_window_best, export_limits_best, False, end_record=end_record
    )
    # Save plan
    my_predbat.charge_limit_best = charge_limit_best
    my_predbat.charge_limit_percent_best = calc_percent_limit(charge_limit_best, my_predbat.soc_max)
    my_predbat.export_limits_best = export_limits_best
    my_predbat.charge_window_best = charge_window_best
    my_predbat.export_window_best = export_window_best

    # Optimise windows
    best_metric, best_cost, best_keep, best_cycle, best_carbon, best_import = my_predbat.optimise_all_windows(metric, metric_keep)
    charge_limit_best = my_predbat.charge_limit_best
    export_limits_best = my_predbat.export_limits_best
    charge_window_best = my_predbat.charge_window_best
    export_window_best = my_predbat.export_window_best

    # Predict
    metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = my_predbat.run_prediction(
        charge_limit_best, charge_window_best, export_window_best, export_limits_best, False, end_record=end_record, save="best"
    )

    # Save plan
    my_predbat.charge_limit_best = charge_limit_best
    my_predbat.charge_limit_percent_best = calc_percent_limit(charge_limit_best, my_predbat.soc_max)
    my_predbat.export_limits_best = export_limits_best
    my_predbat.charge_window_best = charge_window_best
    my_predbat.export_window_best = export_window_best

    if len(expect_charge_limit) != len(my_predbat.charge_limit_best):
        print("ERROR: Expected {} charge limits but got {}".format(len(expect_charge_limit), len(my_predbat.charge_limit_best)))
        failed = True
    else:
        for n in range(len(expect_charge_limit)):
            if expect_charge_limit[n] != my_predbat.charge_limit_best[n]:
                print("ERROR: Expected charge limit {} is {} but got {}".format(n, expect_charge_limit[n], my_predbat.charge_limit_best[n]))
                failed = True
    if len(expect_export_limit) != len(my_predbat.export_limits_best):
        print("ERROR: Expected {} discharge limits but got {}".format(len(expect_export_limit), len(my_predbat.export_limits_best)))
        failed = True
    else:
        for n in range(len(expect_export_limit)):
            if expect_export_limit[n] != my_predbat.export_limits_best[n]:
                print("ERROR: Expected discharge limit {} is {} but got {}".format(n, expect_export_limit[n], my_predbat.export_limits_best[n]))
                failed = True

    if abs(metric - expect_best_price) >= 0.2:
        print("ERROR: Expected best price {} but got {}".format(expect_best_price, metric))
        failed = True

    if failed:
        html_plan, raw_plan = my_predbat.publish_html_plan(my_predbat.pv_forecast_minute_step, my_predbat.pv_forecast_minute_step, my_predbat.load_minutes_step, my_predbat.load_minutes_step, end_record)
        open("plan.html", "w").write(html_plan)
        print("Wrote plan to plan.html")

    return failed


def run_optimise_all_windows_tests(my_predbat):
    print("**** Running Optimise all windows tests ****")
    reset_inverter(my_predbat)
    failed = False

    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 10.0}]

    failed |= run_optimise_all_windows(
        "single_windows",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=[0],
        load_amount=1.0,
        pv_amount=0,
        expect_best_price=1 * 10 * 24,
        inverter_loss=0.9,
    )

    # Created2
    charge_window_best = []
    export_window_best = []
    expect_charge_limit = []
    expect_export_limit = []
    for n in range(0, 48):
        price = 16 - n % 16
        charge_window_best.append({"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": price})
        expect_charge_limit.append(10 if price < 5.0 else 0)
    expect_charge_limit[27] = 0
    expect_charge_limit[28] = 0
    expect_charge_limit[44] = 0.5  # freeze
    failed |= run_optimise_all_windows(
        "created2_windows",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=expect_charge_limit,
        load_amount=0.2,
        pv_amount=0,
        expect_best_price=26.7502,
        inverter_loss=0.9,
        best_soc_keep=0.0,
        battery_size=10,
    )

    if failed:
        return failed

    # One extra charge as we will fall below keep otherwise
    expect_charge_limit[9] = 0.5
    expect_charge_limit[10] = 0.5
    expect_charge_limit[11] = 0.5
    expect_charge_limit[12] = 0.5
    expect_charge_limit[28] = 0.5
    failed |= run_optimise_all_windows(
        "created3_windows",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=expect_charge_limit,
        load_amount=0.2,
        pv_amount=0,
        expect_best_price=13.8,
        inverter_loss=0.9,
        best_soc_keep=1,
        battery_soc=2,
        battery_size=10,
        second_pass=True,
    )
    if failed:
        return failed

    # Created4
    charge_window_best = []
    export_window_best = []
    expect_charge_limit = []
    expect_export_limit = []
    for n in range(0, 48):
        price = n % 16
        charge_window_best.append({"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": price})
        expect_charge_limit.append(10 if price <= 5.0 * 0.9 else 0)

    expect_charge_limit[5] = 0.5
    expect_charge_limit[21] = 0.5
    expect_charge_limit[37] = 0.5
    failed |= run_optimise_all_windows(
        "created4_windows",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=expect_charge_limit,
        load_amount=0.2,
        pv_amount=0,
        expect_best_price=19.5,
        inverter_loss=0.9,
        best_soc_keep=1,
        battery_soc=2,
        battery_size=10,
    )
    if failed:
        return failed

    # Optimise charge limit
    best_soc, best_metric, best_cost, best_soc_min, best_soc_min_minute, best_keep, best_cycle, best_carbon, best_import = my_predbat.optimise_charge_limit(
        0, len(expect_charge_limit), expect_charge_limit, charge_window_best, export_window_best, expect_export_limit, all_n=None, end_record=my_predbat.end_record
    )
    before_best_metric = best_metric
    my_predbat.isCharging = True
    my_predbat.isCharging_Target = 100
    best_soc, best_metric, best_cost, best_soc_min, best_soc_min_minute, best_keep, best_cycle, best_carbon, best_import = my_predbat.optimise_charge_limit(
        0, len(expect_charge_limit), expect_charge_limit, charge_window_best, export_window_best, expect_export_limit, all_n=None, end_record=my_predbat.end_record
    )

    if (before_best_metric - best_metric) < 0.099:
        print("ERROR: Expected best metric to have 0.1 skew for charging but got {} vs {} skew was {}".format(best_metric, before_best_metric, before_best_metric - best_metric))
        failed = True
    if best_cost != 19.5:
        print("ERROR: Expected best cost to be 19.5 but got {}".format(best_cost))
        failed = True
    my_predbat.isCharging = False

    # Low rate tes
    charge_window_best = []
    export_window_best = []
    expect_charge_limit = []
    expect_export_limit = []
    for n in range(0, 48):
        n_mod = n % 24
        off_peak = True if n_mod > 12 else False
        price = 7 if off_peak else 30
        charge_window_best.append({"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": price})
        expect_charge_limit.append(10 if off_peak else 0)
    failed |= run_optimise_all_windows(
        "off_peak_windows",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=expect_charge_limit,
        load_amount=0.5,
        pv_amount=0,
        expect_best_price=115.5,
        inverter_loss=0.9,
        best_soc_keep=0.5,
        battery_size=10,
        battery_soc=5,
        rate_export=15,
    )

    # Compare test
    print("**** Compare test ****")
    compare_tariffs = [
        {"id": "base", "name": "Base", "config": {"load_scaling": 1.0}},
        {"id": "double", "name": "Double Load", "config": {"load_scaling": 2.0}},
    ]
    my_predbat.args["compare_list"] = compare_tariffs
    compare = Compare(my_predbat)
    compare.run_all(debug=True, fetch_sensor=False)

    results = compare.comparisons
    if len(results) != 2:
        print("ERROR: Compare expected 2 results but got {}".format(len(results)))
        failed = True
    else:
        result0 = results.get("base", None)
        result1 = results.get("double", None)
        if not result0:
            print("ERROR: Compare expected result 0 to be valid")
            failed = True
        if not result1:
            print("ERROR: Compare eExpected result 1 to be valid")
            failed = True
    #    if result0['cost'] != 115.5:
    #        print("ERROR: Expected result 0 cost to be 115.5 but got {}".format(result0['cost']))
    #    if result1['cost'] != 231.0:
    #        failed = True
    #        print("ERROR: Expected result 1 cost to be 231.0 but got {}".format(result1['cost']))
    #        failed = True

    return failed
