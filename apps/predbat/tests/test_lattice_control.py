"""Tests for the first topology-bound Lattice scalar dispatcher."""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import lattice_control_v0_3_pb2 as pb
from lattice_control import LatticeScalarDispatcher, RESULT_APPLIED, RESULT_NOT_APPLIED, RESULT_UNKNOWN
from lattice_topology import SourceCapabilityRef


def source(doc_version=9, cap_ref=41, topology_version="0.3.0"):
    """Return exact gateway-local provenance for the charge-power capability."""
    return SourceCapabilityRef(
        provider="predbat-gateway",
        topology_version=topology_version,
        doc_version=doc_version,
        cap_ref=cap_ref,
        node_id="INV-1",
        capability="battery.charge_power_limit",
        access_path="gw-local",
    )


class FakeStorage:
    """Small async Storage component double with controllable save failures."""

    def __init__(self, record=None, fail_save=False):
        self.record = record
        self.fail_save = fail_save
        self.saved = []

    async def save(self, module, filename, data, **kwargs):
        """Capture a defensive copy of each durable transition."""
        if self.fail_save:
            return False
        self.record = dict(data)
        self.saved.append(dict(data))
        return True

    async def load(self, module, filename):
        """Return the current record."""
        return dict(self.record) if self.record else None


class FailNthSaveStorage(FakeStorage):
    """Fail one selected durable transition."""

    def __init__(self, fail_on):
        super().__init__()
        self.fail_on = fail_on
        self.save_calls = 0

    async def save(self, module, filename, data, **kwargs):
        """Delegate every save except the selected call."""
        self.save_calls += 1
        if self.save_calls == self.fail_on:
            return False
        return await super().save(module, filename, data, **kwargs)


def ack(command_id, result, applied=None, error=""):
    """Build one gateway-compatible terminal ACK."""
    message = pb.ControlAck(command_id=command_id, result=result, ok=result == pb.CONTROL_RESULT_APPLIED, error=error, verified=result == pb.CONTROL_RESULT_APPLIED)
    if applied is not None:
        message.applied_scalar.i = applied
    return message.SerializeToString()


class Harness:
    """Wire a dispatcher to scripted ACK behavior."""

    def __init__(self, responses, storage=None, publish_error=False):
        self.responses = list(responses)
        self.storage = storage or FakeStorage()
        self.published = []
        self.legacy_calls = 0
        self.publish_error = publish_error
        self.logs = []
        self.dispatcher = LatticeScalarDispatcher(
            self.publish,
            self.storage,
            self.logs.append,
            "device/control",
            "device/result/get/",
            ack_timeout=0.01,
            reconcile_timeout=0.01,
            command_id_factory=lambda: "cmd-1",
        )

    async def publish(self, topic, payload):
        """Capture the publish and synchronously deliver the next scripted ACK."""
        self.published.append((topic, payload))
        if self.publish_error:
            raise RuntimeError("publish failed")
        response = self.responses.pop(0) if self.responses else None
        if response is None:
            return
        if response == "APPLIED":
            control_id = pb.Control.FromString(payload).command_id if topic == "device/control" else topic.rsplit("/", 1)[-1]
            result = ack(control_id, pb.CONTROL_RESULT_APPLIED, applied=2750)
        elif response == "NOT_APPLIED":
            control_id = pb.Control.FromString(payload).command_id if topic == "device/control" else topic.rsplit("/", 1)[-1]
            result = ack(control_id, pb.CONTROL_RESULT_NOT_APPLIED, error="stale doc_version")
        else:
            control_id = pb.Control.FromString(payload).command_id if topic == "device/control" else topic.rsplit("/", 1)[-1]
            result = ack(control_id, pb.CONTROL_RESULT_UNKNOWN, error="hardware result ambiguous")
        self.dispatcher.accept_ack(control_id, result)

    async def legacy(self):
        """Count legacy writes; a count above one is always a safety failure."""
        self.legacy_calls += 1


class TestLatticeScalarDispatcher(unittest.TestCase):
    """Exercise path selection, reconciliation, and serial fallback."""

    def test_selected_path_uses_exact_local_doc_and_ref(self):
        """The encoded control carries provider-local coordinates, not merged refs."""
        harness = Harness(["APPLIED"])
        result = asyncio.run(harness.dispatcher.dispatch_charge_power(source(17, 83), 3000, harness.legacy))
        control = pb.Control.FromString(harness.published[0][1])
        self.assertEqual(control.doc_version, 17)
        self.assertEqual(control.cap_ref, 83)
        self.assertEqual(control.scalar.i, 3000)
        self.assertEqual(result.state, RESULT_APPLIED)
        self.assertEqual(result.applied_value, 2750)
        self.assertEqual(harness.legacy_calls, 0)

    def test_current_gateway_02_topology_uses_additive_03_control_wire(self):
        """Gateway 0.2 topology provenance remains valid for the 0.3 scalar wire."""
        harness = Harness(["APPLIED"])
        result = asyncio.run(harness.dispatcher.dispatch_charge_power(source(topology_version="0.2.0"), 3000, harness.legacy))
        self.assertEqual(result.state, RESULT_APPLIED)
        self.assertEqual([topic for topic, _payload in harness.published], ["device/control"])
        self.assertEqual(harness.legacy_calls, 0)

    def test_not_applied_allows_one_legacy_fallback(self):
        """A definite gateway NACK permits exactly one serial legacy write."""
        harness = Harness(["NOT_APPLIED"])
        result = asyncio.run(harness.dispatcher.dispatch_charge_power(source(), 3000, harness.legacy))
        self.assertEqual(result.state, RESULT_NOT_APPLIED)
        self.assertTrue(result.fallback_used)
        self.assertEqual(harness.legacy_calls, 1)
        self.assertEqual(harness.storage.record["fallback_state"], "complete")

    def test_lost_ack_queries_durable_result_without_rewriting(self):
        """A lost ACK causes result/get, never a second control publish."""
        harness = Harness([None, "APPLIED"])
        result = asyncio.run(harness.dispatcher.dispatch_charge_power(source(), 3000, harness.legacy))
        self.assertEqual([topic for topic, _payload in harness.published], ["device/control", "device/result/get/cmd-1"])
        self.assertEqual(result.state, RESULT_APPLIED)
        self.assertEqual(harness.legacy_calls, 0)

    def test_unknown_is_reconciled_and_never_falls_back(self):
        """Repeated UNKNOWN remains blocked and cannot trigger a second write."""
        harness = Harness(["UNKNOWN", "UNKNOWN"])
        result = asyncio.run(harness.dispatcher.dispatch_charge_power(source(), 3000, harness.legacy))
        self.assertEqual(result.state, RESULT_UNKNOWN)
        self.assertEqual(harness.legacy_calls, 0)
        self.assertEqual([topic for topic, _payload in harness.published].count("device/control"), 1)

    def test_duplicate_result_does_not_repeat_device_or_legacy_write(self):
        """An identical duplicate ACK is accepted without changing the outcome."""
        harness = Harness([])

        async def duplicate_publish(topic, payload):
            harness.published.append((topic, payload))
            command_id = pb.Control.FromString(payload).command_id
            encoded = ack(command_id, pb.CONTROL_RESULT_APPLIED, applied=2750)
            self.assertTrue(harness.dispatcher.accept_ack(command_id, encoded))
            self.assertTrue(harness.dispatcher.accept_ack(command_id, encoded))

        harness.dispatcher._publish = duplicate_publish
        result = asyncio.run(harness.dispatcher.dispatch_charge_power(source(), 3000, harness.legacy))
        self.assertEqual(result.state, RESULT_APPLIED)
        self.assertEqual(len(harness.published), 1)
        self.assertEqual(harness.legacy_calls, 0)

    def test_stale_topology_nack_falls_back_only_after_ack(self):
        """The gateway's stale-doc NOT_APPLIED result is the fallback boundary."""
        harness = Harness(["NOT_APPLIED"])
        result = asyncio.run(harness.dispatcher.dispatch_charge_power(source(doc_version=1), 3000, harness.legacy))
        self.assertTrue(result.fallback_used)
        self.assertEqual(harness.legacy_calls, 1)

    def test_unsafe_source_never_publishes_lattice_control(self):
        """Non-gateway provenance cannot escape as a provider-local ref."""
        invalid = SourceCapabilityRef("cloud", "0.3.0", 9, 41, "INV-1", "battery.charge_power_limit", "vendor-cloud")
        harness = Harness([])
        result = asyncio.run(harness.dispatcher.dispatch_charge_power(invalid, 3000, harness.legacy))
        self.assertEqual(harness.published, [])
        self.assertTrue(result.fallback_used)
        self.assertEqual(harness.legacy_calls, 1)

    def test_pending_state_is_reconciled_after_restart(self):
        """A restart resumes command-ID lookup and never republishes the control."""
        storage = FakeStorage(
            {
                "command_id": "old-command",
                "state": RESULT_UNKNOWN,
                "value": 3000,
                "provider": "predbat-gateway",
                "node_id": "INV-1",
                "doc_version": 9,
                "cap_ref": 41,
                "fallback_state": None,
            }
        )
        harness = Harness(["NOT_APPLIED"], storage=storage)
        result = asyncio.run(harness.dispatcher.dispatch_charge_power(source(), 3000, harness.legacy))
        self.assertEqual([topic for topic, _payload in harness.published], ["device/result/get/old-command"])
        self.assertTrue(result.fallback_used)
        self.assertEqual(harness.legacy_calls, 1)

    def test_storage_failure_prevents_both_writes(self):
        """Without durable reservation neither Lattice nor legacy may be attempted."""
        harness = Harness([], storage=FakeStorage(fail_save=True))
        result = asyncio.run(harness.dispatcher.dispatch_charge_power(source(), 3000, harness.legacy))
        self.assertEqual(result.state, RESULT_UNKNOWN)
        self.assertEqual(harness.published, [])
        self.assertEqual(harness.legacy_calls, 0)

    def test_corrupt_persisted_state_fails_closed(self):
        """Malformed durable state cannot cause either a new or fallback write."""
        harness = Harness([], storage=FakeStorage({"state": RESULT_UNKNOWN, "command_id": ""}))
        result = asyncio.run(harness.dispatcher.dispatch_charge_power(source(), 3000, harness.legacy))
        self.assertEqual(result.state, RESULT_UNKNOWN)
        self.assertEqual(harness.published, [])
        self.assertEqual(harness.legacy_calls, 0)

    def test_restart_during_legacy_fallback_never_repeats_write(self):
        """A durable fallback-started marker blocks both paths after restart."""
        storage = FakeStorage(
            {
                "command_id": "old-command",
                "state": RESULT_NOT_APPLIED,
                "value": 3000,
                "fallback_state": "started",
            }
        )
        harness = Harness([], storage=storage)
        result = asyncio.run(harness.dispatcher.dispatch_charge_power(source(), 3000, harness.legacy))
        self.assertEqual(result.state, RESULT_UNKNOWN)
        self.assertEqual(harness.published, [])
        self.assertEqual(harness.legacy_calls, 0)

    def test_fallback_completion_save_failure_is_unknown(self):
        """A legacy write without durable completion remains explicitly ambiguous."""
        storage = FailNthSaveStorage(fail_on=4)
        harness = Harness(["NOT_APPLIED"], storage=storage)
        result = asyncio.run(harness.dispatcher.dispatch_charge_power(source(), 3000, harness.legacy))
        self.assertEqual(result.state, RESULT_UNKNOWN)
        self.assertEqual(harness.legacy_calls, 1)
        self.assertEqual(storage.record["fallback_state"], "started")

    def test_publish_exception_reconciles_and_never_falls_back(self):
        """An ambiguous publish failure is UNKNOWN and cannot dual-write."""
        harness = Harness([], publish_error=True)
        result = asyncio.run(harness.dispatcher.dispatch_charge_power(source(), 3000, harness.legacy))
        self.assertEqual(result.state, RESULT_UNKNOWN)
        self.assertEqual(harness.legacy_calls, 0)
        self.assertEqual(harness.published[0][0], "device/control")


if __name__ == "__main__":
    unittest.main()
