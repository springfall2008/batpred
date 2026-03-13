"""Tests for GatewayMQTT component."""
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from proto import gateway_status_pb2 as pb


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
        assert decoded.inverters[0].grid.voltage_v == pytest.approx(242.5, abs=0.1)
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
        assert entities["predbat_gateway_battery_voltage"] == pytest.approx(51.2, abs=0.1)
        assert entities["predbat_gateway_battery_current"] == pytest.approx(19.5, abs=0.1)
        assert entities["predbat_gateway_battery_temp"] == pytest.approx(22.5, abs=0.1)
        assert entities["predbat_gateway_battery_soh"] == 98
        assert entities["predbat_gateway_battery_cycles"] == 150
        assert entities["predbat_gateway_battery_capacity"] == 9500
        assert entities["predbat_gateway_grid_voltage"] == pytest.approx(242.5, abs=0.1)
        assert entities["predbat_gateway_grid_frequency"] == pytest.approx(50.01, abs=0.01)
        assert entities["predbat_gateway_inverter_power"] == 1800
        assert entities["predbat_gateway_inverter_temp"] == pytest.approx(35.0, abs=0.1)
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
