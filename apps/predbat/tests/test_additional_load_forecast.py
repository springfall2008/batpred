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

import plan as plan_module
from tests.test_infra import run_async


def configure_additional_load_test(my_predbat):
    """Configure deterministic clock and plan settings for additional load tests."""
    my_predbat.minutes_now = 10 * 60
    my_predbat.plan_interval_minutes = 30
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.args["plan_interval_minutes"] = 30
    my_predbat.house_load_additional_forecast_overrides = {}
    my_predbat.house_load_additional_forecast_entities = set()


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
    if forecast.get("state") != "on" or forecast.get("slots") != 8 or not forecast.get("target_times"):
        print("ERROR: Flexible API forecast should keep selected target slots after refresh, got {}".format(forecast))
        failed = 1
    if "T11:15:00" not in forecast.get("suggested_start", "") or "T13:15:00" not in forecast.get("suggested_end", ""):
        print("ERROR: Flexible API forecast should publish selected window after refresh, got {}".format(forecast))
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
        selected, _, _ = my_predbat.select_flexible_additional_loads({}, {}, {}, {})
    finally:
        plan_module.Prediction = original_prediction

    if not selected:
        print("ERROR: Flexible prediction metric selection should select a slot")
        failed = 1
    failed |= check_slot(my_predbat.house_load_additional_forecast_adjust, 25 * 60, 0.3, "flexible prediction metric")
    forecast = my_predbat.house_load_additional_forecasts.get("dishwasher", {})
    if "T01:00:00" not in forecast.get("suggested_start", "") or forecast.get("selection_reason") != "prediction_metric":
        print("ERROR: Flexible prediction metric should select 01:00, got {}".format(forecast))
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
    failed |= test_additional_load_multiple_and_service_override(my_predbat)
    failed |= test_additional_load_select_api_override(my_predbat)
    failed |= test_additional_load_select_api_weighting(my_predbat)
    failed |= test_additional_load_select_event_updates_adjustment(my_predbat)
    failed |= test_additional_load_delete_button_removes_api_forecast(my_predbat)
    failed |= test_additional_load_yaml_does_not_publish_delete_button(my_predbat)
    failed |= test_additional_load_api_forecast_auto_expires(my_predbat)
    failed |= test_additional_load_yaml_placeholder_not_published(my_predbat)
    failed |= test_additional_load_stale_delete_button_no_replan(my_predbat)
    failed |= test_additional_load_flexible_api_selection_survives_refresh(my_predbat)
    failed |= test_additional_load_flexible_pending_until_plan(my_predbat)
    failed |= test_additional_load_flexible_done_by_window(my_predbat)
    failed |= test_additional_load_flexible_prediction_metric_selection(my_predbat)
    return failed
