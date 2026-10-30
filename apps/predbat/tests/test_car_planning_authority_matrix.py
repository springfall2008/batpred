"""Cross-state EV authority checks through native dispatch and lease entry points."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
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


def test_native_charger_control_ownership():
    """Independent native control loops must never share an external executor's apply lease."""
    from car_charging_execute import execution_fingerprint

    controls = (
        ("hypervolt", "hypervolt_control", "hypervolt_control"),
        ("gateway", "gateway_evc_control", "gateway_evc_control"),
        ("myenergi", "myenergi_zappi_control", "zappi_control"),
        ("gecloud", "ge_cloud_evc_control", "evc_control"),
        ("ohme", "ohme_control", "ohme_control"),
        ("wallbox", "wallbox_control", "wallbox_control"),
    )
    for component, setting, runtime_setting in controls:
        for runtime_only in (False, True):
            base = _fresh_base()
            with patch("car_charging_plan._observation_clock", side_effect=lambda: base.now_utc), patch("car_charging_execute._clock", side_effect=lambda: base.now_utc):
                _, actuator = _stage_local_candidate(base)
                original = execution_fingerprint(base)
                if runtime_only:
                    native = SimpleNamespace(**{runtime_setting: True})
                    base.components = SimpleNamespace(get_component=lambda name: native if name == component else None)
                else:
                    base.args[setting] = True
                assert execution_fingerprint(base) != original, (component, runtime_only)
                assert execute_car_charging_plan(base) == ("EV blocked", "native_charger_control_unsupported")
                assert not actuator["args"]
                assert base.car_charging_execution is None
                report = evaluate_car_charging(base, publish=False)
                assert report["status"] == "unsupported" and "native_charger_control_unsupported" in report["execution_blockers"], report
                assert base.car_charging_pending is None

            # A later independent controller activation invalidates an accepted
            # lease without any extra inverter write or external switch command.
            base = _fresh_base()
            with patch("car_charging_plan._observation_clock", side_effect=lambda: base.now_utc), patch("car_charging_execute._clock", side_effect=lambda: base.now_utc):
                _, actuator = _stage_local_candidate(base)
                assert execute_car_charging_plan(base)[0] != "EV blocked"
                assert base.car_charging_execution is not None
                writes = len(actuator["args"])
                if runtime_only:
                    native = SimpleNamespace(**{runtime_setting: True})
                    base.components = SimpleNamespace(get_component=lambda name: native if name == component else None)
                else:
                    base.args[setting] = True
                assert publish_car_charging_slot(base) is True
                assert base.car_charging_execution is None
                assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"
                assert base.car_charging_report["execution_reason"] == "native_charger_control_unsupported"
                assert len(actuator["args"]) == writes
            print("PASS:", component, "runtime" if runtime_only else "configured", "single_executor_ownership")

    # Disabled options alone do not prove a component has finished handing back
    # hardware it previously held. Automatic shared control is not disabled.
    from car_charging_execute import native_charger_control_conflict

    for component, setting, runtime_setting in controls:
        for state in ({"charger_control_config": None}, {"charger_control_active": True}, {"charger_control_state": {"charger": False}}):
            base = _fresh_base()
            native = SimpleNamespace(**{runtime_setting: False, **state})
            base.components = SimpleNamespace(get_component=lambda name: native if name == component else None)
            assert native_charger_control_conflict(base), (component, state)
        base = _fresh_base()
        native = SimpleNamespace(**{runtime_setting: False, "charger_control_config": False, "charger_control_active": False, "charger_control_state": {}})
        base.components = SimpleNamespace(get_component=lambda name: native if name == component else None)
        assert not native_charger_control_conflict(base), component

    # Explicitly disabled native controls permit the existing apply path.
    base = _fresh_base()
    with patch("car_charging_plan._observation_clock", side_effect=lambda: base.now_utc), patch("car_charging_execute._clock", side_effect=lambda: base.now_utc):
        _apply_inputs(base)
        base.args.update({setting: False for _, setting, _ in controls})
        assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
        adapter, _ = _make_adapter()
        _make_inverter(base, adapter)
        assert execute_car_charging_plan(base)[0] != "EV blocked"
        assert base.car_charging_execution is not None
    print("PASS:", "native_controls_disabled_preserves_apply")


def test_wallbox_retained_ownership():
    """Actual Wallbox restore/release state must settle before an external apply lease."""
    from car_charging_execute import native_charger_control_conflict
    from ha import run_async
    from tests.test_wallbox import _Storage, _control_component, _status, PLAN_OUTSIDE, CONTROL_NOW

    for field in ("paused", "schedule"):
        storage = _Storage()
        storage.data[("wallbox", "control_state")] = {"control_enabled": False, "paused": [], "schedule": [], field: ["101"]}
        native = _control_component({0: PLAN_OUTSIDE}, {101: _status(status_id=178, power=0)}, storage=storage, wallbox_control=False)
        assert native.charger_control_active is False and native.wallbox_control is False
        base = _fresh_base()
        with patch("car_charging_plan._observation_clock", side_effect=lambda: base.now_utc), patch("car_charging_execute._clock", side_effect=lambda: base.now_utc):
            _, actuator = _stage_local_candidate(base)
            base.components = SimpleNamespace(get_component=lambda name: native if name == "wallbox" else None)
            assert execute_car_charging_plan(base) == ("EV blocked", "native_charger_control_unsupported"), field
            assert not actuator["args"] and base.car_charging_execution is None
            assert native.transport.calls == [], "The guard must not release hardware itself"
            # The native poll can resume a charger even with its option off.
            # Only after that existing release path settles can apply proceed.
            run_async(native.control_tick(CONTROL_NOW))
            assert ("resume_schedule", 101) in native.transport.calls, (field, native.transport.calls)
            if field == "paused":
                assert ("resume", 101) in native.transport.calls
            assert not native_charger_control_conflict(base), (field, native.transport.calls)
            native.charger_control_loaded = False
            assert native_charger_control_conflict(base), "Unread persistent ownership is not settled"
            run_async(native.charger_control_load_enabled())
            assert not native_charger_control_conflict(base)
            native.charger_control_save_pending = True
            assert native_charger_control_conflict(base), "Unsaved ownership is not settled"
            run_async(native.charger_control_save())
            assert not native_charger_control_conflict(base)
        print("PASS:", "wallbox_retained_" + field)


def test_hypervolt_retained_ownership():
    """Missing chargers and failed restores cannot hide Hypervolt's persisted releases."""
    from car_charging_execute import native_charger_control_conflict
    from ha import run_async
    from tests.test_hypervolt import _Storage, _control_component, _charger, _requests, PLAN_OUTSIDE, CONTROL_NOW, V3_ID

    for failed_restore in (False, True):
        storage = _Storage()
        storage.data[("hypervolt", "control_state")] = {"control_enabled": False, "released": [V3_ID]}
        storage.fail_loads = int(failed_restore)
        native = _control_component({0: PLAN_OUTSIDE}, chargers=[], storage=storage, hypervolt_control=False)
        native.sync_held()
        assert not native.charger_control_state, "Absent charger is not in the shared held-state map"
        base = _fresh_base()
        with patch("car_charging_plan._observation_clock", side_effect=lambda: base.now_utc), patch("car_charging_execute._clock", side_effect=lambda: base.now_utc):
            _, actuator = _stage_local_candidate(base)
            base.components = SimpleNamespace(get_component=lambda name: native if name == "hypervolt" else None)
            assert execute_car_charging_plan(base) == ("EV blocked", "native_charger_control_unsupported"), failed_restore
            assert not actuator["args"] and base.car_charging_execution is None
            assert _requests(native) == [], "The ownership guard must not send releases"
            if failed_restore:
                assert native.control_state_loaded is False and not native.released_by_predbat
                run_async(native.load_control_state())
            assert native.released_by_predbat == {V3_ID} and native_charger_control_conflict(base)
            # A later successful poll can resume the previously absent charger.
            native.transport.chargers[V3_ID] = _charger(release_state="released", pilot_status="B")
            native.update_car_order([V3_ID])
            run_async(native.control_tick(CONTROL_NOW))
            assert _requests(native) == [(V3_ID, "sync.apply", {"release": False})]
            assert not native.released_by_predbat and not native_charger_control_conflict(base)
            native.control_state_dirty = True
            assert native_charger_control_conflict(base)
            run_async(native.save_control_state())
            assert not native_charger_control_conflict(base)
        print("PASS:", "hypervolt_retained_release", "failed_restore" if failed_restore else "absent_charger")


class _OwnershipStorage:
    """Fail actual shared-controller restore/save calls without network or filesystem access."""

    def __init__(self):
        """Start with a held stop that a failed restore cannot yet expose in memory."""
        self.document = {"held": {"pending": False}}
        self.fail_load = True
        self.fail_save = False

    async def load(self, *args, **kwargs):
        """Return retained ownership or reproduce a recoverable Storage read failure."""
        if self.fail_load:
            raise OSError("Synthetic ownership restore failure")
        return deepcopy(self.document)

    async def save(self, module, key, value, **kwargs):
        """Expose the shared pending-save flag through its actual refused-write path."""
        if self.fail_save:
            return False
        self.document = deepcopy(value)
        return True


def test_shared_controller_persistence_ownership():
    """Real component shared restore/save failures must block dispatch even with control off."""
    from car_charging_execute import native_charger_control_conflict
    from ha import run_async
    from tests.test_ge_cloud import MockGECloudDirect
    from tests.test_myenergi import _make_component
    from tests.test_ohme import MockOhmeAPI

    for name, native in (("gecloud", MockGECloudDirect()), ("myenergi", _make_component(zappi_control=False)), ("ohme", MockOhmeAPI())):
        storage = _OwnershipStorage()
        if name == "gecloud":
            native._mock_storage = storage
        else:
            native.base.components = SimpleNamespace(get_component=lambda key: storage if key == "storage" else None)
        native.charger_control_config = False
        native.charger_control_active = False
        native.evc_control = native.zappi_control = native.ohme_control = False
        assert native.charger_control_storage is not None
        assert run_async(native.charger_control_load_enabled()) is False
        assert not native.charger_control_loaded and not native.charger_control_state
        base = _fresh_base()
        with patch("car_charging_plan._observation_clock", side_effect=lambda: base.now_utc), patch("car_charging_execute._clock", side_effect=lambda: base.now_utc):
            _, actuator = _stage_local_candidate(base)
            base.components = SimpleNamespace(get_component=lambda key: native if key == name else None)
            assert execute_car_charging_plan(base) == ("EV blocked", "native_charger_control_unsupported"), name
            assert not actuator["args"] and base.car_charging_execution is None
            storage.fail_load = False
            assert run_async(native.charger_control_load_enabled()) is True
            assert native.charger_control_state == {"pending": False}
            assert native_charger_control_conflict(base)
            # Exercise the shared persistence transition only, not a claim that
            # a physical device was released by this offline fixture.
            run_async(native.charger_control_forget("pending"))
            assert not native_charger_control_conflict(base)
            storage.fail_save = True
            run_async(native.charger_control_save())
            assert native.charger_control_save_pending and native_charger_control_conflict(base)
            assert not native.charger_control_state
            storage.fail_save = False
            run_async(native.charger_control_save())
            assert not native.charger_control_save_pending and not native_charger_control_conflict(base)
        print("PASS:", name, "shared_restore_and_save_ownership")


def run_car_planning_authority_matrix_tests(base):
    """Run authority sequences on fresh, deterministic in-memory HA fixtures."""
    del base  # Each row gets a genuinely independent Predbat fixture.
    test_permission_connection_authority_matrix()
    test_applied_authority_revocation_matrix()
    test_publication_cannot_restore_authority_after_failure_or_restart()
    test_modes_keep_execution_authority_separate()
    test_target_complete_stop_waits_for_observed_relay_off()
    test_receipt_and_plan_renewal_exact_boundaries()
    test_native_charger_control_ownership()
    test_shared_controller_persistence_ownership()
    test_wallbox_retained_ownership()
    test_hypervolt_retained_ownership()
    return False
