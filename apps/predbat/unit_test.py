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
from tests.test_saving_session import test_saving_session, test_saving_session_null_octopoints
from tests.test_ge_cloud import (
    run_test_ge_cloud,
    test_async_get_inverter_data_success,
    test_async_get_inverter_data_auth_error,
    test_async_get_inverter_data_rate_limit,
    test_async_get_inverter_data_timeout,
    test_async_get_inverter_data_json_error,
    test_async_get_inverter_data_retry,
    test_async_get_devices_with_ems,
    test_async_get_devices_with_gateway,
    test_async_get_devices_with_batteries,
    test_async_get_devices_empty,
    test_async_get_evc_devices,
    test_async_get_smart_devices,
    test_async_get_evc_commands,
    test_async_get_smart_device,
    test_async_get_evc_sessions,
    test_run_method,
    test_async_automatic_config,
    test_enable_default_options,
    test_async_get_inverter_status,
    test_async_get_inverter_meter,
    test_async_get_device_info,
    test_async_get_inverter_settings_success,
    test_async_get_inverter_settings_partial_failure,
    test_async_read_inverter_setting_success,
    test_async_read_inverter_setting_error_codes,
    test_async_write_inverter_setting_success,
    test_async_write_inverter_setting_failure,
    test_switch_event,
    test_number_event,
    test_select_event,
    test_publish_status,
    test_publish_meter,
    test_publish_info,
    test_publish_registers,
    test_publish_evc_data,
    test_download_ge_data_single_day,
    test_download_ge_data_multi_day,
    test_download_ge_data_pagination,
    test_get_ge_url_cache_hit,
    test_get_ge_url_cache_miss,
    test_clean_ge_url_cache,
    test_load_save_ge_cache,
    test_load_ge_cache_corrupt_file,
    test_regname_to_ha,
    test_get_data,
)
from tests.test_web_if import run_test_web_if
from tests.test_window import run_window_sort_tests, run_intersect_window_tests
from tests.test_find_charge_rate import test_find_charge_rate
from tests.test_manual_api import run_test_manual_api
from tests.test_manual_soc import run_test_manual_soc
from tests.test_manual_times import run_test_manual_times
from tests.test_manual_select import run_test_manual_select
from tests.test_minute_data import test_minute_data
from tests.test_minute_data_state import test_minute_data_state
from tests.test_format_time_ago import test_format_time_ago
from tests.test_override_time import test_get_override_time_from_string
from tests.test_units import run_test_units
from tests.test_previous_days_modal import test_previous_days_modal_filter
from tests.test_octopus_free import test_octopus_free
from tests.test_prune_today import test_prune_today
from tests.test_cumulative import test_get_now_from_cumulative
from tests.test_octopus_url import (
    test_download_octopus_url_wrapper,
    test_async_get_day_night_rates_wrapper,
    test_get_saving_session_data,
    test_async_intelligent_update_sensor_wrapper,
    test_async_find_tariffs_wrapper,
    test_edf_freephase_dynamic_url_wrapper,
)
from tests.test_octopus_cache import test_octopus_cache_wrapper
from tests.test_octopus_events import test_octopus_events_wrapper
from tests.test_octopus_refresh_token import test_octopus_refresh_token_wrapper
from tests.test_octopus_read_response import test_octopus_read_response_wrapper
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
        ("day_night_rates", test_async_get_day_night_rates_wrapper, "Octopus day/night rates tests", False),
        ("saving_sessions", test_get_saving_session_data, "Octopus saving sessions tests", False),
        ("intelligent_dispatch", test_async_intelligent_update_sensor_wrapper, "Octopus intelligent dispatch tests", False),
        ("find_tariffs", test_async_find_tariffs_wrapper, "Octopus find tariffs tests", False),
        ("edf_freephase_dynamic", test_edf_freephase_dynamic_url_wrapper, "EDF FreePhase Dynamic tariff tests", False),
        ("octopus_cache", test_octopus_cache_wrapper, "Octopus cache save/load tests", False),
        ("octopus_events", test_octopus_events_wrapper, "Octopus event handler tests", False),
        ("octopus_refresh_token", test_octopus_refresh_token_wrapper, "Octopus refresh token tests", False),
        ("octopus_read_response", test_octopus_read_response_wrapper, "Octopus read response tests", False),
        ("octopus_fetch_previous_dispatch", test_octopus_fetch_previous_dispatch_wrapper, "Octopus fetch previous dispatch tests", False),
        ("fetch_octopus_rates", test_fetch_octopus_rates, "Fetch Octopus rates tests", False),
        ("fetch_tariffs", test_fetch_tariffs, "Fetch tariffs tests", False),
        ("fetch_url_cached", test_fetch_url_cached, "Fetch URL cached tests", False),
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
        ("find_charge_rate", test_find_charge_rate, "Find charge rate tests", False),
        ("energydataservice", test_energydataservice, "Energy data service tests", False),
        ("saving_session", test_saving_session, "Saving session tests", False),
        ("saving_session_null", test_saving_session_null_octopoints, "Saving session null octopoints test (issue #3079)", False),
        ("alert_feed", test_alert_feed, "Alert feed tests", False),
        ("fox_api", run_fox_api_tests, "Fox API tests", False),
        ("solcast", run_solcast_tests, "Solcast API tests", False),
        ("iboost_smart", run_iboost_smart_tests, "iBoost smart tests", False),
        ("car_charging_smart", run_car_charging_smart_tests, "Car charging smart tests", False),
        ("intersect_window", run_intersect_window_tests, "Intersect window tests", False),
        ("inverter_multi", run_inverter_multi_tests, "Inverter multi tests", False),
        ("octopus_free", test_octopus_free, "Octopus free electricity tests", False),
        ("battery_curve_keys", run_battery_curve_keys_tests, "Battery curve keys tests", False),
        ("balance_inverters", run_balance_inverters_tests, "Balance inverters tests", False),
        # GE Cloud unit tests
        ("ge_api_success", test_async_get_inverter_data_success, "GE Cloud API success", False),
        ("ge_api_auth_error", test_async_get_inverter_data_auth_error, "GE Cloud API auth error", False),
        ("ge_api_rate_limit", test_async_get_inverter_data_rate_limit, "GE Cloud API rate limit", False),
        ("ge_api_timeout", test_async_get_inverter_data_timeout, "GE Cloud API timeout", False),
        ("ge_api_json_error", test_async_get_inverter_data_json_error, "GE Cloud API JSON error", False),
        ("ge_api_retry", test_async_get_inverter_data_retry, "GE Cloud API retry logic", False),
        ("ge_devices_ems", test_async_get_devices_with_ems, "GE Cloud devices with EMS", False),
        ("ge_devices_gateway", test_async_get_devices_with_gateway, "GE Cloud devices with Gateway", False),
        ("ge_devices_batteries", test_async_get_devices_with_batteries, "GE Cloud devices with batteries", False),
        ("ge_devices_empty", test_async_get_devices_empty, "GE Cloud empty devices", False),
        ("ge_evc_devices", test_async_get_evc_devices, "GE Cloud EV charger devices", False),
        ("ge_smart_devices", test_async_get_smart_devices, "GE Cloud smart devices", False),
        ("ge_evc_commands", test_async_get_evc_commands, "GE Cloud EV charger commands", False),
        ("ge_smart_device", test_async_get_smart_device, "GE Cloud smart device", False),
        ("ge_evc_sessions", test_async_get_evc_sessions, "GE Cloud EV charger sessions", False),
        ("ge_run_method", test_run_method, "GE Cloud run method", False),
        ("ge_automatic_config", test_async_automatic_config, "GE Cloud automatic config", False),
        ("ge_enable_defaults", test_enable_default_options, "GE Cloud enable default options", False),
        ("ge_inverter_status", test_async_get_inverter_status, "GE Cloud inverter status", False),
        ("ge_inverter_meter", test_async_get_inverter_meter, "GE Cloud inverter meter", False),
        ("ge_device_info", test_async_get_device_info, "GE Cloud device info", False),
        ("ge_settings_success", test_async_get_inverter_settings_success, "GE Cloud settings fetch", False),
        ("ge_settings_partial", test_async_get_inverter_settings_partial_failure, "GE Cloud settings partial failure", False),
        ("ge_read_setting", test_async_read_inverter_setting_success, "GE Cloud read setting", False),
        ("ge_read_errors", test_async_read_inverter_setting_error_codes, "GE Cloud read error codes", False),
        ("ge_write_success", test_async_write_inverter_setting_success, "GE Cloud write setting", False),
        ("ge_write_failure", test_async_write_inverter_setting_failure, "GE Cloud write failure", False),
        ("ge_switch_event", test_switch_event, "GE Cloud switch event", False),
        ("ge_number_event", test_number_event, "GE Cloud number event", False),
        ("ge_select_event", test_select_event, "GE Cloud select event", False),
        ("ge_publish_status", test_publish_status, "GE Cloud publish status", False),
        ("ge_publish_meter", test_publish_meter, "GE Cloud publish meter", False),
        ("ge_publish_info", test_publish_info, "GE Cloud publish info", False),
        ("ge_publish_registers", test_publish_registers, "GE Cloud publish registers", False),
        ("ge_publish_evc_data", test_publish_evc_data, "GE Cloud publish EVC data", False),
        ("ge_download_single", test_download_ge_data_single_day, "GE Cloud download single day", False),
        ("ge_download_multi", test_download_ge_data_multi_day, "GE Cloud download multi-day", False),
        ("ge_download_pagination", test_download_ge_data_pagination, "GE Cloud download pagination", False),
        ("ge_cache_hit", test_get_ge_url_cache_hit, "GE Cloud cache hit", False),
        ("ge_cache_miss", test_get_ge_url_cache_miss, "GE Cloud cache miss", False),
        ("ge_cache_clean", test_clean_ge_url_cache, "GE Cloud cache cleanup", False),
        ("ge_cache_persist", test_load_save_ge_cache, "GE Cloud cache persistence", False),
        ("ge_cache_corrupt", test_load_ge_cache_corrupt_file, "GE Cloud cache corrupt file", False),
        ("ge_regname_to_ha", test_regname_to_ha, "GE Cloud regname to HA", False),
        ("ge_get_data", test_get_data, "GE Cloud get data", False),
        ("integer_config", test_integer_config_entities, "Integer config entities tests", False),
        ("expose_config_integer", test_expose_config_preserves_integer, "Expose config preserves integer tests", False),
        ("optimise_levels", run_optimise_levels_tests, "Optimise levels tests", True),
        ("optimise_windows", run_optimise_all_windows_tests, "Optimise all windows tests", True),
        ("debug_cases", run_debug_cases, "Debug case file tests", True),
        ("download_octopus_rates", test_octopus_download_rates_wrapper, "Test download octopus rates", False),
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
