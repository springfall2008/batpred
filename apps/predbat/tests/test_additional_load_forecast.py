# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for named fixed house load forecasts."""

from datetime import timedelta
from tests.test_infra import run_async


def configure_additional_load_test(my_predbat):
    """Configure deterministic clock and plan settings for additional load tests."""
    my_predbat.minutes_now = 10 * 60
    my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=my_predbat.minutes_now)
    my_predbat.plan_interval_minutes = 30
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.args["plan_interval_minutes"] = 30
    my_predbat.days_previous = [1]
    my_predbat.days_previous_weight = [1]
    my_predbat.max_days_previous = 2
    my_predbat.house_load_additional_forecast_overrides = {}
    my_predbat.house_load_additional_forecast_entities = set()
    my_predbat.house_load_additional_forecasts = {}
    my_predbat.house_load_additional_forecast_adjust = {}
    if "load_forecast_delta_api" in my_predbat.config_index:
        my_predbat.config_index["load_forecast_delta_api"]["value"] = "off"


def check_slot(load_adjust, minute, expected, label):
    """Check one generated load adjustment minute value."""
    actual = load_adjust.get(minute, 0.0)
    if actual != expected:
        print("ERROR: {} expected {} at minute {} got {}".format(label, expected, minute, actual))
        return 1
    return 0


def test_additional_load_disabled(my_predbat):
    """Test duration 0 disables an additional load forecast."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [{"name": "dishwasher", "start_time": "20:00", "duration": 0, "energy": 1.2}]
    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    if load_adjust or forecasts.get("dishwasher", {}).get("state") != "off":
        print("ERROR: Disabled additional load should have no adjustments and publish off")
        failed = 1
    return failed


def test_additional_load_enabled_false_profile(my_predbat):
    """Test enabled false publishes a disabled profile without load adjustment."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [{"name": "dishwasher", "enabled": False, "start_time": "20:00", "duration": 2.0, "energy": 1.2}]
    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    forecast = forecasts.get("dishwasher", {})
    if load_adjust or forecast.get("state") != "off" or forecast.get("enabled"):
        print("ERROR: enabled false profile should publish off without adjustments")
        failed = 1
    return failed


def test_additional_load_enabled_string_profile(my_predbat):
    """Test additional loads use the shared string-to-boolean conversion."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "disabled", "enabled": "false", "start_time": "20:00", "duration": 1.0, "energy": 0.5},
        {"name": "numeric", "enabled": "1", "start_time": "21:00", "duration": 1.0, "energy": 0.5},
        {"name": "connected", "enabled": "connected", "start_time": "22:00", "duration": 1.0, "energy": 0.5},
    ]
    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    if forecasts.get("disabled", {}).get("enabled") or load_adjust.get(20 * 60, 0.0):
        print("ERROR: String false should disable an additional load")
        failed = 1
    if not forecasts.get("numeric", {}).get("enabled") or not forecasts.get("connected", {}).get("enabled"):
        print("ERROR: Shared truthy string forms should enable additional loads")
        failed = 1
    return failed


def test_additional_load_dishwasher_simple(my_predbat):
    """Test a simple dishwasher total energy forecast and its published sensor."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [{"name": "dishwasher", "start_time": "20:00", "duration": 2.0, "energy": 2.0}]
    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    for minute in [20 * 60, 20 * 60 + 30, 21 * 60, 21 * 60 + 30]:
        failed |= check_slot(load_adjust, minute, 0.5, "dishwasher simple")
    failed |= check_slot(load_adjust, 22 * 60, 0.0, "dishwasher simple end")
    my_predbat.house_load_additional_forecasts = forecasts
    my_predbat.publish_additional_load_forecasts()
    sensor = my_predbat.dashboard_values.get("binary_sensor.predbat_load_forecast_delta_dishwasher", {})
    if forecasts.get("dishwasher", {}).get("state") != "on" or sensor.get("state") != "on" or len(sensor.get("attributes", {}).get("target_times", [])) != 4:
        print("ERROR: Dishwasher should publish an on sensor with four slots")
        failed = 1
    return failed


def test_additional_load_end_time_without_duration(my_predbat):
    """Test fixed additional load can use end_time instead of duration."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [{"name": "cooking", "start_time": "18:00", "end_time": "19:30", "energy": 1.2}]
    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    for minute in [18 * 60, 18 * 60 + 30, 19 * 60]:
        failed |= check_slot(load_adjust, minute, 0.4, "end time without duration")
    if forecasts.get("cooking", {}).get("slots") != 3:
        print("ERROR: end_time without duration should create 3 slots")
        failed = 1
    return failed


def test_additional_load_slot_energy_weighting(my_predbat):
    """Test advanced slot energy weighting multiplies the per-slot energy."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [{"name": "heating", "start_time": "20:00", "duration": 2.0, "slot_energy": 0.5, "weighting": "2,2,*"}]
    load_adjust, _ = my_predbat.fetch_additional_load_forecast()
    for minute, expected in [(1200, 1.0), (1230, 1.0), (1260, 0.5), (1290, 0.5)]:
        failed |= check_slot(load_adjust, minute, expected, "slot energy weighting")
    return failed


def test_additional_load_dishwasher_total_energy(my_predbat):
    """Test dishwasher total energy is distributed across plan slots."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.plan_interval_minutes = 15
    my_predbat.args["plan_interval_minutes"] = 15
    my_predbat.args["house_load_additional_forecast"] = [{"name": "dishwasher", "start_time": "20:00", "duration": 2.0, "energy": 1.2}]
    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    for minute in range(20 * 60, 22 * 60, 15):
        failed |= check_slot(load_adjust, minute, 0.15, "dishwasher total energy")
    forecast = forecasts.get("dishwasher", {})
    if forecast.get("load_mode") != "total_energy" or forecast.get("slots") != 8 or forecast.get("total_energy") != 1.2:
        print("ERROR: Dishwasher should keep 1.2kWh over eight 15-minute slots")
        failed = 1
    return failed


def test_additional_load_dishwasher_total_energy_weighting(my_predbat):
    """Test total energy weighting redistributes, rather than increases, energy."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [{"name": "dishwasher", "start_time": "20:00", "duration": 2.0, "energy": 1.2, "weighting": "2,2,*"}]
    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    for minute, expected in [(1200, 0.4), (1230, 0.4), (1260, 0.2), (1290, 0.2)]:
        failed |= check_slot(load_adjust, minute, expected, "dishwasher total energy weighting")
    if forecasts.get("dishwasher", {}).get("total_energy") != 1.2:
        print("ERROR: Dishwasher weighted total energy should remain 1.2")
        failed = 1
    return failed


def test_additional_load_is_not_scaled_with_historical_load(my_predbat):
    """Test known additional loads are added after historical load scaling."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.forecast_minutes = 60
    my_predbat.load_forecast_only = True
    my_predbat.load_scaling_dynamic = {}
    my_predbat.dynamic_load_baseline = {}
    my_predbat.load_inday_adjustment = 1.0
    my_predbat.load_minutes = {}
    my_predbat.load_forecast = {}
    base_step = my_predbat.step_data_history(
        {},
        my_predbat.minutes_now,
        forward=False,
        scale_today=1.0,
        scale_fixed=1.2,
        type_load=True,
        load_forecast={},
        load_scaling_dynamic={},
        load_adjust={},
        load_baseline={},
    )
    load_step = my_predbat.add_additional_load_to_step_data(base_step, {minute: 0.6 for minute in range(my_predbat.minutes_now, my_predbat.minutes_now + 30)})
    if load_step.get(0) != 0.1:
        print("ERROR: Additional load should add exact 0.1kWh after historical scaling")
        failed = 1
    return failed


def test_additional_load_partial_duration_keeps_total_energy(my_predbat):
    """Test partial final slots preserve the configured total energy."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [{"name": "dishwasher", "start_time": "20:00", "duration": 1.25, "energy": 1.2}]
    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    for minute, expected in [(1200, 0.4), (1230, 0.4), (1260, 0.8)]:
        failed |= check_slot(load_adjust, minute, expected, "partial duration")
    forecast = forecasts.get("dishwasher", {})
    if forecast.get("total_energy") != 1.2 or round(sum(slot["energy"] for slot in forecast.get("target_times", [])), 4) != 1.2:
        print("ERROR: Partial duration should preserve 1.2kWh in prediction and target slots")
        failed = 1
    return failed


def test_additional_load_duration_rounding_keeps_final_slot(my_predbat):
    """Test float round-off cannot remove the final scheduling slot."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.plan_interval_minutes = 5
    my_predbat.args["plan_interval_minutes"] = 5
    my_predbat.args["house_load_additional_forecast"] = [{"name": "dishwasher", "start_time": "12:00", "duration": 4.1, "slot_energy": 0.1}]
    _, forecasts = my_predbat.fetch_additional_load_forecast()
    forecast = forecasts.get("dishwasher", {})
    slots = forecast.get("target_times", [])
    if forecast.get("slots") != 50 or forecast.get("total_energy") != 5.0 or not slots or "T16:06:00" not in slots[-1].get("end", ""):
        print("ERROR: 4.1h at 5-minute intervals must keep the final one-minute slot and 5kWh")
        failed = 1
    return failed


def test_additional_load_multiple_and_api_override(my_predbat):
    """Test multiple loads add together and API override updates one named load."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher", "start_time": "20:00", "duration": 2.0, "energy": 2.0},
        {"name": "heating", "start_time": "20:30", "duration": 1.0, "energy": 0.5},
    ]
    load_adjust, _ = my_predbat.fetch_additional_load_forecast()
    for minute, expected in [(1200, 0.5), (1230, 0.75), (1260, 0.75)]:
        failed |= check_slot(load_adjust, minute, expected, "multiple loads")
    my_predbat.api_select("load_forecast_delta_api", "dishwasher?start_time=18:00&duration=1.0&energy=0.8")
    load_adjust, _ = my_predbat.fetch_additional_load_forecast()
    for minute, expected in [(1080, 0.4), (1110, 0.4), (1200, 0.0), (1230, 0.25)]:
        failed |= check_slot(load_adjust, minute, expected, "API override")
    my_predbat.api_select("load_forecast_delta_api", "off")
    return failed


def test_additional_load_select_api_and_events(my_predbat):
    """Test select events publish weighted forecasts immediately, and both removal forms clear metadata."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = []
    command = "dishwasher?start_time=18:00&duration=2.0&energy=1.2&weighting=2|2|*"
    run_async(my_predbat.trigger_callback({"domain": "select", "service": "select_option", "service_data": {"entity_id": "select.predbat_load_forecast_delta_api", "option": command}}))
    for minute, expected in [(1080, 0.4), (1110, 0.4), (1140, 0.2), (1170, 0.2)]:
        failed |= check_slot(my_predbat.house_load_additional_forecast_adjust, minute, expected, "select event")
    sensor = my_predbat.dashboard_values.get("binary_sensor.predbat_load_forecast_delta_dishwasher", {})
    if sensor.get("state") != "on" or sensor.get("attributes", {}).get("total_energy") != 1.2:
        print("ERROR: Select event should immediately publish the full weighted forecast")
        failed = 1
    stored = my_predbat.api_select_update("load_forecast_delta_api")[0]
    my_predbat.house_load_additional_forecast_overrides["dishwasher"]["stale_metadata"] = True
    my_predbat.api_select("load_forecast_delta_api", "[{}]".format(stored))
    my_predbat.refresh_additional_load_forecast_api()
    if my_predbat.api_select_update("load_forecast_delta_api") or my_predbat.house_load_additional_forecast_overrides or my_predbat.house_load_additional_forecasts:
        print("ERROR: Bracket removal should clear command, metadata and forecast")
        failed = 1
    my_predbat.api_select("load_forecast_delta_api", command)
    my_predbat.api_select("load_forecast_delta_api", "dryer?start_time=21:00&duration=1&energy=0.5")
    my_predbat.refresh_additional_load_forecast_api()
    my_predbat.api_select("load_forecast_delta_api", "off")
    if my_predbat.api_select_update("load_forecast_delta_api") or my_predbat.house_load_additional_forecast_overrides:
        print("ERROR: Selecting off should clear every command and runtime override")
        failed = 1
    return failed


def test_additional_load_delete_buttons(my_predbat):
    """Test ordinary, sanitised and internal-delete names remove only their own forecast and entities."""
    failed = 0
    for name, suffix in [("dishwasher", "dishwasher"), ("Dishwasher Eco", "dishwasher_eco"), ("auto_delete_timer", "auto_delete_timer")]:
        configure_additional_load_test(my_predbat)
        my_predbat.args["house_load_additional_forecast"] = []
        my_predbat.api_select("load_forecast_delta_api", name + "?start_time=20:00&duration=1&energy=0.4")
        my_predbat.api_select("load_forecast_delta_api", "other_load?start_time=21:00&duration=1&energy=0.5")
        my_predbat.refresh_additional_load_forecast_api()
        button = "button.predbat_load_forecast_delta_" + suffix + "_delete"
        sensor = "binary_sensor.predbat_load_forecast_delta_" + suffix
        if my_predbat.dashboard_values.get(button, {}).get("state") != "idle":
            print("ERROR: Dynamic load should publish its delete button")
            failed = 1
        run_async(my_predbat.trigger_callback({"domain": "button", "service": "press", "service_data": {"entity_id": button}}))
        remaining = {my_predbat.additional_load_command_name(value) for value in my_predbat.api_select_update("load_forecast_delta_api")}
        if remaining != {"other_load"} or button in my_predbat.dashboard_values or sensor in my_predbat.dashboard_values:
            print("ERROR: Delete should remove only {} and both its entities".format(name))
            failed = 1
        if not {button, sensor}.issubset(set(my_predbat.ha_interface.delete_state_calls)):
            print("ERROR: Delete should reach the HA interface for both entities")
            failed = 1
        my_predbat.api_select("load_forecast_delta_api", "off")
    return failed


def test_additional_load_rejects_duplicate_safe_names(my_predbat):
    """Test YAML and API names cannot collide after Home Assistant sanitising."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "Dishwasher Eco", "start_time": "20:00", "duration": 1.0, "energy": 0.4},
        {"name": "dishwasher-eco", "start_time": "21:00", "duration": 1.0, "energy": 0.5},
    ]
    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    if set(forecasts) != {"Dishwasher Eco"}:
        print("ERROR: Duplicate safe names should keep only the first forecast")
        failed = 1
    failed |= check_slot(load_adjust, 1200, 0.2, "duplicate kept first")
    failed |= check_slot(load_adjust, 1260, 0.0, "duplicate skipped second")
    my_predbat.api_select("load_forecast_delta_api", "dishwasher-eco?start_time=22:00&duration=1&energy=0.6")
    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    if set(forecasts) != {"Dishwasher Eco"} or load_adjust.get(1320, 0.0):
        print("ERROR: API safe-name collision should be skipped")
        failed = 1
    my_predbat.api_select("load_forecast_delta_api", "off")
    return failed


def test_additional_load_publishing_and_expiry(my_predbat):
    """Test static buttons, empty placeholders, runtime expiry and expired startup entity cleanup."""
    failed = 0
    configure_additional_load_test(my_predbat)
    button = "button.predbat_load_forecast_delta_dishwasher_delete"
    sensor = "binary_sensor.predbat_load_forecast_delta_dishwasher"
    my_predbat.dashboard_values.pop(button, None)
    my_predbat.args["house_load_additional_forecast"] = [{"name": "dishwasher", "start_time": "20:00", "duration": 2, "energy": 1.2}]
    my_predbat.refresh_additional_load_forecast_api()
    if button in my_predbat.dashboard_values:
        print("ERROR: Static YAML loads should not have delete buttons")
        failed = 1
    my_predbat.args["house_load_additional_forecast"] = [{"name": "dishwasher"}]
    my_predbat.refresh_additional_load_forecast_api()
    if sensor in my_predbat.dashboard_values:
        print("ERROR: Empty YAML placeholders should not publish dead sensors")
        failed = 1
    my_predbat.args["house_load_additional_forecast"] = []
    my_predbat.api_select("load_forecast_delta_api", "dishwasher?start_time=18:00&duration=1&energy=0.8")
    my_predbat.refresh_additional_load_forecast_api()
    failed |= check_slot(my_predbat.house_load_additional_forecast_adjust, 1080, 0.4, "before expiry")
    my_predbat.minutes_now = 1140
    my_predbat.refresh_additional_load_forecast_api()
    if my_predbat.api_select_update("load_forecast_delta_api") or my_predbat.house_load_additional_forecasts or sensor in my_predbat.dashboard_values or button in my_predbat.dashboard_values:
        print("ERROR: Expiry should remove the request, forecast and both entities")
        failed = 1
    my_predbat.config_index["load_forecast_delta_api"]["value"] = "+dishwasher?start_time=08:00&duration=1&energy=0.8&_expires_at=" + my_predbat.additional_load_minutes_to_stamp(540)
    my_predbat.dashboard_values[sensor] = {"state": "on", "attributes": {}}
    my_predbat.dashboard_values[button] = {"state": "idle", "attributes": {}}
    my_predbat.dashboard_index.extend([sensor, button])
    my_predbat.minutes_now = 600
    my_predbat.refresh_additional_load_forecast_api()
    if sensor in my_predbat.dashboard_values or button in my_predbat.dashboard_values:
        print("ERROR: Expired requests discovered on startup should unpublish both entities")
        failed = 1
    my_predbat.update_pending = False
    my_predbat.plan_valid = True
    run_async(my_predbat.trigger_callback({"domain": "button", "service": "press", "service_data": {"entity_id": button}}))
    if my_predbat.update_pending or not my_predbat.plan_valid:
        print("ERROR: A stale delete button should not trigger a replan")
        failed = 1
    return failed


def test_additional_load_fixed_metadata(my_predbat):
    """Test omitted API starts survive refreshes and unsupported scheduling modes are rejected."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = []
    my_predbat.api_select("load_forecast_delta_api", "heating?duration=2&energy=1.2")
    my_predbat.refresh_additional_load_forecast_api()
    first = my_predbat.house_load_additional_forecasts["heating"]["requested_start"]
    stored = my_predbat.api_select_update("load_forecast_delta_api")[0]
    my_predbat.house_load_additional_forecast_overrides = {}
    my_predbat.minutes_now = 630
    my_predbat.refresh_additional_load_forecast_api()
    if my_predbat.house_load_additional_forecasts["heating"]["requested_start"] != first or "_requested_start=" not in stored:
        print("ERROR: Omitted fixed API start should remain frozen across refresh/restart")
        failed = 1
    my_predbat.api_select("load_forecast_delta_api", "off")
    my_predbat.args["house_load_additional_forecast"] = [{"name": "unsupported", "mode": "flexible", "start_time": "20:00", "duration": 1, "energy": 1}]
    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    if load_adjust or forecasts:
        print("ERROR: Fixed-only forecasts must reject unsupported scheduling modes")
        failed = 1
    naive = (my_predbat.now_utc - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    if my_predbat.additional_load_stamp_to_minutes(naive) is not None:
        print("ERROR: API timestamps without a timezone should be rejected")
        failed = 1
    return failed


def test_additional_load_textual_plan_summary(my_predbat):
    """Test textual plans include confirmed fixed loads and omit empty profiles."""
    configure_additional_load_test(my_predbat)
    my_predbat.house_load_additional_forecasts = {
        "heating": {"enabled": True, "total_energy": 2.0, "target_times": [{"start": "2026-05-07T17:00:00+02:00", "end": "2026-05-07T19:00:00+02:00", "energy": 2.0}]},
        "empty": {"enabled": True, "total_energy": 0.0, "target_times": []},
    }
    text = my_predbat.get_additional_load_text()
    my_predbat.house_load_additional_forecasts = {}
    if "heating from 17:00 to 19:00 using 2.00 kWh is planned" not in text or "empty" in text:
        print("ERROR: Fixed textual plan should describe only confirmed target slots")
        return 1
    return 0


def run_additional_load_forecast_tests(my_predbat):
    """Run named fixed-load forecast regressions."""
    failed = 0
    for test in [
        test_additional_load_disabled,
        test_additional_load_enabled_false_profile,
        test_additional_load_enabled_string_profile,
        test_additional_load_dishwasher_simple,
        test_additional_load_end_time_without_duration,
        test_additional_load_slot_energy_weighting,
        test_additional_load_dishwasher_total_energy,
        test_additional_load_dishwasher_total_energy_weighting,
        test_additional_load_is_not_scaled_with_historical_load,
        test_additional_load_partial_duration_keeps_total_energy,
        test_additional_load_duration_rounding_keeps_final_slot,
        test_additional_load_multiple_and_api_override,
        test_additional_load_select_api_and_events,
        test_additional_load_delete_buttons,
        test_additional_load_rejects_duplicate_safe_names,
        test_additional_load_publishing_and_expiry,
        test_additional_load_fixed_metadata,
        test_additional_load_textual_plan_summary,
    ]:
        failed |= test(my_predbat)
    return failed
