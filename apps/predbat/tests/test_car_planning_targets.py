"""Actual replay-derived charge ceilings and truthful unavailable-candidate notices."""

from tests.car_planning_fixtures import car_settings

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from car_charging_solver import Slot
from car_charging_plan import _native_windows, _replay, _replay_candidate, _options, build_candidate, evaluate_car_charging
from car_charging_view import build_car_charging_plan_view, _unavailable_reason
from tests.test_car_planning_native import _setup, _controls
from tests.test_car_planning_single_table import _native_plan, _good_report


def test_charge_ceiling_is_replayed_not_cosmetic(base):
    """A short charge uses its attainable paired peak plus margin, not a 100% label."""
    _setup(base, minutes=60)
    base.car_charging_limit = [0] * len(base.car_charging_limit)
    result = SimpleNamespace(status="feasible", actions=tuple(SimpleNamespace(slot_index=index, battery_mode="charge", ev_action="off") for index in range(12)))
    slots = [Slot(index, index * 5, 5, 1 / 12, 0, 10, 0) for index in range(12)]
    options = dict(mode="shadow", ev_soc=0, import_limit_w=6000, terminal_min=base.reserve, initially_on=False, initial_off_minutes=10, pause=15, restart=25, arrival=0, deadline=60, off_minutes=10, load_cap=10)
    before = _controls(base)
    original = _native_windows(base, result, slots)
    baseline = {name: _replay(base, options, result, slots, original, scenario) for name, scenario in (("nominal", 0), ("pv10", 1))}
    windows, checks = _replay_candidate(base, options, result, slots)
    target = windows[2][0] * 100 / base.soc_max
    peak_percent = max(max(check["_charge_peak_kwh"]) for check in baseline.values()) * 100 / base.soc_max
    assert peak_percent + 1 <= target < peak_percent + 2.0001 and target < 100
    assert original[2] == [base.soc_max]
    for scenario, check in checks.items():
        assert check["passed"] and "_charge_peak_kwh" not in check
        assert check["final_battery_kwh"] == baseline[scenario]["final_battery_kwh"]
        assert check["horizon_energy_cost"] == baseline[scenario]["horizon_energy_cost"]
        assert all(abs(row["charge_target_percent"] - target) < 1e-9 for row in check["_preview_rows"])
    assert _controls(base) == before


def test_reduced_target_failure_cannot_reuse_full_target(base):
    """A second replay rejection stays rejected; the first passed result cannot leak."""
    actions = [SimpleNamespace(slot_index=0, battery_mode="charge", ev_action="off")]
    result = SimpleNamespace(actions=actions)
    slots = [Slot(0, 0, 5, 0, 0, 10, 0)]
    reference = {"passed": True, "reasons": [], "_charge_peak_kwh": [base.soc_max * 0.5]}
    rejected = {"passed": False, "reasons": ["site_import_exceeded"], "_charge_peak_kwh": [base.soc_max * 0.5]}
    with patch("car_charging_plan._replay", side_effect=[deepcopy(reference), deepcopy(reference), deepcopy(reference), deepcopy(rejected)]):
        windows, checks = _replay_candidate(base, {"mode": "shadow"}, result, slots)
    assert round(windows[2][0] * 100 / base.soc_max) == 51
    assert checks["pv10"]["passed"] is False
    assert all("_charge_peak_kwh" not in check for check in checks.values())


def test_table_limit_uses_real_candidate_target():
    """Keep the existing column but show the same limit used by native validation."""
    report = _good_report()
    for rows in report["preview"].values():
        rows[0].update(battery_modes="charge", charge_target_percent=53)
    view = build_car_charging_plan_view(_native_plan(), report)
    assert view["rows"][0]["show_limit"] == "53"
    assert view["rows"][0]["soc_percent"] == report["preview"]["nominal"][0]["battery_start_percent"]
    report["preview"]["pv10"][0]["charge_target_percent"] = 54
    assert build_car_charging_plan_view(_native_plan(), report)["plan_unavailable"] is True
    report["preview"]["pv10"][0]["charge_target_percent"] = float("nan")
    assert build_car_charging_plan_view(_native_plan(), report)["plan_unavailable"] is True


def test_rejection_notice_explains_actual_constraints():
    """Show specific failed constraints, never an unverified schedule or private text."""
    report = {"status": "conditional_native_rejected", "native_checks": {"pv10": {"reasons": ["site_import_exceeded", "ev_restart_threshold", "<private>"]}}}
    text = _unavailable_reason(report)
    assert "conservative forecast" in text and "grid import limit" in text and "restart threshold" in text
    assert "previous simulation is hidden" in text and "<private>" not in text


def test_conservative_search_miss_gets_bounded_retry(base):
    """A conservative beam miss gets the same finite retry as the nominal search."""
    _setup(base, minutes=60)
    car_settings(base)["control_model"] = "deye_solarman"
    with patch("car_charging_plan._observation_clock", return_value=base.now_utc):
        options = _options(base)
        result, slots = build_candidate(base, options)
        assert result.actions
        empty = SimpleNamespace(actions=(), status="bounded_no_plan")
        windows = _native_windows(base, result, slots)
        rejected = {"nominal": {"passed": True}, "pv10": {"passed": False, "reasons": ["site_import_exceeded"]}}
        accepted = {"nominal": {"passed": True}, "pv10": {"passed": True}}
        with patch("car_charging_plan._collect_blockers", return_value=[]), patch("car_charging_plan.build_candidate", side_effect=[(result, slots), (empty, slots), (result, slots)]) as search:
            with patch("car_charging_plan._replay_candidate", side_effect=[(windows, rejected), (windows, accepted)]):
                report = evaluate_car_charging(base, publish=False)
        assert [call.args[1]["beam"] for call in search.call_args_list] == [128, 128, 512]
        assert report["status"] == "candidate" and len(report["search_attempts"]) == 3
        assert report["search_attempts"][-1]["forecast_basis"] == "envelope"
        assert not report["execution_enabled"] and not report["enforced"]


def test_apply_ceiling_contract_is_not_changed(base):
    """The presentation request cannot silently change the commissioned apply contract."""
    result = SimpleNamespace(actions=[SimpleNamespace(slot_index=0, battery_mode="charge", ev_action="off")])
    slots = [Slot(0, 0, 5, 0, 0, 10, 0)]
    reference = {"passed": True, "reasons": [], "_charge_peak_kwh": [base.soc_max * 0.5]}
    with patch("car_charging_plan._replay", side_effect=[deepcopy(reference), deepcopy(reference)]) as replay:
        windows, checks = _replay_candidate(base, {"mode": "apply"}, result, slots)
    assert windows[2] == [base.soc_max] and replay.call_count == 2
    assert all("_charge_peak_kwh" not in check for check in checks.values())


def test_rejection_notice_handles_malformed_checks():
    """Malformed diagnostic data cannot crash the page or leak arbitrary values."""
    for checks in (None, [], {"pv10": None}, {"pv10": {"reasons": None}}, {"pv10": {"reasons": "private"}}):
        text = _unavailable_reason({"status": "native_rejected", "native_checks": checks})
        assert "No complete paired" in text and "private" not in text


def test_economic_rejection_recovers_only_revalidated_established_order(base):
    """An economic-order miss may recover a new valid path, never reuse a failed check."""
    _setup(base, minutes=60)
    car_settings(base)["control_model"] = "deye_solarman"
    with patch("car_charging_plan._observation_clock", return_value=base.now_utc):
        options = _options(base)
        result, slots = build_candidate(base, options)
        windows = _native_windows(base, result, slots)
        rejected = {"nominal": {"passed": True}, "pv10": {"passed": False, "reasons": ["ev_pause_threshold"]}}
        accepted = {"nominal": {"passed": True}, "pv10": {"passed": True}}
        for last, expected in ((accepted, "candidate"), (rejected, "native_rejected")):
            with patch("car_charging_plan._best_effort_shadow", return_value=(None, [])), patch("car_charging_plan._collect_blockers", return_value=[]), patch("car_charging_plan.build_candidate", return_value=(result, slots)) as search:
                with patch("car_charging_plan._replay_candidate", side_effect=[(windows, rejected), (windows, rejected), (windows, last)]):
                    report = evaluate_car_charging(base, publish=False)
            assert search.call_count == 3 and search.call_args_list[-1].args[1]["economic_diversity"] is False
            assert report["status"] == expected and report["search_attempts"][-1]["pruning_order"] == "established"
            assert not report["execution_enabled"] and not report["enforced"]
            if expected == "native_rejected":
                assert build_car_charging_plan_view(_native_plan(), report)["plan_unavailable"] is True


def run_car_planning_targets_tests(base):
    """Run replay-target and rejection-message regressions through the native harness."""
    for test in (
        test_charge_ceiling_is_replayed_not_cosmetic,
        test_reduced_target_failure_cannot_reuse_full_target,
        test_conservative_search_miss_gets_bounded_retry,
        test_apply_ceiling_contract_is_not_changed,
        test_economic_rejection_recovers_only_revalidated_established_order,
    ):
        test(base)
    test_table_limit_uses_real_candidate_target()
    test_rejection_notice_explains_actual_constraints()
    test_rejection_notice_handles_malformed_checks()
    return False
