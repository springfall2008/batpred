"""Exercise candidate staging and the real native execution/publication entry points."""

from tests.car_planning_fixtures import car_settings

from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from car_charging_plan import evaluate_car_charging
from tests.test_car_planning_native import _controls, _set_entity, _setup


def _apply_inputs(base):
    """Supply explicit synthetic observations without giving a real device authority."""
    _setup(base, pv=5)
    base.had_errors = base.fatal_error = base.inverter_needs_reset = False
    base.set_read_only = base.set_read_only_axle = False
    base.args["template"] = False
    base.octopus_intelligent_charging = False
    base.args["octopus_intelligent_slot"] = None
    base.plugin_system = None
    base.set_charge_freeze = True
    base.set_discharge_during_charge = True
    base.export_limit = 0
    base.car_charging_pending = None
    base.car_charging_execution = None
    car_settings(base).update(
        mode="apply",
        control_model="deye_tou",
        apply_verified=True,
        ev_executor_lease_verified=True,
        soc_entity="sensor.test_ev_soc",
        soc_entity_unit="kWh",
        relay_entity="binary_sensor.test_relay",
        connection_entity="sensor.test_charger_state",
        connection_entity_states=["connected"],
        permission_entity="switch.test_permission",
        connection_observed_at_entity="sensor.test_connection_observed_at",
        soc_observed_at_entity="sensor.test_soc_observed_at",
    )
    _set_entity(base, "sensor.test_ev_soc", "0")
    _set_entity(base, "binary_sensor.test_relay", "off", last_changed=base.now_utc - timedelta(seconds=600))
    _set_entity(base, "sensor.test_charger_state", "connected")
    _set_entity(base, "switch.test_permission", "on")
    _set_entity(base, "sensor.test_connection_observed_at", base.now_utc.isoformat())
    _set_entity(base, "sensor.test_soc_observed_at", base.now_utc.isoformat())


def test_apply_staging_is_not_execution(base):
    """Real search and replay stage a full timeline, not an actuator command."""
    _apply_inputs(base)
    before = _controls(base)
    with patch.object(base, "call_service_wrapper", side_effect=AssertionError("Staging called an actuator")):
        report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate", report
    assert report["counterfactual_controls"] is False
    assert report["apply_candidate_staged"] is True
    assert report["execution_enabled"] is False
    assert _controls(base) == before
    pending = base.car_charging_pending
    assert len(pending["commands"]) == base.forecast_minutes // 5
    assert pending["commands"][0]["start"] == base.minutes_now
    assert pending["commands"][-1]["end"] == base.minutes_now + base.forecast_minutes
    assert all(type(command["ev_on"]) is bool for command in pending["commands"])
    assert pending["report"] is not report
    report["proposed_ev_windows"].clear()
    assert pending["ev_windows"], "Publication must not mutate staged commands"


def test_failed_apply_calculation_revokes_staging(base):
    """A subsequent invalid calculation cannot leave yesterday's candidate executable."""
    _apply_inputs(base)
    assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
    assert base.car_charging_pending
    del base.load_minutes_step[5]
    assert evaluate_car_charging(base, publish=False)["status"] == "invalid_input"
    assert base.car_charging_pending is None


def test_apply_never_promotes_counterfactual_or_manual_soc(base):
    """Neither an assumed freeze nor prediction-fed manual EV SOC may authorise apply."""
    _apply_inputs(base)
    base.set_charge_freeze = False
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "unsupported", report
    assert "freeze_required" in report["execution_blockers"]
    assert not base.car_charging_pending
    _apply_inputs(base)
    base.car_charging_manual_soc[0] = True
    report = evaluate_car_charging(base, publish=False)
    assert "measured_ev_soc_required" in report["execution_blockers"], report
    assert not base.car_charging_pending
    _apply_inputs(base)
    car_settings(base)["allow_stale_shadow_soc"] = True
    assert evaluate_car_charging(base, publish=False)["status"] == "invalid_input"
    assert not base.car_charging_pending


def test_actual_execute_hook_rejects_legacy_inverter(base):
    """The real execute_plan path cannot fall through to legacy writes when apply is blocked."""
    _apply_inputs(base)
    base.inverters = [SimpleNamespace(inverter_type="SK", deye_local=None)]
    with patch.object(base, "call_service_wrapper", side_effect=AssertionError("Blocked apply called a physical service")):
        result = base.execute_plan()
    assert isinstance(result, tuple) and len(result) == 2
    assert not any(base.car_charging_slots)
    assert base.car_charging_report["execution_enabled"] is False
    assert base.car_charging_report["execution_reason"] == "local_deye_required"


def test_actual_publication_hook_suppresses_legacy_slots(base):
    """Fetch-time publication cannot energise from the old independent EV schedule."""
    _apply_inputs(base)
    base.car_charging_slots[0] = [{"start": base.minutes_now, "end": base.minutes_now + 60, "kwh": 6.77, "average": 10, "cost": 67.7}]
    with patch.object(base, "call_service_wrapper", side_effect=AssertionError("Publication called a physical service")):
        base.publish_car_plan()
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"


def test_actual_shadow_publication_preserves_native_slots(base):
    """Installing the new hooks must not take ownership away from the existing shadow installation."""
    _setup(base)
    base.car_charging_slots[0] = [{"start": base.minutes_now, "end": base.minutes_now + 60, "kwh": 6.77, "average": 10, "cost": 67.7}]
    before = deepcopy(base.car_charging_slots)
    base.publish_car_plan()
    assert base.car_charging_slots == before
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "on"


def test_apply_stable_soc_requires_actual_observation(base):
    """An unchanged SOC is usable only with its independently fresh source timestamp."""
    _apply_inputs(base)
    old = base.now_utc - timedelta(hours=3)
    _set_entity(base, "sensor.test_ev_soc", "0", last_updated=old, last_changed=old)
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate", report
    assert report["soc_observation_age_minutes"] == 0
    _set_entity(base, "sensor.test_soc_observed_at", old.isoformat())
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "unsupported", report
    assert "soc_entity_stale_or_future" in report["execution_blockers"]
    assert base.car_charging_pending is None


def test_ev_off_is_observed_before_battery_support_changes(base):
    """An EV OFF plan cannot remove domestic battery support while the relay remains ON."""
    from car_charging_execute import execute_car_charging_plan
    from tests.test_car_planning_apply import _evaluate, _make_adapter, _make_inverter

    _evaluate(base)
    adapter, _ = _make_adapter()
    _, spy = _make_inverter(base, adapter)
    _set_entity(base, "binary_sensor.test_relay", "on", last_changed=base.now_utc - timedelta(minutes=10))
    base.car_charging_pending["commands"][0].update(ev_on=False, freeze=True, target_soc=0)
    result = execute_car_charging_plan(base)
    assert result == ("EV blocked", "waiting_for_ev_relay_off"), result
    assert not spy["args"]
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"


def test_apply_requires_separate_ev_executor_lease_commissioning(base):
    """Hardware commissioning alone is insufficient for a legacy EV consumer without expiry."""
    from car_charging_execute import execute_car_charging_plan
    from tests.test_car_planning_apply import _evaluate, _make_adapter, _make_inverter

    _apply_inputs(base)
    _evaluate(base)
    adapter, _ = _make_adapter()
    _, spy = _make_inverter(base, adapter)
    car_settings(base).pop("ev_executor_lease_verified")
    assert execute_car_charging_plan(base) == ("EV blocked", "ev_executor_lease_required")
    assert not spy["args"]


def test_apply_blocks_competing_plan_consumers(base):
    """An external scheduler or native-plan follower cannot compete with combined apply."""
    from car_charging_execute import execute_car_charging_plan, native_plan_hook_allowed
    from tests.test_car_planning_apply import _evaluate, _make_adapter, _make_inverter

    for consumer in ("octopus", "gateway"):
        _apply_inputs(base)
        _evaluate(base)
        adapter, _ = _make_adapter()
        _, spy = _make_inverter(base, adapter)
        if consumer == "octopus":
            base.octopus_intelligent_charging = True
            reason = "external_ev_scheduler_unsupported"
        else:
            base.plugin_system = SimpleNamespace(hooks={"on_plan_executed": [lambda **kwargs: None]})
            reason = "external_plan_consumer_unsupported"
        assert execute_car_charging_plan(base) == ("EV blocked", reason)
        assert not spy["args"]
        assert native_plan_hook_allowed(base) is False
    for mode in ("off", "shadow"):
        car_settings(base)["mode"] = mode
        assert native_plan_hook_allowed(base) is True


def _expired_apply_inputs(base, relay="off"):
    """Keep real evidence fresh while closing, never renewing, the dated EV session."""
    _apply_inputs(base)
    session = car_settings(base)["session"]
    session["available_from"] = (base.now_utc - timedelta(hours=2)).isoformat()
    session["departure"] = (base.now_utc - timedelta(minutes=5)).isoformat()
    base.soc_kw = base.reserve + 0.05
    car_settings(base)["terminal_min_percent"] = 15
    base.all_active_keep[base.minutes_now + 5] = base.soc_kw / base.soc_max * 100 + 0.25
    for name in ("pv_forecast_minute_step", "pv_forecast_minute10_step"):
        setattr(base, name, {minute: 0.0 for minute in getattr(base, name)})
    _set_entity(base, "binary_sensor.test_relay", relay, last_changed=base.now_utc - timedelta(minutes=10))
    _set_entity(base, "sensor.test_ev_soc", "1")


def test_expired_apply_replenishes_home_without_renewing_ev(base):
    """The real solver, both replays and Execute continue domestic charging with EV OFF."""
    from tests.test_car_planning_apply import _make_adapter, _make_inverter

    _expired_apply_inputs(base)
    session = deepcopy(car_settings(base)["session"])
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate", report
    assert report["session_closed"] is True
    assert report["target_met_by_departure"] is None
    assert report["ev_session_renewed"] is False
    assert report["ev_initial_state"]["ev_soc_kwh"] == 1
    assert all(check["passed"] and check["checked_minutes"] == base.forecast_minutes for check in report["native_checks"].values())
    assert not report["proposed_ev_windows"]
    assert all(not step["ev_on"] for step in base.car_charging_pending["commands"])
    assert base.car_charging_pending["commands"][0]["target_soc"] == 100
    adapter, _ = _make_adapter()
    _, spy = _make_inverter(base, adapter)
    assert base.execute_plan()[0] == "Charging", base.car_charging_report
    assert spy["args"]
    assert datetime.fromisoformat(base.car_charging_execution["expires_at"]) > base.now_utc
    base.publish_car_plan()
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"
    assert car_settings(base)["session"] == session


def test_expired_apply_waits_for_observed_ev_off(base):
    """Expiry first withdraws the EV lease, without speculatively removing battery support."""
    from tests.test_car_planning_apply import _make_adapter, _make_inverter

    _expired_apply_inputs(base, relay="on")
    assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
    adapter, _ = _make_adapter()
    _, spy = _make_inverter(base, adapter)
    assert base.execute_plan() == ("EV blocked", "waiting_for_ev_relay_off")
    assert not spy["args"]
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"


def test_expired_apply_rejects_any_future_ev_command(base):
    """Even a later injected EV window cannot escape the closed-session contract."""
    from tests.test_car_planning_apply import _make_adapter, _make_inverter

    _expired_apply_inputs(base)
    assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
    base.car_charging_pending["commands"][-1]["ev_on"] = True
    adapter, _ = _make_adapter()
    _, spy = _make_inverter(base, adapter)
    assert base.execute_plan() == ("EV blocked", "closed_session_ev_command")
    assert not spy["args"]


def test_expired_apply_retains_all_evidence_and_ownership_gates(base):
    """Domestic continuation is not an exemption from existing commissioning or safety guards."""
    from tests.test_car_planning_apply import _make_adapter, _make_inverter

    for guard in ("stale", "permission", "connection", "read_only", "manual_floor", "commissioning"):
        _expired_apply_inputs(base)
        assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
        adapter, _ = _make_adapter()
        _, spy = _make_inverter(base, adapter)
        if guard == "stale":
            _set_entity(base, "sensor.test_soc_observed_at", (base.now_utc - timedelta(seconds=181)).isoformat())
        elif guard == "permission":
            _set_entity(base, "switch.test_permission", "off")
        elif guard == "connection":
            _set_entity(base, "sensor.test_charger_state", "disconnected")
        elif guard == "read_only":
            base.set_read_only = True
        elif guard == "manual_floor":
            base.all_active_keep[base.minutes_now] = 50
        else:
            car_settings(base)["apply_verified"] = False
        assert base.execute_plan()[0] == "EV blocked", (guard, base.car_charging_report)
        assert not spy["args"], guard


def test_crossing_departure_requires_new_domestic_candidate(base):
    """A pre-departure candidate cannot turn into a house-only plan without new native replays."""
    from tests.test_car_planning_apply import _make_adapter, _make_inverter

    _apply_inputs(base)
    base.car_charging_limit[0] = 0
    car_settings(base)["session"]["departure"] = (base.now_utc + timedelta(seconds=30)).isoformat()
    assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
    adapter, _ = _make_adapter()
    _, spy = _make_inverter(base, adapter)
    with patch("car_charging_plan._observation_clock", return_value=base.now_utc + timedelta(seconds=31)):
        assert base.execute_plan() == ("EV blocked", "session_phase_changed")
    assert not spy["args"]


def test_crossing_departure_during_dispatch_revokes_authority(base):
    """An observed write that settles across expiry is not a newly accepted house-only plan."""
    from tests.test_car_planning_apply import _make_adapter, _make_inverter

    _apply_inputs(base)
    base.car_charging_limit[0] = 0
    car_settings(base)["session"]["departure"] = (base.now_utc + timedelta(seconds=30)).isoformat()
    assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
    adapter, _ = _make_adapter()
    inverter, spy = _make_inverter(base, adapter)
    dispatch = inverter.adjust_charge_immediate
    clock = {"now": base.now_utc}

    def settle_after_expiry(*args, **kwargs):
        """Advance only the synthetic observation clock while retaining the real spy."""
        result = dispatch(*args, **kwargs)
        clock["now"] += timedelta(seconds=31)
        return result

    with patch("car_charging_plan._observation_clock", side_effect=lambda: clock["now"]), patch.object(inverter, "adjust_charge_immediate", side_effect=settle_after_expiry):
        assert base.execute_plan() == ("EV blocked", "session_phase_changed")
    assert len(spy["args"]) == 1
    assert base.car_charging_execution is None
    assert base.car_charging_report["hardware_confirmed"] is False
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"


def test_expiry_boundary_and_next_day_never_renew_ev(base):
    """The exact departure and a fresh next-day observation both retain a closed EV session."""
    for age in (0, 25 * 60):
        _expired_apply_inputs(base)
        session = car_settings(base)["session"]
        departure = base.now_utc - timedelta(minutes=age)
        session.update(available_from=(departure - timedelta(hours=2)).isoformat(), departure=departure.isoformat())
        before = deepcopy(session)
        report = evaluate_car_charging(base, publish=False)
        assert report["status"] == "candidate", report
        assert report["session_closed"] is True
        assert report["target_met_by_departure"] is None
        assert not report["proposed_ev_windows"]
        assert not any(step["ev_on"] for step in base.car_charging_pending["commands"])
        assert session == before


def run_car_planning_entrypoint_tests(base):
    """Run real entry-point regressions in the isolated unit-test environment."""
    for test in (
        test_apply_staging_is_not_execution,
        test_failed_apply_calculation_revokes_staging,
        test_apply_never_promotes_counterfactual_or_manual_soc,
        test_actual_execute_hook_rejects_legacy_inverter,
        test_actual_publication_hook_suppresses_legacy_slots,
        test_actual_shadow_publication_preserves_native_slots,
        test_apply_stable_soc_requires_actual_observation,
        test_ev_off_is_observed_before_battery_support_changes,
        test_apply_requires_separate_ev_executor_lease_commissioning,
        test_apply_blocks_competing_plan_consumers,
        test_expired_apply_replenishes_home_without_renewing_ev,
        test_expired_apply_waits_for_observed_ev_off,
        test_expired_apply_rejects_any_future_ev_command,
        test_expired_apply_retains_all_evidence_and_ownership_gates,
        test_crossing_departure_requires_new_domestic_candidate,
        test_crossing_departure_during_dispatch_revokes_authority,
        test_expiry_boundary_and_next_day_never_renew_ev,
    ):
        with patch("car_charging_plan._observation_clock", side_effect=lambda: base.now_utc):
            test(base)
        print("PASS:", test.__name__)
    return False
