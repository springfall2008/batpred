"""Offline integration acceptance using the real service template, solver and native hooks."""

from tests.car_planning_fixtures import car_settings
from car_charging_config import native_car_arguments

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import yaml

from config import INVERTER_DEF
from inverter import Inverter
from car_charging_plan import evaluate_car_charging
import car_charging_plan
from tests.test_car_planning_native import _controls, _set_entity, _setup


def _template_inputs(base, minutes=60, pv=5):
    """Load the actual profile with synthetic data and a service recorder, never a live transport."""
    _setup(base, pv=pv, minutes=minutes)
    settings = deepcopy(car_settings(base))
    # The small current-command fixture needs all six available ON steps to
    # reach 3 kWh. A free 60-minute deadline could legitimately defer EV ON.
    settings["session"]["departure"] = (base.now_utc + timedelta(minutes=min(minutes, 30))).isoformat()
    template = yaml.safe_load((Path(__file__).resolve().parents[3] / "templates" / "deye_solarman.yaml").read_text())["pred_bat"]
    base.args.clear()
    base.args.update(deepcopy(template))
    base.args.update(
        deye_entity_prefix="combined_test",
        template=False,
        **native_car_arguments(settings),
        soc_max=[32.1536],
        battery_rate_max=[8192],
        inverter_limit_charge=[3000],
        inverter_limit_discharge=[8192],
        inverter_limit=[16000],
        export_limit=[0],
        battery_min_soc=[10],
    )
    base.args_from_apps_yaml = deepcopy(base.args)
    sources = ["select.combined_test_program_{}_charging".format(row) for row in range(1, 7)]
    targets = ["number.combined_test_program_{}_soc".format(row) for row in range(1, 7)]
    for entity in sources:
        _set_entity(base, entity, "Disabled", attributes={"options": ["Disabled", "Grid", "Generator", "Both"]})
    for entity in targets:
        _set_entity(base, entity, 10, attributes={"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"})
    for suffix, value in (("battery", 62.2), ("battery_voltage", 51.2), ("battery_temperature", 20), ("battery_power", 0), ("pv_power", pv * 1000), ("load_power", 1000), ("grid_power", 1000)):
        _set_entity(base, "sensor.combined_test_" + suffix, value)
    base.had_errors = base.fatal_error = base.inverter_needs_reset = False
    base.set_read_only = base.set_read_only_axle = False
    base.set_charge_window = base.set_soc_enable = True
    base.set_export_window = base.set_charge_low_power = False
    base.octopus_intelligent_charging = False
    base.plugin_system = None
    writes = []

    def service(service_name, **data):
        """Model only the allowed physical-row writes in the mock HA cache."""
        entities = data.get("entity_id", "").split(",")
        assert service_name in ("select/select_option", "number/set_value"), service_name
        assert set(entities).issubset(sources + targets) and entities, data
        writes.append((service_name, deepcopy(data)))
        for entity in entities:
            base.ha_interface.dummy_items[entity]["state"] = data.get("option", data.get("value"))
        return True

    base.call_service_wrapper = service
    inverter = Inverter(base, 0, quiet=True)
    base.inverters = [inverter]
    base.fetch_inverter_data()
    assert not writes
    assert inverter.inverter_type == "DeyeSolarman" and not hasattr(inverter, "deye_local")
    assert not base.set_charge_freeze and not inverter.inv_support_charge_freeze
    base.set_discharge_during_charge = True
    base.car_charging_pending = base.car_charging_execution = None
    car_settings(base).update(
        mode="shadow",
        control_model="deye_solarman",
        beam_width=512,
        import_limit_w=6000,
        ev_pause_percent=15,
        ev_restart_percent=25,
        min_off_minutes=10,
        soc_entity="sensor.combined_test_ev_soc",
        soc_entity_unit="kWh",
        relay_entity="binary_sensor.combined_test_relay",
        connection_entity="sensor.combined_test_connection",
        connection_entity_states=["connected"],
        permission_entity="switch.combined_test_permission",
        soc_observed_at_entity="sensor.combined_test_soc_observed_at",
        connection_observed_at_entity="sensor.combined_test_connection_observed_at",
    )
    _set_entity(base, "sensor.combined_test_ev_soc", 0)
    _set_entity(base, "binary_sensor.combined_test_relay", "off", last_changed=base.now_utc - timedelta(seconds=600))
    _set_entity(base, "sensor.combined_test_connection", "connected")
    _set_entity(base, "switch.combined_test_permission", "on")
    _set_entity(base, "sensor.combined_test_soc_observed_at", base.now_utc.isoformat())
    _set_entity(base, "sensor.combined_test_connection_observed_at", base.now_utc.isoformat())
    base.had_errors = base.fatal_error = base.inverter_needs_reset = False
    return inverter, writes, sources, targets


def _stage(base, **kwargs):
    """Attest only within a synthetic fixture; staging itself must never actuate."""
    fixture = _template_inputs(base, **kwargs)
    car_settings(base).update(mode="apply", apply_verified=True, ev_executor_lease_verified=True)
    before = _controls(base)
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate", report
    assert report["counterfactual_controls"] is False
    assert report["execution_enabled"] is False
    assert report["apply_candidate_staged"] is True
    assert _controls(base) == before and not fixture[1]
    assert all(not command["freeze"] and command["power_w"] is None for command in base.car_charging_pending["commands"])
    return fixture


def test_real_template_shadow_is_not_counterfactual(base):
    """The supported-mode search and native replay work without pretending to have freeze."""
    inverter, writes, _, _ = _template_inputs(base)
    before = _controls(base)
    report = evaluate_car_charging(base)
    assert report["status"] == "candidate", report
    assert report["counterfactual_controls"] is False
    assert all(check["passed"] and check["checked_minutes"] == 60 for check in report["native_checks"].values())
    assert not report["proposed_discharge_power_limits"]
    assert not base.set_charge_freeze and not inverter.inv_support_charge_freeze
    assert _controls(base) == before and not writes


def test_real_template_apply_and_lease(base):
    """The real Execute and publication hooks dispatch an observed action, not a DEYE double."""
    inverter, writes, sources, targets = _stage(base)
    assert base.car_charging_pending["commands"][0]["ev_on"]
    with patch.object(inverter, "adjust_charge_immediate", wraps=inverter.adjust_charge_immediate) as native:
        outcome = base.execute_plan()
    assert outcome[0] == "Demand", (outcome, base.car_charging_report)
    native.assert_called_once_with(0, freeze=False)
    assert all(base.ha_interface.get_state(entity) == "Disabled" for entity in sources)
    assert all(float(base.ha_interface.get_state(entity)) == 10 for entity in targets)
    receipt = base.car_charging_execution
    assert receipt and receipt["hardware_confirmed"] is False
    base.publish_car_plan()
    signal = base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]
    assert signal["state"] == "on"
    attributes = signal["attributes"]
    assert attributes["source"] == "predbat" and attributes["lease_version"] == 1
    assert 0 < (datetime.fromisoformat(attributes["valid_until"]) - datetime.fromisoformat(attributes["valid_from"])).total_seconds() <= 300
    with patch("car_charging_plan._observation_clock", return_value=base.now_utc + timedelta(seconds=301)):
        before = len(writes)
        base.publish_car_plan()
    assert len(writes) == before and base.car_charging_execution is None
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"


def test_real_template_guards_and_forged_controls(base):
    """New template support cannot authorise unsupported actions or bypass an independent gate."""
    for guard in ("hold", "power", "future_hold", "permission", "connection", "stale_soc", "stale_connection", "read_only", "commissioning", "lease", "cooldown", "manual_floor", "mapping", "freeze_service"):
        _, writes, _, _ = _stage(base)
        if guard in ("hold", "future_hold"):
            base.car_charging_pending["commands"][0 if guard == "hold" else -1]["freeze"] = True
        elif guard == "power":
            base.car_charging_pending["commands"][0]["power_w"] = 1000
        elif guard == "permission":
            _set_entity(base, "switch.combined_test_permission", "off")
        elif guard == "connection":
            _set_entity(base, "sensor.combined_test_connection", "disconnected")
        elif guard in ("stale_soc", "stale_connection"):
            field = "soc" if guard == "stale_soc" else "connection"
            _set_entity(base, "sensor.combined_test_" + field + "_observed_at", (base.now_utc - timedelta(seconds=181)).isoformat())
        elif guard == "read_only":
            base.set_read_only = True
        elif guard == "commissioning":
            car_settings(base)["apply_verified"] = False
        elif guard == "lease":
            car_settings(base)["ev_executor_lease_verified"] = False
        elif guard == "cooldown":
            _set_entity(base, "binary_sensor.combined_test_relay", "off", last_changed=base.now_utc - timedelta(seconds=599))
        elif guard == "manual_floor":
            base.all_active_keep[base.minutes_now] = 90
        elif guard == "freeze_service":
            base.args["charge_freeze_service"] = [{"service": "switch.turn_on", "entity_id": "switch.unsafe_control"}]
        else:
            base.args["charge_stop_service"][0]["entity_id"] += ",select.other_inverter_program_1_charging"
        outcome = base.execute_plan()
        assert outcome[0] == "EV blocked", (guard, outcome)
        assert not writes, guard
        assert not base.car_charging_execution and not any(base.car_charging_slots), guard


def test_real_template_failed_write_and_drift(base):
    """Unobserved/partial commands revoke EV permission, as does later cache drift."""
    inverter, writes, sources, targets = _stage(base)
    # A demand command has to change all six sources/targets before an EV lease is possible.
    for entity in sources:
        base.ha_interface.dummy_items[entity]["state"] = "Grid"
    for entity in targets:
        base.ha_interface.dummy_items[entity]["state"] = 100
    inverter.write_and_poll_sleep = 0
    with patch.object(base, "call_service_wrapper", return_value=False):
        outcome = base.execute_plan()
    assert outcome[0] == "EV blocked", outcome
    assert base.car_charging_execution is None and not any(base.car_charging_slots)
    assert not writes
    inverter, writes, sources, targets = _stage(base)
    assert base.execute_plan()[0] == "Demand"
    base.ha_interface.dummy_items[targets[-1]]["state"] = 11
    before = len(writes)
    base.publish_car_plan()
    assert len(writes) == before and base.car_charging_execution is None
    assert base.dashboard_values["binary_sensor." + base.prefix + "_car_charging_slot"]["state"] == "off"


def test_real_template_charge_then_house_only(base):
    """A closed session can replenish the house but never renew EV permission."""
    inverter, writes, sources, targets = _template_inputs(base, pv=0)
    base.soc_kw = base.reserve + 0.05
    session = car_settings(base)["session"]
    session.update(available_from=(base.now_utc - timedelta(hours=2)).isoformat(), departure=(base.now_utc - timedelta(minutes=5)).isoformat())
    original = deepcopy(session)
    car_settings(base).update(mode="apply", apply_verified=True, ev_executor_lease_verified=True, terminal_min_percent=15)
    base.all_active_keep[base.minutes_now + 5] = base.soc_kw / base.soc_max * 100 + 0.25
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate", report
    assert report["session_closed"] and not report["proposed_ev_windows"]
    assert not any(command["ev_on"] for command in base.car_charging_pending["commands"])
    assert base.car_charging_pending["commands"][0]["target_soc"] == 100
    _set_entity(base, "binary_sensor.combined_test_relay", "on", last_changed=base.now_utc - timedelta(minutes=10))
    assert base.execute_plan() == ("EV blocked", "waiting_for_ev_relay_off")
    assert not writes
    base.had_errors = False
    _set_entity(base, "binary_sensor.combined_test_relay", "off", last_changed=base.now_utc - timedelta(minutes=10))
    outcome = base.execute_plan()
    assert outcome[0] == "Charging", (outcome, base.car_charging_report)
    assert all(base.ha_interface.get_state(entity) == "Grid" for entity in sources)
    assert all(float(base.ha_interface.get_state(entity)) == 100 for entity in targets)
    # The existing verified sequence skips the already-observed Disabled phase.
    assert [name for name, _ in writes] == ["number/set_value", "select/select_option"]
    assert not any(base.car_charging_slots) and session == original


def test_real_template_full_horizon(base):
    """The real demand/charge-only model meets a dated EV target and both 48-hour checks."""
    inverter, writes, _, _ = _template_inputs(base, minutes=2880, pv=0)
    base.car_charging_limit[0] = base.car_charging_battery_size[0] = 25
    base.car_charging_soc[0] = 3.25
    _set_entity(base, "sensor.combined_test_ev_soc", 3.25)
    departure = (base.now_utc + timedelta(days=1)).replace(hour=7, minute=30, second=0, microsecond=0)
    car_settings(base)["session"].update(departure=departure.isoformat(), soc_kwh=3.25)
    car_settings(base)["beam_width"] = 128
    before = _controls(base)
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate", report
    assert report["counterfactual_controls"] is False
    assert report["proposed_ev_windows"]
    assert not report["proposed_discharge_power_limits"]
    assert all(check["passed"] and check["checked_minutes"] == 2880 for check in report["native_checks"].values())
    assert all(check["peak_import_w"] <= 6000 and check["final_battery_kwh"] >= base.reserve - 0.002 for check in report["native_checks"].values())
    assert not writes and _controls(base) == before
    assert not base.set_charge_freeze and not inverter.inv_support_charge_freeze
    assert report["search_beam_attempts"][0]["beam_width"] == 128
    print("Combined 48-hour template report:", report["native_checks"])


def test_real_template_changes_during_dispatch(base):
    """Permission lost while a real sequence settles prevents a lease despite final read-back."""
    _, writes, sources, targets = _stage(base)
    for entity in sources:
        base.ha_interface.dummy_items[entity]["state"] = "Grid"
    for entity in targets:
        base.ha_interface.dummy_items[entity]["state"] = 100
    service = base.call_service_wrapper

    def lose_permission(service_name, **data):
        """Use the mock service then mutate only the independent evidence cache."""
        observed = service(service_name, **data)
        _set_entity(base, "switch.combined_test_permission", "off")
        return observed

    with patch.object(base, "call_service_wrapper", side_effect=lose_permission):
        outcome = base.execute_plan()
    assert outcome == ("EV blocked", "permission_not_granted"), outcome
    assert writes and base.car_charging_execution is None and not any(base.car_charging_slots)
    assert base.car_charging_report["hardware_confirmed"] is False


def test_real_template_manual_floor_and_no_plan(base):
    """Manual minima survive supported-mode replay; energy shortage cannot invent hold or EV ON."""
    _, writes, _, _ = _template_inputs(base)
    for minute in range(base.minutes_now, base.minutes_now + 60):
        base.all_active_keep[minute] = 50
    before = deepcopy(base.all_active_keep)
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate", report
    assert all(check["minimum_battery_kwh"] >= base.soc_max * 0.50 - 0.002 for check in report["native_checks"].values())
    assert base.all_active_keep == before and not writes
    _, writes, _, _ = _template_inputs(base, pv=0)
    base.soc_kw = base.reserve
    car_settings(base).update(mode="apply", apply_verified=True, ev_executor_lease_verified=True)
    report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "no_plan_found", report
    assert not base.car_charging_pending
    assert base.execute_plan()[0] == "EV blocked"
    assert not writes and not any(base.car_charging_slots)


def test_real_template_solver_retry_and_rejected_modes(base):
    """A reported bounded retry preserves capabilities; a forged solver hold cannot reach replay."""
    _, writes, _, _ = _template_inputs(base)
    car_settings(base)["beam_width"] = 128
    build = car_charging_plan.build_candidate
    widths = []

    def bounded_miss_then_actual(model, options):
        """Create one artificial search-budget miss, then use the real solver unchanged."""
        widths.append(options["beam"])
        result, slots = build(model, options)
        return (replace(result, actions=(), status="bounded_no_plan"), slots) if len(widths) == 1 else (result, slots)

    with patch.object(car_charging_plan, "build_candidate", side_effect=bounded_miss_then_actual):
        report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "candidate", report
    assert widths == [128, 512]
    assert [attempt["beam_width"] for attempt in report["search_beam_attempts"]] == widths
    assert report["supported_battery_modes"] == ["demand", "charge"] and not writes
    car_settings(base).update(mode="apply", apply_verified=True, ev_executor_lease_verified=True)

    def unsupported_hold(model, options):
        """Simulate an internal result regression; capability validation must catch it independently."""
        result, slots = build(model, options)
        actions = (replace(result.actions[0], battery_mode="hold"),) + result.actions[1:]
        return replace(result, actions=actions), slots

    with patch.object(car_charging_plan, "build_candidate", side_effect=unsupported_hold), patch.object(car_charging_plan, "_replay", side_effect=AssertionError("Unsupported actions reached replay")):
        report = evaluate_car_charging(base, publish=False)
    assert report["status"] == "unsupported" and report["reason"] == "unsupported_template_candidate_controls", report
    assert not base.car_charging_pending and not writes


def test_real_template_model_and_native_capability_mismatch(base):
    """Model rate/capacity drift and an unexpected native writer fail before any service."""
    from inverter_service_control import dispatch_template

    for field in (
        "battery_rate_max_charge",
        "battery_rate_max_charge_dc",
        "battery_rate_max_discharge",
        "inverter_limit",
        "export_limit",
        "soc_max",
        "reserve",
        "battery_rate_max_scaling",
        "battery_rate_max_scaling_discharge",
        "inv_has_target_soc",
        "inv_has_solis_energy_control",
        "inv_has_reserve_soc",
    ):
        inverter, writes, _, _ = _stage(base)
        if field.startswith("inv_"):
            setattr(inverter, field, True)
        else:
            setattr(base, field, getattr(base, field) + 0.01)
        assert base.execute_plan()[0] == "EV blocked", field
        assert not writes and not base.car_charging_execution, field
    inverter, writes, _, _ = _stage(base)
    for target in (True, False, 0.0, 100.0, "100"):
        try:
            dispatch_template(inverter, target)
        except ValueError:
            pass
        else:
            raise AssertionError("Malformed template target accepted: {!r}".format(target))
        assert not writes


def run_car_planning_combined_tests(base):
    """Run each cross-feature regression on a fresh native/mock instance."""
    from unit_test import create_predbat
    from tests.test_car_planning_template import shared_template_available, check_unavailable_template

    if not shared_template_available():
        check_unavailable_template(create_predbat())
        return False

    tests = (
        test_real_template_shadow_is_not_counterfactual,
        test_real_template_apply_and_lease,
        test_real_template_guards_and_forged_controls,
        test_real_template_failed_write_and_drift,
        test_real_template_charge_then_house_only,
        test_real_template_full_horizon,
        test_real_template_changes_during_dispatch,
        test_real_template_manual_floor_and_no_plan,
        test_real_template_solver_retry_and_rejected_modes,
        test_real_template_model_and_native_capability_mismatch,
    )
    with patch.dict(INVERTER_DEF, deepcopy(INVERTER_DEF)):
        for test in tests:
            isolated = create_predbat()
            with patch("car_charging_plan._observation_clock", side_effect=lambda: isolated.now_utc):
                test(isolated)
            print("PASS:", test.__name__)
    return False
