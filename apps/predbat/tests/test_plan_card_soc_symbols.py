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
Keep the "Battery SoC symbols" legend in docs/predbat-plan-card.md consistent with the symbols the
HTML plan actually puts on the SoC % cell (issue #6037).

The legend listed only the three trend arrows, so the weather alert triangle and the manual SoC
markers had no explanation and users had to ask what they meant (#6037, and #4107 before it).
"""

import os
import re

from prediction import Prediction
from tests.test_infra import reset_inverter, reset_rates

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
OUTPUT_PATH = os.path.join(REPO_ROOT, "apps", "predbat", "output.py")
PLAN_CARD_PATH = os.path.join(REPO_ROOT, "docs", "predbat-plan-card.md")

# soc_sym = "&rarr;" (the trend arrow) or soc_sym = "&#9888; " + soc_sym (a marker in front of it)
SOC_SYM_ASSIGN_RE = re.compile(r'soc_sym\s*=\s*"(&[#a-zA-Z0-9]+;)')
# Each legend line is "- <symbol> - <meaning>"
LEGEND_LINE_RE = re.compile(r"^- (&[#a-zA-Z0-9]+;) - ", re.M)


def _soc_symbols_in_plan():
    """Return the set of HTML entities output.py writes into the plan's SoC % cell."""
    with open(OUTPUT_PATH, "r") as handle:
        source = handle.read()
    return set(SOC_SYM_ASSIGN_RE.findall(source))


def _soc_symbols_in_legend():
    """Return the set of HTML entities in the plan card's battery SoC symbols legend, or None if it isn't there."""
    with open(PLAN_CARD_PATH, "r") as handle:
        text = handle.read()
    match = re.search(r"^Battery SoC symbols:\n\n((?:- .*\n)+)", text, re.M)
    if not match:
        return None
    return set(LEGEND_LINE_RE.findall(match.group(1)))


def _render_plan_rows(my_predbat, pv_step, load_step):
    """Render the plan with publish=False and return its JSON rows."""
    _, raw_plan = my_predbat.publish_html_plan(pv_step, pv_step, load_step, load_step, my_predbat.end_record, publish=False)
    return raw_plan.get("rows", []) if raw_plan else []


def _check_zero_ceiling_marker(my_predbat):
    """Check a 0% manual SoC max (a real empty-the-battery ceiling) still gets the down arrow on the SoC % cell."""
    print("Test: a 0% manual SoC max ceiling is marked on the SoC % cell")
    my_predbat.load_user_config()
    my_predbat.fetch_config_options()
    reset_inverter(my_predbat)
    my_predbat.forecast_minutes = 24 * 60
    my_predbat.end_record = 24 * 60
    my_predbat.soc_max = 10.0
    my_predbat.soc_kw = 5.0
    my_predbat.reserve = 0.5

    pv_step = {minute: 0 for minute in range(0, my_predbat.forecast_minutes, 5)}
    load_step = {minute: 0.5 / (60 / 5) for minute in range(0, my_predbat.forecast_minutes, 5)}
    my_predbat.prediction = Prediction(my_predbat, pv_step, pv_step, load_step, load_step)
    reset_rates(my_predbat, 10.0, 5.0)
    my_predbat.charge_window_best = []
    my_predbat.charge_limit_best = []
    my_predbat.export_window_best = []
    my_predbat.export_limits_best = []
    my_predbat.run_prediction([], [], [], [], False, end_record=my_predbat.end_record, save="best")

    my_predbat.manual_soc_max_keep = {}
    rows = _render_plan_rows(my_predbat, pv_step, load_step)
    if len(rows) < 2:
        print("ERROR: the plan rendered {} rows, need at least 2".format(len(rows)))
        return True
    ceiling_slot = rows[0]["slot_minute"]
    other_slot = rows[1]["slot_minute"]

    my_predbat.manual_soc_max_keep = {minute: 0 for minute in range(ceiling_slot, other_slot)}
    rows_by_slot = {row["slot_minute"]: row for row in _render_plan_rows(my_predbat, pv_step, load_step)}
    my_predbat.manual_soc_max_keep = {}

    failed = False
    if "&#11015;" not in rows_by_slot.get(ceiling_slot, {}).get("soc_sym", ""):
        print("ERROR: slot {} has a 0% manual SoC max but its soc_sym '{}' has no &#11015;".format(ceiling_slot, rows_by_slot.get(ceiling_slot, {}).get("soc_sym")))
        failed = True
    if "&#11015;" in rows_by_slot.get(other_slot, {}).get("soc_sym", ""):
        print("ERROR: slot {} has no manual SoC max but its soc_sym '{}' has &#11015;".format(other_slot, rows_by_slot.get(other_slot, {}).get("soc_sym")))
        failed = True
    return failed


def test_plan_card_soc_symbols(my_predbat):
    """Check every symbol the plan can show on the SoC % cell is explained in the plan card legend."""
    failed = False
    print("**** Running plan card SoC symbol legend tests ****")

    legend = _soc_symbols_in_legend()
    if legend is None:
        print("ERROR: docs/predbat-plan-card.md no longer contains a 'Battery SoC symbols:' legend to check")
        return True

    used = _soc_symbols_in_plan()
    if not used:
        print("ERROR: found no soc_sym assignments in output.py, the source scan must be broken")
        return True
    print("Test: the plan card legend explains all {} SoC % cell symbols the plan uses".format(len(used)))

    for symbol in sorted(used):
        if symbol not in legend:
            print("ERROR: the HTML plan shows {} on the SoC % cell but docs/predbat-plan-card.md doesn't explain it".format(symbol))
            failed = True

    if _check_zero_ceiling_marker(my_predbat):
        failed = True

    if failed:
        print("ERROR: test_plan_card_soc_symbols FAILED")
    else:
        print("test_plan_card_soc_symbols PASSED")

    return failed
