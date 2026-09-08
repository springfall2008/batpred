# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

from prediction import Prediction
from tests.test_infra import reset_inverter


def test_dashboard_device_class(my_predbat):
    """
    #3352 - a batch of dashboard sensors published with the right unit but no device_class, so
    Home Assistant can't offer them for energy-dashboard style graphing/statistics. Only
    predbat.temperature (temperature.py) had device_class set; predbat.battery_temperature and the
    eight power sensors below did not. Regression-tests all nine in one pass.
    """
    reset_inverter(my_predbat)
    failed = False

    print("Test: current power sensors (execute.py) get device_class 'power'")
    my_predbat.pv_power = 1000
    my_predbat.grid_power = 500
    my_predbat.load_power = 800
    my_predbat.battery_power = 300
    my_predbat.publish_inverter_data()
    for entity in ["pv_power", "grid_power", "load_power", "battery_power"]:
        attrs = my_predbat.ha_interface.dummy_items.get(my_predbat.prefix + "." + entity)
        if not attrs or attrs.get("device_class") != "power":
            print("  ERROR: {} missing device_class=power, got {}".format(entity, attrs.get("device_class") if attrs else None))
            failed = True

    print("Test: battery_temperature (fetch.py) gets device_class 'temperature'")
    my_predbat.predict_battery_temperature({0: 20}, 30)
    attrs = my_predbat.ha_interface.dummy_items.get(my_predbat.prefix + ".battery_temperature")
    if not attrs or attrs.get("device_class") != "temperature":
        print("  ERROR: battery_temperature missing device_class=temperature, got {}".format(attrs.get("device_class") if attrs else None))
        failed = True

    print("Test: predicted 'best' power sensors (plan.py) get device_class 'power'")
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.end_record = my_predbat.forecast_minutes
    horizon = my_predbat.forecast_minutes + my_predbat.minutes_now + 10
    my_predbat.pv_forecast_minute = {m: 0.0 for m in range(0, horizon)}
    my_predbat.pv_forecast_minute10 = dict(my_predbat.pv_forecast_minute)
    my_predbat.load_minutes_step = {m: 0.0 for m in range(0, horizon)}
    my_predbat.load_minutes_step10 = dict(my_predbat.load_minutes_step)
    pv_step = {m: 0.0 for m in range(0, horizon, 5)}
    load_step = {m: 0.0 for m in range(0, horizon, 5)}
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
    my_predbat.run_prediction([], [], [], [], False, end_record=my_predbat.end_record, save="best")

    for entity in ["pv_power_best", "grid_power_best", "load_power_best", "battery_power_best"]:
        attrs = my_predbat.ha_interface.dummy_items.get(my_predbat.prefix + "." + entity)
        if not attrs or attrs.get("device_class") != "power":
            print("  ERROR: {} missing device_class=power, got {}".format(entity, attrs.get("device_class") if attrs else None))
            failed = True

    return failed
