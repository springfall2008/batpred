# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2024 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
import os
import json
from compare import Compare
from prediction import Prediction
from tests.test_infra import reset_inverter


def run_single_debug(test_name, my_predbat, debug_file, expected_file=None, compare=False, debug=False):
    print("**** Running debug test {} ****\n".format(debug_file))
    if not expected_file:
        re_do_rates = True
        reset_load_model = True
        reload_octopus_slots = True
    else:
        reset_load_model = False
        re_do_rates = False
        reload_octopus_slots = False
    load_override = 1.0
    my_predbat.load_user_config()
    failed = False

    print("**** Test {} ****".format(test_name))
    reset_inverter(my_predbat)
    my_predbat.read_debug_yaml(debug_file)
    my_predbat.config_root = "./"
    my_predbat.save_restore_dir = "./"
    my_predbat.load_user_config()
    my_predbat.args["threads"] = 0
    # my_predbat.fetch_config_options()

    # Force off combine export XXX:
    print("Combined export slots {} min_improvement_export {} set_export_freeze_only {}".format(my_predbat.combine_export_slots, my_predbat.metric_min_improvement_export, my_predbat.set_export_freeze_only))
    if not expected_file:
        my_predbat.plan_debug = True
        my_predbat.debug_enable = debug
        # my_predbat.set_discharge_during_charge = True
        # my_predbat.calculate_export_oncharge = True
        # my_predbat.combine_charge_slots = True
        # my_predbat.combine_export_slots = False
        # my_predbat.metric_min_improvement_export = 0.1
        # my_predbat.metric_min_improvement_export_freeze = 0.1
        # my_predbat.metric_min_improvement = 0.0
        # my_predbat.set_reserve_min = 0

        # my_predbat.metric_self_sufficiency = 5
        # my_predbat.calculate_second_pass = False
        # my_predbat.best_soc_keep = 0
        # my_predbat.best_soc_keep_weight = 0.5
        # my_predbat.rate_low_threshold = 0
        # my_predbat.rate_high_threshold = 0
        # my_predbat.set_charge_freeze = True
        # my_predbat.set_export_freeze = True
        # my_predbat.combine_export_slots = False
        # my_predbat.set_export_freeze = False
        # my_predbat.inverter_loss = 0.97
        # my_predbat.calculate_tweak_plan = False

        # my_predbat.inverter_loss = 0.97
        # my_predbat.calculate_second_pass = True
        # my_predbat.calculate_tweak_plan = True
        # my_predbat.metric_battery_cycle = 2
        # my_predbat.carbon_enable = False
        # my_predbat.metric_battery_value_scaling = 0.50
        # my_predbat.manual_export_times = []
        # my_predbat.manual_all_times = []
        # my_predbat.manual_charge_times = []
        # my_predbat.manual_demand_times = []
        # my_predbat.manual_freeze_charge_times = []
        # my_predbat.manual_freeze_export_times = []
        # my_predbat.battery_loss = 0.97
        # my_predbat.battery_loss_discharge = 0.97
        # my_predbat.set_export_low_power = False
        # my_predbat.combine_charge_slots = False
        # my_predbat.charge_limit_best[0] = 0
        # my_predbat.charge_limit_best[1] = 0
        # my_predbat.iboost_solar_excess = True
        # my_predbat.iboost_min_power = 500 / MINUTE_WATT
        pass

    print("Charge scaling 10 {} load scaling 10 {}".format(my_predbat.charge_scaling10, my_predbat.load_scaling10))

    if re_do_rates:
        # Set rate thresholds
        if my_predbat.rate_import or my_predbat.rate_export:
            print("Set rate thresholds")
            my_predbat.set_rate_thresholds()
            print("Result export {} import {}".format(my_predbat.rate_export_cost_threshold, my_predbat.rate_import_cost_threshold))

        # Find discharging windows
        if my_predbat.rate_export:
            my_predbat.high_export_rates, export_lowest, export_highest = my_predbat.rate_scan_window(my_predbat.rate_export, 5, my_predbat.rate_export_cost_threshold, True, alt_rates=my_predbat.rate_import)
            print("High export rate found rates in range {} to {} based on threshold {}".format(export_lowest, export_highest, my_predbat.rate_export_cost_threshold))
            # Update threshold automatically
            if my_predbat.rate_high_threshold == 0 and export_lowest <= my_predbat.rate_export_max:
                my_predbat.rate_export_cost_threshold = export_lowest

        # Find charging windows
        if my_predbat.rate_import:
            # Find charging window
            print("rate scan window import threshold rate {}".format(my_predbat.rate_import_cost_threshold))
            my_predbat.low_rates, lowest, highest = my_predbat.rate_scan_window(my_predbat.rate_import, 5, my_predbat.rate_import_cost_threshold, False, alt_rates=my_predbat.rate_export)
            # Update threshold automatically
            if my_predbat.rate_low_threshold == 0 and highest >= my_predbat.rate_min:
                my_predbat.rate_import_cost_threshold = highest
    else:
        print("don't re-do rates")

    if compare:
        print("Run compare")
        compare_tariffs = [
            {"name": "Fixed exports", "rates_export": [{"rate": 15.0}], "config": {"load_scaling": 2.0}},
            {"name": "Agile export", "rates_export_octopus_url": "https://api.octopus.energy/v1/products/AGILE-OUTGOING-BB-23-02-28/electricity-tariffs/E-1R-AGILE-OUTGOING-BB-23-02-28-A/standard-unit-rates/"},
        ]
        my_predbat.args["compare"] = compare_tariffs
        compare = Compare(my_predbat)
        compare.run_all(debug=True)
        return

    # Reset load model
    if reset_load_model:
        print("Reset load model")
        my_predbat.load_minutes_step = my_predbat.step_data_history(
            my_predbat.load_minutes,
            my_predbat.minutes_now,
            forward=False,
            scale_today=my_predbat.load_inday_adjustment,
            scale_fixed=1.0 * load_override,
            type_load=True,
            load_forecast=my_predbat.load_forecast,
            load_scaling_dynamic=my_predbat.load_scaling_dynamic,
            cloud_factor=my_predbat.metric_load_divergence,
        )
        my_predbat.load_minutes_step10 = my_predbat.step_data_history(
            my_predbat.load_minutes,
            my_predbat.minutes_now,
            forward=False,
            scale_today=my_predbat.load_inday_adjustment,
            scale_fixed=my_predbat.load_scaling10 * load_override,
            type_load=True,
            load_forecast=my_predbat.load_forecast,
            load_scaling_dynamic=my_predbat.load_scaling_dynamic,
            cloud_factor=min(my_predbat.metric_load_divergence + 0.5, 1.0) if my_predbat.metric_load_divergence else None,
        )
        my_predbat.pv_forecast_minute_step = my_predbat.step_data_history(my_predbat.pv_forecast_minute, my_predbat.minutes_now, forward=True, cloud_factor=my_predbat.metric_cloud_coverage)
        my_predbat.pv_forecast_minute10_step = my_predbat.step_data_history(
            my_predbat.pv_forecast_minute10, my_predbat.minutes_now, forward=True, cloud_factor=min(my_predbat.metric_cloud_coverage + 0.2, 1.0) if my_predbat.metric_cloud_coverage else None, flip=True
        )

    pv_step = my_predbat.pv_forecast_minute_step
    pv10_step = my_predbat.pv_forecast_minute10_step
    load_step = my_predbat.load_minutes_step
    load10_step = my_predbat.load_minutes_step10

    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)

    failed = False
    my_predbat.log("> ORIGINAL PLAN")

    metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = my_predbat.run_prediction(
        my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, False, end_record=my_predbat.end_record, save="best"
    )

    if my_predbat.num_cars > 0 and my_predbat.octopus_slots:
        if reload_octopus_slots:
            my_predbat.car_charging_slots[0] = my_predbat.load_octopus_slots(my_predbat.octopus_slots, my_predbat.octopus_intelligent_consider_full)
            print("Re-loaded car charging slots {}".format(my_predbat.car_charging_slots[0]))
        else:
            print("Current car charging slots {}".format(my_predbat.car_charging_slots[0]))

    # Show setting changes
    if not expected_file:
        for item in my_predbat.CONFIG_ITEMS:
            name = item["name"]
            default = item.get("default", None)
            value = item.get("value", default)
            enable = item.get("enable", None)
            enabled = my_predbat.user_config_item_enabled(item)
            if enabled and value != default:
                print("- {} = {} (default {}) - enable {}".format(name, value, default, enable))

    # Save plan
    # Pre-optimise all plan
    my_predbat.update_target_values()
    html_plan, raw_plan = my_predbat.publish_html_plan(pv_step, pv10_step, load_step, load10_step, my_predbat.end_record)
    filename = "plan_orig.html"
    open(filename, "w").write(html_plan)
    print("Wrote plan to {} metric {}".format(filename, metric))

    ## Calculate the plan
    my_predbat.plan_valid = False
    print("Re-calculate plan")
    my_predbat.calculate_plan(recompute=True, debug_mode=debug)
    print("Plan calculated")

    # Predict
    my_predbat.log("> FINAL PLAN")
    metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = my_predbat.run_prediction(
        my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, False, end_record=my_predbat.end_record, save="best"
    )
    my_predbat.log("Final plan soc_min {} final_soc {}".format(soc_min, soc))

    html_plan, raw_plan = my_predbat.publish_html_plan(pv_step, pv10_step, load_step, load10_step, my_predbat.end_record)
    filename = "plan_final.html"
    open(filename, "w").write(html_plan)
    filename = "plan_final.json"
    open(filename, "w").write(json.dumps(raw_plan, indent=2))
    print("Wrote plan to {} metric {}".format(filename, metric))

    # Expected
    actual_data = {"charge_limit_best": my_predbat.charge_limit_best, "charge_window_best": my_predbat.charge_window_best, "export_window_best": my_predbat.export_window_best, "export_limits_best": my_predbat.export_limits_best}
    actual_json = json.dumps(actual_data)
    if expected_file:
        print("Compare with {}".format(expected_file))
        if not os.path.exists(expected_file):
            failed = True
            print("ERROR: Expected file {} does not exist".format(expected_file))
        else:
            expected_data = json.loads(open(expected_file).read())
            expected_json = json.dumps(expected_data)
            if actual_json != expected_json:
                print("ERROR: Actual plan does not match expected plan")
                failed = True
    # Write actual plan
    filename = test_name + ".actual.json"
    open(filename, "w").write(actual_json)
    print("Wrote plan json to {}".format(filename))

    my_predbat.create_debug_yaml(write_file=True)

    return failed
