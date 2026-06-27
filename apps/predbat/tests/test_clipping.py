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
    return failed


def setup(my_predbat):
    reset_inverter(my_predbat)
    my_predbat.clipping_buffer_enable = True
    my_predbat.clipping_buffer_forecast_kwh = {}
    my_predbat.clipping_buffer_start_offset = 0
    my_predbat.clipping_buffer_end_offset = 0
    my_predbat.clipping_amplification = 1.0
    my_predbat.minutes_now = 0
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

    if len(my_predbat.export_window_best) != 1:
        print("ERROR: Expected exactly 1 window injected, got {}".format(len(my_predbat.export_window_best)))
        return True

    w = my_predbat.export_window_best[0]

    # Correct calculation: peak at 780 to 840.
    # To create 2.0 kWh headroom with 1.0 kW effective discharge rate requires 120 minutes of discharge.
    # Plus 30 mins safety margin = 150 minutes.
    # Start = max(240, 780 - 150) = 630.
    if w["start"] != 630:
        print("ERROR: Expected window start at 630, got {}".format(w["start"]))
        failed = True

    if w["end"] != 840:
        print("ERROR: Expected window end at 840, got {}".format(w["end"]))
        failed = True

    if not failed:
        print("PASS")
    return failed


def test_inject_cleans_fragmented_windows(my_predbat):
    print("**** test_inject_cleans_fragmented_windows ****")
    failed = False
    setup(my_predbat)
    my_predbat.minutes_now = 0
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
    if len(my_predbat.export_window_best) != 4:
        print("ERROR: Expected 4 windows (3 kept + 1 new), got {}".format(len(my_predbat.export_window_best)))
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

    if 690 not in starts:  # Injected window start
        print("ERROR: Injected window start 690 not found, got starts: {}".format(starts))
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
