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
from tests.test_infra import TestHAInterface
from tests.test_compute_metric import run_compute_metric_tests
from tests.test_perf import run_perf_test
from tests.test_model import run_model_tests
from tests.test_execute import run_execute_tests
from tests.test_octopus_slots import run_load_octopus_slots_tests
from tests.test_fetch_config_options import test_fetch_config_options
from tests.test_multi_inverter import run_inverter_multi_tests
from tests.test_window2minutes import test_window2minutes
from tests.test_history_attribute import test_history_attribute
from tests.test_inverter import run_inverter_tests
from tests.test_basic_rates import test_basic_rates
from tests.test_find_charge_curve import run_find_charge_curve_tests
from tests.test_find_battery_size import run_find_battery_size_tests
from tests.test_optimise_all_windows import run_optimise_all_windows_tests
from tests.test_nordpool import run_nordpool_test
from tests.test_car_charging_smart import run_car_charging_smart_tests
from tests.test_plugin_startup import test_plugin_startup_order
from tests.test_optimise_levels import run_optimise_levels_tests
from tests.test_energydataservice import test_energydataservice
from tests.test_iboost import run_iboost_smart_tests
from tests.test_alert_feed import test_alert_feed
from tests.test_solax import run_solax_tests
from tests.test_single_debug import run_single_debug
from tests.test_saving_session import test_saving_session, test_saving_session_null_octopoints, test_saving_session_notify_config
from tests.test_secrets import run_secrets_tests
from tests.test_ge_cloud import test_ge_cloud
from tests.test_axle import test_axle
from tests.test_db_manager import test_db_manager
from tests.test_hahistory import run_hahistory_tests
from tests.test_hainterface_state import run_hainterface_state_tests
from tests.test_hainterface_api import run_hainterface_api_tests
from tests.test_hainterface_service import run_hainterface_service_tests
from tests.test_hainterface_lifecycle import run_hainterface_lifecycle_tests
from tests.test_hainterface_websocket import run_hainterface_websocket_tests
from tests.test_web_if import run_test_web_if
from tests.test_window import run_window_sort_tests, run_intersect_window_tests
from tests.test_find_charge_rate import test_find_charge_rate, test_find_charge_rate_string_temperature, test_find_charge_rate_string_charge_curve
from tests.test_manual_api import run_test_manual_api
from tests.test_manual_soc import run_test_manual_soc
from tests.test_manual_times import run_test_manual_times
from tests.test_manual_select import run_test_manual_select
from tests.test_minute_data import test_minute_data, test_minute_data_load
from tests.test_minute_data_state import test_minute_data_state
from tests.test_format_time_ago import test_format_time_ago
from tests.test_override_time import test_get_override_time_from_string
from tests.test_units import run_test_units
from tests.test_previous_days_modal import test_previous_days_modal_filter
from tests.test_octopus_free import test_octopus_free
from tests.test_prune_today import test_prune_today
from tests.test_cumulative import test_get_now_from_cumulative
from tests.test_octopus_url import test_octopus_url
from tests.test_octopus_cache import test_octopus_cache_wrapper
from tests.test_octopus_events import test_octopus_events_wrapper
from tests.test_octopus_refresh_token import test_octopus_refresh_token_wrapper
from tests.test_octopus_misc import test_octopus_misc_wrapper
from tests.test_octopus_read_response import test_octopus_read_response_wrapper
from tests.test_octopus_rate_limit import test_octopus_rate_limit_wrapper
from tests.test_octopus_fetch_previous_dispatch import test_octopus_fetch_previous_dispatch_wrapper
from tests.test_fetch_octopus_rates import test_fetch_octopus_rates
from tests.test_fetch_tariffs import test_fetch_tariffs
from tests.test_fetch_url_cached import test_fetch_url_cached
from tests.test_load_free_slot import test_load_free_slot
from tests.test_add_now_to_octopus_slot import test_add_now_to_octopus_slot
from tests.test_dynamic_load import test_dynamic_load_car_slot_cancellation
from tests.test_fox_api import run_fox_api_tests
from tests.test_solcast import run_solcast_tests
from tests.test_rate_add_io_slots import run_rate_add_io_slots_tests
from tests.test_battery_curve_keys import run_battery_curve_keys_tests
from tests.test_balance_inverters import run_balance_inverters_tests
from tests.test_octopus_download_rates import test_octopus_download_rates_wrapper
from tests.test_integer_config import test_integer_config_entities, test_expose_config_preserves_integer
from tests.test_rate_replicate_missing_slots import test_rate_replicate
from tests.test_carbon import test_carbon
from tests.test_download import test_download
from tests.test_ohme import test_ohme
from tests.test_component_base import test_component_base_all
from tests.test_solis import run_solis_tests


# Mock the components and plugin system

KEEP_SCALE = 0.5


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


def create_predbat():
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
    return my_predbat


def main():
    # Test registry - table of all available tests
    # Format: (name, function, description, slow)
    TEST_REGISTRY = [
        ("secrets", run_secrets_tests, "Secrets loading tests", False),
        ("perf", run_perf_test, "Performance tests", False),
        ("model", run_model_tests, "Model tests", False),
        ("inverter", run_inverter_tests, "Inverter tests", False),
        ("execute", run_execute_tests, "Execute tests", False),
        ("basic_rates", test_basic_rates, "Basic rates tests", False),
        ("window_sort", run_window_sort_tests, "Window sort tests", False),
        ("window2minutes", test_window2minutes, "Window to minutes tests", False),
        ("compute_metric", run_compute_metric_tests, "Compute metric tests", False),
        ("minute_data", test_minute_data, "Minute data tests", False),
        ("minute_data_load", test_minute_data_load, "Minute data load tests", False),
        ("get_now_cumulative", test_get_now_from_cumulative, "Get now from cumulative tests", False),
        ("prune_today", test_prune_today, "Prune today tests", False),
        ("history_attribute", test_history_attribute, "History attribute tests", False),
        ("minute_data_state", test_minute_data_state, "Minute data state tests", False),
        ("format_time_ago", test_format_time_ago, "Format time ago tests", False),
        ("override_time", test_get_override_time_from_string, "Override time from string tests", False),
        ("previous_days_modal", test_previous_days_modal_filter, "Previous days modal filter tests", False),
        # Octopus Energy URL/API tests
        ("octopus_url", test_octopus_url, "Octopus URL/API comprehensive tests (downloads, day/night rates, saving sessions, intelligent dispatch, tariffs, EDF)", False),
        ("octopus_cache", test_octopus_cache_wrapper, "Octopus cache save/load tests", False),
        ("octopus_events", test_octopus_events_wrapper, "Octopus event handler tests", False),
        ("octopus_refresh_token", test_octopus_refresh_token_wrapper, "Octopus refresh token tests", False),
        ("octopus_misc", test_octopus_misc_wrapper, "Octopus misc API tests (set intelligent schedule, join saving sessions)", False),
        ("octopus_read_response", test_octopus_read_response_wrapper, "Octopus read response tests", False),
        ("octopus_rate_limit", test_octopus_rate_limit_wrapper, "Octopus API rate limit tests", False),
        ("octopus_fetch_previous_dispatch", test_octopus_fetch_previous_dispatch_wrapper, "Octopus fetch previous dispatch tests", False),
        ("download_octopus_rates", test_octopus_download_rates_wrapper, "Test download octopus rates", False),
        ("fetch_octopus_rates", test_fetch_octopus_rates, "Fetch Octopus rates tests", False),
        ("fetch_tariffs", test_fetch_tariffs, "Fetch tariffs tests", False),
        ("fetch_url_cached", test_fetch_url_cached, "Fetch URL cached tests", False),
        ("fetch_config_options", test_fetch_config_options, "Fetch config options tests", False),
        ("load_free_slot", test_load_free_slot, "Load free slot tests", False),
        ("add_now_to_octopus_slot", test_add_now_to_octopus_slot, "Add now to Octopus slot tests", False),
        ("plugin_startup", test_plugin_startup_order, "Plugin startup order tests", False),
        ("dynamic_load_car", test_dynamic_load_car_slot_cancellation, "Dynamic load car slot cancellation tests", False),
        ("units", run_test_units, "Unit tests", False),
        ("manual_api", run_test_manual_api, "Manual API tests", False),
        ("manual_soc", run_test_manual_soc, "Manual SOC target tests", False),
        ("manual_times", run_test_manual_times, "Manual times tests", False),
        ("manual_select", run_test_manual_select, "Manual select tests", False),
        ("web_if", run_test_web_if, "Web interface tests", False),
        ("nordpool", run_nordpool_test, "Nordpool tests", False),
        ("octopus_slots", run_load_octopus_slots_tests, "Load Octopus slots tests", False),
        ("rate_add_io_slots", run_rate_add_io_slots_tests, "Rate add IO slots tests", False),
        ("rate_replicate", test_rate_replicate, "Rate replicate comprehensive tests (missing slots, IO, offsets, gas)", False),
        ("find_charge_rate", test_find_charge_rate, "Find charge rate tests", False),
        ("find_charge_rate_string_temp", test_find_charge_rate_string_temperature, "Find charge rate string temperature", False),
        ("find_charge_rate_string_curve", test_find_charge_rate_string_charge_curve, "Find charge rate string charge curve", False),
        ("find_charge_curve", run_find_charge_curve_tests, "Find charge curve tests", False),
        ("find_battery_size", run_find_battery_size_tests, "Find battery size tests", False),
        ("energydataservice", test_energydataservice, "Energy data service tests", False),
        ("saving_session", test_saving_session, "Saving session tests", False),
        ("saving_session_null", test_saving_session_null_octopoints, "Saving session null octopoints test (issue #3079)", False),
        ("saving_session_notify", test_saving_session_notify_config, "Saving session notification config tests", False),
        ("alert_feed", test_alert_feed, "Alert feed tests", False),
        ("fox_api", run_fox_api_tests, "Fox API tests", False),
        ("solcast", run_solcast_tests, "Solcast API tests", False),
        ("solax", run_solax_tests, "SolaX API tests", False),
        ("iboost_smart", run_iboost_smart_tests, "iBoost smart tests", False),
        ("car_charging_smart", run_car_charging_smart_tests, "Car charging smart tests", False),
        ("intersect_window", run_intersect_window_tests, "Intersect window tests", False),
        ("inverter_multi", run_inverter_multi_tests, "Inverter multi tests", False),
        ("octopus_free", test_octopus_free, "Octopus free electricity tests", False),
        ("battery_curve_keys", run_battery_curve_keys_tests, "Battery curve keys tests", False),
        ("balance_inverters", run_balance_inverters_tests, "Balance inverters tests", False),
        # GE Cloud unit tests
        ("ge_cloud", test_ge_cloud, "GE Cloud comprehensive tests (API, devices, EVC, inverter ops, events, publishing, config, downloads, cache)", False),
        ("integer_config", test_integer_config_entities, "Integer config entities tests", False),
        ("expose_config_integer", test_expose_config_preserves_integer, "Expose config preserves integer tests", False),
        # Download tests
        ("download", test_download, "Predbat download/update comprehensive tests (GitHub API, SHA1, install check, file ops)", False),
        # Axle Energy VPP unit tests
        ("axle", test_axle, "Axle Energy VPP comprehensive tests (init, event fetching, error handling, history, sessions)", False),
        # Database Manager unit tests
        ("db_manager", test_db_manager, "DatabaseManager comprehensive tests (state ops, entities/history, error handling, persistence, commit throttling)", False),
        # HAHistory component tests
        ("hahistory", run_hahistory_tests, "HAHistory component tests", False),
        # HAInterface state management tests
        ("hainterface_state", run_hainterface_state_tests, "HAInterface state management tests", False),
        # HAInterface API tests
        ("hainterface_api", run_hainterface_api_tests, "HAInterface API tests", False),
        # HAInterface service tests
        ("hainterface_service", run_hainterface_service_tests, "HAInterface service tests", False),
        # HAInterface lifecycle tests
        ("hainterface_lifecycle", run_hainterface_lifecycle_tests, "HAInterface lifecycle tests", False),
        # HAInterface websocket tests
        ("hainterface_websocket", run_hainterface_websocket_tests, "HAInterface websocket tests", False),
        # Carbon Intensity API unit tests
        ("carbon", test_carbon, "Carbon Intensity API comprehensive tests (fetch, cache, publish, config)", False),
        # Ohme EV charger API unit tests
        ("ohme", test_ohme, "Ohme EV charger comprehensive tests (helper functions, client methods, API operations, event handlers)", False),
        # ComponentBase lifecycle tests
        ("component_base", test_component_base_all, "ComponentBase tests (all)", False),
        # Solis Cloud API unit tests
        ("solis", run_solis_tests, "Solis Cloud API tests (V1/V2 time window writes, change detection)", False),
        ("optimise_levels", run_optimise_levels_tests, "Optimise levels tests", False),
        ("optimise_windows", run_optimise_all_windows_tests, "Optimise all windows tests", True),
        ("debug_cases", run_debug_cases, "Debug case file tests", True),
    ]

    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Predbat unit tests")
    parser.add_argument("--debug_file", action="store", help="Enable debug output")
    parser.add_argument("--full_debug", action="store_true", help="Enable full debug output")
    parser.add_argument("--compare", action="store_true", help="Run compare")
    parser.add_argument("--test", "-t", action="append", help="Run specific test(s) by name (can be used multiple times, use --list to see available tests)")
    parser.add_argument("--keyword", "-k", action="store", help="Run tests matching keyword pattern (e.g., -k carbon_ runs all carbon tests)")
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
        print("       python unit_test.py --test basic_rates --test units  # Multiple tests")
        print("       python unit_test.py -k carbon_  # Run all tests matching 'carbon_'")
        print("       python unit_test.py --quick  # Skip slow tests")
        sys.exit(0)

    print("**** Starting Predbat tests ****")
    my_predbat = create_predbat()
    print("**** Testing Predbat ****")
    failed = False

    if args.debug_file:
        run_single_debug(args.debug_file, my_predbat, args.debug_file, compare=args.compare, debug=args.full_debug)
        sys.exit(0)

    # Collect tests to run based on arguments
    tests_to_run = []

    if args.keyword:
        # Run tests matching keyword pattern
        keyword = args.keyword
        tests_to_run = [(name, func, desc, slow) for name, func, desc, slow in TEST_REGISTRY if keyword in name]
        if not tests_to_run:
            print(f"ERROR: No tests found matching keyword '{keyword}'")
            sys.exit(1)
    elif args.test:
        # Run specific tests by name
        for test_name in args.test:
            test_found = False
            for name, func, desc, slow in TEST_REGISTRY:
                if name == test_name:
                    tests_to_run.append((name, func, desc, slow))
                    test_found = True
                    break
            if not test_found:
                print(f"ERROR: Test '{test_name}' not found. Use --list to see available tests.")
                sys.exit(1)
    else:
        # Run all tests from the registry
        tests_to_run = TEST_REGISTRY

    print(f"**** Running {len(tests_to_run)} test(s) ****")
    # Single loop to run all collected tests
    total_time = 0
    skipped_count = 0
    for name, func, desc, slow in tests_to_run:
        if args.quick and slow:
            print(f"**** Skipping: {name} (slow) ****")
            skipped_count += 1
            continue

        # Show descriptive message for keyword/specific tests, simple for full suite
        print(f"**** Running: {name} - {desc} ****")

        start_time = time.time()
        test_failed = func(my_predbat)
        elapsed = time.time() - start_time
        total_time += elapsed

        if test_failed:
            if args.keyword or args.test:
                print(f"**** ERROR: Test {name} FAILED in {elapsed:.2f}s ****")
            else:
                print(f"**** {name}: FAILED in {elapsed:.2f}s ****")
            failed = True
            break
        else:
            if args.keyword or args.test:
                print(f"**** Test {name} PASSED in {elapsed:.2f}s ****")
            else:
                print(f"**** {name}: PASSED in {elapsed:.2f}s ****")

    # Report results
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
