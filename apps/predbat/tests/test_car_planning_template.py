# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Bounded tests for the inverter service adapter (charge/demand only).

The adapter is a thin wrapper over the **actual native** immediate entrypoint
``Inverter.adjust_charge_immediate`` and the verified-attribute preflight
``Inverter.prepare_service_sequence``. The tests exercise the three public
functions directly without the parent ``car_charging_execute`` plumbing; the
parent owns commissioning, EV lease, SOC/connection freshness, the 300 s
lease TTL and the EV external executor.
"""

from tests.car_planning_fixtures import car_settings

import copy
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from config import INVERTER_DEF
from inverter import Inverter
from inverter_service_control import ServiceSequenceError
from tests.test_infra import reset_inverter


SIX_PROGRAM_RANGE = range(1, 7)


def shared_template_available():
    """Identify the optional real fixture and verified core without substituting a fake adapter."""
    template = Path(__file__).resolve().parents[3] / "templates" / "deye_solarman.yaml"
    return template.is_file() and callable(getattr(Inverter, "prepare_service_sequence", None))


def check_unavailable_template(base):
    """The independent combined PR must import and fail closed without the optional template core."""
    from car_charging_plan import evaluate_car_charging
    from inverter_service_control import validate_template
    from tests.test_car_planning_native import _setup

    if os.environ.get("PREDBAT_EV_TEMPLATE_REQUIRED") == "1":
        raise AssertionError("Real shared-template integration is required but its template/core is missing")
    _setup(base, pv=5)
    car_settings(base)["control_model"] = "deye_solarman"
    base.set_charge_freeze = False
    base.set_discharge_during_charge = True
    base.export_limit = 0
    missing = SimpleNamespace(inverter_type="DeyeSolarman")
    base.inverters = [missing]
    with patch("car_charging_plan._observation_clock", return_value=base.now_utc), patch.object(base, "call_service_wrapper", side_effect=AssertionError("Missing core attempted a service")):
        try:
            validate_template(missing)
        except ValueError:
            pass
        else:
            raise AssertionError("Synthetic inverter was accepted without verified template core")
        report = evaluate_car_charging(base, publish=False)
        assert report["status"] == "unsupported" and "template_contract_invalid" in report["execution_blockers"], report
        car_settings(base)["mode"] = "apply"
        assert base.execute_plan()[0] == "EV blocked"
        assert not getattr(base, "car_charging_pending", None) and not getattr(base, "car_charging_execution", None)
        assert not any(base.car_charging_slots)
    print("PASS: missing service-sequence core fails closed; SKIP: optional integration requires the service adapter")


def _expected_source_entities(prefix):
    """Return the six canonical source selects for a synthetic inverter prefix."""
    return ["select.{}_program_{}_charging".format(prefix, row) for row in SIX_PROGRAM_RANGE]


def _expected_target_entities(prefix):
    """Return the six canonical SOC numbers for a synthetic inverter prefix."""
    return ["number.{}_program_{}_soc".format(prefix, row) for row in SIX_PROGRAM_RANGE]


class _Fixture:
    """Bound state for a single test scenario - eliminates the unpack-length surface area."""

    def __init__(self, my_predbat, prefix="test_deye"):
        """Load the real YAML and seed only the isolated mock HA control entities."""
        template_path = Path(__file__).resolve().parents[3] / "templates" / "deye_solarman.yaml"
        with template_path.open() as stream:
            self.template = yaml.safe_load(stream)["pred_bat"]
        self.saved_definition = copy.deepcopy(INVERTER_DEF.get("DeyeSolarman"))
        self.saved_call = my_predbat.call_service_wrapper
        my_predbat.args.clear()
        my_predbat.args.update(self.template)
        my_predbat.args["deye_entity_prefix"] = prefix
        my_predbat.args_from_apps_yaml = copy.deepcopy(my_predbat.args)
        my_predbat.last_service_hash = {}
        ha = my_predbat.ha_interface
        self.sources = _expected_source_entities(prefix)
        self.targets = _expected_target_entities(prefix)
        for entity in self.sources:
            ha.dummy_items[entity] = {"state": "Disabled", "options": ["Disabled", "Grid", "Generator", "Both"]}
        for entity in self.targets:
            ha.dummy_items[entity] = {"state": 10, "min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"}
        ha.dummy_items["sensor.{}_battery".format(prefix)] = 60
        ha.dummy_items["sensor.{}_battery_voltage".format(prefix)] = 51.2
        ha.dummy_items["sensor.{}_battery_temperature".format(prefix)] = 20
        ha.dummy_items["sensor.{}_battery_power".format(prefix)] = 0
        ha.dummy_items["sensor.{}_pv_power".format(prefix)] = 0
        ha.dummy_items["sensor.{}_load_power".format(prefix)] = 500
        ha.dummy_items["sensor.{}_grid_power".format(prefix)] = 500
        self.writes = []
        self.rejected = set()
        self.my_predbat = my_predbat
        self.prefix = prefix

        fixture = self

        def service_call(service, **data):
            """Record a synthetic service and update only accepted mock cache entities."""
            entities = data.get("entity_id", "").split(",")
            fixture.writes.append((service, copy.deepcopy(data)))
            if fixture.rejected.intersection(entities):
                return False
            for entity in entities:
                if entity in fixture.sources + fixture.targets:
                    current = ha.dummy_items.get(entity)
                    if isinstance(current, dict):
                        current["state"] = data.get("option", data.get("value"))
                    else:
                        ha.dummy_items[entity] = data.get("option", data.get("value"))
            return True

        my_predbat.call_service_wrapper = service_call
        self.stand_inverter()

    def stand_inverter(self):
        """Construct the real profile and publish its actual model rates to the synthetic base."""
        self.inv = Inverter(self.my_predbat, 0, quiet=True)
        self.inv.sleep = lambda seconds: None
        self.inv.inv_write_and_poll_sleep = 0
        self.inv.update_status(self.my_predbat.minutes_now)
        self.my_predbat.inverters = [self.inv]
        self.my_predbat.battery_rate_max_charge = self.inv.battery_rate_max_charge
        self.my_predbat.battery_rate_max_charge_dc = self.inv.battery_rate_max_charge_dc
        self.my_predbat.battery_rate_max_discharge = self.inv.battery_rate_max_discharge
        self.my_predbat.inverter_limit = self.inv.inverter_limit
        self.my_predbat.export_limit = self.inv.export_limit
        self.my_predbat.soc_max = self.inv.soc_max
        self.my_predbat.reserve = self.inv.reserve

    def teardown(self):
        """Restore the service callback and shared custom-profile registration."""
        self.my_predbat.call_service_wrapper = self.saved_call
        if self.saved_definition is None:
            INVERTER_DEF.pop("DeyeSolarman", None)
        else:
            INVERTER_DEF["DeyeSolarman"] = self.saved_definition


def run_car_planning_template_tests(my_predbat):
    """Drive validate / snapshot / dispatch against a mocked HA interface."""
    if not shared_template_available():
        check_unavailable_template(my_predbat)
        return False

    from inverter_service_control import dispatch_template, snapshot_template, validate_template

    failed = False

    def check(condition, message):
        """Collect an assertion failure without concealing subsequent guard checks."""
        nonlocal failed
        if not condition:
            print("ERROR: EV site template adapter: " + message)
            failed = True

    reset_inverter(my_predbat)
    original_definition = copy.deepcopy(INVERTER_DEF.get("DeyeSolarman"))

    # ---------------------------------------------------------------------
    # validate_template: real template -> True; bad mapping -> ValueError.
    # ---------------------------------------------------------------------
    fx = _Fixture(my_predbat, prefix="test_deye")
    try:
        check(validate_template(fx.inv) is True, "real shared template must validate cleanly")
    except ServiceSequenceError as exc:
        check(False, "real shared template raised unexpectedly: " + str(exc))

    # A non-Inverter object must raise.
    try:
        check(validate_template(object()) is True, "non-Inverter must be rejected")
        check(False, "non-Inverter must NOT validate")
    except ServiceSequenceError:
        pass

    # A foreign inverter_type must raise, even when the canonical mapping
    # is otherwise present (malicious-mapping capture protection).
    fx.inv.inverter_type = "GivEnergy"
    try:
        validate_template(fx.inv)
        check(False, "foreign inverter_type must raise ValueError")
    except ServiceSequenceError:
        pass
    fx.inv.inverter_type = "DeyeSolarman"

    # Empty or missing prefix must raise.
    fx.my_predbat.args["deye_entity_prefix"] = ""
    try:
        validate_template(fx.inv)
        check(False, "empty prefix must raise ValueError")
    except ServiceSequenceError:
        pass
    fx.my_predbat.args["deye_entity_prefix"] = fx.prefix

    # Booleans or NaN/Infinity rates must raise.
    fx.my_predbat.battery_rate_max_charge = True
    try:
        validate_template(fx.inv)
        check(False, "boolean battery_rate_max_charge must raise ValueError")
    except ServiceSequenceError:
        pass
    fx.my_predbat.battery_rate_max_charge = fx.inv.battery_rate_max_charge

    fx.my_predbat.battery_rate_max_charge = float("nan")
    try:
        validate_template(fx.inv)
        check(False, "NaN battery_rate_max_charge must raise ValueError")
    except ServiceSequenceError:
        pass
    fx.my_predbat.battery_rate_max_charge = fx.inv.battery_rate_max_charge

    # Two-step charge_start_service must raise.
    fx.my_predbat.args["charge_start_service"] = fx.template["charge_start_service"][:2]
    fx.stand_inverter()
    try:
        validate_template(fx.inv)
        check(False, "two-step charge_start_service must raise ValueError")
    except ServiceSequenceError:
        pass
    fx.my_predbat.args["charge_start_service"] = fx.template["charge_start_service"]

    # One-step charge_stop_service must raise.
    fx.my_predbat.args["charge_stop_service"] = fx.template["charge_stop_service"][:1]
    fx.stand_inverter()
    try:
        validate_template(fx.inv)
        check(False, "one-step charge_stop_service must raise ValueError")
    except ServiceSequenceError:
        pass
    fx.my_predbat.args["charge_stop_service"] = fx.template["charge_stop_service"]

    # Missing verify:true on any step must raise.
    bad_steps = [copy.deepcopy(step) for step in fx.template["charge_start_service"]]
    bad_steps[1].pop("verify", None)
    fx.my_predbat.args["charge_start_service"] = bad_steps
    fx.stand_inverter()
    try:
        validate_template(fx.inv)
        check(False, "missing verify:true must raise ValueError")
    except ServiceSequenceError:
        pass
    fx.my_predbat.args["charge_start_service"] = fx.template["charge_start_service"]

    # device_id payload on a step must raise.
    bad_steps = [copy.deepcopy(step) for step in fx.template["charge_start_service"]]
    bad_steps[1]["device_id"] = "abc"
    fx.my_predbat.args["charge_start_service"] = bad_steps
    fx.stand_inverter()
    try:
        validate_template(fx.inv)
        check(False, "device_id payload must raise ValueError")
    except ServiceSequenceError:
        pass
    fx.my_predbat.args["charge_start_service"] = fx.template["charge_start_service"]

    # Hard-coded SOC value (not {target_soc}) must raise.
    bad_steps = [copy.deepcopy(step) for step in fx.template["charge_start_service"]]
    bad_steps[1]["value"] = 80
    fx.my_predbat.args["charge_start_service"] = bad_steps
    fx.stand_inverter()
    try:
        validate_template(fx.inv)
        check(False, "hard-coded SOC must raise ValueError")
    except ServiceSequenceError:
        pass
    fx.my_predbat.args["charge_start_service"] = fx.template["charge_start_service"]

    # Wrong enable option (Generator, not Grid) must raise.
    bad_steps = [copy.deepcopy(step) for step in fx.template["charge_start_service"]]
    bad_steps[2]["option"] = "Generator"
    fx.my_predbat.args["charge_start_service"] = bad_steps
    fx.stand_inverter()
    try:
        validate_template(fx.inv)
        check(False, "wrong enable option must raise ValueError")
    except ServiceSequenceError:
        pass
    fx.my_predbat.args["charge_start_service"] = fx.template["charge_start_service"]

    # Malicious mapping: a *different* canonical six-row prefix on another
    # inverter cannot pass preflight only because the cached state already
    # shows the expected terminal - the adapter compares the resolved
    # entity list from prepare_service_sequence to the canonical mapping.
    fx.my_predbat.args["charge_start_service"] = copy.deepcopy(fx.template["charge_start_service"])
    fx.my_predbat.args["charge_stop_service"] = copy.deepcopy(fx.template["charge_stop_service"])
    fx.my_predbat.args["deye_entity_prefix"] = "other_inverter"
    fx.my_predbat.inverter_type = "DeyeSolarman"
    fx.inv.deye_entity_prefix = "other_inverter"
    fx.stand_inverter()
    try:
        validate_template(fx.inv)
        check(False, "malicious mapping (other inverter) must raise ValueError")
    except ServiceSequenceError:
        pass
    fx.my_predbat.args["deye_entity_prefix"] = "test_deye"
    fx.stand_inverter()

    # ---------------------------------------------------------------------
    # snapshot_template: cache-only mirror; raises on bad cache state.
    # Float ceil on demand_reserve_soc + configuration mapping for drift.
    # ---------------------------------------------------------------------
    fx = _Fixture(my_predbat, prefix="test_deye")
    snap = snapshot_template(fx.inv)
    check(snap["profile_id"] == "deye_solarman_shared", "snapshot profile_id mismatch")
    check(snap["source_entities"] == fx.sources, "snapshot source entities mismatch")
    check(snap["target_entities"] == fx.targets, "snapshot target entities mismatch")
    check(snap["current_sources"] == ["Disabled"] * 6, "snapshot source state mismatch")
    check(snap["current_targets"] == [10] * 6, "snapshot target state mismatch")
    # math.ceil not int(round) - a fractional reserve must round UP to the
    # next whole percent at all times.
    fx.inv.reserve_percent = 4.2
    fx.my_predbat.reserve = fx.inv.soc_max * 4.2 / 100
    snap = snapshot_template(fx.inv)
    check(snap["demand_reserve_soc"] == 5, "snapshot demand_reserve_soc must use math.ceil (4.2 -> 5)")
    check(snap["charge_target_soc"] == 100.0, "snapshot charge_target_soc must be 100.0 float")
    check("configuration" in snap, "snapshot must include resolved configuration mapping")
    check(snap["configuration"]["inverter_type"] == "DeyeSolarman", "snapshot configuration inverter_type mismatch")
    check(snap["configuration"]["reserve_percent"] == 4.2, "snapshot configuration reserve_percent must hold raw value")
    check(snap["configuration"]["discharge_stop_service"] == fx.template["discharge_stop_service"], "snapshot configuration must carry discharge_stop_service")
    # Reverting reserve_percent for downstream assertions.
    fx.inv.reserve_percent = 10.0
    fx = _Fixture(my_predbat, prefix="test_deye")
    snap = snapshot_template(fx.inv)
    check(snap["demand_reserve_soc"] == 10, "snapshot demand_reserve_soc with whole-percent reserve must equal that integer")

    # Numeric-string target state (real HA returns '45', not 45): snapshot
    # must still accept the value so service_sequence_state_matches at
    # dispatch time can compare against the expected float.
    fx.my_predbat.ha_interface.dummy_items[fx.targets[0]] = {"state": "45", "min": 0, "max": 100, "step": 1}
    snap = snapshot_template(fx.inv)
    check(snap["current_targets"][0] == "45", "snapshot must preserve HA's numeric-string target state")

    # snapshot must NOT call HA services (no dispatch writes).
    writes_during_snapshot = []

    def counting_service(service, **data):
        """Record any unintended write attempted while taking a read-only snapshot."""
        writes_during_snapshot.append((service, data))
        return True

    fx.my_predbat.ha_interface.dummy_items[fx.targets[0]] = {"state": 10, "min": 0, "max": 100, "step": 1}
    saved = fx.my_predbat.call_service_wrapper
    fx.my_predbat.call_service_wrapper = counting_service
    try:
        snapshot_template(fx.inv)
    finally:
        fx.my_predbat.call_service_wrapper = saved
    check(not writes_during_snapshot, "snapshot must not write any HA service")

    # snapshot must reach every canonical row through get_state_wrapper.
    row_calls = []
    real_get = fx.my_predbat.get_state_wrapper

    def tracking_get(entity_id=None, default=None, attribute=None, refresh=False, raw=False):
        """Track canonical row observations while preserving the mock cache read semantics."""
        if entity_id in (fx.sources + fx.targets):
            row_calls.append(entity_id)
        return real_get(entity_id=entity_id, default=default, attribute=attribute, refresh=refresh, raw=raw)

    with patch.object(fx.my_predbat, "get_state_wrapper", side_effect=tracking_get):
        snapshot_template(fx.inv)
    for entity in fx.sources + fx.targets:
        check(entity in row_calls, "snapshot must reach " + entity + " through get_state_wrapper")

    # Missing source/target entity -> snapshot raises.
    fx.my_predbat.ha_interface.dummy_items.pop(fx.sources[-1], None)
    try:
        snapshot_template(fx.inv)
        check(False, "snapshot must raise ValueError on missing row")
    except ServiceSequenceError:
        pass
    fx.my_predbat.ha_interface.dummy_items[fx.sources[-1]] = {"state": "Disabled", "options": ["Disabled", "Grid", "Generator", "Both"]}

    # Unavailable state on a row -> snapshot raises.
    fx.my_predbat.ha_interface.dummy_items[fx.targets[0]] = {"state": "unavailable", "min": 0, "max": 100, "step": 1}
    try:
        snapshot_template(fx.inv)
        check(False, "snapshot must raise ValueError on unavailable row")
    except ServiceSequenceError:
        pass
    fx.my_predbat.ha_interface.dummy_items[fx.targets[0]] = {"state": 10, "min": 0, "max": 100, "step": 1}

    # ---------------------------------------------------------------------
    # dispatch_template: charge (target_soc=100) and demand (target_soc=0).
    # The native entrypoint is now called exactly once with the 2-arg
    # signature; no signature fallback.
    # ---------------------------------------------------------------------
    fx = _Fixture(my_predbat, prefix="test_deye")
    ok = dispatch_template(fx.inv, target_soc=100, freeze=False, discharge_power_w=None)
    check(ok is True, "charge dispatch must return True when all six rows observed")
    sources_after = [fx.my_predbat.ha_interface.get_state(entity) for entity in fx.sources]
    targets_after = [float(fx.my_predbat.ha_interface.get_state(entity)) for entity in fx.targets]
    check(sources_after == ["Grid"] * 6, "charge must drive every source to Grid")
    check(targets_after == [100.0] * 6, "charge must drive every target to 100")

    # Native returns None -> still True because observation confirms.
    native_calls = []

    def recording_adjust(target_soc, freeze=False):
        """Record the precise native dispatch signature while returning its normal None result."""
        native_calls.append((target_soc, freeze))
        return None

    with patch.object(fx.inv, "adjust_charge_immediate", side_effect=recording_adjust):
        ok = dispatch_template(fx.inv, target_soc=100, freeze=False, discharge_power_w=None)
    check(ok is True, "charge dispatch must succeed even when native returns None")
    check(native_calls == [(100, False)], "native dispatch must be called once with (target_soc=100, freeze=False), no fallback")

    # Demand with whole-percent reserve (10) -> state_matches against float 10.0 must hit.
    ok = dispatch_template(fx.inv, target_soc=0, freeze=False, discharge_power_w=None)
    check(ok is True, "demand dispatch must return True")
    sources_after = [fx.my_predbat.ha_interface.get_state(entity) for entity in fx.sources]
    targets_after = [float(fx.my_predbat.ha_interface.get_state(entity)) for entity in fx.targets]
    check(sources_after == ["Disabled"] * 6, "demand must drive every source to Disabled")
    check(targets_after == [10.0] * 6, "demand must mirror the verified reserve across all six rows")

    # Demand with fractional reserve - ceil must apply on dispatch.
    fx = _Fixture(my_predbat, prefix="test_deye")
    fx.inv.reserve_percent = 4.2
    fx.my_predbat.reserve = fx.inv.soc_max * 4.2 / 100
    ok = dispatch_template(fx.inv, target_soc=0, freeze=False, discharge_power_w=None)
    check(ok is True, "demand dispatch with fractional reserve must return True")
    targets_after = [float(fx.my_predbat.ha_interface.get_state(entity)) for entity in fx.targets]
    check(targets_after == [5.0] * 6, "demand with reserve_percent=4.2 must ceil to 5.0 across all six rows")

    # ---------------------------------------------------------------------
    # dispatch_template: base.had_errors must force False.
    # ---------------------------------------------------------------------
    fx = _Fixture(my_predbat, prefix="test_deye")
    fx.my_predbat.had_errors = True
    try:
        ok = dispatch_template(fx.inv, target_soc=0, freeze=False, discharge_power_w=None)
        check(ok is False, "had_errors=True must force dispatch False")
    finally:
        fx.my_predbat.had_errors = False

    # ---------------------------------------------------------------------
    # dispatch_template: unsupported intents raise ValueError BEFORE the
    # native call.
    # ---------------------------------------------------------------------
    for kwargs in (
        {"target_soc": 80, "freeze": False, "discharge_power_w": None},
        {"target_soc": 100, "freeze": True, "discharge_power_w": None},
        {"target_soc": 0, "freeze": False, "discharge_power_w": 3000},
    ):
        try:
            dispatch_template(fx.inv, **kwargs)
            check(False, "unsupported intent must raise ValueError: " + repr(kwargs))
        except ValueError:
            pass
        check(fx.my_predbat.had_errors is False, "unsupported intent must NOT set had_errors")

    # ---------------------------------------------------------------------
    # dispatch_template: partial observation must NOT silently downgrade.
    # ---------------------------------------------------------------------
    fx = _Fixture(my_predbat, prefix="test_deye")
    fx.my_predbat.ha_interface.dummy_items[fx.targets[-1]] = {"state": 99, "min": 0, "max": 100, "step": 1}
    fx.rejected.add(fx.targets[-1])

    def rejecting_service(service, **data):
        """Model a partial native sequence failure without touching any live transport."""
        entities = data.get("entity_id", "").split(",")
        if fx.rejected.intersection(entities):
            return False
        for entity in entities:
            current = fx.my_predbat.ha_interface.dummy_items.get(entity)
            if isinstance(current, dict):
                current["state"] = data.get("option", data.get("value"))
        return True

    saved_service = fx.my_predbat.call_service_wrapper
    fx.my_predbat.call_service_wrapper = rejecting_service
    try:
        ok = dispatch_template(fx.inv, target_soc=100, freeze=False, discharge_power_w=None)
        check(ok is False, "partial observation must NOT be reported as True")
        check(fx.my_predbat.ha_interface.get_state(fx.sources[-1]) != "Grid", "rejected source row must NOT be reported as Grid")
    finally:
        fx.my_predbat.call_service_wrapper = saved_service

    fx.teardown()
    if original_definition is None:
        INVERTER_DEF.pop("DeyeSolarman", None)
    else:
        INVERTER_DEF["DeyeSolarman"] = original_definition

    print("EV site template adapter tests: {}".format("FAILED" if failed else "PASSED"))
    return failed
