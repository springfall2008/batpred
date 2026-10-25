"""Configurable battery blocks preserve native replay, selector invalidation and EV guards."""

from tests.car_planning_fixtures import car_settings
from car_charging_config import native_car_arguments

import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from config import CONFIG_ITEMS
from car_charging_execute import execute_car_charging_plan, execution_fingerprint, publish_car_charging_slot
from car_charging_plan import _options, evaluate_car_charging
from tests.test_car_planning_actual import _actual_inputs, _set_entity


def test_window_selector_definition(base):
    """Expose precisely the requested choices through the existing persisted native select."""
    item = next(item for item in CONFIG_ITEMS if item["name"] == "battery_min_window_minutes")
    assert item["type"] == "select" and item["options"] == ["5", "15", "30", "60"]
    assert item["default"] == "30" and not item.get("reset_inverter_force")


def test_window_new_default_preserves_saved_choice(base):
    """New installs default to thirty; an existing persisted five-minute choice still wins."""
    from battery_window import selected_window_minutes

    name = "battery_min_window_minutes"
    item = base.config_index[name]
    with patch.dict(base.args, {**native_car_arguments({})}), patch.dict(item, {"value": None}):
        assert base.get_arg(name, "30", indirect=False) == "30"
        assert selected_window_minutes(base) == 30
        item["value"] = "5"
        assert selected_window_minutes(base) == 5
        assert item["default"] == "30" and item["value"] == "5"
    missing_native_config = SimpleNamespace(args={}, get_arg=lambda key, default, **kwargs: default)
    assert selected_window_minutes(missing_native_config) == 30


def test_window_command_validator(base):
    """Lost continuity requires a fresh full run; a genuinely mature mode does not relock."""
    from battery_window import commands_respect_windows

    commands = [
        {"start": 0, "end": 5, "target_soc": 0, "freeze": False},
        {"start": 5, "end": 35, "target_soc": 50, "freeze": False},
    ]
    assert not commands_respect_windows(commands, 30)
    assert not commands_respect_windows(commands, 30, "demand", 25)
    assert commands_respect_windows(commands, 30, "demand", 0)
    assert commands_respect_windows(commands[:1], 30)  # A terminal run can extend beyond the horizon.


def window_inputs(base, minutes=30):
    """Use synthetic cached HA states and the real shared template, never a physical writer."""
    inverter, writes, sources, targets = _actual_inputs(base)
    car_settings(base)["battery_min_window_minutes"] = minutes
    car_settings(base)["ev_charging_priority"] = "cost"
    settings = car_settings(base)
    settings["session"]["departure"] = settings["confirmed_session"]["departure"] = (base.now_utc + timedelta(minutes=60)).isoformat()
    base.soc_kw = base.soc_max * 0.8
    inverter.soc_kw, inverter.soc_percent = base.soc_kw, 80
    base.metric_battery_value_scaling = 0
    base.all_active_keep = {}
    return inverter, writes, sources, targets


def test_window_options_and_rejection(base):
    """Literal and dropdown values are exact enums; missing configured evidence fails closed."""
    window_inputs(base)
    settings = car_settings(base)
    for value in (5, 15, 30, 60, "5", "15", "30", "60"):
        settings["battery_min_window_minutes"] = value
        assert _options(base)["battery_min_window_minutes"] == int(value)
    settings["battery_min_window_minutes"] = "input_select.test_battery_window"
    _set_entity(base, settings["battery_min_window_minutes"], "30")
    assert _options(base)["battery_min_window_minutes"] == 30
    for value in (True, None, 0, 10, 30.5, "30.0", "unknown", "unavailable"):
        settings["battery_min_window_minutes"] = value
        assert evaluate_car_charging(base, publish=False)["status"] == "invalid_input", value


def test_window_native_selector_and_fingerprint(base):
    """The built-in persisted dropdown invalidates both plans and execution without an inverter reset."""
    window_inputs(base, 5)
    car_settings(base).pop("battery_min_window_minutes")
    base.expose_config("battery_min_window_minutes", "5")
    first = execution_fingerprint(base)
    base.update_pending, base.plan_valid = False, True
    entity = base.config_index["battery_min_window_minutes"]["entity"]
    with patch.object(base, "components", SimpleNamespace(select_event=AsyncMock())):
        asyncio.run(base.select_event("call_service", {"service_data": {"entity_id": entity, "option": "30"}}, {}))
    assert _options(base)["battery_min_window_minutes"] == 30
    assert base.update_pending and not base.plan_valid
    assert execution_fingerprint(base) != first


def test_window_candidate_and_priority_contracts(base):
    """Each priority retains paired native checks and future battery blocks of the requested minimum."""
    _, writes, _, _ = window_inputs(base)
    for priority in ("cost", "asap", "now"):
        car_settings(base)["ev_charging_priority"] = priority
        report = evaluate_car_charging(base, publish=False)
        assert report["status"] == "candidate", report
        assert report["battery_min_window_minutes"] == 30
        assert all(check["passed"] for check in report["native_checks"].values())
        commands = base.car_charging_pending["commands"]
        lengths = []
        start, mode = commands[0]["start"], commands[0]["target_soc"] > 0
        for command in commands[1:]:
            if (command["target_soc"] > 0) != mode:
                lengths.append(command["start"] - start)
                start, mode = command["start"], command["target_soc"] > 0
        assert all(length >= 30 for length in lengths), lengths
        if priority == "now":
            assert commands[0]["ev_on"]
    assert not writes


def test_window_change_during_search_blocks_dispatch(base):
    """A late dropdown change cannot bless old-window decisions with the new fingerprint."""
    from car_charging_plan import _continuous_grid_support_shadow

    _, writes, _, _ = window_inputs(base)
    settings = car_settings(base)
    settings["battery_min_window_minutes"] = "input_select.test_battery_window"
    _set_entity(base, settings["battery_min_window_minutes"], "30")

    def change_after_search(model, options):
        """Deliver a changed cached selection after the candidate's search."""
        result = _continuous_grid_support_shadow(model, options)
        _set_entity(base, settings["battery_min_window_minutes"], "60")
        return result

    with patch("car_charging_plan._continuous_grid_support_shadow", side_effect=change_after_search):
        report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate", report
    assert execute_car_charging_plan(base)[0] == "EV blocked" and not writes


def test_window_successful_dispatch_keeps_original_start(base):
    """Repeated same-mode dispatch preserves the commitment clock without extending EV lease duration."""
    _, writes, _, _ = window_inputs(base)
    assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
    assert execute_car_charging_plan(base)[0] != "EV blocked", base.car_charging_report
    first = deepcopy(base.battery_window)
    assert first and base.car_charging_execution
    assert execute_car_charging_plan(base)[0] != "EV blocked", base.car_charging_report
    assert base.battery_window["started_at"] == first["started_at"]
    options = _options(base)
    assert options["initial_window_mode"] == first["mode"]
    assert options["initial_window_remaining_minutes"] == 30
    receipt = base.car_charging_execution
    from datetime import datetime

    assert datetime.fromisoformat(receipt["expires_at"]) - datetime.fromisoformat(receipt["generated_at"]) <= timedelta(seconds=300)
    count = len(writes)
    base.set_read_only = True
    publish_car_charging_slot(base)
    assert base.car_charging_execution is None and len(writes) == count


def test_window_replanning_carries_only_remaining_time(base):
    """A new five-minute forecast honours the old block, not a rolling new thirty-minute lock."""
    window_inputs(base)
    assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
    assert execute_car_charging_plan(base)[0] != "EV blocked"
    previous = deepcopy(base.battery_window)
    base.now_utc += timedelta(minutes=5)
    base.minutes_now += 5
    for rates in (base.rate_import, base.rate_export):
        end = max(rates)
        rates.update({minute: rates[end] for minute in range(end + 1, base.minutes_now + base.forecast_minutes)})
    options = _options(base)
    assert options["initial_window_mode"] == previous["mode"]
    assert options["initial_window_remaining_minutes"] == 25
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate", report
    commands = base.car_charging_pending["commands"]
    mode_is_charge = previous["mode"] == "charge"
    assert all((command["target_soc"] > 0) == mode_is_charge for command in commands[:5])
    assert execute_car_charging_plan(base)[0] != "EV blocked", base.car_charging_report
    assert base.battery_window["started_at"] == previous["started_at"]


def test_window_drift_and_restart_do_not_invent_continuity(base):
    """A stability hint is not a hardware lease and cannot survive contrary observations or lost state."""
    _, writes, sources, _ = window_inputs(base)
    assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
    assert execute_car_charging_plan(base)[0] != "EV blocked"
    original = base.ha_interface.get_state(sources[0])
    _set_entity(base, sources[0], "Disabled" if original == "Grid" else "Grid")
    assert _options(base)["initial_window_mode"] is None
    base.battery_window = None
    base.car_charging_execution = None
    count = len(writes)
    publish_car_charging_slot(base)
    assert base.car_charging_execution is None and len(writes) == count


def test_window_solar_loss_still_revokes_short_lease(base):
    """A thirty-minute domestic block cannot override the independent sixty-second solar authority."""
    from datetime import datetime
    from tests.test_car_planning_preferences import solar_forecast, solar_meters

    _, writes, _, _ = window_inputs(base)
    car_settings(base)["ev_charging_priority"] = "solar"
    solar_forecast(base, 0, base.forecast_minutes)
    solar_meters(base)
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate", report
    assert execute_car_charging_plan(base)[0] != "EV blocked", base.car_charging_report
    receipt = base.car_charging_execution
    assert datetime.fromisoformat(receipt["expires_at"]) - datetime.fromisoformat(receipt["generated_at"]) <= timedelta(seconds=60)
    assert base.battery_window["mode"] == "demand"
    count = len(writes)
    solar_meters(base, pv=3000)
    publish_car_charging_slot(base)
    assert base.car_charging_execution is None and len(writes) == count
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"


def run_car_planning_battery_windows_tests(base):
    """Run selector coverage everywhere, and real native replay/dispatch when the companion exists."""
    from tests.test_car_planning_template import shared_template_available, check_unavailable_template
    from unit_test import create_predbat

    test_window_selector_definition(base)
    test_window_new_default_preserves_saved_choice(base)
    test_window_command_validator(base)
    if not shared_template_available():
        check_unavailable_template(base)
        return False
    for test in (
        test_window_options_and_rejection,
        test_window_native_selector_and_fingerprint,
        test_window_candidate_and_priority_contracts,
        test_window_change_during_search_blocks_dispatch,
        test_window_successful_dispatch_keeps_original_start,
        test_window_replanning_carries_only_remaining_time,
        test_window_drift_and_restart_do_not_invent_continuity,
        test_window_solar_loss_still_revokes_short_lease,
    ):
        fresh = create_predbat()
        with patch("car_charging_plan._observation_clock", side_effect=lambda: fresh.now_utc):
            test(fresh)
    return False
