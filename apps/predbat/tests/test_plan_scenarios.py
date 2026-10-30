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

SCENARIO_ATTRIBUTES = ("predict_soc_best10", "predict_metric_best10", "predict_soc_best90", "predict_metric_best90", "pv_forecast_minute90_step", "load_minutes_step90", "pv_metric10_weight")


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

        print("Test: in the HTML only the SoC cell carries the tooltip, one scenario per line")
        soc_title = 'title="Expected 50%&#10;Pessimistic 40%&#10;Optimistic 60%"'
        if soc_title not in html:
            print("ERROR: expected the SoC tooltip {} in the plan HTML".format(soc_title))
            failed = True
        rows_with_scenarios = sum(1 for plan_row in raw_plan["rows"] if "scenarios" in plan_row)
        if html.count('title="Expected ') != rows_with_scenarios:
            print("ERROR: expected one tooltip per row (the SoC cell), found {} for {} rows".format(html.count('title="Expected '), rows_with_scenarios))
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

        print("Test: PV10 is left out when the plan is not scored against it")
        my_predbat.pv_metric10_weight = 0.0
        my_predbat.predict_soc_best90 = _flat_soc(my_predbat, 6.0)
        my_predbat.predict_metric_best90 = _flat_metric(my_predbat, 0.5)
        _, raw_plan = my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step, my_predbat.end_record, publish=False, show_scenarios=True)
        my_predbat.pv_metric10_weight = saved["pv_metric10_weight"]
        if list(raw_plan.get("scenario_names", {}).keys()) != ["90"]:
            print("ERROR: with pv_metric10_weight 0 only PV90 should be shown, got {}".format(raw_plan.get("scenario_names")))
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

        print("Test: the HTML SoC tooltip is left out when every scenario shows the same SoC")
        if my_predbat.plan_scenario_soc_title(50, {"10": [50, 0.1, 0.2, 0.0, 1.0], "90": [50, 0.0, 0.3, 0.0, 2.0]}) != "":
            print("ERROR: matching SoCs should give no SoC tooltip, even when other values differ")
            failed = True

        print("Test: the web renderer's scenarioTitle() builds the same SoC tooltip as the HTML, and the other cells' too")
        renderer_js = get_plan_renderer_js()
        if "function scenarioTitle(" not in renderer_js or renderer_js.count("scenarioTitle(jsonData, row,") < 5:
            print("ERROR: expected scenarioTitle() and its use on the SoC, PV, load, cost and total cells")
            failed = True
        # On the editable cells the tooltip sits on the value, so it doesn't cover the override menu
        if renderer_js.count("<span${titleAttr}>") != 2 or "${titleAttr} onclick" in renderer_js:
            print("ERROR: expected the editable SoC and load cells to put the tooltip on a span around the value")
            failed = True
        jsonData = {"scenario_fields": PLAN_SCENARIO_FIELDS, "scenario_names": PLAN_SCENARIO_NAMES, "currency_symbols": ["£", "p"]}
        row = {"scenarios": {"90": [60, 0.1, 0.25, -0.12, 1.5], "10": [40, 0.0, 0.5, 0.2, -2.25]}}
        checks = [
            (0, 50, ' title="Expected 50%&#10;{} 40%&#10;{} 60%"'.format(PLAN_SCENARIO_NAMES["10"], PLAN_SCENARIO_NAMES["90"])),
            (2, 0.3, ' title="Expected 0.30 kWh&#10;{} 0.50 kWh&#10;{} 0.25 kWh"'.format(PLAN_SCENARIO_NAMES["10"], PLAN_SCENARIO_NAMES["90"])),
            (3, 0.05, ' title="Expected +5p&#10;{} +20p&#10;{} -12p"'.format(PLAN_SCENARIO_NAMES["10"], PLAN_SCENARIO_NAMES["90"])),
            # The web UI's Total column writes the sign after the currency symbol, and the tooltip matches it
            (4, 1.0, ' title="Expected £1.00&#10;{} £-2.25&#10;{} £1.50"'.format(PLAN_SCENARIO_NAMES["10"], PLAN_SCENARIO_NAMES["90"])),
            # Every scenario has the same PV as the cell: no tooltip, although other values differ
            (1, 0.1, None),
        ]
        node_ran = True
        for field, expected_value, want in checks:
            if field == 1:
                row_same_pv = {"scenarios": {"90": [60, 0.1, 0.25, -0.12, 1.5], "10": [40, 0.1, 0.5, 0.2, -2.25]}}
                got = _run_scenario_title(jsonData, row_same_pv, field, expected_value)
                want = ""
            else:
                got = _run_scenario_title(jsonData, row, field, expected_value)
            if got is None:
                print("  (node not installed - scenarioTitle() output not executed)")
                node_ran = False
                break
            if got != want:
                print("ERROR: field {} tooltip expected {!r}, JS gave {!r}".format(field, want, got))
                failed = True
        if node_ran:
            python_title = my_predbat.plan_scenario_soc_title(50, row["scenarios"])
            if python_title != checks[0][2]:
                print("ERROR: the HTML SoC tooltip {!r} should match the web UI's {!r}".format(python_title, checks[0][2]))
                failed = True
            if _run_scenario_title(jsonData, {}, 0, 50) != "":
                print("ERROR: a row without scenarios should get no tooltip")
                failed = True
            quoted = _run_scenario_title(dict(jsonData, currency_symbols=['"', "p"]), row, 4, 1.0)
            if "&quot;1.00" not in quoted or 'title=""' in quoted:
                print("ERROR: a quote in the currency symbol should be escaped in the tooltip, got {!r}".format(quoted))
                failed = True
    finally:
        for name, value in saved.items():
            setattr(my_predbat, name, value if value is not None else {})
    failed |= _run_calculate_plan_tests()

    print("**** Plan scenario tooltip tests {} ****".format("FAILED" if failed else "PASSED"))
    return failed


def _run_calculate_plan_tests():
    """
    calculate_plan() runs PV90 on the final plan only while it is weighted, clears it otherwise, and leaves the
    expected run's series in place (#5871). A fresh instance is planned from the random benchmark template, as
    test_plan_pv10_hedge does, so the shared one is untouched.
    """
    # Imported here: unit_test imports this module, so a top-level import would be circular
    import contextlib
    import io

    from tests.test_infra import reset_inverter
    from tests.test_plan_pv10_hedge import _scenario
    from tests.test_random_scenarios import RANDOM_TEMPLATE, apply_scenario_to_predbat
    from unit_test import create_predbat

    failed = False
    print("Test: calculate_plan() runs PV90 on the final plan only while it is weighted")
    plan_predbat = create_predbat()
    with contextlib.redirect_stdout(io.StringIO()):
        reset_inverter(plan_predbat)
        plan_predbat.read_debug_yaml(RANDOM_TEMPLATE)
        plan_predbat.config_root = "./"
        plan_predbat.save_restore_dir = "./"
        plan_predbat.load_user_config()
        apply_scenario_to_predbat(plan_predbat, _scenario())
        plan_predbat.args["threads"] = 0
        plan_predbat.debug_enable = False
        plan_predbat.pv_metric90_weight = 0.15
        plan_predbat.plan_valid = False
        plan_predbat.calculate_plan(recompute=True)
        soc90 = plan_predbat.predict_soc_best90
        soc_best = plan_predbat.predict_soc_best
        # The expected run must be the last one: its series is what a fresh nominal run of the same plan gives
        plan_predbat.run_prediction(plan_predbat.charge_limit_best, plan_predbat.charge_window_best, plan_predbat.export_window_best, plan_predbat.export_limits_best, False, end_record=plan_predbat.end_record, save="best")
        nominal = plan_predbat.predict_soc_best
        plan_predbat.pv_metric90_weight = 0.0
        plan_predbat.plan_valid = False
        plan_predbat.calculate_plan(recompute=True)
        soc90_off = plan_predbat.predict_soc_best90
    if not soc90 or soc90 == soc_best:
        print("ERROR: with PV90 weighted, calculate_plan() should keep a PV90 series distinct from the expected one")
        failed = True
    if soc_best != nominal:
        print("ERROR: predict_soc_best after calculate_plan() should be the expected run's, not a scenario run's")
        failed = True
    if soc90_off != {}:
        print("ERROR: with PV90 at zero weight, calculate_plan() should clear the PV90 series, got {} entries".format(len(soc90_off)))
        failed = True
    return failed
