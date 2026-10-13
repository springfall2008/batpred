"""Genuine paired-native partial plans, exact staged evidence and mock dispatch receipts."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from joint_site_apply import execute_joint_site, publish_joint_ev
from joint_site_native import evaluate_joint_site
from tests.test_joint_site_actual import _actual_inputs, _set_entity


def partial_inputs(base):
    """Make 25 kWh impossible by departure without weakening any actual template guard."""
    inverter, writes, sources, targets = _actual_inputs(base)
    base.args["joint_site"].update(ev_partial_target_authorised=True, ev_charging_priority="asap")
    base.car_charging_limit[0] = 25.0
    base.soc_kw = base.soc_max * 0.8
    inverter.soc_kw, inverter.soc_percent = base.soc_kw, 80
    base.all_active_keep = {}
    return inverter, writes, sources, targets


def test_partial_replay_dispatch_and_expiry(base):
    """Execute a real partial candidate via mock HA rows and expire its unchanged lease."""
    _, writes, _, _ = partial_inputs(base)
    report = evaluate_joint_site(base, publish=False)
    assert report["status"] == "partial_candidate", report
    assert report["requested_ev_target_kwh"] == base.car_charging_limit[0] == 25
    assert report["requested_ev_target_met"] is False and report["ev_target_shortfall_kwh"] > 0
    assert all(check["passed"] and check["validated_ev_target_kwh"] == report["planning_ev_target_kwh"] for check in report["native_checks"].values())
    assert base.joint_site_pending and not writes
    assert base.joint_site_pending["commands"][0]["ev_on"] is True
    result = execute_joint_site(base)
    assert result[0] != "Joint blocked", base.joint_site_report
    assert base.joint_site_execution and base.joint_site_report["status"] == "applied"
    from joint_site_preview import build_joint_plan_view, render_joint_site_preview

    assert build_joint_plan_view({}, base.joint_site_report, now=base.now_utc)["joint_execution"] is True
    text = render_joint_site_preview(base.joint_site_report, None, now=base.now_utc)
    assert " / 100.0% requested" in text and "Simulation only" not in text
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "on"
    assert all(service in ("number/set_value", "select/select_option") for service, _ in writes)
    count = len(writes)
    with patch("joint_site_native._observation_clock", return_value=base.now_utc + timedelta(seconds=301)):
        publish_joint_ev(base)
    assert base.joint_site_execution is None and len(writes) == count and base.car_charging_limit[0] == 25


def test_partial_evidence_tampering_rejected(base):
    """Mutating any target or paired evidence must never reach inverter writes."""
    for field, value in (("requested_ev_target_kwh", 3), ("planning_ev_target_kwh", 0.5), ("planned_ev_kwh_by_departure", 25), ("ev_target_shortfall_kwh", 0), ("partial_target_authorised", False), ("planning_ev_target_kwh", float("nan"))):
        _, writes, _, _ = partial_inputs(base)
        report = evaluate_joint_site(base, publish=False)
        assert report["status"] == "partial_candidate", report
        base.joint_site_pending["report"][field] = value
        assert execute_joint_site(base)[0] == "Joint blocked" and not writes, field
    for case in ("disabled", "ev_soc_changed", "replay_target_changed", "manual_floor", "read_only"):
        _, writes, _, _ = partial_inputs(base)
        assert evaluate_joint_site(base, publish=False)["status"] == "partial_candidate"
        if case == "disabled":
            base.args["joint_site"]["ev_partial_target_authorised"] = False
        elif case == "ev_soc_changed":
            _set_entity(base, "sensor.test_ev_energy", "100.1", attributes={"unit_of_measurement": "kWh"})
        elif case == "replay_target_changed":
            base.joint_site_pending["report"]["native_checks"]["pv10"]["validated_ev_target_kwh"] = 0
        elif case == "manual_floor":
            base.all_active_keep[base.minutes_now] = 90
        else:
            base.set_read_only = True
        assert execute_joint_site(base)[0] == "Joint blocked" and not writes, (case, base.joint_site_report)


def test_partial_domestic_soc_drop_replays_lower_target(base):
    """Dispatch replays the unchanged partial controls, not an impossible full-target surrogate."""
    _, _, _, _ = partial_inputs(base)
    report = evaluate_joint_site(base, publish=False)
    assert report["status"] == "partial_candidate"
    commands = deepcopy(base.joint_site_pending["commands"])
    base.soc_kw -= 0.01
    assert execute_joint_site(base)[0] != "Joint blocked", base.joint_site_report
    assert base.joint_site_report["dispatch_soc_revalidated_kwh"] == base.soc_kw
    assert base.joint_site_pending["commands"] == commands and base.car_charging_limit[0] == 25
    assert all(check["passed"] and check["validated_ev_target_kwh"] == report["planning_ev_target_kwh"] for check in base.joint_site_report["native_checks"].values())


def run_joint_site_partial_apply_tests(base):
    """Run actual partial-search and mock-dispatch regressions in fresh native fixtures."""
    from unit_test import create_predbat
    from tests.test_joint_site_template import shared_template_available, check_unavailable_template

    if not shared_template_available():
        check_unavailable_template(create_predbat())
        return False

    for test in (test_partial_replay_dispatch_and_expiry, test_partial_evidence_tampering_rejected, test_partial_domestic_soc_drop_replays_lower_target):
        fresh = create_predbat()
        with patch("joint_site_native._observation_clock", side_effect=lambda: fresh.now_utc):
            test(fresh)
    return False
