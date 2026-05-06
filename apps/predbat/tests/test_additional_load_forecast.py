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

from tests.test_infra import run_async


def configure_additional_load_test(my_predbat):
    """Configure deterministic clock and plan settings for additional load tests."""
    my_predbat.minutes_now = 10 * 60
    my_predbat.plan_interval_minutes = 30
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.args["plan_interval_minutes"] = 30
    my_predbat.house_load_additional_forecast_overrides = {}


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


def test_additional_load_multiple_and_service_override(my_predbat):
    """Test multiple loads add together and service override updates one named load."""
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

    service_data = {
        "domain": "predbat",
        "service": "update_load_forecast_delta",
        "service_data": {"entity_id": "binary_sensor.predbat_load_forecast_delta_dishwasher", "start_time": "18:00", "duration": 1.0, "energy": 0.8},
    }
    run_async(my_predbat.trigger_callback(service_data))
    load_adjust, _ = my_predbat.fetch_additional_load_forecast()
    failed |= check_slot(load_adjust, 18 * 60, 0.4, "service override dishwasher")
    failed |= check_slot(load_adjust, 18 * 60 + 30, 0.4, "service override dishwasher")
    failed |= check_slot(load_adjust, 20 * 60, 0.0, "service override removed old dishwasher")
    failed |= check_slot(load_adjust, 20 * 60 + 30, 0.25, "service override kept heating")
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


def test_additional_load_switch_disables_and_enables(my_predbat):
    """Test companion switch disables and re-enables a named additional load."""
    failed = 0
    configure_additional_load_test(my_predbat)
    my_predbat.args["house_load_additional_forecast"] = [
        {"name": "dishwasher", "start_time": "20:00", "duration": 2.0, "energy": 1.2},
    ]
    my_predbat.refresh_additional_load_forecast_api()

    switch = my_predbat.dashboard_values.get("switch.predbat_load_forecast_delta_dishwasher", {})
    if switch.get("state") != "on":
        print("ERROR: Dishwasher companion switch should publish on, got {}".format(switch))
        failed = 1

    service_data = {
        "domain": "switch",
        "service": "turn_off",
        "service_data": {"entity_id": "switch.predbat_load_forecast_delta_dishwasher"},
    }
    run_async(my_predbat.trigger_callback(service_data))
    failed |= check_slot(my_predbat.house_load_additional_forecast_adjust, 20 * 60, 0.0, "switch disabled dishwasher")
    if my_predbat.dashboard_values.get("switch.predbat_load_forecast_delta_dishwasher", {}).get("state") != "off":
        print("ERROR: Dishwasher companion switch should publish off")
        failed = 1

    service_data["service"] = "turn_on"
    run_async(my_predbat.trigger_callback(service_data))
    failed |= check_slot(my_predbat.house_load_additional_forecast_adjust, 20 * 60, 0.3, "switch enabled dishwasher")
    if my_predbat.dashboard_values.get("switch.predbat_load_forecast_delta_dishwasher", {}).get("state") != "on":
        print("ERROR: Dishwasher companion switch should publish on after re-enable")
        failed = 1

    my_predbat.house_load_additional_forecast_overrides = {}
    return failed


def run_additional_load_forecast_tests(my_predbat):
    """Run additional load forecast tests."""
    failed = 0
    print("Test additional load forecast")
    failed |= test_additional_load_disabled(my_predbat)
    failed |= test_additional_load_dishwasher_simple(my_predbat)
    failed |= test_additional_load_slot_energy_weighting(my_predbat)
    failed |= test_additional_load_dishwasher_total_energy(my_predbat)
    failed |= test_additional_load_dishwasher_total_energy_weighting(my_predbat)
    failed |= test_additional_load_multiple_and_service_override(my_predbat)
    failed |= test_additional_load_select_api_override(my_predbat)
    failed |= test_additional_load_select_api_weighting(my_predbat)
    failed |= test_additional_load_select_event_updates_adjustment(my_predbat)
    failed |= test_additional_load_switch_disables_and_enables(my_predbat)
    return failed
