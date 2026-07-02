"""
Tests for GatewayMQTT component.
"""
import sys
import os
import math
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytz
import gateway_status_pb2 as pb

import importlib.util

HAS_AIOMQTT = importlib.util.find_spec("aiomqtt") is not None


def approx_equal(actual, expected, abs_tol=0.01):
    """Simple float comparison for when pytest is not available."""
    return math.isclose(actual, expected, abs_tol=abs_tol)


class TestProtobufDecode:
    """Test protobuf telemetry → entity mapping."""

    def _make_status(self, soc=50, battery_power=1000, pv_power=2000, grid_power=-500, load_power=1500, mode=0):
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

        inv.control.mode = mode
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
    def test_set_mode_command(self):
        from gateway import GatewayMQTT

        cmd = GatewayMQTT.build_command("set_mode", mode=1)
        import json

        parsed = json.loads(cmd)
        assert parsed["command"] == "set_mode"
        assert parsed["mode"] == 1
        assert "command_id" in parsed

    def test_set_charge_rate_command(self):
        from gateway import GatewayMQTT

        cmd = GatewayMQTT.build_command("set_charge_rate", power_w=2500)
        import json

        parsed = json.loads(cmd)
        assert parsed["command"] == "set_charge_rate"
        assert parsed["power_w"] == 2500

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

        cmd = GatewayMQTT.build_command("set_mode", mode=1, command_id=7)
        parsed = json.loads(cmd)
        assert parsed["command_id"] == "PBAT7"
        assert isinstance(parsed["command_id"], str)

    def test_serial_included_as_dongle_serial(self):
        """serial kwarg is serialised as dongle_serial in the JSON payload."""
        from gateway import GatewayMQTT
        import json

        cmd = GatewayMQTT.build_command("set_mode", mode=1, serial="CE123456789")
        parsed = json.loads(cmd)
        assert parsed["dongle_serial"] == "CE123456789"
        assert "serial" not in parsed

    def test_dongle_serial_preserves_original_case(self):
        """dongle_serial is stored as-is (uppercase) even though entity suffixes are lowercased."""
        from gateway import GatewayMQTT
        import json

        cmd = GatewayMQTT.build_command("set_charge_rate", power_w=3000, serial="CE123456789")
        parsed = json.loads(cmd)
        assert parsed["dongle_serial"] == "CE123456789"
        assert parsed["dongle_serial"] != parsed["dongle_serial"].lower()

    def test_dongle_serial_omitted_when_not_provided(self):
        """dongle_serial key is absent from the JSON when no serial kwarg is given."""
        from gateway import GatewayMQTT
        import json

        cmd = GatewayMQTT.build_command("set_mode", mode=1)
        parsed = json.loads(cmd)
        assert "dongle_serial" not in parsed
        assert "serial" not in parsed


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
        assert gw._serial_from_entity_id("select.predbat_gateway_456789_mode_select") == "CE123456789"

    def test_short_serial_suffix(self):
        """Serials shorter than 6 chars produce a shorter suffix; lookup still succeeds."""
        gw = self._make_gateway()
        gw._suffix_to_serial["abc"] = "ABC"  # serial == suffix (3 chars)
        assert gw._serial_from_entity_id("select.predbat_gateway_abc_mode_select") == "ABC"

    def test_suffix_lookup_is_case_insensitive(self):
        """Entity ID suffix is lowercased before lookup even if entity_id contains upper chars."""
        gw = self._make_gateway()
        gw._suffix_to_serial["456789"] = "CE123456789"
        # Uppercase in entity_id (unusual but should still resolve)
        assert gw._serial_from_entity_id("select.predbat_gateway_456789_mode_select") == "CE123456789"

    def test_no_gateway_marker_returns_none(self):
        """Entity IDs without '_gateway_' return None without logging."""
        gw = self._make_gateway()
        result = gw._serial_from_entity_id("select.predbat_some_other_entity")
        assert result is None
        gw.log.assert_not_called()

    def test_unknown_suffix_returns_none_and_warns(self):
        """Unknown suffix returns None and emits a Warn log."""
        gw = self._make_gateway()
        result = gw._serial_from_entity_id("select.predbat_gateway_456789_mode_select")
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

        # idle_*_time should have one entry per inverter (1 in this case)
        assert len(gw._args["idle_start_time"]) == 1
        assert len(gw._args["idle_end_time"]) == 1
        assert "discharge_slot1_start" in gw._args["idle_start_time"][0]
        assert "discharge_slot1_end" in gw._args["idle_end_time"][0]

    def test_non_ems_does_not_set_aggregate_args(self):
        """Standard GivEnergy inverter does NOT register ems_* args."""
        gw = self._make_gateway()
        gw._last_status = self._basic_status()
        gw.automatic_config()

        assert "ems_total_soc" not in gw._args
        assert "idle_start_time" not in gw._args

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
    """Tests for GatewayMQTT.select_event() — mode and schedule-time routing."""

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

        async def fake_publish_command(command, **kwargs):
            gw._published.append((command, kwargs))

        gw.publish_command = fake_publish_command
        return gw

    def _run(self, coro):
        """Run a coroutine synchronously."""
        import asyncio

        return asyncio.run(coro)

    # ------------------------------------------------------------------
    # Charge slot time routing
    # ------------------------------------------------------------------

    def test_charge_slot_start(self):
        """charge_slot1_start publishes set_charge_slot with start HHMM."""
        import json

        gw = self._make_gateway()
        self._run(gw.select_event("select.predbat_gateway_456789_charge_slot1_start", "01:30:00"))
        assert len(gw._published) == 1
        cmd, kwargs = gw._published[0]
        assert cmd == "set_charge_slot"
        parsed = json.loads(kwargs["schedule_json"])
        assert parsed == {"start": 130}

    def test_charge_slot_end(self):
        """charge_slot1_end publishes set_charge_slot with end HHMM."""
        import json

        gw = self._make_gateway()
        self._run(gw.select_event("select.predbat_gateway_456789_charge_slot1_end", "04:30:00"))
        cmd, kwargs = gw._published[0]
        assert cmd == "set_charge_slot"
        assert json.loads(kwargs["schedule_json"]) == {"end": 430}

    # ------------------------------------------------------------------
    # Discharge slot time routing
    # ------------------------------------------------------------------

    def test_discharge_slot_start(self):
        """discharge_slot1_start publishes set_discharge_slot with start HHMM."""
        import json

        gw = self._make_gateway()
        self._run(gw.select_event("select.predbat_gateway_456789_discharge_slot1_start", "16:00:00"))
        cmd, kwargs = gw._published[0]
        assert cmd == "set_discharge_slot"
        assert json.loads(kwargs["schedule_json"]) == {"start": 1600}

    def test_discharge_slot_end(self):
        """discharge_slot1_end publishes set_discharge_slot with end HHMM."""
        import json

        gw = self._make_gateway()
        self._run(gw.select_event("select.predbat_gateway_456789_discharge_slot1_end", "19:00:00"))
        cmd, kwargs = gw._published[0]
        assert cmd == "set_discharge_slot"
        assert json.loads(kwargs["schedule_json"]) == {"end": 1900}

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_midnight_time_converts_correctly(self):
        """00:00:00 → HHMM 0 (midnight)."""
        import json

        gw = self._make_gateway()
        self._run(gw.select_event("select.predbat_gateway_456789_charge_slot1_start", "00:00:00"))
        parsed = json.loads(gw._published[0][1]["schedule_json"])
        assert parsed == {"start": 0}

    def test_invalid_time_string_no_command(self):
        """Malformed time values (non-numeric) do not publish any command."""
        gw = self._make_gateway()
        self._run(gw.select_event("select.predbat_gateway_456789_charge_slot1_start", "bad_value"))
        assert gw._published == []

    def test_unrecognised_entity_no_command(self):
        """Entities that don't match any known pattern are silently ignored."""
        gw = self._make_gateway()
        self._run(gw.select_event("select.predbat_some_other_select", "01:00:00"))
        assert gw._published == []

    # ------------------------------------------------------------------
    # Serial routing
    # ------------------------------------------------------------------

    def test_mode_select_includes_serial_when_known(self):
        """mode_select passes full inverter serial to publish_command when suffix is in the map."""
        gw = self._make_gateway()
        gw._suffix_to_serial["456789"] = "CE123456789"
        self._run(gw.select_event("select.predbat_gateway_456789_mode_select", "Eco"))
        assert len(gw._published) == 1
        _, kwargs = gw._published[0]
        assert kwargs.get("serial") == "CE123456789"

    def test_charge_slot_includes_serial_when_known(self):
        """charge_slot1_start passes full inverter serial when suffix is in the map."""
        gw = self._make_gateway()
        gw._suffix_to_serial["456789"] = "CE123456789"
        self._run(gw.select_event("select.predbat_gateway_456789_charge_slot1_start", "01:30:00"))
        _, kwargs = gw._published[0]
        assert kwargs.get("serial") == "CE123456789"

    def test_no_command_when_suffix_not_in_map(self):
        """No command is sent when the entity suffix cannot be resolved to a serial."""
        gw = self._make_gateway()
        gw._suffix_to_serial = {}  # clear — suffix "456789" unknown
        self._run(gw.select_event("select.predbat_gateway_456789_mode_select", "Eco"))
        assert gw._published == []
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
        return gw

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
        """When _first_connection_attempted is pre-set, run() returns True without sleeping."""
        if not HAS_AIOMQTT:
            return
        from unittest.mock import patch, AsyncMock

        gw = self._make_gateway()
        gw._first_connection_attempted = True

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
        """run() exits the wait loop as soon as the flag is set, after a single sleep."""
        if not HAS_AIOMQTT:
            return
        from unittest.mock import patch

        gw = self._make_gateway()
        sleep_count = [0]

        async def run_test():
            async def fake_sleep(t):
                sleep_count[0] += 1
                gw._first_connection_attempted = True  # Simulates MQTT loop completing first attempt

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
        assert sleep_count[0] == 120  # 60 * 2 iterations of 0.5s each = 60s total
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
        assert sleep_count[0] == 60 * 2  # 60 * 2 iterations of 0.5s each = 60s total
        warn_logged = any("Warn" in str(c) and "Auto-config not complete" in str(c) for c in gw.log.call_args_list)
        assert warn_logged, "Expected a Warn log when auto-config times out"

    def test_auto_config_wait_skipped_when_not_connected(self):
        """When connection failed, auto-config wait is skipped entirely."""
        if not HAS_AIOMQTT:
            return
        from unittest.mock import patch, AsyncMock

        gw = self._make_gateway()
        gw._first_connection_attempted = True
        # _mqtt_connected stays False (connection failed)

        async def run_test():
            async def fake_mqtt_loop():
                pass

            gw._mqtt_loop = fake_mqtt_loop
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                result = await gw.run(0, True)
            return result, mock_sleep.call_count

        result, sleep_count = self._run(run_test())
        assert result is True
        assert sleep_count == 0  # No sleeps: connection-wait breaks immediately, auto-config skipped


class TestSetChargeSlotPayload:
    """Diagnostic test — captures the raw MQTT JSON for set_charge_slot and prints it.

    Expected hub format:
      {"command": "set_charge_slot", "dongle_serial": "<serial>", "schedule_json": "{\"start\":200}"}
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
        """Print the raw JSON sent for a charge slot start update so the format can be verified."""
        import json

        gw = self._make_gateway()
        # 02:00:00 → HHMM 200, matching the expected {"start":200} in the hub spec
        self._run(gw.select_event("select.predbat_gateway_30g499_charge_slot1_start", "02:00:00"))

        assert gw._raw_published, "No payload was published — serial lookup may have failed"
        actual = json.loads(gw._raw_published[0].decode("utf-8"))

        expected = {
            "command": "set_charge_slot",
            "command_id": "PBAT1",
            "dongle_serial": "CH2330G499",
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

        base = "predbat_gateway_ev"
        assert gw._dashboard_calls[f"binary_sensor.{base}_online"][0] is True
        assert gw._dashboard_calls[f"binary_sensor.{base}_connected"][0] is True  # status="Charging" → car connected
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

    def test_ev_entity_attributes_from_table(self):
        """Published EV entities carry their GATEWAY_ATTRIBUTE_TABLE attributes."""
        from gateway import GATEWAY_ATTRIBUTE_TABLE

        gw = self._make_gateway()
        gw._inject_ev_entities(self._status_with_ev())
        _, attrs = gw._dashboard_calls["sensor.predbat_gateway_ev_power"]
        assert attrs == GATEWAY_ATTRIBUTE_TABLE["ev_power"]

    def test_not_reported_fields_skipped(self):
        """Zero/empty 'not reported' fields are not published, except soc which falls back to session energy."""
        gw = self._make_gateway(battery_size=100)
        # session_energy_wh=12400 → 12.4 kWh; battery_size=100 → fallback soc = 12.4%
        status = self._status_with_ev(soc_percent=0, voltage_v=0, max_current_a=0, current_limit_a=0, eco_mode="", status="")
        gw._inject_ev_entities(status)

        base = "predbat_gateway_ev"
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
        assert approx_equal(gw._dashboard_calls["sensor.predbat_gateway_ev_soc"][0], 24.8)

    def test_charge_rate_uses_230v_when_voltage_missing(self):
        """With max current but no voltage, charge rate assumes 230 V."""
        gw = self._make_gateway()
        gw._inject_ev_entities(self._status_with_ev(max_current_a=16, voltage_v=0))
        # 16 A × 230 V / 1000
        assert approx_equal(gw._dashboard_calls["sensor.predbat_gateway_ev_charge_rate"][0], 3.68)

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

    def _status_with_ev(self, charge_point_id="CP1", max_current_a=32, voltage_v=240):
        status = pb.GatewayStatus()
        ev = status.ev_chargers.add()
        ev.connected = True
        ev.charge_point_id = charge_point_id
        ev.max_current_a = max_current_a
        ev.voltage_v = voltage_v
        return status

    def test_registers_car_when_none_configured(self):
        """With the flag on and no existing cars, the charger is registered as car 1."""
        gw = self._make_gateway(ev_enable=True, num_cars=0)
        gw._register_ev_car(self._status_with_ev())

        assert gw._args["num_cars"] == 1
        assert gw._args["car_charging_planned"] == ["binary_sensor.predbat_gateway_ev_connected"]
        assert gw._args["car_charging_now"] == ["binary_sensor.predbat_gateway_ev_session_active"]
        assert gw._args["car_charging_soc"] == ["sensor.predbat_gateway_ev_soc"]
        # car_charging_rate is a UI config item — set via expose_config, not set_arg
        assert "car_charging_rate" not in gw._args
        gw.base.expose_config.assert_called_once_with("car_charging_rate", 7.68)  # 32A * 240V / 1000
        # Session energy sensor for subtracting EV load from history
        assert gw._args["car_charging_energy"] == "sensor.predbat_gateway_ev_session_energy"
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
        assert gw._args["car_charging_now"] == ["binary_sensor.predbat_gateway_ev_session_active"]

    def test_car_charging_now_omitted_when_controlling(self):
        """car_charging_now is not set when gateway_evc_control is True to prevent feedback loop."""
        gw = self._make_gateway(ev_enable=True, num_cars=0, evc_control=True)
        gw._register_ev_car(self._status_with_ev())
        assert "car_charging_now" not in gw._args

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
        gw._ev_max_current = ev_max_current if ev_max_current is not None else {"CP10000001": 32}
        gw._ev_windows = []
        gw._ev_charging_active = False
        gw.local_tz = pytz.timezone("Europe/London")
        return gw

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
        """Windows whose parsed start would be >23 h in the past get their year bumped."""
        import datetime as dt_mod

        gw = self._make_gateway()
        now = dt_mod.datetime.now(gw.local_tz)
        # Simulate a Jan 1 window parsed with current_year when now is Dec 31
        # by injecting a planned entry whose start, parsed with the current year, is 30 h in the past
        stale = now - dt_mod.timedelta(hours=30)
        future_end = stale + dt_mod.timedelta(hours=2)
        # Format as MM-DD HH:MM:SS — these will be parsed with current year and end up in the past
        planned = [{"start": stale.strftime("%m-%d %H:%M:%S"), "end": future_end.strftime("%m-%d %H:%M:%S"), "kwh": 5.0, "average": 20.0, "cost": 1.0}]
        gw.get_state_wrapper = lambda entity, attribute=None: planned if attribute == "planned" else "on"

        gw._refresh_ev_windows()

        assert len(gw._ev_windows) == 1
        start_dt, end_dt = gw._ev_windows[0]
        # After year bump, start should be in the future (next year)
        assert start_dt > now

    def test_apply_sends_start_on_transition(self):
        """_apply_ev_charging_state sends SetChargingProfile then RemoteStartTransaction when entering a window."""
        import asyncio
        import datetime as dt_mod
        import json

        gw = self._make_gateway()
        published = []

        async def fake_publish_raw(topic, payload, **kwargs):
            published.append(json.loads(payload.decode()))

        gw._publish_raw = fake_publish_raw
        gw.topic_ev_command = "predbat/devices/test/ev/command"

        now = dt_mod.datetime.now(gw.local_tz)
        gw._ev_windows = [(now - dt_mod.timedelta(minutes=5), now + dt_mod.timedelta(hours=1))]
        gw._ev_charging_active = False

        asyncio.run(gw._apply_ev_charging_state())

        assert len(published) == 2
        assert published[0] == {"action": "SetChargingProfile", "current_a": 32}
        assert published[1] == {"action": "RemoteStartTransaction", "id_tag": "predbat"}
        assert gw._ev_charging_active is True

    def test_apply_sends_stop_on_transition(self):
        """_apply_ev_charging_state sends RemoteStopTransaction when leaving a window."""
        import asyncio
        import json

        gw = self._make_gateway()
        published = []

        async def fake_publish_raw(topic, payload, **kwargs):
            published.append(json.loads(payload.decode()))

        gw._publish_raw = fake_publish_raw
        gw.topic_ev_command = "predbat/devices/test/ev/command"
        gw._ev_windows = []  # no active window
        gw._ev_charging_active = True  # was charging

        asyncio.run(gw._apply_ev_charging_state())

        assert len(published) == 1
        assert published[0] == {"action": "RemoteStopTransaction"}
        assert gw._ev_charging_active is False

    def test_apply_no_command_when_state_unchanged(self):
        """_apply_ev_charging_state sends nothing when desired state matches last state."""
        import asyncio

        gw = self._make_gateway()
        published = []

        async def fake_publish_raw(topic, payload, **kwargs):
            published.append((topic, payload))

        gw._publish_raw = fake_publish_raw
        gw.topic_ev_command = "predbat/devices/test/ev/command"
        gw._ev_windows = []
        gw._ev_charging_active = False  # already stopped

        asyncio.run(gw._apply_ev_charging_state())

        assert published == []

    def test_max_current_fallback_32a(self):
        """Falls back to 32 A in SetChargingProfile when max_current_a not in cache for the charge point."""
        import asyncio
        import datetime as dt_mod
        import json

        gw = self._make_gateway(ev_max_current={})
        published = []

        async def fake_publish_raw(topic, payload, **kwargs):
            published.append(json.loads(payload.decode()))

        gw._publish_raw = fake_publish_raw
        gw.topic_ev_command = "predbat/devices/test/ev/command"

        now = dt_mod.datetime.now(gw.local_tz)
        gw._ev_windows = [(now - dt_mod.timedelta(minutes=5), now + dt_mod.timedelta(hours=1))]
        gw._ev_charging_active = False

        asyncio.run(gw._apply_ev_charging_state())

        assert published[0]["current_a"] == 32


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


def run_gateway_tests(my_predbat=None):
    """Run all GatewayMQTT tests. Returns True on failure, False on success."""
    from tests.test_gateway_token_refresh import TestIsAuthFailure, TestApplyRefreshResponse, TestMaybeRefreshOnAuthError

    test_classes = [
        TestProtobufDecode,
        TestPlanSerialization,
        TestCommandFormat,
        TestScheduleSlotCommand,
        TestInjectEntities,
        TestDebugLogging,
        TestAutomaticConfig,
        TestEvTelemetry,
        TestEvAutoConfig,
        TestEvInitialize,
        TestEvNeedsReconfigure,
        TestEvControl,
        TestSelectEvent,
        TestNumberEvent,
        TestSwitchEvent,
        TestTokenRefresh,
        TestPlanHookConversion,
        TestMQTTIntegration,
        TestPlanRepublish,
        TestPublishPredbatData,
        TestIanaToPosixTz,
        TestCheckInverterResets,
        TestCheckReadOnlyState,
        TestRunStartupWait,
        TestSetChargeSlotPayload,
        TestIsAuthFailure,
        TestApplyRefreshResponse,
        TestMaybeRefreshOnAuthError,
        TestRateAnchors,
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
    return False
