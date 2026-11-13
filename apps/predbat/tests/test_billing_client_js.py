# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""Structural tests for the History view's billing columns, summary strip and day picker JS.

There is no JS engine in this suite, so, like test_debug_history_client_js.py, these assert on the
shipped JS source text and on the /api/plan_history handler.
"""

import asyncio
import json

from aiohttp.test_utils import make_mocked_request

from web import WebInterface
from web_helper import get_plan_renderer_js


def _function_source(js, name):
    """The source of one top-level function in the renderer JS, or '' when it is missing."""
    marker = "function {}(".format(name)
    if marker not in js:
        return ""
    start = js.index(marker)
    end = js.find("\n    }\n", start)
    return js[start : end if end > 0 else len(js)]


def _make_web(my_predbat):
    """A WebInterface bound to my_predbat without the aiohttp app."""
    interface = WebInterface.__new__(WebInterface)
    interface.base = my_predbat
    interface.log = my_predbat.log
    interface.prefix = my_predbat.prefix
    interface.registered_endpoints = []
    interface.default_page = "./dash"
    return interface


def _check_js(failed):
    """The renderer JS carries the billing columns, summary strip and picker."""
    js = get_plan_renderer_js()
    table = _function_source(js, "renderPlanTable")
    checks = [
        ("Billed/Diff headers gated on billed rows", "th('billed', 'Billed')" in table and "th('billing_diff', 'Diff')" in table and "r.billed !== undefined" in table),
        ("Billed cell title built from billed_buckets", "billed_buckets" in table and "title=" in table),
        ("Diff coloured red when billed is higher and green when lower", "billing_diff > 0" in table and "billing_diff < 0" in table),
        ("unpublished billing shown as a dash", "&mdash;" in table),
        ("event label rendered in the row", "billing_event" in table),
        ("COLUMN_HEADER_HELP has billed and billing_diff", "billed:" in table and "billing_diff:" in table),
        (
            "summary strip function",
            "function renderBillingSummary(" in js and "billing_summary" in _function_source(js, "renderBillingSummary") and "billed to" in _function_source(js, "renderBillingSummary") and "missing_slots" in _function_source(js, "renderBillingSummary"),
        ),
        ("summary strip footnotes the standing charge", "Standing charge&Dagger;" in _function_source(js, "renderBillingSummary") and "standing charge in the midnight slot" in _function_source(js, "renderBillingSummary")),
        ("summary strip says import only", "import only" in _function_source(js, "renderBillingSummary")),
        ("row waiting for its export is marked", "billing_pending_export" in table and "export not billed yet" in table),
        ("summary strip counts rows waiting for export", "export_pending_slots" in _function_source(js, "renderBillingSummary")),
        ("picker fetches an older day on demand", "./api/plan_history?date=" in _function_source(js, "selectHistoryDay")),
        ("default History view needs no fetch", "if (!day" in _function_source(js, "selectHistoryDay") or "if (day && " in _function_source(js, "selectHistoryDay")),
        ("picker rendered for the History view only", "currentView === 'yesterday'" in _function_source(js, "renderHistoryPicker")),
        ("picker flags days with a recorded event", "historyDayEvents" in _function_source(js, "renderHistoryPicker") and "history_events" in _function_source(js, "renderHistoryPicker")),
        ("plan_data refresh keeps the day events", "window.historyDayEvents = data.history_day_events" in js),
    ]
    for label, ok in checks:
        if not ok:
            print("ERROR: billing JS: {}".format(label))
            failed = True
    return failed


def _check_api(my_predbat, failed):
    """/api/plan_history returns a stored older day, 404 for any other, and plan_data lists the days."""
    web = _make_web(my_predbat)
    yesterday_json = my_predbat.get_state_wrapper(my_predbat.prefix + ".cost_yesterday", attribute="json")
    # plan_data answers "unchanged" with no view published, so publish one for it to send
    my_predbat.dashboard_item(my_predbat.prefix + ".cost_yesterday", state=0, attributes={"json": {"timestamp": "2026-11-02T10:00:00", "rows": []}})
    my_predbat.dashboard_item(my_predbat.prefix + ".plan_history", state=2, attributes={"days": {"2026-10-31": {"rows": [{"slot_minute": 0}]}, "2026-10-30": {"rows": [], "history_events": ["Saving event"]}}})
    try:
        response = asyncio.run(web.html_api_plan_history(make_mocked_request("GET", "/api/plan_history?date=2026-10-31")))
        if response.status != 200 or json.loads(response.text).get("rows") != [{"slot_minute": 0}]:
            print("ERROR: /api/plan_history should return the stored day, got {} {}".format(response.status, response.text))
            failed = True
        response = asyncio.run(web.html_api_plan_history(make_mocked_request("GET", "/api/plan_history?date=2026-10-01")))
        if response.status != 404 or json.loads(response.text).get("error") != "no history for date":
            print("ERROR: an unknown date should 404 with 'no history for date', got {} {}".format(response.status, response.text))
            failed = True
        response = asyncio.run(web.html_api_plan_data(make_mocked_request("GET", "/api/plan_data")))
        body = json.loads(response.text)
        if body.get("unchanged") is not False or body.get("history_days") != ["2026-10-30", "2026-10-31"]:
            print("ERROR: /api/plan_data should list the older history days, got {}".format(body.get("history_days")))
            failed = True
        if body.get("history_day_events") != {"2026-10-30": ["Saving event"]}:
            print("ERROR: /api/plan_data should list the days with recorded events, got {}".format(body.get("history_day_events")))
            failed = True
    finally:
        my_predbat.dashboard_item(my_predbat.prefix + ".plan_history", state=0, attributes={"days": {}})
        my_predbat.dashboard_item(my_predbat.prefix + ".cost_yesterday", state=0, attributes={"json": yesterday_json})
    return failed


def test_billing_client_js(my_predbat):
    """Run the History billing web tests."""
    failed = False
    print("**** Running billing client JS tests ****")
    failed = _check_js(failed)
    failed = _check_api(my_predbat, failed)
    return failed
