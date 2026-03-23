"""Tests for GatewayMQTT component."""
try:
    import pytest
except ImportError:
    pytest = None
import sys
import os
import math

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from proto import gateway_status_pb2 as pb

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

    def test_entity_mapping(self):
        from gateway import GatewayMQTT

        status = self._make_status()
        data = status.SerializeToString()

        entities = GatewayMQTT.decode_telemetry(data)

        assert entities["predbat_gateway_soc"] == 50
        assert entities["predbat_gateway_battery_power"] == 1000
        assert entities["predbat_gateway_pv_power"] == 2000
        assert entities["predbat_gateway_grid_power"] == -500
        assert entities["predbat_gateway_load_power"] == 1500
        assert approx_equal(entities["predbat_gateway_battery_voltage"], 51.2, abs_tol=0.1)
        assert approx_equal(entities["predbat_gateway_battery_current"], 19.5, abs_tol=0.1)
        assert approx_equal(entities["predbat_gateway_battery_temp"], 22.5, abs_tol=0.1)
        assert entities["predbat_gateway_battery_soh"] == 98
        assert entities["predbat_gateway_battery_cycles"] == 150
        assert entities["predbat_gateway_battery_capacity"] == 9.5
        assert approx_equal(entities["predbat_gateway_grid_voltage"], 242.5, abs_tol=0.1)
        assert approx_equal(entities["predbat_gateway_grid_frequency"], 50.01, abs_tol=0.01)
        assert entities["predbat_gateway_inverter_power"] == 1800
        assert approx_equal(entities["predbat_gateway_inverter_temp"], 35.0, abs_tol=0.1)
        assert entities["predbat_gateway_mode"] == 0
        assert entities["predbat_gateway_charge_enabled"] is True
        assert entities["predbat_gateway_discharge_enabled"] is True
        assert entities["predbat_gateway_charge_rate"] == 3000
        assert entities["predbat_gateway_discharge_rate"] == 3000
        assert entities["predbat_gateway_reserve"] == 4
        assert entities["predbat_gateway_target_soc"] == 100
        assert entities["predbat_gateway_charge_start"] == 130
        assert entities["predbat_gateway_charge_end"] == 430
        assert entities["predbat_gateway_discharge_start"] == 1600
        assert entities["predbat_gateway_discharge_end"] == 1900


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
        assert plan.timezone == "Europe/London"
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

        cmd = GatewayMQTT.build_command("set_mode", mode=1, power_w=3000, target_soc=100)
        import json

        parsed = json.loads(cmd)
        assert parsed["command"] == "set_mode"
        assert parsed["mode"] == 1
        assert parsed["power_w"] == 3000
        assert parsed["target_soc"] == 100
        assert "command_id" in parsed
        assert "expires_at" in parsed
        import time

        assert abs(parsed["expires_at"] - int(time.time())) < 310

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


class TestEMSEntities:
    def test_ems_aggregate_entities(self):
        """EMS type produces aggregate entities."""
        status = pb.GatewayStatus()
        status.device_id = "pbgw_ems_test"
        status.timestamp = 1741789200
        status.schema_version = 1
        status.dongle_count = 1

        inv = status.inverters.add()
        inv.type = pb.INVERTER_TYPE_GIVENERGY_EMS
        inv.serial = "EM1234"
        inv.connected = True
        inv.active = True

        inv.ems.num_inverters = 2
        inv.ems.total_soc = 60
        inv.ems.total_charge_w = 3000
        inv.ems.total_pv_w = 5000
        inv.ems.total_grid_w = -1000
        inv.ems.total_load_w = 4000

        sub0 = inv.ems.sub_inverters.add()
        sub0.soc = 55
        sub0.battery_w = 1500
        sub0.pv_w = 2500
        sub1 = inv.ems.sub_inverters.add()
        sub1.soc = 65
        sub1.battery_w = 1500
        sub1.pv_w = 2500

        from gateway import GatewayMQTT

        entities = GatewayMQTT.decode_telemetry(status.SerializeToString())

        # EMS aggregate entities
        assert entities.get("predbat_gateway_ems_total_soc") == 60
        assert entities.get("predbat_gateway_ems_total_pv") == 5000
        assert entities.get("predbat_gateway_ems_total_load") == 4000
        # Per-sub-inverter
        assert entities.get("predbat_gateway_sub0_soc") == 55
        assert entities.get("predbat_gateway_sub1_soc") == 65
        assert entities.get("predbat_gateway_sub0_battery_power") == 1500


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
        assert plan.timezone == "Europe/London"

        # Verify plan_version is monotonically increasing
        data2 = GatewayMQTT.build_execution_plan(entries, plan_version=2, timezone="Europe/London")
        plan2 = pb.ExecutionPlan()
        plan2.ParseFromString(data2)
        assert plan2.plan_version > plan.plan_version


def run_gateway_tests(my_predbat=None):
    """Run all GatewayMQTT tests. Returns True on failure, False on success."""
    test_classes = [
        TestProtobufDecode,
        TestPlanSerialization,
        TestCommandFormat,
        TestScheduleSlotCommand,
        TestEMSEntities,
        TestTokenRefresh,
        TestPlanHookConversion,
        TestMQTTIntegration,
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
