"""Exercise the HA dropdown through planning, invalidation and bounded execution."""

from tests.car_planning_fixtures import car_settings

import asyncio
from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from car_charging_execute import execute_car_charging_plan, publish_car_charging_slot, execution_fingerprint
from car_charging_plan import _options, _continuous_grid_support_shadow, evaluate_car_charging
from tests.test_car_planning_actual import _actual_inputs, _set_entity

PRIORITY_ENTITY = "input_select.test_ev_priority"


def _priority_inputs(base, priority="asap"):
    """Provide two safe tariff bands and an entity-backed priority on the real template."""
    inverter, writes, _, _ = _actual_inputs(base)
    settings = car_settings(base)
    settings["ev_charging_priority"] = PRIORITY_ENTITY
    settings["session"]["departure"] = settings["confirmed_session"]["departure"] = (base.now_utc + timedelta(minutes=60)).isoformat()
    base.soc_kw = base.soc_max * 0.8
    inverter.soc_kw, inverter.soc_percent = base.soc_kw, 80
    base.metric_battery_value_scaling = 0
    base.rate_import.update({base.minutes_now + minute: 50 if minute < 30 else 1 for minute in range(60)})
    base.watch_list = [PRIORITY_ENTITY]
    _set_entity(base, PRIORITY_ENTITY, priority)
    return writes


def test_priority_dropdown_replans_and_preserves_target(base):
    """A watched dropdown switches tariff preference while preserving the session and guards."""
    writes = _priority_inputs(base, priority="now")
    settings = deepcopy(car_settings(base))
    limits = (base.reserve, base.car_charging_rate[:], base.car_charging_limit[:], base.car_charging_battery_size[:])
    first = evaluate_car_charging(base, publish=False)
    assert first["status"] == "candidate" and first["ev_charging_priority"] == "now", first
    assert first["proposed_ev_windows"][0]["start"] == base.minutes_now
    assert execute_car_charging_plan(base)[0] != "EV blocked", base.car_charging_report
    fingerprint = execution_fingerprint(base)
    count = len(writes)
    base.update_pending, base.plan_valid = False, True
    _set_entity(base, PRIORITY_ENTITY, "cost")
    asyncio.run(base.trigger_watch_list(PRIORITY_ENTITY, "state", "now", "cost"))
    assert base.update_pending is True and base.plan_valid is False
    assert execution_fingerprint(base) != fingerprint
    publish_car_charging_slot(base)
    assert base.car_charging_execution is None and len(writes) == count
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"
    second = evaluate_car_charging(base, publish=False)
    assert second["status"] == "candidate" and second["ev_charging_priority"] == "cost", second
    assert second["proposed_ev_windows"][0]["start"] > first["proposed_ev_windows"][0]["start"]
    for report in (first, second):
        assert all(check["passed"] for check in report["native_checks"].values())
        assert all(check["final_ev_kwh"] >= base.car_charging_limit[0] - 0.002 for check in report["native_checks"].values())
        assert max(window["end"] for window in report["proposed_ev_windows"]) <= base.minutes_now + 60
    assert car_settings(base) == settings
    assert limits == (base.reserve, base.car_charging_rate, base.car_charging_limit, base.car_charging_battery_size)
    assert not any(service.startswith("switch") for service, _ in writes)


def test_priority_dropdown_unavailable_revokes_authority(base):
    """Missing or invalid dropdown states cannot silently fall back to another priority."""
    for value in ("unknown", "unavailable", "fastest", None):
        writes = _priority_inputs(base)
        assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
        if value is None:
            base.ha_interface.dummy_items.pop(PRIORITY_ENTITY)
        else:
            _set_entity(base, PRIORITY_ENTITY, value)
        assert execute_car_charging_plan(base)[0] == "EV blocked" and not writes
        assert evaluate_car_charging(base, publish=False)["status"] == "invalid_input"
        assert base.car_charging_pending is None


def test_priority_change_during_calculation_rejects_old_candidate(base):
    """A late selection cannot stamp an old-priority candidate with the new fingerprint."""
    writes = _priority_inputs(base)

    def select_after_search(model, options):
        """Deliver a HA state event after the old-priority search has completed."""
        result = _continuous_grid_support_shadow(model, options)
        _set_entity(base, PRIORITY_ENTITY, "cost")
        return result

    with patch("car_charging_plan._continuous_grid_support_shadow", side_effect=select_after_search):
        report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate" and report["ev_charging_priority"] == "asap", report
    assert base.car_charging_pending["fingerprint"] == execution_fingerprint(base)
    assert execute_car_charging_plan(base)[0] == "EV blocked" and not writes


def test_priority_literal_and_select_compatibility(base):
    """Existing literal settings and HA select entities retain the same enum validation."""
    _priority_inputs(base)
    settings = car_settings(base)
    for priority in ("cost", "asap", "now", "solar"):
        settings["ev_charging_priority"] = priority
        assert _options(base)["ev_charging_priority"] == priority
    settings.pop("ev_charging_priority")
    assert _options(base)["ev_charging_priority"] == "cost"
    settings["ev_charging_priority"] = "select.test_ev_priority"
    _set_entity(base, settings["ev_charging_priority"], "asap")
    assert _options(base)["ev_charging_priority"] == "asap"


def test_priority_dropdown_preserves_recurring_session(base):
    """Changing preference cannot renew standing consent or reset the physical meter baseline."""
    from car_charging_recurring import resolve_recurring_session
    from tests.test_car_planning_recurring import recurring_inputs

    (_, writes, _, _), store = recurring_inputs(base)
    settings = car_settings(base)
    settings["ev_charging_priority"] = PRIORITY_ENTITY
    _set_entity(base, PRIORITY_ENTITY, "asap")
    first = resolve_recurring_session(base)
    assert first["status"] == "ready", first
    saved = deepcopy(store.records)
    fingerprint = execution_fingerprint(base)
    _set_entity(base, PRIORITY_ENTITY, "cost")
    assert _options(base)["ev_charging_priority"] == "cost"
    second = resolve_recurring_session(base)
    assert second["status"] == "ready" and second["identity"] == first["identity"]
    assert second["session"] == first["session"] and store.records == saved
    assert execution_fingerprint(base) != fingerprint and not writes


def run_car_planning_priority_tests(base):
    """Run deterministic dropdown tests against the native template and HA cache."""
    from tests.test_car_planning_template import shared_template_available, check_unavailable_template
    from tests.test_car_planning_now import run_car_planning_now_tests
    from tests.test_car_planning_preferences import run_car_planning_preferences_tests

    run_car_planning_now_tests(base, shared_template_available())
    run_car_planning_preferences_tests(base, shared_template_available())
    if not shared_template_available():
        check_unavailable_template(base)
        return False
    with patch("car_charging_plan._observation_clock", side_effect=lambda: base.now_utc):
        for test in (
            test_priority_dropdown_replans_and_preserves_target,
            test_priority_dropdown_unavailable_revokes_authority,
            test_priority_change_during_calculation_rejects_old_candidate,
            test_priority_literal_and_select_compatibility,
            test_priority_dropdown_preserves_recurring_session,
        ):
            test(base)
    return False
