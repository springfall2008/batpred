# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for the GivTCP component (givtcp.py) - publishes GivEnergy REST
inverter status/controls as HA entities and points Inverter's standard
apps.yaml entity keys at them via automatic_config().
"""

import json

from unittest.mock import MagicMock

from tests.test_infra import run_async
from mock_base import MockBase
from givtcp import GivTCPComponent, GIVTCP_POLL_SECONDS, GIVTCP_REDISCOVER_SECONDS, DISCHARGE_TARGET_UNSUPPORTED_MODELS


def _rest_data_blob(
    charge_rate=1000,
    discharge_rate=2000,
    target_soc=80,
    reserve=10,
    soc_kwh=5.0,
    soc_percent=50,
    charge_start="00:30:00",
    charge_end="04:30:00",
    discharge_start="16:00:00",
    discharge_end="19:00:00",
    mode="Eco",
    pause_mode="Disabled",
    pause_start="00:00:00",
    pause_end="00:00:00",
    charge_target_enable="enable",
):
    """A realistic-shaped GivTCP /readData response, sized to what publish_data() reads."""
    return {
        "Control": {
            "Battery_Charge_Rate": charge_rate,
            "Battery_Discharge_Rate": discharge_rate,
            "Target_SOC": target_soc,
            "Battery_Power_Reserve": reserve,
            "Enable_Charge_Schedule": "enable",
            "Enable_Discharge_Schedule": "disable",
            "Discharge_Target_SOC_1": 20,
            "Mode": mode,
            "Battery_pause_mode": pause_mode,
            "Enable_Charge_Target": charge_target_enable,
        },
        "Power": {"Power": {"SOC_kWh": soc_kwh, "SOC": soc_percent, "Battery_Power": 100.0, "PV_Power": 200.0, "Grid_Power": -50.0, "Load_Power": 250.0, "Battery_Voltage": 51.2}},
        "Timeslots": {
            "Charge_start_time_slot_1": charge_start,
            "Charge_end_time_slot_1": charge_end,
            "Discharge_start_time_slot_1": discharge_start,
            "Discharge_end_time_slot_1": discharge_end,
            "Battery_pause_start_time_slot": pause_start,
            "Battery_pause_end_time_slot": pause_end,
        },
    }


def _make_component(rest_urls="http://givtcp:6345"):
    base = MockBase()
    component = GivTCPComponent(base, rest_urls=rest_urls)
    return base, component


def _mark_discovered(component, indices=None):
    """
    Stand in for run()'s discovery pass.

    automatic_config() is driven by which endpoints actually answered, not by how many URLs are
    configured, so a test calling it directly has to establish that precondition itself.
    """
    component.discovered = list(range(len(component.rest))) if indices is None else list(indices)
    component.discovery_done = True
    return component


def test_initialize_scalar(my_predbat=None):
    """A single URL (not a list) creates exactly one GivTCPRest instance."""
    base, component = _make_component(rest_urls="http://givtcp:6345")
    assert len(component.rest) == 1, f"Expected 1 rest client, got {len(component.rest)}"
    assert component.rest[0].inverter.rest_api == "http://givtcp:6345"
    assert component.rest[0].inverter.id == 0
    print("PASS: scalar rest_urls creates one GivTCPRest instance")
    return 0


def test_initialize_list(my_predbat=None):
    """A list of URLs creates one GivTCPRest per entry, indexed in order."""
    base, component = _make_component(rest_urls=["http://givtcp0:6345", "http://givtcp1:6345"])
    assert len(component.rest) == 2, f"Expected 2 rest clients, got {len(component.rest)}"
    assert component.rest[0].inverter.rest_api == "http://givtcp0:6345"
    assert component.rest[1].inverter.rest_api == "http://givtcp1:6345"
    assert component.rest[0].inverter.id == 0 and component.rest[1].inverter.id == 1
    print("PASS: list rest_urls creates one GivTCPRest per URL, in order")
    return 0


def test_publish_data_no_status_yet(my_predbat=None):
    """No entities are published for an inverter that hasn't returned a status snapshot yet."""
    base, component = _make_component()
    run_async(component.publish_data())
    assert len(base.entities) == 0, f"Expected no entities published, got {list(base.entities)}"
    print("PASS: publish_data publishes nothing before the first successful read")
    return 0


def test_publish_data_controls(my_predbat=None):
    """publish_data() turns a GivTCP snapshot into the expected control entities/states."""
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _rest_data_blob()
    # v3: the discharge target control below is version gated, v2 has no /setDischargeTarget
    component.rest[0].inverter.rest_v3 = True
    run_async(component.publish_data())

    checks = {
        "number.predbat_givtcp_0_charge_rate": 1000,
        "number.predbat_givtcp_0_discharge_rate": 2000,
        "number.predbat_givtcp_0_charge_limit": 80.0,
        "number.predbat_givtcp_0_reserve": 10,
        "number.predbat_givtcp_0_discharge_target_soc": 20,
        "switch.predbat_givtcp_0_scheduled_charge_enable": "on",
        "switch.predbat_givtcp_0_scheduled_discharge_enable": "off",
        "select.predbat_givtcp_0_charge_start_time": "00:30:00",
        "select.predbat_givtcp_0_charge_end_time": "04:30:00",
        "select.predbat_givtcp_0_discharge_start_time": "16:00:00",
        "select.predbat_givtcp_0_discharge_end_time": "19:00:00",
    }
    for entity_id, expected in checks.items():
        assert entity_id in base.entities, f"Missing entity {entity_id}"
        got = base.entities[entity_id]["state"]
        assert got == expected, f"{entity_id}: expected {expected}, got {got}"
    print("PASS: publish_data publishes the expected control entities and states")
    return 0


def test_publish_data_sensors(my_predbat=None):
    """publish_data() publishes the read-only status sensors with the normalised values."""
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _rest_data_blob(soc_kwh=5.0, soc_percent=50)
    component.rest[0].inverter.rest_v3 = True  # battery_voltage only trusts GivTCP's own field on v3+ (normally set by run()'s version detection)
    run_async(component.publish_data())

    checks = {
        "sensor.predbat_givtcp_0_soc_kw": 5.0,
        "sensor.predbat_givtcp_0_soc_percent": 50,
        "sensor.predbat_givtcp_0_battery_power": 100.0,
        "sensor.predbat_givtcp_0_pv_power": 200.0,
        "sensor.predbat_givtcp_0_grid_power": -50.0,
        "sensor.predbat_givtcp_0_load_power": 250.0,
        "sensor.predbat_givtcp_0_battery_voltage": 51.2,
    }
    for entity_id, expected in checks.items():
        assert entity_id in base.entities, f"Missing entity {entity_id}"
        got = base.entities[entity_id]["state"]
        assert got == expected, f"{entity_id}: expected {expected}, got {got}"
    print("PASS: publish_data publishes the expected sensor entities and values")
    return 0


def test_automatic_config_single_inverter(my_predbat=None):
    """automatic_config() points every standard apps.yaml key at this component's own entities."""
    base, component = _make_component(rest_urls="http://givtcp:6345")
    _mark_discovered(component)
    run_async(component.automatic_config())

    assert base.args["num_inverters"] == 1, f"Expected num_inverters 1, got {base.args['num_inverters']}"
    assert base.args["inverter_type"] == ["GE"], f"Expected inverter_type ['GE'], got {base.args['inverter_type']}"
    assert base.args["charge_rate"] == ["number.predbat_givtcp_0_charge_rate"]
    assert base.args["charge_start_time"] == ["select.predbat_givtcp_0_charge_start_time"]
    assert base.args["scheduled_discharge_enable"] == ["switch.predbat_givtcp_0_scheduled_discharge_enable"]
    assert base.args["battery_power"] == ["sensor.predbat_givtcp_0_battery_power"]
    print("PASS: automatic_config points standard keys at this component's entities")
    return 0


def test_automatic_config_two_inverters(my_predbat=None):
    """automatic_config() produces one indexed entity per inverter, in order, for multi-inverter setups."""
    base, component = _make_component(rest_urls=["http://givtcp0:6345", "http://givtcp1:6345"])
    _mark_discovered(component)
    run_async(component.automatic_config())

    assert base.args["num_inverters"] == 2, f"Expected num_inverters 2, got {base.args['num_inverters']}"
    assert base.args["charge_rate"] == ["number.predbat_givtcp_0_charge_rate", "number.predbat_givtcp_1_charge_rate"]
    assert base.args["inverter_type"] == ["GE", "GE"]
    print("PASS: automatic_config indexes entities per inverter for multi-inverter setups")
    return 0


def test_parse_entity(my_predbat=None):
    """_parse_entity extracts the inverter index and control name from a published entity_id."""
    base, component = _make_component()
    n, control = component._parse_entity("number.predbat_givtcp_0_charge_rate")
    assert n == 0 and control == "charge_rate", f"Got n={n} control={control}"
    n, control = component._parse_entity("select.predbat_givtcp_2_discharge_end_time")
    assert n == 2 and control == "discharge_end_time", f"Got n={n} control={control}"
    n, control = component._parse_entity("switch.something_unrelated")
    assert n is None and control is None, f"Expected (None, None) for a non-matching entity, got ({n}, {control})"
    print("PASS: _parse_entity correctly extracts inverter index and control name")
    return 0


def test_number_event_charge_rate(my_predbat=None):
    """A number_event on charge_rate reaches GivTCPRest.set_charge_rate inline, before returning."""
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _rest_data_blob()
    component.rest[0].set_charge_rate = MagicMock(return_value=True)

    run_async(component.number_event("number.predbat_givtcp_0_charge_rate", 1500))

    component.rest[0].set_charge_rate.assert_called_once_with(1500)
    print("PASS: number_event on charge_rate dispatches to GivTCPRest.set_charge_rate")
    return 0


def test_write_event_is_applied_inline_not_deferred(my_predbat=None):
    """
    Write events must be applied before the handler returns, never deferred to the next run().

    Inverter.write_and_poll_value/option() polls the entity back within seconds to decide whether
    the write landed, and only publish_data() updates that entity - so a write deferred to run()'s
    60s cadence would be judged failed long before it was attempted. Regression guard for that.
    """
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _rest_data_blob(charge_rate=1000)
    component.rest[0].set_charge_rate = MagicMock(return_value=True)

    run_async(component.number_event("number.predbat_givtcp_0_charge_rate", 1500))

    assert component.rest[0].set_charge_rate.called, "Write was not applied inline - it must not wait for run()"
    entity = "number.predbat_givtcp_0_charge_rate"
    assert entity in base.entities, "Expected the entity to be republished inline after the write"
    print("PASS: write events are applied inline and republished immediately")
    return 0


def test_switch_event_scheduled_charge_enable(my_predbat=None):
    """A switch turn_on/turn_off maps to enable_charge_schedule(True/False)."""
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _rest_data_blob()
    component.rest[0].enable_charge_schedule = MagicMock(return_value=True)

    run_async(component.switch_event("switch.predbat_givtcp_0_scheduled_charge_enable", "turn_on"))
    component.rest[0].enable_charge_schedule.assert_called_once_with(True)

    component.rest[0].enable_charge_schedule.reset_mock()
    run_async(component.switch_event("switch.predbat_givtcp_0_scheduled_charge_enable", "turn_off"))
    component.rest[0].enable_charge_schedule.assert_called_once_with(False)
    print("PASS: switch_event maps turn_on/turn_off to enable_charge_schedule(True/False)")
    return 0


def test_switch_event_toggle_ignored(my_predbat=None):
    """A 'toggle' service (no on/off semantics for GivTCP) is dropped rather than written."""
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _rest_data_blob()
    component.rest[0].enable_charge_schedule = MagicMock(return_value=True)
    run_async(component.switch_event("switch.predbat_givtcp_0_scheduled_charge_enable", "toggle"))
    assert not component.rest[0].enable_charge_schedule.called, "Expected toggle to be ignored, not written"
    print("PASS: an unsupported switch service is dropped rather than written")
    return 0


def test_select_event_charge_start_time_preserves_end(my_predbat=None):
    """Changing charge_start_time writes the new start alongside the existing end time, not a default."""
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _rest_data_blob(charge_start="00:30:00", charge_end="04:30:00")
    component.rest[0].set_charge_slot1 = MagicMock(return_value=True)

    run_async(component.select_event("select.predbat_givtcp_0_charge_start_time", "01:00:00"))

    component.rest[0].set_charge_slot1.assert_called_once_with("01:00:00", "04:30:00")
    print("PASS: select_event on charge_start_time preserves the existing end time")
    return 0


def test_select_event_discharge_end_time_preserves_start(my_predbat=None):
    """Changing discharge_end_time writes the existing start alongside the new end time."""
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _rest_data_blob(discharge_start="16:00:00", discharge_end="19:00:00")
    component.rest[0].set_discharge_slot1 = MagicMock(return_value=True)

    run_async(component.select_event("select.predbat_givtcp_0_discharge_end_time", "20:00:00"))

    component.rest[0].set_discharge_slot1.assert_called_once_with("16:00:00", "20:00:00")
    print("PASS: select_event on discharge_end_time preserves the existing start time")
    return 0


def test_publish_data_mode_entities(my_predbat=None):
    """inverter_mode publishes on any version; the pause entities are v3 only."""
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _rest_data_blob(mode="Timed Export", pause_mode="PauseCharge", pause_start="01:00:00", pause_end="02:00:00")

    # v2: no /setBatteryPauseMode endpoint, so publishing a pause entity would offer a control that
    # can never be written - Inverter.adjust_pause_mode's REST path was gated on v3 for the same reason
    component.rest[0].inverter.rest_v3 = False
    run_async(component.publish_data())
    assert base.entities["select.predbat_givtcp_0_inverter_mode"]["state"] == "Timed Export", f"Expected inverter_mode published, got {base.entities['select.predbat_givtcp_0_inverter_mode']['state']}"
    assert "select.predbat_givtcp_0_pause_mode" not in base.entities, "pause_mode must not be published for GivTCP v2"

    component.rest[0].inverter.rest_v3 = True
    run_async(component.publish_data())
    assert base.entities["select.predbat_givtcp_0_pause_mode"]["state"] == "PauseCharge", f"Expected pause_mode published, got {base.entities['select.predbat_givtcp_0_pause_mode']['state']}"
    assert base.entities["select.predbat_givtcp_0_pause_start_time"]["state"] == "01:00:00"
    assert base.entities["select.predbat_givtcp_0_pause_end_time"]["state"] == "02:00:00"

    print("PASS: inverter_mode publishes always, pause entities only on v3")
    return 0


def test_select_event_plain_selects_pass_the_value_through(my_predbat=None):
    """
    A select that is not a time slot passes its chosen option straight to the write method.

    Regression: _handle_write only had branches for the slot selects, switches and numbers, so a
    plain select silently did nothing - the write was dropped and the entity then republished with
    the unchanged value, which reads as a successful no-op.
    """
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _rest_data_blob()
    component.rest[0].set_battery_mode = MagicMock(return_value=True)
    component.rest[0].set_battery_pause_mode = MagicMock(return_value=True)

    run_async(component.select_event("select.predbat_givtcp_0_inverter_mode", "Timed Export"))
    component.rest[0].set_battery_mode.assert_called_once_with("Timed Export")

    run_async(component.select_event("select.predbat_givtcp_0_pause_mode", "PauseBoth"))
    component.rest[0].set_battery_pause_mode.assert_called_once_with("PauseBoth")

    print("PASS: plain selects pass their option through to the write method")
    return 0


def test_select_event_pause_start_time_preserves_end(my_predbat=None):
    """Changing pause_start_time writes the new start alongside the existing end, like the charge slot."""
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _rest_data_blob(pause_start="00:00:00", pause_end="23:59:00")
    component.rest[0].set_pause_slot = MagicMock(return_value=True)

    run_async(component.select_event("select.predbat_givtcp_0_pause_start_time", "01:00:00"))

    component.rest[0].set_pause_slot.assert_called_once_with("01:00:00", "23:59:00")
    print("PASS: select_event on pause_start_time preserves the existing end time")
    return 0


def test_automatic_config_pause_keys_need_v3_everywhere(my_predbat=None):
    """Pause keys are only claimed when every inverter is v3, mirroring the power_ignore rule."""
    base, component = _make_component(rest_urls=["http://givtcp0:6345", "http://givtcp1:6345"])
    _mark_discovered(component)
    for rest in component.rest:
        rest.inverter.rest_data = _rest_data_blob()

    # A mixed fleet must leave the pause keys alone rather than pointing the v2 inverter at an
    # entity its GivTCP can never accept a write for
    component.rest[0].inverter.rest_v3 = True
    component.rest[1].inverter.rest_v3 = False
    run_async(component.automatic_config())
    assert "pause_mode" not in base.args, f"Expected pause_mode left unconfigured on a mixed fleet, got {base.args.get('pause_mode')}"
    # inverter_mode is not version gated, so it is still claimed
    assert "inverter_mode" in base.args, "Expected inverter_mode to be auto-configured regardless of version"

    component.rest[1].inverter.rest_v3 = True
    run_async(component.automatic_config())
    assert base.args.get("pause_mode") == ["select.predbat_givtcp_0_pause_mode", "select.predbat_givtcp_1_pause_mode"], f"Expected pause_mode configured for both, got {base.args.get('pause_mode')}"

    print("PASS: pause keys are auto-configured only when every inverter is v3")
    return 0


def test_discharge_target_not_published_for_unsupported_models(my_predbat=None):
    """
    Regression test for issue #4517, moved here with the model check itself.

    Some GivTCP inverter models have no working Discharge_Target_SOC_1 register - GivTCP reports a
    write as successful, but it never persists between cycles, so the caller sees a permanent
    mismatch and rewrites indefinitely. "Ac" (AC Coupled) was confirmed first; "Hybrid_gen1" was
    added after a reporter confirmed live, post-fix, that two of his Gen1 inverters still repeated
    the write every cycle while a third, genuinely AC Coupled, correctly stopped.

    Not publishing the entity is now what stops it: Inverter.adjust_force_export leaves a target it
    cannot read alone, so an absent entity means no write is ever attempted.
    """
    entity_id = "number.predbat_givtcp_0_discharge_target_soc"

    for model in DISCHARGE_TARGET_UNSUPPORTED_MODELS:
        base, component = _make_component()
        component.rest[0].inverter.rest_data = _rest_data_blob()
        component.rest[0].inverter.rest_data["raw"] = {"invertor": {"model": model, "discharge_target_soc_1": "4"}}
        # the model check is a v3-only concern now: v2 publishes no discharge target at all
        component.rest[0].inverter.rest_v3 = True
        run_async(component.publish_data())
        assert entity_id not in base.entities, f"model={model!r} must not publish a discharge target entity"

    # A model not on the list (including a later Hybrid generation, or none reported at all) must
    # still get the entity, so the write goes ahead exactly as before
    for model in ["Hybrid", "Hybrid_gen3", ""]:
        base, component = _make_component()
        component.rest[0].inverter.rest_data = _rest_data_blob()
        component.rest[0].inverter.rest_data["raw"] = {"invertor": {"model": model, "discharge_target_soc_1": "4"}}
        # the model check is a v3-only concern now: v2 publishes no discharge target at all
        component.rest[0].inverter.rest_v3 = True
        run_async(component.publish_data())
        assert entity_id in base.entities, f"model={model!r} should still publish a discharge target entity"

    print("PASS: discharge target entity is withheld only for the unsupported models")
    return 0


def _rest_from_fixture(filename):
    """A component whose single inverter holds a real captured GivTCP /readData response."""
    base, component = _make_component()
    with open(filename, "r") as handle:
        component.rest[0].inverter.rest_data = json.load(handle)
    version = component.rest[0].inverter.rest_data.get("Stats", {}).get("GivTCP_Version", "Unknown")
    component.rest[0].inverter.rest_v3 = version.startswith("3")
    return base, component


def test_discovery_parsing_against_real_captures(my_predbat=None):
    """
    Battery/capacity discovery parsed from real captured GivTCP responses, v2 and v3.

    These assertions moved here with the parsing itself: Inverter.__init__ used to read the REST
    blob directly and this pinned what it produced, but the version normalisation now lives in
    GivTCPRest. v3 is the interesting case - it renames the Invertor_Details block to the inverter's
    own serial number, and reports nominal capacity in kWh where v2 reports raw register units.
    """
    failed = 0

    base, component = _rest_from_fixture("cases/rest_v2.json")
    rest = component.rest[0]
    checks_v2 = {
        "battery_capacity_kwh": (rest.battery_capacity_kwh(), 9.523200000000001),
        "nominal_capacity": (rest.nominal_capacity(), 9.5232),
        "battery_temperature": (rest.battery_temperature(), 15.3),
        "max_battery_rate": (rest.max_battery_rate(), 2600),
        "max_inverter_rate": (rest.max_inverter_rate(), 3600),
        "in_calibration": (rest.in_calibration(), False),
        # from the flat Battery_Details/<serial> block: 184.82 of a 186.0 Ah design
        "battery_soh": (rest.battery_soh(), 0.9937),
    }
    for name, (got, expected) in checks_v2.items():
        if got != expected:
            print(f"ERROR: v2 {name}: expected {expected}, got {got}")
            failed = 1
    if not rest.inverter_time():
        print("ERROR: v2 inverter_time should be reported")
        failed = 1

    base, component = _rest_from_fixture("cases/rest_v3.json")
    rest = component.rest[0]
    # v3 keeps the detail block under the serial number, so an empty Invertor_Details is expected
    checks_v3 = {
        "battery_capacity_kwh": (rest.battery_capacity_kwh(), 9.52),
        # nested under Battery_Stack_1, and 187.61 of a 186.0 Ah design clamps to full health
        "battery_soh": (rest.battery_soh(), 1.0),
        "battery_temperature": (rest.battery_temperature(), 25.0),
        "max_battery_rate": (rest.max_battery_rate(), 3600),
        "max_inverter_rate": (rest.max_inverter_rate(), 3600),
        "in_calibration": (rest.in_calibration(), False),
    }
    for name, (got, expected) in checks_v3.items():
        if got != expected:
            print(f"ERROR: v3 {name}: expected {expected}, got {got}")
            failed = 1
    if not rest.inverter_time():
        print("ERROR: v3 inverter_time should be reported")
        failed = 1

    if not failed:
        print("PASS: discovery parses correctly from both real GivTCP captures")
    return failed


def test_calibration_detected_per_version(my_predbat=None):
    """
    Calibration is reported differently by version and must be detected either way.

    A calibration cycle deliberately drives the battery outside its normal SoC range, so Predbat
    disables itself while one runs - missing it means planning against a battery that is not
    behaving normally. v3 exposes Control.Battery_Calibration directly; older GivTCP only has the
    raw soc_force_adjust register, where 1-6 means in progress.
    """
    failed = 0

    base, component = _make_component()
    rest = component.rest[0]

    rest.inverter.rest_v3 = True
    for value, expected in [("Off", False), ("On", True), ("Calibrating", True)]:
        rest.inverter.rest_data = {"Control": {"Battery_Calibration": value}}
        if rest.in_calibration() != expected:
            print(f"ERROR: v3 Battery_Calibration={value!r}: expected {expected}, got {rest.in_calibration()}")
            failed = 1

    rest.inverter.rest_v3 = False
    for value, expected in [(0, False), (1, True), (6, True), (7, False), (None, False), ("bad", False)]:
        rest.inverter.rest_data = {"raw": {"invertor": {"soc_force_adjust": value}}}
        if rest.in_calibration() != expected:
            print(f"ERROR: v2 soc_force_adjust={value!r}: expected {expected}, got {rest.in_calibration()}")
            failed = 1

    if not failed:
        print("PASS: calibration is detected on both GivTCP versions")
    return failed


def test_rate_entities_carry_the_real_max(my_predbat=None):
    """
    The rate entities must advertise the inverter's own maximum, not the generic ceiling.

    Inverter.__init__ derives battery_rate_max_raw for a GE inverter from the charge_rate entity's
    "max" attribute. Publishing GIVTCP_CONTROLS' generic 20000 would tell Predbat the battery can
    take 20kW - it only went unnoticed while REST discovery was still overriding it.
    """
    base, component = _rest_from_fixture("cases/rest_v2.json")
    run_async(component.publish_data())

    for entity_id in ("number.predbat_givtcp_0_charge_rate", "number.predbat_givtcp_0_discharge_rate"):
        got = base.entities[entity_id]["attributes"]["max"]
        assert got == 2600, f"{entity_id}: expected max 2600 from Invertor_Max_Bat_Rate, got {got}"

    print("PASS: rate entities advertise the inverter's real maximum rate")
    return 0


def test_arbitrary_minute_time_is_a_valid_option(my_predbat=None):
    """
    The published select options must cover every minute, not a coarser step.

    adjust_charge_window() writes whatever minute the plan lands on (shifted again by
    inverter_clock_skew_*), so a coarse option list would not contain the entity's own value.
    """
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _rest_data_blob(charge_start="23:07:00")
    run_async(component.publish_data())

    entity = base.entities["select.predbat_givtcp_0_charge_start_time"]
    options = entity["attributes"]["options"]
    assert "23:07:00" in options, "An arbitrary minute value must be a valid option"
    assert len(options) == 24 * 60, f"Expected all 1440 minutes as options, got {len(options)}"
    print("PASS: every minute of the day is a valid time option")
    return 0


def test_unknown_entity_write_logged_not_crashed(my_predbat=None):
    """A write event for an entity that doesn't match any known control logs a warning, doesn't raise."""
    base, component = _make_component()
    run_async(component.number_event("number.predbat_givtcp_0_not_a_real_control", 5))
    print("PASS: an unrecognised control entity is handled gracefully")
    return 0


def test_unknown_inverter_index_write_logged_not_crashed(my_predbat=None):
    """A write event for an inverter index beyond the configured count is handled gracefully."""
    base, component = _make_component(rest_urls="http://givtcp:6345")
    run_async(component.number_event("number.predbat_givtcp_5_charge_rate", 5))
    print("PASS: a write event for an out-of-range inverter index is handled gracefully")
    return 0


def test_run_first_call_polls_and_configures(my_predbat=None):
    """The first run() call reads status, publishes it, and runs automatic_config() exactly once."""
    base, component = _make_component()
    component.rest[0].read_data = MagicMock(return_value=_rest_data_blob())

    run_async(component.run(seconds=0, first=True))

    component.rest[0].read_data.assert_called_once()
    assert "number.predbat_givtcp_0_charge_rate" in base.entities, "Expected first run to publish entities"
    assert base.args.get("num_inverters") == 1, "Expected first run to have called automatic_config"
    assert component.automatic_config_done is True

    # A second call to run() must not repeat automatic_config
    component.rest[0].read_data.reset_mock()
    run_async(component.run(seconds=1, first=False))
    assert base.args.get("num_inverters") == 1
    print("PASS: run() polls, publishes and auto-configures exactly once on first call")
    return 0


def test_run_detects_givtcp_version(my_predbat=None):
    """run() sets rest_v3 from the polled Stats.GivTCP_Version, matching Inverter.__init__'s own detection."""
    base, component = _make_component()
    data = _rest_data_blob()
    data["Stats"] = {"GivTCP_Version": "3.2.1"}
    component.rest[0].read_data = MagicMock(return_value=data)

    run_async(component.run(seconds=0, first=True))
    assert component.rest[0].inverter.rest_v3 is True, "Expected rest_v3 True for a 3.x GivTCP_Version"

    data2 = _rest_data_blob()
    data2["Stats"] = {"GivTCP_Version": "2.9.0"}
    component.rest[0].read_data = MagicMock(return_value=data2)
    run_async(component.run(seconds=GIVTCP_POLL_SECONDS, first=False))
    assert component.rest[0].inverter.rest_v3 is False, "Expected rest_v3 False for a 2.x GivTCP_Version"
    print("PASS: run() detects GivTCP's version from Stats.GivTCP_Version")
    return 0


def test_write_event_exception_does_not_propagate(my_predbat=None):
    """
    A failing REST write is logged, not raised into HA's shared event dispatch.

    These handlers are awaited from components.py's dispatch loop, which has no exception guard of
    its own - letting one escape would stop other components seeing the same event.
    """
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _rest_data_blob()
    component.rest[0].set_charge_rate = MagicMock(side_effect=Exception("boom"))

    run_async(component.number_event("number.predbat_givtcp_0_charge_rate", 1500))
    print("PASS: an exception during a write is contained, not propagated")
    return 0


def test_run_reports_failure_until_data_arrives(my_predbat=None):
    """
    run() must report failure while no REST endpoint has returned anything.

    Reporting success would mark the component healthy, skip ComponentBase's retry backoff, and -
    worst - let automatic_config() run once against entities that were never published, replacing
    the user's working apps.yaml entity config with unavailable ones.
    """
    base, component = _make_component()
    component.rest[0].read_data = MagicMock(return_value=None)

    result = run_async(component.run(seconds=0, first=True))
    assert result is False, "run() must report failure when nothing could be read"
    assert component.automatic_config_done is False, "automatic_config must not run without data"
    assert "num_inverters" not in base.args, "apps.yaml keys must be left alone until data is available"

    # Once GivTCP responds, the component recovers and configures itself
    component.rest[0].read_data = MagicMock(return_value=_rest_data_blob())
    result = run_async(component.run(seconds=0, first=True))
    assert result is True, "run() should report success once data has been read"
    assert base.args.get("num_inverters") == 1, "Expected automatic_config to run once data arrived"
    print("PASS: run() reports failure until data arrives, then configures")
    return 0


def test_automatic_config_respects_power_ignore(my_predbat=None):
    """
    givtcp_rest_power_ignore leaves the power/voltage keys to the user's own apps.yaml config.

    It is the documented opt-out for setups whose GivTCP power readings are wrong (typically
    multi-inverter systems with their own combined sensors) - claiming those keys would override
    exactly the config it exists to protect.
    """
    base, component = _make_component()
    _mark_discovered(component)
    base.args["givtcp_rest_power_ignore"] = True
    run_async(component.automatic_config())

    assert "pv_power" not in base.args, "pv_power must be left to the user when power_ignore is set"
    assert "battery_power" not in base.args, "battery_power must be left to the user when power_ignore is set"
    # Non-power keys are still claimed as usual
    assert base.args["charge_rate"] == ["number.predbat_givtcp_0_charge_rate"]
    print("PASS: givtcp_rest_power_ignore leaves power entities to the user's config")
    return 0


def test_automatic_config_uses_soc_kw_not_percent(my_predbat=None):
    """
    SoC is bound via soc_kw, not soc_percent.

    Inverter.update_status() prefers soc_percent when both are set, but GivTCP reports SOC only as
    a whole percent (~0.1kWh steps on a 9.5kWh battery) while SOC_kWh carries 3 decimal places.
    """
    base, component = _make_component()
    _mark_discovered(component)
    run_async(component.automatic_config())

    assert base.args["soc_kw"] == ["sensor.predbat_givtcp_0_soc_kw"], f"Expected soc_kw to be bound, got {base.args.get('soc_kw')}"
    assert "soc_percent" not in base.args, "soc_percent must not be bound - it would take precedence and lose precision"
    print("PASS: SoC is bound via the precise soc_kw sensor, not whole-percent soc_percent")
    return 0


def test_automatic_config_counts_discovered_inverters_not_configured_urls(my_predbat=None):
    """
    num_inverters comes from how many endpoints answered, not how many URLs are configured.

    The shipped apps.yaml pairs num_inverters: 1 with a two-entry givtcp_rest list, the same
    over-provisioning every other per-inverter key in that template uses. Counting the list would
    build Inverter(id=1) against a host that does not exist and plan against a phantom battery.
    """
    base, component = _make_component(rest_urls=["http://givtcp0:6345", "http://givtcp1:6345"])
    component.rest[0].read_data = MagicMock(return_value=_rest_data_blob())
    component.rest[1].read_data = MagicMock(return_value=None)

    result = run_async(component.run(seconds=0, first=True))
    assert result is True, "run() should succeed when at least one endpoint answered"
    assert base.args["num_inverters"] == 1, f"Expected num_inverters 1 from one live endpoint, got {base.args.get('num_inverters')}"
    assert base.args["inverter_type"] == ["GE"], f"Expected one inverter_type entry, got {base.args.get('inverter_type')}"
    assert base.args["charge_rate"] == ["number.predbat_givtcp_0_charge_rate"], f"Expected only the live endpoint's entity, got {base.args.get('charge_rate')}"
    print("PASS: num_inverters counts discovered inverters, not configured URLs")
    return 0


def test_automatic_config_maps_predbat_inverter_to_the_live_endpoint(my_predbat=None):
    """
    When an earlier endpoint is dead, the surviving one still drives Predbat inverter 0.

    Entity ids stay pinned to the REST endpoint index because _parse_entity feeds self.rest[n] on
    every write - renumbering them would route inverter 0's writes at the dead client.
    """
    base, component = _make_component(rest_urls=["http://dead:6345", "http://givtcp1:6345"])
    component.rest[0].read_data = MagicMock(return_value=None)
    component.rest[1].read_data = MagicMock(return_value=_rest_data_blob())

    run_async(component.run(seconds=0, first=True))
    assert base.args["num_inverters"] == 1, f"Expected num_inverters 1, got {base.args.get('num_inverters')}"
    assert base.args["charge_rate"] == ["number.predbat_givtcp_1_charge_rate"], f"Expected the live endpoint's own index, got {base.args.get('charge_rate')}"

    # and that entity must still route a write back to the live client
    n, control = component._parse_entity(base.args["charge_rate"][0])
    assert component.rest[n] is component.rest[1], "Predbat inverter 0's entity must address the live REST client"
    print("PASS: a dead leading endpoint leaves the live one driving Predbat inverter 0")
    return 0


def test_automatic_config_skipped_when_nothing_was_discovered(my_predbat=None):
    """automatic_config() must claim nothing at all when no endpoint answered."""
    base, component = _make_component(rest_urls=["http://givtcp0:6345", "http://givtcp1:6345"])
    run_async(component.automatic_config())

    assert "num_inverters" not in base.args, f"Expected no config with nothing discovered, got num_inverters={base.args.get('num_inverters')}"
    assert "charge_rate" not in base.args, "Expected the user's own apps.yaml keys left untouched"
    print("PASS: automatic_config claims nothing when no inverter was discovered")
    return 0


def test_dead_endpoint_is_not_polled_after_discovery(my_predbat=None):
    """
    Once discovery has settled, an endpoint that never answered is left out of the normal poll.

    read_data() retries with 20s then 40s sleeps, so probing the template's placeholder URL on the
    60s poll cadence would spend longer failing than the interval it is running on. It is still
    re-probed on the much slower GIVTCP_REDISCOVER_SECONDS boundary, cheaply - see
    test_rediscovery_uses_a_cheap_single_probe.
    """
    base, component = _make_component(rest_urls=["http://givtcp0:6345", "http://dead:6345"])
    component.rest[0].read_data = MagicMock(return_value=_rest_data_blob())
    component.rest[1].read_data = MagicMock(return_value=None)

    run_async(component.run(seconds=0, first=True))
    assert component.rest[1].read_data.call_count == 1, "the dead endpoint should be probed once during discovery"

    # a normal poll tick, deliberately not a re-probe boundary
    run_async(component.run(seconds=GIVTCP_POLL_SECONDS, first=False))
    assert component.rest[1].read_data.call_count == 1, f"the dead endpoint must not be polled on the normal cadence, got {component.rest[1].read_data.call_count} calls"
    assert component.rest[0].read_data.call_count == 2, f"the live endpoint must keep being polled, got {component.rest[0].read_data.call_count} calls"
    print("PASS: a dead endpoint is left out of the normal poll cadence after discovery")
    return 0


def test_pause_keys_gated_on_discovered_inverters_only(my_predbat=None):
    """
    The all-v3 pause gate considers discovered inverters only.

    An endpoint that never answered has rest_v3 False by default, so counting it would withhold
    pause control from a fleet whose live inverters are all v3.
    """
    base, component = _make_component(rest_urls=["http://givtcp0:6345", "http://dead:6345"])
    live = _rest_data_blob()
    live["Stats"] = {"GivTCP_Version": "3.2.1"}
    component.rest[0].read_data = MagicMock(return_value=live)
    component.rest[1].read_data = MagicMock(return_value=None)

    run_async(component.run(seconds=0, first=True))
    assert base.args.get("pause_mode") == ["select.predbat_givtcp_0_pause_mode"], f"Expected pause claimed for the live v3 inverter, got {base.args.get('pause_mode')}"
    print("PASS: the v3 pause gate ignores endpoints that were never discovered")
    return 0


def test_rediscovery_picks_up_an_inverter_that_was_down_at_startup(my_predbat=None):
    """
    An endpoint that was unreachable at startup is re-probed and adopted when it comes back.

    automatic_config() only ever ran once, so before this a GivTCP restart or a network blip
    during Predbat startup cost the user that inverter until they restarted Predbat.
    """
    base, component = _make_component(rest_urls=["http://givtcp0:6345", "http://givtcp1:6345"])
    component.rest[0].read_data = MagicMock(return_value=_rest_data_blob())
    component.rest[1].read_data = MagicMock(return_value=None)

    run_async(component.run(seconds=0, first=True))
    assert base.args["num_inverters"] == 1, f"Expected 1 inverter at startup, got {base.args.get('num_inverters')}"

    # the second inverter comes back, and the hourly re-probe finds it
    component.rest[1].read_data = MagicMock(return_value=_rest_data_blob())
    run_async(component.run(seconds=GIVTCP_REDISCOVER_SECONDS, first=False))

    assert component.discovered == [0, 1], f"Expected both endpoints discovered, got {component.discovered}"
    assert base.args["num_inverters"] == 2, f"Expected automatic_config re-run for 2 inverters, got {base.args.get('num_inverters')}"
    assert base.args["charge_rate"] == ["number.predbat_givtcp_0_charge_rate", "number.predbat_givtcp_1_charge_rate"]
    print("PASS: an inverter that was down at startup is adopted on re-probe")
    return 0


def test_rediscovery_appends_so_running_inverters_keep_their_identity(my_predbat=None):
    """
    A late arrival is appended, never inserted.

    self.discovered's order *is* Predbat's inverter numbering. Inserting endpoint 0 ahead of the
    endpoint already running as inverter 0 would silently repoint inverter 0 at different physical
    hardware - its SoC, rates and charge windows would start following the wrong battery.
    """
    base, component = _make_component(rest_urls=["http://dead:6345", "http://givtcp1:6345"])
    component.rest[0].read_data = MagicMock(return_value=None)
    component.rest[1].read_data = MagicMock(return_value=_rest_data_blob())

    run_async(component.run(seconds=0, first=True))
    assert component.discovered == [1], f"Expected only endpoint 1 discovered, got {component.discovered}"
    first_inverter_entity = base.args["charge_rate"][0]

    component.rest[0].read_data = MagicMock(return_value=_rest_data_blob())
    run_async(component.run(seconds=GIVTCP_REDISCOVER_SECONDS, first=False))

    assert component.discovered == [1, 0], f"Expected the late endpoint appended, got {component.discovered}"
    assert base.args["charge_rate"][0] == first_inverter_entity, "Predbat inverter 0 must keep addressing the same physical inverter"
    assert base.args["charge_rate"] == ["number.predbat_givtcp_1_charge_rate", "number.predbat_givtcp_0_charge_rate"]
    assert base.args["num_inverters"] == 2
    print("PASS: a late inverter is appended, leaving running inverter identities untouched")
    return 0


def test_rediscovery_never_drops_an_inverter_that_stops_answering(my_predbat=None):
    """
    Losing a discovered inverter is a health problem, not a reconfiguration.

    Shrinking num_inverters would rebuild Predbat's inverter list for a smaller fleet and leave a
    real battery uncontrolled at whatever settings it last had, then thrash when it returned.
    Leaving it in place makes Inverter.__init__ fail loudly instead.
    """
    base, component = _make_component(rest_urls=["http://givtcp0:6345", "http://givtcp1:6345"])
    for rest in component.rest:
        rest.read_data = MagicMock(return_value=_rest_data_blob())

    run_async(component.run(seconds=0, first=True))
    assert component.discovered == [0, 1] and base.args["num_inverters"] == 2

    # inverter 1 goes offline and stays offline across a re-probe boundary
    component.rest[1].read_data = MagicMock(return_value=None)
    run_async(component.run(seconds=GIVTCP_REDISCOVER_SECONDS, first=False))

    assert component.discovered == [0, 1], f"A discovered inverter must never be dropped, got {component.discovered}"
    assert base.args["num_inverters"] == 2, f"num_inverters must not shrink, got {base.args.get('num_inverters')}"
    print("PASS: an inverter that stops answering is kept, not silently dropped")
    return 0


def test_rediscovery_uses_a_cheap_single_probe(my_predbat=None):
    """
    Re-probing must not use read_data()'s retry ladder.

    Startup discovery keeps the 20s/40s retries because GivTCP may still be booting, but an hourly
    re-probe of a placeholder URL that will never answer has to cost one GET, not ~100s of sleeps.
    """
    base, component = _make_component(rest_urls=["http://givtcp0:6345", "http://dead:6345"])
    component.rest[0].read_data = MagicMock(return_value=_rest_data_blob())
    component.rest[1].read_data = MagicMock(return_value=None)

    run_async(component.run(seconds=0, first=True))
    assert component.rest[1].read_data.call_args == (("readData",), {}) or component.rest[1].read_data.call_args == ((), {}), f"Discovery should use the retrying read, got {component.rest[1].read_data.call_args}"

    component.rest[1].read_data.reset_mock()
    run_async(component.run(seconds=GIVTCP_REDISCOVER_SECONDS, first=False))
    assert component.rest[1].read_data.call_count == 1, f"Expected exactly one re-probe, got {component.rest[1].read_data.call_count}"
    assert component.rest[1].read_data.call_args == (("readData", False), {}), f"Re-probe must disable the retry ladder, got {component.rest[1].read_data.call_args}"
    print("PASS: re-probing uses a single cheap GET, not the retry ladder")
    return 0


def test_rediscovery_skipped_once_every_endpoint_is_discovered(my_predbat=None):
    """No re-probe work at all when every configured endpoint is already being managed."""
    base, component = _make_component(rest_urls=["http://givtcp0:6345", "http://givtcp1:6345"])
    for rest in component.rest:
        rest.read_data = MagicMock(return_value=_rest_data_blob())

    run_async(component.run(seconds=0, first=True))
    calls = [rest.read_data.call_count for rest in component.rest]

    run_async(component.run(seconds=GIVTCP_REDISCOVER_SECONDS, first=False))
    # exactly one extra poll each - the normal poll, with no additional re-probe on top
    assert [rest.read_data.call_count for rest in component.rest] == [c + 1 for c in calls], "Expected no extra re-probe when the fleet is complete"
    print("PASS: no re-probe once every configured endpoint is discovered")
    return 0


def _capture_logs(component):
    """
    Collect log lines so a test can assert on what the component reported.

    Patches the component's own bound log rather than base.log - ComponentBase copies base.log
    into self.log at construction, so replacing it on the base afterwards has no effect.
    """
    messages = []
    component.log = lambda message, quiet=True: messages.append(str(message))
    return messages


def test_failed_poll_withholds_the_success_timestamp_and_warns(my_predbat=None):
    """
    A failed poll must not refresh the success timestamp.

    read_data() leaves the previous snapshot in place on failure, so publish_data() keeps
    republishing frozen values with a fresh HA last_updated. Nothing about the entities themselves
    reveals that GivTCP died. Withholding the timestamp lets ComponentManager's 60 minute staleness
    check (components.py) put the component into error instead.
    """
    base, component = _make_component()
    component.rest[0].read_data = MagicMock(return_value=_rest_data_blob())
    run_async(component.run(seconds=0, first=True))
    healthy_at = component.last_success_timestamp
    assert healthy_at is not None, "a successful poll should record a success timestamp"

    component.rest[0].read_data = MagicMock(return_value=None)
    messages = _capture_logs(component)
    result = run_async(component.run(seconds=GIVTCP_POLL_SECONDS, first=False))

    assert result is True, "a failed poll should not restart the component - the health timeout handles it"
    assert component.last_success_timestamp == healthy_at, "the success timestamp must not advance on a failed poll"
    assert any("did not respond" in m for m in messages), f"Expected a warning naming the unresponsive inverter, got {messages}"
    print("PASS: a failed poll warns and withholds the success timestamp")
    return 0


def test_success_timestamp_resumes_once_the_poll_recovers(my_predbat=None):
    """Once GivTCP answers again the component reports healthy without needing a restart."""
    base, component = _make_component()
    component.rest[0].read_data = MagicMock(return_value=_rest_data_blob())
    run_async(component.run(seconds=0, first=True))

    component.rest[0].read_data = MagicMock(return_value=None)
    run_async(component.run(seconds=GIVTCP_POLL_SECONDS, first=False))
    stalled_at = component.last_success_timestamp

    component.rest[0].read_data = MagicMock(return_value=_rest_data_blob())
    run_async(component.run(seconds=GIVTCP_POLL_SECONDS * 2, first=False))

    assert component.last_success_timestamp > stalled_at, "the success timestamp should advance again after recovery"
    print("PASS: the success timestamp resumes once polling recovers")
    return 0


def test_one_failing_inverter_withholds_success_for_the_whole_component(my_predbat=None):
    """
    A partly-dead fleet is still a degraded component.

    Predbat cannot control the battery behind the endpoint that stopped answering, and its entities
    are frozen, so reporting the component healthy would hide a real loss of control.
    """
    base, component = _make_component(rest_urls=["http://givtcp0:6345", "http://givtcp1:6345"])
    for rest in component.rest:
        rest.read_data = MagicMock(return_value=_rest_data_blob())
    run_async(component.run(seconds=0, first=True))
    healthy_at = component.last_success_timestamp

    component.rest[1].read_data = MagicMock(return_value=None)
    run_async(component.run(seconds=GIVTCP_POLL_SECONDS, first=False))

    assert component.last_success_timestamp == healthy_at, "one failing inverter must withhold the whole component's success"
    print("PASS: one failing inverter withholds success for the whole component")
    return 0


def test_an_endpoint_never_discovered_does_not_withhold_success(my_predbat=None):
    """
    A URL that never had an inverter behind it is not a failure.

    The shipped apps.yaml over-provisions givtcp_rest, so a placeholder entry is the normal case -
    holding the component in error for it would make the default config permanently unhealthy.
    """
    base, component = _make_component(rest_urls=["http://givtcp0:6345", "http://placeholder:6345"])
    component.rest[0].read_data = MagicMock(return_value=_rest_data_blob())
    component.rest[1].read_data = MagicMock(return_value=None)
    run_async(component.run(seconds=0, first=True))
    first_success = component.last_success_timestamp
    assert first_success is not None, "discovery of one live inverter should report success"

    run_async(component.run(seconds=GIVTCP_POLL_SECONDS, first=False))
    assert component.last_success_timestamp > first_success, "the never-discovered placeholder must not withhold success"
    print("PASS: an endpoint that never had an inverter does not hold the component in error")
    return 0


def test_charge_limit_enable_switch_is_published(my_predbat=None):
    """
    The Enable_Charge_Target register (reg 20) is published as a switch.

    GivTCP's setChargeTarget writes CHARGE_TARGET_SOC (reg 116) but never enables reg 20, and with
    reg 20 off - GivTCP's default - the inverter ignores the SOC limit and charges to 100%. That
    was the root cause of Hold Charge not holding on AIO inverters (#4141).
    """
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _rest_data_blob(charge_target_enable="enable")
    run_async(component.publish_data())
    assert base.entities["switch.predbat_givtcp_0_charge_limit_enable"]["state"] == "on", f"Expected the enable switch on, got {base.entities.get('switch.predbat_givtcp_0_charge_limit_enable')}"

    component.rest[0].inverter.rest_data = _rest_data_blob(charge_target_enable="disable")
    run_async(component.publish_data())
    assert base.entities["switch.predbat_givtcp_0_charge_limit_enable"]["state"] == "off", "Expected the enable switch to follow the register"
    print("PASS: the charge target enable register is published as a switch")
    return 0


def test_charge_limit_enable_is_auto_configured(my_predbat=None):
    """
    automatic_config claims charge_limit_enable, which is what closes #4141 for REST users.

    Inverter.adjust_battery_target already writes charge_limit_enable straight after charge_limit
    when the key resolves - it was simply never populated for a GivTCP REST user, so the enable
    step main performed via rest_enableChargeTarget() was silently lost.
    """
    base, component = _make_component()
    _mark_discovered(component)
    component.rest[0].inverter.rest_data = _rest_data_blob()
    run_async(component.automatic_config())

    assert base.args["charge_limit_enable"] == ["switch.predbat_givtcp_0_charge_limit_enable"], f"Expected charge_limit_enable claimed, got {base.args.get('charge_limit_enable')}"
    print("PASS: charge_limit_enable is auto-configured so Inverter enables the target")
    return 0


def test_charge_limit_enable_write_hits_the_enable_register(my_predbat=None):
    """A write to the enable switch calls enable_charge_target, not set_charge_target."""
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _rest_data_blob()
    component.rest[0].enable_charge_target = MagicMock(return_value=True)

    run_async(component.switch_event("switch.predbat_givtcp_0_charge_limit_enable", "turn_on"))
    component.rest[0].enable_charge_target.assert_called_once_with(True)

    component.rest[0].enable_charge_target.reset_mock()
    run_async(component.switch_event("switch.predbat_givtcp_0_charge_limit_enable", "turn_off"))
    component.rest[0].enable_charge_target.assert_called_once_with(False)
    print("PASS: the enable switch writes the charge target enable register")
    return 0


def test_charge_limit_enable_withheld_when_the_register_is_absent(my_predbat=None):
    """
    A GivTCP that does not report Enable_Charge_Target gets no switch and no claimed key.

    enable_charge_target() verifies the write by reading that field back, so publishing a control
    for a register GivTCP never reports would burn the full retry ladder and record_status on every
    charge limit change. Claiming only what is actually published also leaves the user's own
    apps.yaml charge_limit_enable in place as the fallback.
    """
    base, component = _make_component()
    _mark_discovered(component)
    blob = _rest_data_blob()
    del blob["Control"]["Enable_Charge_Target"]
    component.rest[0].inverter.rest_data = blob

    run_async(component.publish_data())
    assert "switch.predbat_givtcp_0_charge_limit_enable" not in base.entities, "No enable switch should be published when the register is not reported"

    run_async(component.automatic_config())
    assert "charge_limit_enable" not in base.args, f"charge_limit_enable must be left to the user, got {base.args.get('charge_limit_enable')}"
    # the rest of the config is unaffected
    assert base.args["charge_limit"] == ["number.predbat_givtcp_0_charge_limit"]
    print("PASS: the enable switch is withheld when GivTCP does not report the register")
    return 0


def _capacity_blob(design_kwh=9.5232, full_ah=184.82, design_ah=186.0, v3=False):
    """
    A status blob shaped like the real captures in coverage/cases/rest_v{2,3}.json.

    Battery_Capacity_kWh is the DESIGN capacity (186Ah x 51.2V / 1000); state of health comes from
    the per-module Battery_Capacity vs Battery_Design_Capacity under Battery_Details, which is flat
    on v2 and nested under Battery_Stack_N on v3.
    """
    blob = _rest_data_blob()
    blob["Invertor_Details"] = {"Battery_Capacity_kWh": design_kwh}
    module = {"Battery_Capacity": full_ah, "Battery_Design_Capacity": design_ah}
    if v3:
        blob["Battery_Details"] = {"Battery_Stack_1": {"BMS_Temperature": 25.0, "BMS_Voltage": 53.65, "DF2234G370": module}}
    else:
        blob["Battery_Details"] = {"DF2228G115": module}
    return blob


def test_soc_max_carries_the_design_capacity(my_predbat=None):
    """
    soc_max is the design (nameplate) capacity, and health is expressed through battery_scaling.

    Inverter computes soc_max = nominal_capacity * battery_scaling and derives degradation from
    nominal_capacity, so nominal has to be the design figure. Publishing the already-degraded
    reported capacity as soc_max makes trimmed_mean/nominal collapse to ~1.0 and hides the
    degradation battery_scaling_auto exists to measure.
    """
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _capacity_blob()
    run_async(component.publish_data())

    assert base.entities["sensor.predbat_givtcp_0_soc_max"]["state"] == 9.5232, f"soc_max should be the design capacity, got {base.entities['sensor.predbat_givtcp_0_soc_max']['state']}"
    print("PASS: soc_max carries the design capacity, not the degraded reported one")
    return 0


def test_battery_soh_dod_and_combined_scaling_are_published(my_predbat=None):
    """
    SoH, DoD and their product are published, mirroring GE Cloud's battery_dod_soh.

    Inverter applies exactly one scaling factor, so the combined sensor is what battery_scaling
    must point at - publishing only the parts would make the caller choose, and the true usable
    size is design x soh x dod.
    """
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _capacity_blob(full_ah=184.82, design_ah=186.0)
    run_async(component.publish_data())

    # the same battery, and the same ratio, GE Cloud's own test uses
    assert base.entities["sensor.predbat_givtcp_0_battery_soh"]["state"] == 0.9937, f"Expected soh 184.82/186, got {base.entities['sensor.predbat_givtcp_0_battery_soh']['state']}"
    assert base.entities["sensor.predbat_givtcp_0_battery_dod"]["state"] == 1.0, "DoD defaults to 1.0 - a full readData dump reports none"
    assert base.entities["sensor.predbat_givtcp_0_battery_dod_soh"]["state"] == 0.9937, f"Expected combined 0.9937, got {base.entities['sensor.predbat_givtcp_0_battery_dod_soh']['state']}"
    print("PASS: soh, dod and the combined scaling factor are published")
    return 0


def test_battery_dod_override_is_folded_into_the_scaling(my_predbat=None):
    """givtcp_battery_dod lets a user supply a depth of discharge GivTCP does not report."""
    base, component = _make_component()
    base.args["givtcp_battery_dod"] = 0.8
    component.rest[0].inverter.rest_data = _capacity_blob(full_ah=180.0, design_ah=200.0)
    run_async(component.publish_data())

    assert base.entities["sensor.predbat_givtcp_0_battery_dod"]["state"] == 0.8
    assert base.entities["sensor.predbat_givtcp_0_battery_dod_soh"]["state"] == 0.72, f"Expected 0.9 * 0.8 = 0.72, got {base.entities['sensor.predbat_givtcp_0_battery_dod_soh']['state']}"
    print("PASS: a DoD override is folded into the combined scaling factor")
    return 0


def test_battery_scaling_is_auto_configured_from_the_combined_sensor(my_predbat=None):
    """
    battery_scaling points at the combined sensor, via set_arg_auto.

    set_arg_auto rather than set_arg so a user who set battery_scaling by hand gets the one-time
    note that auto-discovery has displaced it, instead of it changing silently.
    """
    base, component = _make_component()
    _mark_discovered(component)
    component.rest[0].inverter.rest_data = _capacity_blob()
    run_async(component.publish_data())
    run_async(component.automatic_config())

    assert base.args["battery_scaling"] == ["sensor.predbat_givtcp_0_battery_dod_soh"], f"Expected battery_scaling bound to the combined sensor, got {base.args.get('battery_scaling')}"
    assert base.args["soc_max"] == ["sensor.predbat_givtcp_0_soc_max"]
    print("PASS: battery_scaling is auto-configured from the combined dod/soh sensor")
    return 0


def test_no_design_capacity_falls_back_to_the_reported_one(my_predbat=None):
    """
    Without per-module Battery_Details there is no health figure, so soc_max is still published but
    no scaling is claimed - rather than displacing the user's own battery_scaling with a fabricated
    1.0, which is exactly what deriving SoH from Battery_Capacity_kWh would have produced.
    """
    base, component = _make_component()
    _mark_discovered(component)
    blob = _capacity_blob()
    del blob["Battery_Details"]
    component.rest[0].inverter.rest_data = blob

    run_async(component.publish_data())
    assert base.entities["sensor.predbat_givtcp_0_soc_max"]["state"] == 9.5232, "soc_max is still the design capacity"
    assert "sensor.predbat_givtcp_0_battery_soh" not in base.entities, "No SoH without per-module capacities"

    run_async(component.automatic_config())
    assert "battery_scaling" not in base.args, f"battery_scaling must be left to the user, got {base.args.get('battery_scaling')}"
    print("PASS: a missing per-module capacity claims no scaling")
    return 0


def test_rate_write_tolerance_uses_the_real_max_rate(my_predbat=None):
    """
    The write-verification tolerance is sized from the discovered max battery rate.

    It used to come from an InverterRestState placeholder of 1.0, giving
    1.0 * MINUTE_WATT / 12 = 5000W instead of 2600/12 = 217W - so a rate the inverter never applied
    verified as successful, logged as such, and counted as a register write.
    """
    base, component = _make_component()
    blob = _rest_data_blob(charge_rate=3000)
    blob["Invertor_Details"] = {"Invertor_Max_Bat_Rate": 2600}
    component.rest[0].inverter.rest_data = blob
    component.rest[0].post_command = MagicMock(return_value=None)
    component.rest[0].run_all = MagicMock(return_value=blob)

    # asking for 200W while the inverter stays at 3000W is a 2800W miss: inside the old 5000W
    # tolerance, well outside the correct 217W one
    assert component.rest[0].set_charge_rate(200) is False, "A 2800W miss must not verify as a successful write"
    print("PASS: rate write tolerance is sized from the real max battery rate")
    return 0


def test_battery_soh_reads_the_v3_stacked_layout(my_predbat=None):
    """v3 nests the battery modules one level deeper, under Battery_Stack_N."""
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _capacity_blob(full_ah=180.0, design_ah=200.0, v3=True)
    run_async(component.publish_data())

    assert base.entities["sensor.predbat_givtcp_0_battery_soh"]["state"] == 0.9, f"Expected the stacked layout to be walked, got {base.entities.get('sensor.predbat_givtcp_0_battery_soh')}"
    print("PASS: state of health is read from the v3 stacked battery layout")
    return 0


def test_battery_soh_is_clamped_at_full_health(my_predbat=None):
    """
    A pack reporting above its nameplate is clamped to 1.0.

    The real v3 capture does exactly this - 187.61 against a 186.0 design - and treating it as
    106% would have Predbat plan to use capacity the battery does not have.
    """
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _capacity_blob(full_ah=187.61, design_ah=186.0, v3=True)
    run_async(component.publish_data())

    assert base.entities["sensor.predbat_givtcp_0_battery_soh"]["state"] == 1.0, f"Expected clamp to 1.0, got {base.entities['sensor.predbat_givtcp_0_battery_soh']['state']}"
    print("PASS: state of health above nameplate is clamped to 1.0")
    return 0


def test_soh_is_not_derived_from_the_two_design_figures(my_predbat=None):
    """
    Regression guard: Battery_Capacity_kWh and battery_nominal_capacity are the SAME design figure
    in different units (Ah / 19.53125 == kWh at 51.2V), so their ratio is always 1.0. Deriving SoH
    from them yields a constant 1.0 that would silently displace a user's own battery_scaling.
    """
    base, component = _make_component()
    blob = _capacity_blob()
    blob["raw"] = {"invertor": {"battery_nominal_capacity": 186.0}}
    del blob["Battery_Details"]
    component.rest[0].inverter.rest_data = blob

    assert component.rest[0].battery_soh() is None, "SoH must not be derived from two spellings of the design capacity"
    print("PASS: SoH is not derived from the two equivalent design figures")
    return 0


def test_rate_entities_omit_max_when_the_rate_is_unknown(my_predbat=None):
    """
    With no reported maximum rate, no "max" attribute is published at all.

    Inverter derives battery_rate_max_raw for a GE inverter from this attribute
    (get_arg("charge_rate", attribute="max", default=2600.0)), and an absent attribute returns that
    2600 default - which is exactly what main fell back to when REST reported no rate. Publishing
    GIVTCP_CONTROLS' generic 20000 ceiling instead tells Predbat the battery can take 20kW, and
    battery_rate_max_charge/discharge/export are all sized from it.

    inverter_details() returns {} whenever neither Invertor_Details nor the v3 serial-named block
    resolves, so this is the same path that loses capacity, inverter limit and time together.
    """
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _rest_data_blob()
    assert component.rest[0].max_battery_rate() is None, "fixture should report no max battery rate"
    run_async(component.publish_data())

    for entity_id in ("number.predbat_givtcp_0_charge_rate", "number.predbat_givtcp_0_discharge_rate"):
        attributes = base.entities[entity_id]["attributes"]
        assert "max" not in attributes, f"{entity_id}: expected no max when the rate is unknown, got {attributes.get('max')}"

    print("PASS: no max is advertised when GivTCP reports no maximum rate")
    return 0


def test_discovery_keys_are_only_claimed_when_actually_published(my_predbat=None):
    """
    A discovery key is claimed only if its sensor was published.

    publish_data emits soc_max/battery_temperature/inverter_time/inverter_limit/battery_calibration
    only when GivTCP reports them, and its comment says a missing one "falls back to the user's own
    apps.yaml value". It could not: automatic_config claimed every key unconditionally, so the arg
    pointed at an entity that was never created. get_arg then returns its default, and for soc_max
    that is 0.0 - which walks Inverter through battery_scaling_auto to the "Unable to determine
    battery size ... using 8 kWh default" fallback, whose advice is to set soc_max in apps.yaml.
    The user may well have done exactly that, and auto-config overwrote it.
    """
    base, component = _make_component()
    _mark_discovered(component)
    base.args["soc_max"] = [12.0]
    # a status blob with no Invertor_Details and no raw nominal: nothing to discover
    component.rest[0].inverter.rest_data = _rest_data_blob()

    run_async(component.publish_data())
    assert "sensor.predbat_givtcp_0_soc_max" not in base.entities, "fixture should publish no soc_max"

    run_async(component.automatic_config())
    assert base.args["soc_max"] == [12.0], f"soc_max must be left as the user configured it, got {base.args.get('soc_max')}"
    assert "inverter_time" not in base.args, f"inverter_time must not be claimed unpublished, got {base.args.get('inverter_time')}"
    # battery_calibration is the exception: in_calibration() returns a definite False when GivTCP
    # reports nothing, so the sensor is always published and "not calibrating" is the correct
    # default - unlike soc_max, where a default is actively wrong.
    assert base.args["battery_calibration"] == ["sensor.predbat_givtcp_0_battery_calibration"], f"battery_calibration is always published, so it should still be claimed, got {base.args.get('battery_calibration')}"
    # controls are unaffected - those are always published
    assert base.args["charge_rate"] == ["number.predbat_givtcp_0_charge_rate"]
    print("PASS: discovery keys are claimed only when their sensor was published")
    return 0


def test_discovery_keys_still_claimed_when_reported(my_predbat=None):
    """The normal case is unchanged: a reported discovery value is still auto-configured."""
    base, component = _rest_from_fixture("cases/rest_v2.json")
    _mark_discovered(component)
    run_async(component.publish_data())
    run_async(component.automatic_config())

    assert base.args["soc_max"] == ["sensor.predbat_givtcp_0_soc_max"], f"Expected soc_max claimed, got {base.args.get('soc_max')}"
    assert base.args["inverter_limit"] == ["sensor.predbat_givtcp_0_inverter_limit"]
    assert base.args["battery_temperature"] == ["sensor.predbat_givtcp_0_battery_temperature"]
    print("PASS: reported discovery values are still auto-configured")
    return 0


def test_slot_write_before_any_status_is_refused_not_fabricated(my_predbat=None):
    """
    A window write with no status snapshot must be refused, not completed with a made-up end.

    /setChargeSlot1 takes both ends, so the component fills in the one it was not given from
    rest_data. With no snapshot it defaulted that to "00:00:00" - programming a zero-length or
    midnight-terminated window on real hardware. There is no safe default for the half of a window
    you do not know.

    Reachable after a user restarts the component from the web UI (web.py): restart() re-runs
    initialize(), so rest_data is None again, while Predbat's args still point at these entities
    from the previous automatic_config.
    """
    base, component = _make_component()
    component.rest[0].inverter.rest_data = None
    component.rest[0].set_charge_slot1 = MagicMock(return_value=True)
    component.rest[0].set_discharge_slot1 = MagicMock(return_value=True)
    messages = _capture_logs(component)

    run_async(component.select_event("select.predbat_givtcp_0_charge_start_time", "09:00:00"))
    component.rest[0].set_charge_slot1.assert_not_called()

    run_async(component.select_event("select.predbat_givtcp_0_discharge_end_time", "19:00:00"))
    component.rest[0].set_discharge_slot1.assert_not_called()

    assert any("no status" in m for m in messages), f"Expected the refusal to be logged, got {messages}"
    print("PASS: a slot write with no status snapshot is refused rather than fabricated")
    return 0


def test_slot_write_still_works_once_status_is_known(my_predbat=None):
    """The normal path is unchanged: the other end comes from the last status snapshot."""
    base, component = _make_component()
    component.rest[0].inverter.rest_data = _rest_data_blob(charge_start="00:30:00", charge_end="04:30:00")
    component.rest[0].set_charge_slot1 = MagicMock(return_value=True)

    run_async(component.select_event("select.predbat_givtcp_0_charge_start_time", "01:00:00"))
    component.rest[0].set_charge_slot1.assert_called_once_with("01:00:00", "04:30:00")
    print("PASS: a slot write preserves the other end from the last status")
    return 0


def test_discharge_target_not_published_on_v2(my_predbat=None):
    """
    GivTCP v2 has no /setDischargeTarget endpoint, so no entity is published for it.

    main gated the whole export-target block on "self.rest_data and self.rest_v3". Dropping the
    version half means a v2 install publishes the control - raw.invertor.discharge_target_soc_1 is
    present in real v2 captures, and read_discharge_target() returns 0 rather than None - so every
    force-export cycle POSTs to an endpoint that does not exist, burning the retry ladder and
    recording an error each time. That is the every-cycle rewrite loop #4517 was meant to end.
    """
    base, component = _rest_from_fixture("cases/rest_v2.json")
    component.rest[0].inverter.rest_v3 = False
    run_async(component.publish_data())

    assert "number.predbat_givtcp_0_discharge_target_soc" not in base.entities, "v2 must not publish a discharge target control"

    _mark_discovered(component)
    run_async(component.automatic_config())
    assert "discharge_target_soc" not in base.args, f"v2 must not claim discharge_target_soc, got {base.args.get('discharge_target_soc')}"
    print("PASS: no discharge target control on GivTCP v2")
    return 0


def test_discharge_target_still_published_on_v3(my_predbat=None):
    """v3 does have the endpoint, so the control is published and claimed as before."""
    base, component = _rest_from_fixture("cases/rest_v3.json")
    component.rest[0].inverter.rest_v3 = True
    run_async(component.publish_data())

    assert "number.predbat_givtcp_0_discharge_target_soc" in base.entities, "v3 should publish the discharge target control"

    _mark_discovered(component)
    run_async(component.automatic_config())
    assert base.args["discharge_target_soc"] == ["number.predbat_givtcp_0_discharge_target_soc"]
    print("PASS: the discharge target control is still published on GivTCP v3")
    return 0


def test_discharge_target_needs_v3_on_every_inverter(my_predbat=None):
    """A mixed fleet leaves discharge_target_soc to the user, mirroring the pause-key rule."""
    base, component = _make_component(rest_urls=["http://givtcp0:6345", "http://givtcp1:6345"])
    _mark_discovered(component)
    for rest in component.rest:
        rest.inverter.rest_data = _rest_data_blob()
    component.rest[0].inverter.rest_v3 = True
    component.rest[1].inverter.rest_v3 = False

    run_async(component.automatic_config())
    assert "discharge_target_soc" not in base.args, f"a mixed fleet must not claim discharge_target_soc, got {base.args.get('discharge_target_soc')}"
    print("PASS: discharge target needs v3 on every inverter")
    return 0


def test_givtcp_component(my_predbat=None):
    """
    ======================================================================
    GIVTCP COMPONENT TEST SUITE
    ======================================================================
    """
    print("\n" + "=" * 70)
    print("GIVTCP COMPONENT TEST SUITE")
    print("=" * 70)

    sub_tests = [
        ("init_scalar", test_initialize_scalar, "initialize with a scalar URL"),
        ("init_list", test_initialize_list, "initialize with a list of URLs"),
        ("publish_no_data", test_publish_data_no_status_yet, "publish_data before first read"),
        ("publish_controls", test_publish_data_controls, "publish_data control entities"),
        ("publish_sensors", test_publish_data_sensors, "publish_data sensor entities"),
        ("auto_config_one", test_automatic_config_single_inverter, "automatic_config, one inverter"),
        ("auto_config_two", test_automatic_config_two_inverters, "automatic_config, two inverters"),
        ("parse_entity", test_parse_entity, "_parse_entity"),
        ("number_charge_rate", test_number_event_charge_rate, "number_event charge_rate"),
        ("write_inline", test_write_event_is_applied_inline_not_deferred, "writes applied inline, not deferred"),
        ("switch_charge_enable", test_switch_event_scheduled_charge_enable, "switch_event scheduled_charge_enable"),
        ("switch_toggle_ignored", test_switch_event_toggle_ignored, "switch_event toggle ignored"),
        ("select_charge_start", test_select_event_charge_start_time_preserves_end, "select_event charge_start_time"),
        ("select_discharge_end", test_select_event_discharge_end_time_preserves_start, "select_event discharge_end_time"),
        ("publish_modes", test_publish_data_mode_entities, "publish_data mode/pause entities"),
        ("select_plain", test_select_event_plain_selects_pass_the_value_through, "plain select writes pass through"),
        ("select_pause_start", test_select_event_pause_start_time_preserves_end, "select_event pause_start_time"),
        ("auto_config_pause", test_automatic_config_pause_keys_need_v3_everywhere, "pause keys need v3 everywhere"),
        ("discharge_target_models", test_discharge_target_not_published_for_unsupported_models, "discharge target withheld for unsupported models (#4517)"),
        ("discovery_captures", test_discovery_parsing_against_real_captures, "discovery parsed from real GivTCP captures"),
        ("calibration", test_calibration_detected_per_version, "calibration detected on both versions"),
        ("rate_max_attr", test_rate_entities_carry_the_real_max, "rate entities carry the real max"),
        ("rate_max_unknown", test_rate_entities_omit_max_when_the_rate_is_unknown, "no max advertised when rate unknown"),
        ("discovery_unpublished", test_discovery_keys_are_only_claimed_when_actually_published, "unpublished discovery keys not claimed"),
        ("discovery_published", test_discovery_keys_still_claimed_when_reported, "reported discovery keys still claimed"),
        ("slot_no_status", test_slot_write_before_any_status_is_refused_not_fabricated, "slot write refused with no status"),
        ("slot_with_status", test_slot_write_still_works_once_status_is_known, "slot write preserves the other end"),
        ("dt_not_v2", test_discharge_target_not_published_on_v2, "no discharge target on v2"),
        ("dt_on_v3", test_discharge_target_still_published_on_v3, "discharge target published on v3"),
        ("dt_mixed_fleet", test_discharge_target_needs_v3_on_every_inverter, "discharge target needs v3 everywhere"),
        ("time_options", test_arbitrary_minute_time_is_a_valid_option, "every minute is a valid time option"),
        ("unknown_control", test_unknown_entity_write_logged_not_crashed, "unknown control entity write"),
        ("unknown_inverter", test_unknown_inverter_index_write_logged_not_crashed, "out-of-range inverter index write"),
        ("run_first", test_run_first_call_polls_and_configures, "run() first call"),
        ("run_version", test_run_detects_givtcp_version, "run() detects GivTCP_Version"),
        ("run_no_data", test_run_reports_failure_until_data_arrives, "run() reports failure until data arrives"),
        ("power_ignore", test_automatic_config_respects_power_ignore, "automatic_config respects power_ignore"),
        ("soc_kw_binding", test_automatic_config_uses_soc_kw_not_percent, "automatic_config binds soc_kw"),
        ("write_exception", test_write_event_exception_does_not_propagate, "write exception contained"),
        ("discovered_count", test_automatic_config_counts_discovered_inverters_not_configured_urls, "num_inverters counts discovered inverters"),
        ("discovered_mapping", test_automatic_config_maps_predbat_inverter_to_the_live_endpoint, "live endpoint drives inverter 0"),
        ("discovered_none", test_automatic_config_skipped_when_nothing_was_discovered, "no config when nothing discovered"),
        ("discovery_drops_dead", test_dead_endpoint_is_not_polled_after_discovery, "dead endpoint dropped after discovery"),
        ("discovered_pause_gate", test_pause_keys_gated_on_discovered_inverters_only, "pause gate ignores undiscovered endpoints"),
        ("rediscover_late", test_rediscovery_picks_up_an_inverter_that_was_down_at_startup, "late inverter adopted on re-probe"),
        ("rediscover_append", test_rediscovery_appends_so_running_inverters_keep_their_identity, "re-probe appends, preserving identity"),
        ("rediscover_no_shrink", test_rediscovery_never_drops_an_inverter_that_stops_answering, "discovered inverters are never dropped"),
        ("rediscover_cheap", test_rediscovery_uses_a_cheap_single_probe, "re-probe uses a single cheap GET"),
        ("rediscover_complete", test_rediscovery_skipped_once_every_endpoint_is_discovered, "no re-probe when fleet is complete"),
        ("poll_fail_health", test_failed_poll_withholds_the_success_timestamp_and_warns, "failed poll warns and withholds success"),
        ("poll_recover", test_success_timestamp_resumes_once_the_poll_recovers, "success resumes after recovery"),
        ("poll_partial_fail", test_one_failing_inverter_withholds_success_for_the_whole_component, "one failing inverter degrades the component"),
        ("poll_placeholder_ok", test_an_endpoint_never_discovered_does_not_withhold_success, "undiscovered placeholder does not degrade"),
        ("charge_enable_publish", test_charge_limit_enable_switch_is_published, "charge target enable published as a switch"),
        ("charge_enable_config", test_charge_limit_enable_is_auto_configured, "charge_limit_enable auto-configured (#4141)"),
        ("charge_enable_write", test_charge_limit_enable_write_hits_the_enable_register, "enable switch writes the enable register"),
        ("charge_enable_absent", test_charge_limit_enable_withheld_when_the_register_is_absent, "enable switch withheld when unreported"),
        ("soc_max_design", test_soc_max_carries_the_design_capacity, "soc_max is the design capacity"),
        ("soh_dod_publish", test_battery_soh_dod_and_combined_scaling_are_published, "soh/dod/combined published"),
        ("dod_override", test_battery_dod_override_is_folded_into_the_scaling, "givtcp_battery_dod override"),
        ("scaling_config", test_battery_scaling_is_auto_configured_from_the_combined_sensor, "battery_scaling auto-configured"),
        ("no_design_cap", test_no_design_capacity_falls_back_to_the_reported_one, "missing design capacity falls back"),
        ("soh_v3_stack", test_battery_soh_reads_the_v3_stacked_layout, "soh read from v3 stacked layout"),
        ("soh_clamp", test_battery_soh_is_clamped_at_full_health, "soh clamped at 1.0"),
        ("soh_not_design_ratio", test_soh_is_not_derived_from_the_two_design_figures, "soh not derived from design figures"),
        ("rate_tolerance", test_rate_write_tolerance_uses_the_real_max_rate, "rate write tolerance uses real max rate"),
    ]

    passed = 0
    failed = 0
    for key, test_func, description in sub_tests:
        print(f"\n[{key}] {description}")
        print("-" * 70)
        try:
            result = test_func(my_predbat)
            if result:
                print(f"FAILED: {key}")
                failed += 1
            else:
                print(f"PASSED: {key}")
                passed += 1
        except Exception as e:
            print(f"EXCEPTION in {key}: {e}")
            import traceback

            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 70)
    print(f"RESULTS: {passed} passed, {failed} failed out of {len(sub_tests)} tests")
    print("=" * 70)

    return failed > 0
