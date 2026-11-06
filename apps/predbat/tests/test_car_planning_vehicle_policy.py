"""Vehicle-specific codes and quality markers are configuration, never engine policy."""

from tests.car_planning_fixtures import car_settings

from datetime import timedelta
from unittest.mock import patch


def test_vehicle_warning_codes_and_live_execution(base):
    """Arbitrary configured warnings pass real replay/dispatch without a session error."""
    from car_charging_execute import execute_car_charging_plan, publish_car_charging_slot
    from car_charging_plan import evaluate_car_charging
    from car_charging_recurring import resolve_recurring_session
    from tests.test_car_planning_actual import _set_entity
    from tests.test_car_planning_recurring import recurring_inputs

    for code in ("POWER_INTERRUPTED", "203"):
        recurring_inputs(base)
        settings = car_settings(base)
        settings.pop("home_entity")
        settings["connection_warning_states"] = [code]
        _set_entity(base, settings["connection_entity"], code, attributes={"retrievalstatus": "VALID"})
        with patch.object(base, "log") as log:
            first = resolve_recurring_session(base, now=base.now_utc)
            assert first["status"] == "ready", first
            assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
            execute_car_charging_plan(base)
            assert base.car_charging_execution is not None, base.car_charging_report
            assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "on"
            messages = [call.args[0] for call in log.call_args_list if call.args and str(call.args[0]).startswith("Warn: EV configured connection warning")]
            assert len(messages) == 1 and code in messages[0], messages
        _set_entity(base, settings["connection_entity"], "disconnected", attributes={"retrievalstatus": "VALID"})
        publish_car_charging_slot(base)
        assert base.car_charging_execution is None
        assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"


def test_vehicle_quality_marker_is_optional_and_configurable(base):
    """Unrelated integrations work without a vendor field; configured quality fails closed."""
    from car_charging_recurring import resolve_recurring_session
    from tests.test_car_planning_actual import _set_entity
    from tests.test_car_planning_recurring import recurring_inputs

    for quality in (None, {"attribute": "data_quality", "valid_states": ["fresh", "checked"]}):
        recurring_inputs(base)
        settings = car_settings(base)
        settings.pop("vehicle_state_validity", None)
        if quality is not None:
            settings["vehicle_state_validity"] = quality
        for name, value in (("connection_entity", "connected"), ("soc_entity", 0)):
            _set_entity(base, settings[name], value, attributes={} if quality is None else {"data_quality": "checked"})
        assert resolve_recurring_session(base, now=base.now_utc)["status"] == "ready"
        if quality is not None:
            _set_entity(base, settings["connection_entity"], "connected", attributes={})
            assert resolve_recurring_session(base, now=base.now_utc)["reason"] == "connection_not_confirmed"
            _set_entity(base, settings["connection_entity"], "connected", attributes={"data_quality": "fresh"})
            _set_entity(base, settings["soc_entity"], 0, attributes={"data_quality": "stale"})
            assert resolve_recurring_session(base, now=base.now_utc)["reason"] == "recurring_observation_invalid"


def test_vehicle_warning_configuration_is_strict(base):
    """Unknown telemetry cannot be promoted to connection evidence by a warning list."""
    from car_charging_plan import _options
    from tests.test_car_planning_recurring import recurring_inputs

    for value in ("203", [203], [""], ["unknown"], ["unavailable"], ["none"], [True]):
        recurring_inputs(base)
        car_settings(base)["connection_warning_states"] = value
        try:
            _options(base)
        except ValueError:
            pass
        else:
            raise AssertionError(("Invalid warning list accepted", value))
    for value in ({}, {"attribute": "", "valid_states": ["fresh"]}, {"attribute": "quality", "valid_states": []}, {"attribute": "quality", "valid_states": "fresh"}):
        recurring_inputs(base)
        car_settings(base)["vehicle_state_validity"] = value
        try:
            _options(base)
        except ValueError:
            pass
        else:
            raise AssertionError(("Invalid quality policy accepted", value))


def test_vehicle_acknowledgement_binds_configured_state(base):
    """Both acknowledgement paths bind arbitrary exact codes and reject later publications."""
    from car_charging_execute import _acknowledged_stop_state
    from car_charging_recurring import resolve_recurring_session
    from tests.test_car_planning_actual import _actual_inputs, _set_entity
    from tests.test_car_planning_recurring import recurring_inputs

    recurring_inputs(base)
    settings = car_settings(base)
    settings.pop("home_entity")
    first = resolve_recurring_session(base, now=base.now_utc)
    stopped = base.now_utc + timedelta(seconds=1)
    accepted = stopped + timedelta(seconds=1)
    code = "INTERRUPTED_BY_EXECUTOR"
    _set_entity(base, settings["connection_entity"], code, last_changed=stopped, last_updated=stopped, attributes={"retrievalstatus": "VALID"})
    settings["connection_error_acknowledgement"] = {"state": code, "observed_at": stopped.isoformat(), "acknowledged_at": accepted.isoformat(), "departure": first["session"]["departure"], "session_identity": first["identity"]}
    result = resolve_recurring_session(base, now=accepted)
    assert result["status"] == "ready" and result["identity"] == first["identity"], result
    assert result["observation"]["connection_error_state"] == code
    _set_entity(base, settings["connection_entity"], "ANOTHER_CODE", last_changed=stopped, last_updated=stopped, attributes={"retrievalstatus": "VALID"})
    assert resolve_recurring_session(base, now=accepted)["status"] == "blocked"
    _actual_inputs(base)
    settings = car_settings(base)
    observed = base.now_utc - timedelta(minutes=1)
    settings["connection_error_acknowledgement"] = {"state": code, "observed_at": observed.isoformat(), "acknowledged_at": base.now_utc.isoformat(), "departure": settings["session"]["departure"]}
    connection = {"state": code, "last_updated": observed}
    assert _acknowledged_stop_state(settings, connection, base.now_utc)
    assert not _acknowledged_stop_state(settings, {**connection, "state": "ANOTHER_CODE"}, base.now_utc)
    assert not _acknowledged_stop_state(settings, {**connection, "last_updated": base.now_utc}, base.now_utc)


def test_vehicle_physics_has_no_installation_defaults(base):
    """Pure EV inputs require explicitly supplied rate and efficiency."""
    from car_charging_solver import EVConfig

    try:
        EVConfig(40, 5, 30)
    except TypeError:
        pass
    else:
        raise AssertionError("EV power and efficiency must not come from installation defaults")
    ev = EVConfig(40, 5, 30, rate_kw=3.5, efficiency=0.87)
    assert ev.rate_kw == 3.5 and ev.efficiency == 0.87


def test_vehicle_runtime_has_no_vendor_constants(base):
    """Prevent a known integration's codes, attribute names or charger defaults returning."""
    import ast
    import importlib
    import inspect

    for name in ("car_charging_solver", "car_charging_plan", "car_charging_observation", "car_charging_recurring", "car_charging_policy", "car_charging_execute"):
        tree = ast.parse(inspect.getsource(importlib.import_module(name)))
        constants = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)]
        assert not any(value in ("4", "retrievalstatus", "VALID") for value in constants if isinstance(value, str)), name
        assert not any(value == 6.77 or value == 0.92 for value in constants if type(value) is float), name


def test_vehicle_native_yaml_location_and_lease_binding(base):
    """Flat car settings work without legacy fields and changing them revokes an old lease."""
    from config import APPS_SCHEMA
    from car_charging_execute import execute_car_charging_plan, publish_car_charging_slot
    from car_charging_plan import evaluate_car_charging
    from tests.test_car_planning_actual import _set_entity
    from tests.test_car_planning_recurring import recurring_inputs

    recurring_inputs(base)
    settings = car_settings(base)
    settings.pop("vehicle_state_validity", None)
    settings.pop("connection_warning_states", None)
    base.args["car_charging_warning_states"] = ["POWER_INTERRUPTED"]
    base.args["car_charging_state_validity"] = {"attribute": "data_quality", "valid_states": ["fresh"]}
    _set_entity(base, settings["connection_entity"], "POWER_INTERRUPTED", attributes={"data_quality": "fresh"})
    _set_entity(base, settings["soc_entity"], 0, attributes={"data_quality": "fresh"})
    assert APPS_SCHEMA["car_charging_warning_states"]["type"] == "string_list"
    assert APPS_SCHEMA["car_charging_state_validity"]["type"] == "dict"
    assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
    execute_car_charging_plan(base)
    assert base.car_charging_execution is not None, base.car_charging_report
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "on"
    # The live state remains allowed: revocation must bind the changed policy,
    # not accidentally pass only because the connection became ineligible.
    base.args["car_charging_warning_states"].append("ANOTHER_WARNING")
    publish_car_charging_slot(base)
    assert base.car_charging_execution is None
    assert base.car_charging_report["execution_reason"] == "configuration_changed", base.car_charging_report


def test_vehicle_native_yaml_conflicts_fail_closed(base):
    """Equivalent legacy settings remain readable but contradictory duplicate policies fail."""
    from car_charging_observation import vehicle_policy

    settings = {"connection_entity_states": ["connected"], "connection_warning_states": ["INTERRUPTED"], "vehicle_state_validity": {"attribute": "quality", "valid_states": ["fresh"]}}
    args = {"car_charging_warning_states": ["INTERRUPTED"], "car_charging_state_validity": {"attribute": "quality", "valid_states": ["fresh"]}}
    assert vehicle_policy(settings, args) == vehicle_policy(settings)
    for key, value in (("car_charging_warning_states", []), ("car_charging_state_validity", None)):
        try:
            vehicle_policy(settings, {**args, key: value})
        except ValueError:
            pass
        else:
            raise AssertionError(("Conflicting vehicle policies accepted", key))


def test_vehicle_policy_contract(base):
    """Run each integration scenario on a fresh real Predbat fixture."""
    from unit_test import create_predbat

    for test in (
        test_vehicle_warning_codes_and_live_execution,
        test_vehicle_quality_marker_is_optional_and_configurable,
        test_vehicle_warning_configuration_is_strict,
        test_vehicle_acknowledgement_binds_configured_state,
        test_vehicle_physics_has_no_installation_defaults,
        test_vehicle_runtime_has_no_vendor_constants,
        test_vehicle_native_yaml_location_and_lease_binding,
        test_vehicle_native_yaml_conflicts_fail_closed,
    ):
        fresh = create_predbat()
        with patch("car_charging_plan._observation_clock", side_effect=lambda: fresh.now_utc):
            test(fresh)
        print("PASS: " + test.__name__)
