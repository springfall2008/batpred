# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for metric_battery_grid_charge_cost, an extra cost per kWh of grid energy that ends up in the battery.

Some installations lose money on every kWh charged from the grid on top of the import rate - for example a
generation subsidy paid on a generation meter that counts the energy going into a hybrid inverter as negative
generation. The setting adds that cost to the plan metric for the grid import that covers the battery's AC
charge demand only, so charging from solar and grid import that serves the house are unaffected.
"""

from config import CONFIG_ITEMS
from prediction import Prediction
from tests.test_infra import reset_inverter, reset_rates, reset_rates2, simple_scenario, update_rates_import
from utils import dp2
from tests.test_kernel_parity import kernel_available
from tests.test_optimise_levels import run_optimise_levels

GRID_CHARGE_COST = 4.0


def run_grid_charge_scenario(my_predbat, grid_charge_cost, **kwargs):
    """Run one simple_scenario with the given grid charge cost and return the raw prediction results.

    simple_scenario sets up the battery, inverter and step data and builds the Prediction (which copies the
    grid charge cost from my_predbat), then the scenario is re-run through thread_run_prediction_single so
    the caller gets the unrounded metric and import figures rather than simple_scenario's coarse asserts.
    """
    my_predbat.metric_battery_grid_charge_cost = grid_charge_cost
    _, prediction = simple_scenario(
        "grid_charge_cost_{}".format(grid_charge_cost),
        my_predbat,
        assert_final_metric=0,
        assert_final_soc=0,
        quiet=True,
        ignore_failed=True,
        return_prediction_handle=True,
        **kwargs,
    )
    charge = kwargs.get("charge", 0)
    charge_limit = [charge] if charge > 0 else []
    charge_window = [{"start": my_predbat.minutes_now, "end": my_predbat.forecast_minutes + my_predbat.minutes_now, "average": 0}] if charge > 0 else []
    metric, import_kwh_battery, import_kwh_house, export_kwh, soc_min, final_soc, soc_min_minute, battery_cycle, metric_keep, final_iboost, final_carbon_g = prediction.thread_run_prediction_single(
        charge_limit, charge_window, [], [], False, end_record=my_predbat.end_record, step=5
    )
    return metric, import_kwh_battery + import_kwh_house, final_soc


def check_grid_charge_delta(my_predbat, name, expected_grid_to_battery, **kwargs):
    """Compare a scenario with and without the grid charge cost.

    The metric must rise by exactly GRID_CHARGE_COST per kWh of grid energy into the battery, and nothing
    else about the simulation may change. expected_grid_to_battery may be a number or a callable taking
    the zero-cost run's (import_kwh, final_soc). Returns True on failure.
    """
    metric_zero, import_zero, soc_zero = run_grid_charge_scenario(my_predbat, 0.0, **kwargs)
    metric_cost, import_cost, soc_cost = run_grid_charge_scenario(my_predbat, GRID_CHARGE_COST, **kwargs)
    if callable(expected_grid_to_battery):
        expected_grid_to_battery = expected_grid_to_battery(import_zero, soc_zero)
    expected_delta = GRID_CHARGE_COST * expected_grid_to_battery
    delta = metric_cost - metric_zero
    failed = False
    # 4dp rounding of the kernel's scalar results leaves a little slack
    if abs(delta - expected_delta) > 0.002:
        print("ERROR: {}: metric rose by {} with the grid charge cost, expected {} ({} kWh grid to battery)".format(name, round(delta, 4), round(expected_delta, 4), round(expected_grid_to_battery, 4)))
        failed = True
    if abs(import_cost - import_zero) > 0.0001 or abs(soc_cost - soc_zero) > 0.0001:
        print("ERROR: {}: the grid charge cost changed the energy flows (import {} -> {}, final SoC {} -> {})".format(name, import_zero, import_cost, soc_zero, soc_cost))
        failed = True
    if not failed:
        print("  {}: metric +{} for {} kWh grid to battery - ok".format(name, round(delta, 4), round(expected_grid_to_battery, 4)))
    return failed


def run_grid_charge_simulation_tests(my_predbat, prediction_kernel):
    """The simulation charges the cost on grid energy into the battery only. Returns True on failure."""
    print("**** Running battery grid charge cost simulation tests{} ****".format(" (C++ prediction kernel)" if prediction_kernel else ""))
    my_predbat.prediction_kernel_enable = prediction_kernel
    reset_inverter(my_predbat)
    reset_rates(my_predbat, 10.0, 5.0)
    failed = False

    battery_loss = 0.95
    inverter_loss = 0.9
    common = dict(battery_size=10.0, battery_soc=0.0, charge=10.0, battery_loss=battery_loss, inverter_loss=inverter_loss, inverter_limit=3.0, export_limit=3.0, battery_rate_max_charge=1.0)
    # AC energy that has to go into the inverter to store this much in the battery
    grid_for_soc = lambda soc: soc / battery_loss / inverter_loss  # noqa: E731

    # Grid only: every kWh imported goes into the battery, including the inverter and battery losses
    failed |= check_grid_charge_delta(my_predbat, "grid_only_hybrid", lambda imp, soc: imp, load_amount=0.0, pv_amount=0.0, hybrid=True, **common)
    failed |= check_grid_charge_delta(my_predbat, "grid_only_ac_coupled", lambda imp, soc: imp, load_amount=0.0, pv_amount=0.0, hybrid=False, **common)
    failed |= check_grid_charge_delta(my_predbat, "grid_only_soc", lambda imp, soc: grid_for_soc(soc), load_amount=0.0, pv_amount=0.0, hybrid=True, **common)

    # House load during the charge: import covers the house too, but only the battery's share is charged
    failed |= check_grid_charge_delta(my_predbat, "grid_with_house_load", lambda imp, soc: grid_for_soc(soc), load_amount=0.5, pv_amount=0.0, hybrid=True, **common)

    # Solar covers the whole charge (DC on a hybrid, AC surplus on an AC-coupled system): no cost at all
    failed |= check_grid_charge_delta(my_predbat, "solar_charge_hybrid", 0.0, load_amount=0.0, pv_amount=2.0, hybrid=True, **common)
    failed |= check_grid_charge_delta(my_predbat, "solar_charge_ac_coupled", 0.0, load_amount=0.0, pv_amount=2.0, hybrid=False, **common)

    # Solar covers part of the charge on an AC-coupled system: only the grid top-up is charged
    failed |= check_grid_charge_delta(my_predbat, "part_solar_ac_coupled", lambda imp, soc: imp, load_amount=0.0, pv_amount=0.5, hybrid=False, **common)

    # No charge window, battery discharging to the house: no grid charging, no cost
    discharge = dict(common, battery_soc=10.0, charge=0)
    failed |= check_grid_charge_delta(my_predbat, "discharge_only", 0.0, load_amount=0.5, pv_amount=0.0, hybrid=True, **discharge)

    my_predbat.metric_battery_grid_charge_cost = 0.0
    my_predbat.prediction_kernel_enable = False
    reset_inverter(my_predbat)
    return failed


def run_grid_charge_battery_value_tests(my_predbat):
    """battery_value_rate counts the grid charge cost of refilling a kWh. Returns True on failure."""
    print("**** Running battery grid charge cost battery value tests ****")
    reset_inverter(my_predbat)
    # Alternating 10p / 20p so the replacement cost is not capped at the highest import rate
    reset_rates2(my_predbat, 10.0, 5.0)
    saved_scaling = my_predbat.metric_battery_value_export_scaling
    saved_inverter_loss = my_predbat.inverter_loss
    saved_battery_loss = my_predbat.battery_loss
    failed = False
    try:
        my_predbat.metric_battery_value_export_scaling = 1.0
        my_predbat.inverter_loss = 0.96
        my_predbat.battery_loss = 0.97
        minute = my_predbat.minutes_now
        my_predbat.metric_battery_grid_charge_cost = 0.0
        value_zero = my_predbat.battery_value_rate(minute)
        my_predbat.metric_battery_grid_charge_cost = GRID_CHARGE_COST
        value_cost = my_predbat.battery_value_rate(minute)
        expected = GRID_CHARGE_COST / my_predbat.inverter_loss / my_predbat.battery_loss
        if abs((value_cost - value_zero) - expected) > 1e-9:
            print("ERROR: battery_value_rate rose by {} with the grid charge cost, expected {}".format(value_cost - value_zero, expected))
            failed = True
        else:
            print("  battery value {} -> {} - ok".format(round(value_zero, 4), round(value_cost, 4)))
    finally:
        my_predbat.metric_battery_grid_charge_cost = 0.0
        my_predbat.metric_battery_value_export_scaling = saved_scaling
        my_predbat.inverter_loss = saved_inverter_loss
        my_predbat.battery_loss = saved_battery_loss
        reset_rates(my_predbat, 10.0, 5.0)
    return failed


def run_grid_charge_battery_value_cap_tests(my_predbat):
    """On a flat tariff the battery value stays at its cap, which the grid charge cost does not raise. Returns True on failure.

    The cap is what a stored kWh can realise by avoiding an import at the highest rate after losses. The
    grid charge cost is paid on the way into the battery, not on the way out, so it raises the replacement
    cost but not what discharging can save - lifting the cap with it would value stored energy above what
    it can realise, which is exactly what the cap is there to prevent.
    """
    print("**** Running battery grid charge cost battery value cap tests ****")
    reset_inverter(my_predbat)
    reset_rates(my_predbat, 10.0, 5.0)
    saved = (my_predbat.metric_battery_value_export_scaling, my_predbat.inverter_loss, my_predbat.battery_loss)
    failed = False
    try:
        my_predbat.metric_battery_value_export_scaling = 1.0
        my_predbat.inverter_loss = 0.96
        my_predbat.battery_loss = 0.97
        minute = my_predbat.minutes_now
        cap = 10.0 * my_predbat.inverter_loss * my_predbat.battery_loss - my_predbat.metric_battery_cycle
        my_predbat.metric_battery_grid_charge_cost = 0.0
        value_zero = my_predbat.battery_value_rate(minute)
        my_predbat.metric_battery_grid_charge_cost = GRID_CHARGE_COST
        value_cost = my_predbat.battery_value_rate(minute)
        if abs(value_zero - cap) > 1e-9 or abs(value_cost - cap) > 1e-9:
            print("ERROR: flat 10p tariff: battery value should stay at the {} cap with and without the grid charge cost, got {} and {}".format(round(cap, 4), value_zero, value_cost))
            failed = True
        else:
            print("  flat tariff battery value stays at the {} cap - ok".format(round(cap, 4)))
    finally:
        my_predbat.metric_battery_grid_charge_cost = 0.0
        my_predbat.metric_battery_value_export_scaling, my_predbat.inverter_loss, my_predbat.battery_loss = saved
        reset_rates(my_predbat, 10.0, 5.0)
    return failed


def run_grid_charge_output_tests(my_predbat):
    """The plan's adjusted import rate and the options log line include the grid charge cost. Returns True on failure.

    import_rate_adjusted (JSON plan, also read by the agent tools) and the plan_debug HTML figure are the
    cost of a kWh delivered from the battery after charging it from the grid at that slot's rate, so with a
    non-zero cost they must be (rate + cost) / losses + cycle cost. log_option_best names the setting only
    when it is non-zero.
    """
    print("**** Running battery grid charge cost output tests ****")
    failed = False
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    reset_inverter(my_predbat)
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.end_record = 48 * 60
    my_predbat.debug_enable = False
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 5.0
    my_predbat.num_inverters = 1
    my_predbat.reserve = 0.5
    my_predbat.inverter_loss = 0.96
    my_predbat.battery_loss = 0.97
    my_predbat.metric_battery_cycle = 0.5

    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = 0
        load_step[minute] = 0.5 / (60 / 5)
    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step

    saved_plan_debug = my_predbat.plan_debug
    saved_log = my_predbat.log
    try:
        for grid_charge_cost in (0.0, GRID_CHARGE_COST):
            my_predbat.metric_battery_grid_charge_cost = grid_charge_cost
            my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
            charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 10.0}]
            reset_rates(my_predbat, 10.0, 5.0)
            update_rates_import(my_predbat, charge_window_best)
            my_predbat.run_prediction([0], charge_window_best, [], [], False, end_record=my_predbat.end_record, save="best")
            my_predbat.charge_limit_best = [0]
            my_predbat.export_limits_best = []
            my_predbat.charge_window_best = charge_window_best
            my_predbat.export_window_best = []

            my_predbat.plan_debug = True
            html_plan, raw_plan = my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step, my_predbat.end_record, publish=False)
            row = next((r for r in (raw_plan or {}).get("rows", []) if r.get("slot_minute") == my_predbat.minutes_now), None)
            adjusted = (10.0 + grid_charge_cost) / my_predbat.battery_loss / my_predbat.inverter_loss + my_predbat.metric_battery_cycle
            if row is None:
                print("ERROR: grid charge cost {}: no plan row for minute {}".format(grid_charge_cost, my_predbat.minutes_now))
                failed = True
            elif row.get("import_rate_adjusted") != dp2(adjusted):
                print("ERROR: grid charge cost {}: import_rate_adjusted {} expected {}".format(grid_charge_cost, row.get("import_rate_adjusted"), dp2(adjusted)))
                failed = True
            if "(%02.02f)" % adjusted not in html_plan:
                print("ERROR: grid charge cost {}: plan_debug HTML does not show the adjusted import rate ({:.2f})".format(grid_charge_cost, adjusted))
                failed = True

            logged = []
            my_predbat.log = lambda msg, *args, **kwargs: logged.append(msg)
            my_predbat.log_option_best()
            my_predbat.log = saved_log
            options = "".join(logged)
            named = "metric_battery_grid_charge_cost(" in options
            if grid_charge_cost and "metric_battery_grid_charge_cost({}{}/kWh)".format(grid_charge_cost, my_predbat.currency_symbols[1]) not in options:
                print("ERROR: log_option_best does not show metric_battery_grid_charge_cost({}): {}".format(grid_charge_cost, options))
                failed = True
            elif not grid_charge_cost and named:
                print("ERROR: log_option_best names metric_battery_grid_charge_cost while it is 0: {}".format(options))
                failed = True
            if not failed:
                print("  grid charge cost {}: import_rate_adjusted {} and options log - ok".format(grid_charge_cost, dp2(adjusted)))
    finally:
        my_predbat.log = saved_log
        my_predbat.plan_debug = saved_plan_debug
        my_predbat.metric_battery_grid_charge_cost = 0.0
        my_predbat.load_user_config()
        my_predbat.fetch_config_options()
        reset_inverter(my_predbat)
        reset_rates(my_predbat, 10.0, 5.0)
    return failed


def run_grid_charge_config_tests(my_predbat):
    """The setting is an expert-mode input_number defaulting to 0 that fetch_config_options reads. Returns True on failure."""
    print("**** Running battery grid charge cost config tests ****")
    failed = False
    items = [item for item in CONFIG_ITEMS if item["name"] == "metric_battery_grid_charge_cost"]
    if len(items) != 1:
        print("ERROR: expected one metric_battery_grid_charge_cost config item, found {}".format(len(items)))
        return True
    item = items[0]
    if item.get("default") != 0 or item.get("enable") != "expert_mode" or item.get("type") != "input_number" or item.get("min") != 0:
        print("ERROR: metric_battery_grid_charge_cost config item is not an expert-mode input_number with default 0 and min 0: {}".format(item))
        failed = True

    original_expert_mode = my_predbat.config_index["expert_mode"].get("value")
    original_value = my_predbat.config_index["metric_battery_grid_charge_cost"].get("value")
    try:
        my_predbat.expose_config("expert_mode", True, force_ha=True)
        my_predbat.expose_config("metric_battery_grid_charge_cost", 4.05, force_ha=True)
        my_predbat.fetch_config_options()
        if my_predbat.metric_battery_grid_charge_cost != 4.05:
            print("ERROR: metric_battery_grid_charge_cost after fetch_config_options() should be 4.05, got {}".format(my_predbat.metric_battery_grid_charge_cost))
            failed = True
    finally:
        my_predbat.expose_config("metric_battery_grid_charge_cost", original_value, force_ha=True)
        my_predbat.expose_config("expert_mode", original_expert_mode, force_ha=True)
        my_predbat.fetch_config_options()
    return failed


def run_grid_charge_optimiser_tests(my_predbat):
    """The optimiser drops a grid charge that only paid off without the grid charge cost. Returns True on failure.

    Mirrors the "dual" optimise levels case: house load 0.5 kW at 10p, a 6p window with a 10% inverter loss.
    Charging there costs 6 / 0.9 = 6.7p per kWh delivered, so it is taken; with a 4p grid charge cost it is
    (6 + 4) / 0.9 = 11.1p, dearer than importing for the house at 10p, so it must not be.
    """
    print("**** Running battery grid charge cost optimiser tests ****")
    failed = False
    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 10.0}, {"start": my_predbat.minutes_now + 120, "end": my_predbat.minutes_now + 240, "average": 6}]
    this_failed, _, _, _, _ = run_optimise_levels("grid_charge_cost_zero", my_predbat, charge_window_best=charge_window_best, expect_charge_limit=[0, 100], load_amount=0.5, pv_amount=0, expect_best_price=6.0, inverter_loss=0.9, grid_charge_cost=0.0)
    failed |= this_failed
    this_failed, _, _, _, _ = run_optimise_levels(
        "grid_charge_cost_four", my_predbat, charge_window_best=charge_window_best, expect_charge_limit=[0, 0], load_amount=0.5, pv_amount=0, expect_best_price=6.0, inverter_loss=0.9, grid_charge_cost=GRID_CHARGE_COST
    )
    failed |= this_failed
    my_predbat.metric_battery_grid_charge_cost = 0.0
    return failed


def run_battery_grid_charge_cost_tests(my_predbat):
    """Run all metric_battery_grid_charge_cost tests, with the C++ kernel too when it is available. Returns True on failure."""
    failed = False
    failed |= run_grid_charge_config_tests(my_predbat)
    failed |= run_grid_charge_simulation_tests(my_predbat, prediction_kernel=False)
    available, required_failure = kernel_available()
    if available:
        failed |= run_grid_charge_simulation_tests(my_predbat, prediction_kernel=True)
    else:
        failed |= required_failure
    failed |= run_grid_charge_battery_value_tests(my_predbat)
    failed |= run_grid_charge_battery_value_cap_tests(my_predbat)
    failed |= run_grid_charge_output_tests(my_predbat)
    failed |= run_grid_charge_optimiser_tests(my_predbat)
    return failed
