"""Exercise Hub writes through the real standalone service and readback path."""

# cspell:words acks SUBACK

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytz

from predbat import PredBat
from components import Components
from gateway import GatewayMQTT
from ha import HAInterface
from inverter import Inverter
import gateway_status_pb2 as pb


def make_status(aio_count=2, coordinator_connected=True):
    """Make topology explicit, including live connection flags."""
    status = pb.GatewayStatus(device_id="pbgw_regression", firmware="1.0.11", timestamp=int(time.time()), schema_version=1)
    coordinator = status.inverters.add(type=pb.INVERTER_TYPE_GIVENERGY_GATEWAY, serial="GW0000G001", connected=coordinator_connected, active=coordinator_connected, managed=True)
    coordinator.control.reserve_soc = 4
    coordinator.control.discharge_enabled = True
    for index in range(aio_count):
        inv = status.inverters.add(type=pb.INVERTER_TYPE_GIVENERGY, serial=f"CH0000A00{index}", connected=True, active=True, primary=True, managed=True)
        inv.battery.capacity_wh = 13500
        inv.battery.soc_percent = 50
        inv.battery.rate_max_w = 6000
        inv.control.reserve_soc = 4
        inv.control.discharge_enabled = True
    return status


@contextmanager
def write_path(status, bus_ok=True, ack_ok=None, send_ack=True, ack_error="modbus_write_failed"):
    """Run production dispatch, simulating only the MQTT device and physical bus."""
    base = PredBat.__new__(PredBat)
    base.args = {}
    base.prefix = "predbat"
    base.local_tz = pytz.UTC
    base.CONFIG_ITEMS = []
    base.control_ledger = None
    base.log = MagicMock()
    base.record_status = MagicMock()
    base.register_hook = MagicMock()
    states = {}
    base.dashboard_item = lambda entity, state, attributes=None, **kwargs: states.__setitem__(entity, state)
    base.get_state_wrapper = lambda entity_id, **kwargs: states.get(entity_id)
    base.unit_conversion = lambda entity, value, *args, **kwargs: value
    base.set_arg = lambda key, value: base.args.__setitem__(key, value)
    base.define_service_list()
    gateway = GatewayMQTT(base, gateway_device_id=status.device_id)
    gateway._mqtt_connected = True
    gateway._gateway_online = True
    gateway._ack_subscribed = True
    manager = Components.__new__(Components)
    manager.base = base
    manager.components = {"gateway": gateway}
    base.components = manager
    ha = HAInterface.__new__(HAInterface)
    ha.base = base
    ha.websocket_active = False
    base.ha_interface = ha
    inv = Inverter.__new__(Inverter)
    inv.base = base
    inv.id = 0
    inv.log = base.log
    inv.count_register_writes = 0
    inv.inv_write_and_poll_sleep = 2
    inv.sleep = MagicMock()
    published = []
    owner_loop = asyncio.new_event_loop()
    owner_thread = threading.Thread(target=owner_loop.run_forever, daemon=True)
    owner_thread.start()
    gateway._loop = owner_loop

    class Device:
        """Model unchanged telemetry when the inverter does not accept a write."""

        async def publish(self, topic, payload, **kwargs):
            """Apply the physical result, then deliver firmware's JSON ACK."""
            assert asyncio.get_running_loop() is owner_loop
            command = json.loads(payload)
            published.append(command)
            target = next((entry for entry in status.inverters if entry.serial == command.get("serial")), None)
            applied = bool(bus_ok and target is not None and target.connected and target.active)
            if applied:
                if command["command"] == "set_reserve":
                    target.control.reserve_soc = command["target_soc"]
                elif command["command"] == "set_discharge_enable":
                    target.control.discharge_enabled = command["enable"]
                elif command["command"] == "set_charge_slot":
                    schedule = json.loads(command["schedule_json"])
                    target.schedule.charge_start = schedule.get("start", target.schedule.charge_start)
                    target.schedule.charge_end = schedule.get("end", target.schedule.charge_end)
                gateway._process_telemetry(status.SerializeToString())
            if send_ack:
                ack = {"command": command["command"], "command_id": command["command_id"], "ok": applied if ack_ok is None else ack_ok, "error": ack_error}
                message = type("Message", (), {"topic": f"{gateway._topic_base}/ack/{command['command_id']}", "payload": json.dumps(ack).encode(), "retain": False})()
                await gateway._handle_message(message)

    gateway._mqtt_client = Device()
    try:
        gateway._process_telemetry(status.SerializeToString())
        gateway._process_telemetry(status.SerializeToString())
        yield base, gateway, inv, published, states
    finally:
        owner_loop.call_soon_threadsafe(owner_loop.stop)
        owner_thread.join(timeout=2)
        assert not owner_thread.is_alive()
        owner_loop.close()


class TestGatewayWriteResults:
    """Reproduce observed retry exhaustion through the real write/readback path."""

    def test_single_aio_uses_live_aio_and_verifies_write(self):
        """Healthy single-AIO control completes after one service dispatch."""
        with write_path(make_status(aio_count=1, coordinator_connected=False)) as (base, gateway, inv, published, states):
            assert inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
            assert len(published) == 1
            assert published[0]["serial"] == "CH0000A000"
            assert inv.count_register_writes == 1

    def test_disconnected_coordinator_fails_without_publishing_or_rerouting(self):
        """Healthy child telemetry cannot make a disconnected coordinator writable."""
        with write_path(make_status(coordinator_connected=False)) as (base, gateway, inv, published, states):
            assert not inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
            assert published == [], f"Disconnected coordinator received {len(published)} writes"
            assert inv.sleep.call_count == 0

    def test_same_serial_health_transition_blocks_cached_control(self):
        """Health is checked after configuration even when serials do not change."""
        status = make_status()
        with write_path(status) as (base, gateway, inv, published, states):
            status.inverters[0].connected = False
            status.inverters[0].active = False
            gateway._process_telemetry(status.SerializeToString())
            assert gateway._auto_configured
            assert not inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
            assert published == []

    def test_modbus_nack_stops_real_readback_retry_loop(self):
        """A device rejection crosses UI dispatch back to the inverter verifier."""
        with write_path(make_status(), bus_ok=False) as (base, gateway, inv, published, states):
            assert not inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
            assert len(published) == 1, f"NACK caused {len(published)} blind write attempts"
            assert inv.sleep.call_count > 0
            assert any("modbus_write_failed" in str(call) for call in base.log.call_args_list)

    def test_switch_nack_stops_real_readback_retry_loop(self):
        """The production discharge-enable failure follows the same rejection path."""
        with write_path(make_status(), bus_ok=False) as (base, gateway, inv, published, states):
            assert not inv.write_and_poll_switch("discharge_enable", base.args["scheduled_discharge_enable"][0], False)
            assert len(published) == 1, f"NACK caused {len(published)} blind switch attempts"
            assert inv.sleep.call_count > 0

    def test_positive_ack_without_changed_readback_still_fails(self):
        """An ACK cannot replace telemetry proving that the requested value was read."""
        with write_path(make_status(), bus_ok=False, ack_ok=True) as (base, gateway, inv, published, states):
            assert not inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
            assert len(published) == 10
            assert any("didn't complete got 4.0" in str(call) for call in base.log.call_args_list)
            assert inv.count_register_writes == 0

    def test_wrong_or_retained_ack_cannot_complete_write_before_matching_ack(self):
        """Only the delayed matching ACK can release a real cross-loop service call."""
        for bad_kind in ("id", "topic", "command", "retained", "ok_type"):
            delivered = threading.Event()
            with write_path(make_status(), send_ack=False) as (base, gateway, inv, published, states):
                original_publish = gateway._mqtt_client.publish

                async def publish(topic, payload, **kwargs):
                    """Send a misleading ACK immediately and the correct one later."""
                    await original_publish(topic, payload, **kwargs)
                    command = json.loads(payload)
                    ack = {"command": command["command"], "command_id": command["command_id"], "ok": True}
                    ack_topic = f"{gateway._topic_base}/ack/{command['command_id']}"
                    bad_ack = dict(ack)
                    bad_topic = ack_topic
                    if bad_kind == "id":
                        bad_ack["command_id"] = "old-command"
                        bad_topic = f"{gateway._topic_base}/ack/old-command"
                    elif bad_kind == "topic":
                        bad_topic = f"{gateway._topic_base}/ack/different-topic-id"
                    elif bad_kind == "command":
                        bad_ack["command"] = "set_charge_rate"
                    elif bad_kind == "ok_type":
                        bad_ack["ok"] = "true"
                    await gateway._handle_message(SimpleNamespace(topic=bad_topic, payload=json.dumps(bad_ack).encode(), retain=bad_kind == "retained"))

                    async def deliver():
                        """Deliver the legitimate ACK after client.publish has returned."""
                        delivered.set()
                        await gateway._handle_message(SimpleNamespace(topic=ack_topic, payload=json.dumps(ack).encode(), retain=False))

                    asyncio.get_running_loop().call_later(0.01, lambda: asyncio.create_task(deliver()))

                gateway._mqtt_client.publish = publish
                assert inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
                assert delivered.is_set(), f"Unrelated ACK completed the write: {bad_kind}"
                assert len(published) == 1
                assert gateway._pending_command_acks == {}

    def test_missing_ack_with_successful_readback_is_success(self):
        """QoS0 ACK loss does not discard a value proven by subsequent telemetry."""
        with patch("gateway._COMMAND_ACK_TIMEOUT", 0.01):
            with write_path(make_status(), send_ack=False) as (base, gateway, inv, published, states):
                assert inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
                assert len(published) == 1
                assert gateway._pending_command_acks == {}

    def test_missing_ack_and_unchanged_readback_never_resends(self):
        """An unknown result is polled once, without filling the Hub command queue."""
        with patch("gateway._COMMAND_ACK_TIMEOUT", 0.01):
            with write_path(make_status(), bus_ok=False, send_ack=False) as (base, gateway, inv, published, states):
                assert not inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
                assert len(published) == 1
                assert inv.sleep.call_count > 0
                assert gateway._pending_command_acks == {}

    def test_disconnect_wakes_pending_write_as_unknown_without_replaying(self):
        """A disconnect cannot strand a waiter or repeat an in-flight physical write."""
        with write_path(make_status(), bus_ok=False, send_ack=False) as (base, gateway, inv, published, states):
            original_publish = gateway._mqtt_client.publish

            async def publish(topic, payload, **kwargs):
                """Disconnect immediately after broker delivery, before any device ACK."""
                await original_publish(topic, payload, **kwargs)
                asyncio.get_running_loop().call_soon(gateway._fail_pending_command_acks, "test connection lost")

            gateway._mqtt_client.publish = publish
            assert not inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
            assert len(published) == 1
            assert gateway._pending_command_acks == {}
            assert not gateway._ack_subscribed

    def test_cancelled_control_wait_removes_pending_future(self):
        """Cancelling the owner-loop operation leaves no pending correlation state."""
        with write_path(make_status(), bus_ok=False, send_ack=False) as (base, gateway, inv, published, states):

            async def cancel_write():
                """Start and cancel a command once it reaches its ACK wait."""
                task = asyncio.create_task(gateway.publish_command("set_reserve", target_soc=10, serial="GW0000G001"))
                await asyncio.sleep(0)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                assert gateway._pending_command_acks == {}

            asyncio.run_coroutine_threadsafe(cancel_write(), gateway._loop).result(timeout=2)
            assert len(published) == 1

    def test_command_ids_are_unique_across_component_restarts(self):
        """A delayed result cannot collide with a new process counter."""
        command_ids = []
        for _ in range(2):
            with write_path(make_status()) as (base, gateway, inv, published, states):
                assert inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
                command_ids.append(published[0]["command_id"])
        assert len(set(command_ids)) == 2
        assert all(command_id.startswith("PBAT") and len(command_id) == 36 for command_id in command_ids)

    def test_healthy_multi_aio_uses_coordinator(self):
        """Multiple AIOs retain the supported Gateway coordinator control target."""
        with write_path(make_status()) as (base, gateway, inv, published, states):
            assert inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
            assert len(published) == 1
            assert published[0]["serial"] == "GW0000G001"

    def test_single_aio_nack_also_stops_after_one_attempt(self):
        """ACK masking is not exclusive to multi-AIO topology."""
        with write_path(make_status(aio_count=1), bus_ok=False) as (base, gateway, inv, published, states):
            assert not inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
            assert len(published) == 1

    def test_managed_transition_blocks_write(self):
        """A target withdrawn from management is no longer writable."""
        status = make_status()
        with write_path(status) as (base, gateway, inv, published, states):
            status.inverters[0].managed = False
            gateway._process_telemetry(status.SerializeToString())
            assert not inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
            assert published == []

    def test_legacy_omitted_managed_flags_defer_to_hub_ack(self):
        """Proto3 omitted booleans cannot be distinguished from all-false flags."""
        status = make_status()
        for target in status.inverters:
            target.ClearField("managed")
        with write_path(status) as (base, gateway, inv, published, states):
            assert inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
            assert len(published) == 1

    def test_disappeared_coordinator_blocks_write(self):
        """Cached entity configuration must not allow writing an absent target."""
        status = make_status()
        with write_path(status) as (base, gateway, inv, published, states):
            del status.inverters[0]
            gateway._process_telemetry(status.SerializeToString())
            assert not inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
            assert published == []

    def test_old_retained_status_blocks_write(self):
        """Recent MQTT receipt does not make an old device snapshot fresh."""
        status = make_status()
        status.timestamp -= 300
        with write_path(status) as (base, gateway, inv, published, states):
            assert not inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
            assert published == []

    def test_unavailable_ack_subscription_preserves_telemetry_but_blocks_control(self):
        """Old token ACLs must not silently downgrade to blind control writes."""
        with write_path(make_status()) as (base, gateway, inv, published, states):
            gateway._ack_subscribed = False
            assert gateway._mqtt_connected and gateway._last_status is not None
            assert not inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
            assert published == []

    def test_failed_confirmation_with_applied_value_is_verified(self):
        """A failed write reply is not proof that the physical value stayed unchanged."""
        with write_path(make_status(), bus_ok=True, ack_ok=False) as (base, gateway, inv, published, states):
            assert inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
            assert len(published) == 1

    def test_preflight_rejection_does_not_wait_for_readback(self):
        """A known preflight NACK rejects both numeric and switch writes immediately."""
        for numeric in (True, False):
            with write_path(make_status(), bus_ok=False, ack_error="not_polled") as (base, gateway, inv, published, states):
                if numeric:
                    result = inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
                else:
                    result = inv.write_and_poll_switch("discharge_enable", base.args["scheduled_discharge_enable"][0], False)
                assert not result
                assert len(published) == 1
                assert inv.sleep.call_count == 0

    def test_schedule_result_is_verified_once_without_replay(self):
        """The real option service handles applied, partial and preflight results."""
        for bus_ok, error in ((True, "modbus_write_failed"), (False, "modbus_write_failed"), (False, "not_polled")):
            with write_path(make_status(), bus_ok=bus_ok, ack_ok=False, ack_error=error) as (base, gateway, inv, published, states):
                assert inv.write_and_poll_option("charge_start", base.args["charge_start_time"][0], "02:00:00") is bus_ok
                assert len(published) == 1
                if error == "not_polled":
                    assert inv.sleep.call_count == 0

    def test_known_offline_hub_blocks_write_with_recent_telemetry(self):
        """A broker connection cannot override a known offline Hub LWT."""
        with write_path(make_status()) as (base, gateway, inv, published, states):
            gateway._gateway_online = False
            assert not inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
            assert published == []

    def test_ack_denial_refreshes_token_and_reconnects_before_control_ready(self):
        """An old one-level JWT must not block controls until its daily expiry."""
        status = make_status()
        with write_path(status) as (base, gateway, inv, published, states):
            gateway.mqtt_token = "old-token"
            gateway._ack_subscribed = False
            passwords = []
            refreshed = []
            observed_ready = []
            original_handle = gateway._handle_message

            async def refresh():
                """Issue a token containing the new ACK permission."""
                refreshed.append(True)
                gateway.mqtt_token = "new-token"
                return True

            async def handle(message):
                """Record readiness when the new connection delivers telemetry."""
                observed_ready.append(gateway._ack_subscribed)
                await original_handle(message)

            class Client:
                """Broker whose old JWT permits telemetry but denies ACK subscriptions."""

                def __init__(self, **kwargs):
                    """Capture the credentials used for each MQTT connection."""
                    self.password = kwargs["password"]
                    passwords.append(self.password)

                async def __aenter__(self):
                    """Establish the simulated connection."""
                    return self

                async def __aexit__(self, *args):
                    """Close the simulated connection."""
                    return None

                async def subscribe(self, topic, **kwargs):
                    """Enforce the old token's missing second topic-level grant."""
                    if topic == gateway.topic_ack and self.password == "old-token":
                        raise RuntimeError("SUBACK not authorized")

                @property
                def messages(self):
                    """End the listener after one telemetry frame."""

                    async def frames():
                        """Deliver a frame before asking the outer loop to stop."""
                        yield SimpleNamespace(topic=gateway.topic_status, payload=status.SerializeToString(), retain=False)
                        gateway.api_stop = True

                    return frames()

            gateway._do_token_refresh = refresh
            gateway._handle_message = handle
            with patch("gateway.aiomqtt.Client", Client):
                asyncio.run_coroutine_threadsafe(gateway._mqtt_loop(), gateway._loop).result(timeout=2)
            assert passwords == ["old-token", "new-token"]
            assert refreshed == [True]
            assert observed_ready == [True]

    def test_ack_retry_is_bounded_and_transient_denial_recovers(self):
        """Failed refresh preserves telemetry and does not hammer broker or issuer."""
        with write_path(make_status()) as (base, gateway, inv, published, states):
            gateway.mqtt_token = "same-token"
            gateway._ack_subscribed = False
            attempts = []
            refreshes = []

            async def subscribe(topic, **kwargs):
                """Fail once, then allow the unchanged token."""
                attempts.append(topic)
                if len(attempts) == 1:
                    raise RuntimeError("temporary subscription failure")

            async def refresh():
                """Model unavailable credential refresh."""
                refreshes.append(True)
                return False

            async def scenario():
                """Verify the retry interval and successful second subscription."""
                client = SimpleNamespace(subscribe=subscribe)
                with patch("gateway.time.monotonic", return_value=0):
                    assert not await gateway._subscribe_command_acks(client, "same-token", initial=True)
                    assert not await gateway._subscribe_command_acks(client, "same-token")
                assert len(attempts) == 1
                assert len(refreshes) == 1
                with patch("gateway.time.monotonic", return_value=61):
                    assert not await gateway._subscribe_command_acks(client, "same-token")
                assert gateway._ack_subscribed

            gateway._do_token_refresh = refresh
            asyncio.run_coroutine_threadsafe(scenario(), gateway._loop).result(timeout=2)
            assert len(attempts) == 2
            assert len(refreshes) == 1

    def test_legacy_false_service_result_preserves_readback_and_retry_behaviour(self):
        """Only typed Hub outcomes change the existing generic write contract."""
        for kind in ("number", "switch", "option"):
            for applied in (True, False):
                with write_path(make_status()) as (base, gateway, inv, published, states):
                    if kind == "number":
                        entity = base.args["reserve"][0]
                        expected = 10
                        write = lambda: inv.write_and_poll_value("reserve", entity, 10)
                    elif kind == "switch":
                        entity = base.args["scheduled_discharge_enable"][0]
                        expected = "off"
                        write = lambda: inv.write_and_poll_switch("discharge_enable", entity, False)
                    else:
                        entity = base.args["charge_start_time"][0]
                        expected = "02:00:00"
                        write = lambda: inv.write_and_poll_option("charge_start", entity, expected)

                    base.call_service_wrapper = MagicMock(return_value=False)
                    if applied:
                        inv.sleep.side_effect = lambda duration: states.__setitem__(entity, expected)
                    assert write() is applied
                    assert base.call_service_wrapper.call_count == (1 if applied else 10)
                    assert inv.sleep.call_count > 0

    def test_malformed_ack_error_is_an_unknown_outcome_not_an_exception(self):
        """Untrusted ACK errors cannot crash command dispatch or cause a retry."""
        for reason in ({}, [], None, 42):
            with write_path(make_status(), bus_ok=False, ack_error=reason) as (base, gateway, inv, published, states):
                assert not inv.write_and_poll_value("reserve", base.args["reserve"][0], 10)
                assert len(published) == 1

    def test_service_api_serialises_rejected_and_unknown_device_results(self):
        """The HTTP service API exposes device failure without a JSON encoding crash."""
        from web import WebInterface

        for reason in ("not_polled", "modbus_write_failed"):
            with write_path(make_status(), bus_ok=False, ack_error=reason) as (base, gateway, inv, published, states):
                api = WebInterface.__new__(WebInterface)
                api.base = base

                async def request_json():
                    """Submit a real standalone inverter service command."""
                    return {"service": "number/set_value", "data": {"entity_id": base.args["reserve"][0], "value": 10}}

                response = asyncio.run(api.html_api_post_service(SimpleNamespace(json=request_json)))
                result = json.loads(response.text)
                assert response.status == 200
                assert result["success"] is False
                assert reason in result["error"]
                assert result["outcome_unknown"] is (reason == "modbus_write_failed")
                assert len(published) == 1
