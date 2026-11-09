# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Structural tests for the plan card's summary (totals) row in get_plan_renderer_js().

The plan JSON carries the total cost as a float, so a total such as 3.20 arrives as 3.2 and must be formatted to two
decimal places in the browser (#5785). Like test_plan_staleness_js.py, this asserts on the JS source (there is no JS
engine in this suite).
"""

from web_helper import get_plan_renderer_js


def test_plan_totals_js(my_predbat):
    """Both the positive and the negative branch of the summary row's total cost are formatted to 2dp."""
    failed = False
    print("**** Testing the plan card summary row total cost formatting JS ****")
    renderer_js = get_plan_renderer_js()

    start = renderer_js.index("const totalCostStr =")
    expression = renderer_js[start : renderer_js.index(";", start)]
    if "${totals.total_cost.toFixed(2)}" not in expression:
        print("  ERROR: the positive summary total cost should be formatted with toFixed(2), got {!r}".format(expression))
        failed = True
    if "${Math.abs(totals.total_cost).toFixed(2)}" not in expression:
        print("  ERROR: the negative summary total cost should be formatted with toFixed(2), got {!r}".format(expression))
        failed = True
    if "${totals.total_cost}" in expression:
        print("  ERROR: the summary total cost is still interpolated without 2dp formatting")
        failed = True
    return failed
