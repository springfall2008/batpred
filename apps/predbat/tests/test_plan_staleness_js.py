# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Structural tests for the plan page's stale-data warning in get_plan_renderer_js().

Each view (Plan, History, Yesterday without Predbat) is republished on its own cadence, so the warning must be judged
against the data the current view shows and the refresh interval that data carries, and must clear when the view has
no data. Like test_debug_history_client_js.py, this asserts on the JS source (there is no JS engine in this suite).
"""

from web_helper import get_plan_renderer_js


def function_source(renderer_js, signature):
    """The source of one function in the renderer, from its signature to the end of its body."""
    start = renderer_js.index(signature)
    return renderer_js[start : renderer_js.index("\n    }", start)]


def test_plan_staleness_js(my_predbat):
    """The stale warning uses the current view's data and its refresh_minutes, and is hidden when the view has none."""
    failed = False
    print("**** Testing the plan page stale-data warning JS ****")
    renderer_js = get_plan_renderer_js()

    check = function_source(renderer_js, "function checkStaleness(data)")
    if "Math.max(data.refresh_minutes + 5, 15)" not in check or "STALE_MINUTES_DEFAULT[currentView]" not in check:
        print("  ERROR: checkStaleness should allow a run (5 minutes) past the data's refresh_minutes, never under 15, falling back to a per-view default")
        failed = True
    if "ageMs > limitMinutes * 60000" not in check:
        print("  ERROR: checkStaleness should compare the age in milliseconds with the limit in minutes")
        failed = True
    no_data = check[check.index("if (!data || !data.timestamp) {") :] if "if (!data || !data.timestamp) {" in check else ""
    if "staleWarning.style.display = 'none'" not in no_data[: no_data.find("return;")]:
        print("  ERROR: checkStaleness should hide the warning, not leave it up, when the view has no data")
        failed = True
    defaults = next((line for line in renderer_js.splitlines() if "const STALE_MINUTES_DEFAULT" in line), "")
    if "plan: 15" not in defaults or "yesterday: 60" not in defaults or "baseline: 60" not in defaults:
        print("  ERROR: expected default limits of 15 minutes for the plan and 60 for the history views, got {!r}".format(defaults))
        failed = True

    if "checkStaleness(currentViewData());" not in renderer_js:
        print("  ERROR: the unchanged-data poll should check the current view's data, not always the plan's")
        failed = True
    if "checkStaleness(null);" not in function_source(renderer_js, "function refreshPlan()"):
        print("  ERROR: refreshPlan should clear the warning when the current view has no data")
        failed = True
    if "const data = currentViewData();" not in function_source(renderer_js, "function updateTimestampDisplay()"):
        print("  ERROR: updateTimestampDisplay should use currentViewData() rather than its own copy of the view mapping")
        failed = True
    if "checkStaleness(timestamp)" in renderer_js or "window.planData.timestamp);" in renderer_js:
        print("  ERROR: a staleness check still passes a bare timestamp")
        failed = True
    return failed
