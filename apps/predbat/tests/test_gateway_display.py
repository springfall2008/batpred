"""Display-only gateway isolation and lifecycle regressions."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import gateway_status_pb2 as pb
from gateway import GatewayMQTT


class TestGatewayDisplayOnly:
    """A display cannot become an inverter source or publish control messages."""

    def _make_gateway(self, display=True):
        """Build the real gateway state with an in-memory MQTT client."""
        gw = GatewayMQTT.__new__(GatewayMQTT)
        gw.base = MagicMock()
        gw.args = {}
        gw.log = MagicMock()
        gw.initialize(gateway_device_id="pbgw_test", mqtt_host="mqtt.test", gateway_display_only=display)
        gw._mqtt_connected = True
        gw._mqtt_client = SimpleNamespace(publish=AsyncMock())
        gw.api_started = False
        gw.api_stop = False
        gw.update_success_timestamp = MagicMock()
        return gw

    def test_zero_inverters_is_a_ready_display(self):
        """A status with no inverter completes display startup without changing control."""
        gw = self._make_gateway()
        gw.automatic_config = MagicMock()
        gw._inject_entities = MagicMock()
        status = pb.GatewayStatus(device_id="pbgw_test")
        gw._process_telemetry(status.SerializeToString())
        assert gw.api_started
        assert not gw._auto_configured
        gw.automatic_config.assert_not_called()
        gw._inject_entities.assert_not_called()
        gw.update_success_timestamp.assert_called_once()

    def test_discovered_inverter_cannot_replace_cloud(self):
        """Even full telemetry never registers inverters or EVs in display mode."""
        gw = self._make_gateway()
        gw.set_arg = MagicMock()
        status = pb.GatewayStatus(device_id="pbgw_test")
        status.inverters.add(serial="CE12345678", connected=True)
        status.ev_chargers.add(charge_point_id="charger")
        gw._process_telemetry(status.SerializeToString())
        gw.automatic_config()
        gw.set_arg.assert_not_called()
        assert not gw._auto_configured
        assert not gw._configured_ev_chargers

    def test_invalid_telemetry_does_not_start_display(self):
        """A corrupt message must not satisfy the readiness condition."""
        gw = self._make_gateway()
        gw._process_telemetry(b"\xff")
        assert not gw.api_started
        assert gw._last_status is None

    def test_display_only_allows_display_topic(self):
        """The final MQTT boundary blocks all command and schedule paths."""
        gw = self._make_gateway()

        async def exercise():
            for suffix in ("command", "ev/command", "schedule", "unknown"):
                await gw._publish_raw(f"{gw._topic_base}/{suffix}", b"control", retain=True)
            await gw._publish_raw(f"{gw._topic_base}/predbat_data", b"display", retain=True)

        asyncio.run(exercise())
        gw._mqtt_client.publish.assert_awaited_once_with(f"{gw._topic_base}/predbat_data", b"display", qos=1, retain=True)

    def test_public_control_entry_points_cannot_send(self):
        """Direct callers cannot bypass display mode through control helpers."""
        gw = self._make_gateway()

        async def exercise():
            await gw.publish_command("set_read_only", enable=False)
            await gw._send_ev_command({"action": "RemoteStartTransaction"})
            await gw.publish_plan([], "Europe/London")

        asyncio.run(exercise())
        gw._mqtt_client.publish.assert_not_awaited()
        assert gw._last_plan_data is None

    def test_plan_hook_does_not_queue_execution(self):
        """Display plans travel through JSON, never the autonomous schedule hook."""
        gw = self._make_gateway()
        gw._on_plan_executed()
        assert gw._pending_plan is None

    def test_display_housekeeping_never_releases_standby(self):
        """An online display publishes JSON and refreshes auth, without control housekeeping."""
        gw = self._make_gateway()
        gw._last_status = pb.GatewayStatus(device_id="pbgw_test")
        gw._publish_predbat_data = AsyncMock()
        gw._check_token_refresh = AsyncMock()
        gw._check_read_only_state = AsyncMock()
        gw._check_inverter_resets = AsyncMock()
        gw._check_rate_caps = AsyncMock()
        gw._republish_plan_if_stale = AsyncMock()
        with patch("gateway.HAS_AIOMQTT", True):
            assert asyncio.run(gw.run(60, False))
        gw._publish_predbat_data.assert_awaited_once()
        gw._check_token_refresh.assert_awaited_once()
        for method in (gw._check_read_only_state, gw._check_inverter_resets, gw._check_rate_caps, gw._republish_plan_if_stale):
            method.assert_not_awaited()

    def test_display_startup_does_not_wait_for_auto_config(self):
        """Display startup accepts zero-inverter telemetry, using the same bounded budget."""
        gw = self._make_gateway()
        gw.api_started = True
        gw._first_connection_attempted = True
        gw._startup_wait = AsyncMock(side_effect=lambda ticks, ready: ticks if ready() else 999)
        gw._mqtt_loop = AsyncMock()
        with patch("gateway.HAS_AIOMQTT", True):
            assert asyncio.run(gw.run(0, True))
        assert all(call.args[1]() for call in gw._startup_wait.call_args_list)
        assert not gw._auto_configured

    def test_display_is_not_an_inverter_source(self):
        """A display cannot make the engine wait for battery readings it will never supply."""
        from components import Components

        comps = Components.__new__(Components)
        comps.components = {"gateway": self._make_gateway()}
        comps.load_error = lambda name: None
        comps.is_alive = lambda name: True
        assert not comps.inverter_source_active()
        assert comps.inverter_source_names() == []
        assert comps.inverter_source_status() == []
        comps.components["gateway"].gateway_display_only = False
        assert comps.inverter_source_active()
        assert comps.inverter_source_names() == ["PredBat Gateway"]
        assert comps.inverter_source_status() == ["PredBat Gateway (OK)"]

    def test_false_string_keeps_existing_control_mode(self):
        """String config values are parsed rather than treated as truthy strings."""
        assert not self._make_gateway("false").gateway_display_only
        assert self._make_gateway("true").gateway_display_only

    def test_invalid_display_flag_fails_closed(self):
        """An invalid role value must not silently enable inverter control."""
        try:
            self._make_gateway("typo")
        except ValueError:
            return
        raise AssertionError("Invalid display flag was accepted")

    def test_control_mode_can_still_publish(self):
        """The opt-in mode does not block the existing control transport."""
        gw = self._make_gateway(False)
        asyncio.run(gw._publish_raw(gw.topic_command, b"control"))
        gw._mqtt_client.publish.assert_awaited_once()
