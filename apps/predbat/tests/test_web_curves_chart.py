# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for the Curves chart page (WebInterface.get_chart("Curves")): the curve sensors' status table, the SoC power curves and the temperature curves."""

import json
import re

from web import WebInterface

FIELDS = [
    "battery_charge_power_curve",
    "battery_discharge_power_curve",
    "battery_charge_power_curve_auto",
    "battery_discharge_power_curve_auto",
    "battery_temperature_charge_curve",
    "battery_temperature_charge_curve_configured",
    "battery_temperature_charge_curve_auto",
    "battery_temperature_curve_learned",
    "battery_temperature_discharge_curve",
    "battery_power_curve_source",
    "battery_power_curve_updated",
    "soc_max",
    "battery_rate_max_charge",
    "dashboard_values",
]


def _make_web(my_predbat):
    """A WebInterface bound to my_predbat without starting the aiohttp app."""
    web = WebInterface.__new__(WebInterface)
    web.base = my_predbat
    web.log = my_predbat.log
    web.prefix = my_predbat.prefix
    return web


def _chart_options(html, tagname):
    """The ApexCharts options JSON rendered for one chart, as a dict."""
    match = re.search(r"getElementById\('{}'\)".format(tagname), html)
    if not match:
        return None
    script = html[: match.start()]
    start = script.rfind("var options = ")
    end = script.find(";\n", start)
    return json.loads(script[start + len("var options = ") : end])


def _check(name, condition, detail=""):
    """Print and return a failure when condition is false."""
    if not condition:
        print("ERROR: {} failed {}".format(name, detail))
        return True
    return False


def run_web_curves_chart_tests(my_predbat):
    """The Curves page plots each curve as the plan looks it up and shows the curve sensors' status."""
    print("*** Running test: web_curves_chart")
    failed = False
    saved = {field: getattr(my_predbat, field, None) for field in FIELDS}
    try:
        my_predbat.dashboard_values = {}
        my_predbat.battery_charge_power_curve = {99: 0.5, 100: 0.2}
        my_predbat.battery_charge_power_curve_auto = True
        my_predbat.battery_discharge_power_curve = {}
        my_predbat.battery_discharge_power_curve_auto = False
        my_predbat.battery_power_curve_source = {"charge": "learned"}
        my_predbat.battery_power_curve_updated = {"charge": "2026-11-01T03:00:00+0000"}
        my_predbat.battery_temperature_charge_curve_auto = True
        my_predbat.battery_temperature_curve_learned = {5: 0.1, 12: 0.3}
        my_predbat.battery_temperature_charge_curve_configured = {0: 0.05}
        my_predbat.battery_temperature_charge_curve = {0: 0.05, 5: 0.1, 12: 0.3}
        my_predbat.battery_temperature_discharge_curve = {}
        my_predbat.soc_max = 10.0
        my_predbat.battery_rate_max_charge = 5.0 / 60.0
        my_predbat.publish_battery_curve_sensors()
        web = _make_web(my_predbat)

        print("Test 1: the status table shows each sensor")
        html = web.get_chart("Curves")
        failed |= _check("t1 table", "Charge power" in html and "Temperature charge" in html and "2026-11-01T03:00:00+0000" in html, html[:2000])
        failed |= _check("t1 learned yes", ">Yes</td>" in html and ">No</td>" in html)

        print("Test 2: the power chart plots every SoC percent, 1.0 where the curve sets nothing")
        options = _chart_options(html, "chart_power_curve")
        failed |= _check("t2 chart rendered", options is not None)
        if options:
            charge, discharge = options["series"][0]["data"], options["series"][1]["data"]
            failed |= _check("t2 101 points", len(charge) == 101 and len(discharge) == 101, len(charge))
            failed |= _check("t2 charge values", charge[50]["y"] == 1.0 and charge[99]["y"] == 0.5 and charge[100]["y"] == 0.2, charge[98:])
            failed |= _check("t2 discharge flat", all(point["y"] == 1.0 for point in discharge))
            failed |= _check("t2 numeric axis", options["xaxis"]["type"] == "numeric" and options["xaxis"]["min"] == 0 and options["xaxis"]["max"] == 100)

        print("Test 3: the temperature chart shows the cap in kW as the plan looks it up, the learned points and the maximum rate")
        options = _chart_options(html, "chart_temperature_curve")
        failed |= _check("t3 chart rendered", options is not None)
        if options:
            series = {entry["name"]: entry["data"] for entry in options["series"]}
            in_use = {point["x"]: point["y"] for point in series.get("Charge cap in use", [])}
            # 0.05 C of 10 kWh is 0.5 kW; -5 falls back to the 0 entry, while 8 falls back to the unset 20 entry, so is uncapped (1.0 C = 10 kW)
            failed |= _check("t3 in use", in_use.get(0) == 0.5 and in_use.get(5) == 1.0 and in_use.get(12) == 3.0 and in_use.get(8) == 10.0 and in_use.get(-5) == 0.5, in_use)
            failed |= _check("t3 learned points", series.get("Learned") == [{"x": 5, "y": 1.0}, {"x": 12, "y": 3.0}], series.get("Learned"))
            failed |= _check("t3 configured differs so shown", "Charge cap configured" in series, list(series))
            failed |= _check("t3 max rate", series.get("Maximum charge rate", [{}])[0].get("y") == 5.0, series.get("Maximum charge rate"))
            failed |= _check("t3 kW axis", options["yaxis"]["title"]["text"] == "Rate cap (kW)")

        print("Test 4: no temperature curve explains how to get one; the menu links to the page")
        my_predbat.battery_temperature_charge_curve_auto = False
        my_predbat.battery_temperature_curve_learned = {}
        my_predbat.battery_temperature_charge_curve_configured = {}
        my_predbat.battery_temperature_charge_curve = {}
        html = web.get_chart("Curves")
        failed |= _check("t4 no temperature chart", _chart_options(html, "chart_temperature_curve") is None and "battery_temperature_charge_curve_auto" in html)
        failed |= _check("t4 power chart still shown", _chart_options(html, "chart_power_curve") is not None)

        print("Test 5: a name with markup cannot close the script tag")
        text = web.render_curve_chart([{"name": "</script><b>", "data": [{"x": 0, "y": 1}]}], "x", "y", "Title</script>", "chart_test", 0, 1)
        failed |= _check("t5 escaped", "</script><b>" not in text and text.count("</script>") == 1, text)
    finally:
        for field, value in saved.items():
            setattr(my_predbat, field, value)
    return failed
