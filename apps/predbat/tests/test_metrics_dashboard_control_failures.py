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
Test that the control failure monitor's data reaches the Metrics Dashboard: the counts and events
_emit_snapshot_metrics() writes must round-trip through to_dict(), and the dashboard body must
render them.
"""

import time

from control_monitor import ControlMonitor, MODE_CHARGING
from predbat_metrics import metrics
from web_metrics_dashboard import get_metrics_dashboard_body


def test_control_failures_metrics_round_trip(my_predbat):
    """
    _emit_snapshot_metrics() must publish the 24h count, the lifetime total and the event detail.

    Uses real wall-clock time: _emit_snapshot_metrics() calls time.time() internally.
    """
    print("**** test_control_failures_metrics_round_trip ****")
    saved = my_predbat.control_monitor
    failed = False
    try:
        monitor = ControlMonitor()
        now = time.time()
        monitor.events = [
            {"inverter": 0, "mode": MODE_CHARGING, "slot_start": now - 3600, "slot_end": now, "at": now - 3000, "requested_w": 3000, "measured_w": 0, "failed_minutes": 4.0},
            {"inverter": 1, "mode": MODE_CHARGING, "slot_start": now - 3600, "slot_end": now, "at": now - 2900, "requested_w": 3000, "measured_w": -100, "failed_minutes": 2.0},
        ]
        monitor.total = 9
        my_predbat.control_monitor = monitor

        my_predbat._emit_snapshot_metrics()
        data = metrics().to_dict()

        if data["control_failures_24h"] != 2:
            print("ERROR: control_failures_24h {} expected 2".format(data["control_failures_24h"]))
            failed = True
        if data["control_failures_total"] != 9:
            print("ERROR: control_failures_total {} expected 9".format(data["control_failures_total"]))
            failed = True
        if [event["inverter"] for event in data["control_failures_events"]] != [0, 1]:
            print("ERROR: events not published oldest-first: {}".format(data["control_failures_events"]))
            failed = True
    finally:
        my_predbat.control_monitor = saved
    return failed


def test_control_failures_dashboard_renders_section(my_predbat):
    """The dashboard body must contain a Control Failures section wired into the 30s refresh."""
    print("**** test_control_failures_dashboard_renders_section ****")
    body = get_metrics_dashboard_body("{}")
    failed = False
    for needle in ("Control Failures", "mdFailureCards", "mdFailureTable", "mdRenderControlFailures(d)", "d.control_failures_events", "d.control_failures_24h", "d.control_failures_total"):
        if needle not in body:
            print("ERROR: dashboard body is missing {}".format(needle))
            failed = True
    return failed
