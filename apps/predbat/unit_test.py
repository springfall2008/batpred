# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import copy
import os
import time
import sys
import glob
from datetime import datetime, timedelta
import argparse
import asyncio

try:
    import requests
except ImportError:
    requests = None
try:
    import yaml
except ImportError:
    yaml = None
try:
    import json
except ImportError:
    json = None
try:
    import matplotlib.pyplot as plt
except ImportError:
    plt = None
try:
    import numpy as np
except ImportError:
    np = None

from predbat import PredBat
from prediction import Prediction
from utils import (
    calc_percent_limit,
    remove_intersecting_windows,
    find_charge_rate,
    dp2,
    dp4,
    minute_data,
    get_override_time_from_string,
    window2minutes,
    compute_window_minutes,
    get_now_from_cumulative,
    prune_today,
    history_attribute,
    minute_data_state,
    format_time_ago,
)
from futurerate import FutureRate
from config import MINUTE_WATT, INVERTER_MAX_RETRY_REST, PREDICT_STEP
from inverter import Inverter
from compare import Compare
from gecloud import GECloudDirect
from octopus import OctopusAPI
from components import Components
from alertfeed import AlertFeed
from tests.test_infra import TestHAInterface, TestInverter, reset_rates, reset_rates2, reset_inverter, update_rates_import, update_rates_export, simple_scenario
from tests.test_compute_metric import run_compute_metric_tests
from tests.test_perf import run_perf_test
from tests.test_model import run_model_tests
from tests.test_execute import run_execute_tests
from tests.test_octopus_slots import run_load_octopus_slots_tests
from tests.test_multi_inverter import run_inverter_multi_tests
from tests.test_window2minutes import test_window2minutes
from tests.test_history_attribute import test_history_attribute
from tests.test_inverter import run_inverter_tests
from tests.test_basic_rates import test_basic_rates

# Mock the components and plugin system
from unittest.mock import MagicMock, patch

KEEP_SCALE = 0.5


def plot(name, prediction):
    """
    Plot the prediction
    """
    fig, ax = plt.subplots()
    # Predict_soc is a hash on minutes since the start of simulation and the SOC value
    # Convert this into a NP array for plotting
    minutes = np.array(list(prediction.predict_soc.keys()))
    predict_soc = np.array(list(prediction.predict_soc.values()))
    metric_pence = list(prediction.predict_metric_best.values())
    metric = [round(x / 100, 2) for x in metric_pence]
    metric = np.array(metric)
    ax.plot(minutes, predict_soc, label="soc")
    ax.plot(minutes, metric, label="metric")
    ax.set_xticks(range(0, prediction.forecast_minutes, 240))
    ax.set(xlabel="time (minutes)", ylabel="Value", title=name)
    ax.legend()
    plt.savefig("{}.png".format(name))
    plt.show()


def run_nordpool_test(my_predbat):
    """
    Test the compute metric function
    """

    print("**** Running Nordpool tests ****")
    my_predbat.args["futurerate_url"] = "https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices?date=DATE&market=N2EX_DayAhead&deliveryArea=UK&currency=GBP"
    my_predbat.args["futurerate_adjust_import"] = False
    my_predbat.args["futurerate_adjust_export"] = False
    my_predbat.args["futurerate_peak_start"] = "16:00:00"
    my_predbat.args["futurerate_peak_end"] = "19:00:00"
    my_predbat.args["futurerate_peak_premium_import"] = 14
    my_predbat.args["futurerate_peak_premium_export"] = 6.5
    my_predbat.args["futurerate_adjust_import"] = True
    my_predbat.args["futurerate_adjust_export"] = True
    failed = False

    fixed = my_predbat.download_octopus_rates("https://api.octopus.energy/v1/products/OUTGOING-SEG-EO-FIX-12M-24-04-05/electricity-tariffs/E-1R-OUTGOING-SEG-EO-FIX-12M-24-04-05-A/standard-unit-rates/")
    if max(fixed.values()) <= 0:
        print("ERROR: Fixed rates can not be zero")
        failed = True
    if min(fixed.values()) != max(fixed.values()):
        print("ERROR: Fixed rates can not change")
        failed = True
    if len(fixed) > 6 * 24 * 60:
        print("ERROR: Fixed rates too long got {}".format(len(fixed)))
        failed = True

    # Obtain Agile octopus data
    rates_agile = my_predbat.download_octopus_rates("https://api.octopus.energy/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-A/standard-unit-rates/")
    if not rates_agile:
        print("ERROR: No import rate data from Octopus url {}".format("https://api.octopus.energy/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-A/standard-unit-rates/"))
        failed = True
    rates_agile_export = my_predbat.download_octopus_rates("https://api.octopus.energy/v1/products/AGILE-OUTGOING-BB-23-02-28/electricity-tariffs/E-1R-AGILE-OUTGOING-BB-23-02-28-A/standard-unit-rates/")
    if not rates_agile_export:
        print("ERROR: No export rate data from Octopus url {}".format("https://api.octopus.energy/v1/products/AGILE-OUTGOING-BB-23-02-28/electricity-tariffs/E-1R-AGILE-OUTGOING-BB-23-02-28-A/standard-unit-rates/"))
        failed = True
    print("Agile rates downloaded...")

    future = FutureRate(my_predbat)
    rate_import, rate_export = future.futurerate_analysis(rates_agile, rates_agile_export)
    if not rate_import:
        print("ERROR: No rate import data")
        return True
    if not rate_export:
        print("ERROR: No rate export data")
        return True

    future.download_futurerate_data_func = lambda x: ("empty")  # Mock the download function
    rate_import2, rate_export2 = future.futurerate_analysis(rates_agile, rates_agile_export)
    for key in rate_import:
        if rate_import[key] != rate_import2.get(key, None):
            print("ERROR: Rate import data not the same got {} vs {}".format(rate_import[key], rate_import2.get(key, None)))
            failed = True
            break
    for key in rate_export:
        if rate_export[key] != rate_export2.get(key, None):
            print("ERROR: Rate export data not the same got {} vs {}".format(rate_export[key], rate_export2.get(key, None)))
            failed = True
            break

    # Compute the minimum value in the hash, ignoring the keys
    min_import = min(rate_import.values())
    min_export = min(rate_export.values())
    max_import = max(rate_import.values())
    max_export = max(rate_export.values())

    if min_import == max_import:
        print("ERROR: Rate import data is flat")
        failed = True
    if min_export == max_export:
        print("ERROR: Rate import data is flat")
        failed = True
    if min_import < -15 or max_import > 100:
        print("ERROR: Rate import data out of range got min {} max {}".format(min_import, max_import))
        failed = True
    if min_export < 0 or max_export > 100:
        print("ERROR: Rate export data out of range got min {} max {}".format(min_export, max_export))
        failed = True

    # Compare Agile rates against Nordpool
    max_diff = 0
    rate_diff = 0
    for minute in range(0, 24 * 60, 30):
        rate_octopus = rates_agile.get(minute, None)
        rate_nordpool = rate_import.get(minute, None)
        if rate_octopus is not None and rate_nordpool is not None:
            rate_diff = abs(rate_octopus - rate_nordpool)
            max_diff = max(max_diff, rate_diff)
            # print("Import: Minute {} Octopus {} Nordpool {} diff {}".format(my_predbat.time_abs_str(minute), rate_octopus, rate_nordpool, rate_diff))
    if max_diff > 10:
        print("ERROR: Rate import data difference too high")
        failed = True

    rate_diff_export = 0
    for minute in range(0, 24 * 60, 30):
        rate_octopus = rates_agile_export.get(minute, None)
        rate_nordpool = rate_export.get(minute, None)
        if rate_octopus is not None and rate_nordpool is not None:
            rate_diff_export = abs(rate_octopus - rate_nordpool)
            max_diff = max(rate_diff_export, rate_diff)
            # print("Export: Minute {} Octopus {} Nordpool {} diff {}".format(my_predbat.time_abs_str(minute), rate_octopus, rate_nordpool, rate_diff))
    if rate_diff_export > 10:
        print("ERROR: Rate export data difference too high")
        failed = True

    return failed


def test_plugin_startup_order(my_predbat):
    """
    Test that plugins are initialized before the web server starts
    This ensures plugin endpoints can be registered before the router freezes
    """
    print("*** Running test: Plugin startup order and endpoint registration")
    failed = 0

    # Create a mock for tracking call order
    call_order = []

    # Mock the Components class
    mock_components = MagicMock()
    mock_components.initialize = MagicMock(side_effect=lambda **kwargs: call_order.append("components.initialize"))
    mock_components.start = MagicMock(side_effect=lambda **kwargs: call_order.append("components.start") or True)

    # Mock the PluginSystem class
    mock_plugin_system = MagicMock()
    mock_plugin_system.discover_plugins = MagicMock(side_effect=lambda: call_order.append("plugin.discover"))
    mock_plugin_system.call_hooks = MagicMock(side_effect=lambda hook: call_order.append(f"plugin.hook.{hook}"))

    # Test the initialization order
    # Patch needs to return a callable that returns the mock
    with patch("predbat.Components", MagicMock(return_value=mock_components)):
        with patch("predbat.PluginSystem", MagicMock(return_value=mock_plugin_system)):
            # Create a minimal predbat instance for testing
            test_predbat = PredBat()
            test_predbat.reset()
            test_predbat.log = MagicMock()
            test_predbat.reset = MagicMock()
            test_predbat.auto_config = MagicMock()
            test_predbat.load_user_config = MagicMock()
            test_predbat.create_test_elements = MagicMock()
            test_predbat.expose_config = MagicMock()
            test_predbat.run_time_loop = MagicMock()
            test_predbat.ha_interface = MagicMock()
            test_predbat.prefix = "test"
            test_predbat.had_errors = False
            test_predbat.dashboard_index = []
            test_predbat.dashboard_values = {}
            test_predbat.args = {}

            print("ha_interface = ", test_predbat.ha_interface)

            # Clear the call order
            call_order = []

            # Run the initialization
            try:
                test_predbat.update_time()
                test_predbat.initialize()
            except Exception as e:
                # Some exceptions are expected since we're mocking heavily
                print(f"Exception: {e}")
                return 1

    # Verify the order
    # Components should be initialized, then plugins discovered, then components started
    components_init_index = -1
    plugin_discover_index = -1
    components_start_index = -1

    for i, call in enumerate(call_order):
        if call == "components.initialize" and components_init_index == -1:
            components_init_index = i
        elif call == "plugin.discover" and plugin_discover_index == -1:
            plugin_discover_index = i
        elif call == "components.start" and components_start_index == -1:
            components_start_index = i

    if components_init_index == -1:
        print("ERROR: Components.initialize was not called during initialization")
        failed = 1
    elif plugin_discover_index == -1:
        print("ERROR: Plugin discovery was not called during initialization")
        failed = 1
    elif components_start_index == -1:
        print("ERROR: Components.start was not called during initialization")
        failed = 1
    elif components_init_index >= plugin_discover_index:
        print(f"ERROR: Components must be initialized (index {components_init_index}) before plugin discovery (index {plugin_discover_index})")
        print(f"Call order was: {call_order}")
        failed = 1
    elif plugin_discover_index >= components_start_index:
        print(f"ERROR: Plugin discovery (index {plugin_discover_index}) must happen before components.start (index {components_start_index})")
        print(f"Call order was: {call_order}")
        failed = 1
    else:
        print(f"OK: Correct startup order - Components.initialize ({components_init_index}) -> Plugin.discover ({plugin_discover_index}) -> Components.start ({components_start_index})")

    # Now test that a plugin can register an endpoint
    print("*** Testing plugin endpoint registration")

    # Create a mock web component
    mock_web = MagicMock()
    mock_web.registered_endpoints = []
    mock_web.register_endpoint = MagicMock(side_effect=lambda path, handler, method: mock_web.registered_endpoints.append({"path": path, "handler": handler, "method": method}))

    # Create mock components that returns our web component
    mock_components_with_web = MagicMock()
    mock_components_with_web.get_component = MagicMock(return_value=mock_web)

    # Create a test plugin that registers an endpoint
    class TestPlugin:
        def __init__(self, base):
            self.base = base

        def register_hooks(self, plugin_system):
            # Register endpoint immediately like the metrics plugin now does
            if hasattr(self.base, "components"):
                web = self.base.components.get_component("web")
                if web:
                    web.register_endpoint("/test", lambda: "test", "GET")

    # Test the plugin registration
    test_base = MagicMock()
    test_base.components = mock_components_with_web
    test_plugin = TestPlugin(test_base)
    test_plugin.register_hooks(None)

    # Verify the endpoint was registered
    if len(mock_web.registered_endpoints) == 0:
        print("ERROR: Plugin failed to register endpoint")
        failed = 1
    elif mock_web.registered_endpoints[0]["path"] != "/test":
        print(f"ERROR: Wrong endpoint path registered: {mock_web.registered_endpoints[0]['path']}")
        failed = 1
    else:
        print(f"OK: Plugin successfully registered endpoint /test")

    return failed


def run_iboost_smart_test(test_name, my_predbat, today=0, max_energy=1, max_power=1, min_length=0, expect_cost=0, expect_kwh=0, expect_time=0):
    failed = False
    print("**** Running Test: {} ****".format(test_name))

    my_predbat.iboost_smart = True
    my_predbat.iboost_slots = []
    my_predbat.iboost_today = today
    my_predbat.iboost_max_energy = max_energy
    my_predbat.iboost_max_power = max_power / 60
    my_predbat.iboost_smart_min_length = min_length

    slots = my_predbat.plan_iboost_smart()
    total_kwh = 0
    total_cost = 0
    total_time = 0
    for slot in slots:
        total_kwh += slot["kwh"]
        total_cost += slot["cost"]
        total_time += slot["end"] - slot["start"]
    if total_time != expect_time:
        print("ERROR: Iboost total time should be {} got {}".format(expect_time, total_time))
        print(slots)
        failed = True
    if total_kwh != expect_kwh:
        print("ERROR: Iboost total kwh should be {} got {}".format(expect_kwh, total_kwh))
        print(slots)
        failed = True
    if total_cost != expect_cost:
        print(slots)
        print("ERROR: Iboost total cost should be {} got {}".format(expect_cost, total_cost))
        failed = True

    my_predbat.iboost_smart = False
    my_predbat.iboost_slots = []
    my_predbat.iboost_today = 0

    return failed


def run_car_charging_smart_test(test_name, my_predbat, battery_size=10.0, limit=8.0, soc=0, rate=10.0, loss=1.0, max_price=99, smart=True, plan_time="00:00:00", expect_cost=0, expect_kwh=0):
    """
    Run a car charging smart test
    """
    failed = False

    print("**** Running Test: {} ****".format(test_name))

    my_predbat.car_charging_battery_size = [battery_size]
    my_predbat.car_charging_limit = [limit]
    my_predbat.car_charging_soc = [soc]
    my_predbat.car_charging_soc_next = [None]
    my_predbat.car_charging_rate = [rate]
    my_predbat.car_charging_loss = loss
    my_predbat.car_charging_plan_max_price = [max_price]
    my_predbat.car_charging_plan_smart = [smart]
    my_predbat.car_charging_plan_time = [plan_time]
    my_predbat.num_cars = 1

    my_predbat.car_charging_slots[0] = my_predbat.plan_car_charging(0, my_predbat.low_rates)
    total_kwh = 0
    total_cost = 0
    for slot in my_predbat.car_charging_slots[0]:
        total_kwh += slot["kwh"]
        total_cost += slot["cost"]
    if total_kwh != expect_kwh:
        print("ERROR: Car charging total kwh should be {} got {}".format(expect_kwh, total_kwh))
        failed = True
    total_pd = my_predbat.car_charge_slot_kwh(my_predbat.minutes_now, my_predbat.minutes_now + my_predbat.forecast_minutes)
    if total_pd != expect_kwh:
        print("ERROR: Car charging total calculated with car_charge_slot_kwh should be {} got {}".format(expect_kwh, total_pd))
        failed = True
    if total_cost != expect_cost:
        print("ERROR: Car charging total cost should be {} got {}".format(expect_cost, total_cost))
        failed = True

    return failed


def run_iboost_smart_tests(my_predbat):
    """
    Test for Iboost smart
    """
    failed = False
    reset_inverter(my_predbat)

    import_rate = 10.0
    export_rate = 5.0
    reset_rates2(my_predbat, import_rate, export_rate)
    my_predbat.low_rates, lowest, highest = my_predbat.rate_scan_window(my_predbat.rate_import, 5, my_predbat.rate_import_cost_threshold, False)

    failed |= run_iboost_smart_test("iboost1", my_predbat, today=0, max_energy=5, max_power=1, min_length=0, expect_cost=import_rate * 5 * 2, expect_kwh=5 * 2, expect_time=5 * 2 * 60)
    failed |= run_iboost_smart_test("iboost2", my_predbat, today=4.9, max_energy=5, max_power=1, min_length=0, expect_cost=import_rate * (0.1 + 5), expect_kwh=(0.1 + 5), expect_time=10 + 5 * 60)
    failed |= run_iboost_smart_test("iboost3", my_predbat, today=4.95, max_energy=5, max_power=1, min_length=0, expect_cost=import_rate * (0.05 + 5), expect_kwh=(0.05 + 5), expect_time=5 + 5 * 60)

    return failed


def run_car_charging_smart_tests(my_predbat):
    """
    Test car charging smart
    """
    failed = False
    reset_inverter(my_predbat)

    print("**** Running Car Charging Smart tests ****")
    import_rate = 10.0
    export_rate = 5.0
    reset_rates2(my_predbat, import_rate, export_rate)
    my_predbat.low_rates, lowest, highest = my_predbat.rate_scan_window(my_predbat.rate_import, 5, my_predbat.rate_import_cost_threshold, False)

    failed |= run_car_charging_smart_test("smart1", my_predbat, battery_size=12.0, limit=10.0, soc=0, rate=10.0, loss=1.0, max_price=99, smart=True, expect_cost=100, expect_kwh=10)
    failed |= run_car_charging_smart_test("smart2", my_predbat, battery_size=12.0, limit=10.0, soc=0, rate=10.0, loss=1.0, max_price=99, smart=False, expect_cost=150, expect_kwh=10)
    failed |= run_car_charging_smart_test("smart3", my_predbat, battery_size=12.0, limit=10.0, soc=2, rate=10.0, loss=1.0, max_price=99, smart=True, expect_cost=80, expect_kwh=8)
    failed |= run_car_charging_smart_test("smart4", my_predbat, battery_size=12.0, limit=10.0, soc=2, rate=10.0, loss=0.5, max_price=99, smart=True, expect_cost=160, expect_kwh=16)
    failed |= run_car_charging_smart_test("smart5", my_predbat, battery_size=100.0, limit=100.0, soc=0, rate=1.0, loss=1, max_price=99, smart=True, expect_cost=12 * 15, expect_kwh=12, plan_time="00:00:00")
    failed |= run_car_charging_smart_test("smart6", my_predbat, battery_size=100.0, limit=100.0, soc=0, rate=1.0, loss=1, max_price=99, smart=True, expect_cost=14 * 15, expect_kwh=14, plan_time="02:00:00")
    failed |= run_car_charging_smart_test("smart7", my_predbat, battery_size=100.0, limit=100.0, soc=0, rate=1.0, loss=1, max_price=10, smart=True, expect_cost=7 * 10, expect_kwh=7, plan_time="02:00:00")
    failed |= run_car_charging_smart_test("smart8", my_predbat, battery_size=100.0, limit=100.0, soc=0, rate=1.0, loss=1, max_price=10, smart=False, expect_cost=7 * 10, expect_kwh=7, plan_time="02:00:00")

    return failed


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
    my_predbat.charge_limit_percent_best = calc_percent_limit(my_predbat.charge_limit_best, my_predbat.soc_max)
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


def run_test_ge_cloud(my_predbat):
    """
    GE Cloud test
    """
    failed = False

    ge_cloud_direct = GECloudDirect(my_predbat)
    ge_cloud_direct_task = my_predbat.create_task(ge_cloud_direct.start())
    while not "devices" in ge_cloud_direct.__dict__:
        time.sleep(1)
    devices = ge_cloud_direct.devices
    if not devices:
        print("ERROR: No devices found")
        failed = True
    else:
        for device in devices:
            print("Device {} found:".format(device))
            while not ge_cloud_direct.settings.get(device):
                time.sleep(1)
            print("Device {} synced".format(device))

        my_predbat.create_task(ge_cloud_direct.switch_event("switch.predbat_gecloud_sa2243g277_ac_charge_enable", "turn_on"))
        time.sleep(1)
    print("Stopping cloud")
    ge_cloud_direct.stop_cloud = True
    time.sleep(1)

    return failed


def run_test_web_if(my_predbat):
    """
    Test the web interface
    """
    failed = 0
    print("**** Running web interface test ****\n")
    orig_ha_if = my_predbat.ha_interface
    my_predbat.components = Components(my_predbat)
    my_predbat.components.initialize()
    my_predbat.components.start("ha_interface")
    my_predbat.components.start("db")
    my_predbat.components.start("web")
    ha = my_predbat.ha_interface

    # Fetch page from 127.0.0.1:5052
    for page in ["/", "/dash", "/plan", "/config", "/apps", "/charts", "/compare", "/log", "/entity", "/components", "/browse"]:
        print("Fetch page {}".format(page))
        address = "http://127.0.0.1:5052" + page
        res = requests.get(address)
        if res.status_code != 200:
            print("ERROR: Failed to fetch from page {} got status {} value {}".format(address, res.status_code, res.text))
            failed = 1

    # Perform a post to /compare page with data for form 'compareform' value 'run'
    print("**** Running test: Fetch page /compare with post")

    address = "http://127.0.0.1:5052/compare"
    data = {"run": "run"}
    res = requests.post(address, data=data)
    if res.status_code != 200:
        print("ERROR: Failed to post to pagepage {} got status {} value {}".format(address, res.status_code, res.text))
        failed = 1
    time.sleep(0.1)
    # Get service data
    entity_id = "switch.predbat_compare_active"
    result = ha.get_state(entity_id)

    if result != "on":
        print("ERROR: Compare tariffs not triggered - expected {} got {}".format("on", result))
        failed = 1

    # Run stop as task as we need to await it
    my_predbat.create_task(my_predbat.components.stop("ha_interface"))
    my_predbat.create_task(my_predbat.components.stop("web"))
    my_predbat.create_task(my_predbat.components.stop("db"))
    time.sleep(0.1)
    my_predbat.components = Components(my_predbat)
    my_predbat.ha_interface = orig_ha_if
    return failed


def run_window_sort_test(name, my_predbat, charge_window_best, export_window_best, expected=[], inverter_loss=1.0, metric_battery_cycle=0.0, battery_loss=1.0, battery_loss_discharge=1.0):
    failed = False
    end_record = my_predbat.forecast_minutes
    my_predbat.calculate_best_charge = True
    my_predbat.calculate_best_export = True
    my_predbat.inverter_loss = inverter_loss
    my_predbat.metric_battery_cycle = metric_battery_cycle
    my_predbat.battery_loss = battery_loss
    my_predbat.battery_loss_discharge = battery_loss_discharge
    my_predbat.prediction.pv_forecast_minute_step = {}

    # Pretend we have PV all the time to allow discharge freeze to appear in the sort
    for minute in range(end_record + my_predbat.minutes_now):
        my_predbat.prediction.pv_forecast_minute_step[minute] = 1.0

    print("Starting window sort test {}".format(name))

    record_charge_windows = max(my_predbat.max_charge_windows(end_record + my_predbat.minutes_now, charge_window_best), 1)
    record_export_windows = max(my_predbat.max_charge_windows(end_record + my_predbat.minutes_now, export_window_best), 1)

    window_sorted, window_index, price_set, price_links = my_predbat.sort_window_by_price_combined(charge_window_best[:record_charge_windows], export_window_best[:record_export_windows])

    results = []
    for price_key in price_set:
        links = price_links[price_key]
        for key in links:
            typ = window_index[key]["type"]
            window_n = window_index[key]["id"]
            price = window_index[key]["average"]
            results.append((str(typ) + "_" + str(window_n) + "_" + str(price)))

    if len(expected) != len(results):
        print("ERROR: Expected {} results but got {}".format(len(expected), len(results)))
        failed = True
    else:
        for n in range(len(expected)):
            if expected[n] != results[n]:
                print("ERROR: Expected item {} is {} but got {}".format(n, expected[n], results[n]))
                failed = True
    if failed:
        print("Inputs: {} {}".format(charge_window_best, export_window_best))
        print("Results: {}".format(results))

    return failed


def run_intersect_window_tests(my_predbat):
    print("**** Running intersect window tests ****")
    failed = False
    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 10}]
    export_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 30, "average": 10}]
    charge_limit_best = [4]
    export_limit_best = [2]
    new_limit_best, new_window_best = remove_intersecting_windows(charge_limit_best, charge_window_best, export_limit_best, export_window_best)
    if len(new_window_best) != 0:
        print("ERROR: Expected no windows but got {}".format(new_window_best))
        failed = True
    return failed


def run_window_sort_tests(my_predbat):
    import_rate = 10.0
    export_rate = 5.0
    reset_inverter(my_predbat)
    reset_rates(my_predbat, import_rate, export_rate)
    failed = False

    failed |= run_window_sort_test("none", my_predbat, [], [], expected=[])

    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": import_rate}]
    failed |= run_window_sort_test("single_charge", my_predbat, charge_window_best, [], expected=["c_0_10.0", "cf_0_10.0"])
    failed |= run_window_sort_test("single_charge_loss", my_predbat, charge_window_best, [], expected=["c_0_20.0", "cf_0_10.0"], inverter_loss=0.5)

    export_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": export_rate}]
    failed |= run_window_sort_test("single_discharge", my_predbat, [], export_window_best, expected=["d_0_5.0", "df_0_5.0"])
    failed |= run_window_sort_test("single_charge_discharge", my_predbat, charge_window_best, export_window_best, expected=["c_0_10.0", "cf_0_10.0", "d_0_5.0", "df_0_5.0"])
    failed |= run_window_sort_test("single_charge_discharge_loss", my_predbat, charge_window_best, export_window_best, expected=["c_0_20.0", "cf_0_10.0", "df_0_5.0", "d_0_2.4"], inverter_loss=0.5)
    export_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 50.0}]
    failed |= run_window_sort_test("single_charge_discharge_loss2", my_predbat, charge_window_best, export_window_best, expected=["df_0_50.0", "d_0_25.0", "c_0_20.0", "cf_0_10.0"], inverter_loss=0.5)
    failed |= run_window_sort_test("single_charge_discharge_loss3", my_predbat, charge_window_best, export_window_best, expected=["c_0_200.0", "df_0_50.0", "d_0_25.0", "cf_0_10.0"], inverter_loss=0.5, battery_loss=0.1)
    failed |= run_window_sort_test(
        "single_charge_discharge_loss4",
        my_predbat,
        charge_window_best,
        export_window_best,
        expected=["c_0_200.0", "df_0_50.0", "cf_0_10.0", "d_0_2.4"],
        inverter_loss=0.5,
        battery_loss=0.1,
        battery_loss_discharge=0.1,
    )

    charge_window_best.append({"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": import_rate * 2})
    failed |= run_window_sort_test(
        "single_charge_discharge2",
        my_predbat,
        charge_window_best,
        export_window_best,
        expected=["c_1_400.0", "c_0_200.0", "df_0_50.0", "cf_1_20.0", "cf_0_10.0", "d_0_2.4"],
        inverter_loss=0.5,
        battery_loss=0.1,
        battery_loss_discharge=0.1,
    )
    export_window_best.append({"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": export_rate * 3})
    failed |= run_window_sort_test("single_charge_discharge3", my_predbat, charge_window_best, export_window_best, expected=["d_0_50.0", "df_0_50.0", "c_1_20.0", "cf_1_20.0", "d_1_15.0", "df_1_15.0", "c_0_10.0", "cf_0_10.0"])
    failed |= run_window_sort_test("single_charge_discharge3_c1", my_predbat, charge_window_best, export_window_best, expected=["df_0_50.0", "d_0_49.0", "c_1_21.0", "cf_1_20.0", "df_1_15.0", "d_1_14.0", "c_0_11.0", "cf_0_10.0"], metric_battery_cycle=1.0)

    return failed


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

    # Discharge
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
    end_record = my_predbat.forecast_minutes
    failed = False
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


def test_find_charge_rate(my_predbat):
    failed = 0

    # 2025-01-20 04:41:34.362134: Inverter 0 Charge window will be: 2025-01-19 23:30:00+00:00 - 2025-01-20 05:30:00+00:00 - current soc 95 target 100
    # 2025-01-20 04:41:34.364437: Low Power mode: minutes left: 40 absolute: 50 SOC: 9.04 Target SOC: 9.52 Charge left: 0.48 Max rate: 2600.0 Min rate: 576.0 Best rate: 2600.0 Best rate real: 1274.0 Battery temp 17.0
    # 2025-01-20 04:41:34.364497: Inverter 0 Target SOC 100 (this inverter 100.0) Battery temperature 17.0 Select charge rate 2600w (real 1274.0w) current charge rate 952
    # 2025-01-20 04:41:34.364549: Inverter 0 current charge rate is 952W and new target is 2600W
    # 2025-01-20 04:41:34.695878: Inverter 0 set charge rate 2600 via REST successful on retry 0

    current_charge_rate = 952
    soc = 9.04
    soc_max = 9.52
    log_to = print  # my_predbat.log
    minutes_now = my_predbat.minutes_now
    window = {"start": minutes_now - 60, "end": minutes_now + 50}
    target_soc = soc_max
    battery_charge_power_curve = {100: 0.15, 99: 0.15, 98: 0.23, 97: 0.3, 96: 0.42, 95: 0.49, 94: 0.55, 93: 0.69, 92: 0.79, 91: 0.89, 90: 0.96}
    set_charge_low_power = True
    charge_low_power_margin = my_predbat.charge_low_power_margin
    battery_rate_min = 0
    battery_rate_max_scaling = 1
    battery_loss = 0.96
    battery_temperature = 17.0
    battery_temperature_curve = {19: 0.33, 18: 0.33, 17: 0.33, 16: 0.33, 15: 0.33, 14: 0.33, 13: 0.33, 12: 0.33, 11: 0.33, 10: 0.25, 9: 0.25, 8: 0.25, 7: 0.25, 6: 0.25, 5: 0.25, 4: 0.25, 3: 0.25, 2: 0.25, 1: 0.15, 0: 0.00}
    max_rate = 2500

    best_rate, best_rate_real = find_charge_rate(
        minutes_now,
        soc,
        window,
        target_soc,
        max_rate / MINUTE_WATT,
        soc_max,
        battery_charge_power_curve,
        set_charge_low_power,
        charge_low_power_margin,
        battery_rate_min / MINUTE_WATT,
        battery_rate_max_scaling,
        battery_loss,
        log_to,
        battery_temperature=battery_temperature,
        battery_temperature_curve=battery_temperature_curve,
        current_charge_rate=current_charge_rate / MINUTE_WATT,
    )
    print("Best_rate {} Best_rate_real {}".format(best_rate * MINUTE_WATT, best_rate_real * MINUTE_WATT))
    if best_rate * MINUTE_WATT != 2500:
        print("**** ERROR: Best rate should be 2500 ****")
        failed = 1
    if best_rate_real * MINUTE_WATT != 1225:
        print("**** ERROR: Best real rate should be 1225 ****")
        failed = 1
    return failed


def test_energydataservice(my_predbat):
    """
    Test the energy data service
    """
    failed = 0

    print("Test energy data service")

    date_yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"

    data_example = f"""
state: 1.34
state_class: total
current_price: 1.2706032
unit: kWh
currency: EUR
region: Finland
region_code: FI
tomorrow_valid: true
next_data_update: 13:39:54
today: 1.242, 1.242, 1.242, 1.242, 1.243, 1.243, 1.243, 1.243, 1.243, 1.243, 1.244, 1.245, 1.246, 1.246, 1.261, 1.271, 1.286, 1.295, 1.295, 1.288, 1.296, 1.284, 1.273, 1.289
tomorrow: 1.284, 1.273, 1.263, 1.283, 1.309, 1.333, 1.385, 1.389, 1.366, 1.336, 1.323, 1.316, 1.318, 1.338, 1.338, 1.314, 1.299, 1.299, 1.297, 1.286, 1.281, 1.277, 1.268
raw_today:
    - hour: '{today}T00:00:00+{tz_offset}:00'
      price: 1.242
    - hour: '{today}T01:00:00+{tz_offset}:00'
      price: 1.242
    - hour: '{today}T02:00:00+{tz_offset}:00'
      price: 1.242
    - hour: '{today}T03:00:00+{tz_offset}:00'
      price: 1.242
    - hour: '{today}T04:00:00+{tz_offset}:00'
      price: 1.243
    - hour: '{today}T05:00:00+{tz_offset}:00'
      price: 1.243
    - hour: '{today}T06:00:00+{tz_offset}:00'
      price: 1.243
    - hour: '{today}T07:00:00+{tz_offset}:00'
      price: 1.243
    - hour: '{today}T08:00:00+{tz_offset}:00'
      price: 1.243
    - hour: '{today}T09:00:00+{tz_offset}:00'
      price: 1.243
    - hour: '{today}T10:00:00+{tz_offset}:00'
      price: 1.244
    - hour: '{today}T11:00:00+{tz_offset}:00'
      price: 1.245
    - hour: '{today}T12:00:00+{tz_offset}:00'
      price: 1.246
    - hour: '{today}T13:00:00+{tz_offset}:00'
      price: 1.246
    - hour: '{today}T14:00:00+{tz_offset}:00'
      price: 1.261
    - hour: '{today}T15:00:00+{tz_offset}:00'
      price: 1.271
    - hour: '{today}T16:00:00+{tz_offset}:00'
      price: 1.286
    - hour: '{today}T17:00:00+{tz_offset}:00'
      price: 1.295
    - hour: '{today}T18:00:00+{tz_offset}:00'
      price: 1.295
    - hour: '{today}T19:00:00+{tz_offset}:00'
      price: 1.288
    - hour: '{today}T20:00:00+{tz_offset}:00'
      price: 1.296
    - hour: '{today}T21:00:00+{tz_offset}:00'
      price: 1.284
    - hour: '{today}T22:00:00+{tz_offset}:00'
      price: 1.273
    - hour: '{today}T23:00:00+{tz_offset}:00'
      price: 1.289
raw_tomorrow:
    - hour: '{tomorrow}T00:00:00+{tz_offset}:00'
      price: 1.284
    - hour: '{tomorrow}T01:00:00+{tz_offset}:00'
      price: 1.273
    - hour: '{tomorrow}T02:00:00+{tz_offset}:00'
      price: 1.263
    - hour: '{tomorrow}T03:00:00+{tz_offset}:00'
      price: 1.283
    - hour: '{tomorrow}T04:00:00+{tz_offset}:00'
      price: 1.309
    - hour: '{tomorrow}T05:00:00+{tz_offset}:00'
      price: 1.333
    - hour: '{tomorrow}T06:00:00+{tz_offset}:00'
      price: 1.385
    - hour: '{tomorrow}T07:00:00+{tz_offset}:00'
      price: 1.389
    - hour: '{tomorrow}T08:00:00+{tz_offset}:00'
      price: 1.366
    - hour: '{tomorrow}T09:00:00+{tz_offset}:00'
      price: 1.336
    - hour: '{tomorrow}T10:00:00+{tz_offset}:00'
      price: 1.323
    - hour: '{tomorrow}T11:00:00+{tz_offset}:00'
      price: 1.316
    - hour: '{tomorrow}T12:00:00+{tz_offset}:00'
      price: 1.318
    - hour: '{tomorrow}T13:00:00+{tz_offset}:00'
      price: 1.338
    - hour: '{tomorrow}T14:00:00+{tz_offset}:00'
      price: 1.338
    - hour: '{tomorrow}T15:00:00+{tz_offset}:00'
      price: 1.314
    - hour: '{tomorrow}T16:00:00+{tz_offset}:00'
      price: 1.299
    - hour: '{tomorrow}T17:00:00+{tz_offset}:00'
      price: 1.299
    - hour: '{tomorrow}T18:00:00+{tz_offset}:00'
      price: 1.297
    - hour: '{tomorrow}T19:00:00+{tz_offset}:00'
      price: 1.286
    - hour: '{tomorrow}T20:00:00+{tz_offset}:00'
      price: 1.281
    - hour: '{tomorrow}T21:00:00+{tz_offset}:00'
      price: 1.277
    - hour: '{tomorrow}T22:00:00+{tz_offset}:00'
      price: 1.268
    - hour: '{tomorrow}T23:00:00+{tz_offset}:00'
      price: 1.268
today_min:
    hour: '{today}T00:00:00+{tz_offset}:00'
    price: 1.242
today_max:
    hour: '{today}T20:00:00+{tz_offset}:00'
    price: 1.296
today_mean: 1.26
tomorrow_min:
    hour: '{tomorrow}T02:00:00+{tz_offset}:00'
    price: 1.263
tomorrow_max:
    hour: '{tomorrow}T07:00:00+{tz_offset}:00'
    price: 1.389
tomorrow_mean: 1.312
use_cent: false
attribution: Data sourced from Nord Pool
unit_of_measurement: EUR/kWh
device_class: monetary
icon: mdi:flash
friendly_name: Energi Data Service
"""

    ha = my_predbat.ha_interface
    ha.dummy_items["sensor.energi_data_service"] = yaml.safe_load(data_example)
    my_predbat.args["energi_data_service"] = "sensor.energi_data_service"
    rates = my_predbat.fetch_energidataservice_rates("sensor.energi_data_service")

    show = []
    for minute in range(0, 48 * 60, 15):
        show.append(rates[minute])

    expected_show = [
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.2,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.3,
        124.4,
        124.4,
        124.4,
        124.4,
        124.5,
        124.5,
        124.5,
        124.5,
        124.6,
        124.6,
        124.6,
        124.6,
        124.6,
        124.6,
        124.6,
        124.6,
        126.1,
        126.1,
        126.1,
        126.1,
        127.1,
        127.1,
        127.1,
        127.1,
        128.6,
        128.6,
        128.6,
        128.6,
        129.5,
        129.5,
        129.5,
        129.5,
        129.5,
        129.5,
        129.5,
        129.5,
        128.8,
        128.8,
        128.8,
        128.8,
        129.6,
        129.6,
        129.6,
        129.6,
        128.4,
        128.4,
        128.4,
        128.4,
        127.3,
        127.3,
        127.3,
        127.3,
        128.9,
        128.9,
        128.9,
        128.9,
        128.4,
        128.4,
        128.4,
        128.4,
        127.3,
        127.3,
        127.3,
        127.3,
        126.3,
        126.3,
        126.3,
        126.3,
        128.3,
        128.3,
        128.3,
        128.3,
        130.9,
        130.9,
        130.9,
        130.9,
        133.3,
        133.3,
        133.3,
        133.3,
        138.5,
        138.5,
        138.5,
        138.5,
        138.9,
        138.9,
        138.9,
        138.9,
        136.6,
        136.6,
        136.6,
        136.6,
        133.6,
        133.6,
        133.6,
        133.6,
        132.3,
        132.3,
        132.3,
        132.3,
        131.6,
        131.6,
        131.6,
        131.6,
        131.8,
        131.8,
        131.8,
        131.8,
        133.8,
        133.8,
        133.8,
        133.8,
        133.8,
        133.8,
        133.8,
        133.8,
        131.4,
        131.4,
        131.4,
        131.4,
        129.9,
        129.9,
        129.9,
        129.9,
        129.9,
        129.9,
        129.9,
        129.9,
        129.7,
        129.7,
        129.7,
        129.7,
        128.6,
        128.6,
        128.6,
        128.6,
        128.1,
        128.1,
        128.1,
        128.1,
        127.7,
        127.7,
        127.7,
        127.7,
        126.8,
        126.8,
        126.8,
        126.8,
        126.8,
        126.8,
        126.8,
        126.8,
    ]

    if json.dumps(show) != json.dumps(expected_show):
        print("ERROR: Expecting show should be:\n {} got:\n {}".format(expected_show, show))
        failed = 1

    return failed


def test_alert_feed(my_predbat):
    """
    Test the alert feed
    """
    failed = 0
    ha = my_predbat.ha_interface
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"

    birmingham = [52.4823, -1.8900]
    bristol = [51.4545, -2.5879]
    manchester = [53.4808, -2.2426]
    fife = [56.2082, -3.1495]

    alert_data = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2">
  <link href="https://pubsubhubbub.appspot.com/" rel="hub"/>
  <link href="https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-united-kingdom" rel="self" type="application/atom+xml"/>
  <link href="https://meteoalarm.org" rel="alternate" type="text/html"/>
  <rights>Copyright © 2025 MeteoAlarm.Org. Licensed under terms equivalent to CC BY 4.0, with additional requirements for redistributing outlined in our Terms and Conditions.</rights>
  <generator>MeteoAlarm Producer Server</generator>
  <logo>https://feeds.meteoalarm.org/images/logo.svg</logo>
  <author>
    <name>meteoalarm.org</name>
    <uri>https://meteoalarm.org</uri>
    <email>meteoalarm@geosphere.at</email>
  </author>
  <id>tag:meteoalarm.org,2021-02-19:UK</id>
  <title>MeteoAlarm - Alerting Europe for Extreme Weather</title>
  <updated>2025-01-24T18:07:55.906349Z</updated>
  <entry>
    <cap:polygon>56.3439,-7.4487 55.9892,-7.1082 55.5659,-6.8445 55.3573,-6.8774 55.2572,-7.0367 55.1569,-7.1466 55.1161,-7.3718 55.0768,-7.4625 55.0217,-7.5064 54.9287,-7.5586 54.8402,-7.6245 54.7991,-7.6959 54.7959,-7.8113 54.788,-7.9019 54.7595,-7.9623 54.7183,-7.9926 54.6659,-7.9926 54.6246,-7.9953 54.4956,-7.207 54.4892,-6.4929 54.355,-5.7623 54.0658,-4.8999 53.5468,-3.1311 53.7032,-2.2302 53.927,-1.7523 54.2396,-0.5383 54.3742,-0.2307 54.4956,-0.2637 54.6421,-0.4724 54.8133,-0.8459 55.0091,-1.0547 55.3541,-1.1865 55.6528,-1.2964 55.8691,-1.4502 56.1149,-1.8457 56.3043,-2.0215 56.4989,-2.0544 56.8009,-1.9885 57.0766,-1.8018 57.3087,-1.582 57.5099,-1.5271 57.6454,-1.571 57.7687,-1.7084 57.8097,-1.9336 57.8184,-2.2028 57.7921,-2.7081 57.7921,-3.0817 57.8331,-3.3069 57.8973,-3.4277 57.9732,-3.4662 58.0692,-3.3838 58.2546,-3.0267 58.5224,-2.6093 58.9273,-1.9995 59.3612,-1.3623 59.7841,-0.835 60.1634,-0.5328 60.6301,-0.3571 60.8155,-0.401 60.9224,-0.5328 60.9758,-0.7471 60.9758,-1.1096 60.8556,-1.5875 60.6058,-2.1094 60.3568,-2.428 59.7896,-3.0322 59.322,-3.6584 59.0179,-4.4495 58.8535,-5.4822 58.6826,-6.5479 58.4937,-7.2729 58.3153,-7.6904 58.0023,-8.053 57.5571,-8.2288 57.172,-8.1738 56.903,-8.0859 56.5353,-7.6904 56.3439,-7.4487</cap:polygon>
    <link href="https://meteoalarm.org?polygon=3ec5a08b-995f-45fc-88eb-d364b1613e41,0,0,0" hreflang="en" title="Central, Tayside &amp; Fife | Grampian | Highlands &amp; Eilean Siar | North East England | North West England | Northern Ireland | Orkney &amp; Shetland | Strathclyde | SW Scotland, Lothian Borders | Yorkshire &amp; Humber"/>
    <cap:areaDesc>Central, Tayside &amp; Fife | Grampian | Highlands &amp; Eilean Siar | North East England | North West England | Northern Ireland | Orkney &amp; Shetland | Strathclyde | SW Scotland, Lothian Borders | Yorkshire &amp; Humber</cap:areaDesc>
    <cap:event>Yellow wind warning</cap:event>
    <cap:sent>2025-01-24T18:01:26+{tz_offset}:00</cap:sent>
    <cap:expires>{today}T23:59:59+{tz_offset}:00</cap:expires>
    <cap:effective>{yesterday}T10:40:36+{tz_offset}:00</cap:effective>
    <cap:onset>{today}T00:00:00+{tz_offset}:00</cap:onset>
    <cap:certainty>Possible</cap:certainty>
    <cap:severity>Moderate</cap:severity>
    <cap:urgency>Immediate</cap:urgency>
    <cap:scope>Public</cap:scope>
    <cap:message_type>Update</cap:message_type>
    <cap:status>Actual</cap:status>
    <cap:identifier>2.49.0.0.826.0.GB_250124180126_cecc0a37.v6.0.W</cap:identifier>
    <link href="https://feeds.meteoalarm.org/api/v1/warnings/feeds-united-kingdom/3ec5a08b-995f-45fc-88eb-d364b1613e41" type="application/cap+xml"/>
    <link href="https://meteoalarm.org?region=UK" hreflang="en" rel="related" title="United Kingdom"/>
    <author>
      <name>meteoalarm.org</name>
      <uri>https://meteoalarm.org</uri>
    </author>
    <published>2025-01-24T18:01:26Z</published>
    <id>https://feeds.meteoalarm.org/api/v1/warnings/feeds-united-kingdom/3ec5a08b-995f-45fc-88eb-d364b1613e41?index_info=0&amp;index_area=0&amp;index_polygon=0</id>
    <title>Yellow Wind Warning issued for United Kingdom - Central, Tayside &amp; Fife | Grampian | Highlands &amp; Eilean Siar | North East England | North West England | Northern Ireland | Orkney &amp; Shetland | Strathclyde | SW Scotland, Lothian Borders | Yorkshire &amp; Humber</title>
    <updated>2025-01-24T18:01:26Z</updated>
  </entry>
<entry>
    <cap:polygon>60.877,-1.4282 59.4674,-3.23 59.249,-3.7573 58.95,-4.4275 58.9103,-4.8395 58.4618,-6.7053 57.7687,-7.8949 56.7768,-7.8333 56.7407,-7.3059 56.7557,-6.7841 56.7768,-6.4545 56.7286,-6.1908 56.6139,-5.9601 56.4534,-5.5872 56.3653,-5.4108 56.2769,-5.1306 56.2586,-5.0153 56.2403,-4.8395 56.2189,-4.3671 56.222,-4.2462 56.2525,-4.0869 56.2678,-3.8892 56.283,-3.8068 56.3835,-3.4113 56.5776,-2.9169 56.6562,-2.774 56.6894,-2.7301 56.8099,-2.3816 56.8079,-2.1936 57.1184,-1.8896 57.4509,-1.593 57.7218,-1.8018 57.7628,-3.3838 57.9906,-3.6035 58.3499,-2.9114 59.2996,-1.4612 60.5222,-0.4834 60.9411,-0.7031 60.877,-1.4282</cap:polygon>
    <link href="https://meteoalarm.org?polygon=05f2a1ec-58ec-4b6e-b05b-21ddac714680,0,0,0" hreflang="en" title="Central, Tayside &amp; Fife | Grampian | Highlands &amp; Eilean Siar | Orkney &amp; Shetland | Strathclyde"/>
    <cap:areaDesc>Central, Tayside &amp; Fife | Grampian | Highlands &amp; Eilean Siar | Orkney &amp; Shetland | Strathclyde</cap:areaDesc>
    <cap:event>Amber wind warning</cap:event>
    <cap:sent>2025-01-23T10:42:05+{tz_offset}:00</cap:sent>
    <cap:expires>{tomorrow}T06:00:00+{tz_offset}:00</cap:expires>
    <cap:effective>{yesterday}T10:42:05+{tz_offset}:00</cap:effective>
    <cap:onset>{today}T13:00:00+{tz_offset}:00</cap:onset>
    <cap:certainty>Likely</cap:certainty>
    <cap:severity>Severe</cap:severity>
    <cap:urgency>Future</cap:urgency>
    <cap:scope>Public</cap:scope>
    <cap:message_type>Alert</cap:message_type>
    <cap:status>Actual</cap:status>
    <cap:identifier>2.49.0.0.826.0.GB_250123104205_72085267.v1.0.W</cap:identifier>
    <link href="https://feeds.meteoalarm.org/api/v1/warnings/feeds-united-kingdom/05f2a1ec-58ec-4b6e-b05b-21ddac714680" type="application/cap+xml"/>
    <link href="https://meteoalarm.org?region=UK" hreflang="en" rel="related" title="United Kingdom"/>
    <author>
      <name>meteoalarm.org</name>
      <uri>https://meteoalarm.org</uri>
    </author>
    <published>2025-01-23T10:42:05Z</published>
    <id>https://feeds.meteoalarm.org/api/v1/warnings/feeds-united-kingdom/05f2a1ec-58ec-4b6e-b05b-21ddac714680?index_info=0&amp;index_area=0&amp;index_polygon=0</id>
    <title>Orange Wind Warning issued for United Kingdom - Central, Tayside &amp; Fife | Grampian | Highlands &amp; Eilean Siar | Orkney &amp; Shetland | Strathclyde</title>
    <updated>2025-01-23T10:42:05Z</updated>
  </entry>
</feed>
"""
    print("Test alert feed")

    alert_feed = AlertFeed(my_predbat, alert_config={})

    result = alert_feed.parse_alert_data(alert_data)
    if not result:
        print("ERROR: Could not parse stored alert data")
        failed = 1
        return failed

    filter = alert_feed.filter_alerts(result, area="North West England")
    if len(filter) != 1:
        print("ERROR: Expecting 1 alert for North West England got {}".format(len(filter)))
        failed = 1
        return failed

    filter = alert_feed.filter_alerts(result, area="South West England")
    if len(filter) != 0:
        print("ERROR: Expecting 0 alert for South West England got {}".format(len(filter)))
        failed = 1
        return failed

    filter = alert_feed.filter_alerts(result, latitude=birmingham[0], longitude=birmingham[1])
    if len(filter) != 0:
        print("ERROR: Expecting 0 alert for Birmingham got {}".format(len(filter)))
        failed = 1
        return failed

    filter = alert_feed.filter_alerts(result, latitude=fife[0], longitude=fife[1])
    if len(filter) != 1:
        print("ERROR: Expecting 1 alert for Fife got {}".format(len(filter)))
        failed = 1
        return failed

    filter = alert_feed.filter_alerts(result, area="Grampian", severity="Moderate|Severe", certainty="Likely")
    if len(filter) != 1:
        print("ERROR: Expecting 1 alert for Grampian got {}".format(len(filter)))
        failed = 1
        return failed

    filter = alert_feed.filter_alerts(result, event="(Amber|Yellow|Orange|Red).*(Wind|Snow|Fog|Thunderstorm|Avalanche|Frost|Heat|Coastal event|Flood|Forestfire|Ice|Low temperature|Storm|Tornado|Tsunami|Volcano|Wildfire)")
    if len(filter) != 2:
        print("ERROR: Expecting 2 alerts for Yellow|Amber but got {}".format(len(filter)))
        failed = 1
        return failed

    alert_active_keep = alert_feed.apply_alerts(result, 1.0, my_predbat.minutes_now, my_predbat.midnight_utc)
    show = []
    for minute in range(0, 48 * 60, 15):
        show.append(alert_active_keep.get(minute, 0))
    expect_show = [
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    ]
    if json.dumps(show) != json.dumps(expect_show):
        print("ERROR: Expecting show should be {} got {}".format(expect_show, show))
        failed = 1

    url = "https://feeds.meteoalarm.org/feeds/meteoalarm-legacy-atom-united-kingdom"
    xml = alert_feed.download_alert_data(url)
    if not xml:
        print("ERROR: Could not download alert data")
        failed = 1
        return failed

    alert_config = {
        "url": url,
        "area": "North West England",
        "event": "Yellow|Amber",
        "keep": 0.5,
    }
    original_download_alert_data = alert_feed.download_alert_data
    alert_feed.alert_config = alert_config
    alert_feed.alert_xml = alert_data
    alerts, alert_active_keep = alert_feed.process_alerts(my_predbat.minutes_now, my_predbat.midnight_utc, testing=True)
    alert_active_keep = alert_active_keep
    show = []
    for minute in range(0, 48 * 60, 15):
        show.append(alert_active_keep.get(minute, 0))

    expect_show = [
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0.5,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
        0,
    ]
    if json.dumps(show) != json.dumps(expect_show):
        print("ERROR: Expecting show should be {} got {}".format(expect_show, show))
        failed = 1

    alert_text = ha.get_state("sensor." + my_predbat.prefix + "_alertfeed_status")
    expect_text = "Yellow wind warning until " + today + " 23:59:59+{}:00".format(tz_offset)
    if alert_text != expect_text:
        print("ERROR: Expecting alert text to be '{}' got '{}'".format(expect_text, alert_text))
        failed = 1

    return failed


def test_saving_session(my_predbat):
    """
    Test the octopus saving session
    """
    print("Test saving session")
    ha = my_predbat.ha_interface
    failed = False
    date_last_year = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    date_yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    date_before_yesterday = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    date_today = datetime.now().strftime("%Y-%m-%d")
    tz_offset = int(my_predbat.midnight_utc.tzinfo.utcoffset(my_predbat.midnight_utc).total_seconds() / 3600)
    tz_offset = f"{tz_offset:02d}"
    session_binary = f"""

state: off
current_joined_event_start: '{date_today}T16:30:00+{tz_offset}:00'
current_joined_event_end: '{date_today}T17:30:00+{tz_offset}:00'
current_joined_event_duration_in_minutes: 60
next_joined_event_start: null
next_joined_event_end: null
next_joined_event_duration_in_minutes: null
icon: mdi:leaf
friendly_name: Octoplus Saving Session (A-4DD6C5EE)
""".format(
        date_last_year=date_last_year, date_yesterday=date_yesterday, date_today=date_today, date_before_yesterday=date_before_yesterday, tz_offset=tz_offset
    )

    session_sensor = f"""
state: '2025-01-23T12:10:11.108+{tz_offset}:00'
event_types: octopus_energy_all_octoplus_saving_sessions
event_type: octopus_energy_all_octoplus_saving_sessions
account_id: A-4DD6C5EE
available_events:
    - id: 1336
      start: '{date_today}T18:30:00+{tz_offset}:00'
      end: '{date_today}T19:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: 500
      code: 987654
joined_events:
    - id: 1327
      start: '{date_last_year}T17:00:00+{tz_offset}:00'
      end: '{date_last_year}T18:00:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: 936
      octopoints_per_kwh: 576
    - id: 1334
      start: '{date_yesterday}T17:30:00+{tz_offset}:00'
      end: '{date_yesterday}T18:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: 192
    - id: 1335
      start: '{date_today}T16:30:00+{tz_offset}:00'
      end: '{date_today}T17:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: 448
    - id: 1336
      start: '{date_before_yesterday}T23:30:00+{tz_offset}:00'
      end: '{date_yesterday}T10:30:00+{tz_offset}:00'
      duration_in_minutes: 60
      rewarded_octopoints: null
      octopoints_per_kwh: 448
friendly_name: Octoplus Saving Session Events (A-12345678)
""".format(
        date_last_year=date_last_year, date_yesterday=date_yesterday, date_today=date_today, tz_offset=tz_offset
    )
    ha.dummy_items["binary_sensor.octopus_energy_a_12345678_octoplus_saving_sessions"] = yaml.safe_load(session_binary)
    ha.dummy_items["event.octopus_energy_a_12345678_octoplus_saving_session_events"] = yaml.safe_load(session_sensor)
    ha.dummy_items["sensor.octopus_free_session"] = {}
    my_predbat.args["octopus_saving_session"] = "binary_sensor.octopus_energy_a_12345678_octoplus_saving_sessions"
    my_predbat.args["octopus_free_session"] = "sensor.octopus_free_session"
    if "octopus_free_url" in my_predbat.args:
        del my_predbat.args["octopus_free_url"]
    my_predbat.args["octopus_saving_session_octopoints_per_penny"] = 10

    ha.service_store_enable = True
    octopus_free_slots, octopus_saving_slots = my_predbat.fetch_octopus_sessions()
    service_result = ha.get_service_store()
    ha.service_store_enable = False

    expected_saving = [
        {"start": "{}T17:30:00+{}:00".format(date_yesterday, tz_offset), "end": "{}T18:30:00+{}:00".format(date_yesterday, tz_offset), "rate": 19.2, "state": False},
        {"start": "{}T16:30:00+{}:00".format(date_today, tz_offset), "end": "{}T17:30:00+{}:00".format(date_today, tz_offset), "rate": 44.8, "state": False},
        {"start": "{}T23:30:00+{}:00".format(date_before_yesterday, tz_offset), "end": "{}T10:30:00+{}:00".format(date_yesterday, tz_offset), "rate": 44.8, "state": False},
    ]

    # Example format Sat 25/01
    date_today_service = datetime.now().strftime("%a %d/%m")
    expected_service = [
        ["octopus_energy/join_octoplus_saving_session_event", {"event_code": 987654, "entity_id": "event.octopus_energy_a_12345678_octoplus_saving_session_events"}],
        ["notify/notify", {"message": "Predbat: Joined Octopus saving event {} 18:30-19:30, 50.0 p/kWh".format(date_today_service)}],
    ]

    if json.dumps(octopus_saving_slots) != json.dumps(expected_saving):
        print("ERROR: Expecting saving slots should be {} got {}".format(expected_saving, octopus_saving_slots))
        failed = 1
    if json.dumps(service_result) != json.dumps(expected_service):
        print("ERROR: Expecting service store should be {} got {}".format(expected_service, service_result))
        failed = 1
    if octopus_free_slots:
        print("ERROR: Expecting no free slots")
        failed = 1

    rate_import_replicated = {}
    my_predbat.rate_import = {n: 0 for n in range(-24 * 60, 48 * 60)}
    my_predbat.load_saving_slot(expected_saving, export=False, rate_replicate=rate_import_replicated)
    price_ranges = [[(17.5 - 24) * 60, (18.5 - 24) * 60, 19.2], [(16.5) * 60, (17.5) * 60, 44.8], [-24 * 60, (10.5 - 24) * 60, 44.8]]
    for minute in range(-24 * 60, 48 * 60):
        rate = my_predbat.rate_import[minute]
        in_range = False
        for price_range in price_ranges:
            if minute >= price_range[0] and minute < price_range[1]:
                if rate != price_range[2]:
                    print("ERROR: Load Octopus Saving - minute {} Expecting rate to be {} got {}".format(minute, price_range[2], rate))
                    failed = 1
                    break
                in_range = True
        if not in_range:
            if rate != 0:
                print("ERROR: Load Octopus Saving - minute {} Expecting rate to be 0 got {}".format(minute, rate))
                failed = 1
                break

    return failed


def run_test_octopus_api(my_predbat, octopus_api, octopus_account):
    """
    Run the Octopus API tests
    """
    print("Test Octopus API")
    failed = False

    octopus_api = OctopusAPI(octopus_api, octopus_account, my_predbat)
    my_predbat.create_task(octopus_api.start())
    octopus_api.wait_api_started()

    planned_dispatches = octopus_api.get_intelligent_planned_dispatches()
    completed_dispatches = octopus_api.get_intelligent_completed_dispatches()
    vehicle = octopus_api.get_intelligent_vehicle()
    available_events, joined_events = octopus_api.get_saving_session_data()
    print("Planned dispatches: {}".format(planned_dispatches))
    print("Completed dispatches: {}".format(completed_dispatches))
    print("Vehicle: {}".format(vehicle))
    print("Saving session available {}".format(available_events))
    print("Saving session joined {}".format(joined_events))
    octopus_api.join_saving_session_event("EVENT_3_210125")
    time.sleep(10)
    octopus_api.stop()
    time.sleep(1)

    failed = 1
    return failed


def run_test_manual_api(my_predbat):
    failed = 0
    print("Test manual API")

    # Reset
    my_predbat.api_select("manual_api", "")
    original_limit = my_predbat.args["inverter_limit"]

    my_predbat.args["inverter_limit"] = [3600, 3500]
    my_predbat.args["inverter_limit_charge"] = [3600, 3600]
    limit = my_predbat.get_arg("inverter_limit", 0, index=0)
    if limit != 3600:
        print("ERROR: T1 Expecting inverter limit 0 to be 3600 got {}".format(limit))
        failed = 1
    limit = my_predbat.get_arg("inverter_limit", 0, index=1)
    if limit != 3500:
        print("ERROR: T2 Expecting inverter limit 0 to be 3500 got {}".format(limit))
        failed = 1
    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [3600, 3500]
    if limits != expected:
        print("ERROR: T3 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.api_select("manual_api", "inverter_limit=1000")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")

    limit = my_predbat.get_arg("inverter_limit", 0, index=0)
    expected = 1000
    if limit != expected:
        print("ERROR: T4 Expecting inverter limit 0 to be {} got {}".format(expected, limit))
        failed = 1

    limit = my_predbat.get_arg("inverter_limit", 0, index=1)
    expected = 3500
    if limit != expected:
        print("ERROR: T5 Expecting inverter limit 0 to be {} got {}".format(expected, limit))
        failed = 1

    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [1000, 3500]
    if limits != expected:
        print("ERROR: T6 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.api_select("manual_api", "[inverter_limit=1000]")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [3600, 3500]
    if limits != expected:
        print("ERROR: T7 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.api_select("manual_api", "inverter_limit(1)=1000")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [3600, 1000]
    if limits != expected:
        print("ERROR: T8 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.api_select("manual_api", "inverter_limit(0)=900")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [900, 1000]
    if limits != expected:
        print("ERROR: T8 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.api_select("manual_api", "inverter_limit(0)=800")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [800, 1000]
    if limits != expected:
        print("ERROR: T9 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.api_select("manual_api", "off")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    limits = my_predbat.get_arg("inverter_limit", [])
    expected = [3600, 3500]
    if limits != expected:
        print("ERROR: T3 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1

    my_predbat.args["inverter_limit"] = original_limit
    my_predbat.args["rates_export_override"] = []

    export_override = my_predbat.get_arg("rates_export_override", [])
    if export_override != []:
        print("ERROR: T10 Expecting rate export override to be {} got {}".format([], export_override))
        failed = 1

    my_predbat.api_select("manual_api", "rates_export_override?start=17:00:00&end=19:00:00&rate=0")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    export_override = my_predbat.get_arg("rates_export_override", [])
    expected = [{"start": "17:00:00", "end": "19:00:00", "rate": "0"}]
    if export_override != expected:
        print("ERROR: T11 Expecting rate export override to be {} got {}".format(expected, export_override))
        failed = 1

    my_predbat.api_select("manual_api", "rates_export_override(1)?start=12:00:00&end=13:00:00&rate=2")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    export_override = my_predbat.get_arg("rates_export_override", [])
    expected = [{"start": "17:00:00", "end": "19:00:00", "rate": "0"}, {"start": "12:00:00", "end": "13:00:00", "rate": "2"}]
    if export_override != expected:
        print("ERROR: T12 Expecting rate export override to be {} got {}".format(expected, export_override))
        failed = 1

    my_predbat.api_select("manual_api", "off")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    export_override = my_predbat.get_arg("rates_export_override", [])
    expected = []
    if export_override != expected:
        print("ERROR: T13 Expecting rate export override to be {} got {}".format(expected, export_override))
        failed = 1

    my_predbat.api_select("manual_api", "inverter_limit_charge(0)=800")
    my_predbat.api_select("manual_api", "inverter_limit_charge(1)=400")
    my_predbat.manual_api = my_predbat.api_select_update("manual_api")
    limits = my_predbat.get_arg("inverter_limit_charge", [])
    expected = [800, 400]
    if limits != expected:
        print("ERROR: T14 Expecting inverter limit to be {} got {}".format(expected, limits))
        failed = 1
    limit0 = my_predbat.get_arg("inverter_limit_charge", index=0, default=0)
    if limit0 != 800:
        print("ERROR: T15 Expecting inverter limit 0 to be {} got {}".format(800, limit0))
        failed = 1
    limit1 = my_predbat.get_arg("inverter_limit_charge", index=1, default=0)
    if limit1 != 400:
        print("ERROR: T16 Expecting inverter limit 1 to be {} got {}".format(400, limit1))
        failed = 1

    del my_predbat.args["inverter_limit_charge"]

    return failed


def test_minute_data(my_predbat):
    """
    Test the minute_data function from the Fetch class
    """
    failed = False
    print("**** Testing minute_data function ****")

    # Create test datetime objects with timezone awareness
    import pytz

    utc = pytz.UTC
    now = datetime(2024, 10, 4, 12, 5, 0, tzinfo=utc)  # Fixed time for testing

    # Test 1: Basic functionality with simple history data
    print("Test 1: Basic functionality")
    history = [
        {"state": "0.0", "last_updated": "2024-10-04T10:30:00+00:00"},
        {"state": "10.0", "last_updated": "2024-10-04T11:00:00+00:00"},
        {"state": "20.0", "last_updated": "2024-10-04T11:30:00+00:00"},
        {"state": "30.0", "last_updated": "2024-10-04T12:00:00+00:00"},
    ]

    result, ignore_io = minute_data(history=history, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, smoothing=False)

    points = [0, 5, 34, 35, 65]
    result_points = [result.get(p) for p in points]
    expected_points = [30.0, 20.0, 20.0, 10.0, 0.0]

    # Check that we have data and it's reasonable
    if len(result) != 24 * 60:
        print("ERROR: Basic test failed - no data returned")
        # Print result sorted by key for easier reading
        failed = True
    elif result_points != expected_points:
        print("ERROR: Basic test failed - values incorrect, data points were {} expected {}".format(result_points, expected_points))
        for key in sorted(result.keys()):
            print("  {}: {}".format(key, result[key]))
        failed = True

    # Test 2: Empty history
    print("Test 2: Empty history")
    empty_result, ignore_io = minute_data(history=[], days=1, now=now, state_key="state", last_updated_key="last_updated")

    if empty_result != {}:
        print("ERROR: Empty history test failed - should return empty dict")
        failed = True

    # Test 3: Scale parameter
    print("Test 3: Scale parameter")
    scaled_result, ignore_io = minute_data(history=history, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, scale=2.0)

    if 0 not in scaled_result:
        print("ERROR: Scale test failed - no data at minute 0")
        failed = True
    elif scaled_result[0] != result[0] * 2.0:
        print("ERROR: Scale test failed - scaling incorrect, got {} expected {}".format(scaled_result[0], result[0] * 2.0))
        failed = True

    # Test 4: Smoothing enabled vs disabled comparison
    print("Test 4: Smoothing")
    result, ignore_io = minute_data(
        history=history,
        days=1,
        now=now,
        state_key="state",
        last_updated_key="last_updated",
        backwards=True,
        smoothing=True,
    )

    points = [0, 5, 34, 35, 65]
    expected_points = [30.0, 30.0, dp4(20.0 + 10 / 30), 20.0, 10.0]
    result_points = [result.get(p) for p in points]
    if len(result) != 24 * 60:
        print("ERROR: Smoothing test failed - no data returned")
        failed = True
    elif result_points != expected_points:
        print("ERROR: Smoothing test - unsmoothed values incorrect, data points were {} expected {}".format(result_points, expected_points))
        for key in sorted(result.keys()):
            print("  {}: {}".format(key, result[key]))
        failed = True

    # Test 4.1: Smoothing clean increment
    print("Test 4.1: Smoothing clean increment")
    result, ignore_io = minute_data(
        history=history,
        days=1,
        now=now,
        state_key="state",
        last_updated_key="last_updated",
        backwards=True,
        smoothing=True,
        clean_increment=True,
        max_increment=0,
        interpolate=True,
    )
    points = [0, 5, 34, 35, 65]
    expected_points = [dp4(30.0 + 5 * (10 / 30)), 30.0, dp4(20.0 + 10 / 30), 20.0, 10.0]
    result_points = [result.get(p) for p in points]
    if len(result) != 24 * 60:
        print("ERROR: Smoothing test failed - no data returned")
        failed = True
    elif result_points != expected_points:
        print("ERROR: Smoothing test - unsmoothed values incorrect, data points were {} expected {}".format(result_points, expected_points))
        for key in sorted(result.keys()):
            print("  {}: {}".format(key, result[key]))
        failed = True

    # Test 5: Attributes mode
    print("Test 5: Attributes mode")
    history_with_attrs = [
        {"attributes": {"power": "15.0"}, "last_updated": "2024-10-04T11:00:00+00:00"},
        {"attributes": {"power": "25.0"}, "last_updated": "2024-10-04T11:30:00+00:00"},
    ]

    attrs_result, ignore_io = minute_data(history=history_with_attrs, days=1, now=now, state_key="power", last_updated_key="last_updated", backwards=True, attributes=True)

    if len(attrs_result) == 0:
        print("ERROR: Attributes test failed - no data returned")
        failed = True

    # Test 6: Unit conversion
    print("Test 6: Unit conversion")
    history_with_units = [
        {"state": "1000.0", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "W"}},
    ]

    converted_result, ignore_io = minute_data(history=history_with_units, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, required_unit="kWh")

    if len(converted_result) == 0:
        print("ERROR: Unit conversion test failed - no data returned")
        failed = True

    # Check if the conversion is correct
    expected_conversion = 1000.0 / 1000.0  # Convert from Wh to kWh
    if converted_result[0] != expected_conversion:
        print("ERROR: Unit conversion test failed - expected {} got {}".format(expected_conversion, converted_result[0]))
        failed = True

    # Test 7: Invalid/unavailable data filtering
    print("Test 7: Invalid data filtering")
    history_with_invalid = [
        {"state": "10.0", "last_updated": "2024-10-04T11:00:00+00:00"},
        {"state": "unavailable", "last_updated": "2024-10-04T11:15:00+00:00"},
        {"state": "unknown", "last_updated": "2024-10-04T11:30:00+00:00"},
        {"state": "20.0", "last_updated": "2024-10-04T11:45:00+00:00"},
    ]

    filtered_result, ignore_io = minute_data(history=history_with_invalid, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True)

    # Function should filter out invalid data
    if len(filtered_result) == 0:
        print("ERROR: Invalid data filtering test failed - no data returned")
        failed = True

    # Test 8: Accumulate parameter
    print("Test 8: Accumulate parameter")
    accumulate_data = {}
    for i in range(24 * 60):
        accumulate_data[i] = 1  # Accumulate 1 unit per minute
    result, ignore_io = minute_data(history=history, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, smoothing=False)
    accumulated_result, ignore_io = minute_data(history=history, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, accumulate=accumulate_data)

    if 0 not in accumulated_result:
        print("ERROR: Accumulate test failed - no data at minute 0")
        failed = True
    else:
        # Check accumulate data is result + 1
        if accumulated_result[0] != result[0] + 1:
            print("ERROR: Accumulate test failed - expected {} got {}".format(result[0] + 1, accumulated_result[0]))
            failed = True

    # Test 9: Forward time direction
    print("Test 9: Forward time direction")
    forward_result, ignore_io = minute_data(history=history, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=False)

    # Test 10: Missing keys handling
    print("Test 10: Missing keys handling")
    history_missing_keys = [
        {"state": "10.0"},  # Missing last_updated
        {"last_updated": "2024-10-04T11:30:00+00:00"},  # Missing state
    ]

    missing_keys_result, ignore_io = minute_data(history=history_missing_keys, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True)

    # Test 11: Different state key
    print("Test 11: Different state key")
    history_different_key = [
        {"value": "50.0", "last_updated": "2024-10-04T11:00:00+00:00"},
        {"value": "60.0", "last_updated": "2024-10-04T11:30:00+00:00"},
    ]

    different_key_result, ignore_io = minute_data(history=history_different_key, days=1, now=now, state_key="value", last_updated_key="last_updated", backwards=True)

    if len(different_key_result) == 0:
        print("ERROR: Different state key test failed - no data returned")
        failed = True

    # Test 12: clean_increment=True with glitch filter (lines 319-337)
    print("Test 12: clean_increment with glitch filter")
    history_glitch = [
        {"state": "10.0", "last_updated": "2024-10-04T10:00:00+00:00"},
        {"state": "100.0", "last_updated": "2024-10-04T10:30:00+00:00"},  # Spike/glitch
        {"state": "15.0", "last_updated": "2024-10-04T11:00:00+00:00"},  # Back to normal
        {"state": "20.0", "last_updated": "2024-10-04T11:30:00+00:00"},
    ]
    glitch_result, ignore_io = minute_data(history=history_glitch, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, smoothing=True, clean_increment=True, max_increment=50)
    if len(glitch_result) == 0:
        print("ERROR: clean_increment glitch filter test failed - no data returned")
        failed = True
    else:
        # The glitch (100.0 spike) should be filtered out by clean_increment
        # Values should not include the 100.0 spike, results should be smoothed between valid values
        values = list(glitch_result.values())
        if 100.0 in values:
            print(f"ERROR: clean_increment glitch filter test failed - spike value 100.0 should be filtered, got {values[:10]}")
            failed = True
        # Check that we have reasonable values (should be between 10 and 20 range, not 100)
        max_val = max(values)
        if max_val > 50:
            print(f"ERROR: clean_increment glitch filter test failed - max value {max_val} exceeds max_increment threshold")
            failed = True

    # Test 13: W to kWh unit conversion with integrate=True (lines 378-379)
    print("Test 13: W to kWh unit conversion")
    history_watts = [
        {"state": "1000.0", "last_updated": "2024-10-04T10:00:00+00:00", "attributes": {"unit_of_measurement": "W"}},
        {"state": "2000.0", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "W"}},
    ]
    watts_result, ignore_io = minute_data(history=history_watts, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, required_unit="kWh")
    if len(watts_result) == 0:
        print("ERROR: W to kWh conversion test failed - no data returned")
        failed = True
    else:
        # W to kWh conversion divides by 1000 and integrates over time
        # 2000W = 2kW integrated over 60 minutes = 2.0 kWh at minute 0
        val_at_0 = watts_result.get(0)
        if val_at_0 is None:
            print(f"ERROR: W to kWh conversion - no value at minute 0, keys: {list(watts_result.keys())[:5]}")
            failed = True
        elif abs(val_at_0 - 2.0) > 0.1:
            print(f"ERROR: W to kWh conversion - expected 2.0 at minute 0, got {val_at_0}")
            failed = True

    # Test 14: kW to kWh unit conversion
    print("Test 14: kW to kWh unit conversion")
    history_kw = [
        {"state": "1.5", "last_updated": "2024-10-04T10:00:00+00:00", "attributes": {"unit_of_measurement": "kW"}},
        {"state": "2.0", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "kW"}},
    ]
    kw_result, ignore_io = minute_data(history=history_kw, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, required_unit="kWh", to_key=None)
    if len(kw_result) == 0:
        print("ERROR: kW to kWh conversion test failed - no data returned")
        failed = True
    else:
        # kW to kWh with integration over 60 minutes
        # 2.0kW integrated over 60 minutes = 2.0 kWh at minute 0
        val_at_0 = kw_result.get(0)
        if val_at_0 is None:
            print(f"ERROR: kW to kWh conversion - no value at minute 0, keys: {list(kw_result.keys())[:5]}")
            failed = True
        elif abs(val_at_0 - 2.0) > 0.1:
            print(f"ERROR: kW to kWh conversion - expected 2.0 at minute 0, got {val_at_0}")
            failed = True

    # Test 15: spreading parameter (lines 492-494)
    print("Test 15: spreading parameter")
    history_spreading = [
        {"state": "10.0", "last_updated": "2024-10-04T11:00:00+00:00"},
    ]
    spread_result, ignore_io = minute_data(history=history_spreading, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=False, spreading=30)  # Spread over 30 minutes
    # Check that data is spread across multiple minutes
    count_with_value = sum(1 for v in spread_result.values() if v == 10.0)
    if count_with_value < 10:
        print(f"ERROR: spreading test failed - expected multiple minutes with value 10.0, got {count_with_value}")
        failed = True

    # Test 16: divide_by parameter
    print("Test 16: divide_by parameter")
    history_divide = [
        {"state": "100.0", "last_updated": "2024-10-04T11:00:00+00:00"},
    ]
    divide_result, ignore_io = minute_data(history=history_divide, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, divide_by=2)
    if divide_result.get(0) != 50.0:
        print(f"ERROR: divide_by test failed - expected 50.0, got {divide_result.get(0)}")
        failed = True

    # Test 17: smoothing forward mode (backwards=False, smoothing=True) - lines 418-419
    print("Test 17: smoothing forward mode")
    history_forward = [
        {"state": "10.0", "last_updated": "2024-10-04T12:10:00+00:00"},  # 5 minutes in future
        {"state": "20.0", "last_updated": "2024-10-04T12:20:00+00:00"},  # 15 minutes in future
    ]
    forward_smooth_result, ignore_io = minute_data(history=history_forward, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=False, smoothing=True)
    if len(forward_smooth_result) == 0:
        print("ERROR: smoothing forward mode test failed - no data returned")
        failed = True

    # Test 18: W to kW unit conversion (lines 384-385)
    print("Test 18: W to kW unit conversion")
    history_w_to_kw = [
        {"state": "5000.0", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "W"}},
    ]
    w_to_kw_result, ignore_io = minute_data(history=history_w_to_kw, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, required_unit="kW")
    if len(w_to_kw_result) == 0:
        print("ERROR: W to kW conversion test failed - no data returned")
        failed = True
    elif w_to_kw_result.get(0) != 5.0:
        print(f"ERROR: W to kW conversion test failed - expected 5.0, got {w_to_kw_result.get(0)}")
        failed = True

    # Test 19: kW to W unit conversion (line 385 reverse)
    print("Test 19: kW to W unit conversion")
    history_kw_to_w = [
        {"state": "2.5", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "kW"}},
    ]
    kw_to_w_result, ignore_io = minute_data(history=history_kw_to_w, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, required_unit="W")
    if len(kw_to_w_result) == 0:
        print("ERROR: kW to W conversion test failed - no data returned")
        failed = True
    elif kw_to_w_result.get(0) != 2500.0:
        print(f"ERROR: kW to W conversion test failed - expected 2500.0, got {kw_to_w_result.get(0)}")
        failed = True

    # Test 20: Unsupported unit conversion is skipped (line 388)
    print("Test 20: Unsupported unit conversion is skipped")
    history_bad_unit = [
        {"state": "100.0", "last_updated": "2024-10-04T10:00:00+00:00", "attributes": {"unit_of_measurement": "gallons"}},
        {"state": "50.0", "last_updated": "2024-10-04T11:00:00+00:00", "attributes": {"unit_of_measurement": "kWh"}},
    ]
    bad_unit_result, ignore_io = minute_data(history=history_bad_unit, days=1, now=now, state_key="state", last_updated_key="last_updated", backwards=True, required_unit="kWh")
    # Should only have the kWh entry, gallons should be skipped
    if len(bad_unit_result) == 0:
        print("ERROR: Unsupported unit test failed - no data returned")
        failed = True

    print("**** minute_data tests completed ****")
    return failed


def test_get_override_time_from_string(my_predbat):
    """
    Test the get_override_time_from_string function from utils
    """
    failed = False
    print("**** Testing get_override_time_from_string function ****")

    # Create test datetime objects with timezone awareness
    import pytz

    utc = pytz.UTC
    now = datetime(2024, 11, 26, 14, 23, 0, tzinfo=utc)  # Tuesday, 26 Nov 2024, 14:23

    # Test 1: Day with time format - future time today
    print("Test 1: Day with time format - future time same day")
    result = get_override_time_from_string(now, "Tue 15:30", 30)
    expected = datetime(2024, 11, 26, 15, 30, 0, tzinfo=utc)
    if result != expected:
        print("ERROR: Test 1 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 2: Day with time format - past day this week (should go to next week)
    print("Test 2: Day with time format - past day this week")
    result = get_override_time_from_string(now, "Mon 10:00", 30)
    expected = datetime(2024, 12, 2, 10, 0, 0, tzinfo=utc)  # Next Monday
    if result != expected:
        print("ERROR: Test 2 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 3: Day with time format - future day this week
    print("Test 3: Day with time format - future day this week")
    result = get_override_time_from_string(now, "Wed 09:15", 30)
    expected = datetime(2024, 11, 27, 9, 0, 0, tzinfo=utc)  # Next day (rounded down to 9:00)
    if result != expected:
        print("ERROR: Test 3 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 4: Time-only format - future time today
    print("Test 4: Time-only format - future time today")
    result = get_override_time_from_string(now, "16:45", 30)
    expected = datetime(2024, 11, 26, 16, 30, 0, tzinfo=utc)  # Today, rounded down to 16:30
    if result != expected:
        print("ERROR: Test 4 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 5: Time-only format - past time today (should go to tomorrow)
    print("Test 5: Time-only format - past time today (goes to tomorrow)")
    result = get_override_time_from_string(now, "10:00", 30)
    expected = datetime(2024, 11, 27, 10, 0, 0, tzinfo=utc)  # Tomorrow
    if result != expected:
        print("ERROR: Test 5 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 6: Time-only format - exact current time (should go to tomorrow)
    print("Test 6: Time-only format - exact current time")
    result = get_override_time_from_string(now, "14:23", 30)
    expected = datetime(2024, 11, 27, 14, 0, 0, tzinfo=utc)  # Tomorrow, rounded down to 14:00
    if result != expected:
        print("ERROR: Test 6 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 7: Rounding to plan_interval_minutes (15 min intervals)
    print("Test 7: Rounding with 15-minute intervals")
    result = get_override_time_from_string(now, "Fri 13:47", 15)
    expected = datetime(2024, 11, 29, 13, 45, 0, tzinfo=utc)  # Friday, rounded to 13:45
    if result != expected:
        print("ERROR: Test 7 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 8: Rounding to plan_interval_minutes (5 min intervals)
    print("Test 8: Rounding with 5-minute intervals")
    result = get_override_time_from_string(now, "20:33", 5)
    expected = datetime(2024, 11, 26, 20, 30, 0, tzinfo=utc)  # Today (future), rounded to 20:30
    if result != expected:
        print("ERROR: Test 8 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 9: Invalid format - should return None
    print("Test 9: Invalid format")
    result = get_override_time_from_string(now, "invalid", 30)
    if result is not None:
        print("ERROR: Test 9 failed - expected None for invalid format, got {}".format(result))
        failed = True

    # Test 10: Sunday (week boundary test)
    print("Test 10: Sunday - day with time format")
    result = get_override_time_from_string(now, "Sun 12:00", 30)
    expected = datetime(2024, 12, 1, 12, 0, 0, tzinfo=utc)  # Next Sunday
    if result != expected:
        print("ERROR: Test 10 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 11: Edge case - time just after midnight
    print("Test 11: Time-only format - just after midnight")
    now_late = datetime(2024, 11, 26, 23, 50, 0, tzinfo=utc)
    result = get_override_time_from_string(now_late, "00:15", 30)
    expected = datetime(2024, 11, 27, 0, 0, 0, tzinfo=utc)  # Tomorrow at midnight
    if result != expected:
        print("ERROR: Test 11 failed - expected {} got {}".format(expected, result))
        failed = True

    # Test 12: Edge case - time-only exactly at midnight (past, so tomorrow)
    print("Test 12: Time-only format - midnight when it's currently late")
    result = get_override_time_from_string(now_late, "00:00", 30)
    expected = datetime(2024, 11, 27, 0, 0, 0, tzinfo=utc)  # Tomorrow at midnight
    if result != expected:
        print("ERROR: Test 12 failed - expected {} got {}".format(expected, result))
        failed = True

    print("**** get_override_time_from_string tests completed ****")
    return failed


def run_test_units(my_predbat):
    """
    Run the unit tests
    """
    print("Test units")
    failed = False
    ha = my_predbat.ha_interface

    ha.dummy_items["fred"] = {
        "state": 2,
        "unit_of_measurement": "kWh",
    }
    ha.dummy_items["joe"] = {
        "state": 2000,
        "unit_of_measurement": "W",
    }
    print("Test units 1")
    value = my_predbat.get_state_wrapper("fred")
    if float(value) != 2:
        print("ERROR: Expecting fred to be 2 got {}".format(value))
        failed = True
    print("Test units 2")
    value = my_predbat.get_state_wrapper("fred", required_unit="kWh")
    if float(value) != 2:
        print("ERROR: Expecting fred to be 2 got {}".format(value))
        failed = True
    print("Test units 3")
    value = my_predbat.get_state_wrapper("fred", required_unit="Wh")
    if float(value) != 2000:
        print("ERROR: Expecting fred to be 2000 got {}".format(value))
        failed = True
    print("Test units 4")
    value = my_predbat.get_state_wrapper("joe")
    if float(value) != 2000:
        print("ERROR: Expecting joe to be 2000 got {}".format(value))
        failed = True
    print("Test units 5")
    value = my_predbat.get_state_wrapper("joe", required_unit="W")
    if float(value) != 2000:
        print("ERROR: Expecting joe to be 2000 got {}".format(value))
        failed = True
    print("Test units 6")
    value = my_predbat.get_state_wrapper("joe", required_unit="kW")
    if float(value) != 2:
        print("ERROR: Expecting joe to be 2 got {}".format(value))
        failed = True
    print("Test units 7")
    my_predbat.set_state_wrapper("fred", 3, required_unit="kWh", attributes={"unit_of_measurement": "kWh"})
    value = my_predbat.get_state_wrapper("fred")
    if float(value) != 3:
        print("ERROR: Expecting fred to be 3 got {}".format(value))
        failed = True
    print("Test units 8")
    my_predbat.set_state_wrapper("fred", 4000, required_unit="Wh", attributes={"unit_of_measurement": "kWh"})
    value = my_predbat.get_state_wrapper("fred")
    if float(value) != 4:
        print("ERROR: Expecting fred to be 4 got {}".format(value))
        failed = True
    print("Test units 9")
    my_predbat.set_state_wrapper("joe", 3, required_unit="kW", attributes={"unit_of_measurement": "W"})
    value = my_predbat.get_state_wrapper("joe")
    if float(value) != 3000:
        print("ERROR: Expecting joe to be 3000 got {}".format(value))
        failed = True
    print("Test units 10")
    my_predbat.set_state_wrapper("joe", 4000, required_unit="W", attributes={"unit_of_measurement": "W"})
    value = my_predbat.get_state_wrapper("joe")
    if float(value) != 4000:
        print("ERROR: Expecting joe to be 4000 got {}".format(value))
        failed = True
    value = my_predbat.get_state_wrapper("joe", required_unit="kW")
    if float(value) != 4:
        print("ERROR: Expecting joe to be 4 got {}".format(value))
        failed = True

    print("Test units 11")
    ha.dummy_items["pete"] = {
        "state": 2000,
        "unit_of_measurement": "mA",
    }
    my_predbat.set_state_wrapper("pete", 5, required_unit="A", attributes={"unit_of_measurement": "mA"})
    value = my_predbat.get_state_wrapper("pete", required_unit="A")
    if float(value) != 5:
        print("ERROR: Expecting pete to be 5 got {}".format(value))
        failed = True
    value = my_predbat.get_state_wrapper("pete", required_unit="mA")
    if float(value) != 5000:
        print("ERROR: Expecting pete to be 5000 got {}".format(value))
        failed = True

    return failed


def add_incrementing_sensor_total(data):
    max_entry = max(data.keys()) if data else 0
    total = 0
    for minute in range(0, max_entry, PREDICT_STEP):
        increment = max(data.get(minute, 0) - data.get(minute + PREDICT_STEP, 0), 0)
        total += increment
    return total


def test_previous_days_modal_filter(my_predbat):
    """
    Test the previous_days_modal_filter function
    """
    print("**** Running previous_days_modal_filter tests ****")
    failed = False

    # Set up test environment
    my_predbat.load_minutes_age = 7  # 7 days of data
    my_predbat.days_previous = [1, 2]  # Test with 3 days
    my_predbat.days_previous_weight = [1.0, 1.0]  # Equal weighting
    number_of_days = 2
    my_predbat.load_filter_modal = True  # Enable modal filtering
    my_predbat.car_charging_hold = False
    my_predbat.car_charging_energy = None
    my_predbat.iboost_energy_subtract = False
    my_predbat.iboost_energy_today = None
    my_predbat.base_load = 0.0

    # Mock the get_arg method
    original_get_arg = my_predbat.get_arg

    def mock_get_arg(key, default=None):
        if key == "load_filter_threshold":
            return 30
        return original_get_arg(key, default)

    my_predbat.get_arg = mock_get_arg

    # Test 1: Empty data set - should be filled with gap filling logic
    print("Test 1: Empty data set")
    test_data = {}

    # Call the function
    my_predbat.previous_days_modal_filter(test_data)
    data_length = max(test_data.keys()) if test_data else 0
    data_length_days = data_length / (24 * 60)
    print("Data length after processing: {} minutes ({} days)".format(data_length, data_length_days))

    # After gap filling, data should have been populated for the empty gaps
    # Check that data has been filled for at least some minutes
    total_filled_data = dp2(add_incrementing_sensor_total(test_data))
    expected_total_per_day = 24.0  # 24 kWh per day as default when no data

    print("Total filled data: {} kWh".format(dp2(total_filled_data)))

    # With 2 days and complete gaps, should use 24kWh default for each day
    if total_filled_data != expected_total_per_day * number_of_days:
        print("ERROR: Expected gap filling to add approximately {} kWh, got {} kWh".format(expected_total_per_day * number_of_days, total_filled_data))
        for minute in range(0, data_length, 30):
            print("  Minute {}: {}".format(minute, test_data.get(minute, 0)))
        failed = True

    # Test 2: Data with gaps - should be filled to create 1 kWh per hour pattern
    print("Test 2: Data with some gaps")
    test_data = {}

    # Create partial data for 1 day (1 kWh per hour = incrementing total)
    # PREDICT_STEP is 5 minutes, so 12 steps per hour
    # Each step should increment by 1/12 kWh to get 1 kWh per hour
    step_increment = 1.0 / 60
    running_total = 0

    # Fill first half of day with proper incremental data
    for minute in range(0, 12 * 60):  # 12 hours worth
        running_total += step_increment
        test_data[24 * 60 - minute - 1] = dp4(running_total)  # Backwards indexing as used in function
    for minute in range(12 * 60, 24 * 60):  # remainder hours worth
        test_data[24 * 60 - minute - 1] = dp4(running_total)  # Backwards indexing as used in function

    # Leave second half empty to test gap filling

    # Set up days_previous for this test
    my_predbat.days_previous = [1]  # Only test with 1 day
    my_predbat.days_previous_weight = [1.0]

    initial_data_sum = dp2(add_incrementing_sensor_total(test_data))
    print("Initial partial data sum: {} kWh".format(dp2(initial_data_sum)))

    # Call the function
    my_predbat.previous_days_modal_filter(test_data)

    final_data_sum = dp2(add_incrementing_sensor_total(test_data))
    print("Final data sum after gap filling: {} kWh".format(dp2(final_data_sum)))

    # Should now have approximately 24 kWh total (1 kWh per hour for 24 hours)
    expected_final_total = 24.0
    if final_data_sum != expected_final_total:
        print("ERROR: Expected final total around {} kWh, got {} kWh".format(expected_final_total, final_data_sum))
        for minute in range(0, 24 * 60, 15):
            print("  Minute {}: {}".format(minute, test_data.get(minute, 0)))
        failed = True
    else:
        print("Gap filling successful: filled from {} kWh to {} kWh".format(dp2(initial_data_sum), dp2(final_data_sum)))

    # Test 2.1 alternate gaps in one hour intervals
    print("Test 2.1: Data with some gaps")
    test_data = {}

    running_total = 0
    step_increment = 1.0 / 60
    for minute in range(0, 24 * 60):
        hour = int(minute / 60)
        if hour % 2 == 0:
            running_total += step_increment  # Increment only in alternate hours
        test_data[24 * 60 - minute - 1] = dp4(running_total)  # Backwards indexing as used in function

    initial_data_sum = dp2(add_incrementing_sensor_total(test_data))
    print("Initial partial data sum: {} kWh".format(dp2(initial_data_sum)))

    # Call the function
    my_predbat.previous_days_modal_filter(test_data)

    final_data_sum = dp2(add_incrementing_sensor_total(test_data))
    print("Final data sum after gap filling: {} kWh".format(dp2(final_data_sum)))

    # Should now have approximately 24 kWh total (1 kWh per hour for 24 hours)
    expected_final_total = 24.0
    if abs(final_data_sum - expected_final_total) > 1.0:  # Allow 1 kWh tolerance
        print("ERROR: Expected final total around {} kWh, got {} kWh".format(expected_final_total, final_data_sum))
        for minute in range(0, 24 * 60, 15):
            print("  Minute {}: {}".format(minute, test_data.get(minute, 0)))
        failed = True
    else:
        print("Gap filling successful: filled from {} kWh to {} kWh".format(dp2(initial_data_sum), dp2(final_data_sum)))

    # Test 3: Modal filtering - remove lowest consumption day
    print("Test 3: Modal filtering removes lowest day")

    # Reset for modal filter test
    my_predbat.days_previous = [1, 2, 3]
    my_predbat.days_previous_weight = [1.0, 1.0, 1.0]
    original_days_count = len(my_predbat.days_previous)

    # Create test data with different consumption per day
    test_data = {}

    # Day 1: Low consumption (10 kWh total)
    day1_total = 10.0
    step_increment_day1 = day1_total / (24 * 60)
    running_total = 0
    for minute in range(0, 24 * 60):
        running_total += step_increment_day1
        test_data[24 * 60 - minute] = running_total

    # Day 2: Medium consumption (20 kWh total)
    day2_total = 20.0
    step_increment_day2 = day2_total / (24 * 60)
    running_total = 0
    for minute in range(0, 24 * 60):
        running_total += step_increment_day2
        test_data[2 * 24 * 60 - minute] = running_total

    # Day 3: High consumption (30 kWh total)
    day3_total = 30.0
    step_increment_day3 = day3_total / (24 * 60)
    running_total = 0
    for minute in range(0, 24 * 60):
        running_total += step_increment_day3
        test_data[3 * 24 * 60 - minute] = running_total

    print("Created test data with day totals: {} kWh, {} kWh, {} kWh".format(day1_total, day2_total, day3_total))

    # Call the function - should remove day 1 (lowest consumption)
    my_predbat.previous_days_modal_filter(test_data)
    print(my_predbat.days_previous)

    # Check that one day was removed
    final_days_count = len(my_predbat.days_previous)
    if final_days_count != original_days_count - 1:
        print("ERROR: Expected modal filter to remove 1 day, had {} days, now have {} days".format(original_days_count, final_days_count))
        failed = True
    elif 1 in my_predbat.days_previous:
        print("ERROR: Expected modal filter to remove day 1 (lowest consumption), but it's still present")
        failed = True
    else:
        print("Modal filter correctly removed lowest consumption day")

    # Restore original get_arg method
    my_predbat.get_arg = original_get_arg

    return failed


def test_download_octopus_url_wrapper(my_predbat):
    """
    Wrapper to run the async test function
    """
    return asyncio.run(test_download_octopus_url(my_predbat))


async def test_download_octopus_url(my_predbat):
    """
    Test the download_octopus_url function
    """
    print("**** Running download_octopus_url tests ****")
    failed = False

    # Test URL for VAR-22-11-01 tariff
    test_url = "https://api.octopus.energy/v1/products/VAR-22-11-01/electricity-tariffs/E-2R-VAR-22-11-01-A/standard-unit-rates/"

    # Test the download function
    api = OctopusAPI(my_predbat, key="", account_id="", automatic=False)
    # api.now_utc = my_predbat.now_utc
    rates_data = await api.async_download_octopus_url(test_url)

    # Basic validation checks
    if not rates_data:
        print("ERROR: No rate data downloaded from URL {}".format(test_url))
        failed = True
    else:
        print("Successfully downloaded {} rate points from VAR-22-11-01 tariff".format(len(rates_data)))
        pdata, ignore_io = minute_data(rates_data, my_predbat.forecast_days + 1, my_predbat.midnight_utc, "value_inc_vat", "valid_from", backwards=False, to_key="valid_to")
        if len(pdata) < 24 * 60:
            print("ERROR: Expecting at least {} minutes of rate data got {}".format(24 * 60, len(pdata)))
            failed = True
        else:
            print("Successfully processed {} minutes of rate data".format(len(pdata)))
            night_rate = pdata.get(60)
            day_rate = pdata.get(600)
            if night_rate == day_rate:
                print("ERROR: Expecting different night and day rates got {} and {}".format(night_rate, day_rate))
                failed = True

    return failed


def test_dynamic_load_car_slot_cancellation(my_predbat):
    """
    Test the dynamic_load function to verify that car charging slots are cancelled
    when load_last_period is low and within the threshold
    """
    print("*** Running test: Dynamic load car slot cancellation")
    failed = False

    # Setup test parameters
    reset_inverter(my_predbat)
    my_predbat.num_cars = 2
    my_predbat.minutes_now = 12 * 60  # 12:00 PM
    my_predbat.battery_rate_max_discharge = 5.0 / 60.0  # 5kW converted to kW per minute
    my_predbat.car_charging_threshold = 3.0  # 3kW
    my_predbat.metric_dynamic_load_adjust = True
    my_predbat.load_last_status = "baseline"  # Initialize status
    my_predbat.load_last_car_slot = False

    # Test 1: High load - should set load_last_status to "high" but not cancel slots
    print("Test 1: High load case")
    my_predbat.load_last_period = 6.0  # 6kW - higher than battery_rate_max_discharge * MINUTE_WATT / 1000

    # Create car charging slots that overlap with current time period
    my_predbat.car_charging_slots = [[], [], [], []]
    my_predbat.car_charging_slots[0] = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 25, "kwh": 10.0}]
    my_predbat.car_charging_slots[1] = [{"start": my_predbat.minutes_now + 5, "end": my_predbat.minutes_now + 40, "kwh": 8.0}]

    # Store original slot data for comparison
    original_slot_0_kwh = my_predbat.car_charging_slots[0][0]["kwh"]
    original_slot_1_kwh = my_predbat.car_charging_slots[1][0]["kwh"]

    # Call dynamic_load
    status_changed = my_predbat.dynamic_load()

    # Verify high load status
    if my_predbat.load_last_status != "high":
        print(f"ERROR: Expected load_last_status to be 'high', got '{my_predbat.load_last_status}'")
        failed = True

    # Verify slots were NOT cancelled (high load doesn't cancel slots)
    if my_predbat.car_charging_slots[0][0]["kwh"] != original_slot_0_kwh:
        print(f"ERROR: Car slot 0 kwh should not have changed, was {original_slot_0_kwh}, now {my_predbat.car_charging_slots[0][0]['kwh']}")
        failed = True

    if my_predbat.car_charging_slots[1][0]["kwh"] != original_slot_1_kwh:
        print(f"ERROR: Car slot 1 kwh should not have changed, was {original_slot_1_kwh}, now {my_predbat.car_charging_slots[1][0]['kwh']}")
        failed = True

    # Test 2: Low load - should cancel car slots that overlap with current time
    print("Test 2: Low load case")
    my_predbat.load_last_status = "baseline"  # Reset status
    my_predbat.load_last_period = 2.0  # 2kW - low load (< battery_rate_max_discharge * 0.9 * MINUTE_WATT / 1000 and < car_charging_threshold * 0.9)
    my_predbat.load_last_car_slot = True

    my_predbat.car_charging_slots[0] = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 25, "kwh": 10.0}]
    my_predbat.car_charging_slots[1] = [{"start": my_predbat.minutes_now + 30, "end": my_predbat.minutes_now + 45, "kwh": 8.0}]

    # Call dynamic_load
    status_changed = my_predbat.dynamic_load()

    # Verify low load status
    if my_predbat.load_last_status != "low":
        print(f"ERROR: Expected load_last_status to be 'low', got '{my_predbat.load_last_status}'")
        failed = True

    # Verify that car slot 0 was cancelled (overlaps with current time)
    if my_predbat.car_charging_slots[0][0]["kwh"] != 0:
        print(f"ERROR: Car slot 0 should have been cancelled (kwh=0), but kwh = {my_predbat.car_charging_slots[0][0]['kwh']}")
        failed = True

    # Verify that car slot 1 was also cancelled (overlaps with current 30-minute period)
    if my_predbat.car_charging_slots[1][0]["kwh"] != 0:
        print(f"ERROR: Car slot 1 should have been cancelled (kwh=0), but kwh = {my_predbat.car_charging_slots[1][0]['kwh']}")
        failed = True

    # Test 3: Low load but slots don't overlap with current time - should not cancel
    print("Test 3.1: Low load with non-overlapping slots, first time car starts")
    my_predbat.load_last_status = "baseline"  # Reset status
    my_predbat.load_last_period = 2.0  # Low load
    my_predbat.load_last_car_slot = False

    # Create slots that don't overlap with current time period
    my_predbat.car_charging_slots[0] = [{"start": my_predbat.minutes_now - 5, "end": my_predbat.minutes_now + 90, "kwh": 10.0}]
    my_predbat.car_charging_slots[1] = [{"start": my_predbat.minutes_now - 60, "end": my_predbat.minutes_now - 30, "kwh": 8.0}]

    # Call dynamic_load
    status_changed = my_predbat.dynamic_load()

    if my_predbat.car_charging_slots[0][0]["kwh"] != 10.0:
        print(f"ERROR: Car slot 0 should not have been cancelled, kwh = {my_predbat.car_charging_slots[0][0]['kwh']}")
        failed = True

    if my_predbat.car_charging_slots[1][0]["kwh"] != 8.0:
        print(f"ERROR: Car slot 1 should not have been cancelled, kwh = {my_predbat.car_charging_slots[1][0]['kwh']}")
        failed = True

    # Test 3: Low load but slots don't overlap with current time - should not cancel
    print("Test 3.2: Low load with non-overlapping slots")
    my_predbat.load_last_status = "baseline"  # Reset status
    my_predbat.load_last_period = 2.0  # Low load

    # Create slots that don't overlap with current time period
    my_predbat.car_charging_slots[0] = [{"start": my_predbat.minutes_now - 5, "end": my_predbat.minutes_now + 90, "kwh": 10.0}]
    my_predbat.car_charging_slots[1] = [{"start": my_predbat.minutes_now - 60, "end": my_predbat.minutes_now - 30, "kwh": 8.0}]

    # Call dynamic_load
    status_changed = my_predbat.dynamic_load()

    if my_predbat.car_charging_slots[0][0]["kwh"] != 0:
        print(f"ERROR: Car slot 0 should have been cancelled, kwh = {my_predbat.car_charging_slots[0][0]['kwh']}")
        failed = True

    if my_predbat.car_charging_slots[1][0]["kwh"] != 8.0:
        print(f"ERROR: Car slot 1 should not have been cancelled, kwh = {my_predbat.car_charging_slots[1][0]['kwh']}")
        failed = True

    # Test 4: Just after midnight (minutes_now <= 5) - should not cancel even with low load
    print("Test 4: Low load just after midnight")
    my_predbat.minutes_now = 3  # 3 minutes after midnight
    my_predbat.load_last_status = "baseline"  # Reset status
    my_predbat.load_last_period = 2.0  # Low load
    my_predbat.load_last_car_slot = False

    # Create slots that overlap with current time
    my_predbat.car_charging_slots[0] = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 25, "kwh": 10.0}]

    # Call dynamic_load
    status_changed = my_predbat.dynamic_load()

    # Verify slots were NOT cancelled (due to midnight exclusion)
    if my_predbat.car_charging_slots[0][0]["kwh"] != 10.0:
        print(f"ERROR: Car slot should not have been cancelled just after midnight, kwh = {my_predbat.car_charging_slots[0][0]['kwh']}")
        failed = True

    # Test 5: Metric dynamic load adjust disabled - should not cancel slots
    print("Test 5: Dynamic load adjust disabled")
    my_predbat.minutes_now = 12 * 60  # Reset to noon
    my_predbat.metric_dynamic_load_adjust = False  # Disable dynamic load adjust
    my_predbat.load_last_status = "baseline"  # Reset status
    my_predbat.load_last_period = 2.0  # Low load

    # Create slots that overlap with current time
    my_predbat.car_charging_slots[0] = [{"start": my_predbat.minutes_now - 5, "end": my_predbat.minutes_now + 25, "kwh": 10.0}]

    # Call dynamic_load
    status_changed = my_predbat.dynamic_load()

    # Verify slots were NOT cancelled (feature disabled)
    if my_predbat.car_charging_slots[0][0]["kwh"] != 10.0:
        print(f"ERROR: Car slot should not have been cancelled when feature disabled, kwh = {my_predbat.car_charging_slots[0][0]['kwh']}")
        failed = True

    if not failed:
        print("*** Dynamic load car slot cancellation test PASSED")
    else:
        print("*** Dynamic load car slot cancellation test FAILED")

    return failed


def test_octopus_free(my_predbat):
    """
    Test Octopus free electricity session download
    """
    failed = False
    print("**** Running Octopus free electricity test ****")

    free_sessions = my_predbat.download_octopus_free("http://octopus.energy/free-electricity")
    free_sessions = my_predbat.download_octopus_free("http://octopus.energy/free-electricity")
    # if not free_sessions:
    #    print("**** ERROR: No free sessions found ****")
    #    failed = True

    if not failed:
        print("**** Octopus free electricity test PASSED ****")
    else:
        print("**** Octopus free electricity test FAILED ****")

    return failed


def test_get_now_from_cumulative(my_predbat):
    """
    Test the get_now_from_cumulative function from utils
    """
    failed = False
    print("**** Testing get_now_from_cumulative function ****")

    # Test 1: backwards=True - normal data
    print("Test 1: backwards=True with normal data")
    data = {0: 100, 10: 90, 15: 85, 20: 80, 25: 75, 30: 70}
    minutes_now = 15
    result = get_now_from_cumulative(data, minutes_now, backwards=True)
    # backwards: lowest in range 15-10 is min(85,90) = 85, value = data[0] - lowest = 100 - 85 = 15
    expected = 15
    if result != expected:
        print(f"ERROR: Test 1 failed - expected {expected} got {result}")
        failed = True

    # Test 2: backwards=False - normal data
    print("Test 2: backwards=False with normal data")
    data = {0: 10, 1: 12, 2: 14, 3: 16, 4: 18, 10: 30, 15: 45}
    minutes_now = 15
    result = get_now_from_cumulative(data, minutes_now, backwards=False)
    # forwards: lowest in range 0-4 is min(10,12,14,16,18) = 10, value = data[15] - lowest = 45 - 10 = 35
    expected = 35
    if result != expected:
        print(f"ERROR: Test 2 failed - expected {expected} got {result}")
        failed = True

    # Test 3: backwards=True with missing keys in lookup range
    print("Test 3: backwards=True with missing keys")
    data = {0: 100, 15: 85}  # Missing 11,12,13,14
    minutes_now = 15
    result = get_now_from_cumulative(data, minutes_now, backwards=True)
    # lowest = min(data.get(15,inf), data.get(14,inf), data.get(13,inf), data.get(12,inf), data.get(11,inf)) = 85
    expected = 15  # 100 - 85
    if result != expected:
        print(f"ERROR: Test 3 failed - expected {expected} got {result}")
        failed = True

    # Test 4: backwards=False with missing keys at start
    print("Test 4: backwards=False with missing keys at start")
    data = {5: 50, 10: 100}  # Missing 0,1,2,3,4
    minutes_now = 10
    result = get_now_from_cumulative(data, minutes_now, backwards=False)
    # lowest from range 0-4 all missing so lowest stays 9999999999, value = data.get(10,0) - lowest = 100 - 9999999999 < 0, max(value,0) = 0
    expected = 0
    if result != expected:
        print(f"ERROR: Test 4 failed - expected {expected} got {result}")
        failed = True

    # Test 5: Empty data returns 0
    print("Test 5: Empty data")
    data = {}
    result = get_now_from_cumulative(data, 10, backwards=True)
    expected = 0  # max(0 - 9999999999, 0) = 0
    if result != expected:
        print(f"ERROR: Test 5 failed - expected {expected} got {result}")
        failed = True

    # Test 6: backwards=True at minute 0
    print("Test 6: backwards=True at minute 0")
    data = {0: 50, 1: 45, 2: 40}
    minutes_now = 0
    result = get_now_from_cumulative(data, minutes_now, backwards=True)
    # Range is 0 to -4 (but negative indices won't be in data), so lowest is data.get(0) = 50
    expected = 0  # data[0] - lowest = 50 - 50 = 0
    if result != expected:
        print(f"ERROR: Test 6 failed - expected {expected} got {result}")
        failed = True

    print("**** get_now_from_cumulative tests completed ****")
    return failed


def test_prune_today(my_predbat):
    """
    Test the prune_today function from utils
    """
    failed = False
    print("**** Testing prune_today function ****")

    import pytz

    utc = pytz.UTC
    now_utc = datetime(2024, 10, 15, 14, 30, 0, tzinfo=utc)
    midnight_utc = datetime(2024, 10, 15, 0, 0, 0, tzinfo=utc)

    # Test 1: prune=True removes data before midnight
    print("Test 1: prune=True removes data before midnight")
    data = {
        "2024-10-14T23:00:00+00:00": 10,  # Before midnight - should be pruned
        "2024-10-15T01:00:00+00:00": 20,  # After midnight - should be kept
        "2024-10-15T10:00:00+00:00": 30,  # After midnight - should be kept
    }
    result = prune_today(data, now_utc, midnight_utc, prune=True, group=15)
    if "2024-10-14T23:00:00+00:00" in result:
        print("ERROR: Test 1 failed - data before midnight should be pruned")
        failed = True
    if "2024-10-15T01:00:00+00:00" not in result or "2024-10-15T10:00:00+00:00" not in result:
        print("ERROR: Test 1 failed - data after midnight should be kept")
        failed = True

    # Test 2: prune=False keeps all data
    print("Test 2: prune=False keeps all data")
    result = prune_today(data, now_utc, midnight_utc, prune=False, group=15)
    if len(result) != 3:
        print(f"ERROR: Test 2 failed - expected 3 entries, got {len(result)}")
        failed = True

    # Test 3: group parameter filters close timestamps
    print("Test 3: group parameter filters close timestamps")
    data = {
        "2024-10-15T10:00:00+00:00": 10,
        "2024-10-15T10:05:00+00:00": 15,  # Within 15 min of previous - should be skipped
        "2024-10-15T10:20:00+00:00": 20,  # More than 15 min from first - should be kept
    }
    result = prune_today(data, now_utc, midnight_utc, prune=False, group=15)
    if len(result) != 2:
        print(f"ERROR: Test 3 failed - expected 2 entries (grouped), got {len(result)}")
        failed = True

    # Test 4: prune_future=True removes future data
    print("Test 4: prune_future=True removes future data")
    data = {
        "2024-10-15T10:00:00+00:00": 10,  # Past - should be kept
        "2024-10-15T16:00:00+00:00": 20,  # Future - should be pruned
    }
    result = prune_today(data, now_utc, midnight_utc, prune=False, group=15, prune_future=True)
    if "2024-10-15T16:00:00+00:00" in result:
        print("ERROR: Test 4 failed - future data should be pruned")
        failed = True
    if "2024-10-15T10:00:00+00:00" not in result:
        print("ERROR: Test 4 failed - past data should be kept")
        failed = True

    # Test 5: intermediate=True adds data points in gaps
    print("Test 5: intermediate=True adds intermediate data points")
    data = {
        "2024-10-15T10:00:00+00:00": 10,
        "2024-10-15T11:00:00+00:00": 20,  # 60 min gap > 15 min group
    }
    result = prune_today(data, now_utc, midnight_utc, prune=False, group=15, intermediate=True)
    # Should have original 2 entries + intermediate entries (60/15 - 1 = 3 intermediate points)
    if len(result) < 4:
        print(f"ERROR: Test 5 failed - expected at least 4 entries with intermediates, got {len(result)}")
        failed = True
    else:
        # Check original values are preserved
        if result.get("2024-10-15T10:00:00+00:00") != 10:
            print(f"ERROR: Test 5 failed - expected value 10 at 10:00, got {result.get('2024-10-15T10:00:00+00:00')}")
            failed = True
        if result.get("2024-10-15T11:00:00+00:00") != 20:
            print(f"ERROR: Test 5 failed - expected value 20 at 11:00, got {result.get('2024-10-15T11:00:00+00:00')}")
            failed = True

    # Test 6: TIME_FORMAT_SECONDS format (with microseconds)
    print("Test 6: TIME_FORMAT_SECONDS format")
    data = {
        "2024-10-15T10:00:00.123456+00:00": 10,
        "2024-10-15T11:00:00.654321+00:00": 20,
    }
    result = prune_today(data, now_utc, midnight_utc, prune=False, group=15)
    if len(result) != 2:
        print(f"ERROR: Test 6 failed - expected 2 entries, got {len(result)}")
        failed = True
    else:
        # Check values are preserved with microsecond timestamps
        values = list(result.values())
        if 10 not in values or 20 not in values:
            print(f"ERROR: Test 6 failed - expected values [10, 20], got {values}")
            failed = True

    print("**** prune_today tests completed ****")
    return failed


def test_minute_data_state(my_predbat):
    """
    Test the minute_data_state function from utils
    """
    failed = False
    print("**** Testing minute_data_state function ****")

    import pytz

    utc = pytz.UTC
    now = datetime(2024, 10, 15, 12, 0, 0, tzinfo=utc)

    # Test 1: Empty history returns empty dict (line 197)
    print("Test 1: Empty history returns empty dict")
    result = minute_data_state([], days=1, now=now, state_key="state", last_updated_key="last_updated")
    if result != {}:
        print(f"ERROR: Test 1 failed - expected empty dict, got {result}")
        failed = True

    # Test 2: None history returns empty dict
    print("Test 2: None history returns empty dict")
    result = minute_data_state(None, days=1, now=now, state_key="state", last_updated_key="last_updated")
    if result != {}:
        print(f"ERROR: Test 2 failed - expected empty dict, got {result}")
        failed = True

    # Test 3: Missing state_key is skipped (line 203)
    print("Test 3: Missing state_key is skipped")
    history = [
        {"last_updated": "2024-10-15T10:00:00+00:00"},  # Missing state
        {"state": "charging", "last_updated": "2024-10-15T11:00:00+00:00"},
    ]
    result = minute_data_state(history, days=1, now=now, state_key="state", last_updated_key="last_updated")
    # Should process only the second entry
    if len(result) == 0:
        print(f"ERROR: Test 3 failed - expected data, got empty result")
        failed = True

    # Test 4: Missing last_updated_key is skipped (line 205)
    print("Test 4: Missing last_updated_key is skipped")
    history = [
        {"state": "charging"},  # Missing last_updated
        {"state": "idle", "last_updated": "2024-10-15T11:00:00+00:00"},
    ]
    result = minute_data_state(history, days=1, now=now, state_key="state", last_updated_key="last_updated")
    if len(result) == 0:
        print(f"ERROR: Test 4 failed - expected data, got empty result")
        failed = True

    # Test 5: unavailable/unknown values are skipped (lines 208-209)
    print("Test 5: unavailable/unknown values are skipped")
    history = [
        {"state": "unavailable", "last_updated": "2024-10-15T10:00:00+00:00"},
        {"state": "unknown", "last_updated": "2024-10-15T10:30:00+00:00"},
        {"state": "charging", "last_updated": "2024-10-15T11:00:00+00:00"},
    ]
    result = minute_data_state(history, days=1, now=now, state_key="state", last_updated_key="last_updated")
    # Should only process the "charging" entry
    if 0 not in result:
        print(f"ERROR: Test 5 failed - expected data at minute 0")
        failed = True

    # Test 6: Normal state tracking and interpolation
    print("Test 6: Normal state tracking")
    history = [
        {"state": "idle", "last_updated": "2024-10-15T10:00:00+00:00"},
        {"state": "charging", "last_updated": "2024-10-15T11:00:00+00:00"},
    ]
    result = minute_data_state(history, days=1, now=now, state_key="state", last_updated_key="last_updated")
    # Should have data filled for the day
    if len(result) < 60 * 24:
        print(f"ERROR: Test 6 failed - expected full day of data, got {len(result)} entries")
        failed = True

    # Test 7: State at minute 0 should be most recent
    print("Test 7: State at minute 0 is most recent")
    history = [
        {"state": "idle", "last_updated": "2024-10-15T10:00:00+00:00"},
        {"state": "charging", "last_updated": "2024-10-15T11:55:00+00:00"},
    ]
    result = minute_data_state(history, days=1, now=now, state_key="state", last_updated_key="last_updated")
    if result.get(0) != "charging":
        print(f"ERROR: Test 7 failed - expected 'charging' at minute 0, got {result.get(0)}")
        failed = True

    print("**** minute_data_state tests completed ****")
    return failed


def test_format_time_ago(my_predbat):
    """
    Test the format_time_ago function from utils
    """
    failed = False
    print("**** Testing format_time_ago function ****")

    from datetime import timezone

    now = datetime.now(timezone.utc)

    # Test 1: None input returns "Never updated" (line 617)
    print("Test 1: None input returns 'Never updated'")
    result = format_time_ago(None)
    if result != "Never updated":
        print(f"ERROR: Test 1 failed - expected 'Never updated', got '{result}'")
        failed = True

    # Test 2: Just now (0 minutes ago) (line 631)
    print("Test 2: Just now (0 minutes)")
    result = format_time_ago(now)
    if result != "Just now":
        print(f"ERROR: Test 2 failed - expected 'Just now', got '{result}'")
        failed = True

    # Test 3: 1 minute ago (line 633)
    print("Test 3: 1 minute ago")
    one_min_ago = now - timedelta(minutes=1, seconds=30)
    result = format_time_ago(one_min_ago)
    if result != "1 minute ago":
        print(f"ERROR: Test 3 failed - expected '1 minute ago', got '{result}'")
        failed = True

    # Test 4: Multiple minutes ago (line 635)
    print("Test 4: Multiple minutes ago")
    five_min_ago = now - timedelta(minutes=5)
    result = format_time_ago(five_min_ago)
    if result != "5 minutes ago":
        print(f"ERROR: Test 4 failed - expected '5 minutes ago', got '{result}'")
        failed = True

    # Test 5: 45 minutes ago
    print("Test 5: 45 minutes ago")
    fortyfive_min_ago = now - timedelta(minutes=45)
    result = format_time_ago(fortyfive_min_ago)
    if result != "45 minutes ago":
        print(f"ERROR: Test 5 failed - expected '45 minutes ago', got '{result}'")
        failed = True

    # Test 6: 1 hour ago (60-119 minutes) (line 637)
    print("Test 6: 1 hour ago")
    one_hour_ago = now - timedelta(minutes=65)
    result = format_time_ago(one_hour_ago)
    if result != "1 hour ago":
        print(f"ERROR: Test 6 failed - expected '1 hour ago', got '{result}'")
        failed = True

    # Test 7: Multiple hours ago (lines 638-639)
    print("Test 7: Multiple hours ago")
    five_hours_ago = now - timedelta(hours=5)
    result = format_time_ago(five_hours_ago)
    if result != "5 hours ago":
        print(f"ERROR: Test 7 failed - expected '5 hours ago', got '{result}'")
        failed = True

    # Test 8: 1 day ago (line 643)
    print("Test 8: 1 day ago")
    one_day_ago = now - timedelta(days=1, hours=2)
    result = format_time_ago(one_day_ago)
    if result != "1 day ago":
        print(f"ERROR: Test 8 failed - expected '1 day ago', got '{result}'")
        failed = True

    # Test 9: Multiple days ago (line 645)
    print("Test 9: Multiple days ago")
    three_days_ago = now - timedelta(days=3)
    result = format_time_ago(three_days_ago)
    if result != "3 days ago":
        print(f"ERROR: Test 9 failed - expected '3 days ago', got '{result}'")
        failed = True

    # Test 10: Future time returns "Just now" (line 629)
    print("Test 10: Future time returns 'Just now'")
    future_time = now + timedelta(minutes=10)
    result = format_time_ago(future_time)
    if result != "Just now":
        print(f"ERROR: Test 10 failed - expected 'Just now' for future time, got '{result}'")
        failed = True

    print("**** format_time_ago tests completed ****")
    return failed


def run_debug_cases(my_predbat):
    """
    Run debug case files from the cases directory
    """
    failed = False
    print("**** Running debug case files ****")

    # Scan .yaml files in cases directory
    for filename in glob.glob("cases/*.yaml"):
        basename = os.path.basename(filename)
        pathname = os.path.dirname(filename)
        test_failed = run_single_debug(basename, my_predbat, filename, pathname + "/" + basename + ".expected.json")
        if test_failed:
            print(f"**** Debug case {basename}: FAILED ****")
            failed = True
            break
        else:
            print(f"**** Debug case {basename}: PASSED ****")

    return failed


def main():
    # Test registry - table of all available tests
    # Format: (name, function, description, slow)
    TEST_REGISTRY = [
        ("perf", run_perf_test, "Performance tests", False),
        ("model", run_model_tests, "Model tests", False),
        ("inverter", run_inverter_tests, "Inverter tests", False),
        ("execute", run_execute_tests, "Execute tests", False),
        ("basic_rates", test_basic_rates, "Basic rates tests", False),
        ("window_sort", run_window_sort_tests, "Window sort tests", False),
        ("window2minutes", test_window2minutes, "Window to minutes tests", False),
        ("compute_metric", run_compute_metric_tests, "Compute metric tests", False),
        ("minute_data", test_minute_data, "Minute data tests", False),
        ("get_now_cumulative", test_get_now_from_cumulative, "Get now from cumulative tests", False),
        ("prune_today", test_prune_today, "Prune today tests", False),
        ("history_attribute", test_history_attribute, "History attribute tests", False),
        ("minute_data_state", test_minute_data_state, "Minute data state tests", False),
        ("format_time_ago", test_format_time_ago, "Format time ago tests", False),
        ("override_time", test_get_override_time_from_string, "Override time from string tests", False),
        ("previous_days_modal", test_previous_days_modal_filter, "Previous days modal filter tests", False),
        ("octopus_url", test_download_octopus_url_wrapper, "Octopus URL download tests", False),
        ("plugin_startup", test_plugin_startup_order, "Plugin startup order tests", False),
        ("dynamic_load_car", test_dynamic_load_car_slot_cancellation, "Dynamic load car slot cancellation tests", False),
        ("units", run_test_units, "Unit tests", False),
        ("manual_api", run_test_manual_api, "Manual API tests", False),
        ("web_if", run_test_web_if, "Web interface tests", False),
        ("nordpool", run_nordpool_test, "Nordpool tests", False),
        ("octopus_slots", run_load_octopus_slots_tests, "Load Octopus slots tests", False),
        ("find_charge_rate", test_find_charge_rate, "Find charge rate tests", False),
        ("energydataservice", test_energydataservice, "Energy data service tests", False),
        ("saving_session", test_saving_session, "Saving session tests", False),
        ("alert_feed", test_alert_feed, "Alert feed tests", False),
        ("iboost_smart", run_iboost_smart_tests, "iBoost smart tests", False),
        ("car_charging_smart", run_car_charging_smart_tests, "Car charging smart tests", False),
        ("intersect_window", run_intersect_window_tests, "Intersect window tests", False),
        ("inverter_multi", run_inverter_multi_tests, "Inverter multi tests", False),
        ("octopus_free", test_octopus_free, "Octopus free electricity tests", False),
        ("optimise_levels", run_optimise_levels_tests, "Optimise levels tests", True),
        ("optimise_windows", run_optimise_all_windows_tests, "Optimise all windows tests", True),
        ("debug_cases", run_debug_cases, "Debug case file tests", True),
    ]

    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Predbat unit tests")
    parser.add_argument("--debug_file", action="store", help="Enable debug output")
    parser.add_argument("--full_debug", action="store_true", help="Enable full debug output")
    parser.add_argument("--compare", action="store_true", help="Run compare")
    parser.add_argument("--gecloud", action="store_true", help="Run tests for GivEnergy Cloud")
    parser.add_argument("--octopus_api", action="store", help="Run Octopus API tests with given token")
    parser.add_argument("--octopus_account", action="store", help="Octopus API account ID")
    parser.add_argument("--test", "-t", action="store", help="Run a specific test by name (use --list to see available tests)")
    parser.add_argument("--list", "-l", action="store_true", help="List all available tests")
    parser.add_argument("--quick", "-q", action="store_true", help="Skip slow tests (optimise_levels, optimise_windows, debug_cases)")
    args = parser.parse_args()

    # List available tests
    if args.list:
        print("Available tests:")
        print("-" * 70)
        for name, _, desc, slow in TEST_REGISTRY:
            slow_marker = " [slow]" if slow else ""
            print(f"  {name:25s} - {desc}{slow_marker}")
        print("-" * 70)
        print("\nUsage: python unit_test.py --test <test_name>")
        print("       python unit_test.py --test basic_rates")
        print("       python unit_test.py --quick  # Skip slow tests")
        sys.exit(0)

    print("**** Starting Predbat tests ****")
    my_predbat = PredBat()
    my_predbat.states = {}
    my_predbat.reset()
    my_predbat.update_time()
    my_predbat.ha_interface = TestHAInterface()
    my_predbat.ha_interface.history_enable = False
    my_predbat.auto_config()
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.ha_interface.history_enable = True
    my_predbat.expose_config("plan_debug", True)
    print("**** Testing Predbat ****")
    failed = False

    if args.debug_file:
        run_single_debug(args.debug_file, my_predbat, args.debug_file, compare=args.compare, debug=args.full_debug)
        sys.exit(0)

    if not failed and args.gecloud:
        failed |= run_test_ge_cloud(my_predbat)
        return failed

    if not failed and args.octopus_api:
        failed |= run_test_octopus_api(my_predbat, args.octopus_api, args.octopus_account)
        return failed

    # Run a specific test if requested
    if args.test:
        test_found = False
        for name, func, desc, slow in TEST_REGISTRY:
            if name == args.test:
                test_found = True
                print(f"**** Running single test: {name} - {desc} ****")
                start_time = time.time()
                failed = func(my_predbat)
                elapsed = time.time() - start_time
                if failed:
                    print(f"**** ERROR: Test {args.test} FAILED in {elapsed:.2f}s ****")
                else:
                    print(f"**** Test {args.test} PASSED in {elapsed:.2f}s ****")
                break
        if not test_found:
            print(f"ERROR: Test '{args.test}' not found. Use --list to see available tests.")
            sys.exit(1)
        if failed:
            sys.exit(1)
        sys.exit(0)

    # Run all tests from the registry
    total_time = 0
    skipped_count = 0
    for name, func, desc, slow in TEST_REGISTRY:
        if args.quick and slow:
            print(f"**** Skipping: {name} (slow) ****")
            skipped_count += 1
            continue
        print(f"**** Running: {name} ****")
        start_time = time.time()
        test_failed = func(my_predbat)
        elapsed = time.time() - start_time
        total_time += elapsed
        if test_failed:
            print(f"**** {name}: FAILED in {elapsed:.2f}s ****")
            failed = True
            break
        else:
            print(f"**** {name}: PASSED in {elapsed:.2f}s ****")

    if failed:
        print(f"**** ERROR: Some tests failed (total time: {total_time:.2f}s) ****")
        sys.exit(1)
    if skipped_count > 0:
        print(f"**** All tests passed ({skipped_count} slow tests skipped, total time: {total_time:.2f}s) ****")
    else:
        print(f"**** All tests passed (total time: {total_time:.2f}s) ****")
    sys.exit(0)


if __name__ == "__main__":
    main()
