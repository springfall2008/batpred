# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for named additional house load forecasts."""

from datetime import timedelta

import plan as plan_module
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
    my_predbat.house_load_additional_history = []
    my_predbat.house_load_additional_history_loaded = True


def configure_additional_load_rates(my_predbat, cheap_start, cheap_end):
    """Configure deterministic import rates for flexible load tests."""
    my_predbat.rate_import = {minute: 30.0 for minute in range(0, 3 * 24 * 60)}
    for minute in range(cheap_start, cheap_end):
        my_predbat.rate_import[minute] = 5.0


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
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher", "start_time": "20:00", "duration": 0, "energy": 1.2},
    ]

    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    if load_adjust:
        print("ERROR: Disabled additional load should not produce adjustments, got {}".format(load_adjust))
        failed = 1
    if forecasts.get("dishwasher", {}).get("state") != "off":
        print("ERROR: Disabled additional load should publish off state")
        failed = 1
    return failed


def test_additional_load_enabled_false_profile(my_predbat):
    """Test enabled false publishes a disabled profile without load adjustment."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher", "enabled": False, "start_time": "20:00", "duration": 2.0, "energy": 1.2},
    ]

    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    if load_adjust:
        print("ERROR: enabled false profile should not produce adjustments, got {}".format(load_adjust))
        failed = 1
    forecast = forecasts.get("dishwasher", {})
    if forecast.get("state") != "off" or forecast.get("enabled"):
        print("ERROR: enabled false profile should publish off and enabled false, got {}".format(forecast))
        failed = 1
    return failed


def test_additional_load_dishwasher_simple(my_predbat):
    """Test a simple dishwasher total energy forecast."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher", "start_time": "20:00", "duration": 2.0, "energy": 2.0},
    ]

    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    for minute in [20 * 60, 20 * 60 + 30, 21 * 60, 21 * 60 + 30]:
        failed |= check_slot(load_adjust, minute, 0.5, "dishwasher simple")
    failed |= check_slot(load_adjust, 22 * 60, 0.0, "dishwasher simple end")
    if forecasts.get("dishwasher", {}).get("state") != "on":
        print("ERROR: Dishwasher additional load should publish on state")
        failed = 1
    if len(forecasts.get("dishwasher", {}).get("target_times", [])) != 4:
        print("ERROR: Dishwasher target_times should contain 4 slots")
        failed = 1
    my_predbat.house_load_additional_forecasts = forecasts
    my_predbat.publish_additional_load_forecasts()
    sensor = my_predbat.dashboard_values.get("binary_sensor.predbat_load_forecast_delta_dishwasher", {})
    if sensor.get("state") != "on":
        print("ERROR: Dishwasher binary sensor should be published on")
        failed = 1
    if len(sensor.get("attributes", {}).get("target_times", [])) != 4:
        print("ERROR: Dishwasher binary sensor should publish target_times")
        failed = 1
    return failed


def test_additional_load_end_time_without_duration(my_predbat):
    """Test fixed additional load can use end_time instead of duration."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "cooking", "start_time": "18:00", "end_time": "19:30", "energy": 1.2},
    ]

    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    failed |= check_slot(load_adjust, 18 * 60, 0.4, "end time without duration")
    failed |= check_slot(load_adjust, 18 * 60 + 30, 0.4, "end time without duration")
    failed |= check_slot(load_adjust, 19 * 60, 0.4, "end time without duration")
    if forecasts.get("cooking", {}).get("slots") != 3:
        print("ERROR: end_time without duration should create 3 slots")
        failed = 1
    return failed


def test_additional_load_slot_energy_weighting(my_predbat):
    """Test advanced slot energy weighting multiplies the per-slot energy."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "heating", "start_time": "20:00", "duration": 2.0, "slot_energy": 0.5, "weighting": "2,2,*"},
    ]

    load_adjust, _ = my_predbat.fetch_additional_load_forecast()
    failed |= check_slot(load_adjust, 20 * 60, 1.0, "slot energy weighting")
    failed |= check_slot(load_adjust, 20 * 60 + 30, 1.0, "slot energy weighting")
    failed |= check_slot(load_adjust, 21 * 60, 0.5, "slot energy weighting")
    failed |= check_slot(load_adjust, 21 * 60 + 30, 0.5, "slot energy weighting")
    return failed


def test_additional_load_dishwasher_total_energy(my_predbat):
    """Test dishwasher total energy is distributed across plan slots."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.plan_interval_minutes = 15
    my_predbat.args["plan_interval_minutes"] = 15
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher", "start_time": "20:00", "duration": 2.0, "energy": 1.2},
    ]

    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    for minute in [20 * 60, 20 * 60 + 15, 20 * 60 + 30, 20 * 60 + 45, 21 * 60, 21 * 60 + 15, 21 * 60 + 30, 21 * 60 + 45]:
        failed |= check_slot(load_adjust, minute, 0.15, "dishwasher total energy")
    forecast = forecasts.get("dishwasher", {})
    if forecast.get("load_mode") != "total_energy":
        print("ERROR: Dishwasher energy mode should be total_energy")
        failed = 1
    if forecast.get("slots") != 8:
        print("ERROR: Dishwasher energy mode should create 8 slots, got {}".format(forecast.get("slots")))
        failed = 1
    if forecast.get("total_energy") != 1.2:
        print("ERROR: Dishwasher total energy should be 1.2, got {}".format(forecast.get("total_energy")))
        failed = 1
    return failed


def test_additional_load_dishwasher_total_energy_weighting(my_predbat):
    """Test total energy weighting redistributes, rather than increases, energy."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher", "start_time": "20:00", "duration": 2.0, "energy": 1.2, "weighting": "2,2,*"},
    ]

    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    failed |= check_slot(load_adjust, 20 * 60, 0.4, "dishwasher total energy weighting")
    failed |= check_slot(load_adjust, 20 * 60 + 30, 0.4, "dishwasher total energy weighting")
    failed |= check_slot(load_adjust, 21 * 60, 0.2, "dishwasher total energy weighting")
    failed |= check_slot(load_adjust, 21 * 60 + 30, 0.2, "dishwasher total energy weighting")
    if forecasts.get("dishwasher", {}).get("total_energy") != 1.2:
        print("ERROR: Dishwasher weighted total energy should remain 1.2")
        failed = 1
    return failed


def test_additional_load_partial_duration_keeps_total_energy(my_predbat):
    """Test partial final slots preserve the configured total energy."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher", "start_time": "20:00", "duration": 1.25, "energy": 1.2},
    ]

    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    failed |= check_slot(load_adjust, 20 * 60, 0.4, "partial duration first slot")
    failed |= check_slot(load_adjust, 20 * 60 + 30, 0.4, "partial duration second slot")
    failed |= check_slot(load_adjust, 21 * 60, 0.8, "partial duration final half slot")
    forecast = forecasts.get("dishwasher", {})
    target_total = round(sum(slot.get("energy", 0.0) for slot in forecast.get("target_times", [])), 4)
    if forecast.get("total_energy") != 1.2 or target_total != 1.2:
        print("ERROR: Partial duration should publish the configured total energy, got forecast {} target total {}".format(forecast, target_total))
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
    failed |= check_slot(load_adjust, 20 * 60, 0.5, "multiple loads dishwasher")
    failed |= check_slot(load_adjust, 20 * 60 + 30, 0.75, "multiple loads overlap")
    failed |= check_slot(load_adjust, 21 * 60, 0.75, "multiple loads overlap")

    my_predbat.api_select("load_forecast_delta_api", "dishwasher?start_time=18:00&duration=1.0&energy=0.8")
    load_adjust, _ = my_predbat.fetch_additional_load_forecast()
    failed |= check_slot(load_adjust, 18 * 60, 0.4, "api override dishwasher")
    failed |= check_slot(load_adjust, 18 * 60 + 30, 0.4, "api override dishwasher")
    failed |= check_slot(load_adjust, 20 * 60, 0.0, "api override removed old dishwasher")
    failed |= check_slot(load_adjust, 20 * 60 + 30, 0.25, "api override kept heating")
    my_predbat.api_select("load_forecast_delta_api", "off")
    my_predbat.house_load_additional_forecast_overrides = {}
    return failed


def test_additional_load_select_api_override(my_predbat):
    """Test standard HA select API updates a named load forecast."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher", "start_time": "20:00", "duration": 0, "energy": 0},
    ]

    my_predbat.api_select("load_forecast_delta_api", "dishwasher?start_time=18:00&duration=2.0&energy=1.2")
    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    failed |= check_slot(load_adjust, 18 * 60, 0.3, "select API dishwasher")
    failed |= check_slot(load_adjust, 18 * 60 + 30, 0.3, "select API dishwasher")
    failed |= check_slot(load_adjust, 19 * 60, 0.3, "select API dishwasher")
    failed |= check_slot(load_adjust, 19 * 60 + 30, 0.3, "select API dishwasher")
    forecast = forecasts.get("dishwasher", {})
    if forecast.get("state") != "on" or forecast.get("total_energy") != 1.2:
        print("ERROR: Select API dishwasher forecast not enabled correctly: {}".format(forecast))
        failed = 1

    my_predbat.api_select("load_forecast_delta_api", "off")
    my_predbat.house_load_additional_forecast_overrides = {}
    return failed


def test_additional_load_select_api_weighting(my_predbat):
    """Test select API accepts pipe-separated weighting."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = []

    my_predbat.api_select("load_forecast_delta_api", "dishwasher?start_time=18:00&duration=2.0&energy=1.2&weighting=2|2|*")
    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    failed |= check_slot(load_adjust, 18 * 60, 0.4, "select API weighting dishwasher")
    failed |= check_slot(load_adjust, 18 * 60 + 30, 0.4, "select API weighting dishwasher")
    failed |= check_slot(load_adjust, 19 * 60, 0.2, "select API weighting dishwasher")
    failed |= check_slot(load_adjust, 19 * 60 + 30, 0.2, "select API weighting dishwasher")
    if forecasts.get("dishwasher", {}).get("total_energy") != 1.2:
        print("ERROR: Select API weighted dishwasher total energy should remain 1.2")
        failed = 1

    my_predbat.api_select("load_forecast_delta_api", "off")
    my_predbat.house_load_additional_forecast_overrides = {}
    return failed


def test_additional_load_select_event_updates_adjustment(my_predbat):
    """Test HA select event immediately rebuilds additional load adjustment."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher", "start_time": "20:00", "duration": 0, "energy": 0},
    ]

    service_data = {
        "domain": "select",
        "service": "select_option",
        "service_data": {
            "entity_id": "select.predbat_load_forecast_delta_api",
            "option": "dishwasher?start_time=18:00&duration=2.0&energy=1.2",
        },
    }
    run_async(my_predbat.trigger_callback(service_data))

    failed |= check_slot(my_predbat.house_load_additional_forecast_adjust, 18 * 60, 0.3, "select event immediate adjustment")
    failed |= check_slot(my_predbat.house_load_additional_forecast_adjust, 19 * 60 + 30, 0.3, "select event immediate adjustment")
    sensor = my_predbat.dashboard_values.get("binary_sensor.predbat_load_forecast_delta_dishwasher", {})
    attributes = sensor.get("attributes", {})
    if sensor.get("state") != "on" or attributes.get("total_energy") != 1.2:
        print("ERROR: Select event should immediately publish dishwasher forecast, got {}".format(sensor))
        failed = 1

    my_predbat.api_select("load_forecast_delta_api", "off")
    my_predbat.house_load_additional_forecast_overrides = {}
    return failed


def test_additional_load_delete_button_removes_api_forecast(my_predbat):
    """Test delete button removes a one-shot API forecast."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = []
    my_predbat.api_select("load_forecast_delta_api", "dishwasher?start_time=20:00&duration=2.0&energy=1.2")
    my_predbat.refresh_additional_load_forecast_api()

    button = my_predbat.dashboard_values.get("button.predbat_load_forecast_delta_dishwasher_delete", {})
    if button.get("state") != "idle":
        print("ERROR: Dishwasher delete button should publish idle, got {}".format(button))
        failed = 1

    service_data = {
        "domain": "button",
        "service": "press",
        "service_data": {"entity_id": "button.predbat_load_forecast_delta_dishwasher_delete"},
    }
    run_async(my_predbat.trigger_callback(service_data))
    failed |= check_slot(my_predbat.house_load_additional_forecast_adjust, 20 * 60, 0.0, "delete button removed dishwasher")
    if my_predbat.api_select_update("load_forecast_delta_api"):
        print("ERROR: Delete button should clear API forecast")
        failed = 1
    if "binary_sensor.predbat_load_forecast_delta_dishwasher" in my_predbat.dashboard_values:
        print("ERROR: Delete button should remove dishwasher binary sensor")
        failed = 1
    if "button.predbat_load_forecast_delta_dishwasher_delete" in my_predbat.dashboard_values:
        print("ERROR: Delete button should remove dishwasher delete button")
        failed = 1

    my_predbat.house_load_additional_forecast_overrides = {}
    return failed


def test_additional_load_delete_button_removes_sanitized_api_forecast(my_predbat):
    """Test delete buttons remove API forecasts whose names require entity sanitizing."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = []
    my_predbat.api_select("load_forecast_delta_api", "Dishwasher Eco?start_time=20:00&duration=2.0&energy=1.2")
    my_predbat.refresh_additional_load_forecast_api()

    service_data = {
        "domain": "button",
        "service": "press",
        "service_data": {"entity_id": "button.predbat_load_forecast_delta_dishwasher_eco_delete"},
    }
    run_async(my_predbat.trigger_callback(service_data))
    if my_predbat.api_select_update("load_forecast_delta_api") or my_predbat.house_load_additional_forecasts:
        print("ERROR: Sanitized delete button should remove API forecast, got forecasts {} api {}".format(my_predbat.house_load_additional_forecasts, my_predbat.api_select_update("load_forecast_delta_api")))
        failed = 1
    if "button.predbat_load_forecast_delta_dishwasher_eco_delete" in my_predbat.dashboard_values:
        print("ERROR: Sanitized delete button should be unpublished")
        failed = 1

    my_predbat.house_load_additional_forecast_overrides = {}
    return failed


def test_additional_load_yaml_does_not_publish_delete_button(my_predbat):
    """Test YAML forecasts do not get one-shot delete buttons."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.dashboard_values.pop("button.predbat_load_forecast_delta_dishwasher_delete", None)
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher", "start_time": "20:00", "duration": 2.0, "energy": 1.2},
    ]
    my_predbat.refresh_additional_load_forecast_api()

    if "button.predbat_load_forecast_delta_dishwasher_delete" in my_predbat.dashboard_values:
        print("ERROR: YAML forecast should not publish delete button")
        failed = 1

    my_predbat.house_load_additional_forecast_overrides = {}
    return failed


def test_additional_load_api_forecast_auto_expires(my_predbat):
    """Test one-shot API forecasts are removed after their finish time."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = []
    my_predbat.api_select("load_forecast_delta_api", "dishwasher?start_time=18:00&duration=1.0&energy=0.8")
    my_predbat.refresh_additional_load_forecast_api()
    failed |= check_slot(my_predbat.house_load_additional_forecast_adjust, 18 * 60, 0.4, "api forecast before expiry")

    my_predbat.minutes_now = 19 * 60
    my_predbat.refresh_additional_load_forecast_api()
    if my_predbat.api_select_update("load_forecast_delta_api"):
        print("ERROR: API forecast should be removed after expiry")
        failed = 1
    if my_predbat.house_load_additional_forecasts:
        print("ERROR: Expired API forecast should not remain active, got {}".format(my_predbat.house_load_additional_forecasts))
        failed = 1
    if "binary_sensor.predbat_load_forecast_delta_dishwasher" in my_predbat.dashboard_values:
        print("ERROR: Expired API forecast should remove dishwasher binary sensor")
        failed = 1
    if "button.predbat_load_forecast_delta_dishwasher_delete" in my_predbat.dashboard_values:
        print("ERROR: Expired API forecast should remove dishwasher delete button")
        failed = 1

    my_predbat.house_load_additional_forecast_overrides = {}
    return failed


def test_additional_load_yaml_placeholder_not_published(my_predbat):
    """Test empty YAML placeholders do not publish dead forecast entities."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher"},
    ]
    my_predbat.refresh_additional_load_forecast_api()

    if "binary_sensor.predbat_load_forecast_delta_dishwasher" in my_predbat.dashboard_values:
        print("ERROR: Empty YAML placeholder should not publish dishwasher binary sensor")
        failed = 1

    my_predbat.house_load_additional_forecast_overrides = {}
    return failed


def test_additional_load_stale_delete_button_no_replan(my_predbat):
    """Test stale delete button press does not invalidate a plan."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = []
    my_predbat.dashboard_values["binary_sensor.predbat_load_forecast_delta_dishwasher"] = {"state": "off", "attributes": {}}
    my_predbat.dashboard_values["button.predbat_load_forecast_delta_dishwasher_delete"] = {"state": "idle", "attributes": {}}
    my_predbat.dashboard_index.append("binary_sensor.predbat_load_forecast_delta_dishwasher")
    my_predbat.dashboard_index.append("button.predbat_load_forecast_delta_dishwasher_delete")
    my_predbat.update_pending = False
    my_predbat.plan_valid = True

    service_data = {
        "domain": "button",
        "service": "press",
        "service_data": {"entity_id": "button.predbat_load_forecast_delta_dishwasher_delete"},
    }
    run_async(my_predbat.trigger_callback(service_data))
    if my_predbat.update_pending or not my_predbat.plan_valid:
        print("ERROR: Stale delete button should not invalidate plan")
        failed = 1
    if "binary_sensor.predbat_load_forecast_delta_dishwasher" in my_predbat.dashboard_values:
        print("ERROR: Stale delete button should remove stale dishwasher binary sensor")
        failed = 1
    if "button.predbat_load_forecast_delta_dishwasher_delete" in my_predbat.dashboard_values:
        print("ERROR: Stale delete button should remove stale dishwasher delete button")
        failed = 1

    my_predbat.house_load_additional_forecast_overrides = {}
    return failed


def test_additional_load_flexible_api_selection_survives_refresh(my_predbat):
    """Test selected flexible API metadata augments, not replaces, the API command."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.plan_interval_minutes = 15
    my_predbat.args["plan_interval_minutes"] = 15
    my_predbat.args["house_load_additional_forecast"] = []
    my_predbat.api_select("load_forecast_delta_api", "dishwasher?enabled=true&mode=flexible&end_time=22:00&duration=2.0&energy=1.2")
    my_predbat.house_load_additional_forecast_overrides["dishwasher"] = {
        "name": "dishwasher",
        "_selected_start_minutes": 11 * 60 + 15,
        "_selection_reason": "prediction_metric",
        "_candidate_count": 50,
        "_selected_metric": 1615.32,
        "_baseline_metric": 1600.0,
        "_expires_minutes": 13 * 60 + 15,
    }
    my_predbat.refresh_additional_load_forecast_api()

    forecast = my_predbat.house_load_additional_forecasts.get("dishwasher", {})
    if forecast.get("source") != "api" or not forecast.get("auto_expire"):
        print("ERROR: Flexible API forecast should keep API source after selection refresh, got {}".format(forecast))
        failed = 1
    if forecast.get("mode") != "flexible" or forecast.get("energy") != 1.2 or forecast.get("duration") != 2.0:
        print("ERROR: Flexible API forecast should keep command fields after selection refresh, got {}".format(forecast))
        failed = 1
    if forecast.get("state") != "off" or forecast.get("slots") != 0 or forecast.get("target_times") or forecast.get("selection_locked"):
        print("ERROR: Flexible API forecast before suggested start should publish suggestion only, got {}".format(forecast))
        failed = 1
    if "T11:15:00" not in forecast.get("suggested_start", "") or "T13:15:00" not in forecast.get("suggested_end", ""):
        print("ERROR: Flexible API forecast should publish selected window after refresh, got {}".format(forecast))
        failed = 1
    my_predbat.api_select("load_forecast_delta_api", "off")
    my_predbat.house_load_additional_forecast_overrides = {}
    return failed


def test_additional_load_flexible_api_reselects_before_suggested_start(my_predbat):
    """Test selected flexible API forecasts can be reselected before their suggested start."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.minutes_now = 10 * 60
    my_predbat.plan_interval_minutes = 15
    my_predbat.args["plan_interval_minutes"] = 15
    my_predbat.args["house_load_additional_forecast"] = []
    my_predbat.api_select("load_forecast_delta_api", "dishwasher?enabled=true&mode=flexible&start_time=10:00&end_time=22:00&duration=2.0&energy=1.2")
    my_predbat.house_load_additional_forecast_overrides["dishwasher"] = {
        "name": "dishwasher",
        "_selected_start_minutes": 18 * 60,
        "_selection_reason": "prediction_metric",
        "_candidate_count": 20,
        "_selected_metric": 200.0,
        "_baseline_metric": 100.0,
        "_expires_minutes": 20 * 60,
    }
    my_predbat.refresh_additional_load_forecast_api()

    forecast = my_predbat.house_load_additional_forecasts.get("dishwasher", {})
    if "T18:00:00" not in forecast.get("suggested_start", "") or forecast.get("target_times") or forecast.get("selection_locked"):
        print("ERROR: Pre-start selected flexible API forecast should be suggestion only before reselection, got {}".format(forecast))
        failed = 1

    my_predbat.charge_limit_best = []
    my_predbat.charge_window_best = []
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    my_predbat.end_record = my_predbat.forecast_minutes
    original_prediction = plan_module.Prediction

    class FakePrediction:
        """Fake prediction scores 12:00 as cheapest regardless of the previous suggestion."""

        def __init__(self, base, pv_step, pv10_step, load_step, load10_step):
            """Store load step data."""
            self.load_step = load_step

        def run_prediction(self, charge_limit, charge_window, export_window, export_limits, pv10, end_record):
            """Return a metric based on when the injected load appears."""
            first_load_minute = None
            for minute, load in self.load_step.items():
                if load > 0:
                    first_load_minute = my_predbat.minutes_now + minute if first_load_minute is None else min(first_load_minute, my_predbat.minutes_now + minute)
            metric = abs(first_load_minute - 12 * 60) if first_load_minute is not None else 1000.0
            return (metric, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    try:
        plan_module.Prediction = FakePrediction
        selected, load_step, _ = my_predbat.select_flexible_additional_loads({}, {}, {}, {})
    finally:
        plan_module.Prediction = original_prediction

    forecast = my_predbat.house_load_additional_forecasts.get("dishwasher", {})
    if not selected or "T12:00:00" not in forecast.get("suggested_start", "") or forecast.get("target_times") or forecast.get("selection_locked"):
        print("ERROR: Pre-start flexible API forecast should reselect to 12:00 without committing target slots, got {}".format(forecast))
        failed = 1

    my_predbat.api_select("load_forecast_delta_api", "off")
    my_predbat.house_load_additional_forecast_overrides = {}
    return failed


def test_additional_load_flexible_api_stale_selection_not_before_requested_start(my_predbat):
    """Test stale selected flexible metadata is not published before the frozen requested start."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.minutes_now = 12 * 60 + 30
    my_predbat.plan_interval_minutes = 15
    my_predbat.args["plan_interval_minutes"] = 15
    my_predbat.args["house_load_additional_forecast"] = []
    my_predbat.api_select("load_forecast_delta_api", "dishwasher?enabled=true&mode=flexible&end_time=07:00&duration=5.0&energy=0.7")
    my_predbat.house_load_additional_forecast_overrides["dishwasher"] = {
        "name": "dishwasher",
        "_requested_start_minutes": 12 * 60 + 30,
        "_selected_start_minutes": 12 * 60,
        "_selection_reason": "prediction_metric",
        "_candidate_count": 57,
        "_selected_metric": -1737.07,
        "_baseline_metric": -2007.2,
        "_expires_minutes": 17 * 60,
    }
    my_predbat.refresh_additional_load_forecast_api()

    forecast = my_predbat.house_load_additional_forecasts.get("dishwasher", {})
    if "T12:30:00" not in forecast.get("requested_start", "") or "T12:30:00" not in forecast.get("suggested_start", ""):
        print("ERROR: Flexible API stale selection should not start before requested_start, got {}".format(forecast))
        failed = 1
    if forecast.get("total_energy") != 0.7 or forecast.get("slots") != 20 or "T17:30:00" not in forecast.get("expires_at", ""):
        print("ERROR: Flexible API stale selection should keep full shifted load and expiry, got {}".format(forecast))
        failed = 1

    my_predbat.api_select("load_forecast_delta_api", "off")
    my_predbat.house_load_additional_forecast_overrides = {}
    return failed


def test_additional_load_flexible_api_locks_after_suggested_start(my_predbat):
    """Test a selected flexible API forecast locks once the suggested start is reached and then expires."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.minutes_now = 13 * 60
    my_predbat.plan_interval_minutes = 15
    my_predbat.args["plan_interval_minutes"] = 15
    my_predbat.args["house_load_additional_forecast"] = []
    my_predbat.api_select("load_forecast_delta_api", "dishwasher?enabled=true&mode=flexible&end_time=07:00&duration=5.0&energy=0.7")
    my_predbat.house_load_additional_forecast_overrides["dishwasher"] = {
        "name": "dishwasher",
        "_requested_start_minutes": 12 * 60 + 30,
        "_selected_start_minutes": 12 * 60 + 30,
        "_selection_reason": "prediction_metric",
        "_candidate_count": 57,
        "_selected_metric": -1737.07,
        "_baseline_metric": -2007.2,
        "_expires_minutes": 17 * 60 + 30,
    }
    my_predbat.refresh_additional_load_forecast_api()

    forecast = my_predbat.house_load_additional_forecasts.get("dishwasher", {})
    target_times = forecast.get("target_times", [])
    if not forecast.get("selection_locked") or not my_predbat.house_load_additional_forecast_overrides.get("dishwasher", {}).get("_selection_locked"):
        print("ERROR: Flexible API forecast should lock after suggested start, got {}".format(forecast))
        failed = 1
    if "T12:30:00" not in forecast.get("suggested_start", "") or forecast.get("slots") != 18 or forecast.get("total_energy") != 0.63:
        print("ERROR: Locked flexible API forecast should keep original start with remaining slots, got {}".format(forecast))
        failed = 1
    if not target_times or "T13:00:00" not in target_times[0].get("start", ""):
        print("ERROR: Locked flexible API forecast should only publish remaining target slots, got {}".format(target_times))
        failed = 1

    my_predbat.minutes_now = 17 * 60 + 30
    my_predbat.refresh_additional_load_forecast_api()
    if my_predbat.house_load_additional_forecasts or my_predbat.api_select_update("load_forecast_delta_api"):
        print("ERROR: Locked flexible API forecast should expire at suggested end, got forecasts {} api {}".format(my_predbat.house_load_additional_forecasts, my_predbat.api_select_update("load_forecast_delta_api")))
        failed = 1

    my_predbat.api_select("load_forecast_delta_api", "off")
    my_predbat.house_load_additional_forecast_overrides = {}
    return failed


def test_additional_load_flexible_api_metadata_survives_restart(my_predbat):
    """Test omitted start_time and selected flexible metadata survive API command reparse."""
    failed = 0
    configure_additional_load_test(my_predbat)
    original_midnight = my_predbat.midnight_utc
    my_predbat.minutes_now = 20 * 60 + 45
    my_predbat.plan_interval_minutes = 15
    my_predbat.args["plan_interval_minutes"] = 15
    my_predbat.args["house_load_additional_forecast"] = []
    my_predbat.api_select("load_forecast_delta_api", "dishwasher?enabled=true&mode=flexible&end_time=07:00&duration=5.0&energy=0.7")
    my_predbat.refresh_additional_load_forecast_api()
    my_predbat.update_additional_load_api_command_metadata(
        "dishwasher",
        {
            "_selected_start": my_predbat.additional_load_minutes_to_stamp(21 * 60),
            "_selected_end": my_predbat.additional_load_minutes_to_stamp(26 * 60),
            "_expires_at": my_predbat.additional_load_minutes_to_stamp(26 * 60),
        },
    )

    api_command = my_predbat.api_select_update("load_forecast_delta_api")[0]
    if "_requested_start=" not in api_command or "_selected_start=" not in api_command or "_expires_at=" not in api_command:
        print("ERROR: API command should persist requested, selected, and expiry metadata, got {}".format(api_command))
        failed = 1

    my_predbat.house_load_additional_forecast_overrides = {}
    my_predbat.midnight_utc = original_midnight + timedelta(days=1)
    my_predbat.minutes_now = 30
    my_predbat.refresh_additional_load_forecast_api()

    forecast = my_predbat.house_load_additional_forecasts.get("dishwasher", {})
    if not forecast.get("selection_locked") or "T21:00:00" not in forecast.get("suggested_start", "") or "T02:00:00" not in forecast.get("suggested_end", ""):
        print("ERROR: Reparsed API command should restore old locked selection after restart, got {}".format(forecast))
        failed = 1

    my_predbat.minutes_now = 20 * 60 + 45
    my_predbat.refresh_additional_load_forecast_api()
    if my_predbat.house_load_additional_forecasts or my_predbat.api_select_update("load_forecast_delta_api"):
        print("ERROR: Reparsed expired API command should be removed, got forecasts {} api {}".format(my_predbat.house_load_additional_forecasts, my_predbat.api_select_update("load_forecast_delta_api")))
        failed = 1

    my_predbat.midnight_utc = original_midnight
    my_predbat.api_select("load_forecast_delta_api", "off")
    my_predbat.house_load_additional_forecast_overrides = {}
    return failed


def test_additional_load_flexible_api_repeat_preserves_metadata(my_predbat):
    """Test repeating an active API command preserves existing one-shot metadata."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.minutes_now = 20 * 60 + 45
    my_predbat.plan_interval_minutes = 15
    my_predbat.args["plan_interval_minutes"] = 15
    my_predbat.args["house_load_additional_forecast"] = []
    api_command = "dishwasher?enabled=true&mode=flexible&end_time=07:00&duration=5.0&energy=0.7"
    my_predbat.api_select("load_forecast_delta_api", api_command)
    my_predbat.refresh_additional_load_forecast_api()
    my_predbat.update_additional_load_api_command_metadata(
        "dishwasher",
        {
            "_selected_start": my_predbat.additional_load_minutes_to_stamp(21 * 60),
            "_selected_end": my_predbat.additional_load_minutes_to_stamp(26 * 60),
            "_expires_at": my_predbat.additional_load_minutes_to_stamp(26 * 60),
        },
    )

    my_predbat.minutes_now = 21 * 60 + 15
    my_predbat.api_select("load_forecast_delta_api", api_command)
    my_predbat.refresh_additional_load_forecast_api()

    stored_command = my_predbat.api_select_update("load_forecast_delta_api")[0]
    forecast = my_predbat.house_load_additional_forecasts.get("dishwasher", {})
    if "_selected_start=" not in stored_command or "T21:00:00" not in forecast.get("suggested_start", "") or not forecast.get("selection_locked"):
        print("ERROR: Repeated active API command should preserve selected metadata, command {} forecast {}".format(stored_command, forecast))
        failed = 1

    my_predbat.api_select("load_forecast_delta_api", "off")
    my_predbat.house_load_additional_forecast_overrides = {}
    return failed


def test_additional_load_flexible_pending_until_plan(my_predbat):
    """Test flexible additional load is left for plan-time prediction selection."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher", "mode": "flexible", "duration": 2.0, "energy": 1.2},
    ]

    load_adjust, forecasts = my_predbat.fetch_additional_load_forecast()
    if load_adjust:
        print("ERROR: Flexible load should not produce adjustments until plan-time selection, got {}".format(load_adjust))
        failed = 1
    forecast = forecasts.get("dishwasher", {})
    if forecast.get("state") != "off" or forecast.get("selection_reason") != "pending_prediction_metric":
        print("ERROR: Flexible load should publish pending prediction selection, got {}".format(forecast))
        failed = 1
    return failed


def test_additional_load_flexible_done_by_window(my_predbat):
    """Test flexible end_time means done by, with omitted start_time using now."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.minutes_now = 16 * 60
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher", "mode": "flexible", "end_time": "07:00", "duration": 2.0, "energy": 1.2},
    ]

    _, forecasts = my_predbat.fetch_additional_load_forecast()
    forecast = forecasts.get("dishwasher", {})
    if "T16:00:00" not in forecast.get("requested_start", "") or "T07:00:00" not in forecast.get("requested_end", ""):
        print("ERROR: Flexible done-by window should run from now until 07:00, got {}".format(forecast))
        failed = 1
    return failed


def test_additional_load_flexible_done_by_next_reachable_deadline(my_predbat):
    """Test flexible end_time rolls to the next reachable deadline when today's deadline cannot fit."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.forecast_minutes = 30 * 60
    my_predbat.minutes_now = 6 * 60
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher", "mode": "flexible", "end_time": "07:00", "duration": 5.0, "energy": 0.7},
    ]

    _, forecasts = my_predbat.fetch_additional_load_forecast()
    forecast = forecasts.get("dishwasher", {})
    if "T06:00:00" not in forecast.get("requested_start", "") or "T07:00:00" not in forecast.get("requested_end", ""):
        print("ERROR: Flexible done-by window should run from 06:00 to the next reachable 07:00, got {}".format(forecast))
        failed = 1
    if forecast.get("_requested_end_minutes") != 31 * 60:
        print("ERROR: Flexible done-by deadline should roll to tomorrow 07:00, got {}".format(forecast))
        failed = 1

    my_predbat.minutes_now = 30
    _, forecasts = my_predbat.fetch_additional_load_forecast()
    forecast = forecasts.get("dishwasher", {})
    if forecast.get("_requested_end_minutes") != 7 * 60:
        print("ERROR: Flexible done-by deadline should use today's 07:00 when the load fits, got {}".format(forecast))
        failed = 1

    my_predbat.minutes_now = 3 * 60
    _, forecasts = my_predbat.fetch_additional_load_forecast()
    forecast = forecasts.get("dishwasher", {})
    if forecast.get("_requested_end_minutes") != 31 * 60:
        print("ERROR: Flexible done-by deadline should roll when 5h cannot fit by today's 07:00, got {}".format(forecast))
        failed = 1
    return failed


def test_additional_load_flexible_api_omitted_start_is_frozen(my_predbat):
    """Test API flexible forecasts without start_time keep their initial requested start."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.minutes_now = 16 * 60 + 15
    my_predbat.plan_interval_minutes = 15
    my_predbat.args["plan_interval_minutes"] = 15
    my_predbat.args["house_load_additional_forecast"] = []
    my_predbat.api_select("load_forecast_delta_api", "dishwasher?enabled=true&mode=flexible&end_time=07:00&duration=2.0&energy=1.2")
    my_predbat.refresh_additional_load_forecast_api()

    forecast = my_predbat.house_load_additional_forecasts.get("dishwasher", {})
    first_requested_start = forecast.get("requested_start", "")
    if "T16:15:00" not in first_requested_start:
        print("ERROR: Flexible API omitted start should stamp initial plan slot, got {}".format(forecast))
        failed = 1

    my_predbat.minutes_now = 17 * 60
    my_predbat.refresh_additional_load_forecast_api()
    forecast = my_predbat.house_load_additional_forecasts.get("dishwasher", {})
    if forecast.get("requested_start", "") != first_requested_start:
        print("ERROR: Flexible API omitted start should not drift after refresh, got {}".format(forecast))
        failed = 1

    my_predbat.api_select("load_forecast_delta_api", "off")
    my_predbat.house_load_additional_forecast_overrides = {}
    return failed


def test_additional_load_flexible_yaml_omitted_start_rolls(my_predbat):
    """Test YAML flexible forecasts without start_time continue using the current plan slot."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.minutes_now = 16 * 60
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher", "mode": "flexible", "end_time": "07:00", "duration": 2.0, "energy": 1.2},
    ]

    _, forecasts = my_predbat.fetch_additional_load_forecast()
    first_requested_start = forecasts.get("dishwasher", {}).get("requested_start", "")
    my_predbat.minutes_now = 17 * 60
    _, forecasts = my_predbat.fetch_additional_load_forecast()
    second_requested_start = forecasts.get("dishwasher", {}).get("requested_start", "")

    if "T16:00:00" not in first_requested_start or "T17:00:00" not in second_requested_start:
        print("ERROR: Flexible YAML omitted start should roll with current time, got {} then {}".format(first_requested_start, second_requested_start))
        failed = 1
    return failed


def test_additional_load_flexible_prediction_metric_selection(my_predbat):
    """Test flexible additional load uses prediction metric, not raw import rate order."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.minutes_now = 16 * 60
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher", "mode": "flexible", "end_time": "07:00", "duration": 2.0, "energy": 1.2},
    ]
    my_predbat.house_load_additional_forecast_adjust, my_predbat.house_load_additional_forecasts = my_predbat.fetch_additional_load_forecast()
    my_predbat.charge_limit_best = []
    my_predbat.charge_window_best = []
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    my_predbat.end_record = my_predbat.forecast_minutes

    original_prediction = plan_module.Prediction

    class FakePrediction:
        """Fake prediction scores 01:00 as cheapest regardless of candidate order."""

        def __init__(self, base, pv_step, pv10_step, load_step, load10_step):
            """Store load step data."""
            self.load_step = load_step

        def run_prediction(self, charge_limit, charge_window, export_window, export_limits, pv10, end_record):
            """Return a metric based on when the injected load appears."""
            metric = 1000.0
            first_load_minute = None
            for minute, load in self.load_step.items():
                if load > 0:
                    first_load_minute = my_predbat.minutes_now + minute if first_load_minute is None else min(first_load_minute, my_predbat.minutes_now + minute)
            if first_load_minute is not None:
                metric = abs(first_load_minute - 25 * 60)
            return (metric, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    try:
        plan_module.Prediction = FakePrediction
        selected, load_step, _ = my_predbat.select_flexible_additional_loads({}, {}, {}, {})
    finally:
        plan_module.Prediction = original_prediction

    if not selected:
        print("ERROR: Flexible prediction metric selection should select a slot")
        failed = 1
    forecast = my_predbat.house_load_additional_forecasts.get("dishwasher", {})
    if "T01:00:00" not in forecast.get("suggested_start", "") or forecast.get("selection_reason") != "prediction_metric" or forecast.get("target_times"):
        print("ERROR: Flexible prediction metric should select 01:00, got {}".format(forecast))
        failed = 1
    if not load_step:
        print("ERROR: Flexible prediction metric should include selected load in returned plan step data")
        failed = 1
    return failed


def test_additional_load_flexible_candidate_energy_conserved(my_predbat):
    """Test flexible candidate scoring adds the configured energy exactly once."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.plan_interval_minutes = 15
    my_predbat.args["plan_interval_minutes"] = 15
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher", "mode": "flexible", "start_time": "10:15", "end_time": "11:00", "duration": 0.25, "energy": 0.15},
    ]

    _, forecasts = my_predbat.fetch_additional_load_forecast()
    forecast = forecasts.get("dishwasher", {})
    candidate_adjust, _, total_energy = my_predbat.additional_load_candidate_profile(forecast, 10 * 60 + 15)
    load_step = my_predbat.add_additional_load_to_step_data({}, candidate_adjust)
    step_total = round(sum(load_step.values()), 4)

    if total_energy != 0.15 or step_total != 0.15:
        print("ERROR: Flexible candidate should add exactly 0.15kWh, got profile {} step {} data {}".format(total_energy, step_total, load_step))
        failed = 1
    return failed


def test_additional_load_flexible_unchanged_selection_not_marked_changed(my_predbat):
    """Test unchanged flexible selection does not request another optimisation pass."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.minutes_now = 16 * 60
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher", "mode": "flexible", "end_time": "07:00", "duration": 2.0, "energy": 1.2},
    ]
    my_predbat.house_load_additional_forecast_adjust, my_predbat.house_load_additional_forecasts = my_predbat.fetch_additional_load_forecast(selected_flexible={"dishwasher": {"_selected_start_minutes": 25 * 60, "_selection_reason": "prediction_metric"}})
    my_predbat.charge_limit_best = []
    my_predbat.charge_window_best = []
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    my_predbat.end_record = my_predbat.forecast_minutes

    original_prediction = plan_module.Prediction

    class FakePrediction:
        """Fake prediction scores the existing 01:00 selection as cheapest."""

        def __init__(self, base, pv_step, pv10_step, load_step, load10_step):
            """Store load step data."""
            self.load_step = load_step

        def run_prediction(self, charge_limit, charge_window, export_window, export_limits, pv10, end_record):
            """Return a metric based on when the injected load appears."""
            first_load_minute = None
            for minute, load in self.load_step.items():
                if load > 0:
                    first_load_minute = my_predbat.minutes_now + minute if first_load_minute is None else min(first_load_minute, my_predbat.minutes_now + minute)
            metric = abs(first_load_minute - 25 * 60) if first_load_minute is not None else 1000.0
            return (metric, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    try:
        plan_module.Prediction = FakePrediction
        selected, _, _ = my_predbat.select_flexible_additional_loads({}, {}, {}, {})
    finally:
        plan_module.Prediction = original_prediction

    if not selected or my_predbat.house_load_additional_flexible_selection_changed:
        print("ERROR: Unchanged flexible selection should not be marked changed, selected {} changed {}".format(selected, my_predbat.house_load_additional_flexible_selection_changed))
        failed = 1
    return failed


def test_additional_load_textual_plan_summary(my_predbat):
    """Test textual plan includes confirmed and suggested additional load forecasts only."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.house_load_additional_forecasts = {
        "dishwasher": {
            "enabled": True,
            "total_energy": 1.2,
            "target_times": [
                {"start": "2026-05-07T10:00:00+02:00", "end": "2026-05-07T10:30:00+02:00", "energy": 0.6},
                {"start": "2026-05-07T10:30:00+02:00", "end": "2026-05-07T11:00:00+02:00", "energy": 0.6},
            ],
        },
        "washer": {
            "enabled": True,
            "mode": "flexible",
            "energy": 0.7,
            "slot_energy": 0.0,
            "duration": 5.0,
            "plan_interval_minutes": 15,
            "target_times": [],
            "total_energy": 0.0,
            "suggested_start": "2026-05-07T20:15:00+02:00",
            "suggested_end": "2026-05-08T01:15:00+02:00",
        },
        "dryer": {
            "enabled": True,
            "mode": "flexible",
            "energy": 0.9,
            "slot_energy": 0.0,
            "duration": 3.0,
            "plan_interval_minutes": 15,
            "selection_locked": True,
            "target_times": [
                {"start": "2026-05-07T21:15:00+02:00", "end": "2026-05-07T21:30:00+02:00", "energy": 0.075},
                {"start": "2026-05-07T21:30:00+02:00", "end": "2026-05-08T00:00:00+02:00", "energy": 0.825},
            ],
            "total_energy": 0.825,
            "suggested_start": "2026-05-07T21:00:00+02:00",
            "suggested_end": "2026-05-08T00:00:00+02:00",
        },
        "pending": {"enabled": True, "total_energy": 1.0, "target_times": []},
    }

    text = my_predbat.get_additional_load_text()
    if "dishwasher from 10:00 to 11:00 using 1.20 kWh is planned" not in text:
        print("ERROR: Textual plan should include planned dishwasher load, got {}".format(text))
        failed = 1
    if "washer is suggested from 20:15 to 01:15 using 0.70 kWh" not in text:
        print("ERROR: Textual plan should include suggested washer load, got {}".format(text))
        failed = 1
    if "dryer is running from 21:00 to 00:00 using 0.90 kWh" not in text:
        print("ERROR: Textual plan should include running dryer load, got {}".format(text))
        failed = 1
    if "pending" in text:
        print("ERROR: Textual plan should not include pending load, got {}".format(text))
        failed = 1
    my_predbat.house_load_additional_forecasts = {}
    return failed


def test_additional_load_history_archives_expired_api(my_predbat):
    """Test expired one-shot API loads are archived before removal."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.minutes_now = 10 * 60
    my_predbat.api_select("load_forecast_delta_api", "dishwasher?start_time=09:00&duration=1.0&energy=1.2&_expires_at=" + my_predbat.additional_load_minutes_to_stamp(10 * 60))
    my_predbat.house_load_additional_history_loaded = True

    my_predbat.refresh_additional_load_forecast_api()

    records = my_predbat.house_load_additional_history
    if len(records) != 2:
        print("ERROR: Expired API load should archive 2 completed slots, got {}".format(records))
        failed = 1
    if my_predbat.api_select_update("load_forecast_delta_api"):
        print("ERROR: Expired API load should be removed after archive")
        failed = 1
    history_sensor = my_predbat.dashboard_values.get("sensor.predbat_load_forecast_delta_history", {})
    if history_sensor.get("state") != 2:
        print("ERROR: History sensor should publish record count 2, got {}".format(history_sensor))
        failed = 1
    return failed


def test_additional_load_history_deduplicates_completed_slots(my_predbat):
    """Test repeated archiving does not duplicate completed slots."""
    failed = 0
    configure_additional_load_test(my_predbat)
    changed = my_predbat.archive_additional_load_slot("dishwasher", "api", "fixed", 9 * 60, 9 * 60 + 30, 0.6, 30)
    changed_again = my_predbat.archive_additional_load_slot("dishwasher", "api", "fixed", 9 * 60, 9 * 60 + 30, 0.6, 30)
    if not changed or changed_again or len(my_predbat.house_load_additional_history) != 1:
        print("ERROR: History archive should deduplicate identical slots, got changed {} changed_again {} records {}".format(changed, changed_again, my_predbat.house_load_additional_history))
        failed = 1
    return failed


def test_additional_load_history_filters_historical_load(my_predbat):
    """Test archived additional load is subtracted from learned historical load."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.days_previous = [1]
    my_predbat.days_previous_weight = [1]
    my_predbat.load_minutes_age = 2
    my_predbat.car_charging_hold = False
    my_predbat.iboost_energy_subtract = False
    my_predbat.archive_additional_load_slot("dishwasher", "api", "fixed", 9 * 60 - 24 * 60, 9 * 60 + 30 - 24 * 60, 0.4, 30)
    load_minutes = {1500: 1.0, 1530: 0.0}

    load_filtered, load_raw = my_predbat.get_filtered_load_minute(load_minutes, 9 * 60 - my_predbat.minutes_now, historical=True, step=30)
    if load_raw != 1.0 or load_filtered != 0.6:
        print("ERROR: Historical load should subtract archived additional load, got filtered {} raw {}".format(load_filtered, load_raw))
        failed = 1
    return failed


def test_additional_load_history_does_not_archive_future_slots(my_predbat):
    """Test future slots are not archived, allowing cancellation without history pollution."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.minutes_now = 10 * 60
    changed = my_predbat.archive_completed_additional_load_item({"name": "dishwasher", "_source": "api", "start_time": "11:00", "duration": 1.0, "energy": 1.2}, minutes_now_slot=10 * 60)
    if changed or my_predbat.house_load_additional_history:
        print("ERROR: Future additional load slots should not be archived, got {}".format(my_predbat.house_load_additional_history))
        failed = 1
    return failed


def test_additional_load_history_prunes_old_records(my_predbat):
    """Test additional load history prunes records outside the history lookback."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.max_days_previous = 2
    old_start = my_predbat.midnight_utc - timedelta(days=4)
    recent_start = my_predbat.midnight_utc - timedelta(days=1)
    my_predbat.house_load_additional_history = [
        {"id": "old", "name": "old", "source": "api", "mode": "fixed", "start": old_start.isoformat(), "end": (old_start + timedelta(minutes=30)).isoformat(), "energy": 0.3},
        {"id": "recent", "name": "recent", "source": "api", "mode": "fixed", "start": recent_start.isoformat(), "end": (recent_start + timedelta(minutes=30)).isoformat(), "energy": 0.3},
    ]
    my_predbat.prune_additional_load_history()
    if [record.get("id") for record in my_predbat.house_load_additional_history] != ["recent"]:
        print("ERROR: Additional load history should prune old records, got {}".format(my_predbat.house_load_additional_history))
        failed = 1
    return failed


def test_additional_load_history_restores_from_sensor(my_predbat):
    """Test additional load history is restored from the persisted HA sensor attribute."""
    failed = 0
    configure_additional_load_test(my_predbat)
    start = my_predbat.midnight_utc - timedelta(days=1, hours=-9)
    record = {
        "id": "dishwasher:{}:{}".format(start.isoformat(), (start + timedelta(minutes=30)).isoformat()),
        "name": "dishwasher",
        "source": "api",
        "mode": "fixed",
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=30)).isoformat(),
        "energy": "0.4",
    }
    my_predbat.ha_interface.dummy_items["sensor.predbat_load_forecast_delta_history"] = {"state": 1, "records": [record]}
    my_predbat.house_load_additional_history = []
    my_predbat.house_load_additional_history_loaded = False

    my_predbat.load_additional_load_history()

    if len(my_predbat.house_load_additional_history) != 1 or my_predbat.house_load_additional_history[0].get("energy") != 0.4:
        print("ERROR: Additional load history should restore valid sensor records, got {}".format(my_predbat.house_load_additional_history))
        failed = 1
    return failed


def test_additional_load_history_archives_end_time_without_duration(my_predbat):
    """Test completed end_time-only additional loads can be archived."""
    failed = 0
    configure_additional_load_test(my_predbat)
    changed = my_predbat.archive_completed_additional_load_item({"name": "dishwasher", "_source": "api", "start_time": "09:00", "end_time": "10:00", "energy": 1.2}, minutes_now_slot=10 * 60)
    if not changed or len(my_predbat.house_load_additional_history) != 2:
        print("ERROR: End-time-only completed load should archive two slots, got changed {} records {}".format(changed, my_predbat.house_load_additional_history))
        failed = 1
    return failed


def run_additional_load_forecast_tests(my_predbat):
    """Run additional load forecast tests."""
    failed = 0
    print("Test additional load forecast")
    failed |= test_additional_load_disabled(my_predbat)
    failed |= test_additional_load_enabled_false_profile(my_predbat)
    failed |= test_additional_load_dishwasher_simple(my_predbat)
    failed |= test_additional_load_end_time_without_duration(my_predbat)
    failed |= test_additional_load_slot_energy_weighting(my_predbat)
    failed |= test_additional_load_dishwasher_total_energy(my_predbat)
    failed |= test_additional_load_dishwasher_total_energy_weighting(my_predbat)
    failed |= test_additional_load_partial_duration_keeps_total_energy(my_predbat)
    failed |= test_additional_load_multiple_and_api_override(my_predbat)
    failed |= test_additional_load_select_api_override(my_predbat)
    failed |= test_additional_load_select_api_weighting(my_predbat)
    failed |= test_additional_load_select_event_updates_adjustment(my_predbat)
    failed |= test_additional_load_delete_button_removes_api_forecast(my_predbat)
    failed |= test_additional_load_delete_button_removes_sanitized_api_forecast(my_predbat)
    failed |= test_additional_load_yaml_does_not_publish_delete_button(my_predbat)
    failed |= test_additional_load_api_forecast_auto_expires(my_predbat)
    failed |= test_additional_load_yaml_placeholder_not_published(my_predbat)
    failed |= test_additional_load_stale_delete_button_no_replan(my_predbat)
    failed |= test_additional_load_flexible_api_selection_survives_refresh(my_predbat)
    failed |= test_additional_load_flexible_api_reselects_before_suggested_start(my_predbat)
    failed |= test_additional_load_flexible_api_stale_selection_not_before_requested_start(my_predbat)
    failed |= test_additional_load_flexible_api_locks_after_suggested_start(my_predbat)
    failed |= test_additional_load_flexible_api_metadata_survives_restart(my_predbat)
    failed |= test_additional_load_flexible_api_repeat_preserves_metadata(my_predbat)
    failed |= test_additional_load_flexible_pending_until_plan(my_predbat)
    failed |= test_additional_load_flexible_done_by_window(my_predbat)
    failed |= test_additional_load_flexible_done_by_next_reachable_deadline(my_predbat)
    failed |= test_additional_load_flexible_api_omitted_start_is_frozen(my_predbat)
    failed |= test_additional_load_flexible_yaml_omitted_start_rolls(my_predbat)
    failed |= test_additional_load_flexible_prediction_metric_selection(my_predbat)
    failed |= test_additional_load_flexible_candidate_energy_conserved(my_predbat)
    failed |= test_additional_load_flexible_unchanged_selection_not_marked_changed(my_predbat)
    failed |= test_additional_load_textual_plan_summary(my_predbat)
    failed |= test_additional_load_history_archives_expired_api(my_predbat)
    failed |= test_additional_load_history_deduplicates_completed_slots(my_predbat)
    failed |= test_additional_load_history_filters_historical_load(my_predbat)
    failed |= test_additional_load_history_does_not_archive_future_slots(my_predbat)
    failed |= test_additional_load_history_prunes_old_records(my_predbat)
    failed |= test_additional_load_history_restores_from_sensor(my_predbat)
    failed |= test_additional_load_history_archives_end_time_without_duration(my_predbat)
    return failed
