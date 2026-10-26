"""Cross-state EV authority checks through native dispatch and lease entry points."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from car_charging_execute import execute_car_charging_plan, publish_car_charging_slot, refresh_car_charging_lease
from car_charging_plan import evaluate_car_charging
from tests.car_planning_fixtures import car_settings
from tests.test_car_planning_apply import _make_adapter, _make_inverter
from tests.test_car_planning_entrypoints import _apply_inputs


def _fresh_base():
    """Return an isolated Predbat/HA fixture for one authority case."""
    from unit_test import create_predbat

    return create_predbat()


def _set_state(base, entity_id, state, *, last_updated=None, last_changed=None):
    """Update one cached HA state without refreshing or writing to a device."""
    now = base.now_utc
    base.ha_interface.dummy_items[entity_id] = {
        "state": state,
        "last_updated": now if last_updated is None else last_updated,
        "last_changed": now if last_changed is None else last_changed,
    }


def _set_observation_stamp(base, entity_id, stamp):
    """Set a physical-observation timestamp independently of cache freshness."""
    _set_state(base, entity_id, stamp.isoformat(), last_updated=base.now_utc, last_changed=base.now_utc)


def _stage_local_candidate(base):
    """Stage a genuine native candidate and fake only the local inverter boundary."""
    _apply_inputs(base)
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate", report
    adapter, _ = _make_adapter()
    _, spy = _make_inverter(base, adapter)
    return report, spy


def test_permission_connection_authority_matrix():
    """Only exact permission plus confirmed connection can pass, including warning states."""
    cases = (
        ("on_connected", "on", "connected", None, True, None),
        ("on_configured_warning", "on", "OVERNIGHT_INTERRUPTED", "OVERNIGHT_INTERRUPTED", True, None),
        ("off_connected", "off", "connected", None, False, "permission_not_granted"),
        ("unknown_permission", "unknown", "connected", None, False, "permission_not_granted"),
        ("unavailable_permission", "unavailable", "connected", None, False, "permission_not_granted"),
        ("off_warning", "off", "OVERNIGHT_INTERRUPTED", "OVERNIGHT_INTERRUPTED", False, "permission_not_granted"),
        ("unknown_permission_warning", "unknown", "OVERNIGHT_INTERRUPTED", "OVERNIGHT_INTERRUPTED", False, "permission_not_granted"),
        ("unknown_connection", "on", "unknown", None, False, "connection_not_confirmed"),
        ("unplugged_connection", "on", "disconnected", None, False, "connection_not_confirmed"),
        ("unavailable_connection", "on", "unavailable", None, False, "connection_not_confirmed"),
        ("both_unconfirmed", "off", "disconnected", None, False, "connection_not_confirmed"),
    )

    for name, permission, connection, warning, expected_accept, expected_reason in cases:
        base = _fresh_base()
        with patch("car_charging_plan._observation_clock", side_effect=lambda: base.now_utc), patch("car_charging_execute._clock", side_effect=lambda: base.now_utc):
            _apply_inputs(base)
            settings = car_settings(base)
            if warning:
                settings["connection_warning_states"] = [warning]
            report = evaluate_car_charging(base, publish=False)
            assert report["status"] == "candidate", (name, report)
            _set_state(base, settings["permission_entity"], permission)
            _set_state(base, settings["connection_entity"], connection)
            adapter, _ = _make_adapter()
            _, actuator = _make_inverter(base, adapter)
            result = execute_car_charging_plan(base)

        if expected_accept:
            assert isinstance(result, tuple) and result[0] != "EV blocked", (name, result)
            assert actuator["args"], (name, result)
            assert base.car_charging_execution is not None, (name, base.car_charging_report)
            assert base.car_charging_report["hardware_confirmed"] is False, (name, base.car_charging_report)
            assert base.car_charging_report["ready_to_activate"] is False, (name, base.car_charging_report)
        else:
            assert result == ("EV blocked", expected_reason), (name, result, base.car_charging_report)
            assert actuator["args"] == [], (name, actuator)
            assert base.car_charging_execution is None, (name, base.car_charging_execution)
        print("PASS:", name)


def test_applied_authority_revocation_matrix():
    """A formerly accepted receipt is revoked on later permission, telemetry, or config drift."""
    cases = (
        ("permission_unknown", "permission", "unknown", "permission_not_granted"),
        ("connection_unknown", "connection", "unknown", "connection_not_confirmed"),
        ("connection_unplugged", "connection", "disconnected", "connection_not_confirmed"),
        ("soc_stamp_stale", "soc_stamp", None, "physical_observation_stale_or_future"),
        ("connection_stamp_future", "connection_stamp", None, "physical_observation_stale_or_future"),
        ("permission_timestamp_future", "permission_future", None, None),
        ("read_only_after_apply", "read_only", None, "read_only"),
        ("configuration_changed", "configuration", None, "configuration_changed"),
        ("relay_unknown", "relay", "unknown", None),
    )

    for name, fault, value, expected_reason in cases:
        base = _fresh_base()
        with patch("car_charging_plan._observation_clock", side_effect=lambda: base.now_utc), patch("car_charging_execute._clock", side_effect=lambda: base.now_utc):
            _, actuator = _stage_local_candidate(base)
            accepted = execute_car_charging_plan(base)
            assert isinstance(accepted, tuple) and accepted[0] != "EV blocked", (name, accepted)
            assert base.car_charging_execution is not None, (name, base.car_charging_report)
            dispatches = deepcopy(actuator["args"])
            settings = car_settings(base)
            if fault == "permission":
                _set_state(base, settings["permission_entity"], value)
            elif fault == "connection":
                _set_state(base, settings["connection_entity"], value)
            elif fault == "soc_stamp":
                _set_observation_stamp(base, settings["soc_observed_at_entity"], base.now_utc - timedelta(seconds=181))
            elif fault == "connection_stamp":
                _set_observation_stamp(base, settings["connection_observed_at_entity"], base.now_utc + timedelta(seconds=1))
            elif fault == "permission_future":
                item = base.ha_interface.dummy_items[settings["permission_entity"]]
                item["last_updated"] = base.now_utc + timedelta(seconds=1)
            elif fault == "read_only":
                base.set_read_only = True
            elif fault == "configuration":
                base.car_charging_limit[0] += 1
            else:
                _set_state(base, settings["relay_entity"], value)

            renewed = publish_car_charging_slot(base, renew=True)

        assert renewed is True, (name, renewed)
        assert base.car_charging_execution is None, (name, base.car_charging_execution)
        assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off", name
        assert actuator["args"] == dispatches, (name, actuator["args"])
        assert base.car_charging_report["execution_enabled"] is False, (name, base.car_charging_report)
        if expected_reason is not None:
            assert base.car_charging_report["execution_reason"] == expected_reason, (name, base.car_charging_report)
        print("PASS:", name)


def test_publication_cannot_restore_authority_after_failure_or_restart():
    """Recovered connection and surviving dashboard state cannot replace a lost native receipt."""
    for name in ("overnight_disconnect", "restart_without_receipt"):
        base = _fresh_base()
        with patch("car_charging_plan._observation_clock", side_effect=lambda: base.now_utc), patch("car_charging_execute._clock", side_effect=lambda: base.now_utc):
            _, actuator = _stage_local_candidate(base)
            assert execute_car_charging_plan(base)[0] != "EV blocked", base.car_charging_report
            assert base.car_charging_execution is not None
            settings = car_settings(base)

            if name == "overnight_disconnect":
                _set_state(base, settings["connection_entity"], "disconnected")
                publish_car_charging_slot(base)
                assert base.car_charging_execution is None
                assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"
                # The valid connection returns, but publication may only validate an existing receipt.
                _set_state(base, settings["connection_entity"], "connected")
                _set_observation_stamp(base, settings["connection_observed_at_entity"], base.now_utc)
            else:
                # Simulate process loss while the previously published HA cache still says ON.
                base.car_charging_execution = None
                base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] = "on"

            assert publish_car_charging_slot(base, renew=True) is True

        assert base.car_charging_execution is None, (name, base.car_charging_execution)
        assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off", name
        assert actuator["args"], name
        assert base.car_charging_report["hardware_confirmed"] is False, (name, base.car_charging_report)
        print("PASS:", name)


def test_modes_keep_execution_authority_separate():
    """Off/shadow are no-ops; apply can execute, but read-only cannot, and success is not attestation."""
    cases = (("off", False, False), ("shadow", False, False), ("apply", False, True), ("apply", True, False))
    for mode, read_only, expected_accept in cases:
        base = _fresh_base()
        with patch("car_charging_plan._observation_clock", side_effect=lambda: base.now_utc), patch("car_charging_execute._clock", side_effect=lambda: base.now_utc):
            _, actuator = _stage_local_candidate(base)
            car_settings(base)["mode"] = mode
            base.set_read_only = read_only
            result = execute_car_charging_plan(base)
            if mode in ("off", "shadow"):
                assert result is None, (mode, result)
                assert publish_car_charging_slot(base) is False
            elif read_only:
                assert result == ("EV blocked", "read_only"), result
            else:
                assert isinstance(result, tuple) and result[0] != "EV blocked", result

        if expected_accept:
            assert actuator["args"], (mode, read_only)
            assert base.car_charging_execution is not None
            assert base.car_charging_report["hardware_confirmed"] is False
            assert base.car_charging_report["ready_to_activate"] is False
        else:
            assert actuator["args"] == [], (mode, read_only, actuator)
            assert base.car_charging_execution is None, (mode, read_only)
        print("PASS:", f"{mode}_read_only_{read_only}")


def test_target_complete_stop_waits_for_observed_relay_off():
    """A genuine completed target requests OFF but cannot claim the relay already stopped."""
    base = _fresh_base()
    with patch("car_charging_plan._observation_clock", side_effect=lambda: base.now_utc), patch("car_charging_execute._clock", side_effect=lambda: base.now_utc):
        _apply_inputs(base)
        settings = car_settings(base)
        base.car_charging_limit[0] = 0
        report = evaluate_car_charging(base, publish=False)
        assert report["status"] == "candidate", report
        assert not report["proposed_ev_windows"], report
        assert all(not command["ev_on"] for command in base.car_charging_pending["commands"])
        _set_state(base, settings["relay_entity"], "on")
        adapter, _ = _make_adapter()
        _, actuator = _make_inverter(base, adapter)

        result = execute_car_charging_plan(base)

    assert result == ("EV blocked", "waiting_for_ev_relay_off"), result
    assert actuator["args"] == []
    assert base.car_charging_execution is None
    assert base.car_charging_report["hardware_confirmed"] is False
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"
    print("PASS:", "target_complete_stop_waits_for_relay_off")


def test_receipt_and_plan_renewal_exact_boundaries():
    """Ordinary publication cannot roll a 300-second receipt; confirmed renewal stops at 600s."""
    from tests.test_car_planning_actual import _actual_inputs

    # Non-renewing publication preserves the exact accepted lease just before TTL,
    # then revokes it at the strict 300-second boundary.
    base = _fresh_base()
    with patch("car_charging_plan._observation_clock", side_effect=lambda: base.now_utc), patch("car_charging_execute._clock", side_effect=lambda: base.now_utc):
        _, actuator = _stage_local_candidate(base)
        assert execute_car_charging_plan(base)[0] != "EV blocked", base.car_charging_report
        receipt = deepcopy(base.car_charging_execution)
        stamp_entities = ("sensor.test_soc_observed_at", "sensor.test_connection_observed_at")
        for offset, retained in ((299, True), (300, False)):
            now = base.now_utc + timedelta(seconds=offset)
            for entity in stamp_entities:
                _set_observation_stamp(base, entity, now)
            with patch("car_charging_plan._observation_clock", side_effect=lambda: now), patch("car_charging_execute._clock", side_effect=lambda: now):
                assert publish_car_charging_slot(base) is True
            if retained:
                assert base.car_charging_execution == receipt
                assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["attributes"]["valid_until"] == receipt["expires_at"]
            else:
                assert base.car_charging_execution is None
                assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"
            assert len(actuator["args"]) == 1
            print("PASS:", f"receipt_ttl_{offset}s")

    # A confirmed shared-control lease can bridge repeated calculations, but its
    # immutable plan clock—not publication frequency—caps it at exactly 600s.
    base = _fresh_base()
    inverter, writes, _, _ = _actual_inputs(base)
    base.all_active_keep = {}
    base.soc_kw = base.soc_max * 0.8
    inverter.soc_kw, inverter.soc_percent = base.soc_kw, 80
    with patch("car_charging_plan._observation_clock", side_effect=lambda: base.now_utc), patch("car_charging_execute._clock", side_effect=lambda: base.now_utc):
        report = evaluate_car_charging(base, publish=False)
        assert report["status"] == "candidate", report
        assert execute_car_charging_plan(base)[0] != "EV blocked", base.car_charging_report
        initial = deepcopy(base.car_charging_execution)
        plan_time = base.now_utc
        write_count = len(writes)
        for offset in (240, 480, 599):
            now = plan_time + timedelta(seconds=offset)
            with patch("car_charging_plan._observation_clock", side_effect=lambda: now), patch("car_charging_execute._clock", side_effect=lambda: now):
                refresh_car_charging_lease(base)
            current = base.car_charging_execution
            assert current is not None, (offset, base.car_charging_report)
            assert current["plan_generated_at"] == initial["plan_generated_at"]
            assert current["generated_at"] == now.isoformat()
            assert current["expires_at"] <= (plan_time + timedelta(seconds=600)).isoformat()
            assert len(writes) == write_count, (offset, writes)
            print("PASS:", f"plan_renewal_{offset}s")
        at_limit = plan_time + timedelta(seconds=600)
        with patch("car_charging_plan._observation_clock", side_effect=lambda: at_limit), patch("car_charging_execute._clock", side_effect=lambda: at_limit):
            refresh_car_charging_lease(base)
        assert base.car_charging_execution is None, base.car_charging_report
        assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"
        assert len(writes) == write_count
        print("PASS:", "plan_renewal_600s")


def run_car_planning_authority_matrix_tests(base):
    """Run authority sequences on fresh, deterministic in-memory HA fixtures."""
    del base  # Each row gets a genuinely independent Predbat fixture.
    test_permission_connection_authority_matrix()
    test_applied_authority_revocation_matrix()
    test_publication_cannot_restore_authority_after_failure_or_restart()
    test_modes_keep_execution_authority_separate()
    test_target_complete_stop_waits_for_observed_relay_off()
    test_receipt_and_plan_renewal_exact_boundaries()
    return False
