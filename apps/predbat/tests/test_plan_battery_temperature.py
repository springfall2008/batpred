# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""The plan's debug battery temperature column (GH#5919): the live plan only, with the thermal forecast in brackets while it is not used."""

from prediction import Prediction
from tests.test_infra import reset_inverter, reset_rates, update_rates_import


def run_test_plan_battery_temperature(my_predbat):
    """
    The live plan (publish=True) carries battery_temperature fields in its JSON rows for the web dashboard's debug toggle, and
    with plan_debug on its HTML gains a Batt °C column, with the thermal forecast in brackets while the plan uses the look back.
    Without plan_debug the HTML column is absent; in a view that is not published (history, yesterday, compare) both are.
    """
    print("**** Running plan battery temperature column tests ****")
    failed = False

    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    reset_inverter(my_predbat)
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.end_record = 48 * 60
    my_predbat.debug_enable = False
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 5.0
    my_predbat.num_inverters = 1
    my_predbat.reserve = 0.5

    pv_step = {}
    load_step = {}
    for minute in range(0, my_predbat.forecast_minutes, 5):
        pv_step[minute] = 0
        load_step[minute] = 0.5 / (60 / 5)
    my_predbat.load_minutes_step = load_step
    my_predbat.load_minutes_step10 = load_step
    my_predbat.pv_forecast_minute_step = pv_step
    my_predbat.pv_forecast_minute10_step = pv_step
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)

    charge_window_best = [{"start": my_predbat.minutes_now, "end": my_predbat.minutes_now + 60, "average": 10.0}]
    reset_rates(my_predbat, 10.0, 5.0)
    update_rates_import(my_predbat, charge_window_best)
    my_predbat.battery_power_plan_best = {}
    my_predbat.run_prediction([0], charge_window_best, [], [], False, end_record=my_predbat.end_record, save="best")
    # The best plan's battery power is kept for the next cycle's thermal forecast, the one link from the optimiser to it
    if not my_predbat.battery_power_plan_best or not any(value != 0 for value in my_predbat.battery_power_plan_best.values()):
        print("ERROR: saving the best plan should keep its battery power for the thermal forecast, got {} values".format(len(my_predbat.battery_power_plan_best)))
        failed = True
    my_predbat.charge_limit_best = [0]
    my_predbat.export_limits_best = []
    my_predbat.charge_window_best = charge_window_best
    my_predbat.export_window_best = []

    saved = {"plan_debug": my_predbat.plan_debug, "battery_temperature_prediction": my_predbat.battery_temperature_prediction, "battery_temperature_thermal": my_predbat.battery_temperature_thermal}
    try:
        my_predbat.battery_temperature_prediction = {minute: 10.0 + minute / 600.0 for minute in range(0, my_predbat.forecast_minutes, 5)}
        my_predbat.battery_temperature_thermal = {minute: 8.0 for minute in range(0, my_predbat.forecast_minutes, 5)}

        # Debug on, live plan: header, cells and JSON fields
        my_predbat.plan_debug = True
        html_plan, raw_plan = my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step, my_predbat.end_record, publish=True)
        if "Batt °C (thermal)" not in html_plan or "id=batt_temp" not in html_plan or "(8.0)" not in html_plan:
            print("ERROR: the live debug plan should show the battery temperature column with the thermal forecast in brackets")
            failed = True
        rows = raw_plan.get("rows", [])
        if not raw_plan.get("show_temperature") or not rows or rows[0].get("battery_temperature") is None or rows[0].get("battery_temperature_thermal") != 8.0:
            print("ERROR: the live debug plan JSON should carry show_temperature and battery temperature fields, got {} {}".format(raw_plan.get("show_temperature"), rows[:1]))
            failed = True
        later = [row for row in rows if row.get("battery_temperature") is not None and row["battery_temperature"] > rows[0]["battery_temperature"]]
        if not later:
            print("ERROR: later slots should show the forecast for their own time")
            failed = True

        # The plan uses the thermal forecast: no brackets
        my_predbat.battery_temperature_thermal = {}
        html_plan, raw_plan = my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step, my_predbat.end_record, publish=True)
        if "id=batt_temp" not in html_plan or any("battery_temperature_thermal" in row for row in raw_plan.get("rows", [])):
            print("ERROR: without a separate thermal forecast the column should show the plan's temperature alone")
            failed = True

        # A view that is not published, and debug off: no column
        html_plan, raw_plan = my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step, my_predbat.end_record, publish=False)
        if "Batt °C" in html_plan or raw_plan.get("show_temperature") or any("battery_temperature" in row for row in raw_plan.get("rows", [])):
            print("ERROR: a view that is not published should not show the battery temperature")
            failed = True
        my_predbat.plan_debug = False
        html_plan, raw_plan = my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step, my_predbat.end_record, publish=True)
        rows = raw_plan.get("rows", [])
        if "Batt °C" in html_plan or not raw_plan.get("show_temperature") or not rows or rows[0].get("battery_temperature") is None:
            print("ERROR: without plan_debug the HTML plan should not show the battery temperature, but the JSON should carry it for the web debug toggle")
            failed = True
    finally:
        for field, value in saved.items():
            setattr(my_predbat, field, value)
    return failed
