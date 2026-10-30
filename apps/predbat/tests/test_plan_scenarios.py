# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
The plan table's PV10 and PV90 scenario values (#5871).

The plan is scored against the expected forecast and its PV10 and PV90 alternatives, but the table shows only the
expected one. These tests cover the final plan's scenario runs being kept, the per-row scenario lists in the plan
JSON, the HTML title= tooltips, and the web renderer's scenarioTitle() helper.
"""

import json
import shutil
import subprocess

from const import PV_SCENARIO_PV10, PV_SCENARIO_PV90
from output import PLAN_SCENARIO_FIELDS, PLAN_SCENARIO_NAMES
from tests.test_plan_why_reason import _flat_soc, _setup_baseline
from web_helper import get_plan_renderer_js

SCENARIO_ATTRIBUTES = ("predict_soc_best10", "predict_metric_best10", "predict_soc_best90", "predict_metric_best90", "pv_forecast_minute90_step", "load_minutes_step90")


def _flat_metric(my_predbat, pence_per_step):
    """Build a predict_metric_best dict that rises by pence_per_step every 5 minutes."""
    return {minute: pence_per_step * (minute // 5) for minute in range(0, my_predbat.forecast_minutes + my_predbat.plan_interval_minutes + 5, 5)}


def _flat_step(my_predbat, kwh_per_step):
    """Build a per-5-minute energy step dict with the same value in every step."""
    return {minute: kwh_per_step for minute in range(0, my_predbat.forecast_minutes, 5)}


def _first_row(raw_plan):
    """The plan row for the current slot."""
    return raw_plan["rows"][0]


def _run_scenario_title(jsonData, row, field, expected):
    """Run the web renderer's scenarioTitle() under node and return its output, or None without node."""
    node = shutil.which("node")
    if not node:
        return None
    renderer_js = get_plan_renderer_js()
    start = renderer_js.index("function scenarioTitle(")
    end = renderer_js.index("function renderLoadCell(", start)
    script = renderer_js[start:end] + "\nprocess.stdout.write(scenarioTitle({}, {}, {}, {}));\n".format(json.dumps(jsonData), json.dumps(row), field, json.dumps(expected))
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return "NODE ERROR: " + result.stderr
    return result.stdout


def run_test_plan_scenarios(my_predbat):
    """
    The final plan's PV10 and PV90 outcomes are carried on each plan row and shown as tooltips (#5871).
    """
    print("**** Running plan scenario tooltip tests ****")
    failed = False
    saved = {name: getattr(my_predbat, name, None) for name in SCENARIO_ATTRIBUTES}
    try:
        pv_step, load_step = _setup_baseline(my_predbat)
        minutes_now = my_predbat.minutes_now
        my_predbat.charge_window_best = []
        my_predbat.charge_limit_best = []
        my_predbat.export_window_best = []
        my_predbat.export_limits_best = []

        print("Test: a saving run under PV10 or PV90 keeps its own SoC and metric series")
        my_predbat.run_prediction([], [], [], [], PV_SCENARIO_PV10, end_record=my_predbat.end_record, save="best10")
        my_predbat.run_prediction([], [], [], [], PV_SCENARIO_PV90, end_record=my_predbat.end_record, save="best90")
        my_predbat.run_prediction([], [], [], [], False, end_record=my_predbat.end_record, save="best")
        for name in ("predict_soc_best10", "predict_metric_best10", "predict_soc_best90", "predict_metric_best90"):
            if not getattr(my_predbat, name):
                print("ERROR: {} should be filled by its saving run".format(name))
                failed = True
        if my_predbat.predict_soc_best10 is my_predbat.predict_soc_best or my_predbat.predict_soc_best90 is my_predbat.predict_soc_best:
            print("ERROR: the scenario series must not be the expected run's own dict")
            failed = True

        # Fixed scenario series, so the row values can be checked exactly
        my_predbat.predict_soc_best = _flat_soc(my_predbat, 5.0)
        my_predbat.predict_metric_best = _flat_metric(my_predbat, 1.0)
        my_predbat.predict_soc_best10 = _flat_soc(my_predbat, 4.0)
        my_predbat.predict_metric_best10 = _flat_metric(my_predbat, 2.0)
        my_predbat.predict_soc_best90 = _flat_soc(my_predbat, 6.0)
        my_predbat.predict_metric_best90 = _flat_metric(my_predbat, 0.5)
        load_step10 = _flat_step(my_predbat, 0.6 / 12)
        my_predbat.load_minutes_step90 = _flat_step(my_predbat, 0.3 / 12)
        my_predbat.pv_forecast_minute90_step = _flat_step(my_predbat, 0.2 / 12)

        print("Test: rows carry each scenario's SoC, PV, load, slot cost and running total, and name them once")
        html, raw_plan = my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step10, my_predbat.end_record, publish=False, show_scenarios=True)
        if raw_plan.get("scenario_fields") != PLAN_SCENARIO_FIELDS or raw_plan.get("scenario_names") != PLAN_SCENARIO_NAMES:
            print("ERROR: expected the field order and both scenario names once per plan, got {} / {}".format(raw_plan.get("scenario_fields"), raw_plan.get("scenario_names")))
            failed = True
        row = _first_row(raw_plan)
        slot_steps = my_predbat.plan_interval_minutes // 5
        slot_start_step = (row["slot_minute"] - (minutes_now // my_predbat.plan_interval_minutes) * my_predbat.plan_interval_minutes) // 5
        expected = {
            "10": [40, 0.0, round(0.6 / 12 * slot_steps, 2), round(2.0 * slot_steps / 100, 2), round(2.0 * slot_start_step / 100, 2)],
            "90": [60, round(0.2 / 12 * slot_steps, 2), round(0.3 / 12 * slot_steps, 2), round(0.5 * slot_steps / 100, 2), round(0.5 * slot_start_step / 100, 2)],
        }
        if row.get("scenarios") != expected:
            print("ERROR: first row scenarios expected {} got {}".format(expected, row.get("scenarios")))
            failed = True

        print("Test: the HTML cells carry a title= tooltip listing every scenario")
        soc_title = 'title="Expected 50%&#10;{} 40%&#10;{} 60%"'.format(PLAN_SCENARIO_NAMES["10"], PLAN_SCENARIO_NAMES["90"])
        if soc_title not in html:
            print("ERROR: expected the SoC tooltip {} in the plan HTML".format(soc_title))
            failed = True
        if html.count('title="Expected ') < 5:
            print("ERROR: expected tooltips on the SoC, PV, load, cost and total cells, found {}".format(html.count('title="Expected ')))
            failed = True

        print("Test: a row where every scenario matches the expected one carries nothing extra")
        my_predbat.predict_soc_best10 = _flat_soc(my_predbat, 5.0)
        my_predbat.predict_metric_best10 = _flat_metric(my_predbat, 1.0)
        my_predbat.predict_soc_best90 = _flat_soc(my_predbat, 5.0)
        my_predbat.predict_metric_best90 = _flat_metric(my_predbat, 1.0)
        my_predbat.load_minutes_step90 = load_step
        my_predbat.pv_forecast_minute90_step = pv_step
        html, raw_plan = my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step, my_predbat.end_record, publish=False, show_scenarios=True)
        if "scenarios" in _first_row(raw_plan) or 'title="Expected ' in html:
            print("ERROR: matching scenarios should add no per-row data or tooltip, got {}".format(_first_row(raw_plan).get("scenarios")))
            failed = True

        print("Test: with PV90 not run, only PV10 is named and carried")
        my_predbat.predict_soc_best10 = _flat_soc(my_predbat, 4.0)
        my_predbat.predict_soc_best90 = {}
        my_predbat.predict_metric_best90 = {}
        _, raw_plan = my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step, my_predbat.end_record, publish=False, show_scenarios=True)
        if list(raw_plan.get("scenario_names", {}).keys()) != ["10"] or list(_first_row(raw_plan).get("scenarios", {}).keys()) != ["10"]:
            print("ERROR: expected only PV10, got names {} row {}".format(raw_plan.get("scenario_names"), _first_row(raw_plan).get("scenarios")))
            failed = True

        print("Test: without show_scenarios (History, compare, debug passes) nothing is added")
        html, raw_plan = my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step, my_predbat.end_record, publish=False)
        if "scenario_fields" in raw_plan or any("scenarios" in row for row in raw_plan["rows"]) or 'title="Expected ' in html:
            print("ERROR: scenario data should only be added when show_scenarios is set")
            failed = True

        print("Test: a value rounding to -0.0 is carried as 0.0")
        values = my_predbat.plan_row_scenarios({"10": ({0: 5.0}, {0: 0.0, 30: -0.001}, {}, {})}, 0, 30, [0, 0.0, 0.0, 0.0, 0.0])
        if values != {"10": [50, 0.0, 0.0, 0.0, 0.0]} or str(values["10"][3]) != "0.0":
            print("ERROR: expected -0.0 normalised to 0.0, got {}".format(values))
            failed = True

        print("Test: the web renderer's scenarioTitle() builds the same tooltip as the HTML")
        renderer_js = get_plan_renderer_js()
        if "function scenarioTitle(" not in renderer_js or renderer_js.count("scenarioTitle(jsonData, row,") < 5:
            print("ERROR: expected scenarioTitle() and its use on the SoC, PV, load, cost and total cells")
            failed = True
        jsonData = {"scenario_fields": PLAN_SCENARIO_FIELDS, "scenario_names": PLAN_SCENARIO_NAMES, "currency_symbols": ["£", "p"]}
        row = {"scenarios": {"90": [60, 0.1, 0.25, -0.12, 1.5], "10": [40, 0.0, 0.5, 0.2, -2.25]}}
        checks = [
            (0, 50, ' title="Expected 50%&#10;{} 40%&#10;{} 60%"'.format(PLAN_SCENARIO_NAMES["10"], PLAN_SCENARIO_NAMES["90"])),
            (2, 0.3, ' title="Expected 0.30 kWh&#10;{} 0.50 kWh&#10;{} 0.25 kWh"'.format(PLAN_SCENARIO_NAMES["10"], PLAN_SCENARIO_NAMES["90"])),
            (3, 0.05, ' title="Expected +5p&#10;{} +20p&#10;{} -12p"'.format(PLAN_SCENARIO_NAMES["10"], PLAN_SCENARIO_NAMES["90"])),
            (4, 1.0, ' title="Expected £1.00&#10;{} -£2.25&#10;{} £1.50"'.format(PLAN_SCENARIO_NAMES["10"], PLAN_SCENARIO_NAMES["90"])),
        ]
        for field, expected_value, want in checks:
            got = _run_scenario_title(jsonData, row, field, expected_value)
            if got is None:
                print("  (node not installed - scenarioTitle() output not executed)")
                break
            python_title = my_predbat.plan_scenario_title(field, expected_value, row["scenarios"])
            if got != want or python_title != want:
                print("ERROR: field {} tooltip expected {!r}, JS gave {!r}, Python gave {!r}".format(field, want, got, python_title))
                failed = True
        if _run_scenario_title(jsonData, {}, 0, 50) not in (None, ""):
            print("ERROR: a row without scenarios should get no tooltip")
            failed = True
    finally:
        for name, value in saved.items():
            setattr(my_predbat, name, value if value is not None else {})

    print("**** Plan scenario tooltip tests {} ****".format("FAILED" if failed else "PASSED"))
    return failed
