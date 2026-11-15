# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Tests for the Curves page (WebInterface.get_chart("Curves")): the rate grid status table, the 3D charge and discharge grids and their short tables."""

import json
import re

from battery_curve import charge_day
from web import WebInterface, PLOTLY_GL3D_URL

FIELDS = [
    "battery_charge_power_curve",
    "battery_discharge_power_curve",
    "battery_temperature_charge_curve",
    "battery_temperature_discharge_curve",
    "battery_rate_grid_auto",
    "battery_rate_grid_learned",
    "battery_rate_grid_samples",
    "battery_rate_grid_source",
    "battery_rate_grid_updated",
    "battery_rate_grid_last_scan",
    "battery_rate_grid_cache",
    "battery_rate_max_charge",
    "battery_rate_max_discharge",
    "soc_max",
    "dashboard_values",
]


def _make_web(my_predbat):
    """A WebInterface bound to my_predbat without starting the aiohttp app."""
    web = WebInterface.__new__(WebInterface)
    web.base = my_predbat
    web.log = my_predbat.log
    web.prefix = my_predbat.prefix
    return web


def _grid_data(html, kind):
    """The plot data JSON the page embeds for one grid, as a dict, or None."""
    match = re.search(r"var curve_grid_{}_data = (.*?);\n".format(kind), html)
    return json.loads(match.group(1)) if match else None


def _check(name, condition, detail=""):
    """Print and return a failure when condition is false."""
    if not condition:
        print("ERROR: {} failed {}".format(name, detail))
        return True
    return False


def _learned(temps=(10, 20)):
    """A learned charge grid over 10..20 C: full rate below 70% SoC, 0.3 above, the knee 10% lower at 10 C."""
    rows = []
    for degree in range(temps[0], temps[1] + 1):
        knee = 60 if degree < 15 else 70
        rows.append([1.0 if percent < knee else 0.3 for percent in range(101)])
    return {"temp_low": temps[0], "rows": rows, "zero_row": rows[-1], "cells": 40, "days": 4, "segments": 120, "temp_range": list(temps)}


def run_web_curves_chart_tests(my_predbat):
    """Run the Curves page tests."""
    failed = False
    saved = {name: getattr(my_predbat, name, None) for name in FIELDS}
    try:
        my_predbat.soc_max = 10.0
        my_predbat.battery_rate_max_charge = 5000 / 60000
        my_predbat.battery_rate_max_discharge = 4000 / 60000
        my_predbat.battery_charge_power_curve = {}
        my_predbat.battery_discharge_power_curve = {}
        my_predbat.battery_temperature_charge_curve = {}
        my_predbat.battery_temperature_discharge_curve = {}
        my_predbat.battery_rate_grid_auto = True
        my_predbat.battery_rate_grid_learned = {"charge": _learned(), "discharge": None}
        my_predbat.battery_rate_grid_cache = {}
        today = charge_day(my_predbat.now_utc, 0, my_predbat.local_tz)
        old = charge_day(my_predbat.now_utc, 120 * 24 * 60, my_predbat.local_tz)
        my_predbat.battery_rate_grid_samples = {"charge": {today: [[40.0, 12.0, 0.95], [80.0, 18.5, 0.31]], old: [[50.0, 5.0, 0.2]]}, "discharge": {}}
        my_predbat.dashboard_values = {
            "binary_sensor.{}_battery_charge_rate_grid".format(my_predbat.prefix): {
                "state": "on",
                "attributes": {"source": "learned", "cells": 40, "days": 2, "temperature_range": [10, 20], "updated": "2026-11-01T03:00:00+0000", "last_scan": "2026-11-15T03:00:00+0000"},
            },
            "binary_sensor.{}_battery_discharge_rate_grid".format(my_predbat.prefix): {"state": "off", "attributes": {"source": "default", "cells": 0, "days": 0}},
        }
        web = _make_web(my_predbat)
        html = web.get_chart("Curves")

        print("Test 1: the status table has a row per grid")
        failed |= _check("t1 rows", ">Charge<" in html and ">Discharge<" in html and "2026-11-01T03:00:00+0000" in html, html[:2000])
        failed |= _check("t1 learned yes/no", ">Yes</td>" in html and ">No</td>" in html)

        print("Test 2: a 3D surface per grid, plotly loaded once")
        failed |= _check("t2 divs", "id='curve_grid_charge'" in html and "id='curve_grid_discharge'" in html)
        failed |= _check("t2 plotly once", html.count(PLOTLY_GL3D_URL) == 1, html.count(PLOTLY_GL3D_URL))
        charge = _grid_data(html, "charge")
        failed |= _check("t2 data", charge is not None and len(charge["soc"]) == 101 and len(charge["z"]) == len(charge["temperature"]), charge and list(charge))
        if charge:
            temps = charge["temperature"]
            failed |= _check("t2 range around the evidence", temps[0] == 5 and temps[-1] == 25, (temps[0], temps[-1]))
            row_12 = charge["z"][temps.index(12)]
            row_18 = charge["z"][temps.index(18)]
            failed |= _check("t2 learned rows", row_12[65] == 0.3 and row_18[65] == 1.0, (row_12[65], row_18[65]))

        print("Test 3: evidence from the last 90 days only")
        if charge:
            failed |= _check("t3 points", sorted(zip(charge["ex"], charge["ey"], charge["ez"])) == [(40.0, 12.0, 0.95), (80.0, 18.5, 0.31)], list(zip(charge["ex"], charge["ey"], charge["ez"])))

        print("Test 4: a short table per grid at 10% SoC x 5 C")
        failed |= _check("t4 header", html.count("<th style='padding:4px 8px;border:1px solid #555;'>100</th>") == 2, html.count(">100</th>"))
        failed |= _check("t4 cold row", "<td style='padding:4px 8px;border:1px solid #555;'>10</td>" in html)

        print("Test 5: no evidence - the default grid is shown, and the page says so")
        discharge = _grid_data(html, "discharge")
        failed |= _check("t5 default surface", discharge is not None and all(value == 1.0 for row in discharge["z"] for value in row), discharge and discharge["z"][0][:5])
        failed |= _check("t5 says so", "No discharge evidence yet" in html)

        print("Test 6: a grid with no temperature is one row, drawn across the range")
        my_predbat.battery_rate_grid_learned = {"charge": dict(_learned(), temp_low=None, rows=[[1.0] * 70 + [0.3] * 31], temp_range=None), "discharge": None}
        my_predbat.battery_rate_grid_cache = {}
        my_predbat.battery_rate_grid_samples = {"charge": {today: [[40.0, None, 0.95]]}, "discharge": {}}
        html = web.get_chart("Curves")
        charge = _grid_data(html, "charge")
        failed |= _check("t6 flat in temperature", charge is not None and all(row == charge["z"][0] for row in charge["z"]), charge and len(charge["z"]))
        failed |= _check("t6 table one row", "<td style='padding:4px 8px;border:1px solid #555;'>All</td>" in html)
    finally:
        for name, value in saved.items():
            setattr(my_predbat, name, value)
    return failed
