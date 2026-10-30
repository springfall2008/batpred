"""Real-template acceptance for bounded charge ceilings and confirmed unchanged EV sessions."""

from tests.car_planning_fixtures import car_settings

from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from car_charging_execute import execute_car_charging_plan, publish_car_charging_slot, refresh_car_charging_lease
from car_charging_plan import _options, _continuous_grid_support_shadow, evaluate_car_charging
from car_charging_observation import confirmed_session_observation
from tests.test_car_planning_combined import _template_inputs
from tests.test_car_planning_native import _set_entity as _cached_entity


def _set_entity(base, entity, state, attributes=None, **kwargs):
    """Keep both native mock attribute lookups and raw HA payload attributes available."""
    _cached_entity(base, entity, state, attributes={**(attributes or {}), "attributes": attributes or {}}, **kwargs)


def _actual_inputs(base):
    """Build real native controls with an explicit confirmation and old but valid retained states."""
    inverter, writes, sources, targets = _template_inputs(base, pv=0)
    base.soc_kw = base.soc_max * 0.26
    inverter.soc_kw, inverter.soc_percent = base.soc_kw, 26
    base.all_active_keep = {base.minutes_now + minute: 25 for minute in range(5)}
    settings = car_settings(base)
    settings["session"]["available_from"] = (base.now_utc - timedelta(hours=1)).isoformat()
    settings.update(
        mode="apply",
        apply_verified=True,
        ev_executor_lease_verified=True,
        vehicle_state_validity={"attribute": "retrievalstatus", "valid_states": ["VALID"]},
        grid_charge_support={"enabled_entity": "switch.test_support", "limit_entity": "number.test_support"},
        confirmed_session={
            "confirmed_at": (base.now_utc - timedelta(minutes=20)).isoformat(),
            "departure": settings["session"]["departure"],
            "soc_kwh": 0,
            "energy_entity": "sensor.test_ev_energy",
            "energy_kwh": 100,
            "power_entity": "sensor.test_ev_power",
        },
    )
    _set_entity(base, "switch.test_support", "on")
    _set_entity(base, "number.test_support", "6000")
    for key, value in (("soc_entity", 0), ("connection_entity", "connected")):
        _set_entity(base, settings[key], value, last_updated=base.now_utc - timedelta(hours=12), attributes={"retrievalstatus": "VALID"})
    _set_entity(base, "sensor.test_ev_energy", "100", last_updated=base.now_utc - timedelta(hours=12), attributes={"unit_of_measurement": "kWh"})
    _set_entity(base, "sensor.test_ev_power", "0", attributes={"unit_of_measurement": "kW"})
    return inverter, writes, sources, targets


def test_confirmed_unchanged_soc_and_meter(base):
    """Unchanged valid values retain their real timestamps and delivery advances only from the meter."""
    _actual_inputs(base)
    settings = car_settings(base)
    original = deepcopy(base.ha_interface.dummy_items)
    value = confirmed_session_observation(base, settings, base.now_utc)
    assert value["ev_soc"] == 0 and value["ev_soc_is_estimate"] and value["soc_observation_age_minutes"] == 720
    assert value["ev_soc_source"] == "operator_confirmed_metered_session" and base.ha_interface.dummy_items == original
    _set_entity(base, "sensor.test_ev_energy", "101", attributes={"unit_of_measurement": "kWh"})
    value = confirmed_session_observation(base, settings, base.now_utc)
    assert value["ev_metered_ac_kwh"] == 1 and abs(value["ev_soc"] - base.car_charging_loss) < 1e-10
    _set_entity(base, settings["soc_entity"], 1.5, attributes={"retrievalstatus": "VALID"})
    assert confirmed_session_observation(base, settings, base.now_utc)["ev_soc"] == 1.5
    assert _options(base)["entity_blockers"] == []


def test_confirmation_fails_closed_without_fabricating_clock(base):
    """A reset meter, invalid field or changed deadline cannot silently reuse the confirmation."""
    for fault in ("meter_reset", "missing_meter", "bad_soc", "invalid_status", "future", "renewed_deadline", "energy_stalled"):
        _actual_inputs(base)
        settings = car_settings(base)
        if fault == "meter_reset":
            _set_entity(base, "sensor.test_ev_energy", "99", attributes={"unit_of_measurement": "kWh"})
        elif fault == "missing_meter":
            base.ha_interface.dummy_items.pop("sensor.test_ev_energy")
        elif fault == "bad_soc":
            _set_entity(base, settings["soc_entity"], "unavailable", attributes={"retrievalstatus": "VALID"})
        elif fault == "invalid_status":
            _set_entity(base, settings["soc_entity"], 0, attributes={"retrievalstatus": "INVALID"})
        elif fault == "future":
            settings["confirmed_session"]["confirmed_at"] = (base.now_utc + timedelta(seconds=1)).isoformat()
        elif fault == "renewed_deadline":
            settings["session"]["departure"] = (base.now_utc + timedelta(hours=2)).isoformat()
        else:
            _set_entity(base, settings["relay_entity"], "on", last_changed=base.now_utc - timedelta(minutes=11))
            _set_entity(base, "sensor.test_ev_power", "6.77", attributes={"unit_of_measurement": "kW"})
        try:
            confirmed_session_observation(base, settings, base.now_utc)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid confirmation accepted: " + fault)


def test_actual_charge_ceiling_matches_replay_and_lease(base):
    """The real executor writes the replayed integer ceiling, never replaces it with 100 percent."""
    inverter, writes, sources, targets = _actual_inputs(base)
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate", report
    assert all(check["passed"] for check in report["native_checks"].values())
    assert not writes and not report["execution_enabled"]
    command = deepcopy(base.car_charging_pending["commands"][0])
    assert command["ev_on"] and 10 < command["target_soc"] < 100, command
    assert execute_car_charging_plan(base)[0] == "Charging", base.car_charging_report
    assert all(base.ha_interface.get_state(entity) == "Grid" for entity in sources)
    assert all(float(base.ha_interface.get_state(entity)) == command["target_soc"] for entity in targets)
    assert base.car_charging_report["status"] == "applied" and base.car_charging_report["execution_enabled"]
    assert not any(service.startswith("switch") for service, _ in writes)
    signal = base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]
    assert signal["state"] == "on" and signal["attributes"]["source"] == "predbat"
    count = len(writes)
    with patch("car_charging_plan._observation_clock", return_value=base.now_utc + timedelta(seconds=301)):
        publish_car_charging_slot(base)
    assert base.car_charging_execution is None and len(writes) == count
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"


def test_actual_independent_gates_and_cap_drift(base):
    """Operator evidence never overrides read-only, permission, reserve or a changed support setting."""
    for fault in ("read_only", "permission", "support_disabled", "target", "commissioning", "lease", "connection"):
        _, writes, _, _ = _actual_inputs(base)
        report = evaluate_car_charging(base, publish=False)
        assert report["status"] == "candidate"
        settings = car_settings(base)
        if fault == "read_only":
            base.set_read_only = True
        elif fault == "permission":
            _set_entity(base, settings["permission_entity"], "off")
        elif fault == "support_disabled":
            _set_entity(base, "switch.test_support", "off")
        elif fault == "target":
            base.car_charging_pending["commands"][0]["target_soc"] = 101
        elif fault == "commissioning":
            settings["apply_verified"] = False
        elif fault == "lease":
            settings["ev_executor_lease_verified"] = False
        else:
            _set_entity(base, settings["connection_entity"], "disconnected", attributes={"retrievalstatus": "VALID"})
        assert execute_car_charging_plan(base)[0] == "EV blocked" and not writes
        assert not base.car_charging_execution


def test_confirmed_session_progress_and_failures(base):
    """Repeated unchanged SOC cannot freeze progress; invalid meters and clocks fail closed."""
    _actual_inputs(base)
    settings = car_settings(base)
    _set_entity(base, "sensor.test_ev_energy", "101", attributes={"unit_of_measurement": "kWh"})
    _set_entity(base, settings["soc_entity"], 1.5, attributes={"retrievalstatus": "VALID"})
    assert confirmed_session_observation(base, settings, base.now_utc)["ev_soc"] == 1.5
    later = base.now_utc + timedelta(minutes=5)
    _set_entity(base, "sensor.test_ev_energy", "102", last_updated=later, attributes={"unit_of_measurement": "kWh"})
    first = confirmed_session_observation(base, settings, later)
    assert first["ev_soc"] == 2 * base.car_charging_loss and first["ev_soc_is_estimate"]
    assert confirmed_session_observation(base, deepcopy(settings), later) == first
    for fault in ("zero_power_stall", "future_transition", "nan", "bool", "huge_power", "fast_counter", "attributes", "before_arrival", "soc_contradiction"):
        _actual_inputs(base)
        settings = car_settings(base)
        if fault == "zero_power_stall":
            _set_entity(base, settings["relay_entity"], "on", last_changed=base.now_utc - timedelta(minutes=11))
        elif fault == "future_transition":
            _set_entity(base, settings["relay_entity"], "off", last_changed=base.now_utc + timedelta(seconds=1))
        elif fault in ("nan", "bool"):
            _set_entity(base, "sensor.test_ev_energy", float("nan") if fault == "nan" else True, attributes={"unit_of_measurement": "kWh"})
        elif fault == "huge_power":
            _set_entity(base, "sensor.test_ev_power", "30", attributes={"unit_of_measurement": "kW"})
        elif fault == "fast_counter":
            _set_entity(base, "sensor.test_ev_energy", "105", attributes={"unit_of_measurement": "kWh"})
        elif fault == "attributes":
            base.ha_interface.dummy_items["sensor.test_ev_energy"]["attributes"] = None
        elif fault == "before_arrival":
            settings["confirmed_session"]["confirmed_at"] = (base.now_utc - timedelta(hours=2)).isoformat()
        else:
            _set_entity(base, settings["soc_entity"], 9, attributes={"retrievalstatus": "VALID"})
        try:
            confirmed_session_observation(base, settings, base.now_utc)
        except ValueError:
            pass
        else:
            raise AssertionError("Invalid confirmation accepted: " + fault)


def test_confirmed_session_closes_without_renewal(base):
    """After departure, missing EV telemetry cannot block household planning or renew EV charging."""
    _, writes, _, _ = _actual_inputs(base)
    settings = car_settings(base)
    settings["session"]["departure"] = settings["confirmed_session"]["departure"] = (base.now_utc - timedelta(minutes=1)).isoformat()
    for name in ("soc_entity", "connection_entity"):
        base.ha_interface.dummy_items.pop(settings[name])
    base.ha_interface.dummy_items.pop("sensor.test_ev_energy")
    options = _options(base)
    assert options["house_only"] and options["confirmed_observation"]["session_closed"]
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate" and report["observed_ev_target_met"] is None, report
    assert not report["proposed_ev_windows"] and report["target_met_by_departure"] is None
    assert not any(command["ev_on"] for command in base.car_charging_pending["commands"])
    assert execute_car_charging_plan(base)[0] != "EV blocked", base.car_charging_report
    assert not any(service.startswith("switch") for service, _ in writes)


def test_actual_partial_dispatch_retry_after_restart(base):
    """Partial native writes revoke EV authority; restart-like state loss requires full revalidation."""
    for fail_phase in range(3):
        inverter, writes, sources, targets = _actual_inputs(base)
        assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
        target = base.car_charging_pending["commands"][0]["target_soc"]
        for entity in sources:
            _set_entity(base, entity, "Grid", attributes={"options": ["Disabled", "Grid", "Gen", "Grid+Gen"]})
        original = base.call_service_wrapper
        calls = []

        def failing(service, **data):
            """Interrupt one actual service phase while retaining partial cached changes."""
            calls.append(service)
            return False if len(calls) == fail_phase + 1 else original(service, **data)

        base.call_service_wrapper = failing
        assert execute_car_charging_plan(base)[0] == "EV blocked"
        assert not base.car_charging_execution
        assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"
        base.call_service_wrapper = original
        base.had_errors = False
        base.car_charging_pending = None
        assert execute_car_charging_plan(base)[0] == "EV blocked"
        base.had_errors = False
        assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
        assert execute_car_charging_plan(base)[0] == "Charging", base.car_charging_report
        assert all(float(base.ha_interface.get_state(entity)) == target for entity in targets)
        assert all(base.ha_interface.get_state(entity) == "Grid" for entity in sources)
        assert not any(service.startswith("switch") for service, _ in writes)


def test_actual_lease_renewal_is_bounded_and_does_not_dispatch(base):
    """Cached renewal bridges calculation jitter but never extends an old plan indefinitely."""
    _, writes, _, _ = _actual_inputs(base)
    base.all_active_keep = {}
    base.soc_kw = base.soc_max * 0.8
    base.inverters[0].soc_kw, base.inverters[0].soc_percent = base.soc_kw, 80
    assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
    assert execute_car_charging_plan(base)[0] != "EV blocked", base.car_charging_report
    initial = deepcopy(base.car_charging_execution)
    count = len(writes)
    # Keep a fully accepted current command across consecutive equal five-minute steps.
    assert initial["command"]["end"] > base.minutes_now + 10
    for seconds in (240, 480):
        with patch("car_charging_plan._observation_clock", return_value=base.now_utc + timedelta(seconds=seconds)):
            refresh_car_charging_lease(base)
        receipt = base.car_charging_execution
        assert receipt and receipt["plan_generated_at"] == initial["plan_generated_at"]
        assert len(writes) == count
        from car_charging_view import build_car_charging_plan_view

        assert build_car_charging_plan_view({}, base.car_charging_report, now=base.now_utc + timedelta(seconds=seconds))["plan_execution"] is True
    with patch("car_charging_plan._observation_clock", return_value=base.now_utc + timedelta(seconds=600)):
        refresh_car_charging_lease(base)
    assert base.car_charging_execution is None and len(writes) == count
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"


def test_explicit_session_authority_is_not_hardware_attestation(base):
    """Only the explicit confirmed shared-support scope may run with an unverified attestation."""
    for confirmed in (False, True):
        _, writes, _, _ = _actual_inputs(base)
        settings = car_settings(base)
        settings.update(apply_verified=False, execution_authorised=True)
        if not confirmed:
            settings.pop("confirmed_session")
            assert execute_car_charging_plan(base)[0] == "EV blocked" and not writes
        else:
            assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
            assert execute_car_charging_plan(base)[0] == "Charging", base.car_charging_report
            assert settings["apply_verified"] is False and base.car_charging_report["hardware_confirmed"] is False


def test_dispatch_replays_soc_drop_and_binds_exact_commands(base):
    """A later SOC observation requires real paired replay; forged in-range targets fail closed."""
    for case in ("small_drop", "unsafe_drop", "forged_target"):
        _, writes, _, _ = _actual_inputs(base)
        assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
        expected = deepcopy(base.car_charging_pending["commands"])
        if case == "forged_target":
            base.car_charging_pending["commands"][0]["target_soc"] += 1
        else:
            base.soc_kw -= 0.01 if case == "small_drop" else base.soc_max * 0.08
        status = execute_car_charging_plan(base)
        if case == "small_drop":
            assert status[0] == "Charging", base.car_charging_report
            assert base.car_charging_pending["commands"] == expected
            assert base.car_charging_report["dispatch_soc_revalidated_kwh"] == base.soc_kw
            assert all(check["passed"] for check in base.car_charging_report["native_checks"].values())
        else:
            assert status[0] == "EV blocked" and not writes


def test_operator_stop_acknowledgement_is_one_observation_only(base):
    """An acknowledged manual-stop error never permits new errors or weakens independent guards."""
    for case in ("accepted", "absent", "later_error", "other_error", "future", "wrong_departure", "read_only", "permission", "meter_reset"):
        _, writes, _, _ = _actual_inputs(base)
        settings = car_settings(base)
        observed = base.now_utc - timedelta(minutes=10)
        acknowledgement = {"state": "4", "observed_at": observed.isoformat(), "acknowledged_at": base.now_utc.isoformat(), "departure": settings["session"]["departure"]}
        settings["connection_error_acknowledgement"] = acknowledgement
        _set_entity(base, settings["connection_entity"], "4", last_updated=observed, attributes={"retrievalstatus": "VALID"})
        if case == "absent":
            settings.pop("connection_error_acknowledgement")
        elif case == "later_error":
            _set_entity(base, settings["connection_entity"], "4", last_updated=base.now_utc, attributes={"retrievalstatus": "VALID"})
        elif case == "other_error":
            _set_entity(base, settings["connection_entity"], "16", last_updated=observed, attributes={"retrievalstatus": "VALID"})
        elif case == "future":
            acknowledgement["acknowledged_at"] = (base.now_utc + timedelta(seconds=1)).isoformat()
        elif case == "wrong_departure":
            acknowledgement["departure"] = (base.now_utc + timedelta(days=1)).isoformat()
        assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
        if case == "read_only":
            base.set_read_only = True
        elif case == "permission":
            _set_entity(base, settings["permission_entity"], "off")
        elif case == "meter_reset":
            _set_entity(base, "sensor.test_ev_energy", "99", attributes={"unit_of_measurement": "kWh"})
        status = execute_car_charging_plan(base)
        if case == "accepted":
            assert status[0] == "Charging", base.car_charging_report
            assert base.car_charging_execution["connection_error_acknowledged"] is True
            assert "4" not in settings["connection_entity_states"]
        else:
            assert status[0] == "EV blocked" and not writes, (case, base.car_charging_report)


def test_native_readonly_switch_transition_requires_observed_dispatch(base):
    """Fulfil only the native read-only reset request using the verified combined rows, not a flag bypass."""
    for case in ("accepted", "failed_write", "other_reset", "relay_on"):
        _, writes, _, _ = _actual_inputs(base)
        base.expose_config("set_read_only", True, event=True)
        base.expose_config("set_read_only", False, event=True)
        assert base.inverter_needs_reset is True and base.inverter_needs_reset_force == "set_read_only"
        base.set_read_only = False
        assert evaluate_car_charging(base, publish=False)["status"] == "candidate"
        if case == "failed_write":
            base.call_service_wrapper = lambda *a, **k: False
        elif case == "other_reset":
            base.inverter_needs_reset_force = "mode"
        elif case == "relay_on":
            _set_entity(base, car_settings(base)["relay_entity"], "on")
        status = execute_car_charging_plan(base)
        if case == "accepted":
            assert status[0] == "Charging", base.car_charging_report
            assert base.inverter_needs_reset is False and base.inverter_needs_reset_force == ""
            assert base.car_charging_execution["read_only_transition_observed"] is True
            assert all(service in ("number/set_value", "select/select_option") and "_program_" in data["entity_id"] for service, data in writes)
        else:
            assert status[0] == "EV blocked" and base.inverter_needs_reset is True
            assert base.car_charging_execution is None


def test_asap_prefers_earlier_paired_block_without_changing_session(base):
    """ASAP takes the next cheaper band before the cheapest later band without changing consent."""
    _actual_inputs(base)
    settings = car_settings(base)
    settings["session"]["departure"] = settings["confirmed_session"]["departure"] = (base.now_utc + timedelta(minutes=60)).isoformat()
    base.soc_kw = base.soc_max * 0.8
    base.inverters[0].soc_kw, base.inverters[0].soc_percent = base.soc_kw, 80
    base.metric_battery_value_scaling = 0
    base.rate_import.update({base.minutes_now + minute: 50 if minute < 15 else 10 if minute < 30 else 1 for minute in range(60)})
    session = deepcopy(settings["session"])
    confirmation = deepcopy(settings["confirmed_session"])
    ordinary = evaluate_car_charging(base, publish=False)
    assert ordinary["status"] == "candidate" and ordinary["proposed_ev_windows"][0]["start"] > base.minutes_now
    settings["ev_charging_priority"] = "asap"
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate", report
    assert report["ev_charging_priority"] == "asap" and report["proposed_ev_windows"][0]["start"] == base.minutes_now + 15
    assert report["proposed_ev_windows"][0]["start"] < ordinary["proposed_ev_windows"][0]["start"]
    assert report["priority_selection_reason"] == "next_cheaper_band"
    assert len(report["continuous_block_attempts"]) == 1 and all(check["passed"] for check in report["native_checks"].values())
    assert all(left["end"] == right["start"] for left, right in zip(report["proposed_ev_windows"], report["proposed_ev_windows"][1:]))
    assert settings["session"] == session and settings["confirmed_session"] == confirmation
    assert not base.car_charging_pending["commands"][0]["ev_on"]
    assert execute_car_charging_plan(base)[0] != "EV blocked", base.car_charging_report
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"
    # A later priority edit invalidates the old receipt instead of silently reusing it.
    settings["ev_charging_priority"] = "cost"
    publish_car_charging_slot(base)
    assert base.car_charging_execution is None


def test_asap_respects_cooldown_and_both_forecasts(base):
    """ASAP cannot bypass cooldown, a failed replay, scope validation or the twelve-search bound."""
    _actual_inputs(base)
    settings = car_settings(base)
    settings.update(ev_charging_priority="asap")
    settings["session"]["departure"] = settings["confirmed_session"]["departure"] = (base.now_utc + timedelta(minutes=60)).isoformat()
    _set_entity(base, settings["relay_entity"], "off", last_changed=base.now_utc - timedelta(minutes=5))
    options = _options(base)
    chosen, attempts = _continuous_grid_support_shadow(base, options)
    assert chosen and attempts[0]["start_offset"] == 5 and chosen[2][0][0]["start"] >= base.minutes_now + 5
    with patch("car_charging_plan._replay_candidate", return_value=(chosen[2], {"nominal": {"passed": True}, "pv10": {"passed": False}})):
        rejected, failed = _continuous_grid_support_shadow(base, options)
    assert rejected is None and 1 <= len(failed) <= 12 and all(not attempt["native_passed"] for attempt in failed)
    assert [a["start_offset"] for a in failed] == sorted(a["start_offset"] for a in failed)
    assert _continuous_grid_support_shadow(base, {**options, "house_only": True}) == (None, [])
    for value in (None, True, "ASAP", "fastest"):
        settings["ev_charging_priority"] = value
        assert evaluate_car_charging(base, publish=False)["status"] == "invalid_input"
    settings["ev_charging_priority"] = "asap"
    settings.pop("grid_charge_support")
    assert evaluate_car_charging(base, publish=False)["status"] == "invalid_input"


def run_car_planning_actual_tests(base):
    """Run isolated actual-plan tests with a deterministic observation clock."""
    from tests.test_car_planning_template import shared_template_available, check_unavailable_template

    if not shared_template_available():
        check_unavailable_template(base)
        return False

    with patch("car_charging_plan._observation_clock", side_effect=lambda: base.now_utc):
        for test in (
            test_confirmed_unchanged_soc_and_meter,
            test_confirmation_fails_closed_without_fabricating_clock,
            test_actual_charge_ceiling_matches_replay_and_lease,
            test_actual_independent_gates_and_cap_drift,
            test_confirmed_session_progress_and_failures,
            test_confirmed_session_closes_without_renewal,
            test_actual_partial_dispatch_retry_after_restart,
            test_actual_lease_renewal_is_bounded_and_does_not_dispatch,
            test_explicit_session_authority_is_not_hardware_attestation,
            test_dispatch_replays_soc_drop_and_binds_exact_commands,
            test_operator_stop_acknowledgement_is_one_observation_only,
            test_native_readonly_switch_transition_requires_observed_dispatch,
            test_asap_prefers_earlier_paired_block_without_changing_session,
            test_asap_respects_cooldown_and_both_forecasts,
        ):
            test(base)
    return False
