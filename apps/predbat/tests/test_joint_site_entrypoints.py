"""Exercise joint staging and the real native execution/publication entry points."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from joint_site_native import evaluate_joint_site
from tests.test_joint_site_native import _controls, _set_entity, _setup


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
    base.joint_site_pending = None
    base.joint_site_execution = None
    base.args["joint_site"].update(
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
        report = evaluate_joint_site(base, publish=False)
    assert report["status"] == "candidate", report
    assert report["counterfactual_controls"] is False
    assert report["apply_candidate_staged"] is True
    assert report["execution_enabled"] is False
    assert _controls(base) == before
    pending = base.joint_site_pending
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
    assert evaluate_joint_site(base, publish=False)["status"] == "candidate"
    assert base.joint_site_pending
    del base.load_minutes_step[5]
    assert evaluate_joint_site(base, publish=False)["status"] == "invalid_input"
    assert base.joint_site_pending is None


def test_apply_never_promotes_counterfactual_or_manual_soc(base):
    """Neither an assumed freeze nor prediction-fed manual EV SOC may authorise apply."""
    _apply_inputs(base)
    base.set_charge_freeze = False
    report = evaluate_joint_site(base, publish=False)
    assert report["status"] == "unsupported", report
    assert "freeze_required" in report["execution_blockers"]
    assert not base.joint_site_pending
    _apply_inputs(base)
    base.car_charging_manual_soc[0] = True
    report = evaluate_joint_site(base, publish=False)
    assert "measured_ev_soc_required" in report["execution_blockers"], report
    assert not base.joint_site_pending
    _apply_inputs(base)
    base.args["joint_site"]["allow_stale_shadow_soc"] = True
    assert evaluate_joint_site(base, publish=False)["status"] == "invalid_input"
    assert not base.joint_site_pending


def test_actual_execute_hook_rejects_legacy_inverter(base):
    """The real execute_plan path cannot fall through to legacy writes when apply is blocked."""
    _apply_inputs(base)
    base.inverters = [SimpleNamespace(inverter_type="SK", deye_local=None)]
    with patch.object(base, "call_service_wrapper", side_effect=AssertionError("Blocked apply called a physical service")):
        result = base.execute_plan()
    assert isinstance(result, tuple) and len(result) == 2
    assert not any(base.car_charging_slots)
    assert base.joint_site_report["execution_enabled"] is False
    assert base.joint_site_report["execution_reason"] == "local_deye_required"


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
    report = evaluate_joint_site(base, publish=False)
    assert report["status"] == "candidate", report
    assert report["soc_observation_age_minutes"] == 0
    _set_entity(base, "sensor.test_soc_observed_at", old.isoformat())
    report = evaluate_joint_site(base, publish=False)
    assert report["status"] == "unsupported", report
    assert "soc_entity_stale_or_future" in report["execution_blockers"]
    assert base.joint_site_pending is None


def test_ev_off_is_observed_before_battery_support_changes(base):
    """An EV OFF plan cannot remove domestic battery support while the relay remains ON."""
    from joint_site_apply import execute_joint_site
    from tests.test_joint_site_apply import _evaluate, _make_adapter, _make_inverter

    _evaluate(base)
    adapter, _ = _make_adapter()
    _, spy = _make_inverter(base, adapter)
    _set_entity(base, "binary_sensor.test_relay", "on", last_changed=base.now_utc - timedelta(minutes=10))
    base.joint_site_pending["commands"][0].update(ev_on=False, freeze=True, target_soc=0)
    result = execute_joint_site(base)
    assert result == ("Joint blocked", "waiting_for_ev_relay_off"), result
    assert not spy["args"]
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"


def test_apply_requires_separate_ev_executor_lease_commissioning(base):
    """Hardware commissioning alone is insufficient for a legacy EV consumer without expiry."""
    from joint_site_apply import execute_joint_site
    from tests.test_joint_site_apply import _evaluate, _make_adapter, _make_inverter

    _apply_inputs(base)
    _evaluate(base)
    adapter, _ = _make_adapter()
    _, spy = _make_inverter(base, adapter)
    base.args["joint_site"].pop("ev_executor_lease_verified")
    assert execute_joint_site(base) == ("Joint blocked", "ev_executor_lease_required")
    assert not spy["args"]


def test_apply_blocks_competing_plan_consumers(base):
    """An external scheduler or native-plan follower cannot compete with joint apply."""
    from joint_site_apply import execute_joint_site, native_plan_hook_allowed
    from tests.test_joint_site_apply import _evaluate, _make_adapter, _make_inverter

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
        assert execute_joint_site(base) == ("Joint blocked", reason)
        assert not spy["args"]
        assert native_plan_hook_allowed(base) is False
    for mode in ("off", "shadow"):
        base.args["joint_site"]["mode"] = mode
        assert native_plan_hook_allowed(base) is True


def run_joint_site_entrypoint_tests(base):
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
    ):
        with patch("joint_site_native._observation_clock", side_effect=lambda: base.now_utc):
            test(base)
        print("PASS:", test.__name__)
    return False
