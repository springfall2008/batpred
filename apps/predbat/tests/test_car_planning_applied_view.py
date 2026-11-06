"""Truthful applied-plan presentation tests for the native plan view.

The parent executor writes ``status='applied'`` with
``requested_mode='apply'``, ``execution_enabled=True``,
``actuator_signals_modified=True``, ``execution_observed_at``,
``execution_valid_until`` and may also write ``status='apply_blocked'`` on
its own failures. The preview module must show the actual corrected plan
when paired rows + fresh receipt + coherent ``generated_at`` are all in
force, and clear rows + surface a bounded ``execution_blocked`` reason
otherwise. Shadow candidate behaviour (``plan_simulation=True``) is
preserved.
"""

import asyncio
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from car_charging_view import (
    _ALLOWED_BLOCKED_REASONS,
    _applied_display_allowed,
    _applied_receipt_fresh,
    _paired_preview,
    build_car_charging_plan_view,
    render_car_charging_preview,
)
from web import WebInterface


def _now():
    """Return the fixed clock used by deterministic presentation tests."""
    return datetime(2026, 10, 12, 12, 0, 0, tzinfo=timezone.utc)


def _wall_now():
    """Anchor web-route fixtures to the same real clock used by the renderer."""
    return datetime.now(timezone.utc)


def _good_row(scenario="nominal", start="2026-01-01T12:00:00+0000", end="2026-01-01T12:30:00+0000", minutes=30, **overrides):
    """Build a complete synthetic replay row without borrowing native values."""
    row = {
        "start": start,
        "end": end,
        "minutes": minutes,
        "battery_modes": "demand",
        "ev_on_minutes": 0,
        "ev_reserved_kwh": 0.0,
        "house_kwh": 0.5,
        "pv_kwh": 0.2,
        "battery_start_percent": 50.0,
        "battery_end_percent": 51.0,
        "battery_min_percent": 50.0,
        "grid_peak_w": 1500.0,
        "mode_minutes": {"demand": minutes},
    }
    for key, value in overrides.items():
        row[key] = value
    return row


def _good_report(
    status="candidate",
    requested_mode="shadow",
    execution_enabled=False,
    actuator_signals_modified=False,
    execution_observed_at=None,
    execution_valid_until=None,
    generated_minutes_ago=5,
    native_passed=True,
    now=None,
    execution_reason=None,
    uses_stale=False,
    preview=None,
    blockers=None,
    counterfactual_controls=False,
    status_block=None,
):
    """Construct a paired report with independently selectable evidence gates."""
    now = now if now is not None else _now()
    report = {
        "status": status,
        "requested_mode": requested_mode,
        "execution_enabled": execution_enabled,
        "actuator_signals_modified": actuator_signals_modified,
        "generated_at": (now - timedelta(minutes=generated_minutes_ago)).isoformat(),
        "uses_stale_soc_assumption": uses_stale,
        "preview": preview
        if preview is not None
        else {
            "nominal": [_good_row(scenario="nominal", import_rate=5.0, export_rate=0.0, cost_change=0.12, total_cost=1.25)],
            "pv10": [_good_row(scenario="pv10", import_rate=5.0, export_rate=0.0)],
        },
        "native_checks": {"nominal": {"passed": native_passed}, "pv10": {"passed": native_passed}},
    }
    if execution_observed_at is not None:
        report["execution_observed_at"] = execution_observed_at
    if execution_valid_until is not None:
        report["execution_valid_until"] = execution_valid_until
    if execution_reason is not None:
        report["execution_reason"] = execution_reason
    if blockers is not None:
        report["execution_blockers"] = blockers
    if counterfactual_controls:
        report["counterfactual_controls"] = True
    if status_block is not None:
        report["status"] = status_block
    return report


def _applied_report(**overrides):
    """Build a fresh applied receipt anchored to the wall clock so it survives
    ``html_api_plan_data``-style ``datetime.now`` paths. ``generated_at`` is
    pinned to ``observed_at - 1s`` to mirror the parent's executor."""
    now = overrides.pop("now", _wall_now())
    interval = overrides.pop("interval_seconds", 60)
    observed = overrides.pop("observed_at", now - timedelta(seconds=1))
    valid_until = overrides.pop("valid_until", observed + timedelta(seconds=interval))
    base = {
        "status": "applied",
        "requested_mode": "apply",
        "execution_enabled": True,
        "actuator_signals_modified": True,
        "execution_observed_at": observed.isoformat(),
        "execution_valid_until": valid_until.isoformat(),
    }
    base.update(overrides)
    base.setdefault("generated_minutes_ago", max(0.0, (now - observed).total_seconds() / 60.0))
    return _good_report(now=now, **base)


def _native_plan():
    """Provide native display metadata without a candidate trajectory."""
    return {
        "timestamp": "2026-01-01T12:00:00+0000",
        "time": "2026-01-01T00:00:00+0000",
        "currency_symbols": ["£", "p"],
        "num_cars": 0,
        "iboost_enable": False,
        "carbon_enable": False,
        "import_cost_threshold": 10,
        "export_cost_threshold": 0,
        "rows": [],
    }


def _native_diag(state, peak_w=None, limit_w=None, minutes_over=None):
    """Represent the independent native import diagnostic."""
    attrs = {}
    if peak_w is not None:
        attrs["peak_w"] = peak_w
    if limit_w is not None:
        attrs["limit_w"] = limit_w
    if minutes_over is not None:
        attrs["minutes_over"] = minutes_over
    return {"state": state, "attributes": attrs}


def _build_page(report, native_plan=None, diag=None):
    """Create an in-memory web page backed only by the supplied reports."""
    native_plan = native_plan if native_plan is not None else _native_plan()
    diag = diag if diag is not None else _native_diag("ok", peak_w=5400, limit_w=6000)
    holder = SimpleNamespace(car_charging_report=report, manual_times=lambda *a, **k: [], manual_rates=lambda *a, **k: {})
    page = WebInterface.__new__(WebInterface)
    page.base = holder
    page.prefix = "predbat"

    def read(entity_id, attribute=None, default=None, raw=False):
        """Serve isolated native metadata and diagnostics without Home Assistant."""
        if entity_id == "predbat.plan_html" and attribute == "raw":
            return deepcopy(native_plan)
        if entity_id == "predbat.best_import_limit" and raw:
            return deepcopy(diag)
        return default

    page.get_state_wrapper = read
    page.get_header = lambda *a, **k: "<html><body>"
    return page


# ----------------------------------------------------------------------
# Gap 1: valid applied view must set plan_simulation=False
# ----------------------------------------------------------------------


def test_valid_applied_view_sets_car_planning_simulation_false():
    """Fresh observed execution is not labelled as a shadow simulation."""
    now = _wall_now()
    view = build_car_charging_plan_view(_native_plan(), _applied_report(now=now), now=now)
    assert view["plan_simulation"] is False, view
    assert view["plan_execution"] is True, view
    assert "execution_blocked" not in view, view
    assert len(view["rows"]) == 1, view


def test_valid_applied_view_preserves_execution_dates():
    """Retain the original execution receipt dates without renewing them."""
    now = _wall_now()
    report = _applied_report(now=now)
    view = build_car_charging_plan_view(_native_plan(), report, now=now)
    assert view["execution_observed_at"] == report["execution_observed_at"], view
    assert view["execution_valid_until"] == report["execution_valid_until"], view


def test_shadow_candidate_keeps_car_planning_simulation_true():
    """Unexecuted shadow candidates must retain their simulation label."""
    now = _wall_now()
    shadow = _good_report(now=now, status="candidate", requested_mode="shadow")
    view = build_car_charging_plan_view(_native_plan(), shadow, now=now)
    assert view["plan_simulation"] is True, view
    assert "plan_execution" not in view, view


# ----------------------------------------------------------------------
# Gap 2: render applied-fresh branch requires paired + checks + coherent generated
# ----------------------------------------------------------------------


def test_render_fresh_applied_uses_active_plan_label():
    """Name the active plan without claiming the whole forecast was executed."""
    html = render_car_charging_preview(
        _applied_report(now=_wall_now()),
        _native_diag("ok", peak_w=5400, limit_w=6000),
        now=_wall_now(),
    )
    assert "Active plan" in html, html
    assert "Applied plan (executed)" not in html, html


def test_render_fresh_applied_mainline_has_no_iso_dates():
    """Keep detailed timestamps under Show Debug rather than in the compact line."""
    html = render_car_charging_preview(
        _applied_report(now=_wall_now()),
        _native_diag("ok", peak_w=5400, limit_w=6000),
        now=_wall_now(),
    )
    # Strip the Show Debug spans — mainline must be ISO-free.
    visible = re.sub(r'<div class="car-charging-preview-debug"[^>]*>.*?</div>', "", html, flags=re.DOTALL)
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", visible), visible
    # ISO timestamps still appear in Show Debug.
    assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", html), html


def test_render_fresh_applied_suppresses_simulation_only_banner():
    """Do not apply an obsolete simulation warning to verified current execution."""
    html = render_car_charging_preview(
        _applied_report(now=_wall_now()),
        _native_diag("ok", peak_w=5400, limit_w=6000),
        now=_wall_now(),
    )
    for forbidden in ("Simulation only — not executed", "EV SOC unconfirmed", "native grid limit exceeded", "Details: Show Debug"):
        assert forbidden not in html, (forbidden, html)


def test_render_fresh_applied_suppresses_visible_native_grid_warning():
    """Keep the unrelated native counterfactual warning out of the active banner."""
    html = render_car_charging_preview(
        _applied_report(now=_wall_now()),
        _native_diag("exceeded", peak_w=7200, limit_w=6000, minutes_over=15),
        now=_wall_now(),
    )
    # Every block that mentions the native-grid diagnostic must carry
    # ``display:none`` so the visible mainline never claims a current
    # native-grid-limit breach once applied.
    matches = re.findall(
        r'<div[^>]*style="([^"]*)"[^>]*>(?:(?!</div>).)*Native import limit exceeded',
        html,
        flags=re.DOTALL,
    )
    assert matches, html
    for style_attr in matches:
        assert "display:none" in style_attr, style_attr
    # Obsolete counterfactual lives under Show Debug only.
    assert "obsolete native counterfactual" in html, html


def test_render_fresh_applied_no_button_or_input():
    """Displaying execution must not introduce manual actuator controls."""
    html = render_car_charging_preview(
        _applied_report(now=_wall_now()),
        _native_diag("ok", peak_w=5400, limit_w=6000),
        now=_wall_now(),
    )
    assert "<button" not in html, html
    assert "<input" not in html, html


def test_render_applied_with_missing_preview_falls_back_to_execution_blocked():
    """Missing paired rows prevent an active-plan claim."""
    now = _wall_now()
    report = _applied_report(now=now)
    report["preview"] = {}  # No paired rows
    html = render_car_charging_preview(report, _native_diag("ok"), now=now)
    assert "Active plan" not in html, html
    assert "execution blocked" in html.lower(), html
    assert "Simulation only — not executed" not in html, html


def test_render_applied_with_stale_generated_at_falls_back_to_execution_blocked():
    """A recent receipt cannot revive an expired forecast."""
    now = _wall_now()
    report = _applied_report(now=now, generated_minutes_ago=30)
    html = render_car_charging_preview(report, _native_diag("ok"), now=now)
    assert "Active plan" not in html, html
    assert "execution blocked" in html.lower(), html


def test_render_applied_with_stale_soc_hypothesis_falls_back_to_execution_blocked():
    """An assumed stale SOC cannot be promoted to executable evidence."""
    now = _wall_now()
    report = _applied_report(now=now, uses_stale=True)
    html = render_car_charging_preview(report, _native_diag("ok"), now=now)
    assert "Active plan" not in html, html
    assert "execution blocked" in html.lower(), html


def test_render_applied_with_counterfactual_controls_falls_back_to_execution_blocked():
    """Counterfactual control capability never counts as applied execution."""
    now = _wall_now()
    report = _applied_report(now=now, counterfactual_controls=True)
    html = render_car_charging_preview(report, _native_diag("ok"), now=now)
    assert "Active plan" not in html, html
    assert "execution blocked" in html.lower(), html


def test_render_applied_with_failed_paired_checks_falls_back_to_execution_blocked():
    """Both replay checks must pass before the active banner is rendered."""
    now = _wall_now()
    report = _applied_report(now=now, native_passed=False)
    html = render_car_charging_preview(report, _native_diag("ok"), now=now)
    assert "Active plan" not in html, html


# ----------------------------------------------------------------------
# Gap 3: execution_blockers fail closed (only empty list OK)
# ----------------------------------------------------------------------


def test_execution_blockers_non_list_rejects_fresh():
    """Malformed blocker collections fail closed despite a recent receipt."""
    now = _wall_now()
    report = _applied_report(now=now)
    report["execution_blockers"] = "permission_not_granted"  # wrong type
    assert _applied_display_allowed(report, now, _paired_preview(report, now)) is False
    assert _applied_receipt_fresh(report, now) is False


def test_execution_blockers_nonempty_list_rejects_fresh():
    """Any execution blocker prevents active-plan presentation."""
    now = _wall_now()
    report = _applied_report(now=now, blockers=["permission_not_granted"])
    assert _applied_receipt_fresh(report, now) is False


def test_execution_blockers_empty_list_accepts_fresh():
    """An explicitly empty blocker list permits otherwise valid evidence."""
    now = _wall_now()
    report = _applied_report(now=now, blockers=[])
    assert _applied_receipt_fresh(report, now) is True


def test_execution_blockers_absent_accepts_fresh():
    """Legacy reports may omit blockers when every positive gate passes."""
    now = _wall_now()
    report = _applied_report(now=now)
    report.pop("execution_blockers", None)
    assert _applied_receipt_fresh(report, now) is True


# ----------------------------------------------------------------------
# Gap 4: block reason is bounded, not echoed arbitrarily
# ----------------------------------------------------------------------


def test_block_reason_echoes_only_allowlisted_labels():
    """Render only the bounded public reason labels."""
    from car_charging_view import _applied_block_reason

    now = _wall_now()
    for label in _ALLOWED_BLOCKED_REASONS:
        report = _applied_report(now=now)
        report["execution_reason"] = label
        assert _applied_block_reason(report, now) == label, label


def test_block_reason_drops_unknown_reason_strings():
    """Never echo arbitrary executor text into the blocked banner."""
    from car_charging_view import _applied_block_reason

    now = _wall_now()
    report = _applied_report(now=now)
    report["execution_reason"] = "private internal trace id"
    reason = _applied_block_reason(report, now)
    assert "private" not in reason, reason
    assert "internal" not in reason, reason


def test_block_reason_uses_now_check_when_no_reason():
    """Derive missing reasons from actual receipt and clock coherence."""
    from car_charging_view import _applied_block_reason

    now = _wall_now()
    # observed_at slightly in the past, valid_until only 1s after observed,
    # so now >= valid_until and the function should report "expired".
    observed = now - timedelta(seconds=2)
    valid_until = observed + timedelta(seconds=1)
    report = _good_report(
        now=now,
        status="applied",
        requested_mode="apply",
        execution_enabled=True,
        actuator_signals_modified=True,
        execution_observed_at=observed.isoformat(),
        execution_valid_until=valid_until.isoformat(),
        generated_minutes_ago=0.05,
    )
    reason = _applied_block_reason(report, now)
    assert "expired" in reason, reason


def test_block_reason_inverter_observed_label_generic():
    """Do not present an observation disclaimer as a physical confirmation."""
    from car_charging_view import _applied_block_reason

    now = _wall_now()
    report = _applied_report(now=now)
    report["execution_reason"] = "inverter_observed_not_hardware_confirmed"
    reason = _applied_block_reason(report, now)
    assert reason == "awaiting hardware confirmation", reason


# ----------------------------------------------------------------------
# Gap 5: apply_blocked status lands on execution_blocked, not plan_unavailable
# ----------------------------------------------------------------------


def test_apply_blocked_status_surfaces_execution_blocked():
    """An executor rejection is an execution block rather than a simulation."""
    now = _wall_now()
    report = _applied_report(now=now, status_block="apply_blocked", execution_reason="apply_commissioning_required")
    view = build_car_charging_plan_view(_native_plan(), report, now=now)
    assert view.get("execution_blocked") == "apply_commissioning_required", view
    assert view.get("plan_unavailable") is not True, view
    assert view["plan_simulation"] is False, view
    assert view["rows"] == [], view


def test_apply_blocked_status_with_unknown_reason_uses_coherence_reason():
    """Unknown reasons fall back to bounded evidence diagnostics."""
    now = _wall_now()
    report = _applied_report(now=now, status_block="apply_blocked", execution_reason="private leak")
    view = build_car_charging_plan_view(_native_plan(), report, now=now)
    assert "private" not in view.get("execution_blocked", ""), view
    assert "leak" not in view.get("execution_blocked", ""), view


def test_shadow_candidate_unavailable_keeps_generic_replay_path():
    """Preserve the unavailable-shadow display contract."""
    now = _wall_now()
    shadow = _good_report(now=now, status="native_rejected")
    view = build_car_charging_plan_view(_native_plan(), shadow, now=now)
    assert view.get("plan_unavailable") is True, view
    assert "execution_blocked" not in view, view
    assert view["plan_simulation"] is True, view


# ----------------------------------------------------------------------
# Receipt freshness: original gating contract, end-to-end
# ----------------------------------------------------------------------


def test_fresh_applied_receipt_passes():
    """Accept a coherent and still valid current-command receipt."""
    now = _wall_now()
    report = _applied_report(now=now)
    assert _applied_receipt_fresh(report, now) is True


def test_expired_applied_receipt_rejects():
    """Reject an expired execution interval."""
    now = _wall_now()
    report = _applied_report(now=now, valid_until=now - timedelta(seconds=1), observed_at=now - timedelta(seconds=61))
    assert _applied_receipt_fresh(report, now) is False


def test_missing_observed_at_rejects():
    """Require an explicit command observation time."""
    now = _wall_now()
    report = _applied_report(now=now)
    report["execution_observed_at"] = None
    assert _applied_receipt_fresh(report, now) is False


def test_missing_valid_until_rejects():
    """Require bounded expiry rather than open-ended execution authority."""
    now = _wall_now()
    report = _applied_report(now=now)
    report["execution_valid_until"] = None
    assert _applied_receipt_fresh(report, now) is False


def test_future_observed_at_rejects():
    """Future observations cannot authorise a present active-plan display."""
    now = _wall_now()
    future_obs = now + timedelta(seconds=120)
    report = _applied_report(now=now, observed_at=future_obs, valid_until=future_obs + timedelta(seconds=60))
    assert _applied_receipt_fresh(report, now) is False


def test_false_authority_rejects_execution_enabled_false():
    """Disabled execution cannot be made active by receipt fields alone."""
    now = _wall_now()
    report = _applied_report(now=now, execution_enabled=False)
    assert _applied_receipt_fresh(report, now) is False


def test_false_authority_rejects_actuator_signals_modified_false():
    """A report without executor signalling remains unexecuted."""
    now = _wall_now()
    report = _applied_report(now=now, actuator_signals_modified=False)
    assert _applied_receipt_fresh(report, now) is False


def test_wrong_requested_mode_rejects():
    """Only explicit apply mode can produce the active-plan presentation."""
    now = _wall_now()
    report = _applied_report(now=now, requested_mode="shadow")
    assert _applied_receipt_fresh(report, now) is False


def test_interval_over_300_seconds_rejects():
    """Reject execution leases exceeding the five-minute authority bound."""
    now = _wall_now()
    report = _applied_report(now=now, interval_seconds=400)
    assert _applied_receipt_fresh(report, now) is False


def test_invalid_applied_clears_rows_and_no_native_fallback():
    """Invalid execution clears rows instead of substituting the native plan."""
    now = _wall_now()
    native = _native_plan()
    native["rows"] = [{"time": "2026-01-01T12:00:00+0000", "import_rate": 5.0}]
    report = _applied_report(now=now, valid_until=now - timedelta(seconds=1), observed_at=now - timedelta(seconds=61))
    view = build_car_charging_plan_view(native, report, now=now)
    assert view is not native, view
    assert view["rows"] == [], view
    assert view.get("execution_blocked"), view


def test_original_table_columns_styles_totals_preserved_for_valid_applied():
    """Preserve the established table schema, colours and replay-owned totals."""
    now = _wall_now()
    view = build_car_charging_plan_view(_native_plan(), _applied_report(now=now), now=now)
    row = view["rows"][0]
    for key in (
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
        "plan_ev_on_minutes",
        "soc_percent",
        "soc_change",
        "soc_sym",
        "soc_end_percent",
        "plan_soc_pv10_start_percent",
        "plan_soc_pv10_end_percent",
        "plan_grid_peak_w",
        "plan_grid_peak_pv10_w",
        "plan_interval_end",
        "rate_color_import",
        "rate_color_export",
        "cost_color",
        "state_color",
        "soc_color",
        "load_color",
        "pv_color",
        "car_color",
    ):
        assert key in row, key
    assert view["totals"] == {"pv_forecast": 0.2, "load_forecast": 0.5, "car_charging": 0.0, "soc_percent": 51, "total_cost": 1.37, "clipped": 0}, view["totals"]
    assert view["num_cars"] == 1, view
    assert view["iboost_enable"] is False, view
    assert view["carbon_enable"] is False, view


# ----------------------------------------------------------------------
# Web entry points: /api/plan_data and /plan HTML
# ----------------------------------------------------------------------


def test_html_api_plan_data_emits_car_planning_execution_for_fresh_applied():
    """The actual API route must expose the verified execution view."""
    page = _build_page(_applied_report(now=_wall_now()))
    data = json.loads(asyncio.run(page.html_api_plan_data(SimpleNamespace(rel_url=SimpleNamespace(query={})))).text)
    assert data["plan"]["plan_execution"] is True, data
    assert data["plan"]["plan_simulation"] is False, data
    assert len(data["plan"]["rows"]) == 1, data
    assert "execution_blocked" not in data["plan"], data


def test_html_plan_emits_active_plan_banner_for_fresh_applied():
    """The actual HTML route must render a fresh active-plan report."""
    page = _build_page(_applied_report(now=_wall_now()))
    html = asyncio.run(page.html_plan(SimpleNamespace(rel_url=SimpleNamespace(query={})))).text
    assert "Active plan" in html, html
    assert "Applied plan (executed)" not in html, html


def test_html_plan_emits_execution_blocked_banner_for_invalid_applied():
    """The actual HTML route must show expired execution as blocked."""
    now = _wall_now()
    report = _applied_report(now=now, valid_until=now - timedelta(seconds=1), observed_at=now - timedelta(seconds=61))
    page = _build_page(report)
    html = asyncio.run(page.html_plan(SimpleNamespace(rel_url=SimpleNamespace(query={})))).text
    assert "Execution blocked" in html, html
    assert "Active plan" not in html, html


# ----------------------------------------------------------------------
# JS renderer readonly gate
# ----------------------------------------------------------------------


def test_js_renderer_forces_editable_false_for_car_planning_execution():
    """Execution rows cannot edit unrelated native overrides through JavaScript."""
    from web_helper import get_plan_renderer_js

    js = get_plan_renderer_js()
    assert "plan_execution" in js, js
    assert "plan_execution !== true" in js, js
    # Original shadow gate remains intact.
    assert "plan_simulation !== true" in js, js


def test_js_renderer_does_not_advertise_simulation_only_for_applied():
    """Client-side rendering must respect the explicit execution flag."""
    from web_helper import get_plan_renderer_js

    js = get_plan_renderer_js()
    assert "plan_simulation === true || data.plan_execution === true" in js, js


# ----------------------------------------------------------------------
# JS renderer — whole-number SoC display in the original Plan table
# ----------------------------------------------------------------------


def test_js_renderer_rounds_fractional_soc_in_plan_cell():
    """The original Plan table must display integer SoC for every row.

    The combined solver emits fractional ``battery_start_percent`` values such
    as ``29.999377993406732`` for an active (executed) plan; the native
    solver already rounds via ``calc_percent_limit``. The Plan table must
    display whole-number percent in both cases — a previous gating of the
    ``Math.round`` on ``plan_simulation`` only left the executed combined
    plan (``plan_execution=true``, ``plan_simulation=false``) showing
    fractional strings. Rounding is applied at render time so raw planning
    SOC/math is unchanged upstream.
    """
    from web_helper import get_plan_renderer_js

    js = get_plan_renderer_js()
    # Row cell: every Plan row is rendered with Math.round applied
    # unconditionally, including executed combined plans.
    assert "${Math.round(row.soc_percent)}${socSym}" in js, js
    # Totals cell: same integer rounding so totals are consistent.
    assert "Math.round(totals.soc_percent" in js, js


# ----------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------


def run_car_planning_applied_view_tests(base):
    """Registered-style runner. Parent ``unit_test.py`` wires this under the
    suggested name ``car_charging_applied_view``. Each test runs in isolation
    against an in-memory base — no live HA, real inverter, or actuator."""
    tests = [
        test_valid_applied_view_sets_car_planning_simulation_false,
        test_valid_applied_view_preserves_execution_dates,
        test_shadow_candidate_keeps_car_planning_simulation_true,
        test_render_fresh_applied_uses_active_plan_label,
        test_render_fresh_applied_mainline_has_no_iso_dates,
        test_render_fresh_applied_suppresses_simulation_only_banner,
        test_render_fresh_applied_suppresses_visible_native_grid_warning,
        test_render_fresh_applied_no_button_or_input,
        test_render_applied_with_missing_preview_falls_back_to_execution_blocked,
        test_render_applied_with_stale_generated_at_falls_back_to_execution_blocked,
        test_render_applied_with_stale_soc_hypothesis_falls_back_to_execution_blocked,
        test_render_applied_with_counterfactual_controls_falls_back_to_execution_blocked,
        test_render_applied_with_failed_paired_checks_falls_back_to_execution_blocked,
        test_execution_blockers_non_list_rejects_fresh,
        test_execution_blockers_nonempty_list_rejects_fresh,
        test_execution_blockers_empty_list_accepts_fresh,
        test_execution_blockers_absent_accepts_fresh,
        test_block_reason_echoes_only_allowlisted_labels,
        test_block_reason_drops_unknown_reason_strings,
        test_block_reason_uses_now_check_when_no_reason,
        test_block_reason_inverter_observed_label_generic,
        test_apply_blocked_status_surfaces_execution_blocked,
        test_apply_blocked_status_with_unknown_reason_uses_coherence_reason,
        test_shadow_candidate_unavailable_keeps_generic_replay_path,
        test_fresh_applied_receipt_passes,
        test_expired_applied_receipt_rejects,
        test_missing_observed_at_rejects,
        test_missing_valid_until_rejects,
        test_future_observed_at_rejects,
        test_false_authority_rejects_execution_enabled_false,
        test_false_authority_rejects_actuator_signals_modified_false,
        test_wrong_requested_mode_rejects,
        test_interval_over_300_seconds_rejects,
        test_invalid_applied_clears_rows_and_no_native_fallback,
        test_original_table_columns_styles_totals_preserved_for_valid_applied,
        test_html_api_plan_data_emits_car_planning_execution_for_fresh_applied,
        test_html_plan_emits_active_plan_banner_for_fresh_applied,
        test_html_plan_emits_execution_blocked_banner_for_invalid_applied,
        test_js_renderer_forces_editable_false_for_car_planning_execution,
        test_js_renderer_does_not_advertise_simulation_only_for_applied,
        test_js_renderer_rounds_fractional_soc_in_plan_cell,
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
