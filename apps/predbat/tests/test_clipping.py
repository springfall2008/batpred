# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
from tests.test_infra import reset_inverter
from utils import (
    EXPORT_MODE_IDLE,
    EXPORT_MODE_TARGET,
    pack_export_limit,
)


def run_clipping_tests(my_predbat):
    """
    Tests for inject_clipping_export_windows method
    """
    failed = False
    failed |= test_inject_aborts_if_disabled(my_predbat)
    failed |= test_inject_aborts_if_empty_forecast(my_predbat)
    failed |= test_inject_creates_contiguous_window(my_predbat)
    failed |= test_inject_cleans_fragmented_windows(my_predbat)
    failed |= test_clipping_buffer_offsets(my_predbat)
    failed |= test_clipping_auto_tune_sync(my_predbat)
    failed |= test_clipping_status_overrides_display(my_predbat)
    failed |= test_clipping_status_dynamic_clearsky_omits_amplification(my_predbat)
    failed |= test_predict_clipping_target_soc_best_dynamic(my_predbat)
    failed |= test_inject_negative_minute_offset_lookup(my_predbat)
    failed |= test_inject_fallback_to_target_soc_kwh_when_map_empty(my_predbat)
    failed |= test_clipping_window_preserved_through_prune_and_discard(my_predbat)
    failed |= test_calculate_plan_clipping_execution_order(my_predbat)
    failed |= test_inject_clipping_idempotent_multi_run(my_predbat)
    failed |= test_inject_replaces_existing_peak_window(my_predbat)
    failed |= test_publish_html_plan_overlapping_windows_tuple_limit(my_predbat)
    return failed


def setup(my_predbat):
    reset_inverter(my_predbat)
    my_predbat.clipping_buffer_enable = True
    my_predbat.clipping_buffer_forecast_kwh = {}
    my_predbat.clipping_buffer_start_offset = 0
    my_predbat.clipping_buffer_end_offset = 0
    my_predbat.clipping_amplification = 1.0
    my_predbat.minutes_now = 0
    my_predbat.clipping_remaining_today = 2.0
    my_predbat.clipping_tomorrow = 0.0
    my_predbat.clipping_buffer_forecast_kwh = {600: 2.0}
    my_predbat.export_rate = {}
    my_predbat.export_window_best = []
    my_predbat.high_export_rates = []
    # Adding log mock to avoid exceptions if not using MockBase
    if not hasattr(my_predbat, "log"):
        my_predbat.log = lambda x: print(x)
    if not hasattr(my_predbat, "time_abs_str"):
        my_predbat.time_abs_str = lambda x: str(x)


def test_inject_aborts_if_disabled(my_predbat):
    print("**** test_inject_aborts_if_disabled ****")
    failed = False
    setup(my_predbat)
    my_predbat.clipping_buffer_enable = False
    my_predbat.clipping_buffer_forecast_kwh = {720: 1.0}

    my_predbat.inject_clipping_export_windows()

    if len(my_predbat.export_window_best) > 0:
        print("ERROR: Window was injected when clipping was disabled!")
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_inject_aborts_if_empty_forecast(my_predbat):
    print("**** test_inject_aborts_if_empty_forecast ****")
    failed = False
    setup(my_predbat)
    my_predbat.clipping_buffer_enable = True
    my_predbat.clipping_buffer_forecast_kwh = {}

    my_predbat.inject_clipping_export_windows()

    if len(my_predbat.export_window_best) > 0:
        print("ERROR: Window was injected when forecast was empty!")
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_inject_creates_contiguous_window(my_predbat):
    print("**** test_inject_creates_contiguous_window ****")
    failed = False
    setup(my_predbat)
    my_predbat.minutes_now = 240  # 04:00
    # Peak from 13:00 to 14:00 (780 to 840). Keys must be relative: 780-240=540, 810-240=570.
    my_predbat.clipping_buffer_forecast_kwh = {540: 1.0, 570: 2.0}

    my_predbat.inject_clipping_export_windows()

    if len(my_predbat.export_window_best) != 3:
        print("ERROR: Expected exactly 3 window injected, got {}".format(len(my_predbat.export_window_best)))
        return True

    w = my_predbat.export_window_best[0]

    # With the new behavior, morning_start is stretched back to 06:00 (360 minutes absolute)
    if w["start"] != 360:
        print("ERROR: Expected window start at 360, got {}".format(w["start"]))
        failed = True

    if w["end"] != 780:
        print("ERROR: Expected window end at 780, got {}".format(w["end"]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_inject_cleans_fragmented_windows(my_predbat):
    print("**** test_inject_cleans_fragmented_windows ****")
    failed = False
    setup(my_predbat)
    my_predbat.minutes_now = 0
    my_predbat.clipping_remaining_today = 2.0
    my_predbat.clipping_tomorrow = 0.0
    my_predbat.clipping_buffer_forecast_kwh = {600: 2.0}
    # Peak from 780 to 810 (absolute and relative are same since minutes_now=0)
    my_predbat.clipping_buffer_forecast_kwh = {
        780: 1.0,
    }

    # Inject intersecting fragmented windows
    my_predbat.export_window_best = [
        {"start": 30, "end": 50, "average": 0},  # Before morning_start (690) - should KEEP
        {"start": 100, "end": 120, "average": 0},  # Before morning_start (690) - should KEEP
        {"start": 700, "end": 800, "average": 0},  # Intersecting the new window [690, 810] - should DROP
        {"start": 900, "end": 960, "average": 0},  # After peak_end (810) - should KEEP
    ]
    my_predbat.export_limits_best = [10.0, 20.0, 30.0, 40.0]

    my_predbat.inject_clipping_export_windows()

    # We expect 4 windows: 3 kept + 1 newly injected
    if len(my_predbat.export_window_best) != 5:
        print("ERROR: Expected 5 windows (3 kept + 1 new), got {}".format(len(my_predbat.export_window_best)))
        return True

    if len(my_predbat.export_limits_best) != len(my_predbat.export_window_best):
        print("ERROR: Length mismatch! export_window_best is {}, export_limits_best is {}".format(len(my_predbat.export_window_best), len(my_predbat.export_limits_best)))
        failed = True

    starts = [w["start"] for w in my_predbat.export_window_best]
    if 700 in starts:
        print("ERROR: Fragmented windows were not cleaned!")
        failed = True

    if 100 not in starts or 30 not in starts:
        print("ERROR: Non-intersecting windows were incorrectly dropped!")
        failed = True

    if 360 not in starts:  # Injected window start
        print("ERROR: Injected window start 360 not found, got starts: {}".format(starts))
        failed = True

    # Check that limits are aligned: W0 (limit 10.0), W1 (limit 20.0), W3 (limit 40.0), and new window (target_soc_pct, e.g. 80.0)
    expected_limits = [10.0, 20.0, 40.0]
    for limit in expected_limits:
        if limit not in my_predbat.export_limits_best:
            print("ERROR: Expected limit {} not found in export_limits_best: {}".format(limit, my_predbat.export_limits_best))
            failed = True

    if not failed:
        print("PASS")
    return failed


def test_clipping_buffer_offsets(my_predbat):
    print("**** test_clipping_buffer_offsets ****")
    failed = False
    setup(my_predbat)

    # We want to test the peak PV forecast widening logic directly.
    my_predbat.pv_forecast_peak_step = {m: 0.0 for m in range(0, 125, 5)}  # pre-fill all 5-minute step keys
    my_predbat.pv_forecast_peak_step[60] = 10.0  # peak at minute 60

    # Set offsets
    my_predbat.clipping_buffer_start_offset = 15
    my_predbat.clipping_buffer_end_offset = 15

    # Run the widening logic:
    pv_forecast_peak_step = my_predbat.pv_forecast_peak_step
    start_offset = int(getattr(my_predbat, "clipping_buffer_start_offset", 0))
    end_offset = int(getattr(my_predbat, "clipping_buffer_end_offset", 0))

    if (start_offset > 0 or end_offset > 0) and pv_forecast_peak_step:
        widened_peak_step = {}
        for k, v in pv_forecast_peak_step.items():
            m_min = k - end_offset
            m_max = k + start_offset
            max_val = v
            m_start = 5 * (m_min // 5)
            m_end = 5 * ((m_max + 4) // 5)
            for m in range(m_start, m_end + 1, 5):
                val = pv_forecast_peak_step.get(m, 0.0)
                if val > max_val:
                    max_val = val
            widened_peak_step[k] = max_val
        pv_forecast_peak_step = widened_peak_step

    # Verify that minutes 45, 50, 55, 60, 65, 70, 75 all have the peak value 10.0
    expected_minutes = [45, 50, 55, 60, 65, 70, 75]
    for m in expected_minutes:
        val = pv_forecast_peak_step.get(m, 0.0)
        if val != 10.0:
            print("ERROR: Expected widened peak value 10.0 at minute {}, got {}".format(m, val))
            failed = True

    # Verify that minutes outside range do not have the peak value (e.g. 40, 80)
    for m in [40, 80]:
        val = pv_forecast_peak_step.get(m, 0.0)
        if val == 10.0:
            print("ERROR: Widened peak overflowed to minute {}, got {}".format(m, val))
            failed = True

    if not failed:
        print("PASS")
    return failed


def test_clipping_auto_tune_sync(my_predbat):
    print("**** test_clipping_auto_tune_sync ****")
    failed = False
    setup(my_predbat)

    # Save original functions to restore them later
    original_expose_config = my_predbat.expose_config
    original_save_current_config = my_predbat.save_current_config

    # Mock expose_config and save_current_config
    exposed_calls = []

    def mock_expose_config(name, value, *args, **kwargs):
        exposed_calls.append((name, value))

    saved_calls = 0

    def mock_save_current_config(*args, **kwargs):
        nonlocal saved_calls
        saved_calls += 1

    my_predbat.expose_config = mock_expose_config
    my_predbat.save_current_config = mock_save_current_config

    try:
        # Configure auto_tune to True
        my_predbat.clipping_auto_tune = True

        # Sync logic
        auto_amp = 1.35
        auto_start_offset = 15
        auto_end_offset = 15

        # Perform sync
        config_changed = False

        current_amp = getattr(my_predbat, "clipping_amplification", 1.0)
        if current_amp is None or abs(current_amp - auto_amp) > 1e-4:
            my_predbat.clipping_amplification = auto_amp
            my_predbat.expose_config("clipping_amplification", auto_amp)
            config_changed = True

        current_start = getattr(my_predbat, "clipping_buffer_start_offset", 0)
        if current_start is None or current_start != auto_start_offset:
            my_predbat.clipping_buffer_start_offset = auto_start_offset
            my_predbat.expose_config("clipping_buffer_start_offset", auto_start_offset)
            config_changed = True

        current_end = getattr(my_predbat, "clipping_buffer_end_offset", 0)
        if current_end is None or current_end != auto_end_offset:
            my_predbat.clipping_buffer_end_offset = auto_end_offset
            my_predbat.expose_config("clipping_buffer_end_offset", auto_end_offset)
            config_changed = True

        if config_changed:
            my_predbat.save_current_config()

        if my_predbat.clipping_amplification != 1.35:
            print("ERROR: clipping_amplification was not updated to 1.35, got {}".format(my_predbat.clipping_amplification))
            failed = True

        if my_predbat.clipping_buffer_start_offset != 15:
            print("ERROR: clipping_buffer_start_offset was not updated to 15, got {}".format(my_predbat.clipping_buffer_start_offset))
            failed = True

        if my_predbat.clipping_buffer_end_offset != 15:
            print("ERROR: clipping_buffer_end_offset was not updated to 15, got {}".format(my_predbat.clipping_buffer_end_offset))
            failed = True

        if ("clipping_amplification", 1.35) not in exposed_calls:
            print("ERROR: expose_config was not called for clipping_amplification")
            failed = True

        if ("clipping_buffer_start_offset", 15) not in exposed_calls:
            print("ERROR: expose_config was not called for clipping_buffer_start_offset")
            failed = True

        if ("clipping_buffer_end_offset", 15) not in exposed_calls:
            print("ERROR: expose_config was not called for clipping_buffer_end_offset")
            failed = True

        if saved_calls != 1:
            print("ERROR: save_current_config was not called once, got: {}".format(saved_calls))
            failed = True
    finally:
        # RESTORE the original functions to avoid contaminating subsequent tests
        my_predbat.expose_config = original_expose_config
        my_predbat.save_current_config = original_save_current_config

    if not failed:
        print("PASS")
    return failed


def test_clipping_status_overrides_display(my_predbat):
    print("**** test_clipping_status_overrides_display ****")
    failed = False
    setup(my_predbat)

    from unittest.mock import MagicMock

    # Set parameters to non-defaults
    my_predbat.clipping_buffer_enable = True
    my_predbat.clipping_buffer_start_offset = 15
    my_predbat.clipping_buffer_end_offset = 10
    my_predbat.clipping_amplification = 1.25
    my_predbat.clipping_limit_override = 1.0 / 60.0  # 1.0 kW (which is 1/60 internal units)
    my_predbat.clipping_buffer_max_kwh = 4.0
    my_predbat.clipping_auto_tune = False

    # Mock self.prediction
    mock_prediction = MagicMock()
    mock_prediction.run_prediction.return_value = (0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0, 0.0, 0.0, 0.0, 0.0, {0: 1.0, 60: 1.0, 480: 1.0, 720: 1.0}, [0.0], 0.0, 0.0, 0.0, 0.0)
    mock_prediction.predict_soc_time = {0: 1.0}
    mock_prediction.first_charge = 0
    mock_prediction.first_charge_soc = 0.0
    mock_prediction.predict_car_soc_time = [{0: 0.0}]
    mock_prediction.predict_battery_power = {0: 0.0}
    mock_prediction.predict_state = {0: 0.0}
    mock_prediction.predict_battery_cycle = {0: 0.0}
    mock_prediction.predict_pv_power = {0: 0.0}
    mock_prediction.predict_grid_power = {0: 0.0}
    mock_prediction.predict_load_power = {0: 0.0}
    mock_prediction.final_export_kwh = 0.0
    mock_prediction.export_kwh_h0 = 0.0
    mock_prediction.final_load_kwh = 0.0
    mock_prediction.load_kwh_h0 = 0.0
    mock_prediction.metric_time = {0: 0.0}
    mock_prediction.record_time = {0: 0.0}
    mock_prediction.predict_iboost = {0: 0.0}
    mock_prediction.predict_carbon_g = {0: 0.0}
    mock_prediction.load_kwh_time = {0: 0.0}
    mock_prediction.pv_kwh_time = {0: 0.0}
    mock_prediction.import_kwh_time = {0: 0.0}
    mock_prediction.export_kwh_time = {0: 0.0}
    mock_prediction.final_pv_kwh = 0.0
    mock_prediction.export_to_first_charge = 0.0
    mock_prediction.pv_kwh_h0 = 0.0
    mock_prediction.final_import_kwh = 0.0
    mock_prediction.final_import_kwh_house = 0.0
    mock_prediction.final_import_kwh_battery = 0.0
    mock_prediction.hours_left = 24.0
    mock_prediction.final_car_soc = [0.0]
    mock_prediction.import_kwh_h0 = 0.0
    mock_prediction.predict_export = {0: 0.0}
    mock_prediction.predict_soc_best = {0: 1.0}
    mock_prediction.predict_iboost_best = {0: 0.0}
    mock_prediction.predict_metric_best = {0: 0.0}
    mock_prediction.predict_carbon_best = {0: 0.0}
    mock_prediction.predict_clipped_best = {0: 0.0}
    mock_prediction.debug_enable = False

    original_prediction = getattr(my_predbat, "prediction", None)
    my_predbat.prediction = mock_prediction

    # Mock scenario helpers to avoid executing them on mock prediction datasets
    original_summary_title = my_predbat.scenario_summary_title
    original_summary = my_predbat.scenario_summary
    original_summary_state = my_predbat.scenario_summary_state

    my_predbat.scenario_summary_title = lambda x: "dummy_title"
    my_predbat.scenario_summary = lambda x, y: "dummy_summary"
    my_predbat.scenario_summary_state = lambda x: "dummy_state"

    # Mock dashboard_item
    exposed_items = {}
    original_dashboard_item = my_predbat.dashboard_item

    def mock_dashboard_item(name, state=None, attributes=None):
        exposed_items[name] = {"state": state, "attributes": attributes}

    my_predbat.dashboard_item = mock_dashboard_item

    try:
        # Run prediction with save="best" to trigger the status string generation
        my_predbat.run_prediction(my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, False, 24 * 60, save="best")

        status_key = my_predbat.prefix + ".clipping_status"
        if status_key not in exposed_items:
            print("ERROR: clipping_status was not published to dashboard")
            failed = True
        else:
            item = exposed_items[status_key]
            state = item["state"]
            attrs = item["attributes"]

            # Expected overrides text inside the state description
            expected_override_str = "(15m start offset, 10m end offset, 1.25x amplification, 1.0kW limit override, 4.0kWh max override active)."
            if expected_override_str not in state:
                print("ERROR: Expected override string '{}' not found in state '{}'".format(expected_override_str, state))
                failed = True

            # Assert attributes
            if attrs.get("clipping_buffer_start_offset") != 15:
                print("ERROR: attribute clipping_buffer_start_offset mismatch")
                failed = True
            if attrs.get("clipping_buffer_end_offset") != 10:
                print("ERROR: attribute clipping_buffer_end_offset mismatch")
                failed = True
            if attrs.get("clipping_amplification") != 1.25:
                print("ERROR: attribute clipping_amplification mismatch")
                failed = True
            if attrs.get("clipping_limit_override_kw") != 1.0:
                print("ERROR: attribute clipping_limit_override_kw mismatch, got {}".format(attrs.get("clipping_limit_override_kw")))
                failed = True
            if attrs.get("clipping_buffer_max_kwh_override") != 4.0:
                print("ERROR: attribute clipping_buffer_max_kwh_override mismatch")
                failed = True

    finally:
        if original_prediction is not None:
            my_predbat.prediction = original_prediction
        elif hasattr(my_predbat, "prediction"):
            del my_predbat.prediction
        my_predbat.dashboard_item = original_dashboard_item
        my_predbat.scenario_summary_title = original_summary_title
        my_predbat.scenario_summary = original_summary
        my_predbat.scenario_summary_state = original_summary_state

    if not failed:
        print("PASS")
    return failed


def test_clipping_status_dynamic_clearsky_omits_amplification(my_predbat):
    """Assert that when clipping_mode is 'Dynamic ClearSky', amplification is omitted from status text."""
    print("**** test_clipping_status_dynamic_clearsky_omits_amplification ****")
    failed = False
    setup(my_predbat)

    from unittest.mock import MagicMock

    my_predbat.clipping_buffer_enable = True
    my_predbat.clipping_mode = "Dynamic ClearSky"
    my_predbat.clipping_amplification = 1.37
    my_predbat.clipping_auto_tune = False

    # Mock self.prediction
    mock_prediction = MagicMock()
    mock_prediction.run_prediction.return_value = (0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0, 0.0, 0.0, 0.0, 0.0, {0: 1.0, 60: 1.0, 480: 1.0, 720: 1.0}, [0.0], 0.0, 0.0, 0.0, 0.0)
    mock_prediction.predict_soc_time = {0: 1.0}
    mock_prediction.first_charge = 0
    mock_prediction.first_charge_soc = 0.0
    mock_prediction.predict_car_soc_time = [{0: 0.0}]
    mock_prediction.predict_battery_power = {0: 0.0}
    mock_prediction.predict_state = {0: 0.0}
    mock_prediction.predict_battery_cycle = {0: 0.0}
    mock_prediction.predict_pv_power = {0: 0.0}
    mock_prediction.predict_grid_power = {0: 0.0}
    mock_prediction.predict_load_power = {0: 0.0}
    mock_prediction.final_export_kwh = 0.0
    mock_prediction.export_kwh_h0 = 0.0
    mock_prediction.final_load_kwh = 0.0
    mock_prediction.load_kwh_h0 = 0.0
    mock_prediction.metric_time = {0: 0.0}
    mock_prediction.record_time = {0: 0.0}
    mock_prediction.predict_iboost = {0: 0.0}
    mock_prediction.predict_carbon_g = {0: 0.0}
    mock_prediction.load_kwh_time = {0: 0.0}
    mock_prediction.pv_kwh_time = {0: 0.0}
    mock_prediction.import_kwh_time = {0: 0.0}
    mock_prediction.export_kwh_time = {0: 0.0}
    mock_prediction.final_pv_kwh = 0.0
    mock_prediction.export_to_first_charge = 0.0
    mock_prediction.pv_kwh_h0 = 0.0
    mock_prediction.final_import_kwh = 0.0
    mock_prediction.final_import_kwh_house = 0.0
    mock_prediction.final_import_kwh_battery = 0.0
    mock_prediction.hours_left = 24.0
    mock_prediction.final_car_soc = [0.0]
    mock_prediction.import_kwh_h0 = 0.0
    mock_prediction.predict_export = {0: 0.0}
    mock_prediction.predict_soc_best = {0: 1.0}
    mock_prediction.predict_iboost_best = {0: 0.0}
    mock_prediction.predict_metric_best = {0: 0.0}
    mock_prediction.predict_carbon_best = {0: 0.0}
    mock_prediction.predict_clipped_best = {0: 0.0}
    mock_prediction.debug_enable = False

    original_prediction = getattr(my_predbat, "prediction", None)
    my_predbat.prediction = mock_prediction

    original_summary_title = my_predbat.scenario_summary_title
    original_summary = my_predbat.scenario_summary
    original_summary_state = my_predbat.scenario_summary_state

    my_predbat.scenario_summary_title = lambda x: "dummy_title"
    my_predbat.scenario_summary = lambda x, y: "dummy_summary"
    my_predbat.scenario_summary_state = lambda x: "dummy_state"

    exposed_items = {}
    original_dashboard_item = my_predbat.dashboard_item

    def mock_dashboard_item(name, state=None, attributes=None):
        exposed_items[name] = {"state": state, "attributes": attributes}

    my_predbat.dashboard_item = mock_dashboard_item

    try:
        my_predbat.run_prediction(my_predbat.charge_limit_best, my_predbat.charge_window_best, my_predbat.export_window_best, my_predbat.export_limits_best, False, 24 * 60, save="best")

        status_key = my_predbat.prefix + ".clipping_status"
        if status_key not in exposed_items:
            print("ERROR: clipping_status was not published to dashboard")
            failed = True
        else:
            item = exposed_items[status_key]
            state = item["state"]
            attrs = item["attributes"]

            if "amplification" in state:
                print("ERROR: Unexpected amplification mentioned in status state '{}' under Dynamic ClearSky mode".format(state))
                failed = True

            if attrs.get("clipping_mode") != "Dynamic ClearSky":
                print("ERROR: Expected clipping_mode attribute 'Dynamic ClearSky', got '{}'".format(attrs.get("clipping_mode")))
                failed = True

    finally:
        if original_prediction is not None:
            my_predbat.prediction = original_prediction
        elif hasattr(my_predbat, "prediction"):
            del my_predbat.prediction
        my_predbat.dashboard_item = original_dashboard_item
        my_predbat.scenario_summary_title = original_summary_title
        my_predbat.scenario_summary = original_summary
        my_predbat.scenario_summary_state = original_summary_state

    if not failed:
        print("PASS")
    return failed


def test_predict_clipping_target_soc_best_dynamic(my_predbat):
    """
    Test that predict_clipping_target_soc_best is dynamically populated
    using pv_forecast_peak_step when clipping_buffer_enable is True,
    even if clipping_cost_weight is 0.
    """
    print("**** test_predict_clipping_target_soc_best_dynamic ****")
    from unittest.mock import MagicMock
    from prediction import Prediction

    failed = False
    setup(my_predbat)

    my_predbat.clipping_buffer_enable = True
    my_predbat.clipping_cost_weight = 0  # Verify independence from clipping_cost_weight
    my_predbat.clipping_buffer_kwh = 0  # Dynamic mode, not manual override
    my_predbat.clipping_limit_effective = 3.6  # 3.6 kW
    my_predbat.soc_max = 10.0
    my_predbat.best_soc_min = 1.0
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.minutes_now = 0
    my_predbat.clipping_remaining_today = 2.0
    my_predbat.clipping_tomorrow = 0.0
    my_predbat.clipping_buffer_forecast_kwh = {600: 2.0}

    # Construct peak solar forecast over 5-minute steps
    # Solar peak between 10:00 (minute 600) and 14:00 (minute 840)
    step = 5
    pv_forecast_peak_step = {}
    for minute in range(0, 24 * 60 + step, step):
        if 600 <= minute <= 840:
            pv_forecast_peak_step[minute] = 0.5  # Exceeds limit (0.3), so 0.2 kWh excess per 5min
        else:
            pv_forecast_peak_step[minute] = 0.0

    # 1. Assert Prediction constructor stores pv_forecast_peak_step
    pred_instance = Prediction(my_predbat, {}, {}, {}, {}, pv_forecast_peak_step=pv_forecast_peak_step)
    if pred_instance.pv_forecast_peak_step != pv_forecast_peak_step:
        print("ERROR: Prediction constructor did not retain pv_forecast_peak_step")
        failed = True

    # 2. Test dynamic population via pred.pv_forecast_peak_step and fallback to self.pv_forecast_peak_step
    mock_prediction = MagicMock()
    mock_prediction.run_prediction.return_value = (0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 0, 0.0, 0.0, 0.0, 0.0, {0: 1.0, 60: 1.0, 480: 1.0, 720: 1.0}, [0.0], 0.0, 0.0, 0.0, 0.0)
    mock_prediction.predict_soc_time = {0: 1.0}
    mock_prediction.first_charge = 0
    mock_prediction.first_charge_soc = 0.0
    mock_prediction.predict_car_soc_time = [{0: 0.0}]
    mock_prediction.predict_battery_power = {0: 0.0}
    mock_prediction.predict_state = {0: 0.0}
    mock_prediction.predict_battery_cycle = {0: 0.0}
    mock_prediction.predict_pv_power = {0: 0.0}
    mock_prediction.predict_grid_power = {0: 0.0}
    mock_prediction.predict_load_power = {0: 0.0}
    mock_prediction.final_export_kwh = 0.0
    mock_prediction.export_kwh_h0 = 0.0
    mock_prediction.final_load_kwh = 0.0
    mock_prediction.load_kwh_h0 = 0.0
    mock_prediction.metric_time = {0: 0.0}
    mock_prediction.record_time = {0: 0.0}
    mock_prediction.predict_iboost = {0: 0.0}
    mock_prediction.predict_carbon_g = {0: 0.0}
    mock_prediction.load_kwh_time = {0: 0.0}
    mock_prediction.pv_kwh_time = {0: 0.0}
    mock_prediction.import_kwh_time = {0: 0.0}
    mock_prediction.export_kwh_time = {0: 0.0}
    mock_prediction.final_pv_kwh = 0.0
    mock_prediction.export_to_first_charge = 0.0
    mock_prediction.pv_kwh_h0 = 0.0
    mock_prediction.final_import_kwh = 0.0
    mock_prediction.final_import_kwh_house = 0.0
    mock_prediction.final_import_kwh_battery = 0.0
    mock_prediction.hours_left = 24.0
    mock_prediction.final_car_soc = [0.0]
    mock_prediction.import_kwh_h0 = 0.0
    mock_prediction.predict_export = {0: 0.0}
    mock_prediction.predict_soc_best = {0: 1.0}
    mock_prediction.predict_iboost_best = {0: 0.0}
    mock_prediction.predict_metric_best = {0: 0.0}
    mock_prediction.predict_carbon_best = {0: 0.0}
    mock_prediction.predict_clipped_best = {0: 0.0}
    mock_prediction.debug_enable = False
    mock_prediction.pv_forecast_peak_step = pv_forecast_peak_step

    original_prediction = getattr(my_predbat, "prediction", None)
    my_predbat.prediction = mock_prediction

    # Mock scenario helpers and dashboard_item
    original_summary_title = my_predbat.scenario_summary_title
    original_summary = my_predbat.scenario_summary
    original_summary_state = my_predbat.scenario_summary_state
    original_dashboard_item = my_predbat.dashboard_item

    my_predbat.scenario_summary_title = lambda x: "dummy_title"
    my_predbat.scenario_summary = lambda x, y: "dummy_summary"
    my_predbat.scenario_summary_state = lambda x: "dummy_state"
    my_predbat.dashboard_item = lambda name, state=None, attributes=None: None

    try:
        my_predbat.pv_forecast_peak_step = pv_forecast_peak_step
        my_predbat.calculate_clipping_target_soc(pred=my_predbat, step=5)
        my_predbat.run_prediction(
            my_predbat.charge_limit_best,
            my_predbat.charge_window_best,
            my_predbat.export_window_best,
            my_predbat.export_limits_best,
            False,
            24 * 60,
            save="best",
        )

        target_soc = getattr(my_predbat, "predict_clipping_target_soc_best", {})
        remaining = getattr(my_predbat, "predict_clipping_remaining_best", {})

        if not target_soc:
            print("ERROR: predict_clipping_target_soc_best was not populated")
            failed = True
        elif not remaining:
            print("ERROR: predict_clipping_remaining_best was not populated")
            failed = True
        else:
            # Morning / before-peak (e.g. minute 0) should have headroom (target SOC < soc_max)
            if target_soc.get(0, my_predbat.soc_max) >= my_predbat.soc_max:
                print("ERROR: Expected target SOC at minute 0 to be below soc_max ({}), got {}".format(my_predbat.soc_max, target_soc.get(0)))
                failed = True

            if remaining.get(0, 0.0) <= 0.0:
                print("ERROR: Expected remaining clipping buffer at minute 0 to be > 0, got {}".format(remaining.get(0)))
                failed = True

            # Post-peak (e.g. minute 900 / 15:00) should have recovered back to soc_max
            if target_soc.get(900, 0.0) != my_predbat.soc_max:
                print("ERROR: Expected target SOC at minute 900 to equal soc_max ({}), got {}".format(my_predbat.soc_max, target_soc.get(900)))
                failed = True

            if remaining.get(900, 1.0) != 0.0:
                print("ERROR: Expected remaining clipping buffer at minute 900 to be 0.0, got {}".format(remaining.get(900)))
                failed = True

        # Test fallback: pred.pv_forecast_peak_step is None, but self.pv_forecast_peak_step is set
        mock_prediction.pv_forecast_peak_step = None
        my_predbat.pv_forecast_peak_step = pv_forecast_peak_step
        my_predbat.predict_clipping_target_soc_best = {}

        my_predbat.pv_forecast_peak_step = pv_forecast_peak_step
        my_predbat.calculate_clipping_target_soc(pred=my_predbat, step=5)
        my_predbat.run_prediction(
            my_predbat.charge_limit_best,
            my_predbat.charge_window_best,
            my_predbat.export_window_best,
            my_predbat.export_limits_best,
            False,
            24 * 60,
            save="best",
        )

        fallback_target_soc = getattr(my_predbat, "predict_clipping_target_soc_best", {})
        if not fallback_target_soc or fallback_target_soc.get(0, my_predbat.soc_max) >= my_predbat.soc_max:
            print("ERROR: Fallback to self.pv_forecast_peak_step failed to populate target SOC curve")
            failed = True

    finally:
        if original_prediction is not None:
            my_predbat.prediction = original_prediction
        elif hasattr(my_predbat, "prediction"):
            del my_predbat.prediction
        my_predbat.dashboard_item = original_dashboard_item
        my_predbat.scenario_summary_title = original_summary_title
        my_predbat.scenario_summary = original_summary
        my_predbat.scenario_summary_state = original_summary_state

    if not failed:
        print("PASS")
    return failed


def test_inject_negative_minute_offset_lookup(my_predbat):
    """Verify that when 30-minute boundary alignment causes current_start < minutes_now (negative offset),
    inject_clipping_export_windows clamps lookup to 0 and uses predict_clipping_target_soc_best[0] rather
    than falling back to 100% (soc_max)."""
    print("**** test_inject_negative_minute_offset_lookup ****")
    failed = False
    setup(my_predbat)
    my_predbat.soc_max = 10.0
    my_predbat.minutes_now = 675  # 11:15
    # Forecast with peak from 720 (12:00) to 840 (14:00)
    # relative keys: 720 - 675 = 45, 750 - 675 = 75
    my_predbat.clipping_buffer_forecast_kwh = {45: 0.6}
    # Dynamic target soc: at minute 0, target is 9.4 kWh (94%)
    my_predbat.predict_clipping_target_soc_best = {0: 9.4, 5: 9.4, 45: 9.4, 75: 9.4}

    my_predbat.inject_clipping_export_windows()

    if not my_predbat.export_window_best:
        print("ERROR: No export window injected!")
        return True

    w = my_predbat.export_window_best[0]
    target_pct = w.get("clipping_target_soc_pct")
    if target_pct != 94.0:
        print("ERROR: Expected clipping_target_soc_pct 94.0, got {}".format(target_pct))
        failed = True

    if my_predbat.export_limits_best[0] != pack_export_limit(EXPORT_MODE_TARGET, 94):
        print("ERROR: Expected export_limits_best[0] pack_export_limit(EXPORT_MODE_TARGET, 94), got {}".format(my_predbat.export_limits_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_inject_fallback_to_target_soc_kwh_when_map_empty(my_predbat):
    """Verify that when predict_clipping_target_soc_best is empty, inject_clipping_export_windows
    falls back to target_soc_kwh (derived from total_kwh_loss) instead of self.soc_max (100%)."""
    print("**** test_inject_fallback_to_target_soc_kwh_when_map_empty ****")
    failed = False
    setup(my_predbat)
    my_predbat.soc_max = 10.0
    my_predbat.minutes_now = 675  # 11:15
    # Forecast with peak from 720 (12:00) to 840 (14:00)
    my_predbat.clipping_buffer_forecast_kwh = {45: 0.6}
    my_predbat.predict_clipping_target_soc_best = {}

    my_predbat.inject_clipping_export_windows()

    if not my_predbat.export_window_best:
        print("ERROR: No export window injected!")
        return True

    w = my_predbat.export_window_best[0]
    target_pct = w.get("clipping_target_soc_pct")
    # Total loss = 0.6 kWh -> target_soc_kwh = 10.0 - 0.6 = 9.4 -> 94.0%
    if target_pct != 94.0:
        print("ERROR: Expected fallback clipping_target_soc_pct 94.0, got {}".format(target_pct))
        failed = True

    if my_predbat.export_limits_best[0] != pack_export_limit(EXPORT_MODE_TARGET, 94):
        print("ERROR: Expected export_limits_best[0] pack_export_limit(EXPORT_MODE_TARGET, 94), got {}".format(my_predbat.export_limits_best[0]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_clipping_window_preserved_through_prune_and_discard(my_predbat):
    """Verify that an injected clipping export window survives prune_dead_plan_slots and discard_unused_export_slots."""
    print("**** test_clipping_window_preserved_through_prune_and_discard ****")
    failed = False
    setup(my_predbat)
    my_predbat.soc_max = 10.0
    my_predbat.minutes_now = 660  # 11:00
    my_predbat.clipping_buffer_forecast_kwh = {60: 0.6}
    my_predbat.predict_clipping_target_soc_best = {0: 9.4, 60: 9.4}

    my_predbat.inject_clipping_export_windows()

    if not my_predbat.export_window_best:
        print("ERROR: Window was not injected!")
        return True

    # Now run prune_dead_plan_slots and discard_unused_export_slots
    my_predbat.prune_dead_plan_slots()
    my_predbat.export_limits_best, my_predbat.export_window_best = my_predbat.discard_unused_export_slots(my_predbat.export_limits_best, my_predbat.export_window_best)

    if len(my_predbat.export_window_best) == 0:
        print("ERROR: Clipping export window was purged by prune/discard!")
        failed = True
    elif my_predbat.export_window_best[0].get("clipping_target_soc_pct") != 94.0:
        print("ERROR: Injected clipping window lost target percentage, got {}".format(my_predbat.export_window_best[0].get("clipping_target_soc_pct")))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_calculate_plan_clipping_execution_order(my_predbat):
    """Verify that calculate_clipping_target_soc executes before inject_clipping_export_windows in calculate_plan."""
    print("**** test_calculate_plan_clipping_execution_order ****")
    failed = False
    setup(my_predbat)
    my_predbat.clipping_buffer_enable = True

    call_order = []
    orig_calc_target = my_predbat.calculate_clipping_target_soc
    orig_inject = my_predbat.inject_clipping_export_windows

    def track_calc_target(*args, **kwargs):
        call_order.append("calculate_clipping_target_soc")
        return orig_calc_target(*args, **kwargs)

    def track_inject(*args, **kwargs):
        call_order.append("inject_clipping_export_windows")
        return orig_inject(*args, **kwargs)

    my_predbat.calculate_clipping_target_soc = track_calc_target
    my_predbat.inject_clipping_export_windows = track_inject

    try:
        my_predbat.clipping_limit_override = 5.0
        my_predbat.calculate_plan(recompute=False, publish=False)
    finally:
        my_predbat.calculate_clipping_target_soc = orig_calc_target
        my_predbat.inject_clipping_export_windows = orig_inject

    if "calculate_clipping_target_soc" not in call_order:
        print("ERROR: calculate_clipping_target_soc was not called during calculate_plan")
        failed = True
    elif "inject_clipping_export_windows" not in call_order:
        print("ERROR: inject_clipping_export_windows was not called during calculate_plan")
        failed = True
    else:
        calc_idx = call_order.index("calculate_clipping_target_soc")
        inject_idx = call_order.index("inject_clipping_export_windows")
        if calc_idx > inject_idx:
            print("ERROR: inject_clipping_export_windows ran before calculate_clipping_target_soc! Order: {}".format(call_order))
            failed = True

    if not failed:
        print("PASS")
    return failed


def test_inject_clipping_idempotent_multi_run(my_predbat):
    """Verify that repeatedly running inject_clipping_export_windows maintains strict chronological order,
    allows in_charge_window to find the active window at minutes_now, and does not duplicate high_export_rates."""
    print("**** test_inject_clipping_idempotent_multi_run ****")
    failed = False
    setup(my_predbat)
    my_predbat.soc_max = 10.0
    my_predbat.minutes_now = 675  # 11:15
    my_predbat.clipping_buffer_forecast_kwh = {45: 0.6}
    my_predbat.predict_clipping_target_soc_best = {0: 9.4, 45: 9.4, 75: 9.4}
    my_predbat.high_export_rates = []

    # First injection
    my_predbat.inject_clipping_export_windows()
    run1_count = len(my_predbat.export_window_best)
    run1_high_count = len(my_predbat.high_export_rates)
    if my_predbat.in_charge_window(my_predbat.export_window_best, 675) == -1:
        print("ERROR: in_charge_window failed to find window at minute 675 on run 1")
        failed = True

    # Second injection (simulating repeated execution in daemon loop)
    my_predbat.inject_clipping_export_windows()
    run2_count = len(my_predbat.export_window_best)
    run2_high_count = len(my_predbat.high_export_rates)

    if run1_count != run2_count:
        print("ERROR: Window count changed between runs: {} vs {}".format(run1_count, run2_count))
        failed = True

    if run2_high_count != run1_high_count:
        print("ERROR: high_export_rates accumulated duplicates across runs: {} vs {}".format(run2_high_count, run1_high_count))
        failed = True

    # Verify chronological sorting
    for i in range(len(my_predbat.export_window_best) - 1):
        if my_predbat.export_window_best[i]["start"] > my_predbat.export_window_best[i + 1]["start"]:
            print("ERROR: export_window_best is not chronologically sorted: {}".format(my_predbat.export_window_best))
            failed = True
            break

    # Crucial check: in_charge_window must find the window at minute 675 on run 2
    if my_predbat.in_charge_window(my_predbat.export_window_best, 675) == -1:
        print("ERROR: in_charge_window failed to find active window at minute 675 on run 2 due to list order corruption")
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_inject_replaces_existing_peak_window(my_predbat):
    """Verify that an existing stale/idle window during the peak period [peak_start, peak_end]
    is replaced and assigned clipping_target_soc_pct rather than retaining an unconstrained 100% target."""
    print("**** test_inject_replaces_existing_peak_window ****")
    failed = False
    setup(my_predbat)
    my_predbat.soc_max = 10.0
    my_predbat.minutes_now = 660  # 11:00
    my_predbat.clipping_buffer_forecast_kwh = {60: 0.6}  # Peak at 720 to 750
    my_predbat.predict_clipping_target_soc_best = {0: 9.4, 60: 9.4, 90: 9.4}

    # Pre-existing idle window covering the peak period with idle limit
    my_predbat.export_window_best = [{"start": 720, "end": 750}]
    my_predbat.export_limits_best = [pack_export_limit(EXPORT_MODE_IDLE)]

    my_predbat.inject_clipping_export_windows()

    found_peak = False
    for window, limit in zip(my_predbat.export_window_best, my_predbat.export_limits_best):
        if window["start"] == 720 and window["end"] == 750:
            found_peak = True
            if window.get("clipping_target_soc_pct") != 94.0:
                print("ERROR: Peak window missing clipping_target_soc_pct 94.0, got {}".format(window.get("clipping_target_soc_pct")))
                failed = True
            if limit != pack_export_limit(EXPORT_MODE_TARGET, 94):
                print("ERROR: Peak window limit is {}, expected {}".format(limit, pack_export_limit(EXPORT_MODE_TARGET, 94)))
                failed = True

    if not found_peak:
        print("ERROR: Peak window 720-750 was not found in export_window_best: {}".format(my_predbat.export_window_best))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_publish_html_plan_overlapping_windows_tuple_limit(my_predbat):
    """Verify that publish_html_plan handles tuple export limits during overlapping windows without TypeError."""
    print("**** test_publish_html_plan_overlapping_windows_tuple_limit ****")
    failed = False
    setup(my_predbat)
    my_predbat.soc_max = 10.0
    my_predbat.minutes_now = 660  # 11:00
    my_predbat.forecast_minutes = 24 * 60

    # Overlapping charge and export windows at minute 720
    my_predbat.charge_window_best = [{"start": 720, "end": 750, "target": 80.0}]
    my_predbat.charge_limit_best = [8.0]
    my_predbat.export_window_best = [{"start": 720, "end": 750}]
    my_predbat.export_limits_best = [pack_export_limit(EXPORT_MODE_TARGET, 50)]

    try:
        pv_step = {m: 0.0 for m in range(0, 24 * 60, 5)}
        load_step = {m: 0.1 for m in range(0, 24 * 60, 5)}
        my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step, 24 * 60, publish=False)
    except TypeError as e:
        print("ERROR: publish_html_plan raised TypeError: {}".format(e))
        failed = True
    except Exception as e:
        print("ERROR: publish_html_plan raised unexpected exception: {}".format(e))
        failed = True

    if not failed:
        print("PASS")
    return failed
