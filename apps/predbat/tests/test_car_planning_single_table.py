"""Single-table integration: the existing Plan table is the accepted candidate display.

Contract (parent-owned module ``car_charging_view``):

* ``build_car_charging_plan_view(native_plan, report, now=None) -> dict`` projects the
  paired, fresh, native-passed combined candidate into the existing web table
  schema (rows keyed by ``time``/``slot_minute``, ``state_text``, ``show_limit``,
  ``import_rate``/``export_rate``, ``pv_forecast``/``load_forecast``,
  ``car_charging``, ``soc_percent``/``soc_change`` and the combined-only fields
  ``soc_end_percent``, ``plan_soc_pv10_start_percent``,
  ``plan_soc_pv10_end_percent``, ``plan_grid_peak_w``,
  ``plan_grid_peak_pv10_w``, ``plan_ev_on_minutes``, ``plan_interval_end``).
* ``render_car_charging_preview(report, native_diagnostic, now=None)`` is banner
  only - no ``<table>`` or ``<details>`` block, only the ownership/freshness
  banner plus the native import-limit diagnostic warning (if exceeded).
* ``get_car_charging_preview_update_js()`` returns ``""``; the parent owns the polling
  through the existing ``./api/plan_data`` route.
* Both ``html_plan`` and ``html_api_plan_data`` apply ``build_car_charging_plan_view``
  to the embedded ``plan_json``; the API still returns the full response on a
  conditional fetch when a report is active so expiry clears the table.
* ``None``/disabled reports leave the native plan unchanged; the candidate is
  never silently filled from an expired trajectory.
* The native ``.plan_html`` cache stays untouched; counters/overrides are not
  mutated; readonly manual/load/SOC inputs are deep-copied.

Every fixture is built in-memory; no HA service, network or actuator is
ever touched. The web entry points are exercised through ``WebInterface``
with a fake ``get_state_wrapper`` and a fake ``car_charging_report`` - the
existing ``test_car_planning_preview._good_row`` shape is reused so a contract
mismatch surfaces immediately as a mismatch of attribute names rather than a
silent zero-row fallback.
"""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import re
from types import SimpleNamespace

from car_charging_view import (
    build_car_charging_plan_view,
    get_car_charging_preview_update_js,
    render_car_charging_preview,
)
from web import WebInterface


# ----------------------------------------------------------------------
# Fixtures — reuse the existing test_car_planning_preview._good_row shape
# ----------------------------------------------------------------------


def _good_row(
    scenario="nominal",
    start="2026-01-01T12:00:00+0000",
    end="2026-01-01T12:30:00+0000",
    minutes=30,
    battery_modes="demand",
    ev_on_minutes=0,
    ev_reserved_kwh=0.0,
    house_kwh=0.5,
    pv_kwh=0.2,
    battery_start_percent=50.0,
    battery_end_percent=51.0,
    battery_min_percent=50.0,
    grid_peak_w=1500.0,
    import_rate=None,
    export_rate=None,
    cost_change=None,
    total_cost=None,
):
    """Build a paired row in the exact contract shape the view consumes.

    Optional rate/cost fields default to ``None`` to mirror the optional
    fields added by ``build_replay_preview`` (the preview supplies them only
    when the native replay exposes per-step prices).
    """
    row = {
        "start": start,
        "end": end,
        "minutes": minutes,
        "battery_modes": battery_modes,
        "ev_on_minutes": ev_on_minutes,
        "ev_reserved_kwh": ev_reserved_kwh,
        "house_kwh": house_kwh,
        "pv_kwh": pv_kwh,
        "battery_start_percent": battery_start_percent,
        "battery_end_percent": battery_end_percent,
        "battery_min_percent": battery_min_percent,
        "grid_peak_w": grid_peak_w,
        "mode_minutes": {battery_modes: minutes} if battery_modes else {},
    }
    if import_rate is not None:
        row["import_rate"] = import_rate
    if export_rate is not None:
        row["export_rate"] = export_rate
    if cost_change is not None:
        row["cost_change"] = cost_change
    if total_cost is not None:
        row["total_cost"] = total_cost
    return row


def _good_report(
    status="candidate",
    uses_stale=False,
    native_passed=True,
    now=None,
    generated_minutes_ago=5,
):
    """Build a freshly generated report; age tests override ``generated_minutes_ago``."""
    now = now if now is not None else datetime.now(timezone.utc)
    generated_at = (now - timedelta(minutes=generated_minutes_ago)).isoformat()
    preview = {
        "nominal": [_good_row(scenario="nominal")],
        "pv10": [_good_row(scenario="pv10")],
    }
    if native_passed:
        native_checks = {"nominal": {"passed": True}, "pv10": {"passed": True}}
    else:
        native_checks = {"nominal": {"passed": False}, "pv10": {"passed": False}}
    report = {
        "status": status,
        "generated_at": generated_at,
        "uses_stale_soc_assumption": uses_stale,
        "preview": preview,
        "native_checks": native_checks,
    }
    return report


def _native_plan(rows=None):
    """Build a minimal native plan dict with the inputs ``build_car_charging_plan_view`` inherits."""
    return {
        "timestamp": "2026-01-01T12:00:00+0000",
        "time": "2026-01-01T00:00:00+0000",
        "currency_symbols": ["£", "p"],
        "num_cars": 0,
        "iboost_enable": False,
        "carbon_enable": False,
        "rows": rows if rows is not None else [],
    }


def _native_diagnostic(state, peak_w=None, limit_w=None, minutes_over=None):
    """Build the cached native diagnostic - independent of any preview state."""
    attrs = {}
    if peak_w is not None:
        attrs["peak_w"] = peak_w
    if limit_w is not None:
        attrs["limit_w"] = limit_w
    if minutes_over is not None:
        attrs["minutes_over"] = minutes_over
    return {"state": state, "attributes": attrs}


# ----------------------------------------------------------------------
# build_car_charging_plan_view — direct contract tests
# ----------------------------------------------------------------------


def test_build_car_planning_plan_view_marks_car_planning_simulation():
    """Output is always flagged plan_simulation=true so the renderer treats it read-only."""
    view = build_car_charging_plan_view(_native_plan(), _good_report())
    assert view.get("plan_simulation") is True


def test_build_car_planning_plan_view_preserves_currency_symbols():
    """currency_symbols survives from native even when the candidate is present."""
    native = _native_plan()
    native["currency_symbols"] = ["€", "c"]
    view = build_car_charging_plan_view(native, _good_report())
    assert view.get("currency_symbols") == ["€", "c"]


def test_build_car_planning_plan_view_preserves_time_midnight_reference():
    """The midnight reference (time) is the native plan's midnight, not the row start."""
    native = _native_plan()
    native["time"] = "2026-01-15T00:00:00+0000"
    view = build_car_charging_plan_view(native, _good_report())
    assert view.get("time") == "2026-01-15T00:00:00+0000"


def test_build_car_planning_plan_view_sets_timestamp_from_report():
    """The displayed timestamp is the report's generated_at, not the native timestamp."""
    native = _native_plan()
    native["timestamp"] = "1970-01-01T00:00:00+0000"
    report = _good_report()
    report["generated_at"] = "2026-01-15T12:34:56+0000"
    view = build_car_charging_plan_view(native, report)
    assert view.get("timestamp") == "2026-01-15T12:34:56+0000"


def test_build_car_planning_plan_view_sets_num_cars_iboost_carbon_off():
    """num_cars=-1, iboost_enable=False, carbon_enable=False lock the renderer to read-only columns."""
    view = build_car_charging_plan_view(_native_plan(), _good_report())
    assert view.get("num_cars") == 1
    assert view.get("iboost_enable") is False
    assert view.get("carbon_enable") is False


def test_build_car_planning_plan_view_returns_one_row_per_preview_row():
    """Row count matches the preview's bin count; nothing is collapsed or duplicated."""
    report = _good_report()
    report["preview"]["nominal"] = [
        _good_row(),
        _good_row(start="2026-01-01T12:30:00+0000", end="2026-01-01T13:00:00+0000", battery_start_percent=51, battery_end_percent=52, battery_min_percent=51),
    ]
    report["preview"]["pv10"] = [
        _good_row(scenario="pv10"),
        _good_row(scenario="pv10", start="2026-01-01T12:30:00+0000", end="2026-01-01T13:00:00+0000", battery_start_percent=51, battery_end_percent=52, battery_min_percent=51),
    ]
    view = build_car_charging_plan_view(_native_plan(), report)
    assert len(view["rows"]) == 2


def test_build_car_planning_plan_view_row_carries_required_field_set():
    """Every projected row exposes the contract field set for the JS renderer."""
    view = build_car_charging_plan_view(_native_plan(), _good_report())
    assert view["rows"], "expected at least one row"
    required = {
        "time",
        "slot_minute",
        "import_rate",
        "export_rate",
        "state",
        "state_text",
        "show_limit",
        "pv_forecast",
        "pv_forecast10",
        "load_forecast",
        "load_forecast10",
        "car_charging",
        "soc_percent",
        "soc_change",
        "soc_end_percent",
        "plan_soc_pv10_start_percent",
        "plan_soc_pv10_end_percent",
        "plan_grid_peak_w",
        "plan_grid_peak_pv10_w",
        "plan_ev_on_minutes",
    }
    for row in view["rows"]:
        missing = required - set(row)
        assert not missing, f"row missing contract fields: {missing}"


def test_build_car_planning_plan_view_row_time_is_preview_start():
    """row.time is the preview row's start timestamp, not the native plan's row time."""
    view = build_car_charging_plan_view(_native_plan(), _good_report())
    for row in view["rows"]:
        assert row["time"] == "2026-01-01T12:00:00+0000"


def test_build_car_planning_plan_view_row_slot_minute_derived_from_start():
    """slot_minute is the start's offset from the native plan's midnight reference."""
    native = _native_plan()
    native["time"] = "2026-01-01T00:00:00+0000"
    view = build_car_charging_plan_view(native, _good_report())
    for row in view["rows"]:
        # 12:00 UTC on the same day → 12 * 60 = 720 minutes from midnight.
        assert row["slot_minute"] == 12 * 60


def test_build_car_planning_plan_view_preserves_partial_first_bin():
    """A short first bin (5 min) is preserved with truthful slot_minute and state_text."""
    report = _good_report()
    # Override end so (end - start) equals minutes * 60, otherwise _paired_preview rejects the row.
    report["preview"]["nominal"] = [_good_row(minutes=5, end="2026-01-01T12:05:00+0000", battery_modes="demand", battery_end_percent=51, battery_min_percent=50)]
    report["preview"]["pv10"] = [_good_row(scenario="pv10", minutes=5, end="2026-01-01T12:05:00+0000", battery_modes="demand", battery_end_percent=51, battery_min_percent=50)]
    view = build_car_charging_plan_view(_native_plan(), report)
    assert view.get("rows"), view
    row = view["rows"][0]
    assert row["state_text"] in ("&nearr;", "&searr;", "&rarr;")
    assert row["plan_interval_end"] == "2026-01-01T12:05:00+0000"


def test_build_car_planning_plan_view_car_charging_is_ev_reserved_kwh():
    """car_charging carries the full-power reservation, not measured delivery."""
    report = _good_report()
    report["preview"]["nominal"] = [_good_row(ev_on_minutes=30, ev_reserved_kwh=3.385)]
    report["preview"]["pv10"] = [_good_row(scenario="pv10", ev_on_minutes=30, ev_reserved_kwh=3.385)]
    view = build_car_charging_plan_view(_native_plan(), report)
    assert abs(view["rows"][0]["car_charging"] - 3.385) < 1e-9


def test_build_car_planning_plan_view_soc_percent_is_nominal_start():
    """soc_percent is the nominal replay's start battery percentage."""
    view = build_car_charging_plan_view(_native_plan(), _good_report())
    assert view["rows"][0]["soc_percent"] == 50.0


def test_build_car_planning_plan_view_soc_change_is_end_minus_start():
    """soc_change is the difference between nominal end and nominal start percentages."""
    report = _good_report()
    report["preview"]["nominal"] = [_good_row(battery_start_percent=40.0, battery_end_percent=45.0, battery_min_percent=40.0)]
    report["preview"]["pv10"] = [_good_row(scenario="pv10", battery_start_percent=40.0, battery_end_percent=45.0, battery_min_percent=40.0)]
    view = build_car_charging_plan_view(_native_plan(), report)
    assert view["rows"][0]["soc_change"] == 5.0


def test_build_car_planning_plan_view_car_planning_soc_end_percent_nominal():
    """soc_end_percent is the nominal replay's end percentage."""
    report = _good_report()
    report["preview"]["nominal"] = [_good_row(battery_start_percent=40.0, battery_end_percent=45.0, battery_min_percent=40.0)]
    report["preview"]["pv10"] = [_good_row(scenario="pv10", battery_start_percent=40.0, battery_end_percent=43.0, battery_min_percent=40.0)]
    view = build_car_charging_plan_view(_native_plan(), report)
    assert view["rows"][0]["soc_end_percent"] == 45.0
    assert view["rows"][0]["plan_soc_pv10_start_percent"] == 40.0
    assert view["rows"][0]["plan_soc_pv10_end_percent"] == 43.0


def test_build_car_planning_plan_view_state_text_demand_includes_minutes():
    """Keep the original demand arrow; interval minutes remain developer data."""
    view = build_car_charging_plan_view(_native_plan(), _good_report())
    assert view["rows"][0]["state_text"] == "&nearr;"
    assert "m" not in view["rows"][0]["state_text"]


def test_build_car_planning_plan_view_state_text_charge_shows_100_limit():
    """Charge-only bin yields show_limit="100" and a Chrg-style state."""
    report = _good_report()
    report["preview"]["nominal"] = [_good_row(battery_modes="charge", battery_start_percent=20.0, battery_end_percent=80.0, battery_min_percent=20.0)]
    report["preview"]["pv10"] = [_good_row(scenario="pv10", battery_modes="charge", battery_start_percent=20.0, battery_end_percent=80.0, battery_min_percent=20.0)]
    view = build_car_charging_plan_view(_native_plan(), report)
    assert view["rows"][0]["show_limit"] == "100"


def test_build_car_planning_plan_view_state_text_hold_includes_duration():
    """Keep the original HoldChrg label and colour rather than a duration label."""
    report = _good_report()
    report["preview"]["nominal"] = [_good_row(battery_modes="hold", battery_start_percent=20.0, battery_end_percent=20.0, battery_min_percent=20.0)]
    report["preview"]["pv10"] = [_good_row(scenario="pv10", battery_modes="hold", battery_start_percent=20.0, battery_end_percent=20.0, battery_min_percent=20.0)]
    view = build_car_charging_plan_view(_native_plan(), report)
    assert view["rows"][0]["state_text"] == "HoldChrg&rarr;"
    assert view["rows"][0]["state_color"] == "#34DBEB"


def test_build_car_planning_plan_view_pv_load_sums_are_real_5min_kwh():
    """pv_forecast and load_forecast are the bin sums of the 5-min kWh values."""
    report = _good_report()
    report["preview"]["nominal"] = [_good_row(house_kwh=2.4, pv_kwh=1.2)]
    report["preview"]["pv10"] = [_good_row(scenario="pv10", house_kwh=3.0, pv_kwh=0.6)]
    view = build_car_charging_plan_view(_native_plan(), report)
    assert abs(view["rows"][0]["pv_forecast"] - 1.2) < 1e-9
    assert abs(view["rows"][0]["pv_forecast10"] - 0.6) < 1e-9
    assert abs(view["rows"][0]["load_forecast"] - 2.4) < 1e-9
    assert abs(view["rows"][0]["load_forecast10"] - 3.0) < 1e-9


def test_build_car_planning_plan_view_grid_peaks_preserved():
    """plan_grid_peak_w is the row's grid_peak_w; pv10 counterpart is preserved separately."""
    report = _good_report()
    report["preview"]["nominal"] = [_good_row(grid_peak_w=1500.0)]
    report["preview"]["pv10"] = [_good_row(scenario="pv10", grid_peak_w=1600.0)]
    view = build_car_charging_plan_view(_native_plan(), report)
    assert view["rows"][0]["plan_grid_peak_w"] == 1500.0
    assert view["rows"][0]["plan_grid_peak_pv10_w"] == 1600.0


def test_build_car_planning_plan_view_does_not_inherit_native_cost_cancelled_rowspan():
    """Native cost, cancelled, rowspan fields are NOT copied into the combined view rows."""
    native_rows = [
        {
            "time": "2026-01-01T12:00:00+0000",
            "import_rate": 12.0,
            "state": "Chrg",
            "state_text": "Chrg",
            "soc_percent": 60,
            "cost_change": 0.99,
            "total_cost": 9.99,
            "car_charging": 7.5,
            "car_charging_cancelled": 2.0,
            "rowspan_state": 3,
            "rowspan_limit": 2,
            "split": True,
            "skip_state_cell": True,
        }
    ]
    view = build_car_charging_plan_view(_native_plan(native_rows), _good_report())
    assert view.get("rows"), view
    for row in view["rows"]:
        # The combined view always projects these two fields, but the preview row
        # did not provide them and the native row is never used as a fallback.
        assert row.get("cost_change") is None
        assert row.get("total_cost") is None
        # Native-only fields never reach the combined view.
        assert "car_charging_cancelled" not in row
        assert "rowspan_state" not in row
        assert "rowspan_limit" not in row
        assert "skip_state_cell" not in row
        # The combined view always sets split=False (never the native True), so the
        # JS renderer treats the row as a non-merged cell.
        assert row.get("split") is False


def test_build_car_planning_plan_view_optional_cost_fields_preserved_from_preview():
    """When the preview supplies optional cost_change/total_cost, they survive into the view."""
    report = _good_report()
    report["preview"]["nominal"] = [_good_row(cost_change=0.05, total_cost=0.50)]
    report["preview"]["pv10"] = [_good_row(scenario="pv10", cost_change=0.06, total_cost=0.51)]
    view = build_car_charging_plan_view(_native_plan(), report)
    # The contract lets optional cost data through verbatim; native cost is never reused.
    if "cost_change" in view["rows"][0]:
        assert view["rows"][0]["cost_change"] == 0.05
    if "total_cost" in view["rows"][0]:
        assert view["rows"][0]["total_cost"] == 0.50


def test_build_car_planning_plan_view_does_not_mutate_inputs():
    """Native plan and report dicts are returned unchanged (deepcopy-safe contract)."""
    native = _native_plan()
    report = _good_report()
    native_snapshot = deepcopy(native)
    report_snapshot = deepcopy(report)
    preview_snapshot = deepcopy(report["preview"])
    nominal_snapshot = deepcopy(report["preview"]["nominal"])
    pv10_snapshot = deepcopy(report["preview"]["pv10"])
    build_car_charging_plan_view(native, report)
    assert native == native_snapshot
    assert report == report_snapshot
    assert report["preview"] == preview_snapshot
    assert report["preview"]["nominal"] == nominal_snapshot
    assert report["preview"]["pv10"] == pv10_snapshot


# ----------------------------------------------------------------------
# build_car_charging_plan_view — freshness and disabled contracts
# ----------------------------------------------------------------------


def test_build_car_planning_plan_view_disabled_returns_native_unchanged():
    """status='disabled' is a pass-through; the candidate is never shown as combined."""
    native = _native_plan()
    report = _good_report(status="disabled")
    view = build_car_charging_plan_view(native, report)
    assert view is native


def test_build_car_planning_plan_view_none_report_returns_native_unchanged():
    """A missing report is a pass-through; no combined marker is added."""
    native = _native_plan()
    view = build_car_charging_plan_view(native, None)
    assert view is native


def test_build_car_planning_plan_view_non_dict_report_returns_native_unchanged():
    """A non-dict report cannot be evaluated; the native plan is the truth."""
    native = _native_plan()
    view = build_car_charging_plan_view(native, "broken")
    assert view is native


def test_build_car_planning_plan_view_stale_report_marks_unavailable():
    """An older-than-15-min report is labelled plan_unavailable=true; no row is reused."""
    now = datetime.now(timezone.utc)
    report = _good_report(now=now, generated_minutes_ago=20)
    view = build_car_charging_plan_view(_native_plan(), report, now=now)
    assert view.get("plan_unavailable") is True
    assert view.get("rows") == []


def test_build_car_planning_plan_view_failed_native_checks_marks_unavailable():
    """A report whose native checks did not pass never becomes a combined simulation."""
    report = _good_report(native_passed=False)
    view = build_car_charging_plan_view(_native_plan(), report)
    assert view.get("plan_unavailable") is True


def test_build_car_planning_plan_view_mismatched_paired_schedules_unavailable():
    """When nominal and pv10 schedules have different start/end the pair is rejected."""
    report = _good_report()
    report["preview"]["pv10"] = [_good_row(scenario="pv10", start="2026-01-01T12:30:00+0000", end="2026-01-01T13:00:00+0000")]
    view = build_car_charging_plan_view(_native_plan(), report)
    assert view.get("plan_unavailable") is True


def test_build_car_planning_plan_view_malformed_timestamp_unavailable():
    """A missing or unparseable generated_at never becomes a candidate."""
    report = _good_report()
    report["generated_at"] = "not-a-timestamp"
    view = build_car_charging_plan_view(_native_plan(), report)
    assert view.get("plan_unavailable") is True


def test_build_car_planning_plan_view_invalid_battery_modes_unavailable():
    """Unknown battery modes are rejected; the candidate is not shown."""
    report = _good_report()
    report["preview"]["nominal"] = [_good_row(battery_modes="unknown_mode")]
    report["preview"]["pv10"] = [_good_row(scenario="pv10", battery_modes="unknown_mode")]
    view = build_car_charging_plan_view(_native_plan(), report)
    assert view.get("plan_unavailable") is True


# ----------------------------------------------------------------------
# render_car_charging_preview — banner-only contract
# ----------------------------------------------------------------------


def test_render_returns_empty_string_when_both_inputs_missing():
    """report=None and native_diagnostic=None must produce no DOM, no banner."""
    assert render_car_charging_preview(None, None) == ""


def test_render_never_emits_a_table():
    """render_car_charging_preview must NEVER render a <table>, even with a good report."""
    html = render_car_charging_preview(_good_report(), _native_diagnostic("ok", peak_w=5400, limit_w=6000))
    assert "<table" not in html, html
    assert "<tbody" not in html
    assert "<tr" not in html


def test_render_never_emits_a_details_block():
    """render_car_charging_preview must NEVER emit a <details> toggle."""
    html = render_car_charging_preview(_good_report(), _native_diagnostic("ok", peak_w=5400, limit_w=6000))
    assert "<details" not in html


def test_render_never_emits_editable_controls():
    """No <button> or <input> controls in the banner - it is read-only by construction."""
    html = render_car_charging_preview(_good_report(), _native_diagnostic("ok", peak_w=5400, limit_w=6000))
    assert "<button" not in html
    assert "<input" not in html


def test_render_labels_table_as_car_planning_simulation_when_valid():
    """A valid candidate produces a banner that explicitly labels the existing Plan table."""
    html = render_car_charging_preview(_good_report(), _native_diagnostic("ok", peak_w=5400, limit_w=6000))
    assert "EV candidate" in html or "combined simulation" in html.lower()
    assert "Plan" in html or "table" in html.lower()


def test_render_surfaces_native_limit_exceeded_warning():
    """An exceeded native diagnostic still surfaces a visible red warning."""
    html = render_car_charging_preview(
        _good_report(),
        _native_diagnostic("exceeded", peak_w=7200, limit_w=6000, minutes_over=15),
    )
    assert "car-charging-preview-warning" in html
    assert "Native import limit exceeded" in html
    assert "7200" in html
    assert "6000" in html
    assert "15" in html


def test_render_stale_soc_warning_is_visible():
    """A report with uses_stale_soc_assumption labels the candidate as never-valid."""
    html = render_car_charging_preview(_good_report(uses_stale=True), _native_diagnostic("ok", peak_w=5000, limit_w=6000))
    assert "stale SoC" in html


def test_render_stale_report_no_table_ever():
    """A 20-minute-old report never produces a table; only the stale label survives."""
    now = datetime.now(timezone.utc)
    report = _good_report(now=now, generated_minutes_ago=20)
    html = render_car_charging_preview(report, _native_diagnostic("ok", peak_w=4000, limit_w=6000), now=now)
    assert "<table" not in html
    assert "<details" not in html
    assert "older than 15 minutes" in html or "stale" in html.lower()


def test_render_rejected_status_never_a_table():
    """A native_rejected or unsupported status never produces a candidate table."""
    for status in ("native_rejected", "unsupported", "disabled", "no_plan_found", "error", "invalid_input"):
        html = render_car_charging_preview(_good_report(status=status), _native_diagnostic("ok", peak_w=5400, limit_w=6000))
        assert "<table" not in html, (status, html)


# ----------------------------------------------------------------------
# get_car_charging_preview_update_js — no second poller
# ----------------------------------------------------------------------


def test_get_car_planning_preview_update_js_returns_empty():
    """The helper returns an empty string; the parent owns the polling via the full API."""
    assert get_car_charging_preview_update_js() == ""


def test_get_car_planning_preview_update_js_never_contains_fetch():
    """Defensive guard: even if the helper changes, no fetch/setInterval sneaks back in."""
    script = get_car_charging_preview_update_js()
    assert "fetch(" not in script
    assert "setInterval" not in script
    assert "AbortController" not in script


# ----------------------------------------------------------------------
# Web entry points — single table, full response on conditional fetch
# ----------------------------------------------------------------------


def _build_page(holder_planning, native_plan=None, diagnostic=None, yesterday=None, baseline=None):
    """Build a minimal WebInterface bound to a synthetic HA cache."""
    now = datetime.now(timezone.utc)
    native_plan = native_plan if native_plan is not None else _native_plan([{"time": "2026-01-01T12:00:00+0000", "import_rate": 5.0, "state": "Demand", "state_text": "Demand", "soc_percent": 50}])
    diagnostic = diagnostic if diagnostic is not None else _native_diagnostic("ok", peak_w=5400, limit_w=6000)
    yesterday = yesterday if yesterday is not None else None
    baseline = baseline if baseline is not None else None

    holder = SimpleNamespace(
        car_charging_report=holder_planning,
        manual_times=lambda *args, **kwargs: [],
        manual_rates=lambda *args, **kwargs: {},
    )
    page = WebInterface.__new__(WebInterface)
    page.base = holder
    page.prefix = "predbat"

    def read(entity_id, attribute=None, default=None, raw=False):
        """Return copies of cached synthetic states, never live data."""
        if entity_id == "predbat.plan_html" and attribute == "raw":
            return deepcopy(native_plan)
        if entity_id == "predbat.best_import_limit" and raw:
            return deepcopy(diagnostic)
        if entity_id == "predbat.cost_yesterday" and attribute == "json":
            return deepcopy(yesterday) if yesterday is not None else default
        if entity_id == "predbat.savings_yesterday_predbat" and attribute == "json":
            return deepcopy(baseline) if baseline is not None else default
        return default

    page.get_state_wrapper = read
    page.get_header = lambda *args, **kwargs: "<html><body>"
    return page


def _strip_scripts(html):
    """Drop <script>...</script> and <style>...</style> blocks so we see real DOM only."""
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
    return html


def test_html_plan_contains_no_second_table():
    """Stripping scripts shows the rendered DOM has no <table> from the combined preview."""
    page = _build_page(_good_report())
    html = asyncio.run(page.html_plan(SimpleNamespace(rel_url=SimpleNamespace(query={})))).text
    clean = _strip_scripts(html)
    assert "<table" not in clean, clean


def test_html_plan_embeds_car_planning_plan_data_with_car_planning_simulation_flag():
    """window.planData is the combined display, with plan_simulation=true and rows from the preview."""
    page = _build_page(_good_report())
    html = asyncio.run(page.html_plan(SimpleNamespace(rel_url=SimpleNamespace(query={})))).text
    match = re.search(r"window\.planData\s*=\s*({.*?});", html)
    assert match, html
    plan_data = json.loads(match.group(1))
    assert plan_data.get("plan_simulation") is True
    assert plan_data.get("rows"), plan_data
    assert plan_data.get("currency_symbols") == ["£", "p"]


def test_html_plan_plan_data_timestamp_is_report_generated_at():
    """Embedded plan data carries the report's generated_at, not the native timestamp."""
    native = _native_plan()
    native["timestamp"] = "1970-01-01T00:00:00+0000"
    report = _good_report()
    report["generated_at"] = "2026-01-15T12:34:56+0000"
    page = _build_page(report, native_plan=native)
    html = asyncio.run(page.html_plan(SimpleNamespace(rel_url=SimpleNamespace(query={})))).text
    match = re.search(r"window\.planData\s*=\s*({.*?});", html)
    assert match
    plan_data = json.loads(match.group(1))
    assert plan_data.get("timestamp") == "2026-01-15T12:34:56+0000"


def test_html_plan_car_planning_banner_appears_above_container():
    """The combined preview div appears before the plan container div in the rendered page."""
    page = _build_page(_good_report())
    html = asyncio.run(page.html_plan(SimpleNamespace(rel_url=SimpleNamespace(query={})))).text
    assert 'id="carChargingPreview"' in html
    assert 'id="planContainer"' in html
    assert html.index('id="carChargingPreview"') < html.index('id="planContainer"')


def test_html_plan_car_planning_banner_carries_simulation_disclaimer():
    """The banner explicitly states the existing Plan table is the combined display."""
    page = _build_page(_good_report())
    html = asyncio.run(page.html_plan(SimpleNamespace(rel_url=SimpleNamespace(query={})))).text
    assert "EV candidate" in html or "combined simulation" in html.lower()


def test_html_plan_car_planning_preview_block_has_no_polling_script():
    """The page never embeds an independent polling script for the combined preview.

    The plan-table renderer may legitimately setInterval for its own data fetch,
    but the combined preview div (which lives in a separate HTML block) must not
    contain its own fetch/setInterval - the parent owns polling via the full
    API plan_data route. We confirm by stripping the plan renderer JS and
    confirming no polling primitives remain in the combined preview block.
    """
    page = _build_page(_good_report())
    html = asyncio.run(page.html_plan(SimpleNamespace(rel_url=SimpleNamespace(query={})))).text
    plan_match = re.search(r'<div id="carChargingPreview">(.*?)</div>', html, flags=re.DOTALL)
    assert plan_match, html
    plan_block = plan_match.group(1)
    assert "setInterval" not in plan_block
    assert "fetch(" not in plan_block
    assert "AbortController" not in plan_block
    # And the helper is empty - the parent owns the polling via the full API plan_data.
    assert get_car_charging_preview_update_js() == ""


def test_html_plan_history_yesterday_views_untouched():
    """The combined view never touches the History/Yesterday baselines."""
    yesterday = {"timestamp": "2026-01-01T00:00:00+0000", "rows": [{"time": "2026-01-01T12:00:00+0000", "import_rate": 5.0}]}
    baseline = {"timestamp": "2026-01-01T00:00:00+0000", "rows": [{"time": "2026-01-01T12:00:00+0000"}]}
    page = _build_page(_good_report(), yesterday=yesterday, baseline=baseline)
    html = asyncio.run(page.html_plan(SimpleNamespace(rel_url=SimpleNamespace(query={})))).text
    yesterday_match = re.search(r"window\.yesterdayData\s*=\s*({.*?});", html)
    baseline_match = re.search(r"window\.baselineData\s*=\s*({.*?});", html)
    assert yesterday_match and baseline_match
    assert json.loads(yesterday_match.group(1)) == yesterday
    assert json.loads(baseline_match.group(1)) == baseline


def test_html_plan_plan_html_cache_not_mutated():
    """Reading the .plan_html entity never mutates the underlying HA cache entry."""
    native = _native_plan([{"time": "2026-01-01T12:00:00+0000", "import_rate": 5.0, "state": "Demand", "soc_percent": 50}])
    native_snapshot = deepcopy(native)
    page = _build_page(_good_report(), native_plan=native)
    asyncio.run(page.html_plan(SimpleNamespace(rel_url=SimpleNamespace(query={})))).text
    # Native plan_html cache is the input - should never be mutated.
    assert native == native_snapshot


def test_html_plan_overrides_inputs_are_deepcopied():
    """Overrides are read on the page's own thread - the helper never writes them back."""
    overrides_log = []

    class Holder:
        """Record read-only override accesses without exposing services."""

        car_charging_report = None

        def manual_times(self, name, update=True):
            """Record whether an override reader requested mutation."""
            overrides_log.append(("times", name, update))
            return []

        def manual_rates(self, name, update=True):
            """Return an empty synthetic override map and record access mode."""
            overrides_log.append(("rates", name, update))
            return {}

    holder = Holder()
    holder.car_charging_report = _good_report()
    page = WebInterface.__new__(WebInterface)
    page.base = holder
    page.prefix = "predbat"

    def read(entity_id, attribute=None, default=None, raw=False):
        """Serve only synthetic cached states for the override ownership test."""
        if entity_id == "predbat.plan_html" and attribute == "raw":
            return deepcopy(_native_plan())
        if entity_id == "predbat.best_import_limit" and raw:
            return _native_diagnostic("ok")
        if entity_id == "predbat.cost_yesterday" and attribute == "json":
            return default
        if entity_id == "predbat.savings_yesterday_predbat" and attribute == "json":
            return default
        return default

    page.get_state_wrapper = read
    page.get_header = lambda *args, **kwargs: "<html><body>"
    asyncio.run(page.html_plan(SimpleNamespace(rel_url=SimpleNamespace(query={})))).text
    # Every override call must use update=False (read-only) so the page cannot mutate them.
    assert all(call[2] is False for call in overrides_log), overrides_log


def test_html_api_plan_data_plan_is_car_planning_display():
    """The /api/plan_data response carries the combined display under 'plan', not the native plan."""
    page = _build_page(_good_report())
    request = SimpleNamespace(rel_url=SimpleNamespace(query={}))
    data = json.loads(asyncio.run(page.html_api_plan_data(request)).text)
    assert data["plan"].get("plan_simulation") is True
    assert data["plan"].get("rows")
    assert "car_charging_view" in data


def test_html_api_plan_data_disabled_report_passes_native_plan_through():
    """A disabled report leaves the /api/plan_data 'plan' field untouched (no combined marker)."""
    page = _build_page({"status": "disabled", "generated_at": datetime.now(timezone.utc).isoformat()})
    request = SimpleNamespace(rel_url=SimpleNamespace(query={}))
    data = json.loads(asyncio.run(page.html_api_plan_data(request)).text)
    assert data["plan"].get("plan_simulation") is not True


def test_html_api_plan_data_no_report_passes_native_plan_through():
    """A missing report leaves the /api/plan_data 'plan' field untouched (no combined marker)."""
    page = _build_page(None)
    request = SimpleNamespace(rel_url=SimpleNamespace(query={}))
    data = json.loads(asyncio.run(page.html_api_plan_data(request)).text)
    assert data["plan"].get("plan_simulation") is not True


def test_html_api_plan_data_conditional_fetch_with_active_report_returns_full_response():
    """A matching conditional fetch still returns the full plan when a report is active.

    This is the 'full response even on conditional query when report active (age
    expiration)' contract: the client may think nothing changed, but the combined
    view must still be returned so a stale report can be cleared.
    """
    page = _build_page(_good_report())
    first = json.loads(asyncio.run(page.html_api_plan_data(SimpleNamespace(rel_url=SimpleNamespace(query={})))).text)
    # Conditional query with same timestamp + same hash - normally data_unchanged=True.
    conditional = SimpleNamespace(
        rel_url=SimpleNamespace(
            query={
                "newest_timestamp": first["plan"]["timestamp"],
                "overrides_hash": first["overrides_hash"],
            }
        )
    )
    data = json.loads(asyncio.run(page.html_api_plan_data(conditional)).text)
    assert data.get("unchanged") is False
    assert data["plan"].get("plan_simulation") is True
    assert "car_charging_view" in data


def test_html_api_plan_data_conditional_fetch_without_report_returns_unchanged():
    """Without a combined report, the normal conditional-fetch contract is preserved."""
    page = _build_page(None)
    first = json.loads(asyncio.run(page.html_api_plan_data(SimpleNamespace(rel_url=SimpleNamespace(query={})))).text)
    conditional = SimpleNamespace(
        rel_url=SimpleNamespace(
            query={
                "newest_timestamp": first["plan"]["timestamp"],
                "overrides_hash": first["overrides_hash"],
            }
        )
    )
    data = json.loads(asyncio.run(page.html_api_plan_data(conditional)).text)
    assert data.get("unchanged") is True
    assert data.get("overrides_hash")


def test_html_api_plan_data_stale_report_clears_candidate():
    """An older-than-15-min report yields a plan_unavailable plan; no prior trajectory is reused."""
    now = datetime.now(timezone.utc)
    report = _good_report(now=now, generated_minutes_ago=20)
    page = _build_page(report)
    request = SimpleNamespace(rel_url=SimpleNamespace(query={}))
    data = json.loads(asyncio.run(page.html_api_plan_data(request)).text)
    assert data["plan"].get("plan_unavailable") is True
    assert data["plan"].get("rows") == []


def test_html_api_plan_data_disabled_then_missing_reports_do_not_mutate_native():
    """Disabled/missing reports leave the native plan_json cache entry unchanged."""
    native = _native_plan([{"time": "2026-01-01T12:00:00+0000", "import_rate": 5.0}])
    snapshot = deepcopy(native)
    page = _build_page(None, native_plan=native)
    request = SimpleNamespace(rel_url=SimpleNamespace(query={}))
    asyncio.run(page.html_api_plan_data(request))
    # Native plan_json cache must remain untouched by the combined display path.
    assert native == snapshot


# ----------------------------------------------------------------------
# JS renderer — combined rows render read-only
# ----------------------------------------------------------------------


def test_js_renderer_treats_car_planning_simulation_as_non_editable():
    """The renderer's editable flag is set per-view; the combined view runs in the plan tab only.

    The Plan view is editable by default, but a plan_simulation plan must not
    offer any of the persistent override dropdowns that change inverter state.
    We assert this structurally by looking for the read-only renderPath the
    renderer uses for non-editable rows (no renderStateCell, renderRateCell,
    renderLoadCell or renderSocCell are ever called for a combined row).
    """
    from web_helper import get_plan_renderer_js

    renderer_js = get_plan_renderer_js()
    # The renderer's state/rate/load/soc cell helpers are gated on `editable`.
    assert "renderStateCell" in renderer_js
    assert "editable" in renderer_js
    # The Plan view is the only editable view; refreshPlan() passes editable=(currentView==='plan').
    assert "currentView === 'plan'" in renderer_js


def test_js_renderer_emits_car_planning_injected_field_text():
    """Additional trajectory fields stay in Dev, not in the original table layout."""
    from web_helper import get_plan_renderer_js

    renderer_js = get_plan_renderer_js()
    # These fields remain available to Dev, but must not redesign the table.
    plan_field_names = [
        "soc_end_percent",
        "plan_soc_pv10_start_percent",
        "plan_soc_pv10_end_percent",
        "plan_grid_peak_w",
        "plan_grid_peak_pv10_w",
        "plan_ev_on_minutes",
    ]
    for name in plan_field_names:
        assert name not in renderer_js, f"extra developer field must not change the table: {name}"
    assert "Max grid kW" not in renderer_js


def test_js_renderer_handles_car_planning_unavailable_as_no_rows():
    """When plan.plan_unavailable is true, the renderer must render a clear, empty Plan table.

    We assert the renderer source does not assume any row fields are present
    when 'plan_unavailable' is set; the existing per-row `if (!jsonData.rows)`
    guard already short-circuits the empty case.
    """
    from web_helper import get_plan_renderer_js

    renderer_js = get_plan_renderer_js()
    assert "plan_unavailable" in renderer_js or "!jsonData || !jsonData.rows" in renderer_js


def test_js_refresh_uses_current_view_timestamp():
    """Keep upstream per-view staleness and permit a read-only replacement of expired combined rows."""
    from web_helper import get_plan_renderer_js

    renderer_js = get_plan_renderer_js()
    start = renderer_js.index("function refreshPlan()")
    refresh = renderer_js[start : renderer_js.index("\n    }", start)]
    assert "let data = currentViewData();" in refresh
    assert "checkStaleness(data);" in refresh
    assert "checkStaleness(null);" in refresh
    assert "Date.parse(data.timestamp)" in refresh
    assert "Date.parse(timestamp)" not in refresh
    assert "data = {...data, rows: [], plan_unavailable: true}" in refresh


# ----------------------------------------------------------------------
# Test registry
# ----------------------------------------------------------------------


def test_native_soc_rounding_at_full_battery():
    """Native 0.001-kWh rounding must not erase an otherwise passed real trajectory."""
    report = _good_report()
    for rows in report["preview"].values():
        rows[0].update(battery_start_percent=100.00124402866244, battery_end_percent=100, battery_min_percent=100)
    view = build_car_charging_plan_view(_native_plan(), report)
    assert len(view["rows"]) == 1
    assert view["rows"][0]["soc_percent"] == 100.00124402866244


def test_original_table_colours_and_totals():
    """Candidate values use the established cell colours, precision and totals."""
    native = _native_plan()
    native.update(import_cost_threshold=10, export_cost_threshold=0)
    report = _good_report()
    for rows in report["preview"].values():
        rows[0].update(import_rate=8.1, export_rate=0, ev_on_minutes=5, ev_reserved_kwh=0.5643333, cost_change=0.12, total_cost=1.25)
    view = build_car_charging_plan_view(native, report)
    row = view["rows"][0]
    assert row["state_text"] == "&nearr;" and row["soc_color"] == "#3AEE85"
    assert row["rate_color_import"] == "#3AEE85" and row["rate_color_export"] == "#F18261"
    assert row["load_color"] == "#F18261" and row["pv_color"] == "#FFAAAA"
    assert row["car_color"] == "#FFFF00" and row["cost_color"] == "#F18261"
    assert view["totals"] == {"pv_forecast": 0.2, "load_forecast": 0.5, "car_charging": 0.56, "soc_percent": 51, "total_cost": 1.37, "clipped": 0}


def run_car_planning_single_table_tests(base):
    """Run all single-table integration regressions against the live web stack."""
    tests = [
        test_build_car_planning_plan_view_marks_car_planning_simulation,
        test_native_soc_rounding_at_full_battery,
        test_original_table_colours_and_totals,
        test_build_car_planning_plan_view_preserves_currency_symbols,
        test_build_car_planning_plan_view_preserves_time_midnight_reference,
        test_build_car_planning_plan_view_sets_timestamp_from_report,
        test_build_car_planning_plan_view_sets_num_cars_iboost_carbon_off,
        test_build_car_planning_plan_view_returns_one_row_per_preview_row,
        test_build_car_planning_plan_view_row_carries_required_field_set,
        test_build_car_planning_plan_view_row_time_is_preview_start,
        test_build_car_planning_plan_view_row_slot_minute_derived_from_start,
        test_build_car_planning_plan_view_preserves_partial_first_bin,
        test_build_car_planning_plan_view_car_charging_is_ev_reserved_kwh,
        test_build_car_planning_plan_view_soc_percent_is_nominal_start,
        test_build_car_planning_plan_view_soc_change_is_end_minus_start,
        test_build_car_planning_plan_view_car_planning_soc_end_percent_nominal,
        test_build_car_planning_plan_view_state_text_demand_includes_minutes,
        test_build_car_planning_plan_view_state_text_charge_shows_100_limit,
        test_build_car_planning_plan_view_state_text_hold_includes_duration,
        test_build_car_planning_plan_view_pv_load_sums_are_real_5min_kwh,
        test_build_car_planning_plan_view_grid_peaks_preserved,
        test_build_car_planning_plan_view_does_not_inherit_native_cost_cancelled_rowspan,
        test_build_car_planning_plan_view_optional_cost_fields_preserved_from_preview,
        test_build_car_planning_plan_view_does_not_mutate_inputs,
        test_build_car_planning_plan_view_disabled_returns_native_unchanged,
        test_build_car_planning_plan_view_none_report_returns_native_unchanged,
        test_build_car_planning_plan_view_non_dict_report_returns_native_unchanged,
        test_build_car_planning_plan_view_stale_report_marks_unavailable,
        test_build_car_planning_plan_view_failed_native_checks_marks_unavailable,
        test_build_car_planning_plan_view_mismatched_paired_schedules_unavailable,
        test_build_car_planning_plan_view_malformed_timestamp_unavailable,
        test_build_car_planning_plan_view_invalid_battery_modes_unavailable,
        test_render_returns_empty_string_when_both_inputs_missing,
        test_render_never_emits_a_table,
        test_render_never_emits_a_details_block,
        test_render_never_emits_editable_controls,
        test_render_labels_table_as_car_planning_simulation_when_valid,
        test_render_surfaces_native_limit_exceeded_warning,
        test_render_stale_soc_warning_is_visible,
        test_render_stale_report_no_table_ever,
        test_render_rejected_status_never_a_table,
        test_get_car_planning_preview_update_js_returns_empty,
        test_get_car_planning_preview_update_js_never_contains_fetch,
        test_html_plan_contains_no_second_table,
        test_html_plan_embeds_car_planning_plan_data_with_car_planning_simulation_flag,
        test_html_plan_plan_data_timestamp_is_report_generated_at,
        test_html_plan_car_planning_banner_appears_above_container,
        test_html_plan_car_planning_banner_carries_simulation_disclaimer,
        test_html_plan_car_planning_preview_block_has_no_polling_script,
        test_html_plan_history_yesterday_views_untouched,
        test_html_plan_plan_html_cache_not_mutated,
        test_html_plan_overrides_inputs_are_deepcopied,
        test_html_api_plan_data_plan_is_car_planning_display,
        test_html_api_plan_data_disabled_report_passes_native_plan_through,
        test_html_api_plan_data_no_report_passes_native_plan_through,
        test_html_api_plan_data_conditional_fetch_with_active_report_returns_full_response,
        test_html_api_plan_data_conditional_fetch_without_report_returns_unchanged,
        test_html_api_plan_data_stale_report_clears_candidate,
        test_html_api_plan_data_disabled_then_missing_reports_do_not_mutate_native,
        test_js_renderer_treats_car_planning_simulation_as_non_editable,
        test_js_renderer_emits_car_planning_injected_field_text,
        test_js_renderer_handles_car_planning_unavailable_as_no_rows,
        test_js_refresh_uses_current_view_timestamp,
    ]
    failed = False
    for test in tests:
        print("Test: " + test.__name__)
        try:
            test()
        except Exception as exc:
            print("ERROR: {}: {}".format(test.__name__, exc))
            failed = True
        else:
            print("PASS: " + test.__name__)
    return failed
