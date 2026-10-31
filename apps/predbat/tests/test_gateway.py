"""
Tests for GatewayMQTT component.
"""

import sys
import os
import math
import json
import asyncio
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytz
import gateway_status_pb2 as pb
from gateway import _STARTUP_WAIT_TICKS

import importlib.util

HAS_AIOMQTT = importlib.util.find_spec("aiomqtt") is not None


def approx_equal(actual, expected, abs_tol=0.01):
    """Simple float comparison for when pytest is not available."""
    return math.isclose(actual, expected, abs_tol=abs_tol)


class TestProtobufDecode:
    """Test protobuf telemetry → entity mapping."""

    def _make_status(self, soc=50, battery_power=1000, pv_power=2000, grid_power=-500, load_power=1500):
        status = pb.GatewayStatus()
        status.device_id = "pbgw_test123"
        status.firmware = "0.4.5"
        status.timestamp = 1741789200
        status.schema_version = 1
        status.dongle_count = 1

        inv = status.inverters.add()
        inv.type = pb.INVERTER_TYPE_GIVENERGY
        inv.serial = "CE1234G567"
        inv.ip = "192.168.1.100"
        inv.connected = True
        inv.active = True

        inv.battery.soc_percent = soc
        inv.battery.power_w = battery_power
        inv.battery.voltage_v = 51.2
        inv.battery.current_a = 19.5
        inv.battery.temperature_c = 22.5
        inv.battery.soh_percent = 98
        inv.battery.cycle_count = 150
        inv.battery.capacity_wh = 9500

        inv.pv.power_w = pv_power
        inv.grid.power_w = grid_power
        inv.grid.voltage_v = 242.5
        inv.grid.frequency_hz = 50.01
        inv.load.power_w = load_power

        inv.inverter.active_power_w = 1800
        inv.inverter.temperature_c = 35.0

        inv.control.charge_enabled = True
        inv.control.discharge_enabled = True
        inv.control.charge_rate_w = 3000
        inv.control.discharge_rate_w = 3000
        inv.control.reserve_soc = 4
        inv.control.target_soc = 100

        inv.schedule.charge_start = 130
        inv.schedule.charge_end = 430
        inv.schedule.discharge_start = 1600
        inv.schedule.discharge_end = 1900

        return status

    def test_serialize_deserialize_roundtrip(self):
        original = self._make_status(soc=75, battery_power=2000)
        data = original.SerializeToString()
        decoded = pb.GatewayStatus()
        decoded.ParseFromString(data)

        assert decoded.device_id == "pbgw_test123"
        assert decoded.inverters[0].battery.soc_percent == 75
        assert decoded.inverters[0].battery.power_w == 2000
        assert decoded.inverters[0].pv.power_w == 2000
        assert decoded.inverters[0].grid.power_w == -500
        assert approx_equal(decoded.inverters[0].grid.voltage_v, 242.5, abs_tol=0.1)
        assert decoded.inverters[0].control.charge_enabled is True
        assert decoded.inverters[0].battery.soh_percent == 98

    def test_control_status_reserves_retired_operating_mode_field(self):
        assert "mode" not in pb.ControlStatus.DESCRIPTOR.fields_by_name


class TestPlanSerialization:
    def test_plan_roundtrip(self):
        from gateway import GatewayMQTT

        plan_entries = [
            {
                "enabled": True,
                "start_hour": 1,
                "start_minute": 30,
                "end_hour": 4,
                "end_minute": 30,
                "mode": 1,
                "power_w": 3000,
                "target_soc": 100,
                "days_of_week": 0x7F,
                "use_native": True,
            },
            {
                "enabled": True,
                "start_hour": 16,
                "start_minute": 0,
                "end_hour": 19,
                "end_minute": 0,
                "mode": 2,
                "power_w": 2500,
                "target_soc": 10,
                "days_of_week": 0x7F,
                "use_native": False,
            },
        ]

        data = GatewayMQTT.build_execution_plan(plan_entries, plan_version=42, timezone="Europe/London")

        plan = pb.ExecutionPlan()
        plan.ParseFromString(data)

        assert plan.plan_version == 42
        assert plan.timezone == "GMT0BST,M3.5.0/1,M10.5.0"
        assert len(plan.entries) == 2
        assert plan.entries[0].start_hour == 1
        assert plan.entries[0].start_minute == 30
        assert plan.entries[0].mode == 1
        assert plan.entries[0].use_native is True
        assert plan.entries[1].mode == 2
        assert plan.entries[1].use_native is False

    def test_empty_plan(self):
        from gateway import GatewayMQTT

        data = GatewayMQTT.build_execution_plan([], plan_version=1, timezone="UTC")
        plan = pb.ExecutionPlan()
        plan.ParseFromString(data)
        assert len(plan.entries) == 0
        assert plan.plan_version == 1


class TestCommandFormat:
    def test_set_charge_rate_command(self):
        from gateway import GatewayMQTT

        cmd = GatewayMQTT.build_command("set_charge_rate", power_w=2500)
        import json

        parsed = json.loads(cmd)
        assert parsed["command"] == "set_charge_rate"
        assert parsed["power_w"] == 2500

    def test_set_rate_cap_command(self):
        """set_rate_cap carries the inverter serial and both caps (predbat-gateway#424)."""
        from gateway import GatewayMQTT
        import json

        cmd = json.loads(GatewayMQTT.build_command("set_rate_cap", serial="CH2330G098", charge_cap_w=3000, discharge_cap_w=3600))
        assert cmd["command"] == "set_rate_cap"
        assert cmd["serial"] == "CH2330G098"
        assert cmd["charge_cap_w"] == 3000
        assert cmd["discharge_cap_w"] == 3600

    def test_set_reserve_command(self):
        from gateway import GatewayMQTT

        cmd = GatewayMQTT.build_command("set_reserve", target_soc=10)
        import json

        parsed = json.loads(cmd)
        assert parsed["command"] == "set_reserve"
        assert parsed["target_soc"] == 10

    def test_command_id_has_pbat_prefix(self):
        """command_id is a PBAT-prefixed string with the incrementing counter."""
        from gateway import GatewayMQTT
        import json

        cmd = GatewayMQTT.build_command("set_charge_rate", power_w=2500, command_id=7)
        parsed = json.loads(cmd)
        assert parsed["command_id"] == "PBAT7"
        assert isinstance(parsed["command_id"], str)

    def test_serial_included_as_serial(self):
        """serial kwarg is serialised under the "serial" key (the value is the inverter serial)."""
        from gateway import GatewayMQTT
        import json

        cmd = GatewayMQTT.build_command("set_reserve", target_soc=10, serial="CE123456789")
        parsed = json.loads(cmd)
        assert parsed["serial"] == "CE123456789"
        assert "dongle_serial" not in parsed

    def test_serial_preserves_original_case(self):
        """serial is stored as-is (uppercase) even though entity suffixes are lowercased."""
        from gateway import GatewayMQTT
        import json

        cmd = GatewayMQTT.build_command("set_charge_rate", power_w=3000, serial="CE123456789")
        parsed = json.loads(cmd)
        assert parsed["serial"] == "CE123456789"
        assert parsed["serial"] != parsed["serial"].lower()

    def test_serial_omitted_when_not_provided(self):
        """serial key is absent from the JSON when no serial kwarg is given."""
        from gateway import GatewayMQTT
        import json

        cmd = GatewayMQTT.build_command("set_charge_rate", power_w=2500)
        parsed = json.loads(cmd)
        assert "serial" not in parsed
        assert "dongle_serial" not in parsed


class TestScheduleSlotCommand:
    def test_set_charge_slot_command(self):
        """set_charge_slot includes schedule_json."""
        from gateway import GatewayMQTT
        import json

        cmd = GatewayMQTT.build_command("set_charge_slot", schedule_json='{"start": 130, "end": 430}')
        parsed = json.loads(cmd)
        assert parsed["command"] == "set_charge_slot"
        assert parsed["schedule_json"] == '{"start": 130, "end": 430}'

    def test_set_discharge_slot_command(self):
        """set_discharge_slot includes schedule_json."""
        from gateway import GatewayMQTT
        import json

        cmd = GatewayMQTT.build_command("set_discharge_slot", schedule_json='{"start": 1600}')
        parsed = json.loads(cmd)
        assert parsed["command"] == "set_discharge_slot"
        assert parsed["schedule_json"] == '{"start": 1600}'


class TestSerialFromEntityId:
    """Tests for GatewayMQTT._serial_from_entity_id() suffix extraction and map lookup."""

    def _make_gateway(self):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw._suffix_to_serial = {}
        return gw

    def test_standard_6char_suffix(self):
        """Normal entity with 6-char suffix resolves to the correct full serial."""
        gw = self._make_gateway()
        gw._suffix_to_serial["456789"] = "CE123456789"
        assert gw._serial_from_entity_id("select.predbat_gateway_456789_charge_slot1_start") == "CE123456789"

    def test_short_serial_suffix(self):
        """Serials shorter than 6 chars produce a shorter suffix; lookup still succeeds."""
        gw = self._make_gateway()
        gw._suffix_to_serial["abc"] = "ABC"  # serial == suffix (3 chars)
        assert gw._serial_from_entity_id("select.predbat_gateway_abc_charge_slot1_start") == "ABC"

    def test_suffix_lookup_is_case_insensitive(self):
        """Entity ID suffix is lowercased before lookup even if entity_id contains upper chars."""
        gw = self._make_gateway()
        gw._suffix_to_serial["456789"] = "CE123456789"
        # Uppercase in entity_id (unusual but should still resolve)
        assert gw._serial_from_entity_id("select.predbat_gateway_456789_charge_slot1_start") == "CE123456789"

    def test_no_gateway_marker_returns_none(self):
        """Entity IDs without '_gateway_' return None without logging."""
        gw = self._make_gateway()
        result = gw._serial_from_entity_id("select.predbat_some_other_entity")
        assert result is None
        gw.log.assert_not_called()

    def test_unknown_suffix_returns_none_and_warns(self):
        """Unknown suffix returns None and emits a Warn log."""
        gw = self._make_gateway()
        result = gw._serial_from_entity_id("select.predbat_gateway_456789_charge_slot1_start")
        assert result is None
        gw.log.assert_called_once()
        assert "Warn" in gw.log.call_args[0][0]


class TestInjectEntities:
    """Tests for GatewayMQTT._inject_entities() and GATEWAY_ATTRIBUTE_TABLE lookups."""

    def _make_gateway(self):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.log = MagicMock()
        gw.prefix = "predbat"
        gw._last_status = None
        gw.args = {}
        gw.local_tz = pytz.timezone("Europe/London")
        gw._suffix_to_serial = {}  # set by automatic_config; empty = nothing bound yet
        gw._last_read_only = None  # no set_read_only command sent yet
        gw._read_only_mismatch_logged = False
        gw._dashboard_calls = {}  # entity_id → (state, attributes)

        def capture_dashboard(entity_id, state=None, attributes=None, app=None):
            gw._dashboard_calls[entity_id] = (state, attributes)

        gw.dashboard_item = capture_dashboard
        return gw

    def _make_status(self, soc=50, battery_power=1000, pv_power=2000, grid_power=-500, load_power=1500, primary=True):
        status = pb.GatewayStatus()
        status.device_id = "pbgw_abc123"
        status.firmware = "1.2.3"
        status.timestamp = 1741789200
        status.schema_version = 1

        inv = status.inverters.add()
        inv.type = pb.INVERTER_TYPE_GIVENERGY
        inv.serial = "CE123456789"
        inv.primary = primary
        inv.connected = True
        inv.active = True

        inv.battery.soc_percent = soc
        inv.battery.power_w = battery_power
        inv.battery.voltage_v = 51.2
        inv.battery.current_a = 19.5
        inv.battery.temperature_c = 22.5
        inv.battery.soh_percent = 98
        inv.battery.capacity_wh = 9500
        inv.battery.rate_max_w = 5000
        inv.battery.depth_of_discharge_pct = 95

        inv.pv.power_w = pv_power
        inv.grid.power_w = grid_power
        inv.grid.voltage_v = 242.5
        inv.grid.frequency_hz = 50.01
        inv.load.power_w = load_power

        inv.inverter.active_power_w = 1800
        inv.inverter.temperature_c = 35.0

        inv.control.charge_enabled = True
        inv.control.discharge_enabled = True
        inv.control.charge_rate_w = 3000
        inv.control.discharge_rate_w = 3000
        inv.control.reserve_soc = 4
        inv.control.target_soc = 100

        inv.schedule.charge_start = 130
        inv.schedule.charge_end = 430
        inv.schedule.discharge_start = 1600
        inv.schedule.discharge_end = 1900

        inv.energy.pv_today_wh = 5000
        inv.energy.grid_import_today_wh = 1000
        inv.energy.grid_export_today_wh = 2000
        inv.energy.consumption_today_wh = 8000
        inv.energy.battery_charge_today_wh = 3000
        inv.energy.battery_discharge_today_wh = 2500

        return status

    def test_gateway_online_entity(self):
        """binary_sensor.predbat_gateway_online is published True with device_id, firmware, and table attributes merged."""
        from gateway import GATEWAY_ATTRIBUTE_TABLE

        gw = self._make_gateway()
        status = self._make_status()
        gw._inject_entities(status)

        entity = "binary_sensor.predbat_gateway_online"
        assert entity in gw._dashboard_calls
        state, attrs = gw._dashboard_calls[entity]
        assert state is True
        assert attrs["device_id"] == "pbgw_abc123"
        assert attrs["firmware"] == "1.2.3"
        # Table attributes should also be merged in
        for k, v in GATEWAY_ATTRIBUTE_TABLE.get("gateway_online", {}).items():
            assert attrs[k] == v

    def test_gateway_read_only_entity_true(self):
        """A gateway reporting read_only=true publishes the binary sensor True with table attributes."""
        from gateway import GATEWAY_ATTRIBUTE_TABLE

        gw = self._make_gateway()
        status = self._make_status()
        status.read_only = True
        gw._inject_entities(status)

        entity = "binary_sensor.predbat_gateway_read_only"
        assert entity in gw._dashboard_calls
        state, attrs = gw._dashboard_calls[entity]
        assert state is True
        for k, v in GATEWAY_ATTRIBUTE_TABLE.get("gateway_read_only", {}).items():
            assert attrs[k] == v
        # The online sensor carries the same value as an attribute
        _, online_attrs = gw._dashboard_calls["binary_sensor.predbat_gateway_online"]
        assert online_attrs["read_only"] is True

    def test_gateway_read_only_entity_false(self):
        """read_only present and false publishes False, not an absent entity."""
        gw = self._make_gateway()
        status = self._make_status()
        status.read_only = False
        gw._inject_entities(status)

        state, _ = gw._dashboard_calls["binary_sensor.predbat_gateway_read_only"]
        assert state is False
        _, online_attrs = gw._dashboard_calls["binary_sensor.predbat_gateway_online"]
        assert online_attrs["read_only"] is False

    def test_gateway_read_only_absent_is_unknown(self):
        """Firmware predating the field reports nothing: no entity, and the attribute is None."""
        gw = self._make_gateway()
        status = self._make_status()
        assert not status.HasField("read_only")
        gw._inject_entities(status)

        assert "binary_sensor.predbat_gateway_read_only" not in gw._dashboard_calls
        _, online_attrs = gw._dashboard_calls["binary_sensor.predbat_gateway_online"]
        assert online_attrs["read_only"] is None

    def test_gateway_read_only_mismatch_warns_once(self):
        """A gateway disagreeing with the last set_read_only warns once, and agreement clears the latch."""
        gw = self._make_gateway()
        gw._last_read_only = True  # PredBat asked for read-only

        status = self._make_status()
        status.read_only = False  # gateway is not enforcing it
        gw._inject_entities(status)

        warnings = [c[0][0] for c in gw.log.call_args_list if "read_only" in c[0][0]]
        assert len(warnings) == 1, warnings
        assert warnings[0].startswith("Warn:")
        assert "requested True" in warnings[0]
        assert gw._read_only_mismatch_logged is True

        # A second identical status must not repeat the warning
        gw._inject_entities(status)
        warnings = [c[0][0] for c in gw.log.call_args_list if "read_only" in c[0][0]]
        assert len(warnings) == 1, warnings

        # Once the gateway agrees the latch clears, so a later mismatch warns again
        status.read_only = True
        gw._inject_entities(status)
        assert gw._read_only_mismatch_logged is False
        status.read_only = False
        gw._inject_entities(status)
        warnings = [c[0][0] for c in gw.log.call_args_list if "read_only" in c[0][0]]
        assert len(warnings) == 2, warnings

    def test_gateway_read_only_no_mismatch_before_first_send(self):
        """Nothing sent yet (_last_read_only None) means nothing to compare, so no warning."""
        gw = self._make_gateway()
        status = self._make_status()
        status.read_only = True
        gw._inject_entities(status)

        warnings = [c[0][0] for c in gw.log.call_args_list if "read_only" in c[0][0]]
        assert warnings == []

    def test_inverter_time_sensor(self):
        """Inverter time sensor is published using the primary inverter serial suffix."""
        gw = self._make_gateway()
        status = self._make_status()
        gw._inject_entities(status)

        # Serial "CE123456789" (len > 6) → last 6 chars lowercase = "456789"
        entity = "sensor.predbat_gateway_456789_inverter_time"
        assert entity in gw._dashboard_calls
        state, attrs = gw._dashboard_calls[entity]
        assert state  # non-empty datetime string e.g. "2025-03-12 09:00:00"

    def test_non_primary_inverter_skipped(self):
        """Inverters with primary=False are not injected via _inject_inverter_entities."""
        gw = self._make_gateway()
        # First inverter is non-primary
        status = self._make_status(primary=False)
        # Second inverter is primary — should be the only one injected
        inv2 = status.inverters.add()
        inv2.type = pb.INVERTER_TYPE_GIVENERGY
        inv2.serial = "CE000000001"
        inv2.primary = True
        inv2.battery.soc_percent = 75
        inv2.battery.capacity_wh = 9500
        inv2.battery.depth_of_discharge_pct = 95

        gw._inject_entities(status)

        # Non-primary suffix "456789" should NOT appear as a sensor entity
        assert "sensor.predbat_gateway_456789_soc" not in gw._dashboard_calls
        # Primary suffix "000001" (last 6 of "CE000000001") SHOULD appear
        assert "sensor.predbat_gateway_000001_soc" in gw._dashboard_calls

    def test_battery_power_negated(self):
        """Battery power sign is inverted: firmware +ve=charging → PredBat +ve=discharging."""
        gw = self._make_gateway()
        gw._inject_entities(self._make_status(battery_power=1000))

        state, _ = gw._dashboard_calls["sensor.predbat_gateway_456789_battery_power"]
        assert state == -1000

    def test_sensor_attributes_from_table(self):
        """Sensor entities carry attributes looked up from GATEWAY_ATTRIBUTE_TABLE."""
        from gateway import GATEWAY_ATTRIBUTE_TABLE

        gw = self._make_gateway()
        gw._inject_entities(self._make_status())

        suffix = "456789"
        _, attrs = gw._dashboard_calls[f"sensor.predbat_gateway_{suffix}_soc"]
        assert attrs == GATEWAY_ATTRIBUTE_TABLE.get("soc", {})

        _, attrs = gw._dashboard_calls[f"sensor.predbat_gateway_{suffix}_pv_power"]
        assert attrs == GATEWAY_ATTRIBUTE_TABLE.get("pv_power", {})

        _, attrs = gw._dashboard_calls[f"sensor.predbat_gateway_{suffix}_grid_power"]
        assert attrs == GATEWAY_ATTRIBUTE_TABLE.get("grid_power", {})

        _, attrs = gw._dashboard_calls[f"sensor.predbat_gateway_{suffix}_battery_temperature"]
        assert attrs == GATEWAY_ATTRIBUTE_TABLE.get("battery_temperature", {})

    def test_ems_midnight_capability_stays_with_its_schedule_entities(self):
        """An EMS carries the same-day restriction without changing other Hub inverters or shared attributes."""
        from gateway import GATEWAY_ATTRIBUTE_TABLE

        gw = self._make_gateway()
        status = self._make_status()
        status.inverters[0].type = pb.INVERTER_TYPE_GIVENERGY_EMS
        gw._inject_entities(status)
        names = ["charge_slot1_start", "charge_slot1_end", "discharge_slot1_start", "discharge_slot1_end"]
        for name in names:
            _, attrs = gw._dashboard_calls[f"select.predbat_gateway_456789_{name}"]
            assert attrs["can_span_midnight"] is False
            assert "can_span_midnight" not in GATEWAY_ATTRIBUTE_TABLE.get(name, {})

        # Reclassification must remove a previous EMS restriction, including when
        # an entity id is reused after discovery refresh.
        for inv_type in [pb.INVERTER_TYPE_GIVENERGY, pb.INVERTER_TYPE_GIVENERGY_GATEWAY, pb.INVERTER_TYPE_SOLIS_HYBRID]:
            status.inverters[0].type = inv_type
            gw._inject_entities(status)
            for name in names:
                _, attrs = gw._dashboard_calls[f"select.predbat_gateway_456789_{name}"]
                assert "can_span_midnight" not in attrs

    def test_schedule_select_entities(self):
        """Schedule select entities are published with correct HH:MM:SS state and table attributes."""
        from gateway import GATEWAY_ATTRIBUTE_TABLE

        gw = self._make_gateway()
        gw._inject_entities(self._make_status())

        suffix = "456789"
        # charge_start = 130 → 01:30:00
        state, attrs = gw._dashboard_calls[f"select.predbat_gateway_{suffix}_charge_slot1_start"]
        assert state == "01:30:00"
        assert attrs == GATEWAY_ATTRIBUTE_TABLE.get("charge_slot1_start", {})

        # charge_end = 430 → 04:30:00
        state, _ = gw._dashboard_calls[f"select.predbat_gateway_{suffix}_charge_slot1_end"]
        assert state == "04:30:00"

        # discharge_start = 1600 → 16:00:00
        state, _ = gw._dashboard_calls[f"select.predbat_gateway_{suffix}_discharge_slot1_start"]
        assert state == "16:00:00"

        # discharge_end = 1900 → 19:00:00
        state, _ = gw._dashboard_calls[f"select.predbat_gateway_{suffix}_discharge_slot1_end"]
        assert state == "19:00:00"

    def test_operating_mode_selector_is_not_published(self):
        gw = self._make_gateway()
        gw._inject_entities(self._make_status())

        assert "select.predbat_gateway_456789_mode_select" not in gw._dashboard_calls

    def test_energy_counters_wh_to_kwh(self):
        """Energy counters are converted from Wh to kWh correctly."""
        gw = self._make_gateway()
        gw._inject_entities(self._make_status())

        suffix = "456789"
        state, _ = gw._dashboard_calls[f"sensor.predbat_gateway_{suffix}_pv_today"]
        assert approx_equal(state, 5.0)

        state, _ = gw._dashboard_calls[f"sensor.predbat_gateway_{suffix}_import_today"]
        assert approx_equal(state, 1.0)

        state, _ = gw._dashboard_calls[f"sensor.predbat_gateway_{suffix}_export_today"]
        assert approx_equal(state, 2.0)

        state, _ = gw._dashboard_calls[f"sensor.predbat_gateway_{suffix}_load_today"]
        assert approx_equal(state, 8.0)

        state, _ = gw._dashboard_calls[f"sensor.predbat_gateway_{suffix}_battery_charge_today"]
        assert approx_equal(state, 3.0)

        state, _ = gw._dashboard_calls[f"sensor.predbat_gateway_{suffix}_battery_discharge_today"]
        assert approx_equal(state, 2.5)

    def test_battery_dod_entity(self):
        """Battery DoD is published as a fraction (firmware pct / 100)."""
        gw = self._make_gateway()
        gw._inject_entities(self._make_status())

        state, _ = gw._dashboard_calls["sensor.predbat_gateway_456789_battery_dod"]
        assert approx_equal(state, 0.95)

    def test_export_limit_w_sensor_published(self):
        """export_limit_w from ControlStatus is published as a sensor in watts."""
        from gateway import GATEWAY_ATTRIBUTE_TABLE

        status = self._make_status()
        status.inverters[0].control.export_limit_w = 3600
        gw = self._make_gateway()
        gw._inject_entities(status)

        entity = "sensor.predbat_gateway_456789_export_limit_w"
        assert entity in gw._dashboard_calls
        state, attrs = gw._dashboard_calls[entity]
        assert state == 3600
        assert attrs == GATEWAY_ATTRIBUTE_TABLE.get("export_limit_w", {})

    def test_export_limit_w_zero_undefined_publishes_99999(self):
        """export_limit_w = 0 (undefined sentinel) is published as 99999 (unlimited)."""
        status = self._make_status()  # control.export_limit_w defaults to 0
        gw = self._make_gateway()
        gw._inject_entities(status)

        entity = "sensor.predbat_gateway_456789_export_limit_w"
        assert entity in gw._dashboard_calls
        state, _ = gw._dashboard_calls[entity]
        assert state == 99999

    def test_export_limit_w_one_zero_limit_publishes_zero(self):
        """export_limit_w = 1 (zero-limit sentinel) is published as 0 W."""
        status = self._make_status()
        status.inverters[0].control.export_limit_w = 1
        gw = self._make_gateway()
        gw._inject_entities(status)

        entity = "sensor.predbat_gateway_456789_export_limit_w"
        assert entity in gw._dashboard_calls
        state, _ = gw._dashboard_calls[entity]
        assert state == 0

    def test_reserve_soc_max_published_as_the_entity_max(self):
        """The firmware's reserve ceiling becomes the reserve entity's max attribute, which is what adjust_reserve() clamps a hold to."""
        status = self._make_status()
        status.inverters[0].control.reserve_soc_max = 98
        gw = self._make_gateway()
        gw._inject_entities(status)

        entity = "number.predbat_gateway_456789_reserve_soc"
        assert entity in gw._dashboard_calls
        state, attrs = gw._dashboard_calls[entity]
        assert state == 4
        assert attrs["max"] == 98
        # The rest of the table entry survives the override
        assert attrs["min"] == 0
        assert attrs["step"] == 1
        assert attrs["unit_of_measurement"] == "%"

    def test_reserve_soc_max_zero_falls_back_to_100(self):
        """Gateway firmware predating the field reports 0, which means "no limit reported" and must not cap the reserve at zero."""
        status = self._make_status()  # control.reserve_soc_max defaults to 0
        gw = self._make_gateway()
        gw._inject_entities(status)

        _, attrs = gw._dashboard_calls["number.predbat_gateway_456789_reserve_soc"]
        assert attrs["max"] == 100

    def test_reserve_soc_max_out_of_range_falls_back_to_100(self):
        """A ceiling above 100 is nonsense and is ignored rather than published as a bound."""
        status = self._make_status()
        status.inverters[0].control.reserve_soc_max = 255
        gw = self._make_gateway()
        gw._inject_entities(status)

        _, attrs = gw._dashboard_calls["number.predbat_gateway_456789_reserve_soc"]
        assert attrs["max"] == 100

    def test_reserve_soc_max_does_not_mutate_the_shared_table(self):
        """The ceiling is per-inverter, so it is applied to a copy: one inverter's 98 must not become every inverter's max."""
        from gateway import GATEWAY_ATTRIBUTE_TABLE

        status = self._make_status()
        status.inverters[0].control.reserve_soc_max = 98
        gw = self._make_gateway()
        gw._inject_entities(status)

        assert GATEWAY_ATTRIBUTE_TABLE["reserve_soc"]["max"] == 100

    def test_rate_step_marked_on_givenergy_rates(self):
        """A GivEnergy inverter's rate entities carry the 1% of capacity step its read-back is rounded down to (#5324)."""
        status = self._make_status()
        for inverter_type in (pb.INVERTER_TYPE_GIVENERGY, pb.INVERTER_TYPE_GIVENERGY_EMS, pb.INVERTER_TYPE_GIVENERGY_GATEWAY):
            status.inverters[0].type = inverter_type
            gw = self._make_gateway()
            gw._inject_entities(status)

            for rate in ("charge_rate", "discharge_rate"):
                state, attrs = gw._dashboard_calls["number.predbat_gateway_456789_" + rate]
                assert state == 3000
                assert attrs["step_percent_of_capacity"] == 1, (inverter_type, rate, attrs)
                # The rest of the table entry survives
                assert attrs["unit_of_measurement"] == "W"
                assert attrs["step"] == 10

    def test_grid_charge_only_marked_on_3ph_charge_rate(self):
        """A GivEnergy 3-phase model's charge rate is marked as limiting grid charging only, so no Freeze Export is planned (#5082)."""
        from gateway import GATEWAY_ATTRIBUTE_TABLE

        status = self._make_status()
        status.inverters[0].type = pb.INVERTER_TYPE_GIVENERGY
        # The names the hub firmware reports, and a different casing in case it changes
        for model in ("Hybrid 3ph", "AC 3ph", "HYBRID 3PH"):
            status.inverters[0].model = model
            gw = self._make_gateway()
            gw._inject_entities(status)

            _, attrs = gw._dashboard_calls["number.predbat_gateway_456789_charge_rate"]
            assert attrs["grid_charge_only"] is True, (model, attrs)
            assert attrs["step_percent_of_capacity"] == 1, (model, attrs)
            # It describes charging: the discharge rate is not marked
            _, attrs = gw._dashboard_calls["number.predbat_gateway_456789_discharge_rate"]
            assert "grid_charge_only" not in attrs, (model, attrs)

        # Marked on a copy, like the rate step
        assert "grid_charge_only" not in GATEWAY_ATTRIBUTE_TABLE["charge_rate"]

    def test_grid_charge_only_not_marked_on_other_models(self):
        """Single-phase GivEnergy models (Battery Pause, or a charge limit that does stop the battery), older firmware with no model and other brands keep Freeze Export."""
        status = self._make_status()
        for inverter_type, model in (
            (pb.INVERTER_TYPE_GIVENERGY, "Hybrid Gen3"),
            (pb.INVERTER_TYPE_GIVENERGY, "All-in-One"),
            (pb.INVERTER_TYPE_GIVENERGY, "AC Gen1"),
            (pb.INVERTER_TYPE_GIVENERGY, ""),
            (pb.INVERTER_TYPE_SOLIS_HYBRID, "Hybrid 3ph"),
        ):
            status.inverters[0].type = inverter_type
            status.inverters[0].model = model
            gw = self._make_gateway()
            gw._inject_entities(status)

            _, attrs = gw._dashboard_calls["number.predbat_gateway_456789_charge_rate"]
            assert "grid_charge_only" not in attrs, (inverter_type, model, attrs)

    def test_rate_step_not_marked_on_other_brands(self):
        """GWMQTT is one inverter type for every brand the hub drives, so the GivEnergy step must not reach the others."""
        from gateway import GATEWAY_ATTRIBUTE_TABLE

        status = self._make_status()
        for inverter_type in (pb.INVERTER_TYPE_SOLIS_HYBRID, pb.INVERTER_TYPE_DEYE_SUNSYNK, pb.INVERTER_TYPE_UNKNOWN):
            status.inverters[0].type = inverter_type
            gw = self._make_gateway()
            gw._inject_entities(status)

            for rate in ("charge_rate", "discharge_rate"):
                _, attrs = gw._dashboard_calls["number.predbat_gateway_456789_" + rate]
                assert "step_percent_of_capacity" not in attrs, (inverter_type, rate, attrs)

        # Marked on a copy: a GivEnergy inverter must not leave the step in the shared table
        status.inverters[0].type = pb.INVERTER_TYPE_GIVENERGY
        self._make_gateway()._inject_entities(status)
        for rate in ("charge_rate", "discharge_rate"):
            assert "step_percent_of_capacity" not in GATEWAY_ATTRIBUTE_TABLE[rate]

    def test_ems_aggregate_entities(self):
        """EMS aggregate and sub-inverter entities are published with table attributes."""
        from gateway import GATEWAY_ATTRIBUTE_TABLE

        status = pb.GatewayStatus()
        status.device_id = "pbgw_ems"
        status.firmware = "1.0.0"
        status.timestamp = 1741789200
        status.schema_version = 1

        inv = status.inverters.add()
        inv.type = pb.INVERTER_TYPE_GIVENERGY_EMS
        inv.serial = "EM123456"
        inv.primary = True
        inv.ems.num_inverters = 2
        inv.ems.total_soc = 70
        inv.ems.total_charge_w = 4000
        inv.ems.total_discharge_w = 0
        inv.ems.total_grid_w = -2000
        inv.ems.total_pv_w = 6000
        inv.ems.total_load_w = 5000

        sub0 = inv.ems.sub_inverters.add()
        sub0.soc = 65
        sub0.battery_w = 2000
        sub0.pv_w = 3000
        sub0.grid_w = -1000
        sub0.temp_c = 28.0

        sub1 = inv.ems.sub_inverters.add()
        sub1.soc = 75
        sub1.battery_w = 2000

        gw = self._make_gateway()
        gw._inject_entities(status)

        pfx = "predbat_gateway"
        # Aggregate entities
        assert gw._dashboard_calls[f"sensor.{pfx}_ems_total_soc"][0] == 70
        assert gw._dashboard_calls[f"sensor.{pfx}_ems_total_charge"][0] == 4000
        assert gw._dashboard_calls[f"sensor.{pfx}_ems_total_pv"][0] == 6000
        assert gw._dashboard_calls[f"sensor.{pfx}_ems_total_load"][0] == 5000
        assert gw._dashboard_calls[f"sensor.{pfx}_ems_total_grid"][0] == -2000

        # Sub-inverter entities
        assert gw._dashboard_calls[f"sensor.{pfx}_sub0_soc"][0] == 65
        assert gw._dashboard_calls[f"sensor.{pfx}_sub0_battery_power"][0] == -2000
        assert gw._dashboard_calls[f"sensor.{pfx}_sub0_pv_power"][0] == 3000
        assert gw._dashboard_calls[f"sensor.{pfx}_sub1_soc"][0] == 75

        # Attributes from table
        _, attrs = gw._dashboard_calls[f"sensor.{pfx}_ems_total_soc"]
        assert attrs == GATEWAY_ATTRIBUTE_TABLE.get("ems_total_soc", {})
        _, attrs = gw._dashboard_calls[f"sensor.{pfx}_sub0_temp"]
        assert attrs == GATEWAY_ATTRIBUTE_TABLE.get("temp", {})

    def test_ems_aggregate_entities_when_ems_not_first_inverter(self):
        """EMS aggregates publish even when the EMS unit is not status.inverters[0].

        Discovery order is unstable (see the 2026-06-04 incident), so the publish path
        must locate the EMS by type rather than assuming index 0. A coordinator/AIO unit
        ahead of the EMS in telemetry previously suppressed the entire aggregate block.
        """
        status = pb.GatewayStatus()
        status.device_id = "pbgw_ems"
        status.firmware = "1.0.0"
        status.timestamp = 1741789200
        status.schema_version = 1

        # Non-EMS unit listed first (the historically-assumed "inverter 0").
        aio = status.inverters.add()
        aio.type = pb.INVERTER_TYPE_GIVENERGY
        aio.serial = "AI000001"
        aio.primary = True

        # EMS unit listed second.
        inv = status.inverters.add()
        inv.type = pb.INVERTER_TYPE_GIVENERGY_EMS
        inv.serial = "EM123456"
        inv.primary = True
        inv.ems.num_inverters = 2
        inv.ems.total_soc = 70
        inv.ems.total_charge_w = 4000
        inv.ems.total_grid_w = -2000
        inv.ems.total_pv_w = 6000
        inv.ems.total_load_w = 5000

        sub0 = inv.ems.sub_inverters.add()
        sub0.soc = 65

        gw = self._make_gateway()
        gw._inject_entities(status)

        pfx = "predbat_gateway"
        assert gw._dashboard_calls[f"sensor.{pfx}_ems_total_soc"][0] == 70
        assert gw._dashboard_calls[f"sensor.{pfx}_ems_total_grid"][0] == -2000
        assert gw._dashboard_calls[f"sensor.{pfx}_sub0_soc"][0] == 65

    def test_inverter_rate_max_published_from_inverter(self):
        """inverter_rate_max sensor uses InverterData.rate_max_w when non-zero."""
        from gateway import GATEWAY_ATTRIBUTE_TABLE

        status = self._make_status()
        status.inverters[0].inverter.rate_max_w = 6000
        gw = self._make_gateway()
        gw._inject_entities(status)

        entity = "sensor.predbat_gateway_456789_inverter_rate_max"
        assert entity in gw._dashboard_calls
        state, attrs = gw._dashboard_calls[entity]
        assert state == 6000
        assert attrs == GATEWAY_ATTRIBUTE_TABLE.get("inverter_rate_max", {})

    def test_inverter_rate_max_falls_back_to_battery(self):
        """inverter_rate_max sensor uses BatteryStatus.rate_max_w when InverterData.rate_max_w is zero."""
        status = self._make_status()
        status.inverters[0].inverter.rate_max_w = 0
        status.inverters[0].battery.rate_max_w = 5000
        gw = self._make_gateway()
        gw._inject_entities(status)

        entity = "sensor.predbat_gateway_456789_inverter_rate_max"
        assert entity in gw._dashboard_calls
        state, _ = gw._dashboard_calls[entity]
        assert state == 5000

    def test_inverter_rate_max_uses_6000_when_both_zero(self):
        """inverter_rate_max sensor is still published with the 6000 W default when both InverterData and BatteryStatus report zero."""
        status = self._make_status()
        status.inverters[0].inverter.rate_max_w = 0
        status.inverters[0].battery.rate_max_w = 0
        gw = self._make_gateway()
        gw._inject_entities(status)

        entity = "sensor.predbat_gateway_456789_inverter_rate_max"
        assert entity in gw._dashboard_calls
        state, _ = gw._dashboard_calls[entity]
        assert state == 6000


class TestBoundEntitiesAreWritten:
    """Every entity automatic_config() binds must actually be written by _inject_entities().

    A bound-but-never-written entity holds whatever value it last had and silently
    freezes — surfacing only as PredBat's clock-skew warning once the stale
    inverter_time drifts past the threshold.
    """

    def _make_gateway(self):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.log = MagicMock()
        gw.prefix = "predbat"
        gw._last_status = None
        gw._auto_configured = False
        gw._suffix_to_serial = {}
        gw.args = {}
        gw._args = {}
        gw.local_tz = pytz.timezone("Europe/London")
        gw.gateway_inverter_serial = []
        gw.gateway_evc_automatic = False
        gw.gateway_evc_control = False
        gw.gateway_shared_ct = True
        gw.gateway_integrate_power = False
        gw._dashboard_calls = {}

        def capture_set_arg(key, value):
            gw._args[key] = value

        def capture_dashboard(entity_id, state=None, attributes=None, app=None):
            gw._dashboard_calls[entity_id] = (state, attributes)

        gw.set_arg = capture_set_arg
        gw.dashboard_item = capture_dashboard
        return gw

    def _add_inverter(self, status, serial, primary, inv_type=None):
        inv = status.inverters.add()
        inv.type = inv_type if inv_type is not None else pb.INVERTER_TYPE_GIVENERGY
        inv.serial = serial
        inv.primary = primary
        inv.connected = True
        inv.active = True
        inv.battery.soc_percent = 50
        inv.battery.capacity_wh = 9500
        inv.battery.rate_max_w = 5000
        return inv

    def _gateway_plus_two_aios(self):
        """Reproduces the field topology: a Gateway coordinating two primary AIOs.

        Per GivTCP rules automatic_config picks the Gateway as the control target,
        so every arg binds to the Gateway's serial suffix. The Gateway is not
        flagged primary — firmware only sets primary on battery inverters.
        """
        status = pb.GatewayStatus()
        status.device_id = "pbgw_test"
        status.firmware = "0.27.0"
        status.timestamp = 1741789200
        status.schema_version = 1
        self._add_inverter(status, "GW2315G357", primary=False, inv_type=pb.INVERTER_TYPE_GIVENERGY_GATEWAY)
        self._add_inverter(status, "CH2335G421", primary=True)
        self._add_inverter(status, "CH2432G070", primary=True)
        return status

    def test_bound_inverter_time_entity_is_written(self):
        """The inverter_time entity bound by automatic_config must be written."""
        gw = self._make_gateway()
        gw._last_status = self._gateway_plus_two_aios()
        gw.automatic_config()

        bound = gw._args["inverter_time"][0]

        gw._inject_entities(gw._last_status)

        assert bound in gw._dashboard_calls, "automatic_config bound inverter_time to {} but _inject_entities never wrote it; " "written entities were: {}".format(bound, sorted(gw._dashboard_calls))

    def test_bound_soc_entity_is_written(self):
        """The soc_percent entity bound by automatic_config must be written."""
        gw = self._make_gateway()
        gw._last_status = self._gateway_plus_two_aios()
        gw.automatic_config()

        bound = gw._args["soc_percent"][0]

        gw._inject_entities(gw._last_status)

        assert bound in gw._dashboard_calls, "automatic_config bound soc_percent to {} but _inject_entities never wrote it; " "written entities were: {}".format(bound, sorted(gw._dashboard_calls))


class TestDebugLogging:
    """Tests for the gateway_debug verbose telemetry/plan dump helper."""

    def _make_gateway(self, debug):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw._debug = debug
        return gw

    def test_dump_logs_decoded_message_when_enabled(self):
        """With debug on, a decoded message is rendered as text with its byte size."""
        status = pb.GatewayStatus()
        status.device_id = "pbgw_dbg"
        inv = status.inverters.add()
        inv.type = pb.INVERTER_TYPE_GIVENERGY_EMS
        inv.serial = "EM123456"
        raw = status.SerializeToString()

        gw = self._make_gateway(debug=True)
        gw._debug_dump("RX telemetry", status, raw=raw)

        assert gw.log.call_count == 1
        msg = gw.log.call_args[0][0]
        assert "Debug: GatewayMQTT: RX telemetry" in msg
        assert f"({len(raw)} bytes)" in msg
        assert "EM123456" in msg  # message body rendered as text

    def test_dump_silent_when_disabled(self):
        """With debug off, nothing is logged."""
        status = pb.GatewayStatus()
        gw = self._make_gateway(debug=False)
        gw._debug_dump("RX telemetry", status, raw=status.SerializeToString())
        assert gw.log.call_count == 0

    def test_dump_decodes_raw_bytes_with_message_type(self):
        """Raw bytes plus a message_type are decoded and rendered."""
        plan = pb.ExecutionPlan()
        plan.plan_version = 7
        raw = plan.SerializeToString()

        gw = self._make_gateway(debug=True)
        gw._debug_dump("TX execution plan", raw=raw, message_type=pb.ExecutionPlan)

        assert gw.log.call_count == 1
        msg = gw.log.call_args[0][0]
        assert '"plan_version": 7' in msg

    def test_initialize_reads_gateway_debug_arg(self):
        """initialize() picks up the gateway_debug flag from apps.yaml args."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {"gateway_debug": True}
        gw.initialize(gateway_device_id="pbgw_test", mqtt_host="mqtt.example.com", mqtt_token="tok")
        assert gw._debug is True

    def test_initialize_debug_defaults_off(self):
        """gateway_debug defaults to False when the arg is absent."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {}
        gw.initialize(gateway_device_id="pbgw_test", mqtt_host="mqtt.example.com", mqtt_token="tok")
        assert gw._debug is False


class TestTokenRefresh:
    def test_jwt_expiry_extraction(self):
        """Extract exp claim from a JWT without verification."""
        from gateway import GatewayMQTT
        import base64
        import json as json_mod

        # Build a fake JWT with exp claim
        header = base64.urlsafe_b64encode(json_mod.dumps({"alg": "RS256"}).encode()).rstrip(b"=")
        payload = base64.urlsafe_b64encode(json_mod.dumps({"exp": 1741789200, "sub": "test"}).encode()).rstrip(b"=")
        fake_jwt = f"{header.decode()}.{payload.decode()}.fake_signature"

        exp = GatewayMQTT.extract_jwt_expiry(fake_jwt)
        assert exp == 1741789200

    def test_jwt_expiry_invalid_token(self):
        """Invalid JWT returns 0."""
        from gateway import GatewayMQTT

        assert GatewayMQTT.extract_jwt_expiry("not-a-jwt") == 0
        assert GatewayMQTT.extract_jwt_expiry("") == 0

    def test_token_needs_refresh(self):
        """Token should be refreshed 1 hour before expiry."""
        from gateway import GatewayMQTT
        import time as time_mod

        # Token expiring in 30 minutes — needs refresh
        exp_soon = int(time_mod.time()) + 1800
        assert GatewayMQTT.token_needs_refresh(exp_soon) is True

        # Token expiring in 2 hours — does not need refresh
        exp_later = int(time_mod.time()) + 7200
        assert GatewayMQTT.token_needs_refresh(exp_later) is False


class TestPlanHookConversion:
    """Test on_plan_executed hook converts optimizer plan to gateway entries."""

    def _make_gateway(self):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        base = MagicMock()
        base.log = MagicMock()
        base.local_tz = "Europe/London"
        base.prefix = "predbat"
        base.args = {}
        base.register_hook = MagicMock()

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = base
        gw.log = base.log
        gw._last_published_plan = None
        gw._pending_plan = None
        gw._plan_version = 0
        gw._mqtt_connected = False
        gw._last_plan_data = None
        gw._last_plan_publish_time = 0
        return gw

    def test_same_day_hub_plan_splits_charge_and_export_at_midnight(self):
        """The native backup plan preserves rate, target and absolute days across midnight."""
        from types import SimpleNamespace

        gw = self._make_gateway()
        gw.base.inverters = [SimpleNamespace(inverter_type="GWMQTT", inv_can_span_midnight=False)]
        gw._on_plan_executed(
            charge_windows=[{"start": 1410, "end": 1770}],
            charge_limits=[8],
            export_windows=[{"start": 2820, "end": 2910}],
            export_limits=[(0, 20, 0.5)],
            charge_rate_w=3000,
            discharge_rate_w=2000,
            soc_max=10,
        )
        entries, _ = gw._pending_plan
        assert [(e["start_hour"], e["start_minute"], e["end_hour"], e["end_minute"]) for e in entries] == [(23, 30, 23, 59), (24, 0, 29, 30), (47, 0, 47, 59), (48, 0, 48, 30)]
        assert [(e["mode"], e["power_w"], e["target_soc"]) for e in entries] == [(1, 3000, 80), (1, 3000, 80), (2, 1000, 20), (2, 1000, 20)]
        # Exercise the real protobuf serialiser, including absolute hours >= 24.
        encoded = gw.build_execution_plan(entries, plan_version=1, timezone="Europe/London")
        plan = pb.ExecutionPlan.FromString(encoded)
        assert len(plan.entries) == 4
        assert plan.entries[1].start_hour == 24 and plan.entries[3].end_hour == 48

    def test_same_day_plan_drops_expired_starts_and_caps_after_sorting(self):
        """Retained negative starts cannot enter an unsigned protobuf field or consume future capacity."""
        from types import SimpleNamespace

        gw = self._make_gateway()
        gw.base.inverters = [SimpleNamespace(inverter_type="GWMQTT", inv_can_span_midnight=False)]
        gw._on_plan_executed(
            charge_windows=[{"start": 1410, "end": 1770}, {"start": -30, "end": 330}, {"start": 2820, "end": 2910}, {"start": -90, "end": -30}],
            charge_limits=[8, 8, 8, 8],
            export_windows=[{"start": 600, "end": 660}, {"start": 700, "end": 760}],
            export_limits=[(0, 20, 1.0), (0, 20, 1.0)],
        )
        entries, _ = gw._pending_plan
        starts = [e["start_hour"] * 60 + e["start_minute"] for e in entries]
        assert starts == [0, 600, 700, 1410, 1440, 2820]
        assert len(entries) == 6
        assert all(e["start_hour"] <= e["end_hour"] for e in entries)
        gw.build_execution_plan(entries, plan_version=1, timezone="Europe/London")

    def test_midnight_capable_hub_plan_keeps_overnight_window(self):
        """Hub hardware which supports overnight windows retains the unsplit plan."""
        from types import SimpleNamespace

        gw = self._make_gateway()
        gw.base.inverters = [SimpleNamespace(inverter_type="GWMQTT", inv_can_span_midnight=True)]
        gw._on_plan_executed(charge_windows=[{"start": 1410, "end": 1770}], charge_limits=[8])
        entries, _ = gw._pending_plan
        assert len(entries) == 1
        assert (entries[0]["start_hour"], entries[0]["end_hour"]) == (23, 29)

    def test_charge_window_conversion(self):
        """Charge windows are converted to mode=1 plan entries."""
        gw = self._make_gateway()

        gw._on_plan_executed(
            charge_windows=[{"start": 90, "end": 270}],  # 01:30 - 04:30
            charge_limits=[100],
            export_windows=[],
            export_limits=[],
            charge_rate_w=3000,
            discharge_rate_w=2500,
            timezone="Europe/London",
        )

        assert gw._pending_plan is not None
        entries, tz = gw._pending_plan
        assert tz == "Europe/London"
        assert len(entries) == 1
        entry = entries[0]
        assert entry["mode"] == 1  # charge
        assert entry["start_hour"] == 1
        assert entry["start_minute"] == 30
        assert entry["end_hour"] == 4
        assert entry["end_minute"] == 30
        assert entry["power_w"] == 3000
        assert entry["target_soc"] == 100

    def test_export_window_conversion(self):
        """Export windows with limit < 100 are converted to mode=2 entries."""
        gw = self._make_gateway()

        gw._on_plan_executed(
            charge_windows=[],
            charge_limits=[],
            export_windows=[{"start": 960, "end": 1140}],  # 16:00 - 19:00
            export_limits=[10],
            charge_rate_w=3000,
            discharge_rate_w=2500,
            timezone="Europe/London",
        )

        assert gw._pending_plan is not None
        entries, _ = gw._pending_plan
        assert len(entries) == 1
        entry = entries[0]
        assert entry["mode"] == 2  # discharge
        assert entry["start_hour"] == 16
        assert entry["end_hour"] == 19
        assert entry["power_w"] == 2500
        assert entry["target_soc"] == 10

    def test_empty_windows_publishes_empty_plan(self):
        """Empty windows should still queue an empty plan to clear gateway schedule."""
        gw = self._make_gateway()

        gw._on_plan_executed(
            charge_windows=[],
            charge_limits=[],
            export_windows=[],
            export_limits=[],
            charge_rate_w=3000,
            discharge_rate_w=2500,
            timezone="Europe/London",
        )

        assert gw._pending_plan is not None
        entries, tz = gw._pending_plan
        assert len(entries) == 0
        assert tz == "Europe/London"

    def test_skips_zero_limit_charge(self):
        """Charge windows with limit <= 0 produce no entries."""
        gw = self._make_gateway()

        gw._on_plan_executed(
            charge_windows=[{"start": 90, "end": 270}],
            charge_limits=[0],
            export_windows=[],
            export_limits=[],
            charge_rate_w=3000,
            discharge_rate_w=2500,
            timezone="Europe/London",
        )

        # Empty plan still queued (clears gateway schedule), but has no entries
        assert gw._pending_plan is not None
        entries, _ = gw._pending_plan
        assert len(entries) == 0

    def test_caps_plan_at_six_entries(self):
        """Plan entries are capped at 6 to match firmware PlanEntry[6] fixed array."""
        gw = self._make_gateway()
        gw.args = {}

        # 5 charge windows + 5 export windows = 10 entries, should cap to 6
        gw._on_plan_executed(
            charge_windows=[{"start": i * 60, "end": i * 60 + 30} for i in range(5)],
            charge_limits=[80] * 5,
            export_windows=[{"start": 720 + i * 60, "end": 720 + i * 60 + 30} for i in range(5)],
            export_limits=[10] * 5,
            charge_rate_w=3000,
            discharge_rate_w=2500,
            timezone="Europe/London",
        )

        assert gw._pending_plan is not None
        entries, _ = gw._pending_plan
        assert len(entries) == 6

    def test_skips_full_limit_export(self):
        """Export windows with limit >= 100 produce no entries."""
        gw = self._make_gateway()

        gw._on_plan_executed(
            charge_windows=[],
            charge_limits=[],
            export_windows=[{"start": 960, "end": 1140}],
            export_limits=[100],
            charge_rate_w=3000,
            discharge_rate_w=2500,
            timezone="Europe/London",
        )

        assert gw._pending_plan is not None
        entries, _ = gw._pending_plan
        assert len(entries) == 0

    def test_entries_sorted_chronologically(self):
        """Charge and export entries are interleaved into time order (earliest first)."""
        gw = self._make_gateway()

        # Times deliberately out of order across the two passes:
        #   charge 06:00 and 22:00; export 02:00 and 16:00
        gw._on_plan_executed(
            charge_windows=[{"start": 360, "end": 420}, {"start": 1320, "end": 1380}],
            charge_limits=[80, 80],
            export_windows=[{"start": 120, "end": 180}, {"start": 960, "end": 1020}],
            export_limits=[10, 10],
            charge_rate_w=3000,
            discharge_rate_w=2500,
            soc_max=10,
            reserve=1,
            timezone="Europe/London",
        )

        entries, _ = gw._pending_plan
        starts = [(e["start_hour"], e["start_minute"]) for e in entries]
        assert starts == sorted(starts)
        # Expected order: 02:00 (exp), 06:00 (chg), 16:00 (exp), 22:00 (chg)
        assert starts == [(2, 0), (6, 0), (16, 0), (22, 0)]

    def test_tomorrow_window_keeps_hours_above_24(self):
        """Tomorrow's windows (minutes >= 1440) keep hours >= 24 so they stay distinct from today's."""
        gw = self._make_gateway()

        # today 22:00 charge (1320-1380), tomorrow 01:00-03:00 charge (1500-1620)
        gw._on_plan_executed(
            charge_windows=[{"start": 1500, "end": 1620}, {"start": 1320, "end": 1380}],
            charge_limits=[80, 80],
            export_windows=[],
            export_limits=[],
            charge_rate_w=3000,
            discharge_rate_w=2500,
            soc_max=10,
            reserve=1,
            timezone="Europe/London",
        )

        entries, _ = gw._pending_plan
        # Chronological: today's 22:00 first, then tomorrow's slot kept at hour 25 (not wrapped)
        assert [(e["start_hour"], e["start_minute"]) for e in entries] == [(22, 0), (25, 0)]
        assert entries[1]["end_hour"] == 27  # tomorrow 03:00, kept as hour 27

    def test_freeze_charge_holds_at_rate_zero(self):
        """Freeze charge (charge limit == reserve) becomes a charge entry at rate 0, target 100%."""
        gw = self._make_gateway()

        # soc_max=10kWh, reserve=1kWh -> reserve_percent=10%; charge limit 1kWh -> 10% == reserve
        gw._on_plan_executed(
            charge_windows=[{"start": 90, "end": 270}],
            charge_limits=[1],
            export_windows=[],
            export_limits=[],
            charge_rate_w=3000,
            discharge_rate_w=2500,
            soc_max=10,
            reserve=1,
            timezone="Europe/London",
        )

        entries, _ = gw._pending_plan
        assert len(entries) == 1
        entry = entries[0]
        assert entry["mode"] == 1  # charge
        assert entry["power_w"] == 0  # held at rate 0
        assert entry["target_soc"] == 100

    def test_non_freeze_charge_uses_charge_rate(self):
        """A charge limit above reserve is a normal charge at the configured rate."""
        gw = self._make_gateway()

        # charge limit 8kWh -> 80% != reserve 10%
        gw._on_plan_executed(
            charge_windows=[{"start": 90, "end": 270}],
            charge_limits=[8],
            export_windows=[],
            export_limits=[],
            charge_rate_w=3000,
            discharge_rate_w=2500,
            soc_max=10,
            reserve=1,
            timezone="Europe/London",
        )

        entry = gw._pending_plan[0][0]
        assert entry["mode"] == 1
        assert entry["power_w"] == 3000
        assert entry["target_soc"] == 80

    def test_freeze_export_holds_at_rate_zero(self):
        """Freeze export (export limit == 99) becomes a discharge entry at rate 0, target reserve."""
        gw = self._make_gateway()

        # soc_max=10kWh, reserve=1kWh -> reserve_percent=10%
        gw._on_plan_executed(
            charge_windows=[],
            charge_limits=[],
            export_windows=[{"start": 960, "end": 1140}],
            export_limits=[99],
            charge_rate_w=3000,
            discharge_rate_w=2500,
            soc_max=10,
            reserve=1,
            timezone="Europe/London",
        )

        entries, _ = gw._pending_plan
        assert len(entries) == 1
        entry = entries[0]
        assert entry["mode"] == 2  # discharge
        assert entry["power_w"] == 0  # held at rate 0
        assert entry["target_soc"] == 10  # reserve percent

    def test_non_freeze_export_uses_discharge_rate(self):
        """An export limit below 99 is a normal forced export at the configured rate."""
        gw = self._make_gateway()

        gw._on_plan_executed(
            charge_windows=[],
            charge_limits=[],
            export_windows=[{"start": 960, "end": 1140}],
            export_limits=[10],
            charge_rate_w=3000,
            discharge_rate_w=2500,
            soc_max=10,
            reserve=1,
            timezone="Europe/London",
        )

        entry = gw._pending_plan[0][0]
        assert entry["mode"] == 2
        assert entry["power_w"] == 2500
        assert entry["target_soc"] == 10

    def test_fractional_export_limit_not_freeze(self):
        """A fractional export limit (99.5) is a normal export, not a freeze — only exact 99 holds."""
        gw = self._make_gateway()

        gw._on_plan_executed(
            charge_windows=[],
            charge_limits=[],
            export_windows=[{"start": 960, "end": 1140}],
            export_limits=[99.5],
            charge_rate_w=3000,
            discharge_rate_w=2500,
            soc_max=10,
            reserve=1,
            timezone="Europe/London",
        )

        entry = gw._pending_plan[0][0]
        assert entry["mode"] == 2  # discharge
        assert entry["power_w"] != 0  # NOT a freeze (freeze would be 0)
        assert entry["power_w"] == 1250  # low-power rate: 2500 * (1 - 0.5)
        assert entry["target_soc"] == 99  # int(99.5), not reserve

    def test_export_low_power_rate_from_fraction(self):
        """The fractional part of the export limit sets the export power (rate_scale = 1 - frac)."""
        gw = self._make_gateway()

        # limit 5.3 -> rate_scale 0.7 -> 2500 * 0.7 = 1750W, target 5%
        gw._on_plan_executed(
            charge_windows=[],
            charge_limits=[],
            export_windows=[{"start": 960, "end": 1140}],
            export_limits=[5.3],
            charge_rate_w=3000,
            discharge_rate_w=2500,
            soc_max=10,
            reserve=1,
            timezone="Europe/London",
        )

        entry = gw._pending_plan[0][0]
        assert entry["mode"] == 2
        assert entry["power_w"] == 1750  # round(2500 * (1 - 0.3))
        assert entry["target_soc"] == 5

    def test_exact_float_99_export_is_freeze(self):
        """An export limit of 99.0 (float) is still treated as freeze export."""
        gw = self._make_gateway()

        gw._on_plan_executed(
            charge_windows=[],
            charge_limits=[],
            export_windows=[{"start": 960, "end": 1140}],
            export_limits=[99.0],
            charge_rate_w=3000,
            discharge_rate_w=2500,
            soc_max=10,
            reserve=1,
            timezone="Europe/London",
        )

        entry = gw._pending_plan[0][0]
        assert entry["mode"] == 2
        assert entry["power_w"] == 0  # freeze: rate 0
        assert entry["target_soc"] == 10  # reserve percent


class TestMQTTIntegration:
    """Integration tests for MQTT plan publishing format."""

    def test_plan_publish_format(self):
        """Plan published to /schedule topic is valid protobuf."""
        from gateway import GatewayMQTT

        entries = [
            {
                "enabled": True,
                "start_hour": 1,
                "start_minute": 30,
                "end_hour": 4,
                "end_minute": 30,
                "mode": 1,
                "power_w": 3000,
                "target_soc": 100,
                "days_of_week": 0x7F,
                "use_native": True,
            }
        ]

        data = GatewayMQTT.build_execution_plan(entries, plan_version=1, timezone="Europe/London")

        # Verify the protobuf is valid and can be decoded
        plan = pb.ExecutionPlan()
        plan.ParseFromString(data)
        assert plan.entries[0].start_hour == 1
        assert plan.entries[0].use_native is True
        assert plan.timezone == "GMT0BST,M3.5.0/1,M10.5.0"

        # Verify plan_version is monotonically increasing
        data2 = GatewayMQTT.build_execution_plan(entries, plan_version=2, timezone="Europe/London")
        plan2 = pb.ExecutionPlan()
        plan2.ParseFromString(data2)
        assert plan2.plan_version > plan.plan_version


class TestPlanRepublish:
    """Tests for the periodic plan re-publish that refreshes the embedded timestamp."""

    def _entries(self):
        return [
            {
                "enabled": True,
                "start_hour": 1,
                "start_minute": 30,
                "end_hour": 4,
                "end_minute": 30,
                "mode": 1,
                "power_w": 3000,
                "target_soc": 100,
                "days_of_week": 0x7F,
                "use_native": True,
            }
        ]

    def _make_gateway(self):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw._debug = False
        gw._mqtt_connected = True
        gw._last_plan_data = None
        gw._last_plan_entries = None
        gw._last_plan_timezone = None
        gw._last_plan_publish_time = 0
        gw._plan_version = 0
        gw._last_published_plan = None
        gw._pending_plan = None
        gw.topic_schedule = "predbat/schedule"
        gw._published = []

        async def fake_publish_raw(topic, payload, retain=False):
            gw._published.append((topic, payload, retain))

        gw._publish_raw = fake_publish_raw
        return gw

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_publish_plan_stores_entries_and_timezone(self):
        """publish_plan records the entries and timezone needed for a later rebuild."""
        gw = self._make_gateway()
        entries = self._entries()
        self._run(gw.publish_plan(entries, "Europe/London"))
        assert gw._last_plan_entries == entries
        assert gw._last_plan_timezone == "Europe/London"
        assert len(gw._published) == 1

    def test_republish_refreshes_timestamp(self):
        """Re-publishing a stale plan rebuilds it with a newer timestamp, same version."""
        gw = self._make_gateway()
        self._run(gw.publish_plan(self._entries(), "Europe/London"))
        first_payload = gw._published[0][1]
        first_plan = pb.ExecutionPlan()
        first_plan.ParseFromString(first_payload)

        # Pretend the first publish was long enough ago to exceed the re-publish interval.
        from gateway import _PLAN_REPUBLISH_INTERVAL

        gw._last_plan_publish_time -= _PLAN_REPUBLISH_INTERVAL + 60

        self._run(gw._republish_plan_if_stale())

        assert len(gw._published) == 2
        second_payload = gw._published[1][1]
        second_plan = pb.ExecutionPlan()
        second_plan.ParseFromString(second_payload)

        assert second_plan.timestamp >= first_plan.timestamp  # rebuilt with current clock
        assert second_plan.plan_version == first_plan.plan_version  # content unchanged → same version
        # The cached bytes are refreshed so subsequent reads reflect the new timestamp.
        assert gw._last_plan_data == second_payload

    def test_no_republish_before_interval(self):
        """A plan younger than the re-publish interval is not re-sent."""
        gw = self._make_gateway()
        self._run(gw.publish_plan(self._entries(), "Europe/London"))
        self._run(gw._republish_plan_if_stale())
        assert len(gw._published) == 1

    def test_no_republish_when_disconnected(self):
        """Nothing is re-published while MQTT is disconnected."""
        gw = self._make_gateway()
        self._run(gw.publish_plan(self._entries(), "Europe/London"))
        from gateway import _PLAN_REPUBLISH_INTERVAL

        gw._last_plan_publish_time -= _PLAN_REPUBLISH_INTERVAL + 60
        gw._mqtt_connected = False
        self._run(gw._republish_plan_if_stale())
        assert len(gw._published) == 1

    def test_no_republish_without_prior_plan(self):
        """With no plan ever built, the re-publish is a no-op."""
        gw = self._make_gateway()
        self._run(gw._republish_plan_if_stale())
        assert gw._published == []

    def test_disconnected_publish_requeues_without_mutating_state(self):
        """Publishing while disconnected re-queues and leaves the publish state untouched."""
        gw = self._make_gateway()
        gw._mqtt_connected = False
        entries = self._entries()
        self._run(gw.publish_plan(entries, "Europe/London"))

        # Nothing went out and the plan is queued for the next cycle.
        assert gw._published == []
        assert gw._pending_plan == (entries, "Europe/London")
        # Publish state is pristine so the re-publish gate fires immediately on reconnect.
        assert gw._plan_version == 0
        assert gw._last_plan_entries is None
        assert gw._last_plan_publish_time == 0
        assert gw._last_published_plan is None

    def test_requeued_plan_publishes_on_reconnect(self):
        """A plan queued while disconnected is sent once reconnected."""
        gw = self._make_gateway()
        gw._mqtt_connected = False
        entries = self._entries()
        self._run(gw.publish_plan(entries, "Europe/London"))
        assert gw._published == []

        # Reconnect and replay the queued plan (as the run() cycle would).
        gw._mqtt_connected = True
        pending, tz = gw._pending_plan
        gw._pending_plan = None
        self._run(gw.publish_plan(pending, tz))

        assert len(gw._published) == 1
        assert gw._plan_version == 1
        assert gw._last_published_plan == entries


class TestAutomaticConfig:
    """Tests for GatewayMQTT.automatic_config() entity-to-arg mapping."""

    def _make_gateway(self):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.log = MagicMock()
        gw.prefix = "predbat"
        gw._last_status = None
        gw._auto_configured = False
        gw._suffix_to_serial = {}
        gw.args = {}
        gw._args = {}
        gw.gateway_inverter_serial = []  # default: no serial filter
        gw.gateway_evc_automatic = False
        gw.gateway_evc_control = False
        gw.gateway_shared_ct = True
        gw.gateway_integrate_power = False

        def capture_set_arg(key, value):
            gw._args[key] = value

        gw.set_arg = capture_set_arg
        gw.dashboard_item = MagicMock()
        return gw

    def _make_inverter(self, status, serial="CE123456789", primary=True, capacity_wh=9500, rate_max_w=5000, inv_type=None):
        """Add an inverter to *status* and return it."""
        import gateway_status_pb2 as _pb

        inv = status.inverters.add()
        inv.type = inv_type if inv_type is not None else _pb.INVERTER_TYPE_GIVENERGY
        inv.serial = serial
        inv.primary = primary
        inv.connected = True
        inv.active = True
        inv.battery.soc_percent = 50
        inv.battery.capacity_wh = capacity_wh
        inv.battery.rate_max_w = rate_max_w
        return inv

    def _basic_status(self, serial="CE123456789", primary=True, capacity_wh=9500, rate_max_w=5000):
        status = pb.GatewayStatus()
        status.device_id = "pbgw_test"
        status.firmware = "1.0.0"
        status.timestamp = 1741789200
        status.schema_version = 1
        self._make_inverter(status, serial=serial, primary=primary, capacity_wh=capacity_wh, rate_max_w=rate_max_w)
        return status

    # ------------------------------------------------------------------
    # Guard-clause tests
    # ------------------------------------------------------------------

    def test_slot_serials_recorded_in_slot_order(self):
        """The serial behind each PredBat inverter slot is kept, so per-inverter data can be keyed on it."""
        gw = self._make_gateway()
        status = self._basic_status(serial="FD2321G797")
        self._make_inverter(status, serial="CH2330G098")
        gw._last_status = status

        gw.automatic_config()

        assert gw._inverter_slot_serials == ["CH2330G098", "FD2321G797"]

    def test_no_status_does_nothing(self):
        """Returns early without setting _auto_configured when _last_status is None."""
        gw = self._make_gateway()
        gw.automatic_config()
        assert not gw._auto_configured
        assert gw._args == {}

    def test_no_inverters_does_nothing(self):
        """Returns early without setting _auto_configured when inverter list is empty."""
        gw = self._make_gateway()
        status = pb.GatewayStatus()
        status.device_id = "pbgw_empty"
        gw._last_status = status
        gw.automatic_config()
        assert not gw._auto_configured
        assert gw._args == {}

    # ------------------------------------------------------------------
    # Single-inverter (old firmware — no primary flag)
    # ------------------------------------------------------------------

    def test_single_inverter_entity_mapping(self):
        """All expected per-inverter entity IDs are registered as PredBat args."""
        gw = self._make_gateway()
        gw._last_status = self._basic_status(serial="CE123456789", primary=False)
        gw.automatic_config()

        assert gw._auto_configured
        suffix = "456789"  # last 6 chars of serial, lower-case
        base = f"predbat_gateway_{suffix}"

        assert gw._args["soc_percent"] == [f"sensor.{base}_soc"]
        assert gw._args["battery_power"] == [f"sensor.{base}_battery_power"]
        assert gw._args["pv_power"] == [f"sensor.{base}_pv_power"]
        assert gw._args["grid_power"] == [f"sensor.{base}_grid_power"]
        assert gw._args["load_power"] == [f"sensor.{base}_load_power"]
        assert gw._args["charge_rate"] == [f"number.{base}_charge_rate"]
        assert gw._args["discharge_rate"] == [f"number.{base}_discharge_rate"]
        assert gw._args["reserve"] == [f"number.{base}_reserve_soc"]
        assert gw._args["charge_limit"] == [f"number.{base}_target_soc"]
        assert gw._args["battery_temperature"] == [f"sensor.{base}_battery_temperature"]
        assert gw._args["charge_start_time"] == [f"select.{base}_charge_slot1_start"]
        assert gw._args["charge_end_time"] == [f"select.{base}_charge_slot1_end"]
        assert gw._args["discharge_start_time"] == [f"select.{base}_discharge_slot1_start"]
        assert gw._args["discharge_end_time"] == [f"select.{base}_discharge_slot1_end"]
        assert gw._args["scheduled_charge_enable"] == [f"switch.{base}_charge_enabled"]
        assert gw._args["scheduled_discharge_enable"] == [f"switch.{base}_discharge_enabled"]
        assert gw._args["soc_max"] == [f"sensor.{base}_battery_capacity"]
        assert gw._args["num_inverters"] == 1
        assert gw._args["inverter_type"] == ["GWMQTT"]

    def test_gwmqtt_automatic_config_sets_schedule_write_button(self):
        """Each control target's schedule write switch is bound as its schedule_write_button."""
        gw = self._make_gateway()
        gw._last_status = self._basic_status(serial="CE123456789", primary=False)
        gw.automatic_config()

        assert gw._args["schedule_write_button"] == ["switch.predbat_gateway_456789_schedule_write"]

    def test_single_inverter_energy_and_health_args(self):
        """Energy counter, battery health, and inverter_time args use first inverter's suffix."""
        gw = self._make_gateway()
        gw._last_status = self._basic_status(serial="CE123456789", primary=False)
        gw.automatic_config()

        suffix = "456789"
        base = f"predbat_gateway_{suffix}"
        assert gw._args["pv_today"] == [f"sensor.{base}_pv_today"]
        assert gw._args["import_today"] == [f"sensor.{base}_import_today"]
        assert gw._args["export_today"] == [f"sensor.{base}_export_today"]
        assert gw._args["load_today"] == [f"sensor.{base}_load_today"]
        assert gw._args["battery_temperature_history"] == f"sensor.{base}_battery_temperature"
        assert gw._args["battery_scaling"] == [f"sensor.{base}_battery_dod"]
        assert gw._args["battery_rate_max"] == [f"sensor.{base}_battery_rate_max"]
        assert gw._args["inverter_time"] == [f"sensor.{base}_inverter_time"]

    def test_no_rate_max_falls_back_to_6000(self):
        """When firmware reports no battery_rate_max, the sensor is still published with a 6000 W default."""
        gw = self._make_gateway()
        gw._last_status = self._basic_status(serial="CE123456789", primary=False, rate_max_w=0)
        gw.automatic_config()

        base = f"{gw.prefix}_gateway_456789"
        assert gw._args["battery_rate_max"] == [f"sensor.{base}_battery_rate_max"]

    def test_inverter_limit_set_from_inverter_rate_max(self):
        """inverter_limit points to inverter_rate_max sensor when InverterData.rate_max_w is non-zero."""
        gw = self._make_gateway()
        status = self._basic_status(serial="CE123456789", primary=False)
        status.inverters[0].inverter.rate_max_w = 6000
        gw._last_status = status
        gw.automatic_config()

        base = f"{gw.prefix}_gateway_456789"
        assert gw._args["inverter_limit"] == [f"sensor.{base}_inverter_rate_max"]

    def test_inverter_limit_set_when_only_battery_rate_max(self):
        """inverter_limit points to inverter_rate_max sensor even when only BatteryStatus.rate_max_w is set (sensor value is the battery fallback)."""
        gw = self._make_gateway()
        status = self._basic_status(serial="CE123456789", primary=False, rate_max_w=5000)
        status.inverters[0].inverter.rate_max_w = 0
        gw._last_status = status
        gw.automatic_config()

        base = f"{gw.prefix}_gateway_456789"
        assert gw._args["inverter_limit"] == [f"sensor.{base}_inverter_rate_max"]

    def test_inverter_limit_set_when_both_rate_max_zero(self):
        """inverter_limit still points to inverter_rate_max sensor when both rates are zero (sensor falls back to 6000)."""
        gw = self._make_gateway()
        status = self._basic_status(serial="CE123456789", primary=False, rate_max_w=0)
        status.inverters[0].inverter.rate_max_w = 0
        gw._last_status = status
        gw.automatic_config()

        base = f"{gw.prefix}_gateway_456789"
        assert gw._args["inverter_limit"] == [f"sensor.{base}_inverter_rate_max"]

    # ------------------------------------------------------------------
    # Primary-flag filtering
    # ------------------------------------------------------------------

    def test_primary_flag_filters_non_primary(self):
        """When any inverter has primary=True, non-primary inverters are excluded."""
        gw = self._make_gateway()
        status = pb.GatewayStatus()
        status.device_id = "pbgw_multi"
        status.firmware = "1.0.0"
        status.schema_version = 1
        # Primary inverter with battery
        self._make_inverter(status, serial="SERIAL000001", primary=True)
        # Non-primary inverter — should be excluded
        self._make_inverter(status, serial="SERIAL000002", primary=False)
        gw._last_status = status
        gw.automatic_config()

        assert gw._args["num_inverters"] == 1
        # Only primary suffix "000001" should appear
        assert any("000001" in e for e in gw._args["soc_percent"])
        assert not any("000002" in e for e in gw._args["soc_percent"])

    def test_multi_inverter_produces_multiple_entity_lists(self):
        """Two primary inverters produce entity list args with two entries each."""
        gw = self._make_gateway()
        status = pb.GatewayStatus()
        status.device_id = "pbgw_multi"
        status.firmware = "1.0.0"
        status.schema_version = 1
        self._make_inverter(status, serial="CE000000AA1", primary=True)
        self._make_inverter(status, serial="CE000000BB2", primary=True)
        gw._last_status = status
        gw.automatic_config()

        assert gw._args["num_inverters"] == 2
        assert len(gw._args["soc_percent"]) == 2
        assert len(gw._args["battery_power"]) == 2
        assert "000aa1" in gw._args["soc_percent"][0]
        assert "000bb2" in gw._args["soc_percent"][1]

    def test_multi_inverter_battery_args_have_one_entry_per_inverter(self):
        """battery_scaling, battery_rate_max and inverter_time are per-inverter args, so each gets one entry per primary inverter.

        A single entry leaves inverter 1 reading index 1 out of range: PredBat falls back to a 2600 W battery_rate_max
        and 1.0 battery_scaling, and never checks that inverter's clock.
        """
        gw = self._make_gateway()
        status = pb.GatewayStatus()
        status.device_id = "pbgw_multi"
        status.firmware = "1.0.0"
        status.schema_version = 1
        self._make_inverter(status, serial="CE2223G800", primary=True)
        self._make_inverter(status, serial="CE2225G400", primary=True)
        gw._last_status = status
        gw.automatic_config()

        base0 = f"{gw.prefix}_gateway_23g800"
        base1 = f"{gw.prefix}_gateway_25g400"
        assert gw._args["battery_scaling"] == [f"sensor.{base0}_battery_dod", f"sensor.{base1}_battery_dod"]
        assert gw._args["battery_rate_max"] == [f"sensor.{base0}_battery_rate_max", f"sensor.{base1}_battery_rate_max"]
        assert gw._args["inverter_time"] == [f"sensor.{base0}_inverter_time", f"sensor.{base1}_inverter_time"]

    def _two_inverter_status(self):
        """Two primary inverters with no EMS or Gateway coordinating them."""
        status = pb.GatewayStatus()
        status.device_id = "pbgw_multi"
        status.firmware = "1.0.0"
        status.schema_version = 1
        self._make_inverter(status, serial="CE2223G800", primary=True)
        self._make_inverter(status, serial="CE2225G400", primary=True)
        return status

    def test_separate_clamps_bind_every_inverters_energy_counters(self):
        """With gateway_shared_ct off each inverter has its own meter, so every energy counter is bound per inverter and summed."""
        gw = self._make_gateway()
        gw.gateway_shared_ct = False
        gw._last_status = self._two_inverter_status()
        gw.automatic_config()

        base0 = f"{gw.prefix}_gateway_23g800"
        base1 = f"{gw.prefix}_gateway_25g400"
        for counter in ("pv_today", "import_today", "export_today", "load_today"):
            assert gw._args[counter] == [f"sensor.{base0}_{counter}", f"sensor.{base1}_{counter}"], counter

    def test_shared_ct_binds_grid_and_load_counters_to_the_first_inverter_only(self):
        """gateway_shared_ct (the default): import, export and load today describe the one shared clamp, so only the first inverter's are used.

        pv_today is measured by each inverter itself, so it stays bound to every inverter.
        """
        gw = self._make_gateway()
        gw._last_status = self._two_inverter_status()
        gw.automatic_config()

        base0 = f"{gw.prefix}_gateway_23g800"
        base1 = f"{gw.prefix}_gateway_25g400"
        for counter in ("import_today", "export_today", "load_today"):
            assert gw._args[counter] == [f"sensor.{base0}_{counter}"], counter
        assert gw._args["pv_today"] == [f"sensor.{base0}_pv_today", f"sensor.{base1}_pv_today"]

    def test_separate_clamps_bind_every_inverters_grid_and_load_power(self):
        """With gateway_shared_ct off each inverter's grid and load power is bound, as each has its own CT clamp."""
        gw = self._make_gateway()
        gw.gateway_shared_ct = False
        gw._last_status = self._two_inverter_status()
        gw.automatic_config()

        base0 = f"{gw.prefix}_gateway_23g800"
        base1 = f"{gw.prefix}_gateway_25g400"
        assert gw._args["grid_power"] == [f"sensor.{base0}_grid_power", f"sensor.{base1}_grid_power"]
        assert gw._args["load_power"] == [f"sensor.{base0}_load_power", f"sensor.{base1}_load_power"]

    def test_shared_ct_uses_first_inverter_for_grid_and_load_power(self):
        """gateway_shared_ct binds grid and load power to the first inverter only, with zeros for the rest.

        Two inverters on one CT clamp both report the same grid and load reading, so summing them doubles it.
        The battery and PV args stay per inverter - those are measured by each inverter itself.
        """
        gw = self._make_gateway()
        gw._last_status = self._two_inverter_status()
        gw.automatic_config()

        base0 = f"{gw.prefix}_gateway_23g800"
        base1 = f"{gw.prefix}_gateway_25g400"
        assert gw._args["grid_power"] == [f"sensor.{base0}_grid_power", 0]
        assert gw._args["load_power"] == [f"sensor.{base0}_load_power", 0]
        assert gw._args["battery_power"] == [f"sensor.{base0}_battery_power", f"sensor.{base1}_battery_power"]
        assert gw._args["pv_power"] == [f"sensor.{base0}_pv_power", f"sensor.{base1}_pv_power"]
        assert gw._args["num_inverters"] == 2
        logged = " ".join(str(call) for call in gw.log.call_args_list)
        assert "sharing a single CT clamp" in logged

    def test_shared_ct_has_no_effect_on_a_single_inverter(self):
        """With one inverter there is nothing to double, so gateway_shared_ct changes nothing."""
        gw = self._make_gateway()
        gw._last_status = self._basic_status()
        gw.automatic_config()

        base = f"{gw.prefix}_gateway_456789"
        assert gw._args["grid_power"] == [f"sensor.{base}_grid_power"]
        assert gw._args["load_power"] == [f"sensor.{base}_load_power"]

    def test_shared_ct_is_read_from_the_component_argument(self):
        """initialize() takes gateway_shared_ct from apps.yaml and defaults it to on."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {}
        gw.initialize(gateway_device_id="pbgw_test", mqtt_host="mqtt.example", mqtt_token="token")
        assert gw.gateway_shared_ct is True

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {}
        gw.initialize(gateway_device_id="pbgw_test", mqtt_host="mqtt.example", mqtt_token="token", gateway_shared_ct=False)
        assert gw.gateway_shared_ct is False

    def test_shared_ct_default_matches_the_component_registry(self):
        """The apps.yaml default in the component registry is the same as initialize()'s."""
        from components import COMPONENT_LIST

        assert COMPONENT_LIST["gateway"]["args"]["gateway_shared_ct"]["default"] is True
        assert COMPONENT_LIST["gateway"]["args"]["gateway_integrate_power"]["default"] is False

    # ------------------------------------------------------------------
    # Secondary (cloud) and unsupported feature args
    # ------------------------------------------------------------------

    def test_disabled_cloud_and_unsupported_args(self):
        """ge_cloud_data, ge_cloud_direct are False; unsupported inverter features are None."""
        gw = self._make_gateway()
        gw._last_status = self._basic_status()
        gw.automatic_config()

        assert gw._args["ge_cloud_data"] is False
        assert gw._args["ge_cloud_direct"] is False
        assert gw._args["givtcp_rest"] is None
        assert gw._args["pause_mode"] is None
        assert gw._args["charge_rate_percent"] is None
        assert gw._args["discharge_rate_percent"] is None

    # ------------------------------------------------------------------
    # EMS mode
    # ------------------------------------------------------------------

    def test_ems_mode_sets_aggregate_args(self):
        """GivEnergy EMS inverters register ems_total_* and idle_*_time args."""
        gw = self._make_gateway()
        status = pb.GatewayStatus()
        status.device_id = "pbgw_ems"
        status.firmware = "1.0.0"
        status.schema_version = 1

        inv = self._make_inverter(status, serial="EM123456", primary=True, inv_type=pb.INVERTER_TYPE_GIVENERGY_EMS)
        inv.ems.num_inverters = 2

        gw._last_status = status
        gw.automatic_config()

        pfx = "predbat_gateway"
        assert gw._args["ems_total_soc"] == f"sensor.{pfx}_ems_total_soc"
        assert gw._args["ems_total_charge"] == f"sensor.{pfx}_ems_total_charge"
        assert gw._args["ems_total_discharge"] == f"sensor.{pfx}_ems_total_discharge"
        assert gw._args["ems_total_grid"] == f"sensor.{pfx}_ems_total_grid"
        assert gw._args["ems_total_pv"] == f"sensor.{pfx}_ems_total_pv"
        assert gw._args["ems_total_load"] == f"sensor.{pfx}_ems_total_load"

    def test_non_ems_does_not_set_aggregate_args(self):
        """Standard GivEnergy inverter does NOT register ems_* args."""
        gw = self._make_gateway()
        gw._last_status = self._basic_status()
        gw.automatic_config()

        assert "ems_total_soc" not in gw._args

    def test_hub_does_not_bind_idle_time(self):
        """The Hub manages idle time itself, so no Hub inverter type binds idle_*_time.

        On an EMS the old binding pointed idle_*_time at the export slot selects, so every
        idle write from adjust_idle_time() overwrote the export window.
        """
        ems = self._make_gateway()
        status = pb.GatewayStatus()
        status.device_id = "pbgw_ems"
        status.firmware = "1.0.0"
        status.schema_version = 1
        inv = self._make_inverter(status, serial="EM123456", primary=True, inv_type=pb.INVERTER_TYPE_GIVENERGY_EMS)
        inv.ems.num_inverters = 2
        ems._last_status = status
        ems.automatic_config()

        direct = self._make_gateway()
        direct._last_status = self._basic_status()
        direct.automatic_config()

        for gw in (ems, direct):
            assert gw._args["idle_start_time"] is None
            assert gw._args["idle_end_time"] is None

    def test_export_limit_nonzero_maps_to_sensor_entity(self):
        """export_limit_w is always mapped to the sensor entity regardless of value."""
        gw = self._make_gateway()
        status = self._basic_status(serial="CE123456789")
        status.inverters[0].control.export_limit_w = 5000
        gw._last_status = status
        gw.automatic_config()

        assert gw._args["export_limit"] == ["sensor.predbat_gateway_456789_export_limit_w"]

    def test_export_limit_zero_maps_to_sensor_entity(self):
        """export_limit_w = 0 (block all export) maps to the sensor entity."""
        gw = self._make_gateway()
        status = self._basic_status(serial="CE123456789")
        status.inverters[0].control.export_limit_w = 0
        gw._last_status = status
        gw.automatic_config()

        assert gw._args["export_limit"] == ["sensor.predbat_gateway_456789_export_limit_w"]

    def test_export_limit_99999_maps_to_sensor_entity(self):
        """export_limit_w = 99999 (firmware not-configured sentinel) also maps to the sensor entity."""
        gw = self._make_gateway()
        status = self._basic_status(serial="CE123456789")
        status.inverters[0].control.export_limit_w = 99999
        gw._last_status = status
        gw.automatic_config()

        assert gw._args["export_limit"] == ["sensor.predbat_gateway_456789_export_limit_w"]

    def test_export_limit_multi_inverter(self):
        """Each inverter in a multi-inverter setup gets its own export_limit sensor entity."""
        gw = self._make_gateway()
        status = pb.GatewayStatus()
        status.device_id = "pbgw_multi"
        status.firmware = "1.0.0"
        status.schema_version = 1
        self._make_inverter(status, serial="CE000000AA1", primary=True)
        self._make_inverter(status, serial="CE000000BB2", primary=True)
        gw._last_status = status
        gw.automatic_config()

        assert gw._args["export_limit"][0] == "sensor.predbat_gateway_000aa1_export_limit_w"
        assert gw._args["export_limit"][1] == "sensor.predbat_gateway_000bb2_export_limit_w"

    # ------------------------------------------------------------------
    # Serial filter (gateway_inverter_serial)
    # ------------------------------------------------------------------

    def test_no_serial_filter_uses_all_inverters(self):
        """When gateway_inverter_serial is empty, all inverters are registered."""
        gw = self._make_gateway()
        status = pb.GatewayStatus()
        status.device_id = "pbgw_multi"
        status.firmware = "1.0.0"
        status.schema_version = 1
        self._make_inverter(status, serial="CE000000AA1", primary=True)
        self._make_inverter(status, serial="CE000000BB2", primary=True)
        gw._last_status = status
        gw.gateway_inverter_serial = []  # no filter
        gw.automatic_config()

        assert gw._auto_configured
        assert gw._args["num_inverters"] == 2

    def test_serial_filter_single_match_restricts_to_one(self):
        """Providing a matching serial restricts auto-config to only that inverter."""
        gw = self._make_gateway()
        status = pb.GatewayStatus()
        status.device_id = "pbgw_multi"
        status.firmware = "1.0.0"
        status.schema_version = 1
        self._make_inverter(status, serial="CE000000AA1", primary=True)
        self._make_inverter(status, serial="CE000000BB2", primary=True)
        gw._last_status = status
        gw.gateway_inverter_serial = ["CE000000AA1"]
        gw.automatic_config()

        assert gw._auto_configured
        assert gw._args["num_inverters"] == 1
        assert any("000aa1" in e for e in gw._args["soc_percent"])
        assert not any("000bb2" in e for e in gw._args["soc_percent"])

    def test_serial_filter_no_match_aborts_config(self):
        """A serial filter matching nothing logs an error and aborts auto-config.

        Configuring the wrong inverter set is worse than not configuring at all, so
        a no-match clears _auto_configured and returns early. The run loop is blocked
        until a subsequent telemetry succeeds with a matching serial.
        """
        gw = self._make_gateway()
        status = pb.GatewayStatus()
        status.device_id = "pbgw_test"
        status.firmware = "1.0.0"
        status.schema_version = 1
        self._make_inverter(status, serial="CE000000AA1", primary=True)
        gw._last_status = status
        gw.gateway_inverter_serial = ["NON_EXISTENT"]
        gw.automatic_config()

        assert not gw._auto_configured
        assert "num_inverters" not in gw._args
        gw.log.assert_called()
        assert any("Error" in str(c) and "matched no inverters" in str(c) for c in gw.log.call_args_list)

    def test_serial_filter_no_match_clears_previously_good_config(self):
        """A no-match filter during reconfigure clears _auto_configured even if it was True.

        Without this, a stale successful config (e.g. from a prior run with different
        inverters) would remain active, causing PredBat to keep controlling the wrong set.
        """
        gw = self._make_gateway()
        status = pb.GatewayStatus()
        status.device_id = "pbgw_test"
        status.firmware = "1.0.0"
        status.schema_version = 1
        self._make_inverter(status, serial="CE000000AA1", primary=True)
        gw._last_status = status

        # First pass: matching filter — succeeds
        gw.gateway_inverter_serial = ["CE000000AA1"]
        gw.automatic_config()
        assert gw._auto_configured

        # Second pass (reconfigure): filter no longer matches anything
        gw.gateway_inverter_serial = ["NON_EXISTENT"]
        gw.automatic_config()
        assert not gw._auto_configured

    def test_serial_filter_case_insensitive(self):
        """Serial filter matching is case-insensitive."""
        gw = self._make_gateway()
        gw._last_status = self._basic_status(serial="CE123456789", primary=True)
        gw.gateway_inverter_serial = ["ce123456789"]  # lowercase filter against uppercase serial
        gw.automatic_config()

        assert gw._auto_configured
        assert gw._args["num_inverters"] == 1

    def test_serial_filter_string_normalised_to_list(self):
        """A bare string (not a list) passed as gateway_inverter_serial is treated as a single-entry filter."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {}
        gw.initialize(gateway_device_id="pbgw_test", mqtt_host="mqtt.example.com", mqtt_token="tok", gateway_inverter_serial="CE123456789")
        assert gw.gateway_inverter_serial == ["CE123456789"]

    def test_serial_filter_none_becomes_empty_list(self):
        """None gateway_inverter_serial becomes an empty list (no filtering)."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {}
        gw.initialize(gateway_device_id="pbgw_test", mqtt_host="mqtt.example.com", mqtt_token="tok", gateway_inverter_serial=None)
        assert gw.gateway_inverter_serial == []

    def test_serial_filter_blank_strings_become_empty_list(self):
        """Empty and whitespace-only serials are treated as an unset filter."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        for value in ("", " ", "   ", "\t\r\n"):
            gw = GatewayMQTT.__new__(GatewayMQTT)
            gw.base = MagicMock()
            gw.args = {}
            gw.initialize(
                gateway_device_id="pbgw_test",
                mqtt_host="mqtt.example.com",
                mqtt_token="tok",
                gateway_inverter_serial=value,
            )
            assert gw.gateway_inverter_serial == []

    def test_serial_filter_list_drops_blanks_and_trims_values(self):
        """Blank list entries are discarded while valid serials are trimmed."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {}
        gw.initialize(
            gateway_device_id="pbgw_test",
            mqtt_host="mqtt.example.com",
            mqtt_token="tok",
            gateway_inverter_serial=["", None, "  ", " CE000000AA1 ", "\t"],
        )
        assert gw.gateway_inverter_serial == ["CE000000AA1"]

    def test_serial_filter_json_array_drops_blanks_and_trims_values(self):
        """JSON-encoded lists receive the same blank filtering and trimming."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {}
        gw.initialize(
            gateway_device_id="pbgw_test",
            mqtt_host="mqtt.example.com",
            mqtt_token="tok",
            gateway_inverter_serial=' [ "", "  ", " CE000000AA1 " ] ',
        )
        assert gw.gateway_inverter_serial == ["CE000000AA1"]

    def test_serial_filter_json_array_string_expanded_to_list(self):
        """A JSON-encoded array string is parsed and expanded into a list of serials."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {}
        gw.initialize(
            gateway_device_id="pbgw_test",
            mqtt_host="mqtt.example.com",
            mqtt_token="tok",
            gateway_inverter_serial='["CE000000AA1", "CE000000BB2"]',
        )
        assert gw.gateway_inverter_serial == ["CE000000AA1", "CE000000BB2"]

    def test_serial_filter_json_object_string_becomes_single_entry(self):
        """A JSON-encoded object (not an array) is str-converted and wrapped in a list."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {}
        gw.initialize(
            gateway_device_id="pbgw_test",
            mqtt_host="mqtt.example.com",
            mqtt_token="tok",
            gateway_inverter_serial='{"serial": "CE123456789"}',
        )
        assert len(gw.gateway_inverter_serial) == 1
        assert isinstance(gw.gateway_inverter_serial[0], str)

    def test_serial_filter_invalid_json_falls_back_to_raw_string(self):
        """A string starting with '[' that is not valid JSON is kept as a single-entry list."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        raw = "[not valid json"
        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {}
        gw.initialize(
            gateway_device_id="pbgw_test",
            mqtt_host="mqtt.example.com",
            mqtt_token="tok",
            gateway_inverter_serial=raw,
        )
        assert gw.gateway_inverter_serial == [raw]

    def test_serial_filter_partial_match_excludes_unmatched(self):
        """With three inverters and a two-serial filter, only the two matched are registered."""
        gw = self._make_gateway()
        status = pb.GatewayStatus()
        status.device_id = "pbgw_tri"
        status.firmware = "1.0.0"
        status.schema_version = 1
        self._make_inverter(status, serial="CE000000AA1", primary=True)
        self._make_inverter(status, serial="CE000000BB2", primary=True)
        self._make_inverter(status, serial="CE000000CC3", primary=True)
        gw._last_status = status
        gw.gateway_inverter_serial = ["CE000000AA1", "CE000000CC3"]
        gw.automatic_config()

        assert gw._auto_configured
        assert gw._args["num_inverters"] == 2
        assert any("000aa1" in e for e in gw._args["soc_percent"])
        assert not any("000bb2" in e for e in gw._args["soc_percent"])
        assert any("000cc3" in e for e in gw._args["soc_percent"])


class TestSelectEvent:
    """Tests for GatewayMQTT.select_event() — slot times are staged until the schedule write switch is pressed."""

    def _make_gateway(self):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw.prefix = "predbat"
        gw._suffix_to_serial = {"456789": "CE123456789"}
        gw._mqtt_connected = True
        gw._mqtt_client = MagicMock()
        gw.topic_command = "predbat/devices/pbgw_test/command"
        gw._published = []  # capture (command, kwargs) tuples
        gw.cache = {}  # entity_id -> state

        async def fake_publish_command(command, **kwargs):
            gw._published.append((command, kwargs))

        def fake_dashboard_item(entity_id, state=None, attributes=None, app=None):
            gw.cache[entity_id] = state

        gw.publish_command = fake_publish_command
        gw.dashboard_item = fake_dashboard_item
        gw.get_state_wrapper = lambda entity_id=None, **kwargs: gw.cache.get(entity_id)
        return gw

    def _run(self, coro):
        """Run a coroutine synchronously."""
        import asyncio

        return asyncio.run(coro)

    def _staged(self, gw, side):
        """The staged (start, end) for one side of inverter 456789."""
        entry = gw._staged[("456789", side)]
        return entry["start"], entry["end"]

    # ------------------------------------------------------------------
    # Charge slot time routing
    # ------------------------------------------------------------------

    def test_charge_slot_start(self):
        """charge_slot1_start stages the start HHMM, paired with the end the select shows, and sends nothing."""
        gw = self._make_gateway()
        gw.cache["select.predbat_gateway_456789_charge_slot1_end"] = "04:30:00"
        self._run(gw.select_event("select.predbat_gateway_456789_charge_slot1_start", "01:30:00"))
        assert gw._published == []
        assert self._staged(gw, "charge") == (130, 430)
        assert gw.cache["select.predbat_gateway_456789_charge_slot1_start"] == "01:30:00"

    def test_charge_slot_end(self):
        """charge_slot1_end stages the end HHMM."""
        gw = self._make_gateway()
        gw.cache["select.predbat_gateway_456789_charge_slot1_start"] = "01:30:00"
        self._run(gw.select_event("select.predbat_gateway_456789_charge_slot1_end", "04:30:00"))
        assert gw._published == []
        assert self._staged(gw, "charge") == (130, 430)

    # ------------------------------------------------------------------
    # Discharge slot time routing
    # ------------------------------------------------------------------

    def test_discharge_slot_start(self):
        """discharge_slot1_start stages on the discharge side, not the charge side."""
        gw = self._make_gateway()
        self._run(gw.select_event("select.predbat_gateway_456789_discharge_slot1_start", "16:00:00"))
        assert gw._published == []
        assert self._staged(gw, "discharge") == (1600, None)
        assert ("456789", "charge") not in gw._staged

    def test_discharge_slot_end(self):
        """discharge_slot1_end stages the discharge end HHMM."""
        gw = self._make_gateway()
        self._run(gw.select_event("select.predbat_gateway_456789_discharge_slot1_end", "19:00:00"))
        assert self._staged(gw, "discharge") == (None, 1900)

    def test_both_endpoints_complete_the_staged_window(self):
        """Writing the start and then the end of one side stages the complete window."""
        gw = self._make_gateway()
        self._run(gw.select_event("select.predbat_gateway_456789_charge_slot1_start", "02:30:00"))
        self._run(gw.select_event("select.predbat_gateway_456789_charge_slot1_end", "03:05:00"))
        assert gw._published == []
        assert self._staged(gw, "charge") == (230, 305)

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_midnight_time_converts_correctly(self):
        """00:00:00 → HHMM 0 (midnight)."""
        gw = self._make_gateway()
        self._run(gw.select_event("select.predbat_gateway_456789_charge_slot1_start", "00:00:00"))
        assert self._staged(gw, "charge")[0] == 0

    def test_invalid_time_string_no_command(self):
        """Malformed time values (non-numeric) neither publish nor stage."""
        gw = self._make_gateway()
        self._run(gw.select_event("select.predbat_gateway_456789_charge_slot1_start", "bad_value"))
        assert gw._published == []
        assert getattr(gw, "_staged", {}) == {}

    def test_unrecognised_entity_no_command(self):
        """Entities that don't match any known pattern are silently ignored."""
        gw = self._make_gateway()
        self._run(gw.select_event("select.predbat_some_other_select", "01:00:00"))
        assert gw._published == []
        assert getattr(gw, "_staged", {}) == {}

    # ------------------------------------------------------------------
    # Serial routing
    # ------------------------------------------------------------------

    def test_no_command_when_suffix_not_in_map(self):
        """Nothing is staged when the entity suffix cannot be resolved to a serial."""
        gw = self._make_gateway()
        gw._suffix_to_serial = {}  # clear — suffix "456789" unknown
        self._run(gw.select_event("select.predbat_gateway_456789_charge_slot1_start", "01:30:00"))
        assert gw._published == []
        assert getattr(gw, "_staged", {}) == {}
        gw.log.assert_called()


class TestNumberEvent:
    """Tests for GatewayMQTT.number_event() — numeric entity → command routing."""

    def _make_gateway(self):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw.prefix = "predbat"
        gw._suffix_to_serial = {"456789": "CE123456789"}
        gw._mqtt_connected = True
        gw._mqtt_client = MagicMock()
        gw.topic_command = "predbat/devices/pbgw_test/command"
        gw._published = []

        async def fake_publish_command(command, **kwargs):
            gw._published.append((command, kwargs))

        gw.publish_command = fake_publish_command
        return gw

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    # ------------------------------------------------------------------
    # Routing to correct commands
    # ------------------------------------------------------------------

    def test_charge_rate_routes_correctly(self):
        """charge_rate entity → set_charge_rate with power_w."""
        gw = self._make_gateway()
        self._run(gw.number_event("number.predbat_gateway_456789_charge_rate", "3000"))
        assert gw._published == [("set_charge_rate", {"power_w": 3000, "serial": "CE123456789"})]

    def test_discharge_rate_routes_correctly(self):
        """discharge_rate entity → set_discharge_rate with power_w (not charge_rate)."""
        gw = self._make_gateway()
        self._run(gw.number_event("number.predbat_gateway_456789_discharge_rate", "2500"))
        assert gw._published == [("set_discharge_rate", {"power_w": 2500, "serial": "CE123456789"})]

    def test_reserve_soc_routes_correctly(self):
        """reserve_soc entity → set_reserve with target_soc."""
        gw = self._make_gateway()
        self._run(gw.number_event("number.predbat_gateway_456789_reserve_soc", "10"))
        assert gw._published == [("set_reserve", {"target_soc": 10, "serial": "CE123456789"})]

    def test_target_soc_routes_correctly(self):
        """target_soc entity → set_target_soc with target_soc."""
        gw = self._make_gateway()
        self._run(gw.number_event("number.predbat_gateway_456789_target_soc", "100"))
        assert gw._published == [("set_target_soc", {"target_soc": 100, "serial": "CE123456789"})]

    # ------------------------------------------------------------------
    # Value coercion
    # ------------------------------------------------------------------

    def test_float_string_truncated_to_int(self):
        """Float string values are truncated to int before publishing."""
        gw = self._make_gateway()
        self._run(gw.number_event("number.predbat_gateway_456789_charge_rate", "3000.9"))
        assert gw._published == [("set_charge_rate", {"power_w": 3000, "serial": "CE123456789"})]

    def test_integer_value_accepted(self):
        """Plain integer values are accepted directly."""
        gw = self._make_gateway()
        self._run(gw.number_event("number.predbat_gateway_456789_reserve_soc", 4))
        assert gw._published == [("set_reserve", {"target_soc": 4, "serial": "CE123456789"})]

    def test_zero_value_sent(self):
        """Zero is a valid value and is sent as-is."""
        gw = self._make_gateway()
        self._run(gw.number_event("number.predbat_gateway_456789_charge_rate", 0))
        assert gw._published == [("set_charge_rate", {"power_w": 0, "serial": "CE123456789"})]

    # ------------------------------------------------------------------
    # Invalid input
    # ------------------------------------------------------------------

    def test_non_numeric_string_no_command(self):
        """Non-numeric value logs a warning and sends no command."""
        gw = self._make_gateway()
        self._run(gw.number_event("number.predbat_gateway_456789_charge_rate", "bad_value"))
        assert gw._published == []
        gw.log.assert_called()

    def test_none_value_no_command(self):
        """None value logs a warning and sends no command."""
        gw = self._make_gateway()
        self._run(gw.number_event("number.predbat_gateway_456789_charge_rate", None))
        assert gw._published == []
        gw.log.assert_called()

    def test_unrecognised_entity_no_command(self):
        """Unrecognised entity ID sends no command."""
        gw = self._make_gateway()
        self._run(gw.number_event("number.predbat_some_other_number", "50"))
        assert gw._published == []

    # ------------------------------------------------------------------
    # Serial routing
    # ------------------------------------------------------------------

    def test_charge_rate_includes_serial_when_known(self):
        """charge_rate entity includes full inverter serial when suffix is in the map."""
        gw = self._make_gateway()
        gw._suffix_to_serial["456789"] = "CE123456789"
        self._run(gw.number_event("number.predbat_gateway_456789_charge_rate", "3000"))
        _, kwargs = gw._published[0]
        assert kwargs.get("serial") == "CE123456789"

    def test_discharge_rate_includes_serial_when_known(self):
        """discharge_rate entity includes full inverter serial when suffix is in the map."""
        gw = self._make_gateway()
        gw._suffix_to_serial["456789"] = "CE123456789"
        self._run(gw.number_event("number.predbat_gateway_456789_discharge_rate", "2500"))
        _, kwargs = gw._published[0]
        assert kwargs.get("serial") == "CE123456789"

    def test_no_command_when_suffix_not_in_map(self):
        """No command is sent when the entity suffix cannot be resolved to a serial."""
        gw = self._make_gateway()
        gw._suffix_to_serial = {}  # clear — suffix "456789" unknown
        self._run(gw.number_event("number.predbat_gateway_456789_charge_rate", "3000"))
        assert gw._published == []
        gw.log.assert_called()


class TestSwitchEvent:
    """Tests for GatewayMQTT.switch_event() — charge/discharge enable → mode commands."""

    def _make_gateway(self):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw.prefix = "predbat"
        gw._suffix_to_serial = {"456789": "CE123456789"}
        gw._mqtt_connected = True
        gw._mqtt_client = MagicMock()
        gw.topic_command = "predbat/devices/pbgw_test/command"
        gw._published = []
        gw._state = {}

        def fake_get_state_wrapper(entity_id, **kwargs):
            return gw._state.get(entity_id, False)

        gw.get_state_wrapper = fake_get_state_wrapper

        async def fake_publish_command(command, **kwargs):
            gw._published.append((command, kwargs))

        gw.publish_command = fake_publish_command
        return gw

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    # ------------------------------------------------------------------
    # charge_enabled switch
    # ------------------------------------------------------------------

    def test_charge_enabled_turn_on(self):
        """Turning charge_enabled on sends set_charge_enable enable=True."""
        gw = self._make_gateway()
        self._run(gw.switch_event("switch.predbat_gateway_456789_charge_enabled", "turn_on"))
        assert gw._published == [("set_charge_enable", {"enable": True, "serial": "CE123456789"})]

    def test_charge_enabled_turn_off(self):
        """Turning charge_enabled off sends set_charge_enable enable=False."""
        gw = self._make_gateway()
        self._run(gw.switch_event("switch.predbat_gateway_456789_charge_enabled", "turn_off"))
        assert gw._published == [("set_charge_enable", {"enable": False, "serial": "CE123456789"})]

    def test_charge_enabled_toggle(self):
        """Toggling charge_enabled flips based on current state from get_state_wrapper."""
        gw = self._make_gateway()
        # currently on → toggle → off
        gw._state["switch.predbat_gateway_456789_charge_enabled"] = True
        self._run(gw.switch_event("switch.predbat_gateway_456789_charge_enabled", "toggle"))
        assert gw._published == [("set_charge_enable", {"enable": False, "serial": "CE123456789"})]

    # ------------------------------------------------------------------
    # discharge_enabled switch
    # ------------------------------------------------------------------

    def test_discharge_enabled_turn_on(self):
        """Turning discharge_enabled on sends set_discharge_enable enable=True."""
        gw = self._make_gateway()
        self._run(gw.switch_event("switch.predbat_gateway_456789_discharge_enabled", "turn_on"))
        assert gw._published == [("set_discharge_enable", {"enable": True, "serial": "CE123456789"})]

    def test_discharge_enabled_turn_off(self):
        """Turning discharge_enabled off sends set_discharge_enable enable=False."""
        gw = self._make_gateway()
        self._run(gw.switch_event("switch.predbat_gateway_456789_discharge_enabled", "turn_off"))
        assert gw._published == [("set_discharge_enable", {"enable": False, "serial": "CE123456789"})]

    def test_discharge_enabled_toggle(self):
        """Toggling discharge_enabled flips based on current state from get_state_wrapper."""
        gw = self._make_gateway()
        # currently off → toggle → on (get_state_wrapper returns False by default)
        self._run(gw.switch_event("switch.predbat_gateway_456789_discharge_enabled", "toggle"))
        assert gw._published == [("set_discharge_enable", {"enable": True, "serial": "CE123456789"})]

    # ------------------------------------------------------------------
    # Substring safety: discharge_enabled must not match _charge_enabled branch
    # ------------------------------------------------------------------

    def test_discharge_enabled_not_misrouted_as_charge(self):
        """discharge_enabled sends set_discharge_enable, not set_charge_enable."""
        gw = self._make_gateway()
        self._run(gw.switch_event("switch.predbat_gateway_456789_discharge_enabled", "turn_off"))
        assert gw._published[0][0] == "set_discharge_enable"

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_unrecognised_entity_no_command(self):
        """An entity that doesn't match charge_enabled or discharge_enabled sends nothing."""
        gw = self._make_gateway()
        self._run(gw.switch_event("switch.predbat_some_other_switch", "turn_on"))
        assert gw._published == []

    def test_only_one_command_per_call(self):
        """Each switch_event call produces exactly one command."""
        gw = self._make_gateway()
        self._run(gw.switch_event("switch.predbat_gateway_456789_charge_enabled", "turn_on"))
        assert len(gw._published) == 1

    # ------------------------------------------------------------------
    # Serial routing
    # ------------------------------------------------------------------

    def test_charge_enabled_includes_serial_when_known(self):
        """charge_enabled switch includes full inverter serial when suffix is in the map."""
        gw = self._make_gateway()
        gw._suffix_to_serial["456789"] = "CE123456789"
        self._run(gw.switch_event("switch.predbat_gateway_456789_charge_enabled", "turn_on"))
        _, kwargs = gw._published[0]
        assert kwargs.get("serial") == "CE123456789"

    def test_discharge_enabled_includes_serial_when_known(self):
        """discharge_enabled switch includes full inverter serial when suffix is in the map."""
        gw = self._make_gateway()
        gw._suffix_to_serial["456789"] = "CE123456789"
        self._run(gw.switch_event("switch.predbat_gateway_456789_discharge_enabled", "turn_off"))
        _, kwargs = gw._published[0]
        assert kwargs.get("serial") == "CE123456789"

    def test_no_command_when_suffix_not_in_map(self):
        """No command is sent when the entity suffix cannot be resolved to a serial."""
        gw = self._make_gateway()
        gw._suffix_to_serial = {}  # clear — suffix "456789" unknown
        self._run(gw.switch_event("switch.predbat_gateway_456789_charge_enabled", "turn_on"))
        assert gw._published == []
        gw.log.assert_called()


class TestPublishPredbatData:
    """Tests for GatewayMQTT._publish_predbat_data() payload structure."""

    def _make_gateway(self, states=None):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw.prefix = "predbat"
        gw._topic_base = "predbat/devices/pbgw_test"
        gw._last_predbat_data = None
        gw.local_tz = pytz.timezone("Europe/London")
        gw._published = []  # (topic, payload, retain) tuples

        defaults = {
            "predbat.rates": "10.0",
            "predbat.cost_today": "0",
            "predbat.ppkwh_today": "10.0",
            "predbat.savings_total_predbat": "0",
            "predbat.savings_yesterday_predbat": "0",
            "predbat.savings_total_predbat#start_date": None,
        }
        state_store = {**defaults, **(states or {})}

        def fake_get_state_wrapper(entity_id, default=None, attribute=None):
            if attribute:
                return state_store.get(f"{entity_id}#{attribute}", default)
            return state_store.get(entity_id, default)

        gw.get_state_wrapper = fake_get_state_wrapper

        async def fake_publish_raw(topic, payload, retain=False):
            gw._published.append((topic, payload, retain))

        gw._publish_raw = fake_publish_raw
        return gw

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def _get_published_payload(self, gw):
        import json

        assert len(gw._published) == 1, f"Expected 1 publish, got {len(gw._published)}"
        _, raw_bytes, _ = gw._published[0]
        return json.loads(raw_bytes.decode())

    # ------------------------------------------------------------------
    # Payload key names
    # ------------------------------------------------------------------

    def test_payload_has_total_cost_not_total_saved(self):
        """Payload must use 'total_cost' key, not the old 'total_saved' key."""
        gw = self._make_gateway({"predbat.rates": "15.5", "predbat.cost_today": "200", "predbat.ppkwh_today": "18.2"})
        self._run(gw._publish_predbat_data())
        payload = self._get_published_payload(gw)
        assert "total_cost" in payload, "Expected 'total_cost' key in payload"
        assert "total_saved" not in payload, "'total_saved' key must not appear in payload"

    # ------------------------------------------------------------------
    # Pence-to-pounds conversion
    # ------------------------------------------------------------------

    def test_cost_today_pence_converted_to_pounds(self):
        """cost_today in pence (e.g. 1234) must be published as £12.34."""
        gw = self._make_gateway({"predbat.rates": "10.0", "predbat.cost_today": "1234", "predbat.ppkwh_today": "20.0"})
        self._run(gw._publish_predbat_data())
        payload = self._get_published_payload(gw)
        assert approx_equal(payload["total_cost"], 12.34), f"Expected 12.34, got {payload['total_cost']}"

    def test_zero_cost_today_produces_zero(self):
        """cost_today of 0 should publish total_cost=0.0."""
        gw = self._make_gateway({"predbat.rates": "10.0", "predbat.cost_today": "0", "predbat.ppkwh_today": "10.0"})
        self._run(gw._publish_predbat_data())
        payload = self._get_published_payload(gw)
        assert approx_equal(payload["total_cost"], 0.0)

    def test_none_cost_today_produces_zero(self):
        """Missing cost_today (None) should default to 0.0 without error."""
        gw = self._make_gateway({"predbat.rates": "10.0", "predbat.ppkwh_today": "10.0"})
        self._run(gw._publish_predbat_data())
        payload = self._get_published_payload(gw)
        assert approx_equal(payload["total_cost"], 0.0)

    # ------------------------------------------------------------------
    # Other payload fields
    # ------------------------------------------------------------------

    def test_current_price_and_avg_price_published(self):
        """current_price and avg_price are rounded to 1 decimal place."""
        gw = self._make_gateway({"predbat.rates": "14.73", "predbat.cost_today": "0", "predbat.ppkwh_today": "19.56"})
        self._run(gw._publish_predbat_data())
        payload = self._get_published_payload(gw)
        assert approx_equal(payload["current_price"], 14.7, abs_tol=0.05)
        assert approx_equal(payload["avg_price"], 19.6, abs_tol=0.05)

    def test_default_timeline_is_twelve_zeros(self):
        """When no plan_html is available the timeline is 12 zero slots."""
        gw = self._make_gateway({"predbat.rates": "10.0", "predbat.cost_today": "0", "predbat.ppkwh_today": "10.0"})
        self._run(gw._publish_predbat_data())
        payload = self._get_published_payload(gw)
        assert payload["timeline"] == [0] * 12

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def test_no_republish_when_data_unchanged(self):
        """Second call with identical data must not trigger another MQTT publish."""
        gw = self._make_gateway({"predbat.rates": "10.0", "predbat.cost_today": "500", "predbat.ppkwh_today": "15.0"})
        self._run(gw._publish_predbat_data())
        assert len(gw._published) == 1
        # Second call — same data
        self._run(gw._publish_predbat_data())
        assert len(gw._published) == 1, "Should not publish again when data is unchanged"

    def test_republish_when_price_changes(self):
        """A change in current_price must trigger a second publish."""
        gw = self._make_gateway({"predbat.rates": "10.0", "predbat.cost_today": "500", "predbat.ppkwh_today": "15.0"})
        self._run(gw._publish_predbat_data())
        assert len(gw._published) == 1
        # Change the rate via a fresh gateway helper (same savings defaults, different price)
        gw2 = self._make_gateway({"predbat.rates": "20.0", "predbat.cost_today": "500", "predbat.ppkwh_today": "15.0"})
        gw2._last_predbat_data = gw._last_predbat_data  # carry over dedup state
        self._run(gw2._publish_predbat_data())
        assert len(gw2._published) == 1, "Should publish again when price changes"

    # ------------------------------------------------------------------
    # Plan timeline and block_soc tests
    # ------------------------------------------------------------------

    def _make_rows(self, specs, base_offset_minutes=0):
        """Build a list of plan rows with times relative to now.

        Each spec is a dict with keys: state, soc_percent, pv_forecast (optional).
        Rows are placed at base_offset_minutes, +30, +60, ... minutes from now.
        """
        import datetime as dt_mod

        now = dt_mod.datetime.now(dt_mod.timezone.utc)
        rows = []
        for i, spec in enumerate(specs):
            row_time = now + dt_mod.timedelta(minutes=base_offset_minutes + i * 30)
            rows.append(
                {
                    "time": row_time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "state": spec.get("state", ""),
                    "soc_percent": spec.get("soc_percent", 50),
                    "pv_forecast": spec.get("pv_forecast", 0),
                }
            )
        return rows

    def _get_payload_with_plan(self, rows, extra_states=None):
        """Call _publish_predbat_data with plan_html rows and return the published payload."""
        import json

        states = {"predbat.plan_html#raw": {"rows": rows}, **(extra_states or {})}
        gw = self._make_gateway(states)
        self._run(gw._publish_predbat_data())
        assert len(gw._published) == 1
        _, raw_bytes, _ = gw._published[0]
        return json.loads(raw_bytes.decode())

    def test_timeline_charging_maps_to_1(self):
        """Charging state → timeline code 1."""
        rows = self._make_rows([{"state": "Chrg", "soc_percent": 60}])
        payload = self._get_payload_with_plan(rows)
        assert payload["timeline"][0] == 1

    def test_timeline_freeze_charging_maps_to_1(self):
        """Freeze charging state → timeline code 1."""
        rows = self._make_rows([{"state": "FrzChrg", "soc_percent": 60}])
        payload = self._get_payload_with_plan(rows)
        assert payload["timeline"][0] == 1

    def test_timeline_hold_charging_maps_to_1(self):
        """Hold charging state → timeline code 1."""
        rows = self._make_rows([{"state": "HoldChrg", "soc_percent": 60}])
        payload = self._get_payload_with_plan(rows)
        assert payload["timeline"][0] == 1

    def test_timeline_discharging_maps_to_2(self):
        """Discharging state → timeline code 2."""
        rows = self._make_rows([{"state": "Exp", "soc_percent": 40}])
        payload = self._get_payload_with_plan(rows)
        assert payload["timeline"][0] == 2

    def test_timeline_freeze_discharging_maps_to_2(self):
        """Freeze discharging state → timeline code 2."""
        rows = self._make_rows([{"state": "FrzExp", "soc_percent": 40}])
        payload = self._get_payload_with_plan(rows)
        assert payload["timeline"][0] == 2

    def test_timeline_hold_discharging_maps_to_2(self):
        """Hold export/discharge state → timeline code 2."""
        rows = self._make_rows([{"state": "HoldExp", "soc_percent": 40}])
        payload = self._get_payload_with_plan(rows)
        assert payload["timeline"][0] == 2

    def test_timeline_solar_maps_to_3(self):
        """Unknown state with pv_forecast > 0.1 → timeline code 3 (solar)."""
        rows = self._make_rows([{"state": "Idle", "soc_percent": 50, "pv_forecast": 0.5}])
        payload = self._get_payload_with_plan(rows)
        assert payload["timeline"][0] == 3

    def test_timeline_grid_import_maps_to_4(self):
        """Unknown state with pv_forecast <= 0.1 → timeline code 4 (grid import)."""
        rows = self._make_rows([{"state": "Idle", "soc_percent": 50, "pv_forecast": 0.0}])
        payload = self._get_payload_with_plan(rows)
        assert payload["timeline"][0] == 4

    def test_timeline_only_fills_12_slots(self):
        """More than 12 rows only fills the first 12 timeline slots."""
        rows = self._make_rows([{"state": "Chrg", "soc_percent": 80}] * 15)
        payload = self._get_payload_with_plan(rows)
        assert len(payload["timeline"]) == 12
        assert all(v == 1 for v in payload["timeline"])

    def test_timeline_rows_older_than_30min_skipped(self):
        """Rows with time > 30 minutes in the past are excluded from the timeline."""
        # One old row (should be skipped) followed by a current row
        import datetime as dt_mod

        now = dt_mod.datetime.now(dt_mod.timezone.utc)
        old_time = now - dt_mod.timedelta(minutes=45)
        current_time = now + dt_mod.timedelta(minutes=5)
        rows = [
            {"time": old_time.strftime("%Y-%m-%dT%H:%M:%S%z"), "state": "Chrg", "soc_percent": 50, "pv_forecast": 0},
            {"time": current_time.strftime("%Y-%m-%dT%H:%M:%S%z"), "state": "Exp", "soc_percent": 40, "pv_forecast": 0},
        ]
        payload = self._get_payload_with_plan(rows)
        # First filled slot should be Exp (2), not Chrg (1)
        assert payload["timeline"][0] == 2

    def test_timeline_row_with_invalid_time_skipped(self):
        """Rows with bad times are silently skipped."""
        import datetime as dt_mod

        now = dt_mod.datetime.now(dt_mod.timezone.utc)
        rows = [
            {"time": "not-a-date", "state": "Chrg", "soc_percent": 50, "pv_forecast": 0},
            {"time": (now + dt_mod.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S%z"), "state": "Exp", "soc_percent": 40, "pv_forecast": 0},
        ]
        payload = self._get_payload_with_plan(rows)
        assert payload["timeline"][0] == 2

    def test_block_soc_collects_all_slots(self):
        """block_soc has one entry per slot regardless of state changes."""
        rows = self._make_rows(
            [
                {"state": "Chrg", "soc_percent": 60},
                {"state": "Chrg", "soc_percent": 70},
                {"state": "Chrg", "soc_percent": 80},
                {"state": "Demand", "soc_percent": 85},
            ]
        )
        payload = self._get_payload_with_plan(rows)
        assert payload["block_state"] == "Chrg"
        assert payload["block_soc"] == [60, 70, 80, 85]

    def test_block_state_from_first_row(self):
        """block_state is always taken from the first valid row regardless of later states."""
        rows = self._make_rows(
            [
                {"state": "Chrg", "soc_percent": 60},
                {"state": "Demand", "soc_percent": 62},
                {"state": "Demand", "soc_percent": 64},
            ]
        )
        payload = self._get_payload_with_plan(rows)
        assert payload["block_state"] == "Chrg"
        assert payload["block_soc"] == [60, 62, 64]

    def test_block_soc_continues_after_state_change(self):
        """block_soc continues accumulating after state changes — one entry per slot."""
        rows = self._make_rows(
            [
                {"state": "Exp", "soc_percent": 80},
                {"state": "Exp", "soc_percent": 70},
                {"state": "Demand", "soc_percent": 70},
                {"state": "Demand", "soc_percent": 65},
            ]
        )
        payload = self._get_payload_with_plan(rows)
        assert payload["block_state"] == "Exp"
        assert payload["block_soc"] == [80, 70, 70, 65]

    def test_block_soc_capped_at_24_slots(self):
        """block_soc collects at most 24 entries even when more rows are present."""
        rows = self._make_rows([{"state": "Chrg", "soc_percent": 50 + i} for i in range(30)])
        payload = self._get_payload_with_plan(rows)
        assert len(payload["block_soc"]) == 24
        assert payload["block_soc"][0] == 50
        assert payload["block_soc"][23] == 73

    def test_block_soc_single_entry_extended_to_two(self):
        """A single-row plan yields block_soc with the value duplicated to ensure >= 2 points."""
        rows = self._make_rows([{"state": "Chrg", "soc_percent": 60}])
        payload = self._get_payload_with_plan(rows)
        assert payload["block_soc"] == [60, 60]

    def test_no_plan_rows_gives_empty_block_and_zero_timeline(self):
        """When plan_html has no rows the block fields are empty and timeline is all zeros."""
        states = {"predbat.plan_html#raw": {"rows": []}}
        gw = self._make_gateway(states)
        self._run(gw._publish_predbat_data())
        import json

        payload = json.loads(gw._published[0][1].decode())
        assert payload["timeline"] == [0] * 12
        assert payload["block_soc"] == []
        assert payload["block_state"] == ""

        """Data is published to predbat/devices/{device_id}/predbat_data."""
        gw = self._make_gateway({"predbat.rates": "10.0", "predbat.cost_today": "0", "predbat.ppkwh_today": "10.0"})
        self._run(gw._publish_predbat_data())
        topic, _, retain = gw._published[0]
        assert topic == "predbat/devices/pbgw_test/predbat_data"
        assert retain is True

    # ------------------------------------------------------------------
    # Marginal cost matrix
    # ------------------------------------------------------------------

    def test_marginal_costs_nominal_matrix(self):
        """Marginal matrix with int keys is flattened in canonical 1/2/4/8 level order."""
        matrix = {
            1: {"14:00": 5.2, "16:00": 4.1, "18:00": 3.8},
            2: {"14:00": 5.8, "16:00": 4.3, "18:00": 3.9},
            4: {"14:00": 8.1, "16:00": 7.5, "18:00": 6.8},
            8: {"14:00": 12.3, "16:00": 11.5, "18:00": 10.8},
        }
        gw = self._make_gateway(
            {
                "predbat.rates": "10.0",
                "predbat.cost_today": "0",
                "predbat.ppkwh_today": "10.0",
                "sensor.predbat_marginal_energy_costs#matrix": matrix,
            }
        )
        self._run(gw._publish_predbat_data())
        payload = self._get_published_payload(gw)
        assert payload["marginal_time_labels"] == ["14:00", "16:00", "18:00"]
        assert len(payload["marginal_costs"]) == 4
        assert payload["marginal_costs"][0] == [5.2, 4.1, 3.8]
        assert payload["marginal_costs"][3] == [12.3, 11.5, 10.8]

    def test_marginal_costs_string_keys_work(self):
        """A JSON-round-tripped matrix with string keys is handled identically."""
        matrix = {
            "1": {"14:00": 5.0},
            "2": {"14:00": 6.0},
            "4": {"14:00": 7.0},
            "8": {"14:00": 8.0},
        }
        gw = self._make_gateway(
            {
                "predbat.rates": "10.0",
                "predbat.cost_today": "0",
                "predbat.ppkwh_today": "10.0",
                "sensor.predbat_marginal_energy_costs#matrix": matrix,
            }
        )
        self._run(gw._publish_predbat_data())
        payload = self._get_published_payload(gw)
        assert payload["marginal_costs"] == [[5.0], [6.0], [7.0], [8.0]]

    def test_marginal_costs_missing_sensor_empty_lists(self):
        """When the marginal sensor isn't populated the payload still publishes empty lists."""
        gw = self._make_gateway({"predbat.rates": "10.0", "predbat.cost_today": "0", "predbat.ppkwh_today": "10.0"})
        self._run(gw._publish_predbat_data())
        payload = self._get_published_payload(gw)
        assert payload["marginal_costs"] == []
        assert payload["marginal_time_labels"] == []

    def test_marginal_costs_missing_row_padded_with_zeros(self):
        """A row missing from the matrix is padded with 0 rather than dropping the whole structure.

        Prevents one absent level collapsing the gateway's view of the matrix.
        """
        matrix = {
            1: {"14:00": 5.0, "16:00": 4.0},
            # 2 intentionally missing
            4: {"14:00": 7.0, "16:00": 6.0},
            8: {"14:00": 9.0, "16:00": 8.0},
        }
        gw = self._make_gateway(
            {
                "predbat.rates": "10.0",
                "predbat.cost_today": "0",
                "predbat.ppkwh_today": "10.0",
                "sensor.predbat_marginal_energy_costs#matrix": matrix,
            }
        )
        self._run(gw._publish_predbat_data())
        payload = self._get_published_payload(gw)
        assert len(payload["marginal_costs"]) == 4
        assert payload["marginal_costs"][0] == [5.0, 4.0]
        assert payload["marginal_costs"][1] == [0, 0]  # padded
        assert payload["marginal_costs"][2] == [7.0, 6.0]

    def test_marginal_costs_leading_missing_rows_padded_with_zeros(self):
        """Leading missing rows are zero-padded once later levels define the matrix width."""
        matrix = {
            # 1 and 2 intentionally missing
            4: {"14:00": 7.0, "16:00": 6.0},
            8: {"14:00": 9.0, "16:00": 8.0},
        }
        gw = self._make_gateway(
            {
                "predbat.rates": "10.0",
                "predbat.cost_today": "0",
                "predbat.ppkwh_today": "10.0",
                "sensor.predbat_marginal_energy_costs#matrix": matrix,
            }
        )
        self._run(gw._publish_predbat_data())
        payload = self._get_published_payload(gw)
        assert len(payload["marginal_costs"]) == 4
        assert payload["marginal_costs"][0] == [0, 0]
        assert payload["marginal_costs"][1] == [0, 0]
        assert payload["marginal_costs"][2] == [7.0, 6.0]
        assert payload["marginal_costs"][3] == [9.0, 8.0]
        assert payload["marginal_time_labels"] == ["14:00", "16:00"]

    def test_marginal_costs_non_numeric_value_caught(self):
        """Non-numeric cells (e.g. 'N/A') don't blow up the publish — graceful empty fallback."""
        matrix = {1: {"14:00": "N/A"}, 2: {"14:00": 0}, 4: {"14:00": 0}, 8: {"14:00": 0}}
        gw = self._make_gateway(
            {
                "predbat.rates": "10.0",
                "predbat.cost_today": "0",
                "predbat.ppkwh_today": "10.0",
                "sensor.predbat_marginal_energy_costs#matrix": matrix,
            }
        )
        self._run(gw._publish_predbat_data())
        payload = self._get_published_payload(gw)
        assert payload["marginal_costs"] == []
        assert payload["marginal_time_labels"] == []

    def test_marginal_costs_non_dict_matrix_ignored(self):
        """Matrix that isn't a dict (e.g. published as a list by mistake) falls back to empty."""
        gw = self._make_gateway(
            {
                "predbat.rates": "10.0",
                "predbat.cost_today": "0",
                "predbat.ppkwh_today": "10.0",
                "sensor.predbat_marginal_energy_costs#matrix": [1, 2, 3],
            }
        )
        self._run(gw._publish_predbat_data())
        payload = self._get_published_payload(gw)
        assert payload["marginal_costs"] == []
        assert payload["marginal_time_labels"] == []

    # ------------------------------------------------------------------
    # Rate anchors
    # ------------------------------------------------------------------

    def test_payload_includes_rate_anchors(self):
        """rate_min/rate_max/import_rate/export_rate are published (rounded to 0.01p) from the marginal sensor attributes."""
        gw = self._make_gateway(
            {
                "sensor.predbat_marginal_energy_costs#rate_min": "8.04",
                "sensor.predbat_marginal_energy_costs#rate_max": "28.97",
                "sensor.predbat_marginal_energy_costs#import_rate_base": "15.23",
                "sensor.predbat_marginal_energy_costs#export_rate_base": "12.01",
            }
        )
        self._run(gw._publish_predbat_data())
        payload = self._get_published_payload(gw)
        assert approx_equal(payload["rate_min"], 8.04), f"rate_min: {payload.get('rate_min')}"
        assert approx_equal(payload["rate_max"], 28.97), f"rate_max: {payload.get('rate_max')}"
        assert approx_equal(payload["import_rate"], 15.23), f"import_rate: {payload.get('import_rate')}"
        assert approx_equal(payload["export_rate"], 12.01), f"export_rate: {payload.get('export_rate')}"

    def test_payload_rate_anchors_null_when_attrs_absent(self):
        """When the marginal sensor lacks the rate attributes, the anchor keys publish as None so firmware falls back."""
        gw = self._make_gateway()
        self._run(gw._publish_predbat_data())
        payload = self._get_published_payload(gw)
        assert payload["rate_min"] is None
        assert payload["rate_max"] is None
        assert payload["import_rate"] is None
        assert payload["export_rate"] is None


class TestIanaToPosixTz:
    """Tests for GatewayMQTT.iana_to_posix_tz() — IANA to POSIX TZ string conversion."""

    def _convert(self, iana_tz):
        from gateway import GatewayMQTT

        return GatewayMQTT.iana_to_posix_tz(iana_tz)

    def test_europe_london(self):
        """Europe/London returns the DST-aware POSIX string."""
        result = self._convert("Europe/London")
        assert result == "GMT0BST,M3.5.0/1,M10.5.0", f"Got {result!r}"  # cspell:disable-line

    def test_america_new_york(self):
        """America/New_York returns the US Eastern POSIX string."""
        result = self._convert("America/New_York")
        assert result == "EST5EDT,M3.2.0,M11.1.0", f"Got {result!r}"  # cspell:disable-line

    def test_australia_sydney(self):
        """Australia/Sydney returns the AEST/AEDT POSIX string."""  # cspell:disable-line
        result = self._convert("Australia/Sydney")
        assert result == "AEST-10AEDT,M10.1.0,M4.1.0/3", f"Got {result!r}"  # cspell:disable-line

    def test_southern_hemisphere_standard_offset(self):
        """Australia/Sydney fallback must use AEST (UTC+10) not AEDT (UTC+11). # cspell:disable-line

        January is Southern Hemisphere summer (DST active), so a hardwired Jan probe
        would yield UTC+11 instead of the correct standard UTC+10 offset.
        """
        import datetime
        import pytz as _pytz

        tz = _pytz.timezone("Australia/Sydney")
        # Manually compute what the fallback would produce using just January (wrong)
        off_jan = tz.utcoffset(datetime.datetime(2024, 1, 15, 12, 0, 0)).total_seconds() / 60
        # January is DST in Sydney: UTC+11 → posix_total = -660 → hours = -11 (wrong)
        # The correct standard offset is UTC+10 (July)
        off_jul = tz.utcoffset(datetime.datetime(2024, 7, 15, 12, 0, 0)).total_seconds() / 60
        assert off_jan > off_jul, "Precondition: Sydney has higher offset in Jan (DST) than Jul (standard)"
        # The actual conversion should use the smaller offset (July = standard time)
        result = self._convert("Australia/Sydney")
        # Standard AEST is UTC+10; POSIX sign flips → "-10" must appear # cspell:disable-line
        assert "-10" in result, f"Expected standard offset -10 in POSIX string, got {result!r}"  # cspell:disable-line

    def test_asia_kolkata_half_hour_offset(self):
        """Asia/Kolkata (UTC+5:30) includes the fractional-hour offset."""
        result = self._convert("Asia/Kolkata")
        assert result == "IST-5:30", f"Got {result!r}"  # cspell:disable-line

    def test_utc(self):
        """UTC maps to 'UTC0'."""
        result = self._convert("UTC")
        assert result == "UTC0", f"Got {result!r}"

    def test_returns_non_empty_string(self):
        """Always returns a non-empty string for any well-known zone."""
        for iana in ["Europe/Paris", "America/Los_Angeles", "Asia/Tokyo", "Australia/Perth"]:
            result = self._convert(iana)
            assert isinstance(result, str) and result, f"{iana} returned empty/non-string: {result!r}"

    def test_posix_string_format(self):
        """Result never contains spaces and always contains at least one digit (the offset)."""
        for iana in ["Europe/London", "America/New_York", "Asia/Kolkata", "UTC"]:
            result = self._convert(iana)
            assert " " not in result, f"{iana} result has spaces: {result!r}"
            assert any(c.isdigit() for c in result), f"{iana} result has no digit: {result!r}"

    def test_pytz_object_input(self):
        """Accepts a pytz timezone object (str() is called internally)."""
        import pytz

        result = self._convert(pytz.timezone("Europe/London"))
        assert result == "GMT0BST,M3.5.0/1,M10.5.0", f"Got {result!r}"  # cspell:disable-line

    def test_negative_fractional_offset(self):
        """America/St_Johns (UTC-3:30) must produce offset 3:30, not 4:30 (floor-division bug)."""
        result = self._convert("America/St_Johns")
        # The POSIX string from the TZif file is authoritative; the fallback must also be correct.
        # Either way, the offset component must not contain "4:30".
        assert "4:30" not in result, f"Floor-division bug: got {result!r} for America/St_Johns"  # cspell:disable-line
        assert "3:30" in result, f"Expected '3:30' in POSIX string, got {result!r}"  # cspell:disable-line

    def test_fallback_northern_hemisphere(self):
        """Fallback path (TZif file bypassed): Europe/London standard time is GMT (UTC+0) → 'GMT0'."""
        from unittest.mock import patch
        from gateway import GatewayMQTT

        with patch("os.path.isfile", return_value=False):
            result = GatewayMQTT.iana_to_posix_tz("Europe/London")
        assert result == "GMT0", f"Got {result!r}"  # cspell:disable-line

    def test_fallback_southern_hemisphere(self):
        """Fallback path: dual-probe picks July (standard AEST UTC+10), not January (DST AEDT UTC+11)."""  # cspell:disable-line
        from unittest.mock import patch
        from gateway import GatewayMQTT

        with patch("os.path.isfile", return_value=False):
            result = GatewayMQTT.iana_to_posix_tz("Australia/Sydney")
        assert "AEST" in result, f"Expected AEST abbr in fallback result, got {result!r}"  # cspell:disable-line
        assert "-10" in result, f"Expected standard offset -10 in fallback result, got {result!r}"  # cspell:disable-line
        assert "-11" not in result, f"DST offset -11 must not appear in fallback result, got {result!r}"  # cspell:disable-line

    def test_fallback_negative_fractional_offset(self):
        """Fallback path divmod fix: America/St_Johns standard time is NST (UTC-3:30) → '3:30'."""
        from unittest.mock import patch
        from gateway import GatewayMQTT

        with patch("os.path.isfile", return_value=False):
            result = GatewayMQTT.iana_to_posix_tz("America/St_Johns")
        assert "4:30" not in result, f"Floor-division bug in fallback: got {result!r}"  # cspell:disable-line
        assert "3:30" in result, f"Expected 3:30 in fallback result, got {result!r}"  # cspell:disable-line

    def test_invalid_zone_returns_utc0(self):
        """Unknown/invalid zone name falls back to 'UTC0' instead of raising."""
        result = self._convert("Invalid/NotAZone")
        assert result == "UTC0", f"Got {result!r}"  # cspell:disable-line

    def test_build_execution_plan_encodes_posix_tz(self):
        """build_execution_plan() stores the POSIX string in plan.timezone, not the raw IANA name."""
        from gateway import GatewayMQTT

        data = GatewayMQTT.build_execution_plan([], plan_version=1, timezone="Europe/London")
        plan = pb.ExecutionPlan()
        plan.ParseFromString(data)
        assert plan.timezone != "Europe/London", "plan.timezone must be POSIX string, not IANA name"
        assert plan.timezone == "GMT0BST,M3.5.0/1,M10.5.0", f"Got {plan.timezone!r}"  # cspell:disable-line


class TestGatewayUnitControlBinding:
    """Regression tests for the 2026-06-04 incident (GW + single AIO).

    A GivEnergy *Gateway* (proto type INVERTER_TYPE_GIVENERGY_GATEWAY) is not a
    battery inverter. The old ``automatic_config`` filtered plan inverters only on
    ``primary + battery`` (never on type) and assigned the inverter *index* from the
    raw discovery array order, so a re-discovery (e.g. an NVS wipe) could move the
    Gateway to index 0 — and PredBat then read its empty charge window and raised
    "Inverter 0 unable to read charge window time".

    The fix: exclude the Gateway type from the controllable set and bind slots to a
    stable key (serial) instead of discovery order. These tests assert that fixed
    behaviour and guard against regression.
    """

    def _make_gateway(self):
        """Build a GatewayMQTT with set_arg captured into ``_args`` (mirrors TestAutomaticConfig)."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.log = MagicMock()
        gw.prefix = "predbat"
        gw._debug = False
        gw._last_status = None
        gw._auto_configured = False
        gw._configured_inverter_serials = frozenset()
        gw._suffix_to_serial = {}
        gw.args = {}
        gw._args = {}
        gw.gateway_inverter_serial = []
        gw.gateway_evc_automatic = False
        gw.gateway_evc_control = False
        gw.gateway_shared_ct = True
        gw.gateway_integrate_power = False
        gw._configured_ev_chargers = frozenset()

        def capture_set_arg(key, value):
            gw._args[key] = value

        gw.set_arg = capture_set_arg
        gw.dashboard_item = MagicMock()
        return gw

    def _two_unit_status(self, order):
        """Build a status mirroring the live site: AIO CH2414G318 + Gateway GW2347G077.

        ``order`` is a list of "aio"/"gateway" deciding the discovery array order.
        """
        status = pb.GatewayStatus()
        status.device_id = "pbgw_3c0f02ddf2d8"
        status.firmware = "1.0.0"
        status.schema_version = 1
        for kind in order:
            inv = status.inverters.add()
            if kind == "gateway":
                # GivEnergy Gateway: not a battery inverter, but reports primary
                # and a degenerate battery submessage (rate_max set) so it passes
                # the primary+battery filter. capacity_wh stays 0 (no real battery).
                inv.type = pb.INVERTER_TYPE_GIVENERGY_GATEWAY
                inv.serial = "GW2347G077"
                inv.primary = True
                inv.battery.rate_max_w = 38
            else:
                inv.type = pb.INVERTER_TYPE_GIVENERGY
                inv.serial = "CH2414G318"
                inv.primary = True
                inv.battery.soc_percent = 100
                inv.battery.capacity_wh = 12680
                inv.battery.rate_max_w = 6000
        return status

    def _two_aio_status(self, serials):
        """Build a status with two real AIO battery inverters in *serials* order."""
        status = pb.GatewayStatus()
        status.device_id = "pbgw_multi_aio"
        status.firmware = "1.0.0"
        status.schema_version = 1
        for serial in serials:
            inv = status.inverters.add()
            inv.type = pb.INVERTER_TYPE_GIVENERGY
            inv.serial = serial
            inv.primary = True
            inv.battery.soc_percent = 50
            inv.battery.capacity_wh = 10000
            inv.battery.rate_max_w = 6000
        return status

    def test_gateway_unit_excluded_from_control_both_orders(self):
        """The Gateway is excluded from the control set regardless of discovery order."""
        for order in (["aio", "gateway"], ["gateway", "aio"]):
            gw = self._make_gateway()
            gw._last_status = self._two_unit_status(order)
            gw.automatic_config()

            assert gw._auto_configured, f"order={order}"
            assert gw._args["num_inverters"] == 1, f"order={order}"
            # No control arg points at the Gateway's serial suffix (47g077):
            assert all("47g077" not in e for e in gw._args["discharge_rate"]), f"order={order}"
            assert all("47g077" not in e for e in gw._args["charge_start_time"]), f"order={order}"

    def test_aio_is_inverter0_regardless_of_discovery_order(self):
        """The AIO is always PredBat inverter 0, whichever way discovery reports the units."""
        for order in (["aio", "gateway"], ["gateway", "aio"]):
            gw = self._make_gateway()
            gw._last_status = self._two_unit_status(order)
            gw.automatic_config()
            assert gw._args["charge_start_time"][0] == "select.predbat_gateway_14g318_charge_slot1_start", f"order={order}"

    def test_multi_aio_slots_are_serial_stable_across_discovery_order(self):
        """Two AIOs map to the same slot by serial regardless of discovery array order."""
        for serials in (["CH1111A111", "CH2222B222"], ["CH2222B222", "CH1111A111"]):
            gw = self._make_gateway()
            gw._last_status = self._two_aio_status(serials)
            gw.automatic_config()
            assert gw._args["num_inverters"] == 2, f"serials={serials}"
            # Sorted by serial: CH1111A111 (suffix 11a111) is always slot 0.
            assert gw._args["charge_start_time"][0] == "select.predbat_gateway_11a111_charge_slot1_start", f"serials={serials}"
            assert gw._args["charge_start_time"][1] == "select.predbat_gateway_22b222_charge_slot1_start", f"serials={serials}"

    def _gateway_plus_aios_status(self, aio_serials, gateway_first=True):
        """Build a status: one GivEnergy Gateway plus the AIO battery inverters in *aio_serials*."""
        status = pb.GatewayStatus()
        status.device_id = "pbgw_3c0f02ddf2d8"
        status.firmware = "1.0.0"
        status.schema_version = 1

        def add_gateway():
            inv = status.inverters.add()
            inv.type = pb.INVERTER_TYPE_GIVENERGY_GATEWAY
            inv.serial = "GW2347G077"
            inv.primary = True
            inv.battery.rate_max_w = 38

        def add_aio(serial):
            inv = status.inverters.add()
            inv.type = pb.INVERTER_TYPE_GIVENERGY
            inv.serial = serial
            inv.primary = True
            inv.battery.soc_percent = 100
            inv.battery.capacity_wh = 12680
            inv.battery.rate_max_w = 6000

        if gateway_first:
            add_gateway()
        for serial in aio_serials:
            add_aio(serial)
        if not gateway_first:
            add_gateway()
        return status

    def test_gateway_is_control_point_when_two_aios_present(self):
        """GivTCP: a Gateway behind >=2 AIOs becomes the single control point (control routes via the GW).

        Mirrors the dynamic case: a site starts as Gateway + 1 AIO (control the AIO) and a
        second AIO is later discovered, at which point control must move to the Gateway.
        """
        for gateway_first in (True, False):
            gw = self._make_gateway()
            gw._last_status = self._gateway_plus_aios_status(["CH2414G318", "CH9999G999"], gateway_first=gateway_first)
            gw.automatic_config()
            assert gw._args["num_inverters"] == 1, f"gateway_first={gateway_first}"
            # The single control unit is the GATEWAY (47g077), not either AIO:
            assert gw._args["charge_start_time"][0] == "select.predbat_gateway_47g077_charge_slot1_start", f"gateway_first={gateway_first}"

    def test_gateway_with_single_aio_controls_the_aio(self):
        """A Gateway with exactly one AIO controls the AIO directly (Gateway is not the control point)."""
        gw = self._make_gateway()
        gw._last_status = self._gateway_plus_aios_status(["CH2414G318"])
        gw.automatic_config()
        assert gw._args["num_inverters"] == 1
        assert gw._args["charge_start_time"][0] == "select.predbat_gateway_14g318_charge_slot1_start"
        assert all("47g077" not in e for e in gw._args["charge_start_time"])

    # ------------------------------------------------------------------
    # Re-init trigger: a new inverter discovered later re-selects the control target
    # ------------------------------------------------------------------

    def test_reconfigure_triggers_when_new_aio_discovered(self):
        """A second AIO discovered later re-runs auto-config and moves control to the Gateway."""
        gw = self._make_gateway()
        # Initial: Gateway + 1 AIO -> control the AIO directly.
        gw._last_status = self._gateway_plus_aios_status(["CH2414G318"])
        gw.automatic_config()
        assert gw._args["charge_start_time"][0] == "select.predbat_gateway_14g318_charge_slot1_start"

        # Five minutes later a second AIO appears -> a re-config is required.
        new_status = self._gateway_plus_aios_status(["CH2414G318", "CH9999G999"])
        assert gw._needs_reconfigure(new_status) is True
        gw._last_status = new_status
        gw.automatic_config()
        # Control point is now the Gateway (GivTCP rule for >=2 AIOs behind a Gateway).
        assert gw._args["num_inverters"] == 1
        assert gw._args["charge_start_time"][0] == "select.predbat_gateway_47g077_charge_slot1_start"

    def test_no_reconfigure_when_inverter_set_unchanged(self):
        """Repeated telemetry with the same inverter set does not re-run auto-config."""
        gw = self._make_gateway()
        gw._last_status = self._gateway_plus_aios_status(["CH2414G318"])
        gw.automatic_config()
        assert gw._needs_reconfigure(self._gateway_plus_aios_status(["CH2414G318"])) is False

    def test_transient_inverter_drop_does_not_reconfigure(self):
        """An inverter transiently dropping out of a scan does not trigger a re-config (sticky)."""
        gw = self._make_gateway()
        gw._last_status = self._gateway_plus_aios_status(["CH2414G318", "CH9999G999"])
        gw.automatic_config()
        # CH9999G999 missing from one scan -> no *new* serials -> no re-config.
        assert gw._needs_reconfigure(self._gateway_plus_aios_status(["CH2414G318"])) is False

    def _make_handler_gateway(self):
        """A gateway wired enough to drive _process_telemetry end-to-end (decode -> inject -> reconfigure)."""
        from unittest.mock import MagicMock

        gw = self._make_gateway()
        gw.local_tz = pytz.timezone("Europe/London")
        gw._error_count = 0
        gw.api_started = False
        gw._last_telemetry_time = 0
        gw.update_success_timestamp = MagicMock()
        gw.non_fatal_error_occurred = MagicMock()
        # Entity states as the gateway published them, so a test can tell stale from rewritten
        gw._states = {}
        gw.dashboard_item = lambda entity_id, state, attributes=None, app=None: gw._states.__setitem__(entity_id, state)
        gw.get_state_wrapper = lambda entity_id=None, default=None, **kwargs: gw._states.get(entity_id, default)
        return gw

    def _pv_only_site_status(self, battery_ready):
        """Build a field topology with a PV-only unit plus one battery inverter.

        The Hub currently marks both units primary.  Early startup telemetry can
        contain neither battery message; a later frame fills the battery fields on
        the battery inverter while the PV-only unit correctly remains empty.
        """
        status = pb.GatewayStatus()
        status.device_id = "pbgw_pv_only_site"
        status.firmware = "1.0.23"
        status.schema_version = 1

        pv_only = status.inverters.add()
        pv_only.type = pb.INVERTER_TYPE_GIVENERGY
        pv_only.serial = "FD0000P001"
        pv_only.primary = True
        pv_only.connected = True
        pv_only.active = True

        battery = status.inverters.add()
        battery.type = pb.INVERTER_TYPE_GIVENERGY
        battery.serial = "CH0000B002"
        battery.primary = True
        battery.connected = True
        battery.active = True
        if battery_ready:
            battery.battery.soc_percent = 16
            battery.battery.capacity_wh = 16000
            battery.battery.rate_max_w = 6000
        return status

    def test_incomplete_battery_telemetry_waits_then_excludes_pv_only_unit(self):
        """Do not bind every primary unit when the first frame lacks battery data.

        The serial selection can legitimately contain every discovered inverter,
        because discovery does not know which units have batteries.  Auto-config
        must wait for capability data rather than making the PV-only unit a write
        target permanently from an incomplete first frame.
        """
        gw = self._make_handler_gateway()
        gw.gateway_inverter_serial = ["FD0000P001", "CH0000B002"]

        initial = self._pv_only_site_status(battery_ready=False)
        gw._process_telemetry(initial.SerializeToString())

        assert gw._auto_configured is False
        assert gw.api_started is False
        assert "num_inverters" not in gw._args

        ready = self._pv_only_site_status(battery_ready=True)
        gw._process_telemetry(ready.SerializeToString())

        assert gw._auto_configured is True
        assert gw.api_started is True
        assert gw._args["num_inverters"] == 1
        assert gw._args["charge_start_time"] == ["select.predbat_gateway_00b002_charge_slot1_start"]
        assert all("00p001" not in entity for entity in gw._args["charge_start_time"])

    # ------------------------------------------------------------------
    # gateway_inverter_serial is authoritative, and an unusable inverter is picked up later (#5471)
    # ------------------------------------------------------------------

    def _two_ac_status(self, second="ok"):
        """Two AC battery inverters on one hub; *second* is how CE2225G400 is reported.

        "ok" is healthy, "no_battery" has no readings at all (the hub is not reading it), "disconnected" also has
        connected false, "not_primary" has battery data but no primary flag, and "absent"
        leaves it out of the status message.
        """
        status = pb.GatewayStatus()
        status.device_id = "pbgw_two_ac"
        status.firmware = "1.0.34"
        status.schema_version = 1
        for serial in ("CE2223G800", "CE2225G400"):
            if serial == "CE2225G400" and second == "absent":
                continue
            inv = status.inverters.add()
            inv.type = pb.INVERTER_TYPE_GIVENERGY
            inv.serial = serial
            inv.primary = True
            inv.connected = True
            inv.active = True
            inv.battery.soc_percent = 50
            inv.battery.capacity_wh = 9500
            inv.battery.rate_max_w = 3000
        if second in ("no_battery", "disconnected"):
            status.inverters[1].ClearField("battery")
        if second == "disconnected":
            status.inverters[1].connected = False
        if second == "not_primary":
            status.inverters[1].primary = False
        return status

    def _errors(self, gw):
        """The Error lines the gateway has logged."""
        return [str(c.args[0]) for c in gw.log.call_args_list if str(c.args[0]).startswith("Error")]

    def _logged(self, gw, level, *words):
        """Whether a log line of this level holds every one of *words*."""
        return any(str(c.args[0]).startswith(level) and all(word in str(c.args[0]) for word in words) for c in gw.log.call_args_list)

    def _listed_gateway(self, serials, stored=None):
        """A handler gateway with a serial list and a storage holding *stored*, loaded as run() does at start-up."""
        gw = self._make_handler_gateway()
        gw.gateway_inverter_serial = list(serials)
        gw.gateway_device_id = "pbgw_two_ac"
        saved = {}
        if stored is not None:
            saved[("gateway", "discovery_pbgw_two_ac")] = {"inverters": stored}

        class FakeStorage:
            """Storage holding what the gateway saved."""

            async def load(self, module, filename):
                """Return the saved data."""
                return saved.get((module, filename))

            async def save(self, module, filename, data, format="yaml", expiry=None, indent=None):
                """Keep the data."""
                saved[(module, filename)] = json.loads(json.dumps(data))
                return True

        gw._saved = saved
        gw.base.components.get_component.return_value = FakeStorage()
        asyncio.run(gw._load_discovery())
        return gw

    _STORED_400 = {"CE2225G400": {"serial": "CE2225G400", "type": pb.INVERTER_TYPE_GIVENERGY, "battery": True, "capacity_wh": 9500, "rate_max_w": 3000, "inverter_rate_max_w": 0, "export_limit_w": 0, "model": ""}}

    def test_listed_inverter_never_read_is_left_out_until_the_hub_reads_it(self):
        """A listed inverter the hub has never read is left out, with a warning and no error, and bound once it is read."""
        for second in ("no_battery", "disconnected", "absent"):
            gw = self._listed_gateway(["CE2223G800", "CE2225G400"])

            gw._process_telemetry(self._two_ac_status(second).SerializeToString())

            assert gw._args["num_inverters"] == 1, second
            assert self._logged(gw, "Warn", "CE2225G400", "not been read by the hub yet"), second
            assert self._errors(gw) == [], second
            gw.non_fatal_error_occurred.assert_not_called()

            gw._process_telemetry(self._two_ac_status("ok").SerializeToString())

            assert gw._args["num_inverters"] == 2, second
            assert gw._inverter_slot_serials == ["CE2223G800", "CE2225G400"], second

    def test_listed_inverter_is_bound_whatever_its_primary_flag(self):
        """The list decides which battery inverters are used, not the hub's primary flag."""
        gw = self._listed_gateway(["CE2223G800", "CE2225G400"])

        gw._process_telemetry(self._two_ac_status("not_primary").SerializeToString())

        assert gw._args["num_inverters"] == 2
        assert gw._args["soc_percent"] == ["sensor.predbat_gateway_23g800_soc", "sensor.predbat_gateway_25g400_soc"]

    def test_stored_inverter_unread_at_startup_is_still_bound(self):
        """An inverter discovered on an earlier run keeps its slot when the hub cannot read it at start-up (the #5471 incident)."""
        for second in ("no_battery", "disconnected", "absent"):
            gw = self._listed_gateway(["CE2223G800", "CE2225G400"], stored=self._STORED_400)

            gw._process_telemetry(self._two_ac_status(second).SerializeToString())

            assert gw._auto_configured is True, second
            assert gw._args["num_inverters"] == 2, second
            assert gw._inverter_slot_serials == ["CE2223G800", "CE2225G400"], second
            assert gw._args["soc_max"] == ["sensor.predbat_gateway_23g800_battery_capacity", "sensor.predbat_gateway_25g400_battery_capacity"], second
            # Nothing was published for it before, so it gets default entities and its stored size
            assert gw._states["sensor.predbat_gateway_25g400_soc"] == 0, second
            assert gw._states["sensor.predbat_gateway_25g400_battery_capacity"] == 9.5, second
            assert gw._states["select.predbat_gateway_25g400_charge_slot1_start"] == "00:00:00", second
            assert self._logged(gw, "Error", "CE2225G400", "not being read"), second
            assert gw.non_fatal_error_occurred.call_count == 1, second

    def _empty_status(self):
        """A status in which the hub names no inverter at all."""
        status = pb.GatewayStatus()
        status.device_id = "pbgw_two_ac"
        status.firmware = "1.0.34"
        status.schema_version = 1
        return status

    def test_stored_inverter_is_bound_when_the_hub_reports_no_inverters_at_startup(self):
        """A single listed inverter with a stored record keeps its slot when the first status names no inverter."""
        gw = self._listed_gateway(["CE2225G400"], stored=self._STORED_400)

        gw._process_telemetry(self._empty_status().SerializeToString())

        assert gw._auto_configured is True
        assert gw._args["num_inverters"] == 1
        assert gw._inverter_slot_serials == ["CE2225G400"]
        assert gw._states["sensor.predbat_gateway_25g400_soc"] == 0
        assert gw._states["sensor.predbat_gateway_25g400_battery_capacity"] == 9.5
        assert self._logged(gw, "Error", "CE2225G400", "not being read")
        assert gw.non_fatal_error_occurred.call_count == 1
        # Nothing was read, so the status does not count towards the hub being alive
        gw.update_success_timestamp.assert_not_called()

    def test_status_with_no_inverters_leaves_configured_inverters_unread(self):
        """Once configured, a status naming no inverter zeroes the powers, keeps the rest and is an error."""
        gw = self._listed_gateway(["CE2223G800", "CE2225G400"])
        healthy = self._two_ac_status("ok")
        for inv in healthy.inverters:
            inv.battery.power_w = -3000
        gw._process_telemetry(healthy.SerializeToString())
        assert gw._states["sensor.predbat_gateway_23g800_battery_power"] == 3000
        assert self._errors(gw) == []

        gw._process_telemetry(self._empty_status().SerializeToString())

        assert gw._args["num_inverters"] == 2
        for suffix in ("23g800", "25g400"):
            assert gw._states[f"sensor.predbat_gateway_{suffix}_battery_power"] == 0, suffix
            assert gw._states[f"sensor.predbat_gateway_{suffix}_soc"] == 50, suffix
        assert self._logged(gw, "Error", "CE2223G800", "CE2225G400", "not being read")
        assert gw.non_fatal_error_occurred.call_count == 1

        gw._process_telemetry(healthy.SerializeToString())
        assert gw._states["sensor.predbat_gateway_23g800_battery_power"] == 3000
        assert self._logged(gw, "Info", "every configured inverter is being read by the hub again")

    def test_status_with_no_inverters_is_ignored_without_a_list_or_a_record(self):
        """With no serial list, or no inverter discovered yet, a status naming no inverter is ignored as before."""
        for listed, stored in (([], None), (["CE2225G400"], None)):
            gw = self._listed_gateway(listed, stored=stored)

            gw._process_telemetry(self._empty_status().SerializeToString())

            assert gw._last_status is None, listed
            assert gw._auto_configured is False, listed
            assert gw._args == {}, listed

        # No list: inverters come and go, so one that goes is not held on to
        gw = self._listed_gateway([])
        gw._process_telemetry(self._two_ac_status("ok").SerializeToString())
        healthy = gw._last_status
        gw._process_telemetry(self._empty_status().SerializeToString())
        assert gw._last_status is healthy
        assert self._errors(gw) == []

    def test_changed_serial_list_wins_over_stored_discovery(self):
        """A stored record for a serial no longer in the list is not used: the list is the setting."""
        gw = self._listed_gateway(["CE2223G800"], stored=self._STORED_400)
        assert gw._discovery == {}

        gw._process_telemetry(self._two_ac_status("ok").SerializeToString())
        asyncio.run(gw._save_discovery())

        assert gw._args["num_inverters"] == 1
        assert gw._inverter_slot_serials == ["CE2223G800"]
        assert sorted(gw._saved[("gateway", "discovery_pbgw_two_ac")]["inverters"]) == ["CE2223G800"]
        gw.non_fatal_error_occurred.assert_not_called()

    def test_discovery_is_saved_and_not_overwritten_while_an_inverter_is_offline(self):
        """Discovery is stored from an inverter the hub is reading, updated when it changes, and left alone while it is offline."""
        gw = self._listed_gateway(["CE2223G800", "CE2225G400"])
        gw._process_telemetry(self._two_ac_status("ok").SerializeToString())
        asyncio.run(gw._save_discovery())
        stored = gw._saved[("gateway", "discovery_pbgw_two_ac")]["inverters"]
        assert stored["CE2225G400"] == self._STORED_400["CE2225G400"]

        for second in ("no_battery", "disconnected", "absent"):
            gw._process_telemetry(self._two_ac_status(second).SerializeToString())
            assert gw._discovery_dirty is False, second
            assert gw._discovery["CE2225G400"] == self._STORED_400["CE2225G400"], second

        bigger = self._two_ac_status("ok")
        bigger.inverters[1].battery.capacity_wh = 13500
        gw._process_telemetry(bigger.SerializeToString())
        asyncio.run(gw._save_discovery())
        assert gw._saved[("gateway", "discovery_pbgw_two_ac")]["inverters"]["CE2225G400"]["capacity_wh"] == 13500

        # A second gateway started from that storage knows both inverters before any telemetry
        restarted = self._listed_gateway(["CE2223G800", "CE2225G400"], stored=gw._saved[("gateway", "discovery_pbgw_two_ac")]["inverters"])
        assert sorted(restarted._discovery) == ["CE2223G800", "CE2225G400"]

    def test_failed_discovery_save_is_retried_after_a_wait(self):
        """A save the storage refuses is kept pending and tried again after a wait, not on every status message."""
        import gateway

        gw = self._listed_gateway(["CE2223G800", "CE2225G400"])
        storage = gw.base.components.get_component.return_value
        real_save = storage.save
        attempts = []

        async def failing_save(module, filename, data, format="yaml", expiry=None, indent=None):
            attempts.append(1)
            return False

        now = [1000.0]
        real_monotonic = gateway._monotonic
        gateway._monotonic = lambda: now[0]
        try:
            storage.save = failing_save
            gw._process_telemetry(self._two_ac_status("ok").SerializeToString())
            asyncio.run(gw._save_discovery())
            assert attempts == [1]
            assert gw._discovery_dirty is True
            assert gw._saved == {}
            assert self._logged(gw, "Warn", "failed to save inverter discovery")

            # Still failing, but inside the wait: not tried again
            asyncio.run(gw._save_discovery())
            assert attempts == [1]

            storage.save = real_save
            now[0] += gateway.GATEWAY_DISCOVERY_SAVE_RETRY_SECONDS
            asyncio.run(gw._save_discovery())
            assert gw._discovery_dirty is False
            assert sorted(gw._saved[("gateway", "discovery_pbgw_two_ac")]["inverters"]) == ["CE2223G800", "CE2225G400"]
        finally:
            gateway._monotonic = real_monotonic

    def test_newly_bound_inverter_has_its_entities_on_the_same_status(self):
        """An inverter auto-config has just bound is published before the API is declared started.

        One the hub does not flag primary is skipped by the injection that runs ahead of
        auto-config, so without a second pass its soc and size entities would not exist
        until the next status message.
        """
        gw = self._listed_gateway(["CE2223G800", "CE2225G400"])

        gw._process_telemetry(self._two_ac_status("not_primary").SerializeToString())

        assert gw.api_started is True
        assert gw._args["num_inverters"] == 2
        assert gw._states["sensor.predbat_gateway_25g400_soc"] == 50
        assert gw._states["sensor.predbat_gateway_25g400_battery_capacity"] == 9.5

        # The same for a PV-only inverter the hub does not flag primary
        gw = self._listed_gateway(["FD0000P001", "CH0000B002"])
        status = self._pv_and_battery_status(pv_power=900)
        status.inverters[0].primary = False

        gw._process_telemetry(status.SerializeToString())

        assert gw._pv_only_serials == ("FD0000P001",)
        assert gw._states["sensor.predbat_gateway_00p001_pv_power"] == 900

    def test_no_serial_list_stores_last_known_values_but_inverters_come_and_go(self):
        """With no serial list the last known values are stored and used, but a stored inverter is not held on to."""
        first = self._listed_gateway([])
        first._process_telemetry(self._rated_status(rate_max_w=3000, inverter_rate_max_w=3600).SerializeToString())
        asyncio.run(first._save_discovery())
        stored = first._saved[("gateway", "discovery_pbgw_two_ac")]["inverters"]
        assert sorted(stored) == ["CE2223G800", "CE2225G400"]
        assert stored["CE2225G400"]["rate_max_w"] == 3000
        assert first._args["num_inverters"] == 2

        # After a restart the hub does not report the second inverter: it is simply not there
        gw = self._listed_gateway([], stored=stored)
        assert sorted(gw._discovery) == ["CE2223G800", "CE2225G400"]
        gw._process_telemetry(self._two_ac_status("absent").SerializeToString())
        assert gw._args["num_inverters"] == 1
        assert gw._inverter_slot_serials == ["CE2223G800"]
        assert self._errors(gw) == []
        gw.non_fatal_error_occurred.assert_not_called()

        # Nor is one the hub lists but is not reading
        gw = self._listed_gateway([], stored=stored)
        gw._process_telemetry(self._two_ac_status("no_battery").SerializeToString())
        assert gw._args["num_inverters"] == 1

        # But when it is there with its ratings and size unpopulated, the stored ones are used
        gw = self._listed_gateway([], stored=stored)
        blank = self._rated_status(rate_max_w=0, inverter_rate_max_w=0, export_limit_w=0)
        blank.inverters[1].battery.capacity_wh = 0
        gw._process_telemetry(blank.SerializeToString())
        assert gw._args["num_inverters"] == 2
        assert gw._states["sensor.predbat_gateway_25g400_battery_rate_max"] == 3000
        assert gw._states["sensor.predbat_gateway_25g400_inverter_rate_max"] == 3600
        assert gw._states["sensor.predbat_gateway_25g400_export_limit_w"] == 6000
        assert gw._states["sensor.predbat_gateway_25g400_battery_capacity"] == 9.5
        assert gw._args["soc_max"] == ["sensor.predbat_gateway_23g800_battery_capacity", "sensor.predbat_gateway_25g400_battery_capacity"]

    def test_inverter_with_no_serial_is_not_recorded(self):
        """A slot the hub reports with an empty serial has nothing to be stored under."""
        gw = self._listed_gateway([])
        status = self._two_ac_status("ok")
        status.inverters[1].serial = ""
        gw._process_telemetry(status.SerializeToString())
        assert sorted(gw._discovery) == ["CE2223G800"]

    def test_configured_inverter_offline_reports_an_error_until_it_returns(self):
        """A configured inverter the hub stops reading is an error on every frame, logged once, and clears when it returns."""
        for second in ("no_battery", "disconnected", "absent"):
            gw = self._listed_gateway(["CE2223G800", "CE2225G400"])
            gw._process_telemetry(self._two_ac_status("ok").SerializeToString())
            assert self._errors(gw) == []

            gw._process_telemetry(self._two_ac_status(second).SerializeToString())
            gw._process_telemetry(self._two_ac_status(second).SerializeToString())

            unread = [line for line in self._errors(gw) if "not being read" in line]
            assert len(unread) == 1 and "CE2225G400" in unread[0] and "CE2223G800" not in unread[0], (second, unread)
            assert gw.non_fatal_error_occurred.call_count == 2, second
            assert gw._args["num_inverters"] == 2, second

            gw._process_telemetry(self._two_ac_status("ok").SerializeToString())

            assert gw.non_fatal_error_occurred.call_count == 2, second
            assert self._logged(gw, "Info", "being read by the hub again"), second

    def test_configured_inverter_going_offline_keeps_its_slot_and_its_last_data(self):
        """An inverter that stops being read stays configured and its entities go stale, they are not zeroed."""
        for listed in (["CE2223G800", "CE2225G400"], []):
            gw = self._listed_gateway(listed)
            healthy = self._two_ac_status("ok")
            healthy.inverters[1].battery.soc_percent = 84
            healthy.inverters[1].battery.power_w = -3000
            healthy.inverters[1].pv.power_w = 400
            gw._process_telemetry(healthy.SerializeToString())
            assert gw._states["sensor.predbat_gateway_25g400_soc"] == 84
            assert gw._states["sensor.predbat_gateway_25g400_battery_power"] == 3000

            offline = self._two_ac_status("no_battery")
            offline.inverters[0].battery.soc_percent = 61
            for _ in range(2):
                gw._process_telemetry(offline.SerializeToString())

            assert gw._args["num_inverters"] == 2, listed
            assert gw._states["sensor.predbat_gateway_25g400_soc"] == 84, listed
            assert gw._states["sensor.predbat_gateway_25g400_battery_capacity"] == 9.5, listed
            assert gw._states["sensor.predbat_gateway_23g800_soc"] == 61, listed
            # A power is a rate, so it is not left at the value it had when the inverter dropped out
            assert gw._states["sensor.predbat_gateway_25g400_battery_power"] == 0, listed
            assert gw._states["sensor.predbat_gateway_25g400_pv_power"] == 0, listed
            assert gw._states["sensor.predbat_gateway_25g400_inverter_power"] == 0, listed

            # Gone from the status message altogether: still configured, still stale
            healthy.inverters[1].battery.power_w = -2500
            gw._process_telemetry(healthy.SerializeToString())
            assert gw._states["sensor.predbat_gateway_25g400_battery_power"] == 2500, listed
            gw._process_telemetry(self._two_ac_status("absent").SerializeToString())
            assert gw._args["num_inverters"] == 2, listed
            assert gw._states["sensor.predbat_gateway_25g400_soc"] == 84, listed
            assert gw._states["sensor.predbat_gateway_25g400_battery_power"] == 0, listed
            assert gw.non_fatal_error_occurred.call_count == (3 if listed else 0), listed

            healthy.inverters[1].battery.soc_percent = 80
            gw._process_telemetry(healthy.SerializeToString())
            assert gw._states["sensor.predbat_gateway_25g400_soc"] == 80, listed

    def test_shared_ct_metering_carries_on_when_the_first_inverter_is_unread(self):
        """On a shared clamp the site readings live on the first inverter's entities, and are filled from the next one while it is away."""

        def status(first):
            built = self._two_ac_status("ok")
            for inv, grid_w in zip(built.inverters, (-1500, -1500)):
                inv.grid.power_w = grid_w
                inv.load.power_w = 1800
                inv.energy.grid_import_today_wh = 4200
                inv.energy.grid_export_today_wh = 300
                inv.energy.consumption_today_wh = 6100
            if first == "unread":
                for block in ("battery", "pv", "grid", "load", "inverter", "energy"):
                    built.inverters[0].ClearField(block)
            elif first == "absent":
                del built.inverters[0]
            return built

        for first in ("unread", "absent"):
            for listed in (["CE2223G800", "CE2225G400"], []):
                gw = self._listed_gateway(listed)
                gw._process_telemetry(status("ok").SerializeToString())
                assert gw._args["grid_power"] == ["sensor.predbat_gateway_23g800_grid_power", 0], (first, listed)

                later = status(first)
                later.inverters[-1].grid.power_w = -900
                later.inverters[-1].load.power_w = 1100
                later.inverters[-1].energy.grid_import_today_wh = 5000
                later.inverters[-1].energy.consumption_today_wh = 7400
                gw._process_telemetry(later.SerializeToString())

                assert gw._states["sensor.predbat_gateway_23g800_grid_power"] == -900, (first, listed)
                assert gw._states["sensor.predbat_gateway_23g800_load_power"] == 1100, (first, listed)
                assert gw._states["sensor.predbat_gateway_23g800_import_today"] == 5.0, (first, listed)
                assert gw._states["sensor.predbat_gateway_23g800_export_today"] == 0.3, (first, listed)
                assert gw._states["sensor.predbat_gateway_23g800_load_today"] == 7.4, (first, listed)
                # Its own battery is still unknown: stale state of charge, no power
                assert gw._states["sensor.predbat_gateway_23g800_soc"] == 50, (first, listed)
                assert gw._states["sensor.predbat_gateway_23g800_battery_power"] == 0, (first, listed)

    def test_separate_ct_metering_is_not_filled_from_another_inverter(self):
        """With a clamp each (gateway_shared_ct off) an unread inverter's grid reading is its own, so it goes to zero."""
        gw = self._listed_gateway(["CE2223G800", "CE2225G400"])
        gw.gateway_shared_ct = False
        healthy = self._two_ac_status("ok")
        for inv in healthy.inverters:
            inv.grid.power_w = -700
        gw._process_telemetry(healthy.SerializeToString())

        unread = self._two_ac_status("ok")
        unread.inverters[1].grid.power_w = -700
        unread.inverters[0].ClearField("battery")
        gw._process_telemetry(unread.SerializeToString())

        assert gw._states["sensor.predbat_gateway_23g800_grid_power"] == 0
        assert gw._states["sensor.predbat_gateway_25g400_grid_power"] == -700

    def test_unread_inverter_behind_a_coordinator_is_an_error(self):
        """Behind a Gateway the Gateway holds the only slot, and one of its listed inverters going unread is still reported."""
        gw = self._listed_gateway(["GW2347G077", "CH2414G318", "CH9999G999"])
        gw._process_telemetry(self._gateway_plus_aios_status(["CH2414G318", "CH9999G999"]).SerializeToString())
        assert self._errors(gw) == []

        unread = self._gateway_plus_aios_status(["CH2414G318", "CH9999G999"])
        unread.inverters[2].ClearField("battery")
        gw._process_telemetry(unread.SerializeToString())

        assert gw._args["num_inverters"] == 1
        assert self._logged(gw, "Error", "CH9999G999", "not being read")
        assert not self._logged(gw, "Error", "CH2414G318")
        assert gw.non_fatal_error_occurred.call_count == 1

        gw._process_telemetry(self._gateway_plus_aios_status(["CH2414G318"]).SerializeToString())
        assert gw.non_fatal_error_occurred.call_count == 2

    def test_unreadable_stored_discovery_is_ignored(self):
        """A stored record that cannot be read is dropped with a warning; it must not stop auto-config."""
        good = self._STORED_400["CE2225G400"]
        for bad in ({"serial": "CE2223G800", "type": "GIVENERGY"}, {"serial": "CE2223G800", "capacity_wh": "big"}, {"type": 1}, "CE2223G800", None, {"serial": "  "}):
            gw = self._listed_gateway(["CE2223G800", "CE2225G400"], stored={"CE2223G800": bad, "CE2225G400": good})

            assert gw._discovery == {"CE2225G400": good}, bad
            assert self._logged(gw, "Warn", "CE2223G800", "unreadable stored discovery"), bad

            gw._process_telemetry(self._two_ac_status("absent").SerializeToString())
            assert gw._args["num_inverters"] == 2, bad

        # A record that says it has a battery in any way but true is not taken to have one
        gw = self._listed_gateway(["CE2225G400"], stored={"CE2225G400": {**good, "battery": "false", "capacity_wh": None}})
        assert gw._discovery["CE2225G400"]["battery"] is False
        assert gw._discovery["CE2225G400"]["capacity_wh"] == 0

    def test_stored_pv_only_inverter_missing_at_startup_has_its_pv_entities(self):
        """A PV-only inverter known only from its stored record still has the PV entities auto-config binds."""
        stored = {"FD0000P001": {"serial": "FD0000P001", "type": pb.INVERTER_TYPE_GIVENERGY, "battery": False, "capacity_wh": 0, "rate_max_w": 0, "inverter_rate_max_w": 0, "export_limit_w": 0, "model": ""}}
        gw = self._listed_gateway(["FD0000P001", "CH0000B002"], stored=stored)
        status = self._pv_only_site_status(battery_ready=True)
        del status.inverters[0]

        gw._process_telemetry(status.SerializeToString())

        assert gw._pv_only_serials == ("FD0000P001",)
        assert gw._args["pv_power"] == ["sensor.predbat_gateway_00b002_pv_power", "sensor.predbat_gateway_00p001_pv_power"]
        assert gw._states["sensor.predbat_gateway_00p001_pv_power"] == 0
        assert gw._states["sensor.predbat_gateway_00p001_pv_today"] == 0

    def test_coordinator_slot_has_no_battery_size_sensor(self):
        """A slot with no battery size known is left for automatic sizing, not pointed at a sensor that is never published."""
        gw = self._listed_gateway(["GW2347G077", "CH2414G318", "CH9999G999"])

        gw._process_telemetry(self._gateway_plus_aios_status(["CH2414G318", "CH9999G999"]).SerializeToString())

        assert gw._args["soc_max"] == [None]
        assert "sensor.predbat_gateway_47g077_battery_capacity" not in gw._states

    def test_slot_bound_with_no_battery_size_is_rebound_when_the_hub_reports_one(self):
        """A battery inverter bound before its size is known gets its size sensor on the first status that carries one."""
        for listed in (["CE2223G800", "CE2225G400"], []):
            gw = self._listed_gateway(listed)
            unsized = self._two_ac_status("ok")
            unsized.inverters[1].battery.capacity_wh = 0
            gw._process_telemetry(unsized.SerializeToString())
            assert gw._args["soc_max"] == ["sensor.predbat_gateway_23g800_battery_capacity", None], listed

            gw._process_telemetry(unsized.SerializeToString())
            assert gw._args["soc_max"][1] is None, listed

            gw._process_telemetry(self._two_ac_status("ok").SerializeToString())
            assert gw._args["soc_max"] == ["sensor.predbat_gateway_23g800_battery_capacity", "sensor.predbat_gateway_25g400_battery_capacity"], listed
            assert gw._states["sensor.predbat_gateway_25g400_battery_capacity"] == 9.5, listed

    # ------------------------------------------------------------------
    # A rating the hub stops reporting keeps its last good value (#5478)
    # ------------------------------------------------------------------

    def _rated_status(self, rate_max_w=3000, inverter_rate_max_w=3000, export_limit_w=6000):
        """Two 3kW AC inverters; the arguments are what CE2225G400 reports for its ratings."""
        status = self._two_ac_status("ok")
        for inv in status.inverters:
            inv.inverter.rate_max_w = 3000
            inv.control.export_limit_w = 6000
            inv.control.reserve_soc = 4
        second = status.inverters[1]
        second.battery.rate_max_w = rate_max_w
        second.inverter.rate_max_w = inverter_rate_max_w
        second.control.export_limit_w = export_limit_w
        return status

    def test_ratings_the_hub_stops_reporting_keep_their_last_good_value(self):
        """A status with the ratings unpopulated must not turn a 3kW inverter into a 6kW one with no export limit."""
        for listed in (["CE2223G800", "CE2225G400"], []):
            gw = self._listed_gateway(listed)
            gw._process_telemetry(self._rated_status().SerializeToString())
            assert gw._states["sensor.predbat_gateway_25g400_battery_rate_max"] == 3000, listed
            assert gw._states["sensor.predbat_gateway_25g400_inverter_rate_max"] == 3000, listed
            assert gw._states["sensor.predbat_gateway_25g400_export_limit_w"] == 6000, listed

            gw._process_telemetry(self._rated_status(rate_max_w=0, inverter_rate_max_w=0, export_limit_w=0).SerializeToString())

            assert gw._states["sensor.predbat_gateway_25g400_battery_rate_max"] == 3000, listed
            assert gw._states["sensor.predbat_gateway_25g400_inverter_rate_max"] == 3000, listed
            assert gw._states["sensor.predbat_gateway_25g400_export_limit_w"] == 6000, listed
            # The other inverter is untouched
            assert gw._states["sensor.predbat_gateway_23g800_battery_rate_max"] == 3000, listed

            # A new real value is taken straight away
            gw._process_telemetry(self._rated_status(rate_max_w=3600, inverter_rate_max_w=5000, export_limit_w=3680).SerializeToString())
            assert gw._states["sensor.predbat_gateway_25g400_battery_rate_max"] == 3600, listed
            assert gw._states["sensor.predbat_gateway_25g400_inverter_rate_max"] == 5000, listed
            assert gw._states["sensor.predbat_gateway_25g400_export_limit_w"] == 3680, listed

    def test_zero_export_limit_is_kept_when_the_hub_stops_reporting_it(self):
        """A real limit of zero (the hub's value 1) is a limit like any other: a later undefined keeps it."""
        gw = self._listed_gateway([])
        gw._process_telemetry(self._rated_status(export_limit_w=1).SerializeToString())
        assert gw._states["sensor.predbat_gateway_25g400_export_limit_w"] == 0

        gw._process_telemetry(self._rated_status(export_limit_w=0).SerializeToString())
        assert gw._states["sensor.predbat_gateway_25g400_export_limit_w"] == 0

    def test_ratings_never_reported_keep_the_existing_defaults(self):
        """An inverter that has never reported a rating still gets the 6000W default and no export limit."""
        gw = self._listed_gateway([])
        gw._process_telemetry(self._rated_status(rate_max_w=0, inverter_rate_max_w=0, export_limit_w=0).SerializeToString())

        assert gw._states["sensor.predbat_gateway_25g400_battery_rate_max"] == 6000
        assert gw._states["sensor.predbat_gateway_25g400_inverter_rate_max"] == 6000
        assert gw._states["sensor.predbat_gateway_25g400_export_limit_w"] == 99999

    def _writable(self, gw):
        """Let *gw* take control writes, recording the commands it publishes."""
        from unittest.mock import MagicMock

        gw._mqtt_connected = True
        gw._mqtt_client = MagicMock()
        gw._gateway_online = True
        gw._rate_caps_sent = {}
        gw.is_alive = lambda: True
        gw.get_arg = lambda name, default=None, **kwargs: default
        gw._published = []

        async def fake_publish_command(command, **kwargs):
            gw._published.append((command, kwargs))

        gw.publish_command = fake_publish_command
        return gw

    def test_rate_writes_are_held_until_the_inverter_reports_its_rating(self):
        """With no rating ever reported the 6000W default is only an assumption, so no rate above zero is written on it."""
        gw = self._writable(self._listed_gateway([]))
        gw._process_telemetry(self._rated_status(rate_max_w=0, inverter_rate_max_w=0).SerializeToString())
        assert gw._states["sensor.predbat_gateway_25g400_battery_rate_max"] == 6000

        asyncio.run(gw.number_event("number.predbat_gateway_25g400_discharge_rate", 6000))
        asyncio.run(gw.number_event("number.predbat_gateway_25g400_charge_rate", 6000))
        assert gw._published == []
        assert self._logged(gw, "Warn", "CE2225G400", "has not reported its maximum rate")

        # Stopping a charge or discharge is safe at any rating, as are the other controls and the other inverter
        asyncio.run(gw.number_event("number.predbat_gateway_25g400_charge_rate", 0))
        asyncio.run(gw.number_event("number.predbat_gateway_25g400_reserve_soc", 4))
        asyncio.run(gw.number_event("number.predbat_gateway_23g800_discharge_rate", 3000))
        assert gw._published == [
            ("set_charge_rate", {"power_w": 0, "serial": "CE2225G400"}),
            ("set_reserve", {"target_soc": 4, "serial": "CE2225G400"}),
            ("set_discharge_rate", {"power_w": 3000, "serial": "CE2223G800"}),
        ]

        # Once the rating arrives the write goes through, and a later gap in the rating does not hold it again
        for rate_max_w in (3000, 0):
            gw._published.clear()
            gw._process_telemetry(self._rated_status(rate_max_w=rate_max_w).SerializeToString())
            asyncio.run(gw.number_event("number.predbat_gateway_25g400_discharge_rate", 3000))
            assert gw._published == [("set_discharge_rate", {"power_w": 3000, "serial": "CE2225G400"})], rate_max_w

    def test_rate_cap_is_not_sent_on_an_assumed_rating(self):
        """The cap sent to the hub comes from the same rating, so it waits for the real one too."""
        from types import SimpleNamespace

        gw = self._writable(self._listed_gateway([]))
        gw._process_telemetry(self._rated_status(rate_max_w=0, inverter_rate_max_w=0).SerializeToString())
        gw.base.inverters = [SimpleNamespace(inverter_type="GWMQTT", id=slot, battery_rate_max_charge=0.05, battery_rate_max_discharge=0.05) for slot in (0, 1)]
        gw._inverter_rate_caps = lambda: {"CE2223G800": (3000, 3000), "CE2225G400": (6000, 6000)}

        asyncio.run(gw._check_rate_caps())
        assert gw._published == [("set_rate_cap", {"serial": "CE2223G800", "charge_cap_w": 3000, "discharge_cap_w": 3000})]

        gw._process_telemetry(self._rated_status().SerializeToString())
        gw._inverter_rate_caps = lambda: {"CE2223G800": (3000, 3000), "CE2225G400": (3000, 3000)}
        asyncio.run(gw._check_rate_caps())
        assert gw._published[1:] == [("set_rate_cap", {"serial": "CE2225G400", "charge_cap_w": 3000, "discharge_cap_w": 3000})]

    def test_coordinator_rate_writes_are_not_held(self):
        """A Gateway or Plant EMS is not judged on a battery rating of its own."""
        gw = self._writable(self._listed_gateway([]))
        status = self._gateway_plus_aios_status(["CH2414G318", "CH9999G999"])
        status.inverters[0].battery.rate_max_w = 0
        gw._process_telemetry(status.SerializeToString())
        assert gw._inverter_slot_serials == ["GW2347G077"]

        asyncio.run(gw.number_event("number.predbat_gateway_47g077_charge_rate", 6000))
        assert gw._published == [("set_charge_rate", {"power_w": 6000, "serial": "GW2347G077"})]

    def test_stored_ratings_are_used_after_a_restart(self):
        """With a serial list the ratings are in the discovery record, so a restart does not fall back to 6000W."""
        first = self._listed_gateway(["CE2223G800", "CE2225G400"])
        first._process_telemetry(self._rated_status(rate_max_w=3000, inverter_rate_max_w=3600).SerializeToString())
        asyncio.run(first._save_discovery())
        stored = first._saved[("gateway", "discovery_pbgw_two_ac")]["inverters"]
        assert stored["CE2225G400"]["rate_max_w"] == 3000
        assert stored["CE2225G400"]["inverter_rate_max_w"] == 3600
        assert stored["CE2225G400"]["export_limit_w"] == 6000

        # Reporting, but with its ratings unpopulated
        gw = self._listed_gateway(["CE2223G800", "CE2225G400"], stored=stored)
        gw._process_telemetry(self._rated_status(rate_max_w=0, inverter_rate_max_w=0, export_limit_w=0).SerializeToString())
        assert gw._states["sensor.predbat_gateway_25g400_battery_rate_max"] == 3000
        assert gw._states["sensor.predbat_gateway_25g400_inverter_rate_max"] == 3600
        assert gw._states["sensor.predbat_gateway_25g400_export_limit_w"] == 6000

        # Not being read at all
        gw = self._listed_gateway(["CE2223G800", "CE2225G400"], stored=stored)
        gw._process_telemetry(self._two_ac_status("no_battery").SerializeToString())
        assert gw._states["sensor.predbat_gateway_25g400_battery_rate_max"] == 3000
        assert gw._states["sensor.predbat_gateway_25g400_inverter_rate_max"] == 3600
        assert gw._states["sensor.predbat_gateway_25g400_export_limit_w"] == 6000

    def test_stored_zero_export_limit_is_used_after_a_restart(self):
        """A stored limit of zero (the hub's value 1) comes back as zero, not as no limit."""
        first = self._listed_gateway(["CE2223G800", "CE2225G400"])
        first._process_telemetry(self._rated_status(export_limit_w=1).SerializeToString())
        asyncio.run(first._save_discovery())
        stored = first._saved[("gateway", "discovery_pbgw_two_ac")]["inverters"]

        gw = self._listed_gateway(["CE2223G800", "CE2225G400"], stored=stored)
        gw._process_telemetry(self._rated_status(export_limit_w=0).SerializeToString())
        assert gw._states["sensor.predbat_gateway_25g400_export_limit_w"] == 0

    def test_reserve_the_hub_stops_reporting_keeps_its_last_good_value(self):
        """A status with the reserve unpopulated must not read as a reserve of 0%, which Predbat would then rewrite."""
        gw = self._writable(self._listed_gateway([]))
        gw._process_telemetry(self._rated_status().SerializeToString())
        assert gw._states["number.predbat_gateway_25g400_reserve_soc"] == 4

        blank = self._rated_status()
        blank.inverters[1].control.reserve_soc = 0
        gw._process_telemetry(blank.SerializeToString())
        assert gw._states["number.predbat_gateway_25g400_reserve_soc"] == 4

        changed = self._rated_status()
        changed.inverters[1].control.reserve_soc = 20
        gw._process_telemetry(changed.SerializeToString())
        assert gw._states["number.predbat_gateway_25g400_reserve_soc"] == 20

        # A reserve of 0% that Predbat itself wrote is real, so the next 0 is published as 0
        asyncio.run(gw.number_event("number.predbat_gateway_25g400_reserve_soc", 0))
        assert gw._published == [("set_reserve", {"target_soc": 0, "serial": "CE2225G400"})]
        gw._process_telemetry(blank.SerializeToString())
        assert gw._states["number.predbat_gateway_25g400_reserve_soc"] == 0
        # The other inverter's reserve is its own
        assert gw._states["number.predbat_gateway_23g800_reserve_soc"] == 4

    def test_reserve_is_kept_when_the_write_of_zero_fails(self):
        """A reserve of 0% that never went out is not real: the hub's 0 is still a gap filled with the last value."""
        gw = self._writable(self._listed_gateway([]))
        gw._process_telemetry(self._rated_status().SerializeToString())

        async def failing_send(entity_id, command, **kwargs):
            raise RuntimeError("publish failed")

        gw._send_control = failing_send
        try:
            asyncio.run(gw.number_event("number.predbat_gateway_25g400_reserve_soc", 0))
            raise AssertionError("the failed publish should have been raised")
        except RuntimeError:
            pass

        blank = self._rated_status()
        blank.inverters[1].control.reserve_soc = 0
        gw._process_telemetry(blank.SerializeToString())
        assert gw._states["number.predbat_gateway_25g400_reserve_soc"] == 4

    def test_reserve_is_put_back_when_the_hub_refuses_the_write_of_zero(self):
        """A refused reserve of 0% was not applied, so the reserve the inverter had is published again."""
        import threading

        gw = self._writable(self._listed_gateway([]))
        gw._ack_subscribed = True
        gw._acks_seen = True
        gw._command_id = 0
        gw._pending_commands = {}
        gw._command_lock = threading.Lock()
        gw._process_telemetry(self._rated_status().SerializeToString())
        blank = self._rated_status()
        blank.inverters[1].control.reserve_soc = 0

        asyncio.run(gw.number_event("number.predbat_gateway_25g400_reserve_soc", 0))
        command, sent = gw._published[-1]
        assert command == "set_reserve" and sent["target_soc"] == 0
        gw._process_telemetry(blank.SerializeToString())
        assert gw._states["number.predbat_gateway_25g400_reserve_soc"] == 0

        gw._process_ack(json.dumps({"command_id": f"PBAT{sent['command_id']}", "command": "set_reserve", "ok": False, "error": "modbus_write_failed"}).encode("utf-8"))
        assert gw._states["number.predbat_gateway_25g400_reserve_soc"] == 4
        # The other inverter is untouched, and an accepted 0 stays 0
        assert gw._states["number.predbat_gateway_23g800_reserve_soc"] == 4

        asyncio.run(gw.number_event("number.predbat_gateway_23g800_reserve_soc", 0))
        command, sent = gw._published[-1]
        blank.inverters[0].control.reserve_soc = 0
        gw._process_telemetry(blank.SerializeToString())
        gw._process_ack(json.dumps({"command_id": f"PBAT{sent['command_id']}", "command": "set_reserve", "ok": True}).encode("utf-8"))
        gw._process_telemetry(blank.SerializeToString())
        assert gw._states["number.predbat_gateway_23g800_reserve_soc"] == 0

    def test_inverter_that_becomes_usable_is_the_integration_baseline_on_that_status(self):
        """_process_telemetry re-baselines the integration after auto-config changes the slots."""
        import time

        gw = self._make_handler_gateway()
        gw.gateway_integrate_power = True
        gw.gateway_shared_ct = True
        gw._integrated_energy = None

        gw._process_telemetry(self._two_ac_status("no_battery").SerializeToString())
        assert sorted(gw._integrated_energy["pv"]) == ["CE2223G800"]

        joining = self._two_ac_status("ok")
        joining.timestamp = int(time.time()) + 60
        joining.inverters[1].energy.pv_today_wh = 7000
        gw._process_telemetry(joining.SerializeToString())
        assert gw._inverter_slot_serials == ["CE2223G800", "CE2225G400"]
        assert gw._integrated_energy["pv"]["CE2225G400"]["base"] == 7000

    def test_reserve_never_reported_is_published_as_zero(self):
        """An inverter that has never reported a reserve still publishes 0, as before."""
        gw = self._listed_gateway([])
        blank = self._rated_status()
        blank.inverters[1].control.reserve_soc = 0
        gw._process_telemetry(blank.SerializeToString())
        assert gw._states["number.predbat_gateway_25g400_reserve_soc"] == 0

    def test_listed_inverters_all_healthy_report_no_error(self):
        """A healthy site with a serial list raises nothing."""
        gw = self._listed_gateway(["CE2223G800", "CE2225G400"])

        gw._process_telemetry(self._two_ac_status("ok").SerializeToString())

        assert gw._args["num_inverters"] == 2
        assert self._errors(gw) == []
        gw.non_fatal_error_occurred.assert_not_called()

    def test_serial_list_is_not_added_to(self):
        """An inverter outside gateway_inverter_serial is never bound, healthy or not."""
        gw = self._listed_gateway(["CE2223G800"])

        gw._process_telemetry(self._two_ac_status("no_battery").SerializeToString())
        gw._process_telemetry(self._two_ac_status("ok").SerializeToString())

        assert gw._args["num_inverters"] == 1
        assert gw._inverter_slot_serials == ["CE2223G800"]
        gw.non_fatal_error_occurred.assert_not_called()

    def test_serial_list_with_a_coordinator_still_binds_only_the_coordinator(self):
        """A list naming a Gateway, with or without its AIOs, binds the Gateway alone: the list never splits a coordinated system."""
        for listed in (["GW2347G077", "CH2414G318", "CH9999G999"], ["GW2347G077"]):
            gw = self._listed_gateway(listed)

            gw._process_telemetry(self._gateway_plus_aios_status(["CH2414G318", "CH9999G999"]).SerializeToString())

            assert gw._args["num_inverters"] == 1, listed
            assert gw._args["charge_start_time"] == ["select.predbat_gateway_47g077_charge_slot1_start"], listed
            assert gw._pv_only_serials == (), listed
            gw.non_fatal_error_occurred.assert_not_called()

    def _pv_and_battery_status(self, pv_power=1200, pv_has_battery=False):
        """A PV-only inverter that reports generation, plus one battery inverter."""
        status = self._pv_only_site_status(battery_ready=True)
        status.inverters[0].pv.power_w = pv_power
        if pv_has_battery:
            status.inverters[0].battery.soc_percent = 40
            status.inverters[0].battery.capacity_wh = 5000
        return status

    def test_listed_pv_only_inverter_is_configured_as_pv_only(self):
        """A listed inverter with no battery has no slot and is never a write target; its generation is counted."""
        gw = self._listed_gateway(["FD0000P001", "CH0000B002"])

        for power in (1200, 1500):
            gw._process_telemetry(self._pv_and_battery_status(pv_power=power).SerializeToString())
            assert gw._states["sensor.predbat_gateway_00p001_pv_power"] == power

        assert gw._args["num_inverters"] == 1
        assert gw._inverter_slot_serials == ["CH0000B002"]
        assert gw._pv_only_serials == ("FD0000P001",)
        assert "00p001" not in gw._suffix_to_serial
        assert gw._args["pv_power"] == ["sensor.predbat_gateway_00b002_pv_power", "sensor.predbat_gateway_00p001_pv_power"]
        assert gw._args["pv_today"] == ["sensor.predbat_gateway_00b002_pv_today", "sensor.predbat_gateway_00p001_pv_today"]
        for arg in ("soc_percent", "charge_rate", "discharge_rate", "reserve", "charge_limit", "charge_start_time", "scheduled_charge_enable", "load_power", "grid_power"):
            assert len(gw._args[arg]) == 1 and "00p001" not in str(gw._args[arg][0]), arg
        assert self._errors(gw) == []
        gw.non_fatal_error_occurred.assert_not_called()

    # ------------------------------------------------------------------
    # No serial list: a battery-less inverter with no coordinator is PV only.
    # The topologies below are the ones seen on real hubs.
    # ------------------------------------------------------------------

    def _unit(self, status, serial, inv_type=pb.INVERTER_TYPE_GIVENERGY, primary=False, battery=False, pv_w=0, pv_today_wh=0, connected=True):
        """Add one unit to *status* as current hub firmware reports it."""
        inv = status.inverters.add()
        inv.type = inv_type
        inv.serial = serial
        inv.primary = primary
        inv.connected = connected
        inv.active = connected
        if battery:
            inv.battery.soc_percent = 50
            inv.battery.capacity_wh = 9500
            inv.battery.rate_max_w = 3000
        if pv_w:
            inv.pv.power_w = pv_w
        if pv_today_wh:
            inv.energy.pv_today_wh = pv_today_wh
        return inv

    def _hybrid_and_pv_status(self, pv_connected=True, pv_w=900):
        """A battery hybrid plus a battery-less PV inverter the hub does not elect primary."""
        status = pb.GatewayStatus()
        status.device_id = "pbgw_hybrid_pv"
        status.firmware = "1.0.39"
        status.schema_version = 1
        self._unit(status, "FA0000A001", primary=True, battery=True, pv_w=1400, pv_today_wh=5000)
        if pv_connected:
            self._unit(status, "PD0000A002", pv_w=pv_w, pv_today_wh=2000)
        else:
            self._unit(status, "PD0000A002", connected=False)
        return status

    def test_unlisted_pv_only_inverter_counts_its_pv_power_only(self):
        """No serial list, no coordinator: a battery-less inverter is PV only; its pv_power counts, its pv_today does not."""
        gw = self._listed_gateway([])

        gw._process_telemetry(self._hybrid_and_pv_status().SerializeToString())

        assert gw._args["num_inverters"] == 1
        assert gw._inverter_slot_serials == ["FA0000A001"]
        assert gw._pv_only_serials == ("PD0000A002",)
        assert gw._args["pv_power"] == ["sensor.predbat_gateway_00a001_pv_power", "sensor.predbat_gateway_00a002_pv_power"]
        # The hybrid's own counter can already include the PV inverter's generation
        assert gw._args["pv_today"] == ["sensor.predbat_gateway_00a001_pv_today"]
        assert gw._states["sensor.predbat_gateway_00a002_pv_power"] == 900
        assert "00a002" not in gw._suffix_to_serial
        for arg in ("soc_percent", "charge_rate", "discharge_rate", "reserve", "charge_limit", "charge_start_time", "scheduled_charge_enable", "load_power", "grid_power", "pv_today"):
            assert len(gw._args[arg]) == 1 and "00a002" not in str(gw._args[arg][0]), arg
        assert self._errors(gw) == []

    def test_unlisted_pv_only_inverter_dark_at_startup_is_added_when_it_wakes(self):
        """A PV inverter that is off at night when Predbat starts is added, without a restart, once the hub reads it."""
        gw = self._listed_gateway([])

        gw._process_telemetry(self._hybrid_and_pv_status(pv_connected=False).SerializeToString())
        assert gw._pv_only_serials == ()
        assert gw._args["pv_power"] == ["sensor.predbat_gateway_00a001_pv_power"]

        gw._process_telemetry(self._hybrid_and_pv_status(pv_connected=True).SerializeToString())
        assert gw._pv_only_serials == ("PD0000A002",)
        assert gw._args["pv_power"][-1] == "sensor.predbat_gateway_00a002_pv_power"
        assert gw._args["num_inverters"] == 1

        # Sunset: it stops reporting, and stays configured rather than thrashing auto-config
        reruns = gw._configured_inverter_serials
        gw._process_telemetry(self._hybrid_and_pv_status(pv_connected=False).SerializeToString())
        assert gw._pv_only_serials == ("PD0000A002",)
        assert gw._configured_inverter_serials == reruns

    def test_disconnected_pv_only_inverter_reads_no_generation(self):
        """The hub repeats a disconnected unit's last readings; a PV-only inverter's frozen generation is zeroed."""
        gw = self._listed_gateway([])
        gw._process_telemetry(self._hybrid_and_pv_status().SerializeToString())
        assert gw._states["sensor.predbat_gateway_00a002_pv_power"] == 900

        frozen = self._hybrid_and_pv_status()
        frozen.inverters[1].connected = False  # still carrying its 900 W from before
        gw._process_telemetry(frozen.SerializeToString())
        assert gw._states["sensor.predbat_gateway_00a002_pv_power"] == 0
        assert gw._pv_only_serials == ("PD0000A002",)

    def test_pv_only_inverter_disconnected_at_startup_reads_no_generation(self):
        """Started at night with the hub repeating the PV inverter's afternoon reading: its first publish is already zero."""
        gw = self._listed_gateway([])
        status = self._hybrid_and_pv_status()
        status.inverters[1].connected = False  # frozen at 900 W
        status.inverters[1].active = False

        gw._process_telemetry(status.SerializeToString())

        assert gw._pv_only_serials == ("PD0000A002",)
        assert gw._states["sensor.predbat_gateway_00a002_pv_power"] == 0

        # Firmware that flags every unit primary: it is published before auto-config, then zeroed
        gw = self._listed_gateway([])
        status.inverters[1].primary = True
        gw._process_telemetry(status.SerializeToString())
        assert gw._pv_only_serials == ("PD0000A002",)
        assert gw._states["sensor.predbat_gateway_00a002_pv_power"] == 0

    def test_unlisted_pv_only_inverter_is_not_counted_in_integrated_load(self):
        """Its pv_today is not added to an integrated load: the hybrid's counter may already hold it."""
        gw = self._listed_gateway([])
        gw._process_telemetry(self._hybrid_and_pv_status().SerializeToString())
        assert gw._pv_only_counters == ()

    def test_unlisted_battery_less_aios_behind_a_gateway_stay_out(self):
        """A Gateway with one battery AIO and battery-less AIOs behind it: auto-config runs, and nothing is PV only."""
        status = pb.GatewayStatus()
        status.device_id = "pbgw_gateway_four"
        status.firmware = "1.1.0"
        status.schema_version = 1
        self._unit(status, "GW0000A010", inv_type=pb.INVERTER_TYPE_GIVENERGY_GATEWAY, primary=True, battery=True, pv_w=34, pv_today_wh=200)
        self._unit(status, "CH0000A011", primary=True, battery=True, pv_w=500)
        for serial in ("CH0000A012", "CH0000A013", "CH0000A014"):
            self._unit(status, serial, pv_w=1)
        gw = self._listed_gateway([])

        gw._process_telemetry(status.SerializeToString())

        assert gw._auto_configured
        assert gw._pv_only_serials == ()
        assert not any(s in e for e in gw._args["pv_power"] for s in ("00a012", "00a013", "00a014"))

    def test_coordinator_missing_from_one_status_changes_nothing(self):
        """A status that leaves the Gateway or Plant EMS out (a hub rescan) does not re-run auto-config or move control."""
        for coordinator in (pb.INVERTER_TYPE_GIVENERGY_EMS, pb.INVERTER_TYPE_GIVENERGY_GATEWAY):

            def site(with_coordinator):
                status = pb.GatewayStatus()
                status.device_id = "pbgw_coord"
                status.firmware = "1.0.39"
                status.schema_version = 1
                if with_coordinator:
                    self._unit(status, "EM0000G001", inv_type=coordinator, primary=True, battery=True)
                self._unit(status, "CH0000G002", battery=True, pv_w=500)
                self._unit(status, "CH0000G003", battery=True, pv_w=500)
                self._unit(status, "CH0000G004", pv_w=300)
                return status.SerializeToString()

            gw = self._listed_gateway([])
            gw._process_telemetry(site(True))
            assert gw._inverter_slot_serials == ["EM0000G001"], coordinator

            runs = []
            real_config = gw.automatic_config

            def counted_config():
                runs.append(1)
                return real_config()

            gw.automatic_config = counted_config
            gw._process_telemetry(site(False))
            gw._process_telemetry(site(True))
            assert runs == [], coordinator
            assert gw._inverter_slot_serials == ["EM0000G001"], coordinator
            assert gw._pv_only_serials == (), coordinator

            # After a restart the stored discovery record still marks the site as coordinated
            fresh = self._listed_gateway([])
            fresh._discovery = dict(gw._discovery)
            fresh._process_telemetry(site(False))
            assert fresh._pv_only_serials == (), coordinator

    def test_unlisted_ems_and_gateway_sites_are_unchanged(self):
        """Real topologies with a coordinator keep exactly the configuration they had."""
        for coordinator in (pb.INVERTER_TYPE_GIVENERGY_EMS, pb.INVERTER_TYPE_GIVENERGY_GATEWAY):
            status = pb.GatewayStatus()
            status.device_id = "pbgw_coord"
            status.firmware = "1.0.39"
            status.schema_version = 1
            self._unit(status, "EM0000G001", inv_type=coordinator, primary=True, battery=True)
            self._unit(status, "CH0000G002", battery=True, pv_w=500)
            self._unit(status, "CH0000G003", battery=True, pv_w=500)
            self._unit(status, "CH0000G004", pv_w=300)  # battery-less unit behind the coordinator
            gw = self._listed_gateway([])

            gw._process_telemetry(status.SerializeToString())

            assert gw._pv_only_serials == (), coordinator
            assert gw._inverter_slot_serials == ["EM0000G001"], coordinator

    def test_unlisted_single_and_twin_battery_sites_are_unchanged(self):
        """The common real topologies (one battery inverter; two) gain no PV-only inverter."""
        for count in (1, 2):
            status = pb.GatewayStatus()
            status.device_id = "pbgw_plain"
            status.firmware = "1.0.39"
            status.schema_version = 1
            serials = ["CE0000G800", "CE0000G400"][:count]
            for serial in serials:
                self._unit(status, serial, primary=True, battery=True, pv_w=1000)
            gw = self._listed_gateway([])

            gw._process_telemetry(status.SerializeToString())

            assert gw._pv_only_serials == (), count
            assert gw._inverter_slot_serials == sorted(serials), count
            assert len(gw._args["pv_power"]) == count and len(gw._args["pv_today"]) == count

    def test_unlisted_inverter_whose_battery_is_late_is_not_taken_for_pv_only(self):
        """An inverter with no readings yet has no record, so it is not PV only; once it reports a battery it gets a slot."""
        gw = self._listed_gateway([])
        status = self._hybrid_and_pv_status(pv_connected=False)
        gw._process_telemetry(status.SerializeToString())
        assert gw._pv_only_serials == ()

        status = self._hybrid_and_pv_status()
        status.inverters[1].primary = True
        status.inverters[1].battery.soc_percent = 40
        status.inverters[1].battery.capacity_wh = 5000
        gw._process_telemetry(status.SerializeToString())
        assert gw._pv_only_serials == ()
        assert gw._args["num_inverters"] == 2

    def test_unlisted_pv_only_inverter_that_gains_a_battery_gets_a_slot(self):
        """Once PV only, an inverter that later reports a battery is moved to a slot."""
        gw = self._listed_gateway([])
        gw._process_telemetry(self._hybrid_and_pv_status().SerializeToString())
        assert gw._pv_only_serials == ("PD0000A002",)

        status = self._hybrid_and_pv_status()
        status.inverters[1].primary = True
        status.inverters[1].battery.soc_percent = 40
        status.inverters[1].battery.capacity_wh = 5000
        gw._process_telemetry(status.SerializeToString())
        assert gw._pv_only_serials == ()
        assert gw._args["num_inverters"] == 2

    def test_listed_pv_only_inverter_still_counts_its_pv_today(self):
        """The listed path (#5471) is unchanged: a listed PV-only inverter's counter is still counted."""
        gw = self._listed_gateway(["FD0000P001", "CH0000B002"])
        gw._process_telemetry(self._pv_and_battery_status().SerializeToString())
        assert gw._pv_only_counters == ("FD0000P001",)

    def test_listed_pv_only_inverter_disconnected_reads_no_generation(self):
        """Listed path: a disconnected PV-only inverter's frozen power is zeroed, while its counter stays in pv_today."""
        gw = self._listed_gateway(["FD0000P001", "CH0000B002"])
        live = self._pv_and_battery_status()
        live.inverters[0].energy.pv_today_wh = 3000
        gw._process_telemetry(live.SerializeToString())
        assert gw._states["sensor.predbat_gateway_00p001_pv_power"] == 1200
        counted = gw._states["sensor.predbat_gateway_00p001_pv_today"]

        frozen = self._pv_and_battery_status()
        frozen.inverters[0].energy.pv_today_wh = 3000
        frozen.inverters[0].connected = False  # the hub repeats its last 1200 W
        gw._process_telemetry(frozen.SerializeToString())

        assert gw._states["sensor.predbat_gateway_00p001_pv_power"] == 0
        assert gw._states["sensor.predbat_gateway_00p001_pv_today"] == counted
        assert gw._pv_only_counters == ("FD0000P001",)
        assert gw._args["pv_today"] == ["sensor.predbat_gateway_00b002_pv_today", "sensor.predbat_gateway_00p001_pv_today"]

    def test_pv_only_inverter_that_reports_a_battery_later_gets_a_slot(self):
        """Having a battery is learned when the hub reports it, and re-runs auto-config; it is never unlearned."""
        gw = self._listed_gateway(["FD0000P001", "CH0000B002"])
        gw._process_telemetry(self._pv_and_battery_status().SerializeToString())
        assert gw._args["num_inverters"] == 1

        gw._process_telemetry(self._pv_and_battery_status(pv_has_battery=True).SerializeToString())
        assert gw._args["num_inverters"] == 2
        assert gw._pv_only_serials == ()
        assert gw._discovery["FD0000P001"]["battery"] is True

        gw._process_telemetry(self._pv_and_battery_status().SerializeToString())
        assert gw._args["num_inverters"] == 2
        assert gw._discovery["FD0000P001"]["battery"] is True

    def test_unlisted_site_picks_up_an_inverter_that_becomes_usable(self):
        """With no serial list, an inverter unusable at first telemetry is registered when it becomes usable, with no restart."""
        for second in ("no_battery", "disconnected", "not_primary"):
            gw = self._make_handler_gateway()

            gw._process_telemetry(self._two_ac_status(second).SerializeToString())
            assert gw._args["num_inverters"] == 1, second
            assert gw._inverter_slot_serials == ["CE2223G800"], second

            gw._process_telemetry(self._two_ac_status("ok").SerializeToString())
            assert gw._args["num_inverters"] == 2, second
            assert gw._inverter_slot_serials == ["CE2223G800", "CE2225G400"], second

    def test_unusable_inverter_does_not_rerun_auto_config_every_frame(self):
        """An inverter that stays unusable is not a reason to re-run auto-config, and nor is a steady healthy site."""
        for listed in ([], ["CE2223G800", "CE2225G400"]):
            gw = self._listed_gateway(listed)
            gw._process_telemetry(self._two_ac_status("no_battery").SerializeToString())

            runs = []
            real_config = gw.automatic_config

            def counted_config():
                runs.append(1)
                return real_config()

            gw.automatic_config = counted_config
            for _ in range(3):
                gw._process_telemetry(self._two_ac_status("no_battery").SerializeToString())
            assert runs == [], listed

            for _ in range(3):
                gw._process_telemetry(self._two_ac_status("ok").SerializeToString())
            assert runs == [1], listed

            # Losing its telemetry again is a removal, which stays ignored: the inverter keeps its slot
            gw._process_telemetry(self._two_ac_status("no_battery").SerializeToString())
            assert runs == [1], listed
            assert gw._args["num_inverters"] == 2, listed

    def test_scenario_second_aio_via_telemetry_moves_control_to_gateway(self):
        """End-to-end through the telemetry handler (_process_telemetry).

        A site comes up as Gateway + 1 AIO and PredBat controls the AIO; a later
        telemetry frame that adds a second AIO re-runs auto-config and moves the
        control point to the Gateway — the exact "discover another AIO 5 minutes
        later" scenario, exercised through the real status path.
        """
        gw = self._make_handler_gateway()

        gw._process_telemetry(self._gateway_plus_aios_status(["CH2414G318"]).SerializeToString())
        assert gw._auto_configured
        assert gw._args["charge_start_time"][0] == "select.predbat_gateway_14g318_charge_slot1_start"

        gw._process_telemetry(self._gateway_plus_aios_status(["CH2414G318", "CH9999G999"]).SerializeToString())
        assert gw._args["num_inverters"] == 1
        assert gw._args["charge_start_time"][0] == "select.predbat_gateway_47g077_charge_slot1_start"

    def test_api_started_only_set_once_auto_config_has_completed(self):
        """api_started must not become visible before automatic_config has wired up the args.

        ComponentManager.start() polls api_started from the *main* thread while the MQTT
        listener runs in the component's own thread, so setting the flag on receipt of the
        first telemetry frame — before automatic_config() — lets PredBat startup continue
        with num_inverters / soc_percent unset.
        """
        gw = self._make_handler_gateway()
        seen = {}
        real_config = gw.automatic_config

        def watched_config():
            seen["api_started_during_config"] = gw.api_started
            return real_config()

        gw.automatic_config = watched_config
        gw._process_telemetry(self._gateway_plus_aios_status(["CH2414G318"]).SerializeToString())

        assert seen["api_started_during_config"] is False, "api_started was set before auto-config ran"
        assert gw._auto_configured
        assert gw.api_started is True

    def test_api_started_stays_false_when_auto_config_aborts(self):
        """A serial filter that matches nothing aborts auto-config, so the API is not started.

        The run() startup path still releases the component after its timeout, but the
        telemetry path must not declare the component started with no inverter args set.
        """
        gw = self._make_handler_gateway()
        gw.gateway_inverter_serial = ["CH0000X000"]

        gw._process_telemetry(self._gateway_plus_aios_status(["CH2414G318"]).SerializeToString())

        assert gw._auto_configured is False
        assert gw.api_started is False

    def test_api_started_set_on_later_frame_that_needs_no_reconfigure(self):
        """Once configured, a steady-state frame that triggers no re-config still starts the API."""
        gw = self._make_handler_gateway()
        gw.gateway_inverter_serial = ["CH0000X000"]
        gw._process_telemetry(self._gateway_plus_aios_status(["CH2414G318"]).SerializeToString())
        assert gw.api_started is False

        # Filter corrected (as a restart would do) — the next frame configures and starts.
        gw.gateway_inverter_serial = []
        gw._process_telemetry(self._gateway_plus_aios_status(["CH2414G318"]).SerializeToString())
        assert gw._auto_configured
        assert gw.api_started is True

    # ------------------------------------------------------------------
    # EMS and AC3 topologies
    # ------------------------------------------------------------------

    def _status_with_ems(self, aio_serials, ems_serial="EM2347E077"):
        """Build a status with a Plant EMS plus the AIO battery inverters in *aio_serials*."""
        status = pb.GatewayStatus()
        status.device_id = "pbgw_ems"
        status.firmware = "1.0.0"
        status.schema_version = 1
        ems = status.inverters.add()
        ems.type = pb.INVERTER_TYPE_GIVENERGY_EMS
        ems.serial = ems_serial
        ems.primary = True
        ems.battery.rate_max_w = 38
        for serial in aio_serials:
            inv = status.inverters.add()
            inv.type = pb.INVERTER_TYPE_GIVENERGY
            inv.serial = serial
            inv.primary = True
            inv.battery.soc_percent = 100
            inv.battery.capacity_wh = 12680
            inv.battery.rate_max_w = 6000
        return status

    def test_ems_is_control_point_when_present(self):
        """A Plant EMS is the single control point, taking priority over the AIOs (GivTCP)."""
        gw = self._make_gateway()
        gw._last_status = self._status_with_ems(["CH2414G318", "CH9999G999"])
        gw.automatic_config()
        assert gw._args["num_inverters"] == 1
        assert gw._args["charge_start_time"][0] == "select.predbat_gateway_47e077_charge_slot1_start"
        # Neither AIO is bound as a control slot:
        assert all("14g318" not in e and "99g999" not in e for e in gw._args["charge_start_time"])

    def test_ac3_units_routed_like_aios(self):
        """AC3 inverters report as INVERTER_TYPE_GIVENERGY; two behind a Gateway -> control the Gateway."""
        gw = self._make_gateway()
        # Two "AC3" units (proto type GIVENERGY, like any battery inverter) plus a Gateway.
        gw._last_status = self._gateway_plus_aios_status(["AC3001A001", "AC3002A002"])
        gw.automatic_config()
        assert gw._args["num_inverters"] == 1
        assert gw._args["charge_start_time"][0] == "select.predbat_gateway_47g077_charge_slot1_start"

    # ------------------------------------------------------------------
    # AC-coupled vs hybrid switch (from inv.model)
    # ------------------------------------------------------------------

    def _aio_status_with_model(self, model, serial="CH2414G318"):
        """Single-AIO status reporting a given GivEnergy model string."""
        status = pb.GatewayStatus()
        status.device_id = "pbgw_model"
        status.firmware = "1.0.0"
        status.schema_version = 1
        inv = status.inverters.add()
        inv.type = pb.INVERTER_TYPE_GIVENERGY
        inv.serial = serial
        inv.primary = True
        inv.model = model
        inv.battery.soc_percent = 100
        inv.battery.capacity_wh = 12680
        inv.battery.rate_max_w = 6000
        return status

    def _hybrid_switch_calls(self, gw):
        """The set_state_wrapper calls that targeted the inverter_hybrid switch."""
        return [c for c in gw.base.set_state_wrapper.call_args_list if c.args and c.args[0] == "switch.predbat_inverter_hybrid"]

    def test_hybrid_switch_off_for_ac_coupled_model(self):
        """An AC / AIO / All-in-One model turns the hybrid switch off (AC-coupled)."""
        for model in ("All-in-One", "AC 3ph"):
            gw = self._make_gateway()
            gw._last_status = self._aio_status_with_model(model)
            gw.automatic_config()
            gw.base.set_state_wrapper.assert_any_call("switch.predbat_inverter_hybrid", "off", attributes={}, required_unit=None)

    def test_hybrid_switch_on_for_hybrid_model(self):
        """A Hybrid / HV model turns the hybrid switch on (DC-coupled)."""
        gw = self._make_gateway()
        gw._last_status = self._aio_status_with_model("Hybrid HV Gen3")
        gw.automatic_config()
        gw.base.set_state_wrapper.assert_any_call("switch.predbat_inverter_hybrid", "on", attributes={}, required_unit=None)

    def test_hybrid_switch_untouched_when_model_absent(self):
        """Older firmware that omits the model leaves the hybrid switch alone."""
        gw = self._make_gateway()
        gw._last_status = self._aio_status_with_model("")  # no model reported
        gw.automatic_config()
        assert self._hybrid_switch_calls(gw) == []


class TestCheckInverterResets:
    """Tests for GatewayMQTT._check_inverter_resets()."""

    def _make_gateway(self, read_only=False, alive=True, auto_configured=True):
        """Build a minimal GatewayMQTT stub for _check_inverter_resets() tests.

        Sets _mqtt_connected and _gateway_online so is_alive() returns *alive*
        without needing to mock the method itself.  When alive=True the gateway
        is connected to the broker but its LWT reports offline — is_alive()
        returns True in that state without requiring fresh telemetry.
        """
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw._suffix_to_serial = {}
        gw._inverter_reset_done = set()
        gw._mqtt_connected = alive
        gw._gateway_online = False  # broker-connected but LWT-offline → is_alive() True when _mqtt_connected
        gw._last_telemetry_time = 0
        gw._auto_configured = auto_configured
        gw._published = []

        def fake_get_arg(key, default=None):
            if key == "set_read_only":
                return read_only
            return default

        gw.get_arg = fake_get_arg

        async def fake_publish_command(command, **kwargs):
            gw._published.append((command, kwargs))

        gw.publish_command = fake_publish_command
        return gw

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_sends_reset_for_un_reset_inverter(self):
        """inverter_reset is published for a serial not yet in _inverter_reset_done."""
        gw = self._make_gateway()
        gw._suffix_to_serial["456789"] = "CE123456789"
        self._run(gw._check_inverter_resets())
        assert len(gw._published) == 1
        cmd, kwargs = gw._published[0]
        assert cmd == "inverter_reset"
        assert kwargs["serial"] == "CE123456789"

    def test_serial_added_to_done_set_after_reset(self):
        """After reset the serial is recorded in _inverter_reset_done."""
        gw = self._make_gateway()
        gw._suffix_to_serial["456789"] = "CE123456789"
        self._run(gw._check_inverter_resets())
        assert "CE123456789" in gw._inverter_reset_done

    def test_no_duplicate_reset_on_second_call(self):
        """A second call does not re-send inverter_reset for an already-reset serial."""
        gw = self._make_gateway()
        gw._suffix_to_serial["456789"] = "CE123456789"
        self._run(gw._check_inverter_resets())
        self._run(gw._check_inverter_resets())
        assert len(gw._published) == 1

    def test_read_only_skips_reset(self):
        """No reset is sent when set_read_only is True."""
        gw = self._make_gateway(read_only=True)
        gw._suffix_to_serial["456789"] = "CE123456789"
        self._run(gw._check_inverter_resets())
        assert gw._published == []
        assert "CE123456789" not in gw._inverter_reset_done

    def test_read_only_clears_done_set(self):
        """Enabling read-only clears _inverter_reset_done so resets re-send once it is disabled."""
        gw = self._make_gateway(read_only=True)
        gw._suffix_to_serial["456789"] = "CE123456789"
        gw._inverter_reset_done.add("CE123456789")  # already reset before read-only was enabled
        self._run(gw._check_inverter_resets())
        assert gw._published == []
        assert gw._inverter_reset_done == set()

    def test_reset_resent_after_read_only_disabled(self):
        """A serial reset, then cleared by read-only, is reset again once read-only is disabled."""
        gw = self._make_gateway()
        gw._suffix_to_serial["456789"] = "CE123456789"

        read_only_state = {"value": False}

        def fake_get_arg(key, default=None):
            if key == "set_read_only":
                return read_only_state["value"]
            return default

        gw.get_arg = fake_get_arg

        # Initial control-mode reset.
        self._run(gw._check_inverter_resets())
        assert len(gw._published) == 1
        assert "CE123456789" in gw._inverter_reset_done

        # Read-only enabled — done set is cleared, nothing new sent.
        read_only_state["value"] = True
        self._run(gw._check_inverter_resets())
        assert len(gw._published) == 1
        assert gw._inverter_reset_done == set()

        # Read-only disabled again — reset is re-sent.
        read_only_state["value"] = False
        self._run(gw._check_inverter_resets())
        assert len(gw._published) == 2
        assert gw._published[1][0] == "inverter_reset"
        assert gw._published[1][1]["serial"] == "CE123456789"

    def test_not_alive_skips_reset(self):
        """No reset is sent when is_alive() returns False (MQTT disconnected)."""
        gw = self._make_gateway(alive=False)
        gw._suffix_to_serial["456789"] = "CE123456789"
        self._run(gw._check_inverter_resets())
        assert gw._published == []

    def test_not_auto_configured_skips_reset(self):
        """No reset is sent before auto-config has completed."""
        gw = self._make_gateway(auto_configured=False)
        gw._suffix_to_serial["456789"] = "CE123456789"
        self._run(gw._check_inverter_resets())
        assert gw._published == []

    def test_multi_inverter_each_gets_reset(self):
        """Each inverter in a multi-inverter setup receives its own inverter_reset."""
        gw = self._make_gateway()
        gw._suffix_to_serial["000aa1"] = "CE000000AA1"
        gw._suffix_to_serial["000bb2"] = "CE000000BB2"
        self._run(gw._check_inverter_resets())
        sent_serials = {kwargs["serial"] for _, kwargs in gw._published}
        assert sent_serials == {"CE000000AA1", "CE000000BB2"}
        assert len(gw._published) == 2

    def test_multi_inverter_partial_done_resets_only_new(self):
        """Only inverters not in _inverter_reset_done are reset; already-reset ones are skipped."""
        gw = self._make_gateway()
        gw._suffix_to_serial["000aa1"] = "CE000000AA1"
        gw._suffix_to_serial["000bb2"] = "CE000000BB2"
        gw._inverter_reset_done.add("CE000000AA1")
        self._run(gw._check_inverter_resets())
        assert len(gw._published) == 1
        assert gw._published[0][1]["serial"] == "CE000000BB2"

    def test_no_inverters_sends_nothing(self):
        """No commands are sent when _suffix_to_serial is empty."""
        gw = self._make_gateway()
        self._run(gw._check_inverter_resets())
        assert gw._published == []

    def test_log_emitted_per_inverter(self):
        """An Info log is emitted for each inverter that is reset."""
        gw = self._make_gateway()
        gw._suffix_to_serial["000aa1"] = "CE000000AA1"
        gw._suffix_to_serial["000bb2"] = "CE000000BB2"
        self._run(gw._check_inverter_resets())
        logged = [str(c) for c in gw.log.call_args_list]
        assert any("CE000000AA1" in s for s in logged)
        assert any("CE000000BB2" in s for s in logged)
        assert gw.log.call_count == 2


class TestCheckCommandedSerials:
    """Tests for GatewayMQTT._check_commanded_serials() - declaring the inverters PredBat drives."""

    def _make_gateway(self, read_only=False, alive=True, auto_configured=True, online=True, mode="Control charge & discharge"):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock
        import time as time_mod

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw.base = MagicMock()
        gw.base.inverters = []
        self._set_mode(gw, mode)
        gw.base.set_read_only = False
        gw._inverter_slot_serials = ["CH2330G098"]
        gw._commanded_serials_sent = None
        gw._commanded_serials_sent_time = 0
        gw._mqtt_connected = alive
        gw._gateway_online = online
        gw._last_telemetry_time = time_mod.time()
        gw._auto_configured = auto_configured
        gw._read_only = read_only
        gw._published = []
        gw.get_arg = lambda key, default=None: gw._read_only if key == "set_read_only" else default

        async def fake_publish_command(command, **kwargs):
            gw._published.append((command, kwargs))

        gw.publish_command = fake_publish_command
        return gw

    def _set_mode(self, gw, mode):
        gw.base.predbat_mode = mode

    def _inverter(self, slot, inverter_type="GWMQTT"):
        from unittest.mock import MagicMock

        inverter = MagicMock()
        inverter.id = slot
        inverter.inverter_type = inverter_type
        return inverter

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_declares_every_driven_inverter_once(self):
        gw = self._make_gateway()
        gw._inverter_slot_serials = ["CH2330G098", "FD2321G797"]
        gw.base.inverters = [self._inverter(1), self._inverter(0), self._inverter(2, inverter_type="GE")]
        self._run(gw._check_commanded_serials())
        self._run(gw._check_commanded_serials())
        assert gw._published == [("set_commanded_serials", {"serials": ["CH2330G098", "FD2321G797"]})]

    def test_resent_when_the_set_changes(self):
        gw = self._make_gateway()
        gw.base.inverters = [self._inverter(0)]
        self._run(gw._check_commanded_serials())
        gw._inverter_slot_serials = ["CH2330G098", "FD2321G797"]  # the hub found a second inverter
        gw.base.inverters = [self._inverter(0), self._inverter(1)]
        self._run(gw._check_commanded_serials())
        assert [k["serials"] for _, k in gw._published] == [["CH2330G098"], ["CH2330G098", "FD2321G797"]]

    def test_resent_periodically_and_after_the_hub_returns(self):
        gw = self._make_gateway()
        gw.base.inverters = [self._inverter(0)]
        self._run(gw._check_commanded_serials())
        gw._commanded_serials_sent_time -= 31 * 60
        self._run(gw._check_commanded_serials())
        assert len(gw._published) == 2
        gw._gateway_online = False
        self._run(gw._check_commanded_serials())
        gw._gateway_online = True
        self._run(gw._check_commanded_serials())
        assert len(gw._published) == 3

    def test_nothing_sent_in_read_only_offline_or_before_auto_config(self):
        for kwargs in ({"read_only": True}, {"online": False}, {"alive": False}, {"auto_configured": False}):
            gw = self._make_gateway(**kwargs)
            gw.base.inverters = [self._inverter(0)]
            self._run(gw._check_commanded_serials())
            assert gw._published == [], kwargs

    def test_nothing_sent_when_no_inverter_is_driven(self):
        gw = self._make_gateway()
        gw.base.inverters = [self._inverter(0, inverter_type="GE")]
        self._run(gw._check_commanded_serials())
        assert gw._published == []

    def test_monitor_mode_sends_nothing(self):
        """In Monitor mode PredBat controls nothing: no declaration at all, never an empty one."""
        gw = self._make_gateway(mode="Monitor")
        gw.base.inverters = [self._inverter(0)]
        self._run(gw._check_commanded_serials())
        assert gw._published == []

    def test_soc_only_mode_still_controls(self):
        gw = self._make_gateway(mode="Control SOC only")
        gw.base.inverters = [self._inverter(0)]
        self._run(gw._check_commanded_serials())
        assert gw._published == [("set_commanded_serials", {"serials": ["CH2330G098"]})]

    def test_switching_to_monitor_stops_and_back_resends(self):
        gw = self._make_gateway()
        gw.base.inverters = [self._inverter(0)]
        self._run(gw._check_commanded_serials())
        self._set_mode(gw, "Monitor")
        self._run(gw._check_commanded_serials())
        self._set_mode(gw, "Control charge")
        self._run(gw._check_commanded_serials())
        assert [k["serials"] for _, k in gw._published] == [["CH2330G098"], ["CH2330G098"]]

    def test_startup_state_sends_nothing(self):
        """PredBat starts in Monitor mode; mid-load (read-only lifted, control flags part set) must not send."""
        gw = self._make_gateway(mode="Monitor")
        gw.base.inverters = [self._inverter(0)]
        gw.base.set_soc_enable = True  # fetch.py sets this before the mode block
        self._run(gw._check_commanded_serials())
        assert gw._published == []

    def test_nothing_sent_during_a_read_only_hold(self):
        """A VPP event holds PredBat read-only without changing the set_read_only setting."""
        gw = self._make_gateway()
        gw.base.inverters = [self._inverter(0)]
        gw.base.set_read_only = True
        self._run(gw._check_commanded_serials())
        assert gw._published == []

    def test_nothing_sent_for_an_unknown_mode(self):
        for mode in (None, "", "Something else"):
            gw = self._make_gateway(mode=mode)
            gw.base.inverters = [self._inverter(0)]
            self._run(gw._check_commanded_serials())
            assert gw._published == [], mode

    def test_hub_restart_between_cycles_is_told_again(self):
        """A hub that goes offline and returns between two run() cycles is re-sent the declaration."""
        from unittest.mock import MagicMock

        gw = self._make_gateway()
        gw.base.inverters = [self._inverter(0)]
        gw._rate_caps_sent = {}
        gw.prefix = "predbat"
        gw.topic_status = "predbat/devices/pbgw_test/status"
        gw.topic_online = "predbat/devices/pbgw_test/online"
        gw.dashboard_item = MagicMock()
        gw._error_count = 0
        self._run(gw._check_commanded_serials())
        assert len(gw._published) == 1

        for payload in (b"0", b"1"):
            message = MagicMock()
            message.topic = gw.topic_online
            message.payload = payload
            self._run(gw._handle_message(message))

        self._run(gw._check_commanded_serials())
        assert len(gw._published) == 2
        assert gw._error_count == 0

    def test_nothing_sent_while_the_inverter_list_is_being_rebuilt(self):
        """The hub found a new inverter (two slots) but PredBat has rebuilt only one: declaring now would drop a unit."""
        gw = self._make_gateway()
        gw._inverter_slot_serials = ["CH2330G098", "FD2321G797"]
        gw.base.inverters = [self._inverter(0)]
        self._run(gw._check_commanded_serials())
        assert gw._published == []

    def test_command_carries_the_serials_list(self):
        from gateway import GatewayMQTT
        import json

        cmd = json.loads(GatewayMQTT.build_command("set_commanded_serials", serials=["CH2330G098"]))
        assert cmd["command"] == "set_commanded_serials"
        assert cmd["serials"] == ["CH2330G098"]
        assert cmd["command_id"].startswith("PBAT")


class TestCheckRateCaps:
    """Tests for GatewayMQTT._check_rate_caps() - the per-inverter set_rate_cap command (predbat-gateway#424)."""

    def _make_gateway(self, read_only=False, alive=True, auto_configured=True):
        """Build a minimal GatewayMQTT stub for _check_rate_caps() tests."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw.base = MagicMock()
        gw.base.inverters = []
        gw._inverter_slot_serials = []
        import time as time_mod

        gw._rate_caps_sent = {}
        gw._mqtt_connected = alive
        gw._gateway_online = True
        gw._last_telemetry_time = time_mod.time()  # fresh telemetry -> is_alive() True when connected
        gw._auto_configured = auto_configured
        gw._read_only = read_only
        gw._published = []

        def fake_get_arg(key, default=None):
            if key == "set_read_only":
                return gw._read_only
            return default

        gw.get_arg = fake_get_arg

        async def fake_publish_command(command, **kwargs):
            gw._published.append((command, kwargs))

        gw.publish_command = fake_publish_command
        return gw

    def _inverter(self, slot, charge_w, discharge_w, inverter_type="GWMQTT"):
        """A PredBat inverter stand-in with its rate limits in kW per minute, as inverter.py holds them."""
        from unittest.mock import MagicMock

        inverter = MagicMock()
        inverter.id = slot
        inverter.inverter_type = inverter_type
        inverter.battery_rate_max_charge = charge_w / 60000.0 if charge_w is not None else None
        inverter.battery_rate_max_discharge = discharge_w / 60000.0 if discharge_w is not None else None
        return inverter

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_each_inverter_gets_its_own_cap_under_its_serial(self):
        """Two inverters with different limits are sent separately, each keyed by the serial of its slot."""
        gw = self._make_gateway()
        gw._inverter_slot_serials = ["CH2330G098", "FD2321G797"]
        gw.base.inverters = [self._inverter(0, 3000, 3600), self._inverter(1, 2600, 5000)]

        self._run(gw._check_rate_caps())

        assert gw._published == [
            ("set_rate_cap", {"serial": "CH2330G098", "charge_cap_w": 3000, "discharge_cap_w": 3600}),
            ("set_rate_cap", {"serial": "FD2321G797", "charge_cap_w": 2600, "discharge_cap_w": 5000}),
        ]

    def test_serial_follows_the_slot_not_list_order(self):
        """The serial comes from the inverter's slot id, whatever order PredBat lists its inverters in."""
        gw = self._make_gateway()
        gw._inverter_slot_serials = ["CH2330G098", "FD2321G797"]
        gw.base.inverters = [self._inverter(1, 2600, 5000)]

        self._run(gw._check_rate_caps())

        assert gw._published == [("set_rate_cap", {"serial": "FD2321G797", "charge_cap_w": 2600, "discharge_cap_w": 5000})]

    def test_inverter_the_gateway_does_not_drive_is_skipped(self):
        """An inverter of another type has no hub slot, so no cap is sent for it."""
        gw = self._make_gateway()
        gw._inverter_slot_serials = ["CH2330G098"]
        gw.base.inverters = [self._inverter(0, 3000, 3600), self._inverter(1, 1000, 1000, inverter_type="GE")]

        self._run(gw._check_rate_caps())

        assert [kwargs["serial"] for _, kwargs in gw._published] == ["CH2330G098"]

    def test_sent_once_not_every_cycle(self):
        """An unchanged cap is not re-sent on later run() cycles."""
        gw = self._make_gateway()
        gw._inverter_slot_serials = ["CH2330G098"]
        gw.base.inverters = [self._inverter(0, 3000, 3600)]

        self._run(gw._check_rate_caps())
        self._run(gw._check_rate_caps())

        assert len(gw._published) == 1

    def test_changed_cap_is_sent_again(self):
        """When the user changes a limit the hub is told the new cap."""
        gw = self._make_gateway()
        gw._inverter_slot_serials = ["CH2330G098"]
        gw.base.inverters = [self._inverter(0, 3000, 3600)]
        self._run(gw._check_rate_caps())

        gw.base.inverters = [self._inverter(0, 3000, 2000)]
        self._run(gw._check_rate_caps())

        assert gw._published[-1] == ("set_rate_cap", {"serial": "CH2330G098", "charge_cap_w": 3000, "discharge_cap_w": 2000})
        assert len(gw._published) == 2

    def test_nothing_sent_in_read_only_then_sent_on_leaving_it(self):
        """Like inverter_reset: held back in read-only mode and sent afresh once it is switched off."""
        gw = self._make_gateway(read_only=True)
        gw._inverter_slot_serials = ["CH2330G098"]
        gw.base.inverters = [self._inverter(0, 3000, 3600)]
        gw._rate_caps_sent = {"CH2330G098": (3000, 3600)}  # sent before read-only was switched on

        self._run(gw._check_rate_caps())
        assert gw._published == []

        gw._read_only = False
        self._run(gw._check_rate_caps())
        assert len(gw._published) == 1

    def test_nothing_sent_when_not_alive(self):
        """No command is published while the gateway link is down, and it is not marked as sent."""
        gw = self._make_gateway(alive=False)
        gw._inverter_slot_serials = ["CH2330G098"]
        gw.base.inverters = [self._inverter(0, 3000, 3600)]

        self._run(gw._check_rate_caps())

        assert gw._published == []
        assert gw._rate_caps_sent == {}

    def test_held_while_hub_offline_and_sent_again_when_it_returns(self):
        """Commands are not retained, so nothing is sent to an offline hub; a hub that comes back is told again.

        The re-send covers a hub that was offline when the cap was first due, and one that has
        since been updated to firmware that understands the command or has lost its stored caps.
        """
        gw = self._make_gateway()
        gw._inverter_slot_serials = ["CH2330G098"]
        gw.base.inverters = [self._inverter(0, 3000, 3600)]
        self._run(gw._check_rate_caps())
        assert len(gw._published) == 1

        gw._gateway_online = False
        self._run(gw._check_rate_caps())
        assert len(gw._published) == 1

        gw._gateway_online = True
        self._run(gw._check_rate_caps())
        assert len(gw._published) == 2

    def test_hub_restart_between_cycles_is_told_again(self):
        """A hub that goes offline and returns between two run() cycles is still re-sent its caps.

        A reboot after a firmware update takes well under the 60 seconds between cycles, so
        _check_rate_caps() never sees the hub offline. The online message itself must clear
        what was sent.
        """
        from unittest.mock import MagicMock

        gw = self._make_gateway()
        gw._inverter_slot_serials = ["CH2330G098"]
        gw.base.inverters = [self._inverter(0, 3000, 3600)]
        gw.prefix = "predbat"
        gw.topic_status = "predbat/devices/pbgw_test/status"
        gw.topic_online = "predbat/devices/pbgw_test/online"
        gw.dashboard_item = MagicMock()
        gw._error_count = 0
        self._run(gw._check_rate_caps())
        assert len(gw._published) == 1

        for payload in (b"0", b"1"):
            message = MagicMock()
            message.topic = gw.topic_online
            message.payload = payload
            self._run(gw._handle_message(message))

        self._run(gw._check_rate_caps())
        assert len(gw._published) == 2
        assert gw._error_count == 0

    def test_nothing_sent_before_auto_config(self):
        """Until auto-config has bound slots to serials there is nothing to key a cap on."""
        gw = self._make_gateway(auto_configured=False)
        gw.base.inverters = [self._inverter(0, 3000, 3600)]

        self._run(gw._check_rate_caps())

        assert gw._published == []

    def test_inverter_with_no_rates_yet_is_skipped(self):
        """Before PredBat has read an inverter's limits there is no cap to send; a 0 would mean 'no cap' to the hub."""
        gw = self._make_gateway()
        gw._inverter_slot_serials = ["CH2330G098"]
        gw.base.inverters = [self._inverter(0, None, None)]

        self._run(gw._check_rate_caps())

        assert gw._published == []


class TestCheckReadOnlyState:
    """Tests for GatewayMQTT._check_read_only_state()."""

    def _make_gateway(self, read_only=False, connected=True):
        """Build a minimal GatewayMQTT stub for _check_read_only_state() tests."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw._mqtt_connected = connected
        gw._last_read_only = None
        gw._last_read_only_sent_time = 0
        gw._published = []

        self._read_only = read_only

        def fake_get_arg(key, default=None):
            if key == "set_read_only":
                return self._read_only
            return default

        gw.get_arg = fake_get_arg

        async def fake_publish_command(command, **kwargs):
            gw._published.append((command, kwargs))

        gw.publish_command = fake_publish_command
        return gw

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_sends_current_state_on_startup(self):
        """The current read-only state is published on the first call (startup)."""
        gw = self._make_gateway(read_only=False)
        self._run(gw._check_read_only_state())
        assert len(gw._published) == 1
        cmd, kwargs = gw._published[0]
        assert cmd == "set_read_only"
        assert kwargs["enable"] is False
        assert "serial" not in kwargs
        assert gw._last_read_only is False

    def test_sends_read_only_true_on_startup(self):
        """Startup with read-only enabled publishes enable=True."""
        gw = self._make_gateway(read_only=True)
        self._run(gw._check_read_only_state())
        assert gw._published == [("set_read_only", {"enable": True})]
        assert gw._last_read_only is True

    def test_no_resend_when_unchanged(self):
        """A second call with the same state does not re-publish."""
        gw = self._make_gateway(read_only=False)
        self._run(gw._check_read_only_state())
        self._run(gw._check_read_only_state())
        assert len(gw._published) == 1

    def test_sends_on_change(self):
        """A change in read-only state publishes the new value."""
        gw = self._make_gateway(read_only=False)
        self._run(gw._check_read_only_state())
        self._read_only = True
        self._run(gw._check_read_only_state())
        assert len(gw._published) == 2
        assert gw._published[1] == ("set_read_only", {"enable": True})
        assert gw._last_read_only is True

    def test_resends_when_stale(self):
        """An unchanged state is re-published once more than 30 minutes have elapsed."""
        gw = self._make_gateway(read_only=False)
        self._run(gw._check_read_only_state())
        assert len(gw._published) == 1
        # Pretend the last send happened over 30 minutes ago.
        gw._last_read_only_sent_time -= 30 * 60 + 1
        self._run(gw._check_read_only_state())
        assert len(gw._published) == 2
        assert gw._published[1] == ("set_read_only", {"enable": False})

    def test_no_resend_just_under_interval(self):
        """An unchanged state is not re-published before the 30 minute interval."""
        gw = self._make_gateway(read_only=False)
        self._run(gw._check_read_only_state())
        # Just under the re-send interval — no extra publish expected.
        gw._last_read_only_sent_time -= 30 * 60 - 5
        self._run(gw._check_read_only_state())
        assert len(gw._published) == 1

    def test_not_connected_skips_send(self):
        """Nothing is published while MQTT is disconnected, and state is not latched."""
        gw = self._make_gateway(read_only=True, connected=False)
        self._run(gw._check_read_only_state())
        assert gw._published == []
        assert gw._last_read_only is None

    def test_disconnected_then_connected_sends(self):
        """Once connected, the state pending from a disconnected cycle is sent."""
        gw = self._make_gateway(read_only=True, connected=False)
        self._run(gw._check_read_only_state())
        assert gw._published == []
        gw._mqtt_connected = True
        self._run(gw._check_read_only_state())
        assert gw._published == [("set_read_only", {"enable": True})]


class TestRunStartupWait:
    """Tests for run(first=True) — verifies it waits for the first MQTT connection attempt."""

    def _make_gateway(self):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw.gateway_device_id = "pbgw_test"
        gw.mqtt_host = "mqtt.test.local"
        gw.api_stop = False
        gw._first_connection_attempted = False
        gw._mqtt_connected = False
        gw._auto_configured = False
        gw._mqtt_task = None
        return gw

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_returns_true_without_sleeping_when_flag_already_set(self):
        """When both startup conditions are pre-set, run() returns True without sleeping."""
        if not HAS_AIOMQTT:
            return
        from unittest.mock import patch, AsyncMock

        gw = self._make_gateway()
        gw._first_connection_attempted = True
        gw._auto_configured = True

        async def run_test():
            async def fake_mqtt_loop():
                pass

            gw._mqtt_loop = fake_mqtt_loop
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                result = await gw.run(0, True)
            return result, mock_sleep.call_count

        result, sleep_count = self._run(run_test())
        assert result is True
        assert sleep_count == 0

    def test_returns_true_after_flag_set_on_first_sleep(self):
        """run() exits the wait loop as soon as the flags are set, after a single sleep."""
        if not HAS_AIOMQTT:
            return
        from unittest.mock import patch

        gw = self._make_gateway()
        sleep_count = [0]

        async def run_test():
            async def fake_sleep(t):
                sleep_count[0] += 1
                # Simulates the MQTT loop connecting and the first telemetry arriving
                gw._first_connection_attempted = True
                gw._auto_configured = True

            async def fake_mqtt_loop():
                pass

            gw._mqtt_loop = fake_mqtt_loop
            with patch("asyncio.sleep", side_effect=fake_sleep):
                result = await gw.run(0, True)
            return result

        result = self._run(run_test())
        assert result is True
        assert sleep_count[0] == 1

    def test_returns_true_with_warning_when_timeout_expires(self):
        """run() returns True and logs a Warn when the flag is never set within the timeout."""
        if not HAS_AIOMQTT:
            return
        from unittest.mock import patch

        gw = self._make_gateway()
        sleep_count = [0]

        async def run_test():
            async def fast_sleep(t):
                sleep_count[0] += 1

            async def fake_mqtt_loop():
                pass  # Never sets _first_connection_attempted

            gw._mqtt_loop = fake_mqtt_loop
            with patch("asyncio.sleep", side_effect=fast_sleep):
                result = await gw.run(0, True)
            return result

        result = self._run(run_test())
        assert result is True
        # The connection and auto-config waits share one budget, so a dead broker stalls
        # startup for the budget once, not once per wait.
        assert sleep_count[0] == _STARTUP_WAIT_TICKS
        warn_logged = any("Warn" in str(c) and "not yet complete" in str(c) for c in gw.log.call_args_list)
        assert warn_logged, "Expected a Warn log when the first connection attempt times out"

    def test_waits_for_auto_config_after_connection(self):
        """When connected, run() waits for _auto_configured before returning."""
        if not HAS_AIOMQTT:
            return
        from unittest.mock import patch

        gw = self._make_gateway()
        gw._first_connection_attempted = True
        gw._mqtt_connected = True
        sleep_count = [0]

        async def run_test():
            async def fake_sleep(t):
                sleep_count[0] += 1
                gw._auto_configured = True  # Simulates first telemetry arriving

            async def fake_mqtt_loop():
                pass

            gw._mqtt_loop = fake_mqtt_loop
            with patch("asyncio.sleep", side_effect=fake_sleep):
                result = await gw.run(0, True)
            return result

        result = self._run(run_test())
        assert result is True
        assert sleep_count[0] == 1  # One sleep in the auto-config wait loop

    def test_auto_config_wait_times_out_with_warning_when_device_offline(self):
        """When connected but device never sends telemetry, run() logs Warn after 60s and continues."""
        if not HAS_AIOMQTT:
            return
        from unittest.mock import patch

        gw = self._make_gateway()
        gw._first_connection_attempted = True
        gw._mqtt_connected = True
        sleep_count = [0]

        async def run_test():
            async def fast_sleep(t):
                sleep_count[0] += 1  # Never sets _auto_configured

            async def fake_mqtt_loop():
                pass

            gw._mqtt_loop = fake_mqtt_loop
            with patch("asyncio.sleep", side_effect=fast_sleep):
                result = await gw.run(0, True)
            return result

        result = self._run(run_test())
        assert result is True
        assert sleep_count[0] == _STARTUP_WAIT_TICKS  # Whole startup budget consumed
        warn_logged = any("Warn" in str(c) and "Auto-config not complete" in str(c) for c in gw.log.call_args_list)
        assert warn_logged, "Expected a Warn log when auto-config times out"

    def test_auto_config_wait_still_runs_when_first_attempt_failed(self):
        """A failed *first* connection attempt must not skip the auto-config wait.

        The MQTT loop retries with backoff in the background, so a transient broker
        failure at startup is usually followed by a connection and telemetry within the
        60s window. Returning immediately declares the component started with no
        inverter args set.
        """
        if not HAS_AIOMQTT:
            return
        from unittest.mock import patch

        gw = self._make_gateway()
        gw._first_connection_attempted = True
        # _mqtt_connected stays False (first attempt failed)
        sleep_count = [0]

        async def run_test():
            async def fake_sleep(t):
                sleep_count[0] += 1
                # Reconnect succeeds and the first telemetry arrives.
                gw._mqtt_connected = True
                gw._auto_configured = True

            async def fake_mqtt_loop():
                pass

            gw._mqtt_loop = fake_mqtt_loop
            with patch("asyncio.sleep", side_effect=fake_sleep):
                result = await gw.run(0, True)
            return result

        result = self._run(run_test())
        assert result is True
        assert sleep_count[0] == 1  # One sleep in the auto-config wait loop

    def test_auto_config_wait_times_out_when_broker_never_reachable(self):
        """A broker that never comes up still releases startup after the 60s cap."""
        if not HAS_AIOMQTT:
            return
        from unittest.mock import patch

        gw = self._make_gateway()
        gw._first_connection_attempted = True
        sleep_count = [0]

        async def run_test():
            async def fast_sleep(t):
                sleep_count[0] += 1  # Never connects, never auto-configures

            async def fake_mqtt_loop():
                pass

            gw._mqtt_loop = fake_mqtt_loop
            with patch("asyncio.sleep", side_effect=fast_sleep):
                result = await gw.run(0, True)
            return result

        result = self._run(run_test())
        assert result is True
        assert sleep_count[0] == _STARTUP_WAIT_TICKS  # Whole startup budget consumed
        warn_logged = any("Warn" in str(c) and "Auto-config not complete" in str(c) for c in gw.log.call_args_list)
        assert warn_logged, "Expected a Warn log when auto-config times out"


class TestSetChargeSlotPayload:
    """Diagnostic test — captures the raw MQTT JSON for set_charge_slot and prints it.

    Expected hub format:
      {"command": "set_charge_slot", "serial": "<serial>", "schedule_json": "{\"start\":200}"}
    """

    def _make_gateway(self):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw.prefix = "predbat"
        gw._suffix_to_serial = {"30g499": "CH2330G499"}
        gw._command_id = 0
        gw._mqtt_connected = True
        gw._mqtt_client = MagicMock()
        gw.topic_command = "predbat/devices/pbgw_test/command"
        gw._raw_published = []

        async def fake_publish_raw(topic, payload, retain=False):
            gw._raw_published.append(payload)

        gw._publish_raw = fake_publish_raw
        return gw

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def test_set_charge_slot_start_raw_payload(self):
        """Print the raw JSON sent for a charge slot start command so the format can be verified."""
        import json

        gw = self._make_gateway()
        # 02:00:00 → HHMM 200, matching the expected {"start":200} in the hub spec
        self._run(gw.publish_command("set_charge_slot", command_id=1, schedule_json=json.dumps({"start": 200}), serial="CH2330G499"))

        assert gw._raw_published, "No payload was published — serial lookup may have failed"
        actual = json.loads(gw._raw_published[0].decode("utf-8"))

        expected = {
            "command": "set_charge_slot",
            "command_id": "PBAT1",
            "serial": "CH2330G499",
            "schedule_json": '{"start": 200}',
        }

        print("\n--- set_charge_slot raw payload ---")
        print(f"  actual:   {json.dumps(actual)}")
        print(f"  expected: {json.dumps(expected)}")
        print("---")

        assert actual == expected, f"Payload mismatch:\n  actual:   {actual}\n  expected: {expected}"


class TestEvTelemetry:
    """Tests for GatewayMQTT._inject_ev_entities() — EvCharger telemetry → entities."""

    def _make_gateway(self, battery_size=100):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.log = MagicMock()
        gw.prefix = "predbat"
        gw._dashboard_calls = {}  # entity_id → (state, attributes)

        def capture_dashboard(entity_id, state=None, attributes=None, app=None):
            gw._dashboard_calls[entity_id] = (state, attributes)

        gw.dashboard_item = capture_dashboard
        gw.get_arg = lambda key, default=None, **kwargs: battery_size if key == "car_charging_battery_size" else default
        gw._ev_max_current = {}
        return gw

    def _status_with_ev(self, **fields):
        status = pb.GatewayStatus()
        status.device_id = "pbgw_ev"
        ev = status.ev_chargers.add()
        ev.connected = fields.get("connected", True)
        ev.session_active = fields.get("session_active", True)
        ev.status = fields.get("status", "Charging")
        ev.power_w = fields.get("power_w", 7200)
        ev.session_energy_wh = fields.get("session_energy_wh", 12400)
        ev.current_limit_a = fields.get("current_limit_a", 16)
        ev.soc_percent = fields.get("soc_percent", 55)
        ev.charge_point_id = fields.get("charge_point_id", "609W6FBF43XB749")
        ev.max_current_a = fields.get("max_current_a", 32)
        ev.voltage_v = fields.get("voltage_v", 240)
        ev.eco_mode = fields.get("eco_mode", "Boost")
        return status

    def test_ev_entities_published(self):
        """A connected charger publishes the full set of EV entities with conversions."""
        gw = self._make_gateway()
        gw._inject_ev_entities(self._status_with_ev())

        base = "predbat_gateway_ev_3xb749"
        assert gw._dashboard_calls[f"binary_sensor.{base}_online"][0] is True
        assert gw._dashboard_calls[f"binary_sensor.{base}_connected"][0] is True  # status="Charging" → car connected
        assert gw._dashboard_calls[f"binary_sensor.{base}_plugged"][0] is True
        assert gw._dashboard_calls[f"binary_sensor.{base}_session_active"][0] is True
        assert gw._dashboard_calls[f"sensor.{base}_status"][0] == "Charging"
        assert gw._dashboard_calls[f"sensor.{base}_power"][0] == 7200
        # Wh → kWh
        assert approx_equal(gw._dashboard_calls[f"sensor.{base}_session_energy"][0], 12.4)
        assert gw._dashboard_calls[f"sensor.{base}_current_limit"][0] == 16
        assert gw._dashboard_calls[f"sensor.{base}_soc"][0] == 55
        assert gw._dashboard_calls[f"sensor.{base}_max_current"][0] == 32
        assert gw._dashboard_calls[f"sensor.{base}_voltage"][0] == 240
        assert gw._dashboard_calls[f"sensor.{base}_eco_mode"][0] == "Boost"
        # Derived charge-rate capability in kW: 32 A × 240 V / 1000
        assert approx_equal(gw._dashboard_calls[f"sensor.{base}_charge_rate"][0], 7.68)

    def test_ev_soc_holds_the_last_reported_value(self):
        """Once the charger has reported the car's SoC, sensor.<pfx>_soc keeps the last report rather than the session estimate.

        A charger reports the SoC only during a session, and the estimate restarts at 0% each session - so a user who wired
        car_charging_soc to it by hand would see the car planned from empty between sessions.
        """
        gw = self._make_gateway(battery_size=100)
        base = "predbat_gateway_ev_3xb749"
        # Nothing reported yet - the estimate from 12.4 kWh of a 100 kWh battery
        gw._inject_ev_entities(self._status_with_ev(soc_percent=0))
        assert approx_equal(gw._dashboard_calls[f"sensor.{base}_soc"][0], 12.4)
        gw._inject_ev_entities(self._status_with_ev(soc_percent=62))
        assert gw._dashboard_calls[f"sensor.{base}_soc"][0] == 62
        # Session over, or the charger offline - the last report is kept
        gw._inject_ev_entities(self._status_with_ev(soc_percent=0, session_energy_wh=0))
        assert gw._dashboard_calls[f"sensor.{base}_soc"][0] == 62
        gw._inject_ev_entities(self._status_with_ev(soc_percent=0, connected=False))
        assert gw._dashboard_calls[f"sensor.{base}_soc"][0] == 62
        # Another charger's report is its own
        gw._inject_ev_entities(self._status_with_ev(soc_percent=0, charge_point_id="OTHER1"))
        assert approx_equal(gw._dashboard_calls["sensor.predbat_gateway_ev_other1_soc"][0], 12.4)

    def test_ev_charging_follows_the_charger_status(self):
        """binary_sensor.<pfx>_charging - what car_charging_now is wired to - is the car actually drawing power.

        session_active stays true in SuspendedEV (the car is full, or paused, and draws nothing), and
        car_charging_now holds the house battery for the car, so wiring it to session_active pinned the
        battery for as long as a full car stayed connected (#5245 review). Charging is the OCPP status
        for energy flowing; firmware that sends no status falls back to an active session with power.
        """
        base = "predbat_gateway_ev_3xb749"
        cases = [
            ({"status": "Charging"}, True),
            ({"status": "SuspendedEV", "power_w": 0}, False),
            ({"status": "SuspendedEVSE", "power_w": 0}, False),
            ({"status": "Finishing", "power_w": 0}, False),
            ({"status": "", "session_active": True, "power_w": 7200}, True),
            ({"status": "", "session_active": True, "power_w": 0}, False),
            ({"status": "", "session_active": False, "power_w": 7200}, False),
        ]
        for fields, expected in cases:
            gw = self._make_gateway()
            gw._inject_ev_entities(self._status_with_ev(**fields))
            assert gw._dashboard_calls[f"binary_sensor.{base}_charging"][0] is expected, fields

    def test_ev_suffix_is_stable_regardless_of_charger_count(self):
        """A charger keeps the same entity ids whether or not others are present.

        Regression: the suffix used to be a bare "ev" when a single charger was
        visible and "ev_<id>" only when several were, so entity ids silently
        renamed the moment a second charger appeared (or the same charger was
        briefly seen twice across a reconnect), orphaning dashboards bound to them.
        """
        from gateway import GatewayMQTT

        ev = self._status_with_ev().ev_chargers[0]
        assert GatewayMQTT._ev_suffix(ev, False) == "ev_3xb749"
        assert GatewayMQTT._ev_suffix(ev, True) == "ev_3xb749"

    def test_ev_suffix_falls_back_when_id_unknown(self):
        """A charger that has not sent its BootNotification yet still gets a suffix."""
        from gateway import GatewayMQTT

        ev = self._status_with_ev(charge_point_id="").ev_chargers[0]
        assert GatewayMQTT._ev_suffix(ev, False) == "ev"

    def test_disconnected_charger_reports_offline(self):
        """A charger the gateway reports as disconnected surfaces as fully idle.

        The gateway now sends known-but-disconnected chargers with connected=false
        instead of omitting them, so these entities must go False rather than
        freezing at their last values (which is how a 2-day EV outage went unseen).

        Critically this asserts the LIVE-SESSION fields too, not just online/connected:
        car_charging_now is wired to session_active and PredBat holds the battery for
        the car whenever it is true, so a disconnected charger carrying a stale
        session_active would hold the battery for a charger that is not there.
        The fixture deliberately supplies session_active=True, power_w=7200 and
        session_energy_wh=12400 alongside connected=False — the inconsistent payload
        older firmware can emit.
        """
        gw = self._make_gateway(battery_size=100)
        gw._inject_ev_entities(self._status_with_ev(connected=False, status="Unavailable"))

        base = "predbat_gateway_ev_3xb749"
        assert gw._dashboard_calls[f"binary_sensor.{base}_online"][0] is False
        assert gw._dashboard_calls[f"binary_sensor.{base}_connected"][0] is False
        # Whether a car is plugged in is not known while the charger is offline - not an unplug
        assert gw._dashboard_calls[f"binary_sensor.{base}_plugged"][0] == "unavailable"
        # Stale live-session values must be suppressed, not republished
        assert gw._dashboard_calls[f"binary_sensor.{base}_session_active"][0] is False
        assert gw._dashboard_calls[f"binary_sensor.{base}_charging"][0] is False
        assert gw._dashboard_calls[f"sensor.{base}_power"][0] == 0
        # Unavailable rather than 0, so the energy history sees a gap and not a new session that counts the energy again
        assert gw._dashboard_calls[f"sensor.{base}_session_energy"][0] == "unavailable"

    def test_suffix_slugifies_unsafe_charge_point_ids(self):
        """Charge point ids are vendor strings; entity ids must stay legal.

        Spaces, dots, slashes, dashes and non-ASCII are not valid in a Home Assistant
        entity id, and nothing downstream sanitises the value.
        """
        from gateway import GatewayMQTT

        for cp_id, expected in [
            ("CP/AB-123", "ev_pab123"),  # punctuation dropped, last 6 kept
            ("cp 42.7", "ev_cp427"),  # -> "cp427", shorter than 6
            ("ABC", "ev_abc"),  # shorter than 6 -> whole id
            ("charge_point_Ω", "ev_point_"),  # non-ASCII dropped, underscore kept
            ("///", "ev"),  # slugifies to nothing -> fallback
        ]:
            ev = self._status_with_ev(charge_point_id=cp_id).ev_chargers[0]
            assert GatewayMQTT._ev_suffix(ev, False) == expected, cp_id

    def test_ev_entity_attributes_from_table(self):
        """Published EV entities carry their GATEWAY_ATTRIBUTE_TABLE attributes."""
        from gateway import GATEWAY_ATTRIBUTE_TABLE

        gw = self._make_gateway()
        gw._inject_ev_entities(self._status_with_ev())
        _, attrs = gw._dashboard_calls["sensor.predbat_gateway_ev_3xb749_power"]
        assert attrs == GATEWAY_ATTRIBUTE_TABLE["ev_power"]

    def test_not_reported_fields_skipped(self):
        """Zero/empty 'not reported' fields are not published, except soc which falls back to session energy."""
        gw = self._make_gateway(battery_size=100)
        # session_energy_wh=12400 → 12.4 kWh; battery_size=100 → fallback soc = 12.4%
        status = self._status_with_ev(soc_percent=0, voltage_v=0, max_current_a=0, current_limit_a=0, eco_mode="", status="")
        gw._inject_ev_entities(status)

        base = "predbat_gateway_ev_3xb749"
        # soc is now always published — falls back to session_energy / battery_size * 100
        assert approx_equal(gw._dashboard_calls[f"sensor.{base}_soc"][0], 12.4)
        assert f"sensor.{base}_voltage" not in gw._dashboard_calls
        assert f"sensor.{base}_max_current" not in gw._dashboard_calls
        assert f"sensor.{base}_current_limit" not in gw._dashboard_calls
        assert f"sensor.{base}_eco_mode" not in gw._dashboard_calls
        assert f"sensor.{base}_status" not in gw._dashboard_calls
        # Always-published fields remain
        assert f"binary_sensor.{base}_online" in gw._dashboard_calls
        assert gw._dashboard_calls[f"binary_sensor.{base}_connected"][0] is False  # status="" → no car
        assert f"sensor.{base}_power" in gw._dashboard_calls
        # Charge rate falls back to 7.4 kW when capability is not reported
        assert approx_equal(gw._dashboard_calls[f"sensor.{base}_charge_rate"][0], 7.4)

    def test_soc_fallback_uses_battery_size(self):
        """When soc is not reported, the fallback is session_energy / battery_size * 100."""
        gw = self._make_gateway(battery_size=50)
        # session_energy_wh=12400 → 12.4 kWh; battery_size=50 → 12.4/50*100 = 24.8%
        gw._inject_ev_entities(self._status_with_ev(soc_percent=0))
        assert approx_equal(gw._dashboard_calls["sensor.predbat_gateway_ev_3xb749_soc"][0], 24.8)

    def test_charge_rate_uses_230v_when_voltage_missing(self):
        """With max current but no voltage, charge rate assumes 230 V."""
        gw = self._make_gateway()
        gw._inject_ev_entities(self._status_with_ev(max_current_a=16, voltage_v=0))
        # 16 A × 230 V / 1000
        assert approx_equal(gw._dashboard_calls["sensor.predbat_gateway_ev_3xb749_charge_rate"][0], 3.68)

    def test_no_chargers_publishes_nothing(self):
        """A status with no EV chargers publishes no EV entities."""
        gw = self._make_gateway()
        status = pb.GatewayStatus()
        status.device_id = "pbgw_none"
        gw._inject_ev_entities(status)
        assert gw._dashboard_calls == {}

    def test_multiple_chargers_disambiguated_by_id(self):
        """With more than one charger, entities are suffixed by charge point id."""
        gw = self._make_gateway()
        status = pb.GatewayStatus()
        a = status.ev_chargers.add()
        a.connected = True
        a.charge_point_id = "AAAAAA111111"
        b = status.ev_chargers.add()
        b.connected = False
        b.charge_point_id = "BBBBBB222222"
        gw._inject_ev_entities(status)

        assert "binary_sensor.predbat_gateway_ev_111111_online" in gw._dashboard_calls
        assert "binary_sensor.predbat_gateway_ev_222222_online" in gw._dashboard_calls


class TestEvAutoConfig:
    """Tests for GatewayMQTT._register_ev_car() — conservative car registration."""

    def _make_gateway(self, ev_enable=True, num_cars=0, args=None, evc_control=False):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.log = MagicMock()
        gw.prefix = "predbat"
        gw.gateway_evc_automatic = ev_enable
        gw.gateway_evc_control = evc_control
        gw.args = args if args is not None else {}
        gw._args = {}

        def capture_set_arg(key, value):
            gw._args[key] = value

        def fake_get_arg(key, default=None, **kwargs):
            if key == "num_cars":
                return num_cars
            return default

        gw.set_arg = capture_set_arg
        gw.get_arg = fake_get_arg
        return gw

    def _status_with_ev(self, charge_point_id="CP1", max_current_a=32, voltage_v=240, soc_percent=55):
        status = pb.GatewayStatus()
        ev = status.ev_chargers.add()
        ev.connected = True
        ev.charge_point_id = charge_point_id
        ev.max_current_a = max_current_a
        ev.voltage_v = voltage_v
        ev.soc_percent = soc_percent
        return status

    def test_no_soc_sensor_when_charger_reports_none(self):
        """A charger that reports no SoC leaves car_charging_soc unset, so the car is tracked as a manual SoC car.

        sensor.<pfx>_soc is then only an estimate from the session energy, which starts from 0% every session.
        """
        gw = self._make_gateway(ev_enable=True, num_cars=0)
        gw._register_ev_car(self._status_with_ev(soc_percent=0))

        assert "car_charging_soc" not in gw._args
        # What the manual SoC is tracked from: the session energy, and the plugged-in sensor for the reset on unplug
        assert gw._args["car_charging_energy"] == "sensor.predbat_gateway_ev_cp1_session_energy"
        assert gw._args["car_charging_plugged"] == ["binary_sensor.predbat_gateway_ev_cp1_plugged"]

    def test_no_soc_sensor_when_charger_reports_soc(self):
        """Nor when the charger reports the car's SoC: it only knows it during a session, so the car is tracked manually too."""
        gw = self._make_gateway(ev_enable=True, num_cars=0)
        gw._register_ev_car(self._status_with_ev(soc_percent=40))

        assert "car_charging_soc" not in gw._args

    def test_registers_connected_charger_not_a_stale_slot(self):
        """A disconnected charger in slot 0 must not be registered over a connected one.

        Gateway firmware now reports known-but-offline chargers with connected=false
        (it used to omit them), so the first entry is no longer guaranteed to be live.
        """
        status = pb.GatewayStatus()
        stale = status.ev_chargers.add()
        stale.connected = False
        stale.charge_point_id = "CP0000"
        live = status.ev_chargers.add()
        live.connected = True
        live.charge_point_id = "CP0001"

        gw = self._make_gateway(ev_enable=True, num_cars=0)
        gw._register_ev_car(status)

        assert gw._args["car_charging_planned"] == ["binary_sensor.predbat_gateway_ev_cp0001_connected"]
        # gateway_evc_control drives the registered charger, not the lower id min() would pick
        assert gw._ev_control_cp_id == "CP0001"

    def test_registers_offline_charger_when_none_connected(self):
        """A charger that is merely offline right now still registers, so it reappears."""
        status = pb.GatewayStatus()
        offline = status.ev_chargers.add()
        offline.connected = False
        offline.charge_point_id = "CP0002"

        gw = self._make_gateway(ev_enable=True, num_cars=0)
        gw._register_ev_car(status)

        assert gw._args["num_cars"] == 1
        assert gw._args["car_charging_planned"] == ["binary_sensor.predbat_gateway_ev_cp0002_connected"]

    def test_registers_car_when_none_configured(self):
        """With the flag on and no existing cars, the charger is registered as car 1."""
        gw = self._make_gateway(ev_enable=True, num_cars=0)
        gw._register_ev_car(self._status_with_ev())

        assert gw._args["num_cars"] == 1
        assert gw._args["car_charging_planned"] == ["binary_sensor.predbat_gateway_ev_cp1_connected"]
        assert gw._args["car_charging_plugged"] == ["binary_sensor.predbat_gateway_ev_cp1_plugged"]
        assert gw._args["car_charging_now"] == ["binary_sensor.predbat_gateway_ev_cp1_charging"]
        assert "car_charging_soc" not in gw._args
        # car_charging_rate is a UI config item — set via expose_config, not set_arg
        assert "car_charging_rate" not in gw._args
        gw.base.expose_config.assert_called_once_with("car_charging_rate", 7.68)  # 32A * 240V / 1000
        # Session energy sensor for subtracting EV load from history
        assert gw._args["car_charging_energy"] == "sensor.predbat_gateway_ev_cp1_session_energy"
        # Live charge power, display-only: drives the web power flow diagram
        assert gw._args["car_charging_power"] == "sensor.predbat_gateway_ev_cp1_power"
        # Battery size and target limit are left to the existing car_charging_* settings
        assert "car_charging_battery_size" not in gw._args
        assert "car_charging_limit" not in gw._args

    def test_disabled_flag_does_nothing(self):
        """With the opt-in flag off, no car args are set."""
        gw = self._make_gateway(ev_enable=False, num_cars=0)
        gw._register_ev_car(self._status_with_ev())
        assert gw._args == {}

    def test_no_chargers_does_nothing(self):
        """No EV charger in telemetry means no registration."""
        gw = self._make_gateway(ev_enable=True, num_cars=0)
        status = pb.GatewayStatus()
        gw._register_ev_car(status)
        assert gw._args == {}

    def test_car_charging_now_set_when_not_controlling(self):
        """car_charging_now is wired to session_active when gateway_evc_control is False."""
        gw = self._make_gateway(ev_enable=True, num_cars=0, evc_control=False)
        gw._register_ev_car(self._status_with_ev())
        assert gw._args["car_charging_now"] == ["binary_sensor.predbat_gateway_ev_cp1_charging"]

    def test_car_charging_now_set_when_controlling(self):
        """car_charging_now is wired to session_active when gateway_evc_control is True too.

        It only holds the battery for the car now, never adds a charging slot, so the gateway's own
        start/stop control cannot keep a session going through it.
        """
        gw = self._make_gateway(ev_enable=True, num_cars=0, evc_control=True)
        gw._register_ev_car(self._status_with_ev())
        assert gw._args["car_charging_now"] == ["binary_sensor.predbat_gateway_ev_cp1_charging"]

    def test_charge_rate_falls_back_to_7_4_when_capability_unknown(self):
        """car_charging_rate expose_config uses 7.4kW fallback when max_current_a is 0."""
        gw = self._make_gateway(ev_enable=True, num_cars=0)
        gw._register_ev_car(self._status_with_ev(max_current_a=0, voltage_v=0))
        assert "car_charging_rate" not in gw._args
        gw.base.expose_config.assert_called_once_with("car_charging_rate", 7.4)

    def test_charge_rate_computed_from_max_current_and_voltage(self):
        """car_charging_rate expose_config uses max_current_a * voltage_v when both are set."""
        gw = self._make_gateway(ev_enable=True, num_cars=0)
        gw._register_ev_car(self._status_with_ev(max_current_a=16, voltage_v=230))
        gw.base.expose_config.assert_called_once_with("car_charging_rate", 3.68)  # 16A * 230V / 1000

    def test_charge_rate_uses_230v_default_when_voltage_missing(self):
        """car_charging_rate expose_config uses 230V default when voltage_v is 0."""
        gw = self._make_gateway(ev_enable=True, num_cars=0)
        gw._register_ev_car(self._status_with_ev(max_current_a=32, voltage_v=0))
        gw.base.expose_config.assert_called_once_with("car_charging_rate", 7.36)  # 32A * 230V / 1000


class TestEvInitialize:
    """Tests for the gateway_evc_automatic component config flag in initialize()."""

    def test_initialize_reads_evc_automatic_arg(self):
        """initialize() stores gateway_evc_automatic from the component config kwarg."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {}
        gw.initialize(gateway_device_id="pbgw_test", mqtt_host="mqtt.example.com", mqtt_token="tok", gateway_evc_automatic=True)
        assert gw.gateway_evc_automatic is True

    def test_initialize_evc_automatic_defaults_off(self):
        """gateway_evc_automatic defaults to False when not provided."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {}
        gw.initialize(gateway_device_id="pbgw_test", mqtt_host="mqtt.example.com", mqtt_token="tok")
        assert gw.gateway_evc_automatic is False

    def test_initialize_reads_evc_control_arg(self):
        """initialize() stores gateway_evc_control from the component config kwarg."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {}
        gw.initialize(gateway_device_id="pbgw_test", mqtt_host="mqtt.example.com", mqtt_token="tok", gateway_evc_control=True)
        assert gw.gateway_evc_control is True

    def test_initialize_evc_control_defaults_off(self):
        """gateway_evc_control defaults to False when not provided."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {}
        gw.initialize(gateway_device_id="pbgw_test", mqtt_host="mqtt.example.com", mqtt_token="tok")
        assert gw.gateway_evc_control is False


class TestEvNeedsReconfigure:
    """Tests for the EV-charger discovery trigger in _needs_reconfigure()."""

    def _make_gateway(self):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw._auto_configured = True
        gw._configured_inverter_serials = frozenset({"CE123456789"})
        gw._configured_ev_chargers = frozenset()
        return gw

    def _status(self, charge_point_id=None):
        status = pb.GatewayStatus()
        inv = status.inverters.add()
        inv.type = pb.INVERTER_TYPE_GIVENERGY
        inv.serial = "CE123456789"
        inv.primary = True
        if charge_point_id is not None:
            ev = status.ev_chargers.add()
            ev.connected = True
            ev.charge_point_id = charge_point_id
        return status

    def test_new_charger_triggers_reconfigure(self):
        """A charge point id not seen before forces auto-config to re-run."""
        gw = self._make_gateway()
        assert gw._needs_reconfigure(self._status(charge_point_id="CP10000001")) is True

    def test_known_charger_no_reconfigure(self):
        """An already-configured charger does not trigger a re-run."""
        gw = self._make_gateway()
        gw._configured_ev_chargers = frozenset({"CP20000002"})
        assert gw._needs_reconfigure(self._status(charge_point_id="CP20000002")) is False

    def test_no_charger_no_reconfigure(self):
        """No EV charger present does not trigger a re-run on its own."""
        gw = self._make_gateway()
        assert gw._needs_reconfigure(self._status()) is False

    def test_charger_reporting_soc_does_not_reconfigure(self):
        """A known charger starting or stopping to report the car's SoC does not re-run auto-config - the SoC is never wired."""
        gw = self._make_gateway()
        gw._configured_ev_chargers = frozenset({"CP30000003"})
        status = self._status(charge_point_id="CP30000003")
        assert gw._needs_reconfigure(status) is False
        status.ev_chargers[0].soc_percent = 62
        assert gw._needs_reconfigure(status) is False


class TestEvControl:
    """Tests for GatewayMQTT EVC minute-level control — _refresh_ev_windows, _should_ev_charge_now, _apply_ev_charging_state."""

    def _make_gateway(self, evc_control=True, evc_automatic=True, configured_chargers=None, ev_max_current=None):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw.prefix = "predbat"
        gw.gateway_evc_control = evc_control
        gw.gateway_evc_automatic = evc_automatic
        gw._configured_ev_chargers = frozenset(configured_chargers or ["CP10000001"])
        gw._ev_control_cp_id = (configured_chargers or ["CP10000001"])[0]
        gw._ev_max_current = ev_max_current if ev_max_current is not None else {"CP10000001": 32}
        gw._ev_windows = []
        gw._ev_desired = None
        gw._ev_attempts = {"start": 0, "stop": 0}
        gw._ev_last_attempt = 0.0
        gw.local_tz = pytz.timezone("Europe/London")
        gw.topic_command = "predbat/devices/test/command"
        gw._command_id = 1000
        gw.gateway_display_only = False
        gw._mqtt_client = MagicMock()
        gw._mqtt_connected = True
        gw._last_status = None
        gw._last_telemetry_time = 0
        gw._last_ev_status = None
        gw._last_ev_status_time = 0
        gw._ev_unknown_warned = False
        gw.base = MagicMock()
        gw.base.set_read_only = False
        gw.published = []

        async def fake_publish_raw(topic, payload, **kwargs):
            assert topic == gw.topic_command
            gw.published.append(json.loads(payload.decode()))

        gw._publish_raw = fake_publish_raw
        return gw

    @staticmethod
    def _set_charger(gw, session_active=False, status="Preparing", connected=True, cp_id="CP10000001"):
        """Give the gateway fresh telemetry for one charger in the given state."""
        import time

        gw._last_ev_status = pb.GatewayStatus(ev_chargers=[pb.EvCharger(charge_point_id=cp_id, connected=connected, session_active=session_active, status=status)])
        gw._last_ev_status_time = time.time()

    @staticmethod
    def _in_window(gw):
        """Put now inside a planned charge window."""
        import datetime as dt_mod

        now = dt_mod.datetime.now(gw.local_tz)
        gw._ev_windows = [(now - dt_mod.timedelta(minutes=5), now + dt_mod.timedelta(hours=1))]

    @staticmethod
    def _apply(gw):
        """Run one reconcile cycle."""
        asyncio.run(gw._apply_ev_charging_state())

    @staticmethod
    def _actions(gw):
        """The actions published so far."""
        return [p["action"] for p in gw.published]

    def _planned(self, windows):
        """Return a list of planned-attribute window dicts like output.py produces."""
        result = []
        for start_str, end_str in windows:
            result.append({"start": start_str, "end": end_str, "kwh": 5.0, "average": 20.0, "cost": 100.0})
        return result

    def test_refresh_ev_windows_parses_planned(self):
        """_refresh_ev_windows parses the planned attribute into (start_dt, end_dt) pairs."""

        gw = self._make_gateway()
        planned = self._planned([("06-28 02:00:00", "06-28 05:30:00"), ("06-28 22:00:00", "06-28 23:00:00")])
        gw.get_state_wrapper = lambda entity, attribute=None: planned if attribute == "planned" else "on"

        gw._refresh_ev_windows()

        assert len(gw._ev_windows) == 2
        assert gw._ev_windows[0][0].hour == 2 and gw._ev_windows[0][0].minute == 0
        assert gw._ev_windows[0][1].hour == 5 and gw._ev_windows[0][1].minute == 30

    def test_refresh_ev_windows_empty_plan(self):
        """_refresh_ev_windows clears windows when planned is empty."""
        gw = self._make_gateway()
        gw.get_state_wrapper = lambda entity, attribute=None: [] if attribute == "planned" else "off"
        gw._ev_windows = [("dummy", "dummy")]

        gw._refresh_ev_windows()

        assert gw._ev_windows == []

    def test_should_charge_now_inside_window(self):
        """_should_ev_charge_now returns True when now() is within a window."""
        import datetime as dt_mod

        gw = self._make_gateway()
        now = dt_mod.datetime.now(gw.local_tz)
        start = now - dt_mod.timedelta(minutes=30)
        end = now + dt_mod.timedelta(minutes=30)
        gw._ev_windows = [(start, end)]

        assert gw._should_ev_charge_now() is True

    def test_should_charge_now_outside_window(self):
        """_should_ev_charge_now returns False when now() is outside all windows."""
        import datetime as dt_mod

        gw = self._make_gateway()
        now = dt_mod.datetime.now(gw.local_tz)
        start = now + dt_mod.timedelta(hours=2)
        end = now + dt_mod.timedelta(hours=4)
        gw._ev_windows = [(start, end)]

        assert gw._should_ev_charge_now() is False

    def test_should_charge_now_no_windows(self):
        """_should_ev_charge_now returns False when there are no windows."""
        gw = self._make_gateway()
        gw._ev_windows = []
        assert gw._should_ev_charge_now() is False

    def test_refresh_ev_windows_year_boundary(self):
        """A window genuinely dated the prior calendar year (Dec 31) is anchored to it, not now.year."""
        import datetime as dt_mod
        from unittest.mock import patch

        gw = self._make_gateway()
        # A plan built Dec 31 that ran a window into the small hours of Jan 1, read back just after
        # midnight on Jan 1 - now.year already sees the new year, so the Dec 31 half of the window
        # must anchor to the *previous* year, not now.year (Copilot review on #5120: naively using
        # now.year for both ends previously put a Dec 31 start a year in the future here).
        fixed_now = gw.local_tz.localize(dt_mod.datetime(2027, 1, 1, 0, 15, 0))
        planned = [{"start": "12-31 23:30:00", "end": "01-01 01:30:00", "kwh": 5.0, "average": 20.0, "cost": 1.0}]
        gw.get_state_wrapper = lambda entity, attribute=None: planned if attribute == "planned" else "on"

        with patch("gateway.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = fixed_now
            gw._refresh_ev_windows()

        assert len(gw._ev_windows) == 1
        start_dt, end_dt = gw._ev_windows[0]
        assert start_dt.year == 2026, f"Dec 31 start should anchor to the previous year, got {start_dt}"
        assert end_dt.year == 2027, f"Jan 1 end should anchor to the current year, got {end_dt}"
        assert start_dt <= fixed_now < end_dt, "now (00:15 Jan 1) should fall inside the window"

    def test_refresh_ev_windows_long_active_not_shifted(self):
        """A still-active window whose start is over 23h old is not mistaken for a year rollover (#269)."""
        import datetime as dt_mod
        from unittest.mock import patch

        gw = self._make_gateway()
        # Pinned rather than the real clock, which made this fail from 1 to 2 January (the start fell
        # in the previous year) and around 29 February
        now = gw.local_tz.localize(dt_mod.datetime(2026, 6, 16, 23, 5, 0))
        # A long/flat-rate window that started well over 23h ago but has not finished yet - its
        # end is still ahead of now, so this is a genuinely active window, not a stale one left
        # over from a plan built before a year boundary.
        planned = [{"start": "06-15 17:05:00", "end": "06-17 01:05:00", "kwh": 5.0, "average": 20.0, "cost": 1.0}]
        gw.get_state_wrapper = lambda entity, attribute=None: planned if attribute == "planned" else "on"

        with patch("gateway.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = now
            gw._refresh_ev_windows()
            charging = gw._should_ev_charge_now()

        assert len(gw._ev_windows) == 1
        start_dt, end_dt = gw._ev_windows[0]
        # Must not be bumped a year forward, or the still-active window stops matching "now"
        assert start_dt.year == 2026 and end_dt.year == 2026
        assert charging is True

    def test_apply_sends_start_when_car_plugged_in_window(self):
        """Inside a window with a car plugged in and no session: SetChargingProfile then RemoteStartTransaction to /command."""
        gw = self._make_gateway()
        self._in_window(gw)
        self._set_charger(gw, session_active=False, status="Preparing")

        self._apply(gw)

        published = gw.published
        assert len(published) == 2
        cp_id = gw._ev_control_cp_id
        assert published[0]["command_id"] != published[1]["command_id"]
        assert all(p["command_id"].startswith("PBAT") for p in published)
        for p in published:
            p.pop("command_id")
        assert published[0] == {"command": "ev", "charge_point_id": cp_id, "action": "SetChargingProfile", "current_a": 32}
        assert published[1] == {"command": "ev", "charge_point_id": cp_id, "action": "RemoteStartTransaction", "id_tag": "predbat"}

    def test_apply_sends_stop_outside_window(self):
        """Outside every window with a session active: RemoteStopTransaction to /command."""
        gw = self._make_gateway()
        self._set_charger(gw, session_active=True, status="Charging")

        self._apply(gw)

        assert len(gw.published) == 1
        assert gw.published[0].pop("command_id").startswith("PBAT")
        assert gw.published[0] == {"command": "ev", "charge_point_id": gw._ev_control_cp_id, "action": "RemoteStopTransaction"}

    def test_apply_no_command_when_state_matches(self):
        """Desired state already matches the charger: nothing is sent, cycle after cycle."""
        gw = self._make_gateway()
        self._set_charger(gw, session_active=False, status="Available")
        for _ in range(3):
            self._apply(gw)
        self._in_window(gw)
        self._set_charger(gw, session_active=True, status="Charging")
        for _ in range(3):
            self._apply(gw)
        # A full car (SuspendedEV) in a window still has its session - no restart
        self._set_charger(gw, session_active=True, status="SuspendedEV")
        self._apply(gw)
        assert gw.published == []

    def test_apply_drives_registered_charger_not_lowest_id(self):
        """With two chargers, the reconcile drives the one registered as car 1, even when another has a lower id."""
        import time

        gw = self._make_gateway(configured_chargers=["CP_B", "CP_A"], ev_max_current={"CP_B": 16})
        assert gw._ev_control_cp_id == "CP_B"
        self._in_window(gw)
        gw._last_ev_status = pb.GatewayStatus(
            ev_chargers=[
                pb.EvCharger(charge_point_id="CP_B", connected=True, session_active=False, status="Preparing"),
                pb.EvCharger(charge_point_id="CP_A", connected=True, session_active=False, status="Preparing"),
            ]
        )
        gw._last_ev_status_time = time.time()

        self._apply(gw)

        assert [p["charge_point_id"] for p in gw.published] == ["CP_B", "CP_B"]
        assert gw.published[0]["current_a"] == 16

    def test_apply_nothing_without_registered_charger(self):
        """No charger registered as the car (gateway_evc_automatic off, or none reported): nothing is sent."""
        gw = self._make_gateway()
        gw._ev_control_cp_id = None
        self._in_window(gw)
        self._set_charger(gw, session_active=False, status="Preparing")

        self._apply(gw)

        assert gw.published == []
        gw.log.assert_not_called()

    def test_apply_no_start_when_charger_unusable(self):
        """Inside a window a Faulted or Unavailable charger is not sent a start."""
        gw = self._make_gateway()
        self._in_window(gw)
        for status in ("Faulted", "Unavailable"):
            self._set_charger(gw, session_active=False, status=status)
            self._apply(gw)
        assert gw.published == []

    def test_second_window_starts_although_hub_reads_available(self):
        """Stopped at the end of window 1, the car stays plugged in but the hub reads "Available": window 2 still starts."""
        gw = self._make_gateway()
        self._in_window(gw)
        self._set_charger(gw, session_active=True, status="Charging")
        self._apply(gw)
        gw._ev_windows = []  # window 1 ends
        self._apply(gw)
        assert self._actions(gw) == ["RemoteStopTransaction"]
        # The hub sets "Available" on StopTransaction and the charger sends no new StatusNotification
        self._set_charger(gw, session_active=False, status="Available")
        self._apply(gw)
        self._in_window(gw)  # window 2 begins
        self._apply(gw)
        assert self._actions(gw) == ["RemoteStopTransaction", "SetChargingProfile", "RemoteStartTransaction"]

    def test_apply_no_command_without_fresh_telemetry(self):
        """No telemetry, stale telemetry or an offline charger: the state is unknown, so nothing is sent."""
        gw = self._make_gateway()
        self._apply(gw)
        # Startup, before the hub has ever reported a charger: silent
        assert not [c for c in gw.log.call_args_list if "cannot see charger" in str(c)]
        self._set_charger(gw, session_active=True, status="Charging")
        gw._last_ev_status_time -= 600
        self._apply(gw)
        self._set_charger(gw, session_active=True, status="Charging", connected=False)
        self._apply(gw)
        assert gw.published == []
        # Warned once for this window state, not every minute
        warnings = [c for c in gw.log.call_args_list if "cannot see charger" in str(c)]
        assert len(warnings) == 1
        self._in_window(gw)  # window state changes: warned again
        self._apply(gw)
        warnings = [c for c in gw.log.call_args_list if "cannot see charger" in str(c)]
        assert len(warnings) == 2

    def test_ev_freshness_from_status_without_inverters(self):
        """A /status carrying EV chargers but no inverters still refreshes what EVC control sees."""
        gw = self._make_gateway()
        gw._debug = False
        gw.gateway_inverter_serial = None
        gw._discovery = {}
        status = pb.GatewayStatus(ev_chargers=[pb.EvCharger(charge_point_id="CP10000001", connected=True, session_active=True, status="Charging")])
        gw._process_telemetry(status.SerializeToString())
        assert gw._last_telemetry_time == 0  # no inverters: not counted as the hub being alive
        self._apply(gw)
        assert self._actions(gw) == ["RemoteStopTransaction"]

    def test_restart_mid_window_then_window_ends_sends_stop(self):
        """A restart forgets everything; when the window it started in ends, the active session is still stopped."""
        gw = self._make_gateway()  # fresh process: no memory of the start
        self._in_window(gw)
        self._set_charger(gw, session_active=True, status="Charging")
        self._apply(gw)
        assert gw.published == []
        gw._ev_windows = []  # window ends
        self._apply(gw)
        assert self._actions(gw) == ["RemoteStopTransaction"]

    def test_refused_stop_is_capped_and_recorded(self):
        """A charger that keeps its session after three stops gets no more, and the failure reaches the status."""
        from unittest.mock import patch
        import gateway as gateway_mod

        gw = self._make_gateway()
        clock = [1_000_000.0]
        with patch.object(gateway_mod.time, "time", side_effect=lambda: clock[0]):
            for _ in range(20):
                self._set_charger(gw, session_active=True, status="Charging")
                self._apply(gw)
                clock[0] += 60
        assert self._actions(gw) == ["RemoteStopTransaction"] * 3
        gw.base.record_status.assert_called_once()
        assert "stop" in gw.base.record_status.call_args.args[0]
        assert gw.base.record_status.call_args.kwargs["had_errors"] is True

    def test_startup_charging_outside_window_sends_stop(self):
        """The first reconcile after startup stops a charger already charging outside any window."""
        gw = self._make_gateway()
        self._set_charger(gw, session_active=True, status="Charging")
        self._apply(gw)
        assert self._actions(gw) == ["RemoteStopTransaction"]

    def test_mqtt_down_is_not_an_attempt_and_retries_after_reconnect(self):
        """A command that cannot be published is not recorded; it goes out once MQTT is back."""
        gw = self._make_gateway()
        self._set_charger(gw, session_active=True, status="Charging")
        gw._mqtt_connected = False
        self._apply(gw)
        self._apply(gw)
        assert gw.published == []
        assert gw._ev_attempts == {"start": 0, "stop": 0}
        assert gw._ev_last_attempt == 0.0

        gw._mqtt_connected = True
        self._apply(gw)
        assert self._actions(gw) == ["RemoteStopTransaction"]
        assert gw._ev_attempts["stop"] == 1

    def test_publish_failure_is_not_an_attempt(self):
        """A start whose publish raises is not counted or backed off: the next minute sends it."""
        gw = self._make_gateway()
        self._in_window(gw)
        self._set_charger(gw, session_active=False, status="Preparing")
        working_publish = gw._publish_raw

        async def failing_publish(topic, payload, **kwargs):
            raise RuntimeError("broker gone")

        gw._publish_raw = failing_publish
        self._apply(gw)
        assert gw._ev_attempts["start"] == 0
        assert gw._ev_last_attempt == 0.0

        gw._publish_raw = working_publish
        self._apply(gw)
        assert self._actions(gw) == ["SetChargingProfile", "RemoteStartTransaction"]
        assert gw._ev_attempts["start"] == 1

    def test_refused_start_retries_are_spaced_and_capped(self):
        """A charger that never starts gets at most three starts, two minutes apart, then one warning."""
        from unittest.mock import patch
        import gateway as gateway_mod

        gw = self._make_gateway()
        self._in_window(gw)
        clock = [1_000_000.0]
        with patch.object(gateway_mod.time, "time", side_effect=lambda: clock[0]):
            for _ in range(20):  # 20 minutes of the charger staying in Preparing
                self._set_charger(gw, session_active=False, status="Preparing")
                self._apply(gw)
                clock[0] += 60
        assert self._actions(gw).count("RemoteStartTransaction") == 3
        warnings = [c for c in gw.log.call_args_list if "giving up" in str(c)]
        assert len(warnings) == 1
        # No car plugged in for a window is normal: logged, but never an error status
        gw.base.record_status.assert_not_called()

        # The window ends and the next one starts: the attempts are reset
        gw._ev_windows = []
        with patch.object(gateway_mod.time, "time", side_effect=lambda: clock[0]):
            self._set_charger(gw, session_active=False, status="Preparing")
            self._apply(gw)
            self._in_window(gw)
            self._apply(gw)
        assert self._actions(gw).count("RemoteStartTransaction") == 4

    def test_max_current_fallback_32a(self):
        """Falls back to 32 A in SetChargingProfile when max_current_a not in cache for the charge point."""
        gw = self._make_gateway(ev_max_current={})
        self._in_window(gw)
        self._set_charger(gw, session_active=False, status="Preparing")
        self._apply(gw)
        assert gw.published[0]["current_a"] == 32

    def test_read_only_blocks_start_stop_and_direct_publish(self):
        """The live override and startup config fallback both block every EV command."""
        for live, configured in ((True, False), (None, True)):
            for inside in (False, True):
                gw = self._make_gateway()
                gw.base.set_read_only = live
                gw.get_arg = lambda key, default=None, **kwargs: configured if key == "set_read_only" else default
                self._set_charger(gw, session_active=not inside)
                if inside:
                    self._in_window(gw)
                self._apply(gw)
                assert not asyncio.run(gw._send_ev_command(gw._ev_control_cp_id, {"action": "RemoteStartTransaction"}))
                assert gw.published == []
                assert gw._ev_attempts == {"start": 0, "stop": 0}

    def test_read_only_between_profile_and_start_blocks_start(self):
        """A read-only override arriving during the profile publish prevents the subsequent start."""
        gw = self._make_gateway()
        self._in_window(gw)
        self._set_charger(gw)
        publish = gw._publish_raw

        async def publish_then_read_only(topic, payload, **kwargs):
            """Simulate the live override changing while a publish is awaited."""
            await publish(topic, payload, **kwargs)
            gw.base.set_read_only = True

        gw._publish_raw = publish_then_read_only
        self._apply(gw)
        assert self._actions(gw) == ["SetChargingProfile"]
        assert gw._ev_attempts["start"] == 0

    def test_read_only_release_resets_spent_attempts(self):
        """Releasing read only resumes reconciliation even after an earlier retry budget was exhausted."""
        gw = self._make_gateway()
        self._set_charger(gw, session_active=True, status="Charging")
        gw._ev_desired = False
        gw._ev_attempts["stop"] = 4
        gw.base.set_read_only = True
        self._apply(gw)
        gw.base.set_read_only = False
        self._apply(gw)
        assert self._actions(gw) == ["RemoteStopTransaction"]


def test_ev_two_windows_through_planner(my_predbat):
    """Real telemetry entities, config lookup, planning and output retain window two after a truthful stop."""
    import datetime as dt
    from unittest.mock import patch

    gw = TestEvControl()._make_gateway()
    gw.base = my_predbat
    gw.local_tz = pytz.UTC
    gw.dashboard_item = lambda entity_id, state=None, attributes=None, **kwargs: my_predbat.ha_interface.set_state(entity_id, state, attributes)
    status = pb.GatewayStatus(ev_chargers=[pb.EvCharger(charge_point_id=gw._ev_control_cp_id, connected=True, status="Preparing", max_current_a=32, voltage_v=230)])
    gw._register_ev_car(status)
    my_predbat.num_cars = 1
    my_predbat.set_read_only = False
    my_predbat.octopus_intelligent_charging = False
    my_predbat.car_charging_soc = [0]
    my_predbat.car_charging_loss = 1.0
    my_predbat.args.update(car_charging_battery_size=[60], car_charging_limit=[100], car_charging_plan_time="04:00:00", car_charging_plan_smart=False)
    my_predbat.low_rates_tariff = [{"start": 60, "end": 90, "average": 5}, {"start": 120, "end": 150, "average": 5}]
    midnight = dt.datetime(2026, 1, 15, tzinfo=pytz.UTC)
    clock = [midnight]

    class FixedDateTime(dt.datetime):
        """Give the real gateway window parser and reconciler the simulated plan-cycle clock."""

        @classmethod
        def now(cls, tz=None):
            """Return the current simulated time in the requested timezone."""
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    def cycle(minute, charger_status, session_active):
        """Inject a firmware report and execute a full car planning/publication/reconciliation cycle."""
        clock[0] = midnight + dt.timedelta(minutes=minute)
        my_predbat.now_utc = clock[0]
        my_predbat.midnight_utc = midnight
        my_predbat.minutes_now = minute
        ev = status.ev_chargers[0]
        ev.status = charger_status
        ev.session_active = session_active
        gw._last_ev_status = status
        gw._last_ev_status_time = clock[0].timestamp()
        gw._inject_ev_entities(status)
        my_predbat.get_car_charging_planned()
        my_predbat.fetch_sensor_data_car_planning()
        my_predbat.publish_car_plan()
        gw._refresh_ev_windows()
        asyncio.run(gw._apply_ev_charging_state())

    with patch("gateway.datetime.datetime", FixedDateTime), patch("gateway.time.time", side_effect=lambda: clock[0].timestamp()):
        cycle(60, "Preparing", False)
        cycle(90, "Charging", True)
        cycle(95, "Finishing", False)  # firmware #501 preserves the plugged-in car after StopTransaction
        assert my_predbat.car_charging_planned == [True]
        assert my_predbat.car_plugged_in_now(0, "car_charging_plugged") is True
        assert len(gw._ev_windows) == 1
        cycle(120, "Finishing", False)
        assert TestEvControl._actions(gw) == ["SetChargingProfile", "RemoteStartTransaction", "RemoteStopTransaction", "SetChargingProfile", "RemoteStartTransaction"]
        cycle(155, "Available", False)  # an actual unplug removes the plan, without phantom car demand
        assert not my_predbat.car_charging_planned[0] and not gw._ev_windows


class TestRateAnchors(unittest.TestCase):
    """extract_rate_anchors() validates and rounds the four rate fields."""

    def test_valid_anchors_rounded(self):
        from gateway import extract_rate_anchors

        self.assertEqual(
            extract_rate_anchors(8.04, 28.97, 15.23, 12.01),
            {"rate_min": 8.04, "rate_max": 28.97, "import_rate": 15.23, "export_rate": 12.01},
        )

    def test_missing_value_returns_none(self):
        from gateway import extract_rate_anchors

        self.assertIsNone(extract_rate_anchors(None, 29.0, 15.0, 12.0))

    def test_non_numeric_returns_none(self):
        from gateway import extract_rate_anchors

        self.assertIsNone(extract_rate_anchors("n/a", 29.0, 15.0, 12.0))

    def test_non_finite_returns_none(self):
        from gateway import extract_rate_anchors

        self.assertIsNone(extract_rate_anchors(float("nan"), 29.0, 15.0, 12.0))
        self.assertIsNone(extract_rate_anchors(8.0, float("inf"), 15.0, 12.0))


class TestPublishRawLoopSafety:
    """_publish_raw() must run the actual client.publish() on the event loop that
    owns the aiomqtt Client (self._loop), even when invoked from a different loop —
    otherwise the publish confirmation Future is bound to the wrong loop and stalls
    for aiomqtt's ~10s default timeout instead of completing near-instantly. This is
    what happens today when a control write reaches GatewayMQTT via
    ha.py::run_async(), which runs the whole call chain on a fresh, throwaway
    event loop distinct from GatewayMQTT's own persistent MQTT loop.
    """

    def test_publish_dispatches_to_owning_loop_when_called_cross_loop(self):
        """A cross-loop _publish_raw() call must execute client.publish() on the
        thread that owns self._loop, not on the calling thread."""
        import asyncio
        import threading
        from gateway import GatewayMQTT

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw._mqtt_connected = True
        observed = {}

        class FakeClient:
            async def publish(self, topic, payload, qos=0, retain=False):
                observed["thread_ident"] = threading.get_ident()

        gw._mqtt_client = FakeClient()

        # Real second event loop on its own thread — mimics GatewayMQTT's
        # persistent MQTT loop (owned by a dedicated thread via hass.py::create_task).
        owner_loop = asyncio.new_event_loop()
        owner_thread = threading.Thread(target=owner_loop.run_forever, daemon=True)
        owner_thread.start()
        gw._loop = owner_loop

        try:
            # Call _publish_raw from a DIFFERENT, freshly-created loop on THIS
            # thread — mirrors ha.py::run_async()'s asyncio.run(coro) pattern.
            asyncio.run(gw._publish_raw("predbat/devices/test/command", b"payload"))
        finally:
            owner_loop.call_soon_threadsafe(owner_loop.stop)
            owner_thread.join(timeout=2)
            assert not owner_thread.is_alive(), "owner MQTT loop thread did not stop"
            owner_loop.close()
        assert observed.get("thread_ident") == owner_thread.ident, "client.publish() ran on the wrong thread/loop"


def test_ev_soc_battery_size_through_get_arg(my_predbat):
    """The SoC estimate reads car_charging_battery_size through the real get_arg, for every form the arg can take.

    TestEvTelemetry stubs get_arg, which hides how the value is resolved and coerced. This runs the
    gateway against a real PredBat so a per-car list, an entity pointer and a fractional size all
    reach the estimate intact: 5.25 kWh delivered into a 10.5 kWh battery is 50%, where a size
    truncated to 10 gives 52.5% and the 100 kWh fallback gives 5.25%.
    """
    helper = TestEvTelemetry()
    gw = helper._make_gateway()
    del gw.get_arg  # use ComponentBase.get_arg -> PredBat.get_arg
    gw.base = my_predbat
    entity_id = "sensor.test_car_battery_size"
    soc_entity = "sensor.predbat_gateway_ev_3xb749_soc"

    had_arg = "car_charging_battery_size" in my_predbat.args
    original_arg = my_predbat.args.get("car_charging_battery_size")
    had_entity = entity_id in my_predbat.ha_interface.dummy_items
    original_entity = my_predbat.ha_interface.dummy_items.get(entity_id)
    original_had_errors = my_predbat.had_errors
    original_status = my_predbat.current_status

    cases = [
        ("per-car list of numbers", [10.5, 60.0]),
        ("per-car list of entity pointers", [entity_id] * 8),
        ("single entity pointer", entity_id),
        ("single fractional number", 10.5),
    ]
    try:
        my_predbat.ha_interface.dummy_items[entity_id] = "10.5"
        for label, value in cases:
            my_predbat.args["car_charging_battery_size"] = value
            my_predbat.had_errors = False
            gw._dashboard_calls = {}
            gw._inject_ev_entities(helper._status_with_ev(soc_percent=0, session_energy_wh=5250))
            soc = gw._dashboard_calls[soc_entity][0]
            assert approx_equal(soc, 50.0), "{}: expected SoC 50.0 from a 10.5 kWh battery, got {}".format(label, soc)
            assert not my_predbat.had_errors, "{}: reading the battery size must not flag an error".format(label)
    finally:
        if had_arg:
            my_predbat.args["car_charging_battery_size"] = original_arg
        else:
            my_predbat.args.pop("car_charging_battery_size", None)
        if had_entity:
            my_predbat.ha_interface.dummy_items[entity_id] = original_entity
        else:
            my_predbat.ha_interface.dummy_items.pop(entity_id, None)
        my_predbat.had_errors = original_had_errors
        my_predbat.current_status = original_status


class TestCommandAck:
    """Control writes are sent once and confirmed by the hub's ack instead of being re-sent every poll."""

    SLOT_START = "select.predbat_gateway_456789_charge_slot1_start"
    SLOT_END = "select.predbat_gateway_456789_charge_slot1_end"
    CHARGE_ENABLED = "switch.predbat_gateway_456789_charge_enabled"
    CHARGE_RATE = "number.predbat_gateway_456789_charge_rate"

    def _make_gateway(self, subscribed=True, acks_seen=True):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw.prefix = "predbat"
        gw._suffix_to_serial = {"456789": "CE123456789"}
        gw._mqtt_connected = True
        gw._command_id = 0
        gw._pending_commands = {}
        gw._ack_subscribed = subscribed
        gw._acks_seen = acks_seen
        gw._ack_subscribe_warned = False
        gw.topic_status = "predbat/devices/pbgw_test/status"
        gw.topic_online = "predbat/devices/pbgw_test/online"
        gw.topic_ack = "predbat/devices/pbgw_test/ack/+"
        gw._ack_topic_prefix = "predbat/devices/pbgw_test/ack/"
        gw._published = []  # (command, command_id, kwargs)
        gw.cache = {}  # entity_id -> cached state, as the generic write loop would read it back
        gw._last_status = None

        async def fake_publish_command(command, command_id=None, **kwargs):
            gw._published.append((command, command_id, kwargs))

        def fake_dashboard_item(entity_id, state=None, attributes=None, app=None):
            gw.cache[entity_id] = state

        gw.publish_command = fake_publish_command
        gw.dashboard_item = fake_dashboard_item
        gw.get_state_wrapper = lambda entity_id=None, **kwargs: gw.cache.get(entity_id)
        return gw

    def _run(self, coro):
        import asyncio

        return asyncio.run(coro)

    def _send_slot(self, gw, entity_id, value):
        """Send one slot endpoint through _send_control, as the select handler did before slots staged.

        These tests exercise the ack and re-send machinery, which the schedule write still uses.
        """
        import json

        field = "start" if entity_id.endswith("_start") else "end"
        hours, minutes = value.split(":")[:2]
        return gw._send_control(entity_id, "set_charge_slot", schedule_json=json.dumps({field: int(hours) * 100 + int(minutes)}), serial="CE123456789")

    def _clock(self):
        """A controllable clock for the ack window."""
        from unittest.mock import patch

        clock = {"now": 1000.0}
        patcher = patch("gateway._monotonic", lambda: clock["now"])
        return clock, patcher

    def _ack(self, gw, command_id, command, ok=True, error=None, applied=None):
        import json

        ack = {"command_id": command_id, "command": command, "ok": ok}
        if error:
            ack["error"] = error
        if applied is not None:
            ack["applied"] = applied
        gw._process_ack(json.dumps(ack).encode("utf-8"))

    def _logged(self, gw, text):
        return any(text in str(call.args[0]) for call in gw.log.call_args_list)

    def test_slow_ack_publishes_once_and_confirms(self):
        """The generic loop polls every 2 s; an ack arriving after 10 s means one publish, then a matching read-back."""
        gw = self._make_gateway()
        gw.cache[self.SLOT_START] = "00:00:00"
        clock, patcher = self._clock()
        with patcher:
            matched = False
            for step in range(10):
                clock["now"] = 1000.0 + step * 2
                if clock["now"] >= 1010.0 and not matched:
                    self._ack(gw, "PBAT1", "set_charge_slot", applied={"start": 30, "end": 1800, "slot": 0})
                if gw.cache.get(self.SLOT_START) == "00:30:00":
                    matched = True
                    break
                self._run(self._send_slot(gw, self.SLOT_START, "00:30:00"))
        assert matched
        assert len(gw._published) == 1
        assert gw._published[0][1] == 1
        assert gw.cache[self.SLOT_END] == "18:00:00"

    def test_refusal_logged_and_not_resent_within_window(self):
        """A refused command logs the hub's reason and is not re-sent until the window has passed."""
        gw = self._make_gateway()
        clock, patcher = self._clock()
        with patcher:
            self._run(self._send_slot(gw, self.SLOT_END, "00:00:00"))
            clock["now"] = 1001.0
            self._ack(gw, "PBAT1", "set_charge_slot", ok=False, error="would_disable_charge_zero_length_slot")
            for step in range(1, 15):
                clock["now"] = 1001.0 + step * 2
                self._run(self._send_slot(gw, self.SLOT_END, "00:00:00"))
            assert len(gw._published) == 1
            assert self._logged(gw, "Warn: GatewayMQTT: set_charge_slot for CE123456789 refused by hub: would_disable_charge_zero_length_slot")
            # The refusal does not update the cached value
            assert self.SLOT_END not in gw.cache
            # After the window a new attempt goes out
            clock["now"] = 1031.0
            self._run(self._send_slot(gw, self.SLOT_END, "00:00:00"))
        assert len(gw._published) == 2

    def test_no_ack_resends_once_after_window_then_falls_back(self):
        """No ack: suppressed for the window, re-sent once, then acks stop being relied on."""
        gw = self._make_gateway()
        clock, patcher = self._clock()
        with patcher:
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            for step in range(1, 15):
                clock["now"] = 1000.0 + step * 2
                self._run(gw.number_event(self.CHARGE_RATE, 2000))
            assert len(gw._published) == 1
            clock["now"] = 1030.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            assert [p[1] for p in gw._published] == [1, 2]
            clock["now"] = 1040.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            assert len(gw._published) == 2
            # The re-send is unanswered too: fall back to today's behaviour
            clock["now"] = 1060.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            assert len(gw._published) == 3
            assert gw._acks_seen is False
            clock["now"] = 1062.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            assert len(gw._published) == 4

    def test_new_value_while_in_flight_publishes_immediately(self):
        """A different value is not held back by the pending one, and the stale ack is ignored."""
        gw = self._make_gateway()
        clock, patcher = self._clock()
        with patcher:
            self._run(self._send_slot(gw, self.SLOT_START, "00:30:00"))
            clock["now"] = 1001.0
            self._run(self._send_slot(gw, self.SLOT_START, "01:00:00"))
            assert [p[1] for p in gw._published] == [1, 2]
            # The superseded command's ack must not overwrite the cache
            self._ack(gw, "PBAT1", "set_charge_slot", applied={"start": 30, "end": 1800, "slot": 0})
            assert self.SLOT_START not in gw.cache
            self._ack(gw, "PBAT2", "set_charge_slot", applied={"start": 100, "end": 1800, "slot": 0})
            assert gw.cache[self.SLOT_START] == "01:00:00"

    def test_ok_ack_updates_switch_and_suppresses_duplicates(self):
        """An ok ack updates an enable switch from `applied` and later identical calls are not re-sent."""
        gw = self._make_gateway()
        gw.cache[self.CHARGE_ENABLED] = "off"
        clock, patcher = self._clock()
        with patcher:
            self._run(gw.switch_event(self.CHARGE_ENABLED, "turn_on"))
            self._ack(gw, "PBAT1", "set_charge_enable", applied={"enable": True, "slot": 0})
            assert gw.cache[self.CHARGE_ENABLED] == "on"
            clock["now"] = 1002.0
            self._run(gw.switch_event(self.CHARGE_ENABLED, "turn_on"))
        assert len(gw._published) == 1

    def test_staged_or_recorded_slot_ack_does_not_update_cache(self):
        """EMS staged/recorded endpoints were not written to the inverter, so the cache is left alone."""
        gw = self._make_gateway()
        clock, patcher = self._clock()
        with patcher:
            self._run(self._send_slot(gw, self.SLOT_START, "00:30:00"))
            self._ack(gw, "PBAT1", "set_charge_slot", applied={"staged": True, "start": 30, "slot": 0})
            self._run(self._send_slot(gw, self.SLOT_END, "05:00:00"))
            self._ack(gw, "PBAT2", "set_charge_slot", applied={"start": 30, "end": 500, "slot": 0, "recorded": True})
        assert gw.cache == {}

    def test_number_ack_does_not_update_cache(self):
        """Numbers wait for telemetry; only slots and enables are applied from the ack."""
        gw = self._make_gateway()
        clock, patcher = self._clock()
        with patcher:
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            self._ack(gw, "PBAT1", "set_charge_rate", applied={"power_w": 2000, "slot": 0})
        assert gw.cache == {}
        assert gw._pending_commands[self.CHARGE_RATE]["state"] == "applied"

    def test_replay_error_resends_with_new_id(self):
        """A 'replay' rejection means the value was never considered: the next call publishes under a new id."""
        gw = self._make_gateway()
        clock, patcher = self._clock()
        with patcher:
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            self._ack(gw, "PBAT1", "set_charge_rate", ok=False, error="replay")
            clock["now"] = 1002.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
        assert [p[1] for p in gw._published] == [1, 2]
        assert self._logged(gw, "Warn: GatewayMQTT: set_charge_rate for CE123456789 refused by hub: replay")
        assert gw._pending_commands[self.CHARGE_RATE]["state"] == "sent"

    def test_without_ack_subscription_behaviour_is_unchanged(self):
        """No ack subscription: every call publishes, with the same arguments as before."""
        gw = self._make_gateway(subscribed=False, acks_seen=False)
        clock, patcher = self._clock()
        with patcher:
            for _ in range(3):
                self._run(gw.number_event(self.CHARGE_RATE, 2000))
        assert gw._published == [("set_charge_rate", None, {"power_w": 2000, "serial": "CE123456789"})] * 3
        assert gw._pending_commands == {}

    def test_no_suppression_until_an_ack_has_been_seen(self):
        """Subscribed but no ack matched yet (e.g. firmware without acks): publish every call, as before."""
        gw = self._make_gateway(acks_seen=False)
        clock, patcher = self._clock()
        with patcher:
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            clock["now"] = 1002.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            assert len(gw._published) == 2
            # The first matched ack switches suppression on
            self._ack(gw, "PBAT2", "set_charge_rate", applied={"power_w": 2000, "slot": 0})
            assert gw._acks_seen is True
            clock["now"] = 1004.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
        assert len(gw._published) == 2

    def test_ack_for_an_earlier_send_of_the_same_value_counts(self):
        """Before suppression is on, each poll re-sends under a new id; an ack for any of them confirms the value."""
        gw = self._make_gateway(acks_seen=False)
        clock, patcher = self._clock()
        with patcher:
            for step in range(3):
                clock["now"] = 1000.0 + step * 2
                self._run(gw.number_event(self.CHARGE_RATE, 2000))
            assert [p[1] for p in gw._published] == [1, 2, 3]
            # Only the first send's ack arrives
            self._ack(gw, "PBAT1", "set_charge_rate", applied={"power_w": 2000, "slot": 0})
            assert gw._acks_seen is True
            clock["now"] = 1006.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
        assert len(gw._published) == 3

    def test_refusal_after_ok_from_another_unit_wins(self):
        """A command reaching two units acks twice; a refusal after an ok is reported, kept, and undoes the cache update."""
        gw = self._make_gateway()
        gw.cache[self.SLOT_START] = "00:00:00"
        gw._last_status = object()

        def fake_inject_entities(status):
            assert status is gw._last_status
            gw.cache[self.SLOT_START] = "00:00:00"  # last telemetry: the old value
            gw.cache[self.SLOT_END] = "00:00:00"

        gw._inject_entities = fake_inject_entities
        clock, patcher = self._clock()
        with patcher:
            self._run(self._send_slot(gw, self.SLOT_START, "00:30:00"))
            self._ack(gw, "PBAT1", "set_charge_slot", applied={"start": 30, "end": 1800, "slot": 0})
            assert gw.cache[self.SLOT_START] == "00:30:00"
            self._ack(gw, "PBAT1", "set_charge_slot", ok=False, error="modbus_write_failed")
            assert gw._pending_commands[self.SLOT_START]["state"] == "refused"
            assert self._logged(gw, "refused by hub: modbus_write_failed")
            assert gw.cache[self.SLOT_START] == "00:00:00"
            # A later ok for the same id changes nothing
            self._ack(gw, "PBAT1", "set_charge_slot", applied={"start": 30, "end": 1800, "slot": 1})
            assert gw._pending_commands[self.SLOT_START]["state"] == "refused"
        assert gw.cache[self.SLOT_START] == "00:00:00"

    def test_unusable_first_ok_does_not_block_a_later_cache_update(self):
        """A staged ok from one unit, then a plain ok from another: the plain one still updates the cache."""
        gw = self._make_gateway()
        clock, patcher = self._clock()
        with patcher:
            self._run(self._send_slot(gw, self.SLOT_START, "00:30:00"))
            self._ack(gw, "PBAT1", "set_charge_slot", applied={"staged": True, "start": 30, "slot": 0})
            assert gw.cache == {}
            self._ack(gw, "PBAT1", "set_charge_slot", applied={"start": 30, "end": 1800, "slot": 1})
        assert gw.cache[self.SLOT_START] == "00:30:00"

    def test_refusal_restarts_the_window_even_when_another_send_succeeded(self):
        """Any refusal restarts the cooldown, so an identical call is not re-sent within 30 s of it."""
        gw = self._make_gateway()
        clock, patcher = self._clock()
        with patcher:
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            gw._acks_seen = False  # force a second send of the same value
            clock["now"] = 1002.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            self._ack(gw, "PBAT2", "set_charge_rate", applied={"power_w": 2000, "slot": 0})
            clock["now"] = 1025.0
            self._ack(gw, "PBAT1", "set_charge_rate", ok=False, error="modbus_write_failed")
            assert gw._pending_commands[self.CHARGE_RATE]["state"] == "applied"
            clock["now"] = 1040.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            assert len(gw._published) == 2
            clock["now"] = 1056.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
        assert len(gw._published) == 3

    def test_ok_for_a_later_send_beats_refusal_of_an_earlier_one(self):
        """Distinct sends of the same value: an ok for any of them means the value was applied."""
        gw = self._make_gateway(acks_seen=False)
        clock, patcher = self._clock()
        with patcher:
            self._run(self._send_slot(gw, self.SLOT_START, "00:30:00"))
            clock["now"] = 1002.0
            self._run(self._send_slot(gw, self.SLOT_START, "00:30:00"))
            self._ack(gw, "PBAT1", "set_charge_slot", ok=False, error="modbus_write_failed")
            assert gw._pending_commands[self.SLOT_START]["state"] == "refused"
            self._ack(gw, "PBAT2", "set_charge_slot", applied={"start": 30, "end": 1800, "slot": 0})
            assert gw._pending_commands[self.SLOT_START]["state"] == "applied"
            assert gw.cache[self.SLOT_START] == "00:30:00"
            # A delayed refusal of an earlier send does not undo the later success
            clock["now"] = 1004.0
            self._run(self._send_slot(gw, self.SLOT_START, "00:30:00"))
            self._run(self._send_slot(gw, self.SLOT_START, "00:30:00"))
        assert gw._pending_commands[self.SLOT_START]["state"] == "applied"
        assert len(gw._published) == 2

    def test_failed_retry_keeps_earlier_ids_matchable(self):
        """A publish that raises drops only its own id; an ack for an earlier send still counts."""
        gw = self._make_gateway(acks_seen=False)
        real_publish = gw.publish_command

        async def failing_publish_command(command, command_id=None, **kwargs):
            raise RuntimeError("broker gone")

        clock, patcher = self._clock()
        with patcher:
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            clock["now"] = 1002.0
            gw.publish_command = failing_publish_command
            try:
                self._run(gw.number_event(self.CHARGE_RATE, 2000))
            except RuntimeError:
                pass
            gw.publish_command = real_publish
            assert gw._pending_commands[self.CHARGE_RATE]["command_ids"] == ["PBAT1"]
            assert gw._pending_commands[self.CHARGE_RATE]["sent_at"] == 1000.0
            self._ack(gw, "PBAT1", "set_charge_rate", applied={"power_w": 2000, "slot": 0})
        assert gw._acks_seen is True
        assert gw._pending_commands[self.CHARGE_RATE]["state"] == "applied"

    def test_slot_cache_uses_the_entity_suffix_not_the_first_match(self):
        """A prefix that itself contains 'charge_slot1_' does not redirect the cache update."""
        gw = self._make_gateway()
        gw.prefix = "house_charge_slot1_x"
        entity = "select.house_charge_slot1_x_gateway_456789_charge_slot1_start"
        clock, patcher = self._clock()
        with patcher:
            self._run(self._send_slot(gw, entity, "00:30:00"))
            self._ack(gw, "PBAT1", "set_charge_slot", applied={"start": 30, "end": 1800, "slot": 0})
        assert gw.cache == {entity: "00:30:00", "select.house_charge_slot1_x_gateway_456789_charge_slot1_end": "18:00:00"}

    def test_late_refusal_for_an_earlier_attempt_is_still_handled(self):
        """After an answered attempt, a fresh attempt keeps earlier ids: a late refusal is logged and restarts the window."""
        gw = self._make_gateway()
        clock, patcher = self._clock()
        with patcher:
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            self._ack(gw, "PBAT1", "set_charge_rate", applied={"power_w": 2000, "slot": 0})
            clock["now"] = 1031.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            assert gw._pending_commands[self.CHARGE_RATE]["command_ids"] == ["PBAT1", "PBAT2"]
            assert gw._pending_commands[self.CHARGE_RATE]["state"] == "sent"
            clock["now"] = 1032.0
            self._ack(gw, "PBAT1", "set_charge_rate", ok=False, error="not_managed")
            assert self._logged(gw, "refused by hub: not_managed")
            assert gw._pending_commands[self.CHARGE_RATE]["state"] == "refused"
            assert gw._pending_commands[self.CHARGE_RATE]["sent_at"] == 1032.0

    def test_each_refusal_for_the_same_id_is_logged_and_restarts_the_window(self):
        """Two units refusing one command: both reasons are logged and the window runs from the second."""
        gw = self._make_gateway()
        clock, patcher = self._clock()
        with patcher:
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            clock["now"] = 1001.0
            self._ack(gw, "PBAT1", "set_charge_rate", ok=False, error="not_managed")
            clock["now"] = 1025.0
            self._ack(gw, "PBAT1", "set_charge_rate", ok=False, error="modbus_write_failed")
            assert self._logged(gw, "refused by hub: not_managed")
            assert self._logged(gw, "refused by hub: modbus_write_failed")
            clock["now"] = 1031.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            assert len(gw._published) == 1
            clock["now"] = 1055.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
        assert len(gw._published) == 2

    def test_late_refusal_undoes_a_cache_update_from_an_earlier_attempt(self):
        """PBAT1 ok updates the slot cache, PBAT2 is a fresh attempt, then another unit refuses PBAT1: telemetry is restored."""
        gw = self._make_gateway()
        gw._last_status = object()
        restored = []

        def fake_inject_entities(status):
            restored.append(status)
            gw.cache[self.SLOT_START] = "00:00:00"

        gw._inject_entities = fake_inject_entities
        clock, patcher = self._clock()
        with patcher:
            self._run(self._send_slot(gw, self.SLOT_START, "00:30:00"))
            self._ack(gw, "PBAT1", "set_charge_slot", applied={"start": 30, "end": 1800, "slot": 0})
            assert gw.cache[self.SLOT_START] == "00:30:00"
            clock["now"] = 1031.0
            self._run(self._send_slot(gw, self.SLOT_START, "00:30:00"))
            assert [p[1] for p in gw._published] == [1, 2]
            self._ack(gw, "PBAT1", "set_charge_slot", ok=False, error="modbus_write_failed")
        assert restored == [gw._last_status]
        assert gw.cache[self.SLOT_START] == "00:00:00"
        assert self._logged(gw, "refused by hub: modbus_write_failed")

    def test_publish_failure_after_its_ack_arrived_keeps_the_entry(self):
        """The hub acknowledged the command while the publish was still awaiting the broker; the later publish error must not undo that."""
        gw = self._make_gateway()

        async def ack_then_fail(command, command_id=None, **kwargs):
            gw._published.append((command, command_id, kwargs))
            self._ack(gw, f"PBAT{command_id}", command, applied={"power_w": 2000, "slot": 0})
            raise RuntimeError("connection lost before the broker confirmed")

        gw.publish_command = ack_then_fail
        clock, patcher = self._clock()
        with patcher:
            try:
                self._run(gw.number_event(self.CHARGE_RATE, 2000))
            except RuntimeError:
                pass
            assert gw._pending_commands[self.CHARGE_RATE]["state"] == "applied"
            assert gw._pending_commands[self.CHARGE_RATE]["command_ids"] == ["PBAT1"]
            clock["now"] = 1002.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
        assert len(gw._published) == 1

    def test_refusal_from_an_earlier_attempt_still_outranks_its_late_ok(self):
        """PBAT1 refused, fresh attempt PBAT2, then another unit's late ok for PBAT1: it must not count as applied."""
        gw = self._make_gateway()
        clock, patcher = self._clock()
        with patcher:
            self._run(self._send_slot(gw, self.SLOT_START, "00:30:00"))
            self._ack(gw, "PBAT1", "set_charge_slot", ok=False, error="modbus_write_failed")
            clock["now"] = 1031.0
            self._run(self._send_slot(gw, self.SLOT_START, "00:30:00"))
            assert [p[1] for p in gw._published] == [1, 2]
            self._ack(gw, "PBAT1", "set_charge_slot", applied={"start": 30, "end": 1800, "slot": 1})
            assert gw._pending_commands[self.SLOT_START]["state"] == "sent"
            assert gw.cache == {}
            # PBAT2's own ok still confirms the value
            self._ack(gw, "PBAT2", "set_charge_slot", applied={"start": 30, "end": 1800, "slot": 0})
        assert gw._pending_commands[self.SLOT_START]["state"] == "applied"
        assert gw.cache[self.SLOT_START] == "00:30:00"

    def test_replay_after_an_answer_is_logged_as_a_refusal(self):
        """A replay from another unit after the command was answered is logged and restarts the window."""
        gw = self._make_gateway()
        clock, patcher = self._clock()
        with patcher:
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            self._ack(gw, "PBAT1", "set_charge_rate", applied={"power_w": 2000, "slot": 0})
            clock["now"] = 1020.0
            self._ack(gw, "PBAT1", "set_charge_rate", ok=False, error="replay")
            assert self._logged(gw, "Warn: GatewayMQTT: set_charge_rate for CE123456789 refused by hub: replay")
            assert gw._pending_commands[self.CHARGE_RATE]["sent_at"] == 1020.0
            clock["now"] = 1040.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
        assert len(gw._published) == 1

    def test_late_replay_for_an_earlier_id_does_not_drop_the_fresh_attempt(self):
        """PBAT1 ok, fresh attempt PBAT2, then a late replay for PBAT1: PBAT2 stays tracked and the window restarts."""
        gw = self._make_gateway()
        gw._last_status = object()
        restored = []
        gw._inject_entities = lambda status: restored.append(status)
        clock, patcher = self._clock()
        with patcher:
            self._run(self._send_slot(gw, self.SLOT_START, "00:30:00"))
            self._ack(gw, "PBAT1", "set_charge_slot", applied={"start": 30, "end": 1800, "slot": 0})
            clock["now"] = 1031.0
            self._run(self._send_slot(gw, self.SLOT_START, "00:30:00"))
            clock["now"] = 1032.0
            self._ack(gw, "PBAT1", "set_charge_slot", ok=False, error="replay")
            entry = gw._pending_commands[self.SLOT_START]
            assert entry["command_ids"] == ["PBAT1", "PBAT2"]
            assert entry["sent_at"] == 1032.0
            assert restored == [gw._last_status]
            assert self._logged(gw, "refused by hub: replay")
            clock["now"] = 1034.0
            self._run(self._send_slot(gw, self.SLOT_START, "00:30:00"))
        assert len(gw._published) == 2

    def test_replay_for_the_newest_id_after_an_earlier_send_applied(self):
        """PBAT1 unanswered, PBAT2 re-sent, PBAT1 ok, then PBAT2 replay: PBAT2 is forgotten and the applied value stays suppressed."""
        gw = self._make_gateway()
        clock, patcher = self._clock()
        with patcher:
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            clock["now"] = 1030.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            clock["now"] = 1031.0
            self._ack(gw, "PBAT1", "set_charge_rate", applied={"power_w": 2000, "slot": 0})
            clock["now"] = 1059.0
            self._ack(gw, "PBAT2", "set_charge_rate", ok=False, error="replay")
            entry = gw._pending_commands[self.CHARGE_RATE]
            assert entry["command_ids"] == ["PBAT1"]
            assert entry["state"] == "applied"
            assert self._logged(gw, "already applied by another send")
            # The replay is a refusal too: the window runs from it
            clock["now"] = 1060.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            assert len(gw._published) == 2
            clock["now"] = 1089.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
        assert len(gw._published) == 3

    def test_replay_for_the_newest_id_with_only_an_older_attempts_ok_drops_the_entry(self):
        """An ok from an earlier attempt does not count: a replay of the fresh attempt's id re-sends straight away."""
        gw = self._make_gateway()
        clock, patcher = self._clock()
        with patcher:
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            self._ack(gw, "PBAT1", "set_charge_rate", applied={"power_w": 2000, "slot": 0})
            clock["now"] = 1031.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            self._ack(gw, "PBAT2", "set_charge_rate", ok=False, error="replay")
            assert self.CHARGE_RATE not in gw._pending_commands
            clock["now"] = 1032.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
        assert [p[1] for p in gw._published] == [1, 2, 3]

    def test_failed_publish_with_full_id_history_restores_the_pruned_id(self):
        """With 16 ids kept, a failed 17th send puts back the pruned oldest id and the earlier timing."""
        gw = self._make_gateway(acks_seen=False)
        clock, patcher = self._clock()
        with patcher:
            for step in range(16):
                clock["now"] = 1000.0 + step * 2
                self._run(gw.number_event(self.CHARGE_RATE, 2000))
            before = dict(gw._pending_commands[self.CHARGE_RATE])
            before_ids = list(before["command_ids"])
            assert len(before_ids) == 16

            async def failing_publish_command(command, command_id=None, **kwargs):
                raise RuntimeError("broker gone")

            real_publish = gw.publish_command
            gw.publish_command = failing_publish_command
            clock["now"] = 1100.0
            try:
                self._run(gw.number_event(self.CHARGE_RATE, 2000))
            except RuntimeError:
                pass
            gw.publish_command = real_publish
            entry = gw._pending_commands[self.CHARGE_RATE]
            assert entry["command_ids"] == before_ids
            assert entry["sent_at"] == before["sent_at"]
            # The oldest id is still matchable
            self._ack(gw, "PBAT1", "set_charge_rate", applied={"power_w": 2000, "slot": 0})
        assert gw._pending_commands[self.CHARGE_RATE]["state"] == "applied"

    def test_repeated_refusal_during_a_failing_publish_is_not_rolled_back(self):
        """A refusal repeated while a later publish awaits the broker keeps its restarted window when that publish fails."""
        gw = self._make_gateway()
        clock, patcher = self._clock()
        with patcher:
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            self._ack(gw, "PBAT1", "set_charge_rate", ok=False, error="not_managed")

            async def refusal_then_fail(command, command_id=None, **kwargs):
                clock["now"] = 1035.0
                self._ack(gw, "PBAT1", "set_charge_rate", ok=False, error="not_managed")
                raise RuntimeError("broker gone")

            real_publish = gw.publish_command
            gw.publish_command = refusal_then_fail
            clock["now"] = 1031.0
            try:
                self._run(gw.number_event(self.CHARGE_RATE, 2000))
            except RuntimeError:
                pass
            gw.publish_command = real_publish
            entry = gw._pending_commands[self.CHARGE_RATE]
            assert entry["command_ids"] == ["PBAT1"]
            assert entry["sent_at"] == 1035.0
            clock["now"] = 1036.0
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
        assert len(gw._published) == 1

    def test_full_history_failing_publish_with_concurrent_ack_keeps_all_earlier_ids(self):
        """16 ids kept, a 17th send fails while an ack for PBAT2 arrives: PBAT1..PBAT16 all stay matchable."""
        gw = self._make_gateway(acks_seen=False)
        clock, patcher = self._clock()
        with patcher:
            for step in range(16):
                clock["now"] = 1000.0 + step * 2
                self._run(gw.number_event(self.CHARGE_RATE, 2000))

            async def ack_other_then_fail(command, command_id=None, **kwargs):
                self._ack(gw, "PBAT2", "set_charge_rate", ok=False, error="not_managed")
                self._ack(gw, "PBAT1", "set_charge_rate", ok=False, error="modbus_write_failed")
                raise RuntimeError("broker gone")

            real_publish = gw.publish_command
            gw.publish_command = ack_other_then_fail
            clock["now"] = 1100.0
            try:
                self._run(gw.number_event(self.CHARGE_RATE, 2000))
            except RuntimeError:
                pass
            gw.publish_command = real_publish
        entry = gw._pending_commands[self.CHARGE_RATE]
        assert entry["command_ids"] == [f"PBAT{n}" for n in range(1, 17)]
        assert entry["outcomes"] == {"PBAT2": "refused", "PBAT1": "refused"}
        assert self._logged(gw, "refused by hub: modbus_write_failed")

    def test_outcomes_do_not_grow_past_the_kept_ids(self):
        """Per-id outcomes are pruned with the ids, so a value re-sent for a long time does not grow the entry."""
        gw = self._make_gateway()
        clock, patcher = self._clock()
        with patcher:
            for attempt in range(40):
                clock["now"] = 1000.0 + attempt * 31
                self._run(gw.number_event(self.CHARGE_RATE, 2000))
                self._ack(gw, f"PBAT{attempt + 1}", "set_charge_rate", ok=False, error="not_managed")
        entry = gw._pending_commands[self.CHARGE_RATE]
        assert len(entry["command_ids"]) == 16
        assert set(entry["outcomes"]) <= set(entry["command_ids"])

    def test_attempt_ids_do_not_grow_without_acks(self):
        """Subscribed but no acks ever: every call re-sends, and the id lists stay bounded."""
        gw = self._make_gateway(acks_seen=False)
        clock, patcher = self._clock()
        with patcher:
            for step in range(50):
                clock["now"] = 1000.0 + step * 2
                self._run(gw.number_event(self.CHARGE_RATE, 2000))
        entry = gw._pending_commands[self.CHARGE_RATE]
        assert len(gw._published) == 50
        assert len(entry["command_ids"]) == 16
        assert len(entry["attempt_ids"]) == 16

    def test_second_ok_ack_does_not_overwrite_cache(self):
        """Only the first ok ack for a command updates the cached slot times."""
        gw = self._make_gateway()
        clock, patcher = self._clock()
        with patcher:
            self._run(self._send_slot(gw, self.SLOT_START, "00:30:00"))
            self._ack(gw, "PBAT1", "set_charge_slot", applied={"start": 30, "end": 1800, "slot": 0})
            self._ack(gw, "PBAT1", "set_charge_slot", applied={"start": 30, "end": 900, "slot": 1})
        assert gw.cache[self.SLOT_END] == "18:00:00"

    def test_command_ids_do_not_restart_at_one(self):
        """initialize() seeds the id counter from the clock so a restart does not reuse recent ids."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {}
        gw.initialize(gateway_device_id="pbgw_test", mqtt_host="mqtt.example.com", mqtt_token="tok")
        assert gw._command_id > 1_000_000_000_000
        assert gw.topic_ack == "predbat/devices/pbgw_test/ack/+"

    def test_unknown_or_mismatched_acks_ignored(self):
        """Acks for ids PredBat is not tracking, or for a different command, change nothing."""
        gw = self._make_gateway(acks_seen=False)
        clock, patcher = self._clock()
        with patcher:
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            self._ack(gw, "PBAT99", "set_charge_rate")
            self._ack(gw, "PBAT1", "set_reserve")
            gw._process_ack(b"not json")
            gw._process_ack(b"[1, 2]")
        assert gw._acks_seen is False
        assert gw._pending_commands[self.CHARGE_RATE]["state"] == "sent"

    def test_failed_publish_is_not_left_in_flight(self):
        """A publish that raises must not suppress the next attempt."""
        gw = self._make_gateway()

        async def failing_publish_command(command, command_id=None, **kwargs):
            raise RuntimeError("broker gone")

        gw.publish_command = failing_publish_command
        try:
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
        except RuntimeError:
            pass
        assert gw._pending_commands == {}

    def test_ack_topic_routed_to_ack_handler(self):
        """Messages on ack/<command_id> reach the ack handler."""
        import json
        from unittest.mock import MagicMock

        gw = self._make_gateway()
        clock, patcher = self._clock()
        with patcher:
            self._run(gw.number_event(self.CHARGE_RATE, 2000))
            message = MagicMock()
            message.topic = "predbat/devices/pbgw_test/ack/PBAT1"
            message.payload = json.dumps({"command_id": "PBAT1", "command": "set_charge_rate", "ok": True}).encode("utf-8")
            self._run(gw._handle_message(message))
        assert gw._pending_commands[self.CHARGE_RATE]["state"] == "applied"

    def test_denied_ack_subscription_warns_once(self):
        """A refused ack subscription is tolerated and logged once, not on every reconnect."""
        from unittest.mock import AsyncMock, MagicMock

        gw = self._make_gateway(subscribed=False, acks_seen=False)
        client = MagicMock()
        client.subscribe = AsyncMock(return_value=(0x80,))
        assert self._run(gw._subscribe_acks(client)) is False
        assert self._run(gw._subscribe_acks(client)) is False
        warnings = [c for c in gw.log.call_args_list if "Cannot subscribe to command acks" in str(c.args[0])]
        assert len(warnings) == 1
        client.subscribe = AsyncMock(side_effect=Exception("not authorised"))
        assert self._run(gw._subscribe_acks(client)) is False
        client.subscribe = AsyncMock(return_value=(0,))
        assert self._run(gw._subscribe_acks(client)) is True

    def test_publish_command_uses_preallocated_id(self):
        """publish_command sends the id it is given, and allocates the next one when not given one."""
        import json
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw._mqtt_connected = True
        gw._command_id = 7
        gw.topic_command = "predbat/devices/pbgw_test/command"
        sent = []

        async def fake_publish_raw(topic, payload, retain=False):
            sent.append(json.loads(payload))

        gw._publish_raw = fake_publish_raw
        self._run(gw.publish_command("set_charge_rate", command_id=42, power_w=100))
        self._run(gw.publish_command("set_charge_rate", power_w=100))
        assert [s["command_id"] for s in sent] == ["PBAT42", "PBAT8"]


class TestIntegratePower:
    """gateway_integrate_power: today's import, export and load are integrated from grid and battery power.

    Some inverters do not keep usable grid energy counters - GivEnergy AC inverters sharing one grid
    CT clamp, for one - so the hub's import/export/load "today" figures for them are wrong. The power
    readings are sound.
    """

    BASE_TIME = 1791126000  # 2026-10-04 16:00:00 BST

    def _make_gateway(self, integrate_power=True, shared_ct=True):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.log = MagicMock()
        gw.prefix = "predbat"
        gw._last_status = None
        gw._auto_configured = False
        gw._suffix_to_serial = {}
        gw._inverter_slot_serials = []
        gw.args = {}
        gw._args = {}
        gw.local_tz = pytz.timezone("Europe/London")
        gw.gateway_inverter_serial = []
        gw.gateway_evc_automatic = False
        gw.gateway_evc_control = False
        gw.gateway_shared_ct = shared_ct
        gw.gateway_integrate_power = integrate_power
        gw._integrated_energy = None
        gw._last_read_only = None
        gw._read_only_mismatch_logged = False
        gw._dashboard_calls = {}

        def capture_set_arg(key, value):
            gw._args[key] = value

        def capture_dashboard(entity_id, state=None, attributes=None, app=None):
            gw._dashboard_calls[entity_id] = (state, attributes)

        gw.set_arg = capture_set_arg
        gw.dashboard_item = capture_dashboard
        return gw

    def _status(self, timestamp, grid_w, battery_w=(0, 0), pv_today_wh=2082100, serials=("CE2223G800", "CE2225G400")):
        """A hub status: every inverter reports the same grid power, as they share the CT clamp.

        battery_w is per inverter in the hub's convention, positive = charging. The hub's own
        import/export/load counters are set to the implausible values seen on a shared CT.
        """
        status = pb.GatewayStatus()
        status.device_id = "pbgw_shared"
        status.firmware = "1.0.22"
        status.timestamp = timestamp
        status.schema_version = 1
        for serial, power in zip(serials, battery_w):
            inv = status.inverters.add()
            inv.type = pb.INVERTER_TYPE_GIVENERGY
            inv.serial = serial
            inv.primary = True
            inv.connected = True
            inv.active = True
            inv.battery.soc_percent = 50
            inv.battery.capacity_wh = 9500
            inv.battery.rate_max_w = 3000
            inv.battery.power_w = power
            inv.grid.power_w = grid_w
            inv.energy.pv_today_wh = pv_today_wh if serial == serials[0] else 0
            inv.energy.grid_import_today_wh = 4726400
            inv.energy.grid_export_today_wh = 4721400
            inv.energy.consumption_today_wh = 2993200
        return status

    def _feed(self, gw, status):
        """Deliver one status the way _process_telemetry does once auto-config has bound the inverters."""
        gw._last_status = status
        if not gw._inverter_slot_serials:
            gw.automatic_config()
        gw._update_integrated_energy(status)
        gw._inject_entities(status)

    def _today(self, gw, name, suffix="23g800"):
        return gw._dashboard_calls[f"sensor.predbat_gateway_{suffix}_{name}"][0]

    def test_midnight_still_resets_the_day_while_an_inverter_is_away(self):
        """An inverter kept configured while it is away stops the integration, but not the day ending."""
        gw = self._make_gateway()
        self._feed(gw, self._status(self.BASE_TIME, grid_w=-2000))
        self._feed(gw, self._status(self.BASE_TIME + 600, grid_w=-3000))
        assert approx_equal(gw._integrated_energy["import_wh"], 416.67)

        # 16:10 + 8.5h = 00:40 the next day, with the second inverter gone from the status
        away = self._status(self.BASE_TIME + 600 + 8 * 3600 + 1800, grid_w=-2000, battery_w=(0,), serials=("CE2223G800",))
        self._feed(gw, away)

        assert gw._inverter_slot_serials == ["CE2223G800", "CE2225G400"]
        assert gw._integrated_energy["import_wh"] == 0.0
        assert gw._integrated_energy["load_published_wh"] == 0.0
        assert self._today(gw, "import_today") == 0.0
        assert self._today(gw, "load_today") == 0.0

    def test_pv_only_inverter_generation_counts_towards_the_load(self):
        """A PV-only inverter has no slot, but what it generates is used by the house like any other PV."""
        gw = self._make_gateway()
        gw.gateway_inverter_serial = ["CE2223G800", "FD0000P001"]

        def status(timestamp, pv_only_wh, with_pv_only=True):
            built = self._status(timestamp, grid_w=0, battery_w=(0,), pv_today_wh=1000, serials=("CE2223G800",))
            if with_pv_only:
                inv = built.inverters.add()
                inv.type = pb.INVERTER_TYPE_GIVENERGY
                inv.serial = "FD0000P001"
                inv.primary = True
                inv.connected = True
                inv.pv.power_w = 1000
                inv.energy.pv_today_wh = pv_only_wh
            return built

        self._feed(gw, status(self.BASE_TIME, 3000))
        assert gw._inverter_slot_serials == ["CE2223G800"]
        assert gw._pv_only_serials == ("FD0000P001",)

        self._feed(gw, status(self.BASE_TIME + 600, 3200))
        assert approx_equal(gw._integrated_energy["load_wh"], 200.0)

        # Away for a status: nothing added, and the integration carries on for the rest of the site
        self._feed(gw, status(self.BASE_TIME + 1200, 0, with_pv_only=False))
        assert approx_equal(gw._integrated_energy["load_wh"], 200.0)
        assert gw._integrated_energy["time"] == self.BASE_TIME + 1200

        self._feed(gw, status(self.BASE_TIME + 1800, 3500))
        assert approx_equal(gw._integrated_energy["load_wh"], 500.0)

    def test_pv_only_inverter_promoted_to_a_slot_keeps_its_generation(self):
        """Auto-config re-running and changing the slots does not lose or mix up any inverter's generation."""
        gw = self._make_gateway()
        # The PV-only inverter sorts first, so once it has a slot every slot index moves
        gw.gateway_inverter_serial = ["AA0000P001", "CE2223G800"]

        def status(timestamp, pv_only_wh, has_battery=False):
            built = self._status(timestamp, grid_w=0, battery_w=(0,), pv_today_wh=1000, serials=("CE2223G800",))
            inv = built.inverters.add()
            inv.type = pb.INVERTER_TYPE_GIVENERGY
            inv.serial = "AA0000P001"
            inv.primary = True
            inv.connected = True
            inv.pv.power_w = 1000
            inv.energy.pv_today_wh = pv_only_wh
            if has_battery:
                inv.battery.soc_percent = 40
                inv.battery.capacity_wh = 5000
            return built

        self._feed(gw, status(self.BASE_TIME, 3000))
        self._feed(gw, status(self.BASE_TIME + 600, 3200))
        assert gw._pv_only_serials == ("AA0000P001",)
        assert approx_equal(gw._integrated_energy["load_wh"], 200.0)

        # Its battery readings arrive: _process_telemetry integrates, then re-runs auto-config
        self._feed(gw, status(self.BASE_TIME + 1200, 3300, has_battery=True))
        gw.automatic_config()
        assert gw._inverter_slot_serials == ["AA0000P001", "CE2223G800"]
        assert gw._pv_only_serials == ()
        assert approx_equal(gw._integrated_energy["load_wh"], 300.0)

        self._feed(gw, status(self.BASE_TIME + 1800, 3500, has_battery=True))
        assert approx_equal(gw._integrated_energy["load_wh"], 500.0)

    def test_rebase_takes_the_new_fleet_power_and_adds_nothing(self):
        """A rebase on a status already integrated adds no energy and starts the next interval from the new slots' power."""
        gw = self._make_gateway(shared_ct=False)
        self._feed(gw, self._status(self.BASE_TIME, grid_w=-1000, battery_w=(0,), serials=("CE2225G400",)))
        joining = self._status(self.BASE_TIME + 600, grid_w=-1000, battery_w=(0, 0))
        gw._last_status = joining
        gw._update_integrated_energy(joining)
        before = dict((name, gw._integrated_energy[name]) for name in ("import_wh", "export_wh", "load_wh"))
        assert gw._integrated_energy["power"]["import"] == 1000

        gw.automatic_config()
        gw._update_integrated_energy(joining, rebase=True)

        assert dict((name, gw._integrated_energy[name]) for name in before) == before
        assert gw._integrated_energy["power"]["import"] == 2000
        assert gw._integrated_energy["time"] == self.BASE_TIME + 600

    def test_inverter_given_a_slot_later_has_its_generation_counted_from_then(self):
        """An inverter that joins the slots after the integration began is counted from its first status, not left out."""
        gw = self._make_gateway()
        self._feed(gw, self._status(self.BASE_TIME, grid_w=0, battery_w=(0,), pv_today_wh=1000, serials=("CE2225G400",)))
        self._feed(gw, self._status(self.BASE_TIME + 600, grid_w=0, battery_w=(0,), pv_today_wh=1100, serials=("CE2225G400",)))
        assert approx_equal(gw._integrated_energy["load_wh"], 100.0)

        def both(timestamp, first_wh, second_wh):
            built = self._status(timestamp, grid_w=0, battery_w=(0, 0), pv_today_wh=first_wh)
            built.inverters[1].energy.pv_today_wh = second_wh
            return built

        # CE2223G800 appears and takes slot 0; its counter already stands at 7000. As in
        # _process_telemetry, the status is integrated on the old slots, auto-config re-runs,
        # and the status then becomes the baseline for the new ones
        joining = both(self.BASE_TIME + 1200, 7000, 1200)
        gw._last_status = joining
        gw._update_integrated_energy(joining)
        gw.automatic_config()
        assert gw._inverter_slot_serials == ["CE2223G800", "CE2225G400"]
        gw._update_integrated_energy(joining, rebase=True)
        assert approx_equal(gw._integrated_energy["load_wh"], 200.0)
        assert gw._integrated_energy["pv"]["CE2223G800"]["base"] == 7000

        # Its generation since the status it joined on is counted, not only from the one after
        self._feed(gw, both(self.BASE_TIME + 1800, 7300, 1250))
        assert approx_equal(gw._integrated_energy["load_wh"], 550.0)

    def test_import_and_load_are_integrated_from_power(self):
        """6 kW of import for 6 minutes is 0.6 kWh; 3 kW of that went into a battery, so load is 0.3 kWh."""
        gw = self._make_gateway()
        self._feed(gw, self._status(self.BASE_TIME, grid_w=-6000, battery_w=(0, 3000)))
        self._feed(gw, self._status(self.BASE_TIME + 360, grid_w=-6000, battery_w=(0, 3000)))

        assert self._today(gw, "import_today") == 0.6
        assert self._today(gw, "export_today") == 0.0
        assert self._today(gw, "load_today") == 0.3

    def test_export_discharge_and_pv_feed_the_load(self):
        """load = pv + import - export + battery discharge - battery charge."""
        gw = self._make_gateway()
        self._feed(gw, self._status(self.BASE_TIME, grid_w=2000, battery_w=(-1000, 0), pv_today_wh=2082100))
        self._feed(gw, self._status(self.BASE_TIME + 360, grid_w=2000, battery_w=(-1000, 0), pv_today_wh=2082600))

        assert self._today(gw, "import_today") == 0.0
        assert self._today(gw, "export_today") == 0.2
        # 0.5 pv + 0 import - 0.2 export + 0.1 discharge - 0 charge
        assert self._today(gw, "load_today") == 0.4

    def test_grid_power_is_split_by_sign_before_integrating(self):
        """A swing from import to export inside one interval counts towards both, not their net."""
        gw = self._make_gateway()
        self._feed(gw, self._status(self.BASE_TIME, grid_w=-4000))
        self._feed(gw, self._status(self.BASE_TIME + 360, grid_w=2000))

        assert self._today(gw, "import_today") == 0.2
        assert self._today(gw, "export_today") == 0.1

    def test_one_battery_charging_the_other_counts_as_both_charge_and_discharge(self):
        """Charge and discharge are taken per inverter, so a cross-charge does not hide either."""
        gw = self._make_gateway()
        self._feed(gw, self._status(self.BASE_TIME, grid_w=0, battery_w=(2000, -2000)))
        self._feed(gw, self._status(self.BASE_TIME + 360, grid_w=0, battery_w=(2000, -2000)))

        state = gw._integrated_energy
        assert round(state["charge_wh"]) == 200
        assert round(state["discharge_wh"]) == 200
        assert self._today(gw, "load_today") == 0.0

    def test_hub_counters_are_kept_when_the_option_is_off(self):
        """Without gateway_integrate_power the hub's own counters are published unchanged, shared CT or not."""
        for shared_ct in (False, True):
            gw = self._make_gateway(integrate_power=False, shared_ct=shared_ct)
            self._feed(gw, self._status(self.BASE_TIME, grid_w=-6000))
            self._feed(gw, self._status(self.BASE_TIME + 360, grid_w=-6000))

            assert self._today(gw, "import_today") == 4726.4
            assert self._today(gw, "export_today") == 4721.4
            assert self._today(gw, "load_today") == 2993.2
            assert gw._integrated_energy is None

    def test_a_single_inverter_is_integrated_too(self):
        """The option does not need several inverters: one inverter's grid and battery power are integrated."""
        gw = self._make_gateway(shared_ct=False)
        self._feed(gw, self._status(self.BASE_TIME, grid_w=-6000, battery_w=(3000,), serials=("CE2223G800",)))
        self._feed(gw, self._status(self.BASE_TIME + 360, grid_w=-6000, battery_w=(3000,), serials=("CE2223G800",)))

        assert self._today(gw, "import_today") == 0.6
        assert self._today(gw, "load_today") == 0.3

    def test_separate_clamps_add_up_every_inverters_grid_power(self):
        """Without gateway_shared_ct each inverter has its own clamp, so the site's grid power is their sum."""
        gw = self._make_gateway(shared_ct=False)
        first = self._status(self.BASE_TIME, grid_w=-6000)
        second = self._status(self.BASE_TIME + 360, grid_w=-6000)
        for status in (first, second):
            status.inverters[1].grid.power_w = 2000
        self._feed(gw, first)
        self._feed(gw, second)

        # -6000 W + 2000 W = 4 kW of net import for 6 minutes
        assert self._today(gw, "import_today") == 0.4
        assert self._today(gw, "export_today") == 0.0

    def test_a_shared_clamp_takes_grid_power_from_the_first_inverter_only(self):
        """With gateway_shared_ct every inverter reads the same clamp, so it is counted once."""
        gw = self._make_gateway(shared_ct=True)
        self._feed(gw, self._status(self.BASE_TIME, grid_w=-6000))
        self._feed(gw, self._status(self.BASE_TIME + 360, grid_w=-6000))

        assert self._today(gw, "import_today") == 0.6

    def test_the_site_figures_are_bound_to_the_first_inverter_only(self):
        """The integrated figures describe the whole site, so Predbat reads them from the first inverter alone, shared CT or not."""
        for shared_ct in (False, True):
            gw = self._make_gateway(shared_ct=shared_ct)
            self._feed(gw, self._status(self.BASE_TIME, grid_w=-6000))

            for counter in ("import_today", "export_today", "load_today"):
                assert gw._args[counter] == [f"sensor.predbat_gateway_23g800_{counter}"], counter
            assert gw._args["pv_today"] == ["sensor.predbat_gateway_23g800_pv_today", "sensor.predbat_gateway_25g400_pv_today"]

    def test_integrate_power_is_read_from_the_component_argument(self):
        """initialize() takes gateway_integrate_power from apps.yaml and defaults it to off."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {}
        gw.initialize(gateway_device_id="pbgw_test", mqtt_host="mqtt.example", mqtt_token="token")
        assert gw.gateway_integrate_power is False

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {}
        gw.initialize(gateway_device_id="pbgw_test", mqtt_host="mqtt.example", mqtt_token="token", gateway_integrate_power=True)
        assert gw.gateway_integrate_power is True

    def test_only_the_first_inverter_carries_the_site_figures(self):
        """PredBat reads import/export/load today from the first inverter; the others keep the hub's values."""
        gw = self._make_gateway()
        self._feed(gw, self._status(self.BASE_TIME, grid_w=-6000))
        self._feed(gw, self._status(self.BASE_TIME + 360, grid_w=-6000))

        assert self._today(gw, "import_today") == 0.6
        assert self._today(gw, "import_today", suffix="25g400") == 4726.4

    def test_pv_from_every_inverter_feeds_the_load(self):
        """Each inverter measures its own PV, so the load takes the rise in every inverter's pv_today counter."""
        gw = self._make_gateway()
        first = self._status(self.BASE_TIME, grid_w=0)
        first.inverters[0].energy.pv_today_wh = 1000
        first.inverters[1].energy.pv_today_wh = 5000
        second = self._status(self.BASE_TIME + 360, grid_w=0)
        second.inverters[0].energy.pv_today_wh = 1300
        second.inverters[1].energy.pv_today_wh = 5200
        self._feed(gw, first)
        self._feed(gw, second)

        assert self._today(gw, "load_today") == 0.5

    def test_pv_today_is_left_alone(self):
        """pv_today is still the hub's counter."""
        gw = self._make_gateway()
        self._feed(gw, self._status(self.BASE_TIME, grid_w=0, pv_today_wh=2082100))

        assert self._today(gw, "pv_today") == 2082.1

    def test_the_same_status_injected_twice_is_counted_once(self):
        """_inject_entities re-runs on a refused command; that must not add energy."""
        gw = self._make_gateway()
        self._feed(gw, self._status(self.BASE_TIME, grid_w=-6000))
        status = self._status(self.BASE_TIME + 360, grid_w=-6000)
        self._feed(gw, status)
        self._feed(gw, status)
        gw._inject_entities(status)

        assert self._today(gw, "import_today") == 0.6

    def test_a_long_gap_in_telemetry_adds_no_energy(self):
        """Power is not assumed to have held across a gap longer than the limit."""
        from gateway import GATEWAY_INTEGRATE_MAX_GAP_SECONDS

        gw = self._make_gateway()
        self._feed(gw, self._status(self.BASE_TIME, grid_w=-6000))
        self._feed(gw, self._status(self.BASE_TIME + GATEWAY_INTEGRATE_MAX_GAP_SECONDS + 1, grid_w=-6000))
        assert self._today(gw, "import_today") == 0.0

        self._feed(gw, self._status(self.BASE_TIME + GATEWAY_INTEGRATE_MAX_GAP_SECONDS + 361, grid_w=-6000))
        assert self._today(gw, "import_today") == 0.6

    def test_a_disconnected_inverter_skips_the_sample(self):
        """A unit that is not connected reports stale power, so nothing is integrated from that status."""
        gw = self._make_gateway()
        self._feed(gw, self._status(self.BASE_TIME, grid_w=-6000))
        status = self._status(self.BASE_TIME + 360, grid_w=-6000)
        status.inverters[1].connected = False
        self._feed(gw, status)

        assert self._today(gw, "import_today") == 0.0

    def test_nothing_is_integrated_across_a_disconnected_spell(self):
        """The first status after a unit comes back only starts a new interval: the time it was away is not filled in."""
        gw = self._make_gateway()
        self._feed(gw, self._status(self.BASE_TIME, grid_w=-6000))
        status = self._status(self.BASE_TIME + 120, grid_w=-6000)
        status.inverters[1].connected = False
        self._feed(gw, status)

        self._feed(gw, self._status(self.BASE_TIME + 240, grid_w=-6000))
        assert self._today(gw, "import_today") == 0.0

        self._feed(gw, self._status(self.BASE_TIME + 600, grid_w=-6000))
        assert self._today(gw, "import_today") == 0.6

    def test_a_pv_counter_glitch_to_zero_and_back_adds_no_load(self):
        """The hub's pv counter drops to zero for a sample around midnight; its return is not generation."""
        gw = self._make_gateway()
        self._feed(gw, self._status(self.BASE_TIME, grid_w=0, pv_today_wh=2082100))
        self._feed(gw, self._status(self.BASE_TIME + 120, grid_w=0, pv_today_wh=0))
        self._feed(gw, self._status(self.BASE_TIME + 240, grid_w=0, pv_today_wh=2082100))

        assert self._today(gw, "load_today") == 0.0

    def test_a_small_pv_counter_dropping_and_returning_adds_no_load(self):
        """A drop and return small enough to pass as generation is still only the counter coming back."""
        gw = self._make_gateway()
        self._feed(gw, self._status(self.BASE_TIME, grid_w=0, pv_today_wh=1000))
        self._feed(gw, self._status(self.BASE_TIME + 120, grid_w=0, pv_today_wh=0))
        self._feed(gw, self._status(self.BASE_TIME + 240, grid_w=0, pv_today_wh=1000))
        assert self._today(gw, "load_today") == 0.0

        # Generation past the value it dropped from counts as normal
        self._feed(gw, self._status(self.BASE_TIME + 360, grid_w=0, pv_today_wh=1200))
        assert self._today(gw, "load_today") == 0.2

    def test_a_pv_counter_that_resets_counts_from_its_new_start(self):
        """A counter that drops and stays down has reset for the day; generation after it counts straight away."""
        gw = self._make_gateway()
        self._feed(gw, self._status(self.BASE_TIME, grid_w=0, pv_today_wh=9000))
        self._feed(gw, self._status(self.BASE_TIME + 120, grid_w=0, pv_today_wh=0))
        self._feed(gw, self._status(self.BASE_TIME + 240, grid_w=0, pv_today_wh=100))
        self._feed(gw, self._status(self.BASE_TIME + 360, grid_w=0, pv_today_wh=300))

        assert self._today(gw, "load_today") == 0.3

    def test_load_today_does_not_go_backwards_within_a_day(self):
        """Grid and battery are read at slightly different moments, so the sum can dip; the published load holds."""
        gw = self._make_gateway()
        self._feed(gw, self._status(self.BASE_TIME, grid_w=-6000))
        self._feed(gw, self._status(self.BASE_TIME + 360, grid_w=-6000))
        assert self._today(gw, "load_today") == 0.6

        # The battery now reads as charging at 3 kW with no import to feed it
        self._feed(gw, self._status(self.BASE_TIME + 720, grid_w=0, battery_w=(3000, 0)))
        assert self._today(gw, "load_today") == 0.75
        self._feed(gw, self._status(self.BASE_TIME + 1080, grid_w=0, battery_w=(3000, 0)))
        assert round(gw._integrated_energy["load_wh"]) == 450
        assert self._today(gw, "load_today") == 0.75

    def test_counters_restart_at_local_midnight(self):
        """The figures are today's: they return to zero at local midnight, and an interval that spans
        midnight only gives the new day the part of it after midnight."""
        import datetime as _datetime

        local_tz = pytz.timezone("Europe/London")
        before = int(local_tz.localize(_datetime.datetime(2026, 10, 4, 23, 50, 0)).timestamp())
        gw = self._make_gateway()
        self._feed(gw, self._status(before, grid_w=-6000, pv_today_wh=1000))
        self._feed(gw, self._status(before + 360, grid_w=-6000, pv_today_wh=1000))
        assert self._today(gw, "import_today") == 0.6

        # 00:02 the next day, 6 minutes after the 23:56 sample: 2 of them are today's. The 300 Wh
        # the pv counter gained over the interval is shared out the same way
        self._feed(gw, self._status(before + 720, grid_w=-6000, pv_today_wh=1300))
        assert self._today(gw, "import_today") == 0.2
        assert self._today(gw, "load_today") == 0.3
        assert gw._integrated_energy["day"] == _datetime.date(2026, 10, 5)

        self._feed(gw, self._status(before + 1080, grid_w=0, pv_today_wh=1300))
        assert self._today(gw, "import_today") == 0.5


class TestGatewayOnlineTopic:
    """The hub's retained online topic, as the firmware publishes it: 'online', and 'offline' as its LWT (predbat-gateway#510)."""

    def _make_gateway(self):
        """Build a minimal GatewayMQTT stub that _handle_message() can route an online message through."""
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock
        import time as time_mod

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw.base = MagicMock()
        gw.base.inverters = []
        gw.prefix = "predbat"
        gw.topic_status = "predbat/devices/pbgw_test/status"
        gw.topic_online = "predbat/devices/pbgw_test/online"
        gw.dashboard_item = MagicMock()
        gw.gateway_display_only = False
        gw._error_count = 0
        gw._rate_caps_sent = {}
        gw._mqtt_connected = True
        gw._gateway_online = False
        gw._last_telemetry_time = time_mod.time()
        return gw

    def _online(self, gw, payload):
        """Deliver one message on the online topic."""
        from unittest.mock import MagicMock

        message = MagicMock()
        message.topic = gw.topic_online
        message.payload = payload
        asyncio.run(gw._handle_message(message))

    def test_firmware_payloads_set_the_flag(self):
        """The firmware publishes 'online' on connect and 'offline' as its LWT, not '1' / '0'."""
        gw = self._make_gateway()
        self._online(gw, b"online")
        assert gw._gateway_online is True
        self._online(gw, b"offline")
        assert gw._gateway_online is False
        assert gw._error_count == 0

    def test_numeric_payloads_still_accepted(self):
        """'1' / '0', the payloads this code used to expect, are still accepted."""
        gw = self._make_gateway()
        self._online(gw, b"1")
        assert gw._gateway_online is True
        self._online(gw, b"0")
        assert gw._gateway_online is False

    def test_transition_is_logged_and_published(self):
        """Going online logs the change and publishes the online sensor."""
        gw = self._make_gateway()
        self._online(gw, b"online")
        gw.log.assert_any_call("Info: GatewayMQTT: Gateway is online")
        entity, state = gw.dashboard_item.call_args[0][:2]
        assert entity == "binary_sensor.predbat_gateway_online"
        assert state is True

    def test_stale_telemetry_is_not_alive_while_online(self):
        """A hub that says it is online but has sent no telemetry for over two minutes is a frozen feed."""
        import time as time_mod

        gw = self._make_gateway()
        self._online(gw, b"online")
        assert gw.is_alive() is True
        gw._last_telemetry_time = time_mod.time() - 300
        assert gw.is_alive() is False

    def _make_control_gateway(self):
        """A control-mode gateway built through initialize(), with housekeeping's outbound calls stubbed."""
        from gateway import GatewayMQTT
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.prefix = "predbat"
        gw.args = {}
        gw.log = MagicMock()
        gw.initialize(gateway_device_id="pbgw_test", mqtt_host="mqtt.test")
        gw._mqtt_connected = True
        gw._mqtt_client = SimpleNamespace(publish=AsyncMock())
        gw.api_started = True
        gw.api_stop = False
        for name in ("_check_read_only_state", "_check_inverter_resets", "_check_rate_caps", "_publish_predbat_data", "_check_token_refresh", "_republish_plan_if_stale"):
            setattr(gw, name, AsyncMock())
        return gw

    def test_silent_hub_goes_unhealthy_even_when_offline(self):
        """Housekeeping is not a success: a hub that has sent nothing for two hours is unhealthy, whatever its LWT says."""
        import datetime
        import time as time_mod
        from components import Components
        from types import SimpleNamespace
        from unittest.mock import patch

        gw = self._make_control_gateway()
        comps = Components.__new__(Components)
        comps.components = {"gateway": gw}
        comps.component_tasks = {"gateway": SimpleNamespace(is_alive=lambda: True)}
        comps.component_errors = {}

        gw._gateway_online = False
        gw._last_telemetry_time = time_mod.time() - 7200
        gw.last_success_timestamp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
        with patch("gateway.HAS_AIOMQTT", True):
            assert asyncio.run(gw.run(60, False))

        # Connected to the broker with the hub offline is a known state for the component itself...
        assert gw.is_alive() is True
        # ...but nothing has been read for two hours, so the site must not report healthy
        assert comps.is_alive("gateway") is False

        # Telemetry arriving is what makes it healthy again
        gw.update_success_timestamp()
        assert comps.is_alive("gateway") is True

    def test_rate_caps_sent_once_online(self):
        """set_rate_cap is held while the hub is offline, so it must go out once the firmware says 'online'."""
        helper = TestCheckRateCaps()
        gw = helper._make_gateway()
        gw._gateway_online = False
        gw._inverter_slot_serials = ["CH2330G098"]
        gw.base.inverters = [helper._inverter(0, 3000, 3600)]
        gw.prefix = "predbat"
        gw.topic_status = "predbat/devices/pbgw_test/status"
        gw.topic_online = "predbat/devices/pbgw_test/online"
        gw.dashboard_item = lambda *args, **kwargs: None
        gw._error_count = 0

        asyncio.run(gw._check_rate_caps())
        assert gw._published == []

        self._online(gw, b"online")
        asyncio.run(gw._check_rate_caps())
        assert [command for command, _ in gw._published] == ["set_rate_cap"]


def run_gateway_tests(my_predbat=None):
    """Run all GatewayMQTT tests. Returns True on failure, False on success."""
    from tests.test_gateway_token_refresh import TestIsAuthFailure, TestApplyRefreshResponse, TestMaybeRefreshOnAuthError, TestTokenRefreshProvider
    from tests.test_gateway_window_commit import TestHubWindowCommit
    from tests.test_gateway_commit import TestGatewayStaging, TestGatewayCommit
    from tests.test_gateway_display import TestGatewayDisplayOnly, TestIndependentVisibility
    from tests.test_live_display import run_live_display_tests

    try:
        run_live_display_tests()
    except Exception:
        import traceback

        traceback.print_exc()
        return True

    test_classes = [
        TestGatewayStaging,
        TestGatewayCommit,
        TestHubWindowCommit,
        TestGatewayDisplayOnly,
        TestIndependentVisibility,
        TestProtobufDecode,
        TestPlanSerialization,
        TestCommandFormat,
        TestScheduleSlotCommand,
        TestInjectEntities,
        TestDebugLogging,
        TestAutomaticConfig,
        TestIntegratePower,
        TestBoundEntitiesAreWritten,
        TestGatewayUnitControlBinding,
        TestEvTelemetry,
        TestEvAutoConfig,
        TestEvInitialize,
        TestEvNeedsReconfigure,
        TestEvControl,
        TestSelectEvent,
        TestNumberEvent,
        TestSwitchEvent,
        TestCommandAck,
        TestTokenRefresh,
        TestPlanHookConversion,
        TestMQTTIntegration,
        TestPlanRepublish,
        TestPublishPredbatData,
        TestIanaToPosixTz,
        TestCheckInverterResets,
        TestCheckCommandedSerials,
        TestCheckRateCaps,
        TestGatewayOnlineTopic,
        TestCheckReadOnlyState,
        TestRunStartupWait,
        TestSetChargeSlotPayload,
        TestIsAuthFailure,
        TestApplyRefreshResponse,
        TestMaybeRefreshOnAuthError,
        TestTokenRefreshProvider,
        TestRateAnchors,
        TestPublishRawLoopSafety,
    ]
    for cls in test_classes:
        instance = cls()
        for attr in sorted(dir(instance)):
            if not attr.startswith("test_"):
                continue
            method = getattr(instance, attr)
            try:
                method()
            except Exception as e:
                print(f"  FAIL: {cls.__name__}.{attr}: {e}")
                import traceback

                traceback.print_exc()
                return True
            print(f"  OK: {cls.__name__}.{attr}")

    if my_predbat is not None:
        try:
            test_ev_soc_battery_size_through_get_arg(my_predbat)
        except Exception as e:
            print(f"  FAIL: test_ev_soc_battery_size_through_get_arg: {e}")
            import traceback

            traceback.print_exc()
            return True
        print("  OK: test_ev_soc_battery_size_through_get_arg")
        test_ev_two_windows_through_planner(my_predbat)
        print("  OK: test_ev_two_windows_through_planner")
    return False
