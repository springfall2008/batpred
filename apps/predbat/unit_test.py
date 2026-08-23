# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
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

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from predbat import PredBat
from tests.test_infra import TestHAInterface
from tests.test_compute_metric import run_compute_metric_tests
from tests.test_pv90 import run_pv90_tests
from tests.test_performance_tweaks import run_performance_tweaks_tests
from tests.test_perf import run_perf_test
from tests.test_model import run_model_tests
from tests.test_predict_pv_power import run_predict_pv_power_tests
from tests.test_dashboard_device_class import test_dashboard_device_class
from tests.test_inverter_config_sensor import test_inverter_config_sensor
from tests.test_kernel_parity import run_kernel_parity_tests, run_model_kernel_tests
from tests.test_prediction_batch import run_prediction_batch_tests
from tests.test_kernel_static_cache import run_kernel_static_cache_tests
from tests.test_execute import run_execute_tests
from tests.test_execute_multi_inverter_status import test_multi_inverter_status
from tests.test_load_car_energy import test_load_car_energy_warns_when_configured_entity_has_no_data
from tests.test_debug_enable_auto_scope import test_debug_enable_auto_scope
from tests.test_octopus_slots import run_load_octopus_slots_tests
from tests.test_multi_car_iog import run_multi_car_iog_tests
from tests.test_fetch_config_options import test_fetch_config_options
from tests.test_multi_inverter import run_inverter_multi_tests
from tests.test_window2minutes import test_window2minutes
from tests.test_hass_watcher import test_hass_watcher
from tests.test_new_install_detection import test_new_install_detection
from tests.test_history_attribute import test_history_attribute
from tests.test_inverter import run_inverter_tests
from tests.test_basic_rates import test_basic_rates
from tests.test_rate_export_max_forward_calc import test_rate_export_max_forward_calc
from tests.test_rate_min_forward_calc import test_rate_min_forward_calc
from tests.test_find_charge_curve import run_find_charge_curve_tests
from tests.test_find_battery_size import run_find_battery_size_tests
from tests.test_optimise_all_windows import run_optimise_all_windows_kernel_tests
from tests.test_optimise_solar import run_optimise_solar_tests
from tests.test_optimise_swap_charge import run_optimise_swap_charge_tests
from tests.test_optimise_swap_export import run_optimise_swap_export_tests
from tests.test_nordpool import run_nordpool_test
from tests.test_futurerate_auto import test_futurerate_auto
from tests.test_car_charging_smart import run_car_charging_smart_tests
from tests.test_plugin_startup import test_plugin_startup_order
from tests.test_active_flag import test_active_flag
from tests.test_component_health_status import test_component_health_status
from tests.test_optimise_levels import run_optimise_levels_tests
from tests.test_trim_export import run_trim_export_tests
from tests.test_plan_tiebreak import run_plan_tiebreak_tests
from tests.test_plan_preclip import run_plan_preclip_tests
from tests.test_export_commitment import run_export_commitment_tests
from tests.test_optimise_export_copy import run_optimise_export_copy_tests
from tests.test_energydataservice import run_energydataservice_tests
from tests.test_iboost import run_iboost_smart_tests
from tests.test_alert_feed import test_alert_feed
from tests.test_solax import run_solax_tests
from tests.test_sigenergy import run_sigenergy_tests
from tests.test_single_debug import run_single_debug
from tests.test_saving_session import (
    test_saving_session,
    test_saving_session_null_octopoints,
    test_saving_session_notify_config,
    test_saving_session_default_rate,
    test_saving_session_axle_conflict,
    test_saving_session_join_service_fallback,
    test_trigger_callback_success_signal,
    test_saving_session_auto_join_toggle,
    test_saving_session_custom_entity_no_rewrite_match,
    test_saving_session_zero_rate_skip,
    test_saving_session_min_octopoints_threshold,
    test_saving_session_entity_regex_power_rename,
    test_saving_session_select_entity_join_defers_notify,
)
from tests.test_secrets import run_secrets_tests
from tests.test_ge_cloud import test_ge_cloud
from tests.test_teslemetry import test_teslemetry
from tests.test_compare import test_compare
from tests.test_gateway import run_gateway_tests
from tests.test_axle import test_axle
from tests.test_db_manager import test_db_manager
from tests.test_hahistory import run_hahistory_tests
from tests.test_hainterface_state import run_hainterface_state_tests
from tests.test_hainterface_api import run_hainterface_api_tests
from tests.test_hainterface_service import run_hainterface_service_tests
from tests.test_hainterface_lifecycle import run_hainterface_lifecycle_tests
from tests.test_hainterface_websocket import run_hainterface_websocket_tests
from tests.test_history_chunking import run_history_chunking_tests
from tests.test_web_if import run_test_web_if
from tests.test_web_chart_currency import test_rates_chart_series_names_use_currency_symbol
from tests.test_web_debug_history_routes import test_web_debug_history_routes
from tests.test_debug_history_client_js import test_debug_history_client_js
from tests.test_metrics_dashboard_soc_refresh import test_soc_chart_center_text_reads_live_data
from tests.test_web_functions import run_web_functions_tests, run_web_logo_image_tests
from tests.test_web_history_table import run_web_history_table_tests
from tests.test_web_charts import run_web_charts_tests
from tests.test_web_chart_grouping import run_web_chart_grouping_tests
from tests.test_web_entity_unit_resolution import run_web_entity_unit_resolution_tests
from tests.test_web_annual import (
    test_web_annual,
    test_web_annual_error_isolation,
    test_web_annual_fast_mode,
    test_web_annual_form,
    test_web_annual_pages,
    test_web_annual_plan_route,
    test_web_annual_post_numeric_coercion,
    test_web_annual_results,
    test_web_annual_routes,
    test_web_annual_routes_registered,
    test_web_annual_run_refuses_while_running,
    test_web_annual_store_failure_surfaces,
    test_web_annual_terminal_state,
    test_web_annual_validation_error_preserves_input,
)
from tests.test_window import run_window_sort_tests, run_intersect_window_tests, run_clone_windows_tests, run_window_cache_tests
from tests.test_hit_charge_cache import run_hit_charge_cache_tests
from tests.test_window_selection import run_window_selection_tests
from tests.test_find_charge_rate import test_find_charge_rate, test_find_charge_rate_pv_overlap, test_find_charge_rate_string_temperature, test_find_charge_rate_string_charge_curve
from tests.test_manual_api import run_test_manual_api
from tests.test_manual_soc import run_test_manual_soc
from tests.test_manual_times import run_test_manual_times
from tests.test_manual_select import run_test_manual_select
from tests.test_minute_array import test_minute_array
from tests.test_minute_data import test_minute_data, test_minute_data_load, test_minute_data_no_smoothing_backwards, test_minute_data_no_smoothing_forward
from tests.test_minute_data_import_export import test_minute_data_import_export
from tests.test_faq_recorder_config import test_faq_recorder_config
from tests.test_minute_data_state import test_minute_data_state
from tests.test_minute_data_copy import run_minute_data_copy_tests
from tests.test_format_time_ago import test_format_time_ago
from tests.test_str2time import test_str2time
from tests.test_override_time import test_get_override_time_from_string
from tests.test_units import run_test_units
from tests.test_previous_days_modal import test_previous_days_modal_filter
from tests.test_load_forecast_history import test_load_forecast_history
from tests.test_filtered_load_minute import test_filtered_load_minute
from tests.test_fill_load_from_power import run_all_tests as test_fill_load_from_power
from tests.test_fetch_pv_forecast import run_all_tests as test_fetch_pv_forecast
from tests.test_octopus_free import test_octopus_free
from tests.test_prune_today import test_prune_today
from tests.test_cumulative import test_get_now_from_cumulative
from tests.test_octopus_url import test_octopus_url
from tests.test_octopus_cache import test_octopus_cache_wrapper
from tests.test_octopus_events import test_octopus_events_wrapper
from tests.test_octopus_refresh_token import test_octopus_refresh_token_wrapper
from tests.test_octopus_misc import test_octopus_misc_wrapper
from tests.test_octopus_read_response import test_octopus_read_response_wrapper
from tests.test_octopus_read_response_retry import test_octopus_read_response_retry_wrapper
from tests.test_octopus_waf_block import test_octopus_waf_block_wrapper
from tests.test_octopus_catalogue_cache import test_octopus_catalogue_cache_wrapper
from tests.test_octopus_rate_limit import test_octopus_rate_limit_wrapper
from tests.test_octopus_logging import test_octopus_logging_wrapper
from tests.test_octopus_fetch_previous_dispatch import test_octopus_fetch_previous_dispatch_wrapper
from tests.test_octopus_intelligent_devices import test_octopus_intelligent_devices_wrapper
from tests.test_octopus_sensor_due import test_octopus_sensor_due_wrapper
from tests.test_octopus_day_night_rates import test_octopus_day_night_rates_wrapper
from tests.test_fetch_octopus_rates import test_fetch_octopus_rates
from tests.test_fetch_tariffs import test_fetch_tariffs
from tests.test_fetch_url_cached import test_fetch_url_cached
from tests.test_load_free_slot import test_load_free_slot
from tests.test_add_now_to_octopus_slot import test_add_now_to_octopus_slot
from tests.test_octopus_slots_change import test_octopus_slots_change
from tests.test_dynamic_load import test_dynamic_load_car_slot_cancellation, test_dynamic_load_high_load_baseline
from tests.test_fox_api import run_fox_api_tests
from tests.test_deye_const import run_deye_const_tests
from tests.test_deye_config import run_deye_config_tests
from tests.test_deye_api import run_deye_api_tests
from tests.test_deye_oauth import run_deye_oauth_tests
from tests.test_deye_control import run_deye_control_tests
from tests.test_deye_publish import run_deye_publish_tests
from tests.test_deye_storage import run_deye_storage_tests
from tests.test_sunsynk_const import run_sunsynk_const_tests
from tests.test_sunsynk_auth import run_sunsynk_auth_tests
from tests.test_sunsynk_api import run_sunsynk_api_tests
from tests.test_sunsynk_control import run_sunsynk_control_tests
from tests.test_control_ledger import run_control_ledger_tests
from tests.test_sunsynk_publish import run_sunsynk_publish_tests
from tests.test_sunsynk_storage import run_sunsynk_storage_tests
from tests.test_sunsynk_config import run_sunsynk_config_tests
from tests.test_alphaess_const import run_alphaess_const_tests
from tests.test_alphaess_api import run_alphaess_api_tests
from tests.test_alphaess_config import run_alphaess_config_tests
from tests.test_alphaess_publish import run_alphaess_publish_tests
from tests.test_alphaess_control import run_alphaess_control_tests
from tests.test_alphaess_storage import run_alphaess_storage_tests
from tests.test_enphase_api import run_enphase_api_tests
from tests.test_solcast import run_solcast_tests
from tests.test_open_meteo import run_open_meteo_tests
from tests.test_solar_model import test_solar_model
from tests.test_annual_profiles import test_annual_profiles
from tests.test_annual_load import test_annual_load, test_annual_load_octopus
from tests.test_annual_weather import test_annual_weather
from tests.test_annual_tariff import test_annual_tariff
from tests.test_rate_add_io_slots import run_rate_add_io_slots_tests
from tests.test_iog_charge_skew import run_iog_charge_skew_tests
from tests.test_battery_curve_keys import run_battery_curve_keys_tests
from tests.test_balance_inverters import run_balance_inverters_tests
from tests.test_octopus_download_rates import test_octopus_download_rates_wrapper
from tests.test_integer_config import (
    test_integer_config_entities,
    test_expose_config_preserves_integer,
    test_config_item_range_clamp,
    test_config_item_step_min_max_types_consistent,
    test_get_ha_config_normalises_int_default_for_fractional_step,
    test_metric_battery_cycle_fractional_value_not_truncated,
)
from tests.test_predbat_metrics_data_age import test_data_age_metrics_round_trip
from tests.test_metrics_dashboard_control_conflicts import test_control_conflicts_metrics_round_trip, test_control_conflicts_dashboard_renders_section
from tests.test_validate_config import test_validate_config, test_validate_config_retry
from tests.test_get_arg_missing_index import test_get_arg_missing_index_uses_default_quietly
from tests.test_plan_json_rate_adjust import run_test_plan_json_rate_adjust
from tests.test_plan_why_reason import run_test_plan_why_reason
from tests.test_rate_replicate_missing_slots import test_rate_replicate
from tests.test_find_charge_window import test_find_charge_window
from tests.test_random_scenarios import generate_scenarios, save_scenarios, run_scenarios_from_file, compare_results, profile_scenario, run_random_scenario_tests
from tests.test_carbon import test_carbon
from tests.test_storage import test_storage
from tests.test_plan_persistence import test_plan_persistence
from tests.test_github import test_github
from tests.test_download import test_download
from tests.test_ohme import test_ohme
from tests.test_myenergi import test_myenergi
from tests.test_component_base import test_component_base_all
from tests.test_mock_base import test_mock_base_all
from tests.test_solis import run_solis_tests
from tests.test_load_ml import test_load_ml
from tests.test_ml_memory import run_ml_memory_tests
from tests.test_ml_training_perf import run_ml_training_perf_tests
from tests.test_temperature import test_temperature
from tests.test_oauth_mixin import run_oauth_mixin_tests
from tests.test_fox_oauth import run_fox_oauth_tests
from tests.test_band_rate_text import test_band_rate_text
from tests.test_kraken import run_kraken_tests
from tests.test_kraken_auth_mixin import run_kraken_auth_mixin_tests
from tests.test_clip_export_slots import run_clip_export_slots_tests
from tests.test_prune_dead_slots import run_prune_dead_slots_tests
from tests.test_clip_charge_slots import run_clip_charge_slots_tests
from tests.test_discard_unused_charge_slots import run_discard_unused_charge_slots_tests
from tests.test_discard_unused_export_slots import run_discard_unused_export_slots_tests
from tests.test_marginal_costs import test_marginal_costs
from tests.test_savings_stability import test_savings_stability
from tests.test_calculate_yesterday import test_calculate_yesterday
from tests.test_load_today_comparison import test_load_today_comparison
from tests.test_annual_config import test_annual_config
from tests.test_annual_bootstrap import test_annual_bootstrap
from tests.test_annual_sampling import test_annual_sampling
from tests.test_annual_interpolate import test_annual_fast_mode_assembly, test_annual_interpolate
from tests.test_annual_curve_reference import test_annual_curve_reference
from tests.test_annual_scenarios import test_annual_scenarios
from tests.test_annual_results import test_annual_results
from tests.test_annual_integration import test_annual_integration
from tests.test_annual_cli import test_annual_cli, test_annual_cli_fast_flag, test_annual_cli_machine, test_annual_cli_machine_end_to_end
from tests.test_annual_job import test_annual_job
from tests.test_tariff_catalogue import test_tariff_catalogue
from tests.test_annual_store import test_annual_store
from tests.test_annual_costs import test_annual_costs
from tests.test_debug_history import test_debug_history
from tests.test_debug_history_capture import test_debug_history_capture, test_debug_history_capture_slot_alignment

# Mock the components and plugin system

KEEP_SCALE = 0.5


def run_debug_cases(my_predbat):
    """Run debug case files from the cases directory.

    my_predbat is deliberately unused: each case gets a freshly created instance instead. read_debug_yaml
    only restores the attributes its dump actually carries, so on a shared instance anything the dump omits
    inherits whatever the previous test happened to leave behind - which made these cases depend on test
    ordering, and made the plan produced here differ from the one `--debug <case>` produces standalone.
    Neither is a property a golden regression test can afford. Building the instance the same way the
    standalone path does makes the two agree and makes the result independent of what ran before.
    """
    failed = False
    print("**** Running debug case files ****")

    total_calculate_plan_time = 0.0
    case_count = 0

    # Scan .yaml files in cases directory
    for filename in glob.glob("cases/*.yaml"):
        basename = os.path.basename(filename)
        pathname = os.path.dirname(filename)
        if basename == "random_scenarios.yaml":
            continue  # Skip the random scenarios template file
        case_predbat = create_predbat()
        test_failed = run_single_debug(basename, case_predbat, filename, pathname + "/" + basename + ".expected.json")
        total_calculate_plan_time += getattr(case_predbat, "last_calculate_plan_time", 0.0)
        case_count += 1
        if test_failed:
            print(f"**** Debug case {basename}: FAILED ****")
            failed = True
            break
        else:
            print(f"**** Debug case {basename}: PASSED ****")

    if case_count:
        print("**** Debug cases calculate_plan total time: {} seconds across {} case(s), average {} seconds ****".format(round(total_calculate_plan_time, 3), case_count, round(total_calculate_plan_time / case_count, 3)))

    return failed


def run_annual_integration_isolated(my_predbat):
    """Run the annual integration test against a freshly created instance.

    my_predbat is unused, for the same reason run_debug_cases ignores it: this test plans a year of
    sampled days against whatever state the shared instance is carrying, and never sets that state up
    itself. It used to be shielded by debug_cases running immediately before it and overwriting most of
    the instance from a debug dump; once debug_cases stopped mutating the shared instance, the ambient
    state it inherited instead made it 13x slower (34s -> 463s) without ever failing, which is exactly
    the kind of coupling a test suite should not have.
    """
    return test_annual_integration(create_predbat())


def run_window_cache_tests_isolated(my_predbat):
    """Run the window bounds cache tests against a freshly created instance.

    my_predbat is unused, for the same reason run_annual_integration_isolated ignores it: the last
    test in the group replays a full calculate_plan with the cache validator on, and the shared
    instance carries whatever planning state earlier tests left behind - enough that calculate_plan
    raises on it before the cache is ever exercised. A fresh instance is loaded from a debug dump
    inside the test so the replay is deterministic wherever it runs in the suite.
    """
    return run_window_cache_tests(create_predbat())


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
        ("predict_pv_power", run_predict_pv_power_tests, "predict_pv_power plan-interval scaling tests", False),
        ("dashboard_device_class", test_dashboard_device_class, "Dashboard sensor device_class regression tests (#3352)", False),
        ("inverter_config_sensor", test_inverter_config_sensor, "Aggregated static prediction inputs published as sensor.<prefix>_inverter_config", False),
        ("model_kernel", run_model_kernel_tests, "Model tests run with the C++ prediction kernel enabled", False),
        ("kernel_parity", run_kernel_parity_tests, "C++ prediction kernel vs Python engine parity tests", False),
        ("prediction_batch", run_prediction_batch_tests, "Batched prediction fan-out tests", False),
        ("inverter", run_inverter_tests, "Inverter tests", False),
        ("execute", run_execute_tests, "Execute tests", False),
        ("multi_inverter_status", test_multi_inverter_status, "Multi-inverter headline status resolution tests (#4446)", False),
        ("load_car_energy", test_load_car_energy_warns_when_configured_entity_has_no_data, "car_charging_energy configured-but-empty warning tests (#4458 follow-up)", False),
        ("debug_enable_auto_scope", test_debug_enable_auto_scope, "debug_enable auto-disable-after-N-hours tests (#4438 review)", False),
        ("basic_rates", test_basic_rates, "Basic rates tests", False),
        ("rate_min_forward_calc", test_rate_min_forward_calc, "Rate min forward calc tests", False),
        ("rate_export_max_forward_calc", test_rate_export_max_forward_calc, "Rate export max forward calc tests", False),
        ("window_sort", run_window_sort_tests, "Window sort tests", False),
        ("window2minutes", test_window2minutes, "Window to minutes tests", False),
        ("hass_watcher", test_hass_watcher, "Standalone-mode file watcher tests (#4397/#4396)", False),
        ("new_install_detection", test_new_install_detection, "New-install misdetection tests (Bug B, #4397/#4396, #3259, #3306)", False),
        ("compute_metric", run_compute_metric_tests, "Compute metric tests", False),
        ("pv90", run_pv90_tests, "pv90 upside scenario tests", False),
        ("performance_tweaks", run_performance_tweaks_tests, "performance_tweaks toggle tests", False),
        ("minute_array", test_minute_array, "MinuteArray class tests", False),
        ("minute_data", test_minute_data, "Minute data tests", False),
        ("minute_data_load", test_minute_data_load, "Minute data load tests", False),
        ("minute_data_import_export", test_minute_data_import_export, "Minute data import/export tests", False),
        ("faq_recorder_config", test_faq_recorder_config, "FAQ recorder filter example matches the entities Predbat reads history for", False),
        ("minute_data_no_smoothing_backwards", test_minute_data_no_smoothing_backwards, "Minute data no-smoothing backwards tests", False),
        ("minute_data_no_smoothing_forward", test_minute_data_no_smoothing_forward, "Minute data no-smoothing forward tests", False),
        ("get_now_cumulative", test_get_now_from_cumulative, "Get now from cumulative tests", False),
        ("prune_today", test_prune_today, "Prune today tests", False),
        ("history_attribute", test_history_attribute, "History attribute tests", False),
        ("minute_data_state", test_minute_data_state, "Minute data state tests", False),
        ("minute_data_copy", run_minute_data_copy_tests, "Minute data history copying tests", False),
        ("format_time_ago", test_format_time_ago, "Format time ago tests", False),
        ("str2time", test_str2time, "Time string parsing tests", False),
        ("override_time", test_get_override_time_from_string, "Override time from string tests", False),
        ("previous_days_modal", test_previous_days_modal_filter, "Previous days modal filter tests", False),
        ("load_forecast_history", test_load_forecast_history, "Weighted historical load forecast tests", False),
        ("filtered_load_minute", test_filtered_load_minute, "Filtered load minute / window tests", False),
        ("fill_load_from_power", test_fill_load_from_power, "Fill load from power sensor tests", False),
        ("fetch_pv_forecast", test_fetch_pv_forecast, "Fetch PV forecast with relative_time offset tests", False),
        # Octopus Energy URL/API tests
        ("octopus_url", test_octopus_url, "Octopus URL/API comprehensive tests (downloads, day/night rates, saving sessions, intelligent dispatch, tariffs, EDF)", False),
        ("octopus_cache", test_octopus_cache_wrapper, "Octopus cache save/load tests", False),
        ("octopus_events", test_octopus_events_wrapper, "Octopus event handler tests", False),
        ("octopus_refresh_token", test_octopus_refresh_token_wrapper, "Octopus refresh token tests", False),
        ("octopus_misc", test_octopus_misc_wrapper, "Octopus misc API tests (set intelligent schedule, join saving sessions)", False),
        ("octopus_read_response", test_octopus_read_response_wrapper, "Octopus read response tests", False),
        ("octopus_read_response_retry", test_octopus_read_response_retry_wrapper, "Octopus read response retry with exponential backoff tests", False),
        ("octopus_waf_block", test_octopus_waf_block_wrapper, "Octopus CloudFront/WAF 403 handling tests", False),
        ("octopus_catalogue_cache", test_octopus_catalogue_cache_wrapper, "Octopus EV catalogue caching tests", False),
        ("octopus_rate_limit", test_octopus_rate_limit_wrapper, "Octopus API rate limit tests", False),
        ("octopus_logging", test_octopus_logging_wrapper, "Octopus GraphQL logging redaction tests", False),
        ("octopus_fetch_previous_dispatch", test_octopus_fetch_previous_dispatch_wrapper, "Octopus fetch previous dispatch tests", False),
        ("octopus_intelligent_devices", test_octopus_intelligent_devices_wrapper, "Octopus intelligent devices tests (flexPlannedDispatches, energyAddedKwh)", False),
        ("octopus_sensor_due", test_octopus_sensor_due_wrapper, "Octopus intelligent sensor 2-minute update scheduling tests", False),
        ("octopus_day_night_rates", test_octopus_day_night_rates_wrapper, "Octopus day/night rate window selection tests (IOG TOU, GO, Economy 7)", False),
        ("download_octopus_rates", test_octopus_download_rates_wrapper, "Test download octopus rates", False),
        ("fetch_octopus_rates", test_fetch_octopus_rates, "Fetch Octopus rates tests", False),
        ("fetch_tariffs", test_fetch_tariffs, "Fetch tariffs tests", False),
        ("fetch_url_cached", test_fetch_url_cached, "Fetch URL cached tests", False),
        ("fetch_config_options", test_fetch_config_options, "Fetch config options tests", False),
        ("load_free_slot", test_load_free_slot, "Load free slot tests", False),
        ("add_now_to_octopus_slot", test_add_now_to_octopus_slot, "Add now to Octopus slot tests", False),
        ("octopus_slots_change", test_octopus_slots_change, "Octopus slots change-detection signature tests (in-progress re-clock vs genuine change)", False),
        ("plugin_startup", test_plugin_startup_order, "Plugin startup order tests", False),
        ("active_flag", test_active_flag, "Active flag cleared on exception tests", False),
        ("component_health_status", test_component_health_status, "Component errors fail the recorded run status tests", False),
        ("dynamic_load_car", test_dynamic_load_car_slot_cancellation, "Dynamic load car slot cancellation tests", False),
        ("dynamic_load_high", test_dynamic_load_high_load_baseline, "Dynamic load high-load baseline tests", False),
        ("units", run_test_units, "Unit tests", False),
        ("manual_api", run_test_manual_api, "Manual API tests", False),
        ("manual_soc", run_test_manual_soc, "Manual SOC target tests", False),
        ("manual_times", run_test_manual_times, "Manual times tests", False),
        ("manual_select", run_test_manual_select, "Manual select tests", False),
        ("web_if", run_test_web_if, "Web interface tests", False),
        ("web_chart_currency", test_rates_chart_series_names_use_currency_symbol, "Rates chart series names follow currency_symbols tests", False),
        ("web_debug_history_routes", test_web_debug_history_routes, "Debug-history web routes tests (#4438 review items 4, 6, 21)", False),
        ("debug_history_client_js", test_debug_history_client_js, "Debug-history client-side JS structure tests (#4438 review item 22)", False),
        ("metrics_dashboard_soc_refresh", test_soc_chart_center_text_reads_live_data, "Metrics dashboard SoC chart live-refresh tests", False),
        ("web_functions", run_web_functions_tests, "Web function unit tests", False),
        ("web_logo_image", run_web_logo_image_tests, "Local logo image route tests (issue #4562)", False),
        ("web_annual", test_web_annual, "Annual web tab prefill tests", False),
        ("web_annual_form", test_web_annual_form, "Annual web tab form tests", False),
        ("web_annual_fast_mode", test_web_annual_fast_mode, "Annual web tab fast mode tests", False),
        ("web_annual_routes", test_web_annual_routes, "Annual web tab route tests", False),
        ("web_annual_results", test_web_annual_results, "Annual web tab results tests", False),
        ("web_annual_terminal_state", test_web_annual_terminal_state, "Annual web tab terminal-state claim/no-redirect-loop tests", False),
        ("web_annual_error_isolation", test_web_annual_error_isolation, "Annual web tab per-request error isolation tests", False),
        ("web_annual_routes_registered", test_web_annual_routes_registered, "Annual web tab route registration test", False),
        ("web_annual_validation_error_preserves_input", test_web_annual_validation_error_preserves_input, "Annual web tab validation error keeps posted form input tests", False),
        ("web_annual_run_refuses_while_running", test_web_annual_run_refuses_while_running, "Annual web tab second-run refusal tests", False),
        ("web_annual_store_failure_surfaces", test_web_annual_store_failure_surfaces, "Annual web tab storage-failure visibility tests", False),
        ("web_annual_post_numeric_coercion", test_web_annual_post_numeric_coercion, "Annual web tab posted-form numeric coercion tests", False),
        ("web_annual_plan_route", test_web_annual_plan_route, "Annual web tab captured-plan route tests", False),
        ("web_annual_pages", test_web_annual_pages, "Annual web tab config/viewer/compare page split and nav tests", False),
        ("web_history_table", run_web_history_table_tests, "Web /entity history table bucketing tests", False),
        ("web_charts", run_web_charts_tests, "Web chart rendering tests (percent/special-character units)", False),
        ("web_chart_grouping", run_web_chart_grouping_tests, "Web /entity chart numeric vs timeline grouping tests", False),
        ("web_entity_unit_resolution", run_web_entity_unit_resolution_tests, "Web /entity chart unit/name resolution tests", False),
        ("nordpool", run_nordpool_test, "Nordpool tests", False),
        ("futurerate_auto", test_futurerate_auto, "FutureRate auto Agile detection tests", False),
        ("octopus_slots", run_load_octopus_slots_tests, "Load Octopus slots tests", False),
        ("multi_car_iog", run_multi_car_iog_tests, "Multi-car IOG tests", False),
        ("rate_add_io_slots", run_rate_add_io_slots_tests, "Rate add IO slots tests", False),
        ("iog_charge_skew", run_iog_charge_skew_tests, "IOG earlier-charge skew characterisation tests", False),
        ("rate_replicate", test_rate_replicate, "Rate replicate comprehensive tests (missing slots, IO, offsets, gas)", False),
        ("find_charge_window", test_find_charge_window, "Find charge window gap handling tests", False),
        ("find_charge_rate", test_find_charge_rate, "Find charge rate tests", False),
        ("find_charge_rate_pv", test_find_charge_rate_pv_overlap, "Find charge rate with PV overlap", False),
        ("find_charge_rate_string_temp", test_find_charge_rate_string_temperature, "Find charge rate string temperature", False),
        ("find_charge_rate_string_curve", test_find_charge_rate_string_charge_curve, "Find charge rate string charge curve", False),
        ("find_charge_curve", run_find_charge_curve_tests, "Find charge curve tests", False),
        ("find_battery_size", run_find_battery_size_tests, "Find battery size tests", False),
        ("energydataservice", run_energydataservice_tests, "Energy data service tests", False),
        ("saving_session", test_saving_session, "Saving session tests", False),
        ("saving_session_null", test_saving_session_null_octopoints, "Saving session null octopoints test (issue #3079)", False),
        ("saving_session_notify", test_saving_session_notify_config, "Saving session notification config tests", False),
        ("saving_session_default_rate", test_saving_session_default_rate, "Saving session default rate injection test", False),
        ("saving_session_axle_conflict", test_saving_session_axle_conflict, "Saving session Axle conflict avoidance test (issue #4120)", False),
        ("saving_session_join_service_fallback", test_saving_session_join_service_fallback, "Saving session join service fallback test (issue #4548 point 3)", False),
        ("trigger_callback_success_signal", test_trigger_callback_success_signal, "trigger_callback loopback success signal test (PR #4601 review)", False),
        ("saving_session_auto_join_toggle", test_saving_session_auto_join_toggle, "Saving session auto-join toggle test (issue #4120)", False),
        ("saving_session_custom_entity_no_rewrite_match", test_saving_session_custom_entity_no_rewrite_match, "Saving session custom entity no rewrite match test (issue #4573)", False),
        ("saving_session_zero_rate_skip", test_saving_session_zero_rate_skip, "Saving session zero reward rate skip test (issue #4593)", False),
        ("saving_session_min_octopoints_threshold", test_saving_session_min_octopoints_threshold, "Saving session configurable minimum octopoints threshold test (issue #4595)", False),
        ("saving_session_entity_regex_power_rename", test_saving_session_entity_regex_power_rename, "Saving/free session entity regex Power Down/Up rename test (issue #4548 point 2)", False),
        ("saving_session_select_entity_join_defers_notify", test_saving_session_select_entity_join_defers_notify, "Select-entity join defers the joined notification test (issue #4593)", False),
        ("alert_feed", test_alert_feed, "Alert feed tests", False),
        ("fox_api", run_fox_api_tests, "Fox API tests", False),
        ("deye_const", run_deye_const_tests, "DEYE constants tests", False),
        ("deye_config", run_deye_config_tests, "DEYE config/INVERTER_DEF tests", False),
        ("deye_api", run_deye_api_tests, "DEYE API tests", False),
        ("deye_oauth", run_deye_oauth_tests, "DEYE auth tests", False),
        ("deye_control", run_deye_control_tests, "DEYE control-logic tests", False),
        ("deye_publish", run_deye_publish_tests, "DEYE publish/config tests", False),
        ("deye_storage", run_deye_storage_tests, "DEYE storage persistence tests", False),
        ("sunsynk_const", run_sunsynk_const_tests, "Sunsynk constants tests", False),
        ("sunsynk_auth", run_sunsynk_auth_tests, "Sunsynk auth tests", False),
        ("sunsynk_api", run_sunsynk_api_tests, "Sunsynk API tests", False),
        ("sunsynk_control", run_sunsynk_control_tests, "Sunsynk control-logic tests", False),
        ("control_ledger", run_control_ledger_tests, "Control ownership ledger tests", False),
        ("sunsynk_publish", run_sunsynk_publish_tests, "Sunsynk publish tests", False),
        ("sunsynk_storage", run_sunsynk_storage_tests, "Sunsynk storage tests", False),
        ("sunsynk_config", run_sunsynk_config_tests, "Sunsynk config/INVERTER_DEF tests", False),
        ("alphaess_const", run_alphaess_const_tests, "AlphaESS constants tests", False),
        ("alphaess_api", run_alphaess_api_tests, "AlphaESS API tests", False),
        ("alphaess_config", run_alphaess_config_tests, "AlphaESS config/INVERTER_DEF tests", False),
        ("alphaess_publish", run_alphaess_publish_tests, "AlphaESS publish/config tests", False),
        ("alphaess_control", run_alphaess_control_tests, "AlphaESS control-logic tests", False),
        ("alphaess_storage", run_alphaess_storage_tests, "AlphaESS storage tests", False),
        ("enphase_api", run_enphase_api_tests, "Enphase API tests", False),
        ("solcast", run_solcast_tests, "Solcast API tests", False),
        ("open_meteo", run_open_meteo_tests, "Open-Meteo solar forecast provider tests", False),
        ("solar_model", test_solar_model, "Shared solar GTI conversion model tests", False),
        ("annual_profiles", test_annual_profiles, "Annual prediction load profile table tests", False),
        ("annual_load", test_annual_load, "Annual prediction load profile tests", False),
        ("annual_load_octopus", test_annual_load_octopus, "Annual prediction Octopus consumption tests", False),
        ("annual_weather", test_annual_weather, "Annual prediction Open-Meteo weather tests", False),
        ("annual_tariff", test_annual_tariff, "Annual prediction tariff tests", False),
        ("solax", run_solax_tests, "SolaX API tests", False),
        ("sigenergy", run_sigenergy_tests, "Sigenergy Cloud API tests", False),
        ("iboost_smart", run_iboost_smart_tests, "iBoost smart tests", False),
        ("car_charging_smart", run_car_charging_smart_tests, "Car charging smart tests", False),
        ("intersect_window", run_intersect_window_tests, "Intersect window tests", False),
        ("clone_windows", run_clone_windows_tests, "Clone windows tests", False),
        ("window_cache", run_window_cache_tests_isolated, "Window bounds cache tests", False),
        ("hit_charge_cache", run_hit_charge_cache_tests, "Hit charge window cache tests", False),
        ("window_selection", run_window_selection_tests, "Window selection picker tests", False),
        ("kernel_static_cache", run_kernel_static_cache_tests, "Kernel static context cache tests", False),
        ("optimise_export_copy", run_optimise_export_copy_tests, "Optimise export window copying tests", False),
        ("inverter_multi", run_inverter_multi_tests, "Inverter multi tests", False),
        ("octopus_free", test_octopus_free, "Octopus free electricity tests", False),
        ("battery_curve_keys", run_battery_curve_keys_tests, "Battery curve keys tests", False),
        ("balance_inverters", run_balance_inverters_tests, "Balance inverters tests", False),
        # GE Cloud unit tests
        ("ge_cloud", test_ge_cloud, "GE Cloud comprehensive tests (API, devices, EVC, inverter ops, events, publishing, config, downloads, cache)", False),
        ("teslemetry", test_teslemetry, "Teslemetry Tesla Powerwall component tests (data path, control, tariff)", False),
        ("integer_config", test_integer_config_entities, "Integer config entities tests", False),
        ("validate_config", test_validate_config, "APPS_SCHEMA validator tests (string types, sensor boolean states)", False),
        ("get_arg_missing_index", test_get_arg_missing_index_uses_default_quietly, "get_arg missing (out-of-range index) vs malformed numeric coercion tests", False),
        ("validate_config_retry", test_validate_config_retry, "Config validation retry-after-failure tests (#4379)", False),
        ("expose_config_integer", test_expose_config_preserves_integer, "Expose config preserves integer tests", False),
        ("config_item_range_clamp", test_config_item_range_clamp, "Config item min/max range clamp tests", False),
        ("config_item_step_min_max_types", test_config_item_step_min_max_types_consistent, "Config item step/min/max type consistency tests", False),
        ("get_ha_config_fractional_default", test_get_ha_config_normalises_int_default_for_fractional_step, "get_ha_config normalises int default to float for fractional-step items (#4296)", False),
        ("metric_battery_cycle_fractional", test_metric_battery_cycle_fractional_value_not_truncated, "metric_battery_cycle fractional value not truncated by get_arg (#4296)", False),
        ("data_age_metrics", test_data_age_metrics_round_trip, "Metrics dashboard data_age_days/data_age_required_days tests", False),
        ("control_conflicts_metrics", test_control_conflicts_metrics_round_trip, "Metrics dashboard control_conflicts round-trip tests", False),
        ("control_conflicts_dashboard", test_control_conflicts_dashboard_renders_section, "Metrics dashboard control_conflicts section render tests", False),
        ("plan_json_rate_adjust", run_test_plan_json_rate_adjust, "Plan JSON rate adjust type field tests", False),
        ("plan_why_reason", run_test_plan_why_reason, "Plan JSON per-slot 'why' reason text tests", False),
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
        # History chunking (long windows fetched in pieces) tests
        ("history_chunking", run_history_chunking_tests, "History chunking tests", False),
        # Carbon Intensity API unit tests
        ("carbon", test_carbon, "Carbon Intensity API comprehensive tests (fetch, cache, publish, config)", False),
        # Storage component unit tests
        ("storage", test_storage, "Storage component tests (yaml/json/text round-trip, expiry, cleanup)", False),
        ("plan_persistence", test_plan_persistence, "Plan persistence tests (save/load round-trip, expiry, missing storage)", False),
        ("github", test_github, "GitHub mixin tests (cache hit/miss/stale, HTTP errors, release parsing, auto-update)", False),
        # Ohme EV charger API unit tests
        ("ohme", test_ohme, "Ohme EV charger comprehensive tests (helper functions, client methods, API operations, event handlers)", False),
        # myenergi Zappi and Eddi unit tests
        ("myenergi", test_myenergi, "myenergi Zappi and Eddi comprehensive tests (normalisation, transports, publishing, auto-config, controls)", False),
        # ComponentBase lifecycle tests
        ("component_base", test_component_base_all, "ComponentBase tests (all)", False),
        # Shared MockBase tests
        ("mock_base", test_mock_base_all, "Shared CLI-harness MockBase tests", False),
        # Solis Cloud API unit tests
        ("solis", run_solis_tests, "Solis Cloud API tests (V1/V2 time window writes, change detection)", False),
        # External Temperature API tests
        ("temperature", test_temperature, "External Temperature API tests (initialization, zone.home fallback, timezone conversion, caching)", False),
        ("band_rate_text", test_band_rate_text, "Band rate text tests (flat rate, Cosy, Flux import/export)", False),
        # OAuth infrastructure tests
        ("oauth_mixin", run_oauth_mixin_tests, "OAuth mixin tests (token refresh, expiry, 401 handling, env var fallback)", False),
        ("fox_oauth", run_fox_oauth_tests, "Fox API OAuth tests (dual auth headers, 401 retry, initialize params)", False),
        # Kraken Energy (EDF/E.ON) tests
        ("kraken", run_kraken_tests, "Kraken API tests (init, GraphQL, tariff discovery, rate fetching, run lifecycle)", False),
        ("kraken_auth", run_kraken_auth_mixin_tests, "Kraken auth mixin tests (API key, email, refresh, 401 handling)", False),
        ("clip_export_slots", run_clip_export_slots_tests, "Clip export slots tests", False),
        ("prune_dead_slots", run_prune_dead_slots_tests, "Prune dead plan slots tests", False),
        ("clip_charge_slots", run_clip_charge_slots_tests, "Clip charge slots tests", False),
        ("discard_unused_charge_slots", run_discard_unused_charge_slots_tests, "Discard unused charge slots tests", False),
        ("discard_unused_export_slots", run_discard_unused_export_slots_tests, "Discard unused export slots tests", False),
        ("marginal_costs", test_marginal_costs, "Marginal energy cost matrix tests", False),
        ("savings_stability", test_savings_stability, "Savings yesterday rate_low stability tests", False),
        ("calculate_yesterday", test_calculate_yesterday, "Calculate yesterday savings and IOG car-slot subtraction tests", False),
        ("load_today_comparison", test_load_today_comparison, "load_today_comparison None-guard regression test", False),
        ("compare", test_compare, "Compare tariff engine tests (hardware overrides, bleed isolation)", False),
        ("gateway", run_gateway_tests, "GatewayMQTT component tests (protobuf, plan serialization, commands, telemetry)", False),
        ("optimise_levels", run_optimise_levels_tests, "Optimise levels tests", False),
        ("trim_export", run_trim_export_tests, "Export trim ordering (buffer from cheapest slot) tests", False),
        ("plan_tiebreak", run_plan_tiebreak_tests, "Plan fragmentation near-tie tie-break tests", False),
        ("plan_preclip", run_plan_preclip_tests, "Plan selection scores the pre-clip plan", True),
        ("export_commitment", run_export_commitment_tests, "Forced-export commitment / anti-flapping tests", False),
        ("optimise_solar", run_optimise_solar_tests, "Optimise export more solar tests", False),
        ("optimise_windows_kernel", run_optimise_all_windows_kernel_tests, "Optimise all windows tests with the C++ kernel", False),
        ("optimise_swap_charge", run_optimise_swap_charge_tests, "Optimise pairwise charge-window swap tests", False),
        ("optimise_swap_export", run_optimise_swap_export_tests, "Optimise pairwise export-window swap tests", False),
        ("debug_cases", run_debug_cases, "Debug case file tests", False),
        ("annual_config", test_annual_config, "Annual prediction config validation tests", False),
        ("annual_bootstrap", test_annual_bootstrap, "Annual prediction bootstrap and state reset tests", False),
        ("annual_sampling", test_annual_sampling, "Annual prediction sample selection tests", False),
        ("annual_scenarios", test_annual_scenarios, "Annual prediction scenario helper tests", False),
        ("annual_results", test_annual_results, "Annual prediction results assembly tests", False),
        ("annual_cli", test_annual_cli, "Annual prediction CLI output tests", False),
        ("annual_cli_fast_flag", test_annual_cli_fast_flag, "Annual CLI --fast flag tests", False),
        ("annual_interpolate", test_annual_interpolate, "Annual fast-mode interpolation curve tests", False),
        ("annual_fast_mode_assembly", test_annual_fast_mode_assembly, "Annual fast-mode assembly tests", False),
        ("annual_curve_reference", test_annual_curve_reference, "Annual fast-mode curve reference scoring", False),
        ("annual_cli_machine", test_annual_cli_machine, "Annual CLI machine mode tests", False),
        ("annual_cli_machine_end_to_end", test_annual_cli_machine_end_to_end, "Annual CLI machine mode end-to-end tests", False),
        ("annual_job", test_annual_job, "Annual subprocess job control tests", False),
        ("annual_store", test_annual_store, "Annual run store tests", False),
        ("annual_costs", test_annual_costs, "Annual install cost and payback model tests", False),
        ("tariff_catalogue", test_tariff_catalogue, "Tariff catalogue tests", False),
        ("annual_integration", run_annual_integration_isolated, "Annual prediction integration tests", True),
        ("load_ml", test_load_ml, "ML Load Forecaster tests (MLP, training, persistence, validation)", True),
        # ML training memory: dataset construction, normalisation dtype and statistics accuracy
        ("ml_memory", run_ml_memory_tests, "ML training memory tests", False),
        # Production-scale ML training harness against a captured history fixture
        ("ml_training_perf", run_ml_training_perf_tests, "ML training performance tests", True),
        ("random", run_random_scenario_tests, "Random scenario plan regression against the committed baseline", False),
        ("debug_history", test_debug_history, "Rolling debug-history snapshot buffer tests", False),
        ("debug_history_capture", test_debug_history_capture, "Debug history capture throttle/force-capture tests", False),
        ("debug_history_capture_alignment", test_debug_history_capture_slot_alignment, "Debug history capture timestamp is floored to the plan slot grid", False),
    ]

    # Parse command line arguments
    parser = argparse.ArgumentParser(description="Predbat unit tests")
    parser.add_argument("--debug_file", action="store", help="Enable debug output")
    parser.add_argument("--full_debug", action="store_true", help="Enable full debug output")
    parser.add_argument("--redo", action="store_true", help="Redo rates, load model and octopus slots for debug test")
    parser.add_argument("--compare", action="store_true", help="Run compare")
    parser.add_argument("--test", "-t", action="append", help="Run specific test(s) by name (can be used multiple times, use --list to see available tests)")
    parser.add_argument("--keyword", "-k", action="store", help="Run tests matching keyword pattern (e.g., -k carbon_ runs all carbon tests)")
    parser.add_argument("--list", "-l", action="store_true", help="List all available tests")
    parser.add_argument("--quick", "-q", action="store_true", help="Skip slow tests (optimise_levels, optimise_windows, debug_cases)")
    parser.add_argument("--random-generate", action="store_true", help="Generate random benchmark scenarios and write to a YAML file")
    parser.add_argument("--random-count", type=int, default=100, metavar="N", help="Number of random scenarios to generate (default: 100)")
    parser.add_argument("--random-seed", type=int, default=0, metavar="N", help="Starting random seed (default: 0)")
    parser.add_argument("--random-output", default="random_scenarios.yaml", metavar="PATH", help="Output YAML file for generated scenarios (default: random_scenarios.yaml)")
    parser.add_argument("--random-run", action="store_true", help="Run all scenarios from a scenarios YAML file and save results to JSON")
    parser.add_argument("--random-scenarios", default="random_scenarios.yaml", metavar="PATH", help="Scenarios YAML file to load for --random-run (default: random_scenarios.yaml)")
    parser.add_argument("--random-scenario", type=int, default=None, metavar="N", help="Run only scenario with this id number (default: run all)")
    parser.add_argument("--random-template", metavar="PATH", help="Template debug YAML file to use as baseline for --random-run (required)")
    parser.add_argument("--random-results", default="random_results.json", metavar="PATH", help="Output JSON file for benchmark results (default: random_results.json)")
    parser.add_argument("--random-compare", nargs=2, metavar=("FILE_A", "FILE_B"), help="Compare two random_results JSON files and print a diff table")
    parser.add_argument("--random-profile", action="store_true", help="Run cProfile on a single scenario's optimisation")
    parser.add_argument("--random-profile-lines", type=int, default=30, metavar="N", help="Number of top functions to show in profile output (default: 30)")
    parser.add_argument("--random-profile-sort", default="cumulative", metavar="KEY", help="pstats sort key: cumulative, tottime, calls (default: cumulative)")
    parser.add_argument("--random-profile-output", default=None, metavar="PATH", help="Optional .prof file to write raw profile data to")
    parser.add_argument("--random-profile-callers", default=None, metavar="FUNC", help="Print caller breakdown for a specific function name (e.g. round)")
    parser.add_argument("--random-profile-line", action="append", metavar="MOD:FUNC", dest="random_profile_line", help="Line-profile a specific function (e.g. prediction:run_prediction). Can be used multiple times. Requires line_profiler.")
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

    if args.random_generate:
        print("**** Generating {} random scenario(s) starting from seed {} ****".format(args.random_count, args.random_seed))
        scenarios = generate_scenarios(args.random_count, args.random_seed)
        save_scenarios(scenarios, args.random_output)
        sys.exit(0)

    print("**** Starting Predbat tests ****")
    my_predbat = create_predbat()
    print("**** Testing Predbat ****")
    failed = False

    if args.random_run:
        if not args.random_template:
            print("ERROR: --random-template is required with --random-run")
            sys.exit(1)
        run_scenarios_from_file(my_predbat, args.random_scenarios, args.random_template, args.random_results, debug=args.full_debug, scenario_id=args.random_scenario)
        sys.exit(0)

    if args.random_compare:
        compare_results(args.random_compare[0], args.random_compare[1])
        sys.exit(0)

    if args.random_profile:
        if not args.random_template:
            print("ERROR: --random-template is required with --random-profile")
            sys.exit(1)
        profile_scenario(
            my_predbat,
            args.random_scenarios,
            args.random_template,
            scenario_id=args.random_scenario if args.random_scenario is not None else 0,
            top_n=args.random_profile_lines,
            sort_key=args.random_profile_sort,
            prof_output=args.random_profile_output,
            callers_of=args.random_profile_callers,
            line_profile_funcs=args.random_profile_line,
        )
        sys.exit(0)

    if args.debug_file:
        run_single_debug(args.debug_file, my_predbat, args.debug_file, compare=args.compare, debug=args.full_debug, redo=args.redo)
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
