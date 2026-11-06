"""Immediate EV starts remain constrained by real planning, replay and execution evidence."""

from tests.car_planning_fixtures import car_settings

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from car_charging_solver import BatteryConfig, EVConfig, PlannerConfig, PlannerInputError, SiteConfig, Slot, plan
from car_charging_execute import command_fingerprint, execute_car_charging_plan, publish_car_charging_slot
from car_charging_plan import _continuous_grid_support_shadow, _options, _replay_candidate, evaluate_car_charging
from tests.test_car_planning_actual import _set_entity


def test_now_pure_start_constraint():
    """A more expensive immediate start is required, but cooldown and availability still reject it."""
    slots = [Slot(index, index * 5, 5, 0.1, 0, price, 0) for index, price in enumerate((50, 1))]
    ev = EVConfig(25, 0, 0.5, rate_kw=6, efficiency=1, deadline_min=10)
    battery = BatteryConfig(20, 10, 2, 0, 0, 1, 1, 2)
    site = SiteConfig(10)
    ordinary = PlannerConfig(beam_width=None, allowed_battery_modes=("demand",))
    immediate = replace(ordinary, require_ev_start=True)
    cheapest = plan(slots, ev, battery, site, ordinary)
    now = plan(slots, ev, battery, site, immediate)
    assert cheapest.actions[0].ev_action == "off" and now.actions[0].ev_action == "on"
    assert now.total_cost > cheapest.total_cost
    for constrained in (replace(ev, initial_off_minutes=5), replace(ev, availability=(1,))):
        assert plan(slots, constrained, battery, site, ordinary).actions
        assert not plan(slots, constrained, battery, site, immediate).actions
    assert not plan(slots, ev, replace(battery, initial_kwh=4), site, immediate).actions
    for invalid in (1, "true", None):
        try:
            plan(slots, ev, battery, site, replace(immediate, require_ev_start=invalid))
        except PlannerInputError:
            pass
        else:
            raise AssertionError("Immediate-start flag accepted a non-boolean value")


def now_inputs(base):
    """Use the real shared template and watched HA selector with cheaper future tariffs."""
    from tests.test_car_planning_priority import _priority_inputs

    return _priority_inputs(base, priority="now")


def test_now_current_command_and_selector_event(base):
    """A watched NOW selection invalidates old evidence and stages an immediate full-rate lease."""
    from tests.test_car_planning_priority import PRIORITY_ENTITY, _priority_inputs

    writes = _priority_inputs(base, priority="cost")
    assert evaluate_car_charging(base, publish=False)["proposed_ev_windows"][0]["start"] > base.minutes_now
    settings = deepcopy(car_settings(base))
    base.update_pending, base.plan_valid = False, True
    _set_entity(base, PRIORITY_ENTITY, "now")
    asyncio.run(base.trigger_watch_list(PRIORITY_ENTITY, "state", "cost", "now"))
    assert base.update_pending and not base.plan_valid
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate" and report["ev_run_strategy"] == "immediate_paired_continuous_block", report
    assert report["proposed_ev_windows"][0]["start"] == base.minutes_now
    assert all(attempt["start_offset"] == 0 for attempt in report["continuous_block_attempts"])
    assert all(check["passed"] for check in report["native_checks"].values())
    assert base.car_charging_pending["commands"][0]["ev_on"]
    assert execute_car_charging_plan(base)[0] != "EV blocked", base.car_charging_report
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "on"
    assert car_settings(base) == settings
    assert all(service in ("number/set_value", "select/select_option") for service, _ in writes)
    count = len(writes)
    _set_entity(base, PRIORITY_ENTITY, "asap")
    publish_car_charging_slot(base)
    assert base.car_charging_execution is None and len(writes) == count


def test_now_never_substitutes_asap_or_cost_fallback(base):
    """A valid later start under a current domestic floor is ASAP, never an accepted NOW plan."""
    from tests.test_car_planning_priority import PRIORITY_ENTITY

    writes = now_inputs(base)
    base.all_active_keep = {base.minutes_now + minute: 80 for minute in range(5)}
    _set_entity(base, PRIORITY_ENTITY, "asap")
    later = evaluate_car_charging(base, publish=False)
    assert later["status"] == "candidate" and later["proposed_ev_windows"][0]["start"] > base.minutes_now, later
    _set_entity(base, PRIORITY_ENTITY, "now")
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "no_plan_found" and "immediate_start_not_validated" in report["execution_blockers"], report
    assert base.car_charging_pending is None
    assert execute_car_charging_plan(base)[0] == "EV blocked" and not writes


def test_now_cooldown_and_active_continuation(base):
    """NOW cannot waive the OFF cooldown; an already-ON EV does not acquire a new restart wait."""
    writes = now_inputs(base)
    relay = car_settings(base)["relay_entity"]
    _set_entity(base, relay, "off", last_changed=base.now_utc - timedelta(minutes=5))
    assert _continuous_grid_support_shadow(base, _options(base)) == (None, [])
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "no_plan_found" and base.car_charging_pending is None, report
    assert not writes
    _set_entity(base, relay, "on", last_changed=base.now_utc - timedelta(seconds=20))
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate" and report["proposed_ev_windows"][0]["start"] == base.minutes_now, report


def test_now_requires_both_native_forecasts(base):
    """A failed conservative replay cannot turn into a delayed or nominal-only NOW approval."""
    writes = now_inputs(base)

    def failed_conservative(*args, **kwargs):
        """Keep the actual native trajectories but reject the conservative acceptance evidence."""
        windows, checks = _replay_candidate(*args, **kwargs)
        checks["pv10"]["passed"] = False
        return windows, checks

    with patch("car_charging_plan._replay_candidate", side_effect=failed_conservative):
        report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "native_rejected" and "immediate_start_not_validated" in report["execution_blockers"], report
    assert base.car_charging_pending is None and not writes


def test_now_partial_target_preserves_immediate_start(base):
    """Authorised shortfall planning retains the requested target and cannot move the first ON step."""
    from tests.test_car_planning_partial_apply import partial_inputs

    _, writes, _, _ = partial_inputs(base)
    car_settings(base)["ev_charging_priority"] = "now"
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "partial_candidate" and report["ev_target_shortfall_kwh"] > 0, report
    assert report["requested_ev_target_kwh"] == base.car_charging_limit[0] == 25
    assert base.car_charging_pending["commands"][0]["ev_on"]
    assert report["proposed_ev_windows"][0]["start"] == base.minutes_now
    assert execute_car_charging_plan(base)[0] != "EV blocked", base.car_charging_report
    assert all(service in ("number/set_value", "select/select_option") for service, _ in writes)
    # A fresh fixture with the same explicit partial consent still honours cooldown.
    partial_inputs(base)
    car_settings(base)["ev_charging_priority"] = "now"
    _set_entity(base, car_settings(base)["relay_entity"], "off", last_changed=base.now_utc - timedelta(minutes=5))
    assert evaluate_car_charging(base, publish=False)["status"] == "no_plan_found"
    assert base.car_charging_pending is None


def test_now_execution_rejects_a_deferred_command(base):
    """Even a re-fingerprinted schedule cannot satisfy NOW with a current OFF command."""
    writes = now_inputs(base)
    assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
    pending = base.car_charging_pending
    pending["commands"][0]["ev_on"] = False
    pending["command_fingerprint"] = command_fingerprint(pending["commands"])
    assert execute_car_charging_plan(base) == ("EV blocked", "immediate_start_not_validated")
    assert not writes


def test_now_availability_before_vehicle_soc(base):
    """An away vehicle still routes to household control before any EV energy read."""
    from tests.test_car_planning_session import session_inputs

    _, writes, _, _ = session_inputs(base)
    settings = car_settings(base)
    settings["ev_charging_priority"] = "now"
    _set_entity(base, settings["home_entity"], "not_home")
    original = base.get_state_wrapper

    def guard(*args, **kwargs):
        """Reject any direct or indirect attempt to fabricate an absent vehicle's energy."""
        entity = kwargs.get("entity_id", args[0] if args else None)
        assert entity not in (settings["soc_entity"], "sensor.test_ev_energy", "sensor.test_ev_power")
        return original(*args, **kwargs)

    with patch.object(base, "get_state_wrapper", side_effect=guard):
        report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "household_fallback" and base.car_charging_pending is None and not writes


def test_now_completed_target_and_unsupported_controls(base):
    """NOW does not charge a satisfied target or enable an unsupported control backend."""
    now_inputs(base)
    base.car_charging_limit[0] = 0
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate" and not report["proposed_ev_windows"], report
    assert not base.car_charging_pending["commands"][0]["ev_on"]
    assert execute_car_charging_plan(base)[0] != "EV blocked", base.car_charging_report
    car_settings(base).pop("grid_charge_support")
    assert evaluate_car_charging(base, publish=False)["status"] == "invalid_input"


def run_car_planning_now_tests(base, template_available):
    """Run pure constraints independently, and real-template cases only with their actual backend."""
    test_now_pure_start_constraint()
    if template_available:
        from unit_test import create_predbat

        for test in (
            test_now_current_command_and_selector_event,
            test_now_never_substitutes_asap_or_cost_fallback,
            test_now_cooldown_and_active_continuation,
            test_now_requires_both_native_forecasts,
            test_now_partial_target_preserves_immediate_start,
            test_now_execution_rejects_a_deferred_command,
            test_now_availability_before_vehicle_soc,
            test_now_completed_target_and_unsupported_controls,
        ):
            fresh = create_predbat()
            with patch("car_charging_plan._observation_clock", side_effect=lambda: fresh.now_utc):
                test(fresh)
