# fmt: off
# pylint: disable=line-too-long
"""
Unit tests for the GivTCP component (givtcp.py) - publishes GivEnergy REST
inverter status/controls as HA entities and points Inverter's standard
apps.yaml entity keys at them via automatic_config().
"""

from unittest.mock import MagicMock

from tests.test_infra import run_async
from mock_base import MockBase
from givtcp import GivTCPComponent, GIVTCP_POLL_SECONDS


def _rest_data_blob(charge_rate=1000, discharge_rate=2000, target_soc=80, reserve=10, soc_kwh=5.0, soc_percent=50, charge_start="00:30:00", charge_end="04:30:00", discharge_start="16:00:00", discharge_end="19:00:00"):
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
        },
        "Power": {"Power": {"SOC_kWh": soc_kwh, "SOC": soc_percent, "Battery_Power": 100.0, "PV_Power": 200.0, "Grid_Power": -50.0, "Load_Power": 250.0, "Battery_Voltage": 51.2}},
        "Timeslots": {
            "Charge_start_time_slot_1": charge_start,
            "Charge_end_time_slot_1": charge_end,
            "Discharge_start_time_slot_1": discharge_start,
            "Discharge_end_time_slot_1": discharge_end,
        },
    }


def _make_component(rest_urls="http://givtcp:6345"):
    base = MockBase()
    component = GivTCPComponent(base, rest_urls=rest_urls)
    return base, component


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
    run_async(component.automatic_config())

    assert base.args["soc_kw"] == ["sensor.predbat_givtcp_0_soc_kw"], f"Expected soc_kw to be bound, got {base.args.get('soc_kw')}"
    assert "soc_percent" not in base.args, "soc_percent must not be bound - it would take precedence and lose precision"
    print("PASS: SoC is bound via the precise soc_kw sensor, not whole-percent soc_percent")
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
        ("time_options", test_arbitrary_minute_time_is_a_valid_option, "every minute is a valid time option"),
        ("unknown_control", test_unknown_entity_write_logged_not_crashed, "unknown control entity write"),
        ("unknown_inverter", test_unknown_inverter_index_write_logged_not_crashed, "out-of-range inverter index write"),
        ("run_first", test_run_first_call_polls_and_configures, "run() first call"),
        ("run_version", test_run_detects_givtcp_version, "run() detects GivTCP_Version"),
        ("run_no_data", test_run_reports_failure_until_data_arrives, "run() reports failure until data arrives"),
        ("power_ignore", test_automatic_config_respects_power_ignore, "automatic_config respects power_ignore"),
        ("soc_kw_binding", test_automatic_config_uses_soc_kw_not_percent, "automatic_config binds soc_kw"),
        ("write_exception", test_write_event_exception_does_not_propagate, "write exception contained"),
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
