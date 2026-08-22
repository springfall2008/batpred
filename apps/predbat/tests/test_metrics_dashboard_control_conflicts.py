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
Test that the control ownership ledger's data reaches the Metrics Dashboard: the summary
counts and event/control detail _emit_snapshot_metrics() writes into the Prometheus registry
must round-trip through to_dict(), and the dashboard body must actually render them.
"""

import time

from predbat_metrics import metrics
from web_metrics_dashboard import get_metrics_dashboard_body


def test_control_conflicts_metrics_round_trip(my_predbat):
    """
    _emit_snapshot_metrics() reads self.control_ledger.recent_events()/sustained_controls()/
    newest_events() and must publish counts that agree with the ledger, plus the actual event
    and sustained-control detail the dashboard table needs - a bare count cannot show WHICH
    control was touched or WHEN.

    Uses real wall-clock time rather than a synthetic "now": _emit_snapshot_metrics() calls
    time.time() internally with no way to inject a clock, so events are timestamped close to
    the real time.time() it will use a moment later, well inside the 24h window.
    """
    print("**** test_control_conflicts_metrics_round_trip ****")

    ledger = my_predbat.control_ledger
    ledger.records.clear()
    ledger.events.clear()
    ledger.session_events.clear()

    # sustained_controls() needs 3+ hits on the same control - three conflicts on inverter_mode,
    # one on reserve, so the dashboard should report 4 changes with exactly one sustained control.
    now = time.time()
    for read_value in ("Boost", "Eco", "Away"):
        ledger.begin_cycle()
        ledger.record_write("select.inverter_mode", "inverter_mode", "Normal", now=now, generation=now)
        ledger.begin_cycle()
        ledger.observe("select.inverter_mode", "inverter_mode", read_value, now=now + 1, generation=now + 1)
        now += 2

    ledger.begin_cycle()
    ledger.record_write("number.reserve", "reserve", 4.0, now=now, generation=now)
    ledger.begin_cycle()
    ledger.observe("number.reserve", "reserve", 20.0, now=now + 1, generation=now + 1)

    real_conflict_events = ledger.recent_events(time.time())
    real_sustained = ledger.sustained_controls(real_conflict_events)
    assert len(real_conflict_events) == 4, f"Expected 4 conflicts set up directly on the ledger, got {len(real_conflict_events)}"
    assert real_sustained == ["inverter_mode"], f"inverter_mode fired three times and should be sustained, got {real_sustained}"

    my_predbat._emit_snapshot_metrics()

    data = metrics().to_dict()

    assert data["control_conflicts_24h"] == 4, f"Expected control_conflicts_24h=4, got {data['control_conflicts_24h']}"
    assert data["control_conflicts_sustained_total"] == 1, f"Expected 1 sustained control, got {data['control_conflicts_sustained_total']}"
    assert data["control_conflicts_sustained_controls"] == ["inverter_mode"], f"Expected sustained controls ['inverter_mode'], got {data['control_conflicts_sustained_controls']}"

    events = data["control_conflicts_events"]
    assert len(events) == 4, f"Expected 4 events published for the dashboard table, got {len(events)}"
    controls_seen = sorted(e["control"] for e in events)
    assert controls_seen == ["inverter_mode", "inverter_mode", "inverter_mode", "reserve"], f"Unexpected controls in published events: {controls_seen}"
    assert events[0]["entity_id"] == "select.inverter_mode", "Events must stay oldest-first, matching newest_events()"

    print("✓ control_conflicts_24h, sustained total and event/control detail all round-trip through to_dict()")
    print("✓ Test passed: control_conflicts_metrics_round_trip")
    return False


def test_control_conflicts_dashboard_renders_section(my_predbat):
    """
    The dashboard body must contain a Control Conflicts section wired to the fields
    _emit_snapshot_metrics() publishes - a section with no matching MD_DATA field would render
    permanently empty.
    """
    print("**** test_control_conflicts_dashboard_renders_section ****")

    body = get_metrics_dashboard_body("{}")

    assert "Control Conflicts" in body, "Dashboard body should contain a Control Conflicts section heading"
    assert "mdConflictCards" in body, "Dashboard body should contain the summary card container"
    assert "mdConflictTable" in body, "Dashboard body should contain the recent-events table container"
    assert "mdRenderControlConflicts(d)" in body, "mdRenderAll must call mdRenderControlConflicts so the section refreshes on every 30s poll"
    assert "d.control_conflicts_events" in body, "Render function must read control_conflicts_events from MD_DATA"
    assert "d.control_conflicts_sustained_controls" in body, "Render function must read control_conflicts_sustained_controls from MD_DATA"
    assert "d.control_conflicts_24h" in body, "Render function must read control_conflicts_24h from MD_DATA"

    print("✓ Control Conflicts section present and wired into the 30s refresh cycle")
    print("✓ Test passed: control_conflicts_dashboard_renders_section")
    return False
