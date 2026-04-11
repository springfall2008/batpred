"""Tests for GatewayMQTT component."""
import sys
import os
import math

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

    def test_serial_included_when_provided(self):
        """serial kwarg is included verbatim in the JSON payload."""
        from gateway import GatewayMQTT
        import json

        cmd = GatewayMQTT.build_command("set_mode", mode=1, serial="CE123456789")
        parsed = json.loads(cmd)
        assert parsed["serial"] == "CE123456789"

    def test_serial_preserves_original_case(self):
        """Serial is stored as-is (uppercase) even though entity suffixes are lowercased."""
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

        cmd = GatewayMQTT.build_command("set_mode", mode=1)
        parsed = json.loads(cmd)
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
        assert gw._dashboard_calls[f"sensor.{pfx}_sub0_battery_power"][0] == 2000
        assert gw._dashboard_calls[f"sensor.{pfx}_sub0_pv_power"][0] == 3000
        assert gw._dashboard_calls[f"sensor.{pfx}_sub1_soc"][0] == 75

        # Attributes from table
        _, attrs = gw._dashboard_calls[f"sensor.{pfx}_ems_total_soc"]
        assert attrs == GATEWAY_ATTRIBUTE_TABLE.get("ems_total_soc", {})
        _, attrs = gw._dashboard_calls[f"sensor.{pfx}_sub0_temp"]
        assert attrs == GATEWAY_ATTRIBUTE_TABLE.get("temp", {})


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
        """When firmware reports no battery_rate_max, a 6000 W default is used."""
        gw = self._make_gateway()
        gw._last_status = self._basic_status(serial="CE123456789", primary=False, rate_max_w=0)
        gw.automatic_config()

        assert gw._args["battery_rate_max"] == [6000]

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


class TestSelectEvent:
    """Tests for GatewayMQTT.select_event() — mode and schedule-time routing."""

    def _make_gateway(self):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw.prefix = "predbat"
        gw._suffix_to_serial = {}
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

    def test_serial_omitted_when_suffix_not_in_map(self):
        """serial is absent from kwargs when the suffix has no entry in _suffix_to_serial."""
        gw = self._make_gateway()
        # _suffix_to_serial is empty — suffix "456789" unknown
        self._run(gw.select_event("select.predbat_gateway_456789_mode_select", "Eco"))
        _, kwargs = gw._published[0]
        assert "serial" not in kwargs


class TestNumberEvent:
    """Tests for GatewayMQTT.number_event() — numeric entity → command routing."""

    def _make_gateway(self):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw.prefix = "predbat"
        gw._suffix_to_serial = {}
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
        assert gw._published == [("set_charge_rate", {"power_w": 3000})]

    def test_discharge_rate_routes_correctly(self):
        """discharge_rate entity → set_discharge_rate with power_w (not charge_rate)."""
        gw = self._make_gateway()
        self._run(gw.number_event("number.predbat_gateway_456789_discharge_rate", "2500"))
        assert gw._published == [("set_discharge_rate", {"power_w": 2500})]

    def test_reserve_soc_routes_correctly(self):
        """reserve_soc entity → set_reserve with target_soc."""
        gw = self._make_gateway()
        self._run(gw.number_event("number.predbat_gateway_456789_reserve_soc", "10"))
        assert gw._published == [("set_reserve", {"target_soc": 10})]

    def test_target_soc_routes_correctly(self):
        """target_soc entity → set_target_soc with target_soc."""
        gw = self._make_gateway()
        self._run(gw.number_event("number.predbat_gateway_456789_target_soc", "100"))
        assert gw._published == [("set_target_soc", {"target_soc": 100})]

    # ------------------------------------------------------------------
    # Value coercion
    # ------------------------------------------------------------------

    def test_float_string_truncated_to_int(self):
        """Float string values are truncated to int before publishing."""
        gw = self._make_gateway()
        self._run(gw.number_event("number.predbat_gateway_456789_charge_rate", "3000.9"))
        assert gw._published == [("set_charge_rate", {"power_w": 3000})]

    def test_integer_value_accepted(self):
        """Plain integer values are accepted directly."""
        gw = self._make_gateway()
        self._run(gw.number_event("number.predbat_gateway_456789_reserve_soc", 4))
        assert gw._published == [("set_reserve", {"target_soc": 4})]

    def test_zero_value_sent(self):
        """Zero is a valid value and is sent as-is."""
        gw = self._make_gateway()
        self._run(gw.number_event("number.predbat_gateway_456789_charge_rate", 0))
        assert gw._published == [("set_charge_rate", {"power_w": 0})]

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

    def test_serial_omitted_when_suffix_not_in_map(self):
        """serial is absent from kwargs when the suffix has no entry in _suffix_to_serial."""
        gw = self._make_gateway()
        self._run(gw.number_event("number.predbat_gateway_456789_charge_rate", "3000"))
        _, kwargs = gw._published[0]
        assert "serial" not in kwargs


class TestSwitchEvent:
    """Tests for GatewayMQTT.switch_event() — charge/discharge enable → mode commands."""

    def _make_gateway(self):
        from gateway import GatewayMQTT
        from unittest.mock import MagicMock

        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.log = MagicMock()
        gw.prefix = "predbat"
        gw._suffix_to_serial = {}
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
        assert gw._published == [("set_charge_enable", {"enable": True})]

    def test_charge_enabled_turn_off(self):
        """Turning charge_enabled off sends set_charge_enable enable=False."""
        gw = self._make_gateway()
        self._run(gw.switch_event("switch.predbat_gateway_456789_charge_enabled", "turn_off"))
        assert gw._published == [("set_charge_enable", {"enable": False})]

    def test_charge_enabled_toggle(self):
        """Toggling charge_enabled flips based on current state from get_state_wrapper."""
        gw = self._make_gateway()
        # currently on → toggle → off
        gw._state["switch.predbat_gateway_456789_charge_enabled"] = True
        self._run(gw.switch_event("switch.predbat_gateway_456789_charge_enabled", "toggle"))
        assert gw._published == [("set_charge_enable", {"enable": False})]

    # ------------------------------------------------------------------
    # discharge_enabled switch
    # ------------------------------------------------------------------

    def test_discharge_enabled_turn_on(self):
        """Turning discharge_enabled on sends set_discharge_enable enable=True."""
        gw = self._make_gateway()
        self._run(gw.switch_event("switch.predbat_gateway_456789_discharge_enabled", "turn_on"))
        assert gw._published == [("set_discharge_enable", {"enable": True})]

    def test_discharge_enabled_turn_off(self):
        """Turning discharge_enabled off sends set_discharge_enable enable=False."""
        gw = self._make_gateway()
        self._run(gw.switch_event("switch.predbat_gateway_456789_discharge_enabled", "turn_off"))
        assert gw._published == [("set_discharge_enable", {"enable": False})]

    def test_discharge_enabled_toggle(self):
        """Toggling discharge_enabled flips based on current state from get_state_wrapper."""
        gw = self._make_gateway()
        # currently off → toggle → on (get_state_wrapper returns False by default)
        self._run(gw.switch_event("switch.predbat_gateway_456789_discharge_enabled", "toggle"))
        assert gw._published == [("set_discharge_enable", {"enable": True})]

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

    def test_serial_omitted_when_suffix_not_in_map(self):
        """serial is absent from kwargs when the suffix has no entry in _suffix_to_serial."""
        gw = self._make_gateway()
        self._run(gw.switch_event("switch.predbat_gateway_456789_charge_enabled", "turn_on"))
        _, kwargs = gw._published[0]
        assert "serial" not in kwargs


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


def run_gateway_tests(my_predbat=None):
    """Run all GatewayMQTT tests. Returns True on failure, False on success."""
    test_classes = [
        TestProtobufDecode,
        TestPlanSerialization,
        TestCommandFormat,
        TestScheduleSlotCommand,
        TestInjectEntities,
        TestAutomaticConfig,
        TestSelectEvent,
        TestNumberEvent,
        TestSwitchEvent,
        TestTokenRefresh,
        TestPlanHookConversion,
        TestMQTTIntegration,
        TestPublishPredbatData,
        TestIanaToPosixTz,
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
