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
from tests.test_infra import reset_rates, update_rates_import, update_rates_export, reset_inverter
from prediction import Prediction


def run_optimise_levels(
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
    rate_export=5.0,
    battery_size=100.0,
    battery_soc=0.0,
    hybrid=False,
    inverter_loss=1.0,
    best_soc_keep=0.0,
):
    failed = False
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    reset_inverter(my_predbat)
    my_predbat.forecast_minutes = 24 * 60

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

    # Reset state that may have been set by previous tests
    my_predbat.best_soc_max = 0  # Reset SOC max cap - 0 means no cap
    my_predbat.best_soc_keep_weight = 0.5  # Reset to default
    my_predbat.metric_min_improvement = 0.0  # Reset to default
    my_predbat.metric_min_improvement_export = 0.1  # Reset to default
    my_predbat.end_record = 48 * 60
    my_predbat.best_soc_step = 0.25
    my_predbat.soc_percent = 0
    my_predbat.num_inverters = 1

    end_record = my_predbat.forecast_minutes

    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.soc_max = battery_size
    my_predbat.soc_kw = battery_soc
    my_predbat.inverter_hybrid = hybrid
    my_predbat.inverter_loss = inverter_loss
    my_predbat.best_soc_keep = best_soc_keep

    reset_rates(my_predbat, rate_import, rate_export)
    update_rates_import(my_predbat, charge_window_best)
    update_rates_export(my_predbat, export_window_best)

    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = pv_amount / (60 / 5)
        load_step[minute] = load_amount / (60 / 5)
    my_predbat.load_minutes_step = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
    my_predbat.debug_enable = True

    charge_limit_best = [0 for n in range(len(charge_window_best))]
    export_limits_best = [100 for n in range(len(export_window_best))]

    record_charge_windows = max(my_predbat.max_charge_windows(end_record + my_predbat.minutes_now, charge_window_best), 1)
    record_export_windows = max(my_predbat.max_charge_windows(end_record + my_predbat.minutes_now, export_window_best), 1)
    print("Starting optimise levels test {}".format(name))

    window_sorted, window_index, price_set, price_links = my_predbat.sort_window_by_price_combined(charge_window_best[:record_charge_windows], export_window_best[:record_export_windows])

    my_predbat.optimise_charge_windows_reset(reset_all=True)
    my_predbat.optimise_charge_windows_manual()
    (
        charge_limit_best,
        export_limits_best,
        best_metric,
        best_cost,
        best_keep,
        best_soc_min,
        best_cycle,
        best_carbon,
        best_import,
        best_battery_value,
        tried_list,
        level_results,
        best_max_charge_slots,
        best_max_export_slots,
    ) = my_predbat.optimise_charge_limit_price_threads(
        price_set,
        price_links,
        window_index,
        record_charge_windows,
        record_export_windows,
        charge_limit_best,
        charge_window_best,
        export_window_best,
        export_limits_best,
        end_record=end_record,
        fast=True,
        quiet=True,
    )
    best_price_charge, best_price_export, best_price_charge_level, best_price_export_level = my_predbat.find_price_levels(price_set, price_links, window_index, charge_limit_best, charge_window_best, export_window_best, export_limits_best)

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

    text = my_predbat.short_textual_plan(soc_min, soc_min_minute, pv_step, pv_step, load_step, load_step, end_record)
    print(text)

    if len(expect_charge_limit) != len(charge_limit_best):
        print("ERROR: Expected {} charge limits but got {}".format(len(expect_charge_limit), len(charge_limit_best)))
        failed = True
    else:
        for n in range(len(expect_charge_limit)):
            if expect_charge_limit[n] != charge_limit_best[n]:
                print("ERROR: Expected charge limit {} is {} but got {}".format(n, expect_charge_limit[n], charge_limit_best[n]))
                failed = True
    if len(expect_export_limit) != len(export_limits_best):
        print("ERROR: Expected {} discharge limits but got {}".format(len(expect_export_limit), len(export_limits_best)))
        failed = True
    else:
        for n in range(len(expect_export_limit)):
            if expect_export_limit[n] != export_limits_best[n]:
                print("ERROR: Expected discharge limit {} is {} but got {}".format(n, expect_export_limit[n], export_limits_best[n]))
                failed = True

    if abs(expect_best_price - best_price_charge) >= 0.2:
        print("ERROR: Expected best price {} but got {} ({})".format(expect_best_price, best_price_charge, best_price_charge_level))
        failed = True

    if failed:
        html_plan, raw_plan = my_predbat.publish_html_plan(my_predbat.pv_forecast_minute_step, my_predbat.pv_forecast_minute_step, my_predbat.load_minutes_step, my_predbat.load_minutes_step, end_record)
        open("plan.html", "w").write(html_plan)
        print("Wrote plan to plan.html")

        old_log = my_predbat.log
        my_predbat.log = print
        my_predbat.optimise_charge_limit_price_threads(
            price_set,
            price_links,
            window_index,
            record_charge_windows,
            record_export_windows,
            charge_limit_best,
            charge_window_best,
            export_window_best,
            export_limits_best,
            end_record=end_record,
            fast=True,
            quiet=True,
            test_mode=True,
        )
        my_predbat.log = old_log
        print(
            "Best price: {} Best metric: {} Best cost: {} Best keep: {} Best soc min: {} Best cycle: {} Best carbon: {} Best import: {}".format(best_price_charge_level, best_metric, best_cost, best_keep, best_soc_min, best_cycle, best_carbon, best_import)
        )
        print("Charge limit best: {} expected {} Discharge limit best {} expected {}".format(charge_limit_best, expect_charge_limit, export_limits_best, expect_export_limit))

    return failed, best_metric, best_keep, charge_limit_best, export_limits_best


def run_optimise_levels_tests(my_predbat):
    print("**** Running Optimise levels tests ****")
    reset_inverter(my_predbat)
    failed = False

    # Single charge window
    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 10.0}]

    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "single",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=[0],
        load_amount=1.0,
        pv_amount=0,
        expect_best_price=10.0,
        inverter_loss=0.9,
    )
    failed |= this_failed
    if failed:
        return failed

    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "single_pv",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=[0],
        load_amount=1.0,
        pv_amount=1.0,
        expect_best_price=10.0,
        inverter_loss=0.9,
    )
    failed |= this_failed
    if failed:
        return failed

    # Two windows charge low + high rate
    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 10.0}, {"start": my_predbat.minutes_now + 120, "end": my_predbat.minutes_now + 240, "average": 6}]

    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "dual",
        my_predbat,
        charge_window_best=charge_window_best,
        expect_charge_limit=[0, 100],
        load_amount=0.5,
        pv_amount=0,
        expect_best_price=6.0,
        inverter_loss=0.9,
    )
    failed |= this_failed
    if failed:
        return failed

    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "dual_pv", my_predbat, charge_window_best=charge_window_best, expect_charge_limit=[0, 0], load_amount=1.0, pv_amount=1.0, expect_best_price=6.0, inverter_loss=0.9
    )
    failed |= this_failed
    if failed:
        return failed

    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "dual_pv2", my_predbat, charge_window_best=charge_window_best, expect_charge_limit=[0, 100], load_amount=2.0, pv_amount=1.0, expect_best_price=6.0, inverter_loss=0.9
    )
    failed |= this_failed
    if failed:
        return failed

    # Discharge - create fresh window dictionaries to avoid state contamination
    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 10.0}, {"start": my_predbat.minutes_now + 120, "end": my_predbat.minutes_now + 240, "average": 6}]
    export_window_best = [{"start": my_predbat.minutes_now + 240, "end": my_predbat.minutes_now + 300, "average": 7.5}]
    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "discharge",
        my_predbat,
        charge_window_best=charge_window_best,
        export_window_best=export_window_best,
        expect_charge_limit=[0, 100],
        expect_export_limit=[0],
        load_amount=0,
        pv_amount=0,
        expect_best_price=6.0,
        inverter_loss=1,
    )
    failed |= this_failed
    if failed:
        return failed

    # Discharge
    export_window_best = [{"start": my_predbat.minutes_now + 240, "end": my_predbat.minutes_now + 300, "average": 6}]
    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "discharge2",
        my_predbat,
        charge_window_best=charge_window_best,
        export_window_best=export_window_best,
        expect_charge_limit=[0, 0],
        expect_export_limit=[100],
        load_amount=0,
        pv_amount=0,
        expect_best_price=6.0,
        inverter_loss=1,
        rate_export=6.0,
    )
    failed |= this_failed
    if failed:
        return failed

    # Created
    charge_window_best = []
    export_window_best = []
    expect_charge_limit = []
    expect_export_limit = []
    for n in range(0, 48):
        price = n % 8
        charge_window_best.append({"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": price})
        expect_charge_limit.append(100 if price <= 5.0 * 0.9 else 0)
    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "created1",
        my_predbat,
        charge_window_best=charge_window_best,
        export_window_best=export_window_best,
        expect_charge_limit=expect_charge_limit,
        expect_export_limit=expect_export_limit,
        load_amount=0.5,
        pv_amount=0,
        expect_best_price=4.0,
        inverter_loss=0.9,
    )
    failed |= this_failed
    if failed:
        return failed

    # Created2
    charge_window_best = []
    export_window_best = []
    expect_charge_limit = []
    expect_export_limit = []
    for n in range(0, 48):
        price = 16 - n % 16
        charge_window_best.append({"start": my_predbat.minutes_now + 30 * n, "end": my_predbat.minutes_now + 30 * (n + 1), "average": price})
        expect_charge_limit.append(100 if price <= 5.0 * 0.9 else 0)
    this_failed, best_metric, metric_keep, charge_limit_best, export_limit_best = run_optimise_levels(
        "created2",
        my_predbat,
        charge_window_best=charge_window_best,
        export_window_best=export_window_best,
        expect_charge_limit=expect_charge_limit,
        expect_export_limit=expect_export_limit,
        load_amount=0.2,
        pv_amount=0,
        expect_best_price=4.0,
        inverter_loss=0.9,
        best_soc_keep=0.5,
    )
    failed |= this_failed
    if failed:
        return failed

    return failed
