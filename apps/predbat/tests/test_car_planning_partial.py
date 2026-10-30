"""Truthful partial shadow schedules retain the requested target and all control gates."""

from tests.car_planning_fixtures import car_settings

from copy import deepcopy
from datetime import timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from car_charging_plan import _best_effort_shadow, _options, _replay_candidate, evaluate_car_charging
from car_charging_view import build_car_charging_plan_view, render_car_charging_preview
from tests.test_car_planning_native import _setup, _controls, _set_entity


def _partial_fixture(base):
    """Demand/charge-only inputs with insufficient time for the requested full EV target."""
    _setup(base, minutes=180)
    base.soc_kw = base.soc_max * 0.20
    base.car_charging_limit = [25.0] * len(base.car_charging_limit)
    car_settings(base)["control_model"] = "deye_solarman"
    base.set_discharge_during_charge = True
    base.set_read_only = True


def test_real_partial_plan_preserves_target_and_power(base):
    """A fresh alternative includes battery grid charge and EV, not an empty or old table."""
    _partial_fixture(base)
    before = _controls(base)
    target = deepcopy(base.car_charging_limit)
    settings = deepcopy(car_settings(base))
    with patch("car_charging_plan._observation_clock", return_value=base.now_utc), patch("car_charging_plan._collect_blockers", return_value=[]):
        report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "partial_candidate", report
    assert report["requested_ev_target_kwh"] == 25 and report["requested_ev_target_percent"] == 100
    assert report["requested_ev_target_met"] is False and 0 < report["planned_ev_percent_by_departure"] < 100
    assert report["proposed_charge_windows"] and report["proposed_ev_windows"], report["partial_search_attempts"]
    assert 1 <= len(report["partial_search_attempts"]) <= 8
    for check in report["native_checks"].values():
        assert check["passed"] and check["checked_minutes"] == 180 and check["peak_import_w"] <= 6000.501
        assert check["final_ev_kwh"] >= report["planning_ev_target_kwh"] - 0.002
    view = build_car_charging_plan_view({}, report, now=base.now_utc)
    assert view["rows"] and not view.get("plan_unavailable")
    text = render_car_charging_preview(report, None, now=base.now_utc)
    assert "Partial charging plan" in text and "100.0% remains unchanged" in text and "NOT validated" in text
    assert not report["execution_enabled"] and not report["enforced"] and not report["actuator_signals_modified"]
    assert getattr(base, "car_charging_pending", None) is None and _controls(base) == before
    assert base.car_charging_limit == target and car_settings(base) == settings and base.set_read_only
    return report


def test_partial_preview_requires_truthful_shortfall(base):
    """Missing, contradictory, executable or expired partial evidence must hide the rows."""
    report = test_real_partial_plan_preserves_target_and_power(base)
    for key, bad in (("requested_ev_target_met", True), ("ev_target_shortfall_kwh", 0), ("planned_ev_kwh_by_departure", 25), ("requested_mode", "apply"), ("execution_enabled", True), ("planned_ev_percent_by_departure", float("nan"))):
        changed = deepcopy(report)
        changed[key] = bad
        assert build_car_charging_plan_view({}, changed, now=base.now_utc)["plan_unavailable"] is True
    changed = deepcopy(report)
    changed.pop("planning_ev_target_kwh")
    assert build_car_charging_plan_view({}, changed, now=base.now_utc)["plan_unavailable"] is True
    changed = deepcopy(report)
    changed["ev_departure_at"] = (base.now_utc - timedelta(minutes=1)).isoformat()
    assert build_car_charging_plan_view({}, changed, now=base.now_utc)["plan_unavailable"] is True
    changed = deepcopy(report)
    changed["native_checks"]["pv10"]["passed"] = False
    assert build_car_charging_plan_view({}, changed, now=base.now_utc)["plan_unavailable"] is True


def test_partial_search_cannot_touch_apply_or_closed_sessions(base):
    """Partial alternatives never change apply's target contract or renew a departure."""
    _partial_fixture(base)
    with patch("car_charging_plan._observation_clock", return_value=base.now_utc):
        options = _options(base)
    for change in ({"mode": "apply"}, {"house_only": True}, {"deadline": 0}, {"control_model": "deye_tou"}):
        with patch("car_charging_plan.build_candidate", side_effect=AssertionError("Forbidden partial search")):
            assert _best_effort_shadow(base, {**options, **change}) == (None, [])


def test_partial_misses_are_bounded_and_clear_no_constraints(base):
    """An eight-attempt miss does not manufacture a candidate or alter its inputs."""
    _partial_fixture(base)
    with patch("car_charging_plan._observation_clock", return_value=base.now_utc):
        options = _options(base)
    before = _controls(base)
    empty = SimpleNamespace(actions=(), status="bounded_no_plan")
    with patch("car_charging_plan.build_candidate", return_value=(empty, ())) as search:
        partial, attempts = _best_effort_shadow(base, options)
    assert partial is None and 1 <= len(attempts) == search.call_count <= 8
    assert all(abs(attempt["planning_ev_target_kwh"] * 4 - round(attempt["planning_ev_target_kwh"] * 4)) < 1e-9 for attempt in attempts)
    assert all(not attempt["native_passed"] for attempt in attempts)
    assert _controls(base) == before and base.car_charging_limit[0] == 25


def test_full_candidate_does_not_run_partial_search(base):
    """Always keep a validated complete target before considering any partial alternative."""
    _setup(base)
    with patch("car_charging_plan._observation_clock", return_value=base.now_utc), patch("car_charging_plan._best_effort_shadow", side_effect=AssertionError("Full candidate was replaced")):
        report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate", report


def test_partial_soc_assumption_remains_conditional(base):
    """Partial delivery cannot turn a stale observed SOC into execution authority."""
    _partial_fixture(base)
    old_stamp = base.now_utc - timedelta(hours=2)
    _set_entity(base, "sensor.test_ev_soc", "0", last_updated=old_stamp, last_changed=old_stamp)
    car_settings(base).update(soc_entity="sensor.test_ev_soc", soc_entity_unit="kWh", allow_stale_shadow_soc=True)
    before = _controls(base)
    with patch("car_charging_plan._observation_clock", return_value=base.now_utc), patch("car_charging_plan._collect_blockers", return_value=["soc_entity_stale_or_future"]):
        report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "conditional_partial_candidate", report
    assert report["uses_stale_soc_assumption"] and report["soc_observed_at"] == old_stamp.astimezone(timezone.utc).isoformat()
    assert report["execution_blockers"] == report["input_blockers"] == ["soc_entity_stale_or_future"]
    assert "fresh_ev_soc_observation" in report["activation_requirements"]
    assert report["ev_initial_state"]["ev_soc_source"] == "stale_entity_assumption"
    assert not report["ready_to_activate"] and not report["execution_enabled"] and base.car_charging_limit[0] == 25
    assert _controls(base) == before and build_car_charging_plan_view({}, report, now=base.now_utc)["rows"]
    text = render_car_charging_preview(report, None, now=base.now_utc)
    assert "stale SoC assumption" in text and "Partial charging plan" in text


def test_partial_search_rejects_failed_paired_replay(base):
    """Even a passing nominal replay cannot rescue a rejected PV10 alternative."""
    _partial_fixture(base)
    with patch("car_charging_plan._observation_clock", return_value=base.now_utc):
        options = _options(base)
    before = _controls(base)

    def reject_pv10(*args):
        """Keep the actual fresh replays but demonstrate a PV10 rejection."""
        windows, checks = _replay_candidate(*args)
        checks["pv10"].update(passed=False, reasons=["site_import_exceeded"])
        return windows, checks

    with patch("car_charging_plan._replay_candidate", side_effect=reject_pv10):
        partial, attempts = _best_effort_shadow(base, options)
    assert partial is None and attempts and not any(attempt["native_passed"] for attempt in attempts)
    assert _controls(base) == before and base.car_charging_limit[0] == 25


def test_compact_partial_status_keeps_truth_and_debug_details(base):
    """One short normal-view line keeps simulation, target and blockers without verbose warnings."""
    report = test_real_partial_plan_preserves_target_and_power(base)
    report["uses_stale_soc_assumption"] = True
    diagnostic = {"state": "exceeded", "attributes": {"limit_w": 6000, "peak_w": 7840, "minutes_over": 195}}
    text = render_car_charging_preview(report, diagnostic, now=base.now_utc)
    marker = '<div class="car-charging-preview-debug" style="display:none;">'
    assert marker in text and text.index('class="car-charging-preview-summary"') < text.index(marker)
    visible, details = text.split(marker, 1)
    assert "Simulation only — not executed" in visible and "% / 100.0% requested" in visible
    assert "EV SOC unconfirmed" in visible and "native grid limit exceeded" in visible and "Show Debug" in visible
    assert len(visible) < 320
    for long_text in ("EV candidate — read-only simulation", "Native import limit exceeded", "EV SOC is old", "Partial charging plan"):
        assert long_text not in visible and long_text in details
    assert "100.0% remains unchanged" in details and "NOT validated" in details
    assert "<table" not in text and "<details" not in text
    # A failed or stale report must not keep an attractive compact candidate status.
    rejected = deepcopy(report)
    rejected["native_checks"]["pv10"]["passed"] = False
    failed_text = render_car_charging_preview(rejected, diagnostic, now=base.now_utc)
    assert 'class="car-charging-preview-summary"' not in failed_text and "No complete paired" in failed_text


def run_car_planning_partial_tests(base):
    """Run actual replay, target honesty and shadow-only partial search regressions."""
    for test in (
        test_real_partial_plan_preserves_target_and_power,
        test_partial_preview_requires_truthful_shortfall,
        test_partial_search_cannot_touch_apply_or_closed_sessions,
        test_partial_misses_are_bounded_and_clear_no_constraints,
        test_full_candidate_does_not_run_partial_search,
        test_partial_soc_assumption_remains_conditional,
        test_partial_search_rejects_failed_paired_replay,
        test_compact_partial_status_keeps_truth_and_debug_details,
    ):
        test(base)
    return False
