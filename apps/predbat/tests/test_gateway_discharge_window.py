"""Joined engine discharge window, command serialization and ACK regressions."""

# cspell:ignore excepthook

import asyncio
import datetime
import json
import types
import unittest
from unittest.mock import MagicMock, patch

from gateway import GatewayMQTT, GATEWAY_COORDINATOR_TYPES
from inverter import Inverter
from tests.test_gateway import TestCommandAck


class DischargeBase:
    """Synthetic service boundary backed by the production gateway ACK cache."""

    START = "select.predbat_gateway_456789_discharge_slot1_start"
    END = "select.predbat_gateway_456789_discharge_slot1_end"
    ENABLE = "switch.predbat_gateway_456789_discharge_enabled"

    def __init__(self, gateway):
        """Configure one direct battery and retain errors and service calls."""
        self.gateway = gateway
        self.args = {"discharge_start_time": self.START, "discharge_end_time": self.END, "scheduled_discharge_enable": self.ENABLE}
        self.dashboard_index_app = {entity: "gateway" for entity in self.args.values()}
        self.components = types.SimpleNamespace(get_component=lambda name: gateway if name == "gateway" else None)
        self.control_ledger = None
        self.set_read_only = self.set_inverter_notify = False
        self.inverter_clock_skew_discharge_start = self.inverter_clock_skew_discharge_end = 0
        self.logs, self.errors, self.services = [], [], []

    def get_arg(self, name, default=None, index=None, indirect=True, **kwargs):
        """Resolve configured entities or their observed values."""
        entity = self.args.get(name)
        return self.gateway.cache.get(entity, default) if indirect else entity

    def get_state_wrapper(self, entity_id, **kwargs):
        """Read the production ACK cache."""
        return self.gateway.cache.get(entity_id)

    def log(self, message):
        """Retain execution messages."""
        self.logs.append(message)

    def record_status(self, message, **kwargs):
        """Retain failed verification reports."""
        self.errors.append(message)

    def call_service_wrapper(self, service, entity_id, **kwargs):
        """Route ordinary entity writes to their actual gateway handlers."""
        self.services.append((service, entity_id, kwargs))
        if service.endswith("/select_option"):
            asyncio.run(self.gateway.select_event(entity_id, kwargs["option"]))
        else:
            asyncio.run(self.gateway.switch_event(entity_id, service.split("/")[1]))


class TestGatewayDischargeWindow(unittest.TestCase):
    """Exercise actual inverter writes, gateway commands and ACK handling."""

    def fixture(self, outcome="applied", start="00:00:00", end="00:01:00", enabled="off"):
        """Use synthetic transport with applied, failed, staged or missing ACKs."""
        gw = TestCommandAck()._make_gateway()
        base = DischargeBase(gw)
        gw.base = base
        gw.topic_command = "predbat/devices/pbgw_test/command"
        gw.publish_command = types.MethodType(GatewayMQTT.publish_command, gw)
        gw.cache.update({base.START: start, base.END: end, base.ENABLE: enabled})
        wire, mode = [], {"outcome": outcome}

        async def transport(topic, payload, retain=False):
            """Apply actual ACK updates while controlling transport outcomes."""
            command = json.loads(payload)
            wire.append(command)
            self.assertFalse(retain)
            if mode["outcome"] == "exception":
                raise OSError("synthetic MQTT failure")
            if mode["outcome"] == "timeout":
                return
            refused = False
            if command["command"] == "set_discharge_slot":
                part = json.loads(command["schedule_json"])
                # Both the legacy single endpoint and paired paths reach this sink.
                hhmm = lambda value: int(value[:2]) * 100 + int(value[3:5])
                applied = {"start": hhmm(gw.cache[base.START]), "end": hhmm(gw.cache[base.END]), **part}
                refused = mode["outcome"] == "refused" and "end" in part
                if mode["outcome"] == "staged":
                    applied["staged"] = True
                elif mode["outcome"] == "partial":
                    applied["end"] = 1
            else:
                applied = {"enable": command["enable"]}
            ack = {"command_id": command["command_id"], "command": command["command"], "ok": not refused, "applied": applied}
            if refused:
                ack["error"] = "modbus_write_failed"
            gw._process_ack(json.dumps(ack))

        gw._publish_raw = transport
        inv = Inverter.__new__(Inverter)
        inv.base, inv.log, inv.id = base, base.log, 0
        inv.count_register_writes = inv.registers_moved = 0
        inv.inv_write_backoff = False
        inv.write_backoff = {}
        inv.inv_write_max_retry, inv.inv_write_and_poll_sleep = 3, 0.01
        inv.inv_has_discharge_enable_time = inv.inv_has_charge_enable_time = True
        inv.inv_has_ge_inverter_mode = inv.inv_time_button_press = False
        inv.inv_charge_time_format = "H:M"
        inv.reserve_percent = 4
        inv.commit_pending, inv.last_committed = {}, {}
        inv.sleep = lambda seconds: None
        inv.adjust_idle_time = MagicMock()
        inv.adjust_inverter_mode = MagicMock()
        return inv, gw, base, wire, mode

    def apply(self, inv, force=True, start="02:55:00", end="03:01:00"):
        """Run the actual discharge scheduling method."""
        parse = lambda value: datetime.datetime.fromisoformat("2026-10-13T" + value) if value else None
        inv.adjust_force_export(force, parse(start), parse(end))

    def test_failed_endpoint_never_enables_stale_window(self):
        """A failed discharge pair must not enable its stale midnight window."""
        inv, gw, base, wire, mode = self.fixture(outcome="refused")
        self.apply(inv)
        self.assertTrue(base.errors)
        self.assertFalse(any(command["command"] == "set_discharge_enable" for command in wire))
        inv.adjust_inverter_mode.assert_not_called()
        self.assertEqual(gw.cache[base.ENABLE], "off")
        self.assertTrue(inv.commit_pending.get("export_window"))

    def test_pair_applied_before_enable_and_full_serial(self):
        """Only both applied endpoint values allow the subsequent enable command."""
        inv, gw, base, wire, mode = self.fixture()
        self.apply(inv)
        self.assertEqual([command["command"] for command in wire], ["set_discharge_slot", "set_discharge_enable"])
        self.assertEqual(json.loads(wire[0]["schedule_json"]), {"start": 255, "end": 301})
        self.assertEqual(wire[0]["serial"], "CE123456789")
        self.assertEqual((gw.cache[base.START], gw.cache[base.END], gw.cache[base.ENABLE]), ("02:55:00", "03:01:00", "on"))
        self.assertFalse(base.errors)

    def test_paired_write_counts_one_write_and_both_moved_registers(self):
        """The pair is one physical write that moves both endpoints."""
        inv, gw, base, wire, mode = self.fixture(enabled="on")
        self.apply(inv)
        self.assertEqual([command["command"] for command in wire], ["set_discharge_slot"])
        self.assertEqual((inv.count_register_writes, inv.registers_moved), (1, 2))

    def test_refused_window_disables_an_already_exporting_schedule(self):
        """A refused move must not leave the previous enabled window exporting."""
        inv, gw, base, wire, mode = self.fixture(outcome="refused", start="02:00:00", end="04:00:00", enabled="on")
        self.apply(inv, start="02:30:00", end="03:00:00")
        self.assertEqual(json.loads(wire[0]["schedule_json"]), {"start": 230, "end": 300})
        self.assertEqual((gw.cache[base.START], gw.cache[base.END]), ("02:00:00", "04:00:00"))
        self.assertEqual(gw.cache[base.ENABLE], "off")
        self.assertEqual([command.get("enable") for command in wire if command["command"] == "set_discharge_enable"], [False])
        inv.adjust_inverter_mode.assert_called_once_with(False)
        self.assertTrue(inv.commit_pending["export_window"])

    def test_unconfirmed_move_of_an_exporting_schedule_is_left_running(self):
        """Only a refusal disables: an unanswered or incomplete write may have applied."""
        for outcome in ("timeout", "partial", "staged"):
            inv, gw, base, wire, mode = self.fixture(outcome=outcome, start="02:00:00", end="04:00:00", enabled="on")
            self.apply(inv, start="02:30:00", end="03:00:00")
            self.assertEqual([command["command"] for command in wire], ["set_discharge_slot"], outcome)
            self.assertEqual(gw.cache[base.ENABLE], "on", outcome)
            inv.adjust_inverter_mode.assert_not_called()
            self.assertTrue(inv.commit_pending["export_window"], outcome)
            self.assertTrue(base.errors, outcome)

    def test_applied_move_of_an_exporting_schedule_keeps_it_exporting(self):
        """The fail-safe disable is only for a failed write: a successful move stays enabled."""
        inv, gw, base, wire, mode = self.fixture(start="02:00:00", end="04:00:00", enabled="on")
        self.apply(inv, start="02:30:00", end="03:00:00")
        self.assertEqual([command["command"] for command in wire], ["set_discharge_slot"])
        self.assertEqual((gw.cache[base.START], gw.cache[base.END], gw.cache[base.ENABLE]), ("02:30:00", "03:00:00", "on"))
        inv.adjust_inverter_mode.assert_called_once_with(True, changed_start_end=True)
        self.assertFalse(inv.commit_pending["export_window"])
        self.assertFalse(base.errors)

    def test_refusal_is_not_polled_to_timeout_or_resent(self):
        """A refusal ends the attempt at once; the identical command is not re-sent or re-polled."""
        inv, gw, base, wire, mode = self.fixture(outcome="refused")
        inv.inv_write_and_poll_sleep = 3600  # any poll to timeout would hang the test
        polls = []
        original = inv._poll_after_write
        inv._poll_after_write = lambda *args, **kwargs: polls.append(args) or original(*args, **kwargs)
        self.apply(inv)
        self.assertEqual(len(polls), 1)
        self.assertEqual([command["command"] for command in wire], ["set_discharge_slot"])
        self.assertTrue(base.errors)

    def test_unverified_pair_clears_ledger_and_cannot_activate(self):
        """Refusal, incomplete or staged ACKs, timeout and exceptions fail closed."""
        for outcome in ("refused", "partial", "staged", "timeout", "exception"):
            inv, gw, base, wire, mode = self.fixture(outcome=outcome)
            base.control_ledger = MagicMock()
            self.apply(inv)
            self.assertTrue(base.errors, outcome)
            self.assertFalse(any(command["command"] == "set_discharge_enable" for command in wire), outcome)
            self.assertEqual(base.control_ledger.clear.call_count, 2, outcome)
            base.control_ledger.record_write.assert_not_called()
            inv.adjust_inverter_mode.assert_not_called()
            self.assertTrue(inv.commit_pending.get("export_window"))
            self.assertNotIn("export_window", inv.last_committed)

    def test_retry_after_refusal_can_apply_and_enable(self):
        """A later verified retry recovers without treating a failed pair as committed."""
        inv, gw, base, wire, mode = self.fixture(outcome="refused")
        self.apply(inv)
        mode["outcome"] = "applied"
        with patch("gateway._monotonic", return_value=10**12):
            self.apply(inv)
        self.assertEqual(gw.cache[base.END], "03:01:00")
        self.assertEqual(gw.cache[base.ENABLE], "on")
        self.assertFalse(inv.commit_pending["export_window"])
        self.assertEqual(json.loads(wire[-2]["schedule_json"]), {"start": 255, "end": 301})

    def test_read_only_owner_and_mismatched_pair_refuse(self):
        """Do not publish across read-only or mismatched endpoint boundaries."""
        for reason in ("read_only", "owner", "mismatched_pair"):
            inv, gw, base, wire, mode = self.fixture()
            if reason == "read_only":
                base.set_read_only = True
            elif reason == "owner":
                base.dashboard_index_app[base.END] = "gecloud"
            else:
                # An end slot of another serial is not this start's pair
                entity = base.END.replace("456789", "654321")
                base.args["discharge_end_time"] = entity
                base.dashboard_index_app[entity] = "gateway"
            self.apply(inv)
            self.assertFalse(wire, reason)
            self.assertTrue(base.errors, reason)
            inv.adjust_inverter_mode.assert_not_called()

    def test_async_exception_does_not_fall_back(self):
        """A swallowed async bridge exception remains failure, never coordinator fallback."""
        inv, gw, base, wire, mode = self.fixture(outcome="exception")

        async def timer():
            """Match a timer invoking synchronous engine execution on its event loop."""
            self.apply(inv)

        with patch("threading.excepthook") as error:
            asyncio.run(timer())
        error.assert_called_once()
        self.assertEqual(len(wire), 1)
        self.assertFalse(base.services)
        inv.adjust_inverter_mode.assert_not_called()

    def test_other_provider_and_coordinator_keep_individual_sequence(self):
        """Only direct Hub inverters opt into the new pair and verification gate."""
        for kind in (None, *GATEWAY_COORDINATOR_TYPES):
            inv, gw, base, wire, mode = self.fixture()
            if kind is None:
                base.dashboard_index_app = {entity: "gecloud" for entity in base.args.values()}
            else:
                gw._discovery = {"CE123456789": {"type": kind}}
            self.apply(inv)
            self.assertEqual([json.loads(command["schedule_json"]) for command in wire if command["command"] == "set_discharge_slot"], [{"start": 255}, {"end": 301}])
            self.assertEqual(wire[-1]["command"], "set_discharge_enable")

    def test_coordinator_with_partial_ownership_keeps_staged_sequence(self):
        """A coordinator is identified before ownership checks, so it is never refused by them."""
        for kind in GATEWAY_COORDINATOR_TYPES:
            inv, gw, base, wire, mode = self.fixture()
            gw._discovery = {"CE123456789": {"type": kind}}
            base.dashboard_index_app[base.END] = None
            self.apply(inv)
            self.assertFalse(any("inconsistent entity ownership" in error for error in base.errors), kind)
            self.assertEqual([json.loads(command["schedule_json"]) for command in wire if command["command"] == "set_discharge_slot"], [{"start": 255}, {"end": 301}])

    def test_disable_and_midnight_zero_window_remain_valid(self):
        """A disable request never activates export and may program midnight endpoints."""
        inv, gw, base, wire, mode = self.fixture(enabled="on")
        self.apply(inv, force=False, start="00:00:00", end="00:00:00")
        self.assertFalse(any(command.get("enable") is True for command in wire))
        self.assertEqual(gw.cache[base.ENABLE], "off")
        self.assertEqual((gw.cache[base.START], gw.cache[base.END]), ("00:00:00", "00:00:00"))

    def test_success_ledger_and_backoff(self):
        """Paired writes own both endpoints and use a stable pair retry key."""
        inv, gw, base, wire, mode = self.fixture(outcome="refused")
        base.control_ledger = MagicMock()
        inv.inv_write_backoff = True
        self.apply(inv)
        key = base.START + "|" + base.END
        self.assertEqual(inv.write_backoff[key]["failures"], 1)
        mode["outcome"] = "applied"
        with patch("gateway._monotonic", return_value=10**12):
            self.apply(inv)
        self.assertNotIn(key, inv.write_backoff)
        writes = base.control_ledger.record_write.call_args_list
        self.assertTrue(any(call.args[:3] == (base.START, "discharge_start_time", "02:55:00") for call in writes))
        self.assertTrue(any(call.args[:3] == (base.END, "discharge_end_time", "03:01:00") for call in writes))
