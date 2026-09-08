# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Structural tests for the debug-history client-side JS embedded in get_plan_renderer_js()
(findNearestDebugSnapshot, loadDebugHistoryData, the plan table's Debug column) - previously
untested per @springfall2008's #4438 review (item 22). Follows test_plan_why_reason.py's
precedent for this kind of embedded-JS test: assert on the JS source text's structure
directly (there is no JS engine in this test suite), rather than executing it.
"""

from web_helper import get_plan_renderer_js


def test_debug_history_client_js(my_predbat):
    """Verify the shipped JS matches the documented behaviour (#4438 review items 8, 9, 22):
    exact (not "nearest window") snapshot matching, one fetch per Yesterday-view switch (not
    every plan poll), and a Debug column link built from the matched snapshot's own id.
    """
    failed = False
    print("**** Testing debug-history client-side JS structure ****")

    renderer_js = get_plan_renderer_js()

    print("Test: findNearestDebugSnapshot exists and matches by a tight, fixed tolerance, not scanning for the closest of several candidates")
    if "function findNearestDebugSnapshot(" not in renderer_js:
        print("  ERROR: expected a findNearestDebugSnapshot() function")
        failed = True
    else:
        fn_start = renderer_js.index("function findNearestDebugSnapshot(")
        fn_end = renderer_js.index("\n    }", fn_start)
        fn_src = renderer_js[fn_start:fn_end]
        # "Exact" match per the docs means a tight fixed-tolerance guard against formatting
        # noise, not an unbounded nearest-of-all-candidates search - assert the tolerance is
        # both present and small (milliseconds, not e.g. half a plan interval).
        if "DEBUG_SNAPSHOT_MATCH_TOLERANCE_MS" not in fn_src:
            print("  ERROR: expected findNearestDebugSnapshot to use a named tolerance constant")
            failed = True
        if "Math.abs(rowTime - snapTime) <=" not in fn_src:
            print("  ERROR: expected a tight symmetric time-difference comparison, not a running-minimum nearest search")
            failed = True
        if "return snap" not in fn_src:
            print("  ERROR: expected the function to return the matched snapshot object")
            failed = True
        tolerance_line = next((line for line in renderer_js.splitlines() if "DEBUG_SNAPSHOT_MATCH_TOLERANCE_MS =" in line), "")
        if "1000" not in tolerance_line:
            print("  ERROR: expected the tolerance constant to be defined as 1000ms (guarding only sub-second formatting noise), got: {!r}".format(tolerance_line))
            failed = True

    print("Test: loadDebugHistoryData exists, fetches the list route, and is wired to the Yesterday view switch rather than the frequent plan poll")
    if "async function loadDebugHistoryData(" not in renderer_js:
        print("  ERROR: expected a loadDebugHistoryData() function")
        failed = True
    else:
        fn_start = renderer_js.index("async function loadDebugHistoryData(")
        fn_end = renderer_js.index("\n    }", fn_start)
        fn_src = renderer_js[fn_start:fn_end]
        if "debug_history_list" not in fn_src:
            print("  ERROR: expected loadDebugHistoryData to fetch the debug_history_list route")
            failed = True
        if "window.debugHistoryData" not in fn_src:
            print("  ERROR: expected loadDebugHistoryData to populate window.debugHistoryData")
            failed = True
    if "loadDebugHistoryData().then(refreshPlan)" not in renderer_js:
        print("  ERROR: expected loadDebugHistoryData to be called (and awaited before a refresh) when switching view, not on every poll")
        failed = True

    print("Test: the plan table's Debug column links to the matched snapshot's own id and is labelled with a real time, not a 'steps ago' count")
    if "findNearestDebugSnapshot(row.time)" not in renderer_js:
        print("  ERROR: expected renderPlanTable to look up a Debug column entry via findNearestDebugSnapshot(row.time)")
        failed = True
    if "debug_history_download?id=${encodeURIComponent(snap.id)}" not in renderer_js:
        print("  ERROR: expected the Debug column link to download the matched snapshot by its own id")
        failed = True
    if "toLocaleTimeString" not in renderer_js:
        print("  ERROR: expected the Debug column label to be built from a real timestamp (toLocaleTimeString), not a step count")
        failed = True
    if ".steps_back" in renderer_js or "snapshots ago" in renderer_js:
        print("  ERROR: found leftover 'steps ago'-style wording/field access in the renderer JS - docs and shipped behaviour both moved to absolute time labels")
        failed = True

    return failed
