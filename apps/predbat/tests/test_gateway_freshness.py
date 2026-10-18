"""Stale selected battery data must not support planning or new control writes."""

import asyncio
import time
from types import SimpleNamespace, MethodType
from unittest.mock import AsyncMock, MagicMock

import gateway_status_pb2 as pb
from gateway import GatewayMQTT
from execute import Execute
from predbat_metrics import metrics


def fresh_gateway():
    """Build an initialised controller with a fresh, connected battery."""
    gw = GatewayMQTT.__new__(GatewayMQTT)
    gw.base = MagicMock()
    gw.args = {}
    gw.prefix = "predbat"
    gw.log = MagicMock()
    gw.initialize(gateway_device_id="pbgw_test", mqtt_host="mqtt.test")
    gw._mqtt_connected = True
    gw._gateway_online = True
    gw._last_telemetry_time = time.time()
    gw._last_status = pb.GatewayStatus(device_id="pbgw_test", timestamp=int(time.time()))
    inv = gw._last_status.inverters.add(serial="BATTERY", type=pb.INVERTER_TYPE_GIVENERGY, connected=True, primary=True)
    inv.battery.soc_percent = 50
    inv.battery.capacity_wh = 10000
    gw._inverter_slot_serials = ["BATTERY"]
    gw._discovery = {"BATTERY": {"serial": "BATTERY", "battery": True, "type": pb.INVERTER_TYPE_GIVENERGY}}
    gw._publish_raw = AsyncMock()
    return gw


class TestGatewayFreshness:
    """Check physical input health, planning gates and autonomous plan refreshes."""

    def test_fresh_battery_is_healthy(self):
        """Source time, receipt time and connected battery readings agree."""
        gw = fresh_gateway()
        assert gw.inverter_data_error() is None
        assert gw.is_alive()

    def test_transport_and_source_failures_are_unhealthy(self):
        """A broker, LWT, retained replay or missing source cannot mask an outage."""
        for mutation in (
            lambda gw: setattr(gw, "_mqtt_connected", False),
            lambda gw: setattr(gw, "_gateway_online", False),
            lambda gw: setattr(gw, "_last_telemetry_time", time.time() - 121),
            lambda gw: setattr(gw._last_status, "timestamp", int(time.time()) - 121),
            lambda gw: setattr(gw._last_status, "timestamp", 0),
            lambda gw: setattr(gw._last_status, "timestamp", int(time.time()) + 121),
            lambda gw: setattr(gw, "_last_status", None),
        ):
            gw = fresh_gateway()
            mutation(gw)
            assert gw.inverter_data_error()
            assert not gw.is_alive()

    def test_battery_missing_disconnected_or_without_readings_fails(self):
        """Fresh envelope data cannot make a lost battery usable."""
        for mutation in (
            lambda status: status.ClearField("inverters"),
            lambda status: setattr(status.inverters[0], "connected", False),
            lambda status: status.inverters[0].ClearField("battery"),
        ):
            gw = fresh_gateway()
            mutation(gw._last_status)
            assert gw.inverter_data_error()

    def test_offline_pv_is_ignored(self):
        """A separate PV-only unit may sleep overnight without failing battery health."""
        gw = fresh_gateway()
        gw._last_status.inverters.add(serial="PV", type=pb.INVERTER_TYPE_GIVENERGY, connected=False)
        gw._discovery["PV"] = {"serial": "PV", "battery": False, "type": pb.INVERTER_TYPE_GIVENERGY}
        assert gw.inverter_data_error() is None

    def test_coordinator_does_not_hide_missing_battery(self):
        """A fresh EMS aggregate cannot replace an expected battery member."""
        gw = fresh_gateway()
        ems = gw._last_status.inverters.add(serial="EMS", type=pb.INVERTER_TYPE_GIVENERGY_EMS, connected=True)
        ems.ems.total_soc = 50
        gw._inverter_slot_serials = ["EMS"]
        assert gw.inverter_data_error() is None
        gw._last_status.inverters[0].connected = False
        assert "BATTERY" in gw.inverter_data_error()
        gw._last_status.inverters[0].connected = True
        ems.connected = False
        assert "EMS" in gw.inverter_data_error()
        ems.connected = True
        ems.ClearField("ems")
        assert "EMS" in gw.inverter_data_error()

    def test_optional_display_cannot_fail_selected_cloud_provider(self):
        """An offline display has no responsibility for battery data."""
        gw = fresh_gateway()
        gw.gateway_display_only = True
        gw.api_started = True
        gw._mqtt_connected = False
        gw._gateway_online = False
        gw._last_status = None
        assert gw.inverter_data_error() is None
        assert gw.is_alive()

    def test_stale_data_invalidates_metric_before_reads_and_execution(self):
        """Neither path can touch cached inverter data after the source stops."""
        gw = fresh_gateway()
        gw._gateway_online = False
        for operation in (Execute.fetch_inverter_data, Execute.execute_plan):
            base = SimpleNamespace(components=SimpleNamespace(get_component=lambda name: gw), plan_valid=True, record_status=MagicMock())
            base.check_inverter_data_freshness = MethodType(Execute.check_inverter_data_freshness, base)
            metrics().plan_valid.set(1)
            result = operation(base)
            assert not base.plan_valid
            assert metrics().plan_valid._value.get() == 0
            assert result is False or result[0].startswith("Error:")
            assert "Hub is offline" in base.record_status.call_args.args[0]
            assert base.record_status.call_args.kwargs["had_errors"]

    def test_main_cycle_stops_before_sensor_fetch(self):
        """The real engine cycle preserves the precise error and skips planning."""
        from predbat import PredBat

        gw = fresh_gateway()
        gw._last_status.timestamp = int(time.time()) - 3600
        base = SimpleNamespace(components=SimpleNamespace(get_component=lambda name: gw), plan_valid=True, record_status=MagicMock())
        for name in ("reset_run_status", "update_time", "save_current_config", "expose_config", "fetch_config_options", "fetch_sensor_data", "calculate_plan", "execute_plan"):
            setattr(base, name, MagicMock())
        base.get_arg = lambda name, default=None: "test" if name == "user_id" else default
        base.is_template_mode = lambda: False
        base.check_inverter_data_freshness = MethodType(Execute.check_inverter_data_freshness, base)
        PredBat.update_pred(base)
        assert not base.plan_valid
        base.fetch_sensor_data.assert_not_called()
        base.calculate_plan.assert_not_called()
        base.execute_plan.assert_not_called()
        assert "source timestamp" in base.record_status.call_args.args[0]

    def test_stale_commands_blocked_but_safety_read_only_allowed(self):
        """Housekeeping cannot reset or release an inverter on stale data."""
        gw = fresh_gateway()
        gw._gateway_online = False
        for command, kwargs in (("inverter_reset", {"serial": "BATTERY"}), ("set_charge_rate", {"power_w": 3000}), ("set_read_only", {"enable": False})):
            try:
                asyncio.run(gw.publish_command(command, **kwargs))
            except RuntimeError as error:
                assert "Hub is offline" in str(error)
            else:
                raise AssertionError("Stale inverter command was allowed")
        gw._publish_raw.assert_not_awaited()
        asyncio.run(gw.publish_command("set_read_only", enable=True))
        gw._publish_raw.assert_awaited_once()

    def test_recovery_requires_new_plan_before_cached_refresh(self):
        """Reconnection cannot re-arm the old autonomous plan behind the engine's back."""
        gw = fresh_gateway()
        asyncio.run(gw.publish_plan([], "Europe/London"))
        assert gw._last_plan_entries == []
        gw._publish_raw.reset_mock()
        gw._gateway_online = False
        asyncio.run(gw._republish_plan_if_stale())
        assert gw._last_plan_entries is None
        gw._gateway_online = True
        asyncio.run(gw._republish_plan_if_stale())
        gw._publish_raw.assert_not_awaited()
        asyncio.run(gw.publish_plan([], "Europe/London"))
        gw._publish_raw.assert_awaited_once()

    def test_stale_pending_plan_cannot_be_published(self):
        """A queued execution result may expire before the component handles it."""
        gw = fresh_gateway()
        gw._last_status.timestamp = int(time.time()) - 121
        gw._pending_plan = ([], "Europe/London")
        asyncio.run(gw.publish_plan([], "Europe/London"))
        assert gw._pending_plan is None
        gw._publish_raw.assert_not_awaited()

    def test_unselected_battery_does_not_block_direct_target(self):
        """A non-primary unit outside this site's selection is not a dependency."""
        gw = fresh_gateway()
        gw._discovery["OTHER"] = {"serial": "OTHER", "battery": True, "type": pb.INVERTER_TYPE_GIVENERGY}
        assert gw.inverter_data_error() is None
        gw.gateway_inverter_serial = ["BATTERY", "OTHER"]
        assert "OTHER" in gw.inverter_data_error()

    def test_stale_housekeeping_can_discard_plan_without_releasing_control(self):
        """A blocked read-only release cannot abort cleanup of the old schedule."""
        gw = fresh_gateway()
        gw._pending_plan = ([], "Europe/London")
        gw._last_plan_entries = []
        gw._gateway_online = False
        gw.get_arg = lambda name, default=None: False if name == "set_read_only" else default
        gw.publish_command = AsyncMock()
        asyncio.run(gw._check_read_only_state())
        gw.publish_command.assert_not_awaited()
        asyncio.run(gw._republish_plan_if_stale())
        assert gw._pending_plan is None
        assert gw._last_plan_entries is None

    def test_unbound_secondary_gateway_is_not_a_battery_dependency(self):
        """A secondary Gateway with partial battery fields must not gate its AIO."""
        gw = fresh_gateway()
        gw.gateway_inverter_serial = ["BATTERY", "SECONDARY"]
        gw._discovery["SECONDARY"] = {"serial": "SECONDARY", "battery": True, "type": pb.INVERTER_TYPE_GIVENERGY_GATEWAY}
        inv = gw._last_status.inverters.add(serial="SECONDARY", type=pb.INVERTER_TYPE_GIVENERGY_GATEWAY, connected=False)
        inv.battery.rate_max_w = 38
        assert gw.inverter_data_error() is None

    def test_source_timestamp_errors_distinguish_clock_and_connectivity(self):
        """Explain clock skew without assuming an old timestamp is only a clock fault."""
        for offset, expected in ((-300, "old; check Hub connectivity and clock synchronisation"), (300, "ahead; check Hub and PredBat clock synchronisation")):
            gw = fresh_gateway()
            gw._last_status.timestamp = int(time.time()) + offset
            assert expected in gw.inverter_data_error()
            assert not gw.is_alive()
        gw = fresh_gateway()
        gw._last_status.timestamp = 0
        assert "no valid source timestamp; check Hub time synchronisation" in gw.inverter_data_error()
        assert not gw.is_alive()

    def test_parallel_gateway_rejects_cached_disconnected_member(self):
        """A fresh coordinator envelope cannot validate a member's cached readings."""
        gw = fresh_gateway()
        gw.gateway_inverter_serial = ["COORDINATOR", "BATTERY", "BATTERY2"]
        gw._inverter_slot_serials = ["COORDINATOR"]
        coordinator = gw._last_status.inverters.add(serial="COORDINATOR", type=pb.INVERTER_TYPE_GIVENERGY_GATEWAY, connected=True)
        coordinator.battery.soc_percent = 50
        member = gw._last_status.inverters.add(serial="BATTERY2", type=pb.INVERTER_TYPE_GIVENERGY, connected=True)
        member.battery.soc_percent = 60
        member.battery.capacity_wh = 10000
        gw._discovery["BATTERY2"] = {"serial": "BATTERY2", "battery": True, "type": pb.INVERTER_TYPE_GIVENERGY}
        assert gw.inverter_data_error() is None
        member.connected = False
        assert member.battery.ByteSize() > 0
        assert gw._last_status.timestamp > time.time() - 2
        assert "BATTERY2 is disconnected" in gw.inverter_data_error()
        member.connected = True
        assert gw.inverter_data_error() is None
