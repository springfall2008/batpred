# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2025 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import os
import time
import sys
import glob
import argparse

from predbat import PredBat
from octopus import OctopusAPI
from tests.test_infra import TestHAInterface
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
from tests.test_optimise_all_windows import run_optimise_all_windows_tests
from tests.test_nordpool import run_nordpool_test
from tests.test_car_charging_smart import run_car_charging_smart_tests
from tests.test_plugin_startup import test_plugin_startup_order
from tests.test_optimise_levels import run_optimise_levels_tests
from tests.test_energydataservice import test_energydataservice
from tests.test_iboost import run_iboost_smart_tests
from tests.test_alert_feed import test_alert_feed
from tests.test_single_debug import run_single_debug
from tests.test_saving_session import test_saving_session
from tests.test_ge_cloud import run_test_ge_cloud
from tests.test_web_if import run_test_web_if
from tests.test_window import run_window_sort_tests, run_intersect_window_tests
from tests.test_find_charge_rate import test_find_charge_rate
from tests.test_manual_api import run_test_manual_api
from tests.test_minute_data import test_minute_data
from tests.test_minute_data_state import test_minute_data_state
from tests.test_format_time_ago import test_format_time_ago
from tests.test_override_time import test_get_override_time_from_string
from tests.test_units import run_test_units
from tests.test_previous_days_modal import test_previous_days_modal_filter
from tests.test_octopus_free import test_octopus_free
from tests.test_prune_today import test_prune_today
from tests.test_cumulative import test_get_now_from_cumulative
from tests.test_octopus_url import test_download_octopus_url_wrapper
from tests.test_dynamic_load import test_dynamic_load_car_slot_cancellation
from tests.test_fox_api import run_fox_api_tests
from tests.test_solcast import run_solcast_tests
from tests.test_rate_add_io_slots import run_rate_add_io_slots_tests


# Mock the components and plugin system

KEEP_SCALE = 0.5


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
        ("rate_add_io_slots", run_rate_add_io_slots_tests, "Rate add IO slots tests", False),
        ("find_charge_rate", test_find_charge_rate, "Find charge rate tests", False),
        ("energydataservice", test_energydataservice, "Energy data service tests", False),
        ("saving_session", test_saving_session, "Saving session tests", False),
        ("alert_feed", test_alert_feed, "Alert feed tests", False),
        ("fox_api", run_fox_api_tests, "Fox API tests", False),
        ("solcast", run_solcast_tests, "Solcast API tests", False),
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
