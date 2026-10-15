"""Joined engine window, Hub JSON and applied-ACK regressions."""
# cspell:ignore excepthook

import asyncio
import datetime
import json
import types
import unittest
from unittest.mock import MagicMock, patch

from gateway import GatewayMQTT
from inverter import Inverter
from tests.test_gateway import TestCommandAck


class WindowBase:
    """Predbat state/service boundary, backed by actual Hub ACK entity updates."""

    def __init__(self, gateway):
        """Configure two owned endpoint entities and capture logs and errors."""
        self.gateway = gateway
        self.args = {"charge_start_time": TestCommandAck.SLOT_START, "charge_end_time": TestCommandAck.SLOT_END}
        self.dashboard_index_app = {entity: "gateway" for entity in self.args.values()}
        self.components = types.SimpleNamespace(get_component=lambda name: gateway if name == "gateway" else None)
        self.control_ledger = None
        self.set_read_only = False
        self.set_inverter_notify = False
        self.inverter_clock_skew_start = self.inverter_clock_skew_end = 0
        self.logs = []
        self.errors = []

    def get_arg(self, name, default=None, index=None, indirect=True):
        """Resolve configured endpoint ids or their currently observed values."""
        if name == "scheduled_charge_enable":
            return "on"
        entity = self.args.get(name)
        return self.gateway.cache.get(entity, default) if indirect else entity

    def get_state_wrapper(self, entity_id, **kwargs):
        """Expose the actual ACK cache to the engine polling method."""
        return self.gateway.cache.get(entity_id)

    def log(self, message):
        """Capture human-readable execution logs."""
        self.logs.append(message)

    def record_status(self, message, **kwargs):
        """Capture reported execution errors."""
        self.errors.append(message)


class TestGatewayChargeWindow(unittest.TestCase):
    """Run real engine methods through serialization and real ACK handling."""

    def fixture(self, start="00:30:00", end="01:30:00", outcome="applied"):
        """Use synthetic MQTT transport while preserving production command/ACK code."""
        gateway = TestCommandAck()._make_gateway()
        base = WindowBase(gateway)
        gateway.base = base
        gateway.topic_command = "predbat/devices/pbgw_test/command"
        gateway.publish_command = types.MethodType(GatewayMQTT.publish_command, gateway)
        gateway.cache.update({TestCommandAck.SLOT_START: start, TestCommandAck.SLOT_END: end})
        wire = []

        async def transport(topic, payload, retain=False):
            """Capture exact wire and simulate applied, staged, refused or absent ACKs."""
            command = json.loads(payload)
            wire.append(command)
            self.assertFalse(retain)
            if outcome == "timeout":
                return
            if outcome == "exception":
                raise OSError("synthetic MQTT failure")
            applied = json.loads(command["schedule_json"])
            if outcome == "partial":
                applied["end"] = 130
            if outcome == "staged":
                applied["staged"] = True
            ack = {"command_id": command["command_id"], "command": command["command"], "ok": outcome != "refused", "applied": applied}
            if outcome == "refused":
                ack["error"] = "modbus_write_failed"
            gateway._process_ack(json.dumps(ack))

        gateway._publish_raw = transport
        inverter = Inverter.__new__(Inverter)
        inverter.base = base
        inverter.log = base.log
        inverter.id = 0
        inverter.count_register_writes = inverter.registers_moved = 0
        inverter.inv_write_backoff = False
        inverter.inv_write_max_retry = 3
        inverter.inv_write_and_poll_sleep = 0.01
        inverter.inv_has_charge_enable_time = True
        inverter.inv_charge_time_format = "H:M"
        inverter.inv_time_button_press = False
        inverter.commit_pending = {}
        inverter.last_committed = {}
        inverter.sleep = lambda seconds: None
        inverter.adjust_idle_time = lambda **kwargs: None
        inverter.disable_charge_window = MagicMock()
        inverter.write_and_poll_option = MagicMock(return_value=True)
        inverter.write_and_poll_switch = MagicMock(return_value=True)
        return inverter, gateway, base, wire

    def apply(self, inverter, start="01:30:00", end="02:00:00"):
        """Run the actual engine charge-window update at 01:30 local time."""
        parse = lambda value: datetime.datetime.strptime("2026-10-13 " + value, "%Y-%m-%d %H:%M:%S")
        inverter.adjust_charge_window(parse(start), parse(end), 90)

    def test_zero_length_intermediate_and_midnight_window_use_one_full_pair(self):
        """The two live failure shapes now reach the requested complete slot."""
        for old_start in ("00:30:00", "23:30:00"):
            inverter, gateway, base, wire = self.fixture(start=old_start)
            self.apply(inverter)
            self.assertEqual(len(wire), 1)
            self.assertEqual(json.loads(wire[0]["schedule_json"]), {"start": 130, "end": 200})
            self.assertEqual(wire[0]["serial"], "CE123456789")
            self.assertEqual(gateway.cache[TestCommandAck.SLOT_START], "01:30:00")
            self.assertEqual(gateway.cache[TestCommandAck.SLOT_END], "02:00:00")
            inverter.write_and_poll_option.assert_not_called()
            inverter.disable_charge_window.assert_not_called()
            self.assertEqual(base.errors, [])

    def test_swap_of_endpoints_does_not_need_an_invalid_intermediate(self):
        """Either single-endpoint ordering would disable this slot temporarily."""
        inverter, gateway, base, wire = self.fixture(start="02:00:00", end="01:30:00")
        self.apply(inverter)
        self.assertEqual(json.loads(wire[0]["schedule_json"]), {"start": 130, "end": 200})
        self.assertEqual(base.errors, [])

    def test_unverified_pair_never_logs_success_or_enables_window(self):
        """Refused, staged, wrong-end and missing ACKs cannot confirm the pair."""
        for outcome in ("refused", "staged", "partial", "timeout", "exception"):
            inverter, gateway, base, wire = self.fixture(outcome=outcome)
            base.control_ledger = MagicMock()
            self.apply(inverter)
            self.assertTrue(base.errors, outcome)
            self.assertFalse(any("Updated start and end" in line for line in base.logs), outcome)
            inverter.write_and_poll_switch.assert_not_called()
            inverter.write_and_poll_option.assert_not_called()
            self.assertEqual(base.control_ledger.clear.call_count, 2, outcome)
            base.control_ledger.record_write.assert_not_called()
            self.assertEqual(len(wire), 1, outcome)

    def test_success_records_both_controls_and_no_redundant_second_command(self):
        """Applied pair owns both endpoints and the next unchanged cycle is quiet."""
        inverter, gateway, base, wire = self.fixture()
        base.control_ledger = MagicMock()
        self.apply(inverter)
        self.assertEqual(base.control_ledger.record_write.call_count, 2)
        self.apply(inverter)
        self.assertEqual(len(wire), 1)

    def test_async_timer_publication_exception_never_becomes_coordinator_fallback(self):
        """RunThread returns None on error; that must remain a failed direct write."""
        inverter, gateway, base, wire = self.fixture(outcome="exception")

        async def timer():
            """Match the standalone timer calling synchronous execution on its loop."""
            self.apply(inverter)

        with patch("threading.excepthook") as thread_error:
            asyncio.run(timer())
        thread_error.assert_called_once()
        self.assertEqual(len(wire), 1)
        self.assertTrue(base.errors)
        inverter.write_and_poll_option.assert_not_called()
        inverter.write_and_poll_switch.assert_not_called()
        self.assertFalse(any("Updated start and end" in line for line in base.logs))

    def test_read_only_or_mismatched_entity_ownership_refuses_without_publication(self):
        """Never route a Cloud endpoint or user read-only window to the Hub."""
        for reason in ("read_only", "owner", "serial"):
            inverter, gateway, base, wire = self.fixture()
            if reason == "read_only":
                base.set_read_only = True
            elif reason == "owner":
                base.dashboard_index_app[TestCommandAck.SLOT_END] = "gecloud"
            else:
                base.args["charge_end_time"] = TestCommandAck.SLOT_END.replace("456789", "654321")
                base.dashboard_index_app[base.args["charge_end_time"]] = "gateway"
            self.apply(inverter)
            self.assertEqual(wire, [], reason)
            self.assertTrue(base.errors, reason)

    def test_other_provider_keeps_individual_entity_writes(self):
        """Cloud and other integrations retain their existing update path."""
        inverter, gateway, base, wire = self.fixture()
        base.dashboard_index_app = {entity: "gecloud" for entity in base.args.values()}
        self.apply(inverter)
        self.assertEqual(wire, [])
        self.assertEqual(inverter.write_and_poll_option.call_count, 2)

    def test_coordinator_slot_staging_keeps_existing_enable_sequence(self):
        """EMS and Gateway coordinators keep their staged-slot update path."""
        from gateway import GATEWAY_COORDINATOR_TYPES

        for kind in GATEWAY_COORDINATOR_TYPES:
            inverter, gateway, base, wire = self.fixture()
            gateway._discovery = {"CE123456789": {"type": kind}}
            self.apply(inverter)
            self.assertEqual(wire, [])
            self.assertEqual(inverter.write_and_poll_option.call_count, 2)
            self.assertEqual(base.errors, [])

    def test_coordinator_with_partial_ownership_keeps_staged_sequence(self):
        """A coordinator is identified before ownership checks, so it is never refused by them."""
        from gateway import GATEWAY_COORDINATOR_TYPES

        for kind in GATEWAY_COORDINATOR_TYPES:
            inverter, gateway, base, wire = self.fixture()
            gateway._discovery = {"CE123456789": {"type": kind}}
            base.dashboard_index_app[TestCommandAck.SLOT_END] = None
            self.apply(inverter)
            self.assertFalse(any("inconsistent entity ownership" in error for error in base.errors), kind)
            self.assertEqual(wire, [], kind)
            self.assertEqual(inverter.write_and_poll_option.call_count, 2, kind)

    def test_invalid_time_or_pair_is_rejected_before_command(self):
        """The public paired method validates inputs before calling the command sink."""
        inverter, gateway, base, wire = self.fixture()
        for start, end in (("25:00:00", "02:00:00"), (None, "02:00:00")):
            self.assertFalse(asyncio.run(gateway.update_charge_window(TestCommandAck.SLOT_START, TestCommandAck.SLOT_END, start, end)))
        self.assertEqual(wire, [])
