"""Tests for pure Lattice fragment auto-configuration compilation."""

# cspell:ignore autoconfig

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_autoconfig import (
    AliasRole,
    AutoConfigCompileError,
    CompileStatus,
    LatticeAutoConfigCompiler,
    ProviderAlias,
    ProviderHealth,
    ProviderIdentityAlias,
    ProviderSnapshot,
    compile_auto_config,
)


def fragment(provider, generation, node_id="INV1", kind="inverter"):
    """Build a compact provider-owned topology fragment."""
    access_path = "{}-path".format(provider)
    return {
        "topologyVersion": "0.3.0",
        "scope": "fragment",
        "docVersion": generation,
        "producer": {"name": provider, "provider": provider, "authority": 10},
        "nodes": [
            {
                "id": node_id,
                "kind": kind,
                "deviceType": "hybrid",
                "accessPaths": [{"id": access_path, "provider": provider, "preference": 10}],
                "capabilities": [
                    {
                        "capability": "battery.target_soc",
                        "accessPath": access_path,
                        "ref": 1,
                        "shape": "setpoint",
                        "control": {"protocol": "mqtt"},
                    }
                ],
            }
        ],
    }


def snapshot(provider, generation=1, node_id="INV1", kind="inverter", health=ProviderHealth.HEALTHY, aliases=(), identity_aliases=()):
    """Build one typed provider snapshot."""
    return ProviderSnapshot(provider, generation, health, fragment(provider, generation, node_id=node_id, kind=kind), aliases, identity_aliases)


class MutableReader:
    """Thread-safe-enough mutable snapshot reader for deterministic tests."""

    def __init__(self, value):
        """Store the first snapshot and initialise the call count."""
        self.value = value
        self.calls = 0

    def __call__(self):
        """Return the current snapshot and count the fresh read."""
        self.calls += 1
        return self.value


class TestPlanCompilation(unittest.TestCase):
    """Compiler output is safe, immutable, deterministic, and attributable."""

    def test_order_independent_digest_and_provider_qualified_aliases(self):
        """Input order and shared local alias names cannot alter a plan."""
        alias = ProviderAlias("battery", "INV1", frozenset((AliasRole.REFERENCE, AliasRole.PRIMARY, AliasRole.CONTROL)))
        gateway = snapshot("gateway", aliases=(alias,), identity_aliases=(ProviderIdentityAlias("serial", "SER123", "INV1"),))
        cloud = snapshot("cloud", aliases=(alias,), identity_aliases=(ProviderIdentityAlias("serial", "SER123", "INV1"),))

        left = compile_auto_config((gateway, cloud))
        right = compile_auto_config((cloud, gateway))

        self.assertEqual(left.digest, right.digest)
        self.assertEqual([binding.qualified_name for binding in left.aliases], ["cloud:battery", "gateway:battery"])
        self.assertEqual(dict(left.provider_generations), {"cloud": 1, "gateway": 1})
        self.assertEqual({field.name for field in left.fields}, {"alias.cloud:battery", "alias.gateway:battery", "control_target", "primary_target"})
        self.assertTrue(all(field.provenance for field in left.fields))
        with self.assertRaises(TypeError):
            left.topology["scope"] = "fragment"

    def test_generation_bookkeeping_does_not_change_semantic_digest(self):
        """An identical newer fragment generation avoids materialization churn."""
        alias = ProviderAlias("battery", "INV1", frozenset((AliasRole.PRIMARY,)))
        first = compile_auto_config((snapshot("gateway", generation=1, aliases=(alias,)),))
        second = compile_auto_config((snapshot("gateway", generation=2, aliases=(alias,)),))

        self.assertEqual(first.digest, second.digest)
        self.assertNotEqual(first.provider_generations, second.provider_generations)

    def test_shared_stable_identity_correlates_different_local_nodes(self):
        """Gateway and cloud assertions for one serial compile to one node."""
        gateway_alias = ProviderIdentityAlias("serial", "SER123", "gw-local-1")
        cloud_alias = ProviderIdentityAlias("serial", "SER123", "cloud-local-9")
        primary = ProviderAlias("battery", "gw-local-1", frozenset((AliasRole.PRIMARY, AliasRole.CONTROL)))
        gateway = snapshot("gateway", node_id="gw-local-1", aliases=(primary,), identity_aliases=(gateway_alias,))
        cloud = snapshot("cloud", node_id="cloud-local-9", identity_aliases=(cloud_alias,))

        plan = compile_auto_config((gateway, cloud))

        self.assertEqual(len(plan.topology["nodes"]), 1)
        self.assertEqual(plan.topology["nodes"][0]["id"], "identity:serial:SER123")
        self.assertEqual({path["id"] for path in plan.topology["nodes"][0]["accessPaths"]}, {"gateway-path", "cloud-path"})
        self.assertEqual(plan.aliases[0].node_id, "identity:serial:SER123")
        self.assertEqual({binding.provider_id for binding in plan.identity_aliases}, {"gateway", "cloud"})
        capability_sources = {item.provider_id: item.source_path for item in plan.provenance if "/capabilities/" in item.field_path}
        self.assertIn("gw-local-1", capability_sources["gateway"])
        self.assertIn("cloud-local-9", capability_sources["cloud"])

    def test_equal_provider_local_ids_do_not_implicitly_correlate(self):
        """Matching local labels remain separate without a shared stable identity."""
        plan = compile_auto_config((snapshot("gateway", node_id="INV1"), snapshot("cloud", node_id="INV1")))

        self.assertEqual(
            {node["id"] for node in plan.topology["nodes"]},
            {"provider:gateway:INV1", "provider:cloud:INV1"},
        )

    def test_duplicate_provider_snapshots_fail_closed(self):
        """The pure compile entry point cannot accept two generations of one provider."""
        with self.assertRaisesRegex(AutoConfigCompileError, "duplicate provider"):
            compile_auto_config((snapshot("gateway", generation=1), snapshot("gateway", generation=2)))

    def test_provider_cannot_reuse_stable_identity_for_two_nodes(self):
        """One integration cannot correlate two local nodes to one identity."""
        document = fragment("gateway", 1, node_id="INV1")
        second = dict(document["nodes"][0])
        second["id"] = "INV2"
        document["nodes"].append(second)
        aliases = (ProviderIdentityAlias("serial", "SER123", "INV1"), ProviderIdentityAlias("serial", "SER123", "INV2"))
        provider = ProviderSnapshot("gateway", 1, ProviderHealth.HEALTHY, document, (), aliases)

        with self.assertRaisesRegex(AutoConfigCompileError, "identity alias collision"):
            compile_auto_config((provider,))

    def test_correlated_strong_identity_values_cannot_conflict(self):
        """A cross-kind correlation cannot conceal conflicting serial values."""
        gateway_aliases = (
            ProviderIdentityAlias("serial", "SER-A", "gw"),
            ProviderIdentityAlias("mac", "AA:BB", "gw"),
        )
        cloud_aliases = (
            ProviderIdentityAlias("serial", "SER-B", "cloud"),
            ProviderIdentityAlias("mac", "AA:BB", "cloud"),
        )
        gateway = snapshot("gateway", node_id="gw", identity_aliases=gateway_aliases)
        cloud = snapshot("cloud", node_id="cloud", identity_aliases=cloud_aliases)

        with self.assertRaisesRegex(AutoConfigCompileError, "strong identity aliases conflict"):
            compile_auto_config((gateway, cloud))

    def test_duplicate_qualified_alias_fails_closed(self):
        """One provider cannot publish the same qualified alias twice."""
        aliases = (ProviderAlias("battery", "INV1"), ProviderAlias("battery", "INV1"))
        with self.assertRaisesRegex(AutoConfigCompileError, "alias collision"):
            compile_auto_config((snapshot("gateway", aliases=aliases),))

    def test_identity_collision_fails_closed(self):
        """Conflicting identity fields for one node cannot be authority-merged."""
        gateway_alias = ProviderIdentityAlias("serial", "SER123", "INV1")
        cloud_alias = ProviderIdentityAlias("serial", "SER123", "INV1")
        with self.assertRaisesRegex(AutoConfigCompileError, "identity collision"):
            compile_auto_config(
                (
                    snapshot("gateway", kind="inverter", identity_aliases=(gateway_alias,)),
                    snapshot("cloud", kind="battery", identity_aliases=(cloud_alias,)),
                )
            )

    def test_ambiguous_primary_and_control_targets_fail_closed(self):
        """Several distinct target nodes cannot silently pick a winner."""
        primary_a = ProviderAlias("battery", "INV1", frozenset((AliasRole.PRIMARY,)))
        primary_b = ProviderAlias("battery", "INV2", frozenset((AliasRole.PRIMARY,)))
        with self.assertRaisesRegex(AutoConfigCompileError, "ambiguous primary"):
            compile_auto_config((snapshot("gateway", node_id="INV1", aliases=(primary_a,)), snapshot("cloud", node_id="INV2", aliases=(primary_b,))))

        control_a = ProviderAlias("control", "INV1", frozenset((AliasRole.CONTROL,)))
        control_b = ProviderAlias("control", "INV2", frozenset((AliasRole.CONTROL,)))
        with self.assertRaisesRegex(AutoConfigCompileError, "ambiguous control"):
            compile_auto_config((snapshot("gateway", node_id="INV1", aliases=(control_a,)), snapshot("cloud", node_id="INV2", aliases=(control_b,))))

    def test_alias_must_target_provider_local_identity(self):
        """An alias cannot smuggle a target owned only by another provider."""
        bad = ProviderAlias("battery", "INV2")
        with self.assertRaisesRegex(AutoConfigCompileError, "unknown provider-local node"):
            compile_auto_config((snapshot("gateway", node_id="INV1", aliases=(bad,)), snapshot("cloud", node_id="INV2")))


class TestInvalidationStateMachine(unittest.TestCase):
    """Invalidations coalesce without losing freshness or last-known-good state."""

    def test_replayed_and_stale_generations_are_rejected(self):
        """A generation is accepted once and never rolls backwards."""
        reader = MutableReader(snapshot("gateway", generation=1))
        compiler = LatticeAutoConfigCompiler({"gateway": reader})

        self.assertTrue(compiler.invalidate("gateway", 1, "first telemetry"))
        self.assertFalse(compiler.invalidate("gateway", 1, "duplicate"))
        self.assertFalse(compiler.invalidate("gateway", 0, "stale"))
        run = compiler.drain()
        self.assertEqual(run.attempts, 1)
        self.assertFalse(compiler.invalidate("gateway", 1, "observed replay"))

    def test_burst_of_ten_simultaneous_invalidations_coalesces(self):
        """Ten integrations invalidating together need only one all-provider read."""
        readers = {name: MutableReader(snapshot(name, generation=1, node_id="INV1")) for name in ("a", "b", "c", "d", "e", "f", "g", "h", "i", "j")}
        compiler = LatticeAutoConfigCompiler(readers)
        barrier = threading.Barrier(len(readers))
        accepted = []

        def invalidate(name):
            """Release the burst together and record acceptance."""
            barrier.wait()
            accepted.append(compiler.invalidate(name, 1, "simultaneous discovery"))

        threads = [threading.Thread(target=invalidate, args=(name,)) for name in readers]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        run = compiler.drain()
        self.assertTrue(all(accepted))
        self.assertEqual(run.attempts, 1)
        self.assertLessEqual(run.attempts, 2)
        self.assertTrue(all(reader.calls == 1 for reader in readers.values()))

    def test_invalidation_during_compile_guarantees_one_fresh_follow_up(self):
        """A mid-compile generation change is included by one bounded follow-up."""
        entered = threading.Event()
        release = threading.Event()
        state = {"value": snapshot("gateway", generation=1), "calls": 0}

        def reader():
            """Block only the first read after capturing its generation."""
            state["calls"] += 1
            value = state["value"]
            if state["calls"] == 1:
                entered.set()
                release.wait(5)
            return value

        requests = []
        compiler = LatticeAutoConfigCompiler({"gateway": reader}, requests.append)
        result = {}
        worker = threading.Thread(target=lambda: result.setdefault("run", compiler.drain()))
        worker.start()
        self.assertTrue(entered.wait(5))
        state["value"] = snapshot("gateway", generation=2)
        self.assertTrue(compiler.invalidate("gateway", 2, "new device"))
        release.set()
        worker.join(5)

        run = result["run"]
        self.assertEqual(run.attempts, 2)
        self.assertEqual(state["calls"], 2)
        self.assertEqual(dict(run.plan.provider_generations), {"gateway": 2})
        self.assertEqual([dict(request.plan.provider_generations) for request in requests], [{"gateway": 2}])
        self.assertEqual(run.materializations, 1)
        self.assertFalse(run.pending)

    def test_all_accepted_invalidation_causes_survive_coalescing(self):
        """Execution coalesces while distinct provider causes remain auditable."""
        reader = MutableReader(snapshot("gateway", generation=2))
        compiler = LatticeAutoConfigCompiler({"gateway": reader})

        self.assertTrue(compiler.invalidate("gateway", 1, "health changed"))
        self.assertTrue(compiler.invalidate("gateway", 2, "topology changed"))
        run = compiler.drain()

        self.assertEqual(
            {(item.provider_id, item.generation, item.reason) for item in run.invalidations},
            {
                ("gateway", 1, "health changed"),
                ("gateway", 2, "topology changed"),
            },
        )
        self.assertEqual(run.attempts, 1)

    def test_single_flight_rejects_a_concurrent_drain(self):
        """Only one caller can own the compile flight."""
        entered = threading.Event()
        release = threading.Event()

        def reader():
            """Hold the active flight until the competing drain returns."""
            entered.set()
            release.wait(5)
            return snapshot("gateway")

        compiler = LatticeAutoConfigCompiler({"gateway": reader})
        result = {}
        worker = threading.Thread(target=lambda: result.setdefault("run", compiler.drain()))
        worker.start()
        self.assertTrue(entered.wait(5))
        concurrent = compiler.drain()
        release.set()
        worker.join(5)

        self.assertEqual(concurrent.attempts, 0)
        self.assertEqual(result["run"].attempts, 1)

    def test_provider_failures_and_offline_health_are_isolated(self):
        """Bad and offline producers do not erase a healthy provider's plan."""
        good = MutableReader(snapshot("gateway"))
        offline = MutableReader(snapshot("cloud", health=ProviderHealth.OFFLINE))

        def broken():
            """Simulate an integration-local reader failure."""
            raise RuntimeError("cloud API failed")

        compiler = LatticeAutoConfigCompiler({"gateway": good, "cloud": offline, "broken": broken})
        run = compiler.drain()

        self.assertEqual(run.status, CompileStatus.DEGRADED)
        self.assertEqual(dict(run.plan.provider_generations), {"gateway": 1})
        self.assertEqual({issue.code for issue in run.issues}, {"provider_offline", "provider_read_failed"})
        self.assertEqual((good.calls, offline.calls), (1, 1))

    def test_malformed_provider_is_isolated(self):
        """Malformed topology from one integration does not poison healthy input."""
        malformed = ProviderSnapshot("cloud", 1, ProviderHealth.HEALTHY, {"not": "topology"})
        compiler = LatticeAutoConfigCompiler({"gateway": MutableReader(snapshot("gateway")), "cloud": MutableReader(malformed)})
        run = compiler.drain()

        self.assertEqual(run.status, CompileStatus.DEGRADED)
        self.assertEqual(dict(run.plan.provider_generations), {"gateway": 1})
        self.assertIn("provider_invalid", {issue.code for issue in run.issues})

    def test_compile_failure_preserves_last_known_good_as_stale(self):
        """A later global collision leaves the prior immutable plan active."""
        reader = MutableReader(snapshot("gateway", generation=1))
        compiler = LatticeAutoConfigCompiler({"gateway": reader})
        first = compiler.drain()
        last_known_good = first.plan

        bad_fragment = fragment("gateway", 2)
        bad_fragment["nodes"].append(dict(bad_fragment["nodes"][0]))
        reader.value = ProviderSnapshot("gateway", 2, ProviderHealth.HEALTHY, bad_fragment)
        self.assertTrue(compiler.invalidate("gateway", 2, "conflicting rediscovery"))
        failed = compiler.drain()

        self.assertEqual(failed.status, CompileStatus.STALE)
        self.assertIs(failed.plan, last_known_good)
        self.assertIn("compile_failed", {issue.code for issue in failed.issues})

    def test_all_offline_preserves_last_known_good_as_stale(self):
        """Temporary total provider loss cannot replace a working plan."""
        reader = MutableReader(snapshot("gateway", generation=1))
        compiler = LatticeAutoConfigCompiler({"gateway": reader})
        last_known_good = compiler.drain().plan

        reader.value = snapshot("gateway", generation=2, health=ProviderHealth.OFFLINE)
        self.assertTrue(compiler.invalidate("gateway", 2, "connection lost"))
        failed = compiler.drain()

        self.assertEqual(failed.status, CompileStatus.STALE)
        self.assertIs(failed.plan, last_known_good)
        self.assertEqual(
            {issue.code for issue in failed.issues},
            {"provider_offline", "active_provider_unavailable", "compile_failed"},
        )
        self.assertTrue(failed.pending)

    def test_unavailable_active_provider_cannot_materialize_destructive_removal(self):
        """A transient provider outage keeps the complete last-known-good plan."""
        gateway = MutableReader(snapshot("gateway", generation=1, node_id="GW1"))
        cloud = MutableReader(snapshot("cloud", generation=1, node_id="CLOUD1"))
        requests = []
        compiler = LatticeAutoConfigCompiler(
            {"gateway": gateway, "cloud": cloud},
            requests.append,
        )
        first = compiler.drain()
        last_known_good = first.plan

        cloud.value = snapshot(
            "cloud",
            generation=2,
            node_id="CLOUD1",
            health=ProviderHealth.OFFLINE,
        )
        self.assertTrue(compiler.invalidate("cloud", 2, "cloud unavailable"))
        failed = compiler.drain()

        self.assertEqual(failed.status, CompileStatus.STALE)
        self.assertIs(failed.plan, last_known_good)
        self.assertEqual(len(requests), 1)
        self.assertEqual(
            set(dict(failed.plan.provider_generations)),
            {"gateway", "cloud"},
        )
        self.assertIn(
            "active_provider_unavailable",
            {issue.code for issue in failed.issues},
        )
        self.assertTrue(failed.pending)

    def test_materialization_failure_is_retryable_without_new_generation(self):
        """Caller-driven retry can materialize the same complete generation."""
        reader = MutableReader(snapshot("gateway", generation=1))
        requests = []

        def materialize(request):
            """Fail once, then accept the exact same semantic plan."""
            requests.append(request)
            if len(requests) == 1:
                raise RuntimeError("temporary config store failure")

        compiler = LatticeAutoConfigCompiler({"gateway": reader}, materialize)
        failed = compiler.drain()

        self.assertEqual(failed.status, CompileStatus.STALE)
        self.assertIsNone(failed.plan)
        self.assertEqual(failed.materializations, 0)
        self.assertTrue(failed.pending)
        self.assertIn(
            "materialization_failed",
            {issue.code for issue in failed.issues},
        )

        recovered = compiler.drain()

        self.assertEqual(recovered.status, CompileStatus.FRESH)
        self.assertIsNotNone(recovered.plan)
        self.assertEqual(recovered.materializations, 1)
        self.assertFalse(recovered.pending)
        self.assertEqual(reader.calls, 2)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].plan.digest, requests[1].plan.digest)

    def test_unchanged_digest_skips_materialization(self):
        """A newer generation with identical semantics updates provenance only."""
        reader = MutableReader(snapshot("gateway", generation=1))
        requests = []
        compiler = LatticeAutoConfigCompiler({"gateway": reader}, requests.append)
        first = compiler.drain()

        reader.value = snapshot("gateway", generation=2)
        self.assertTrue(compiler.invalidate("gateway", 2, "heartbeat refresh"))
        second = compiler.drain()

        self.assertEqual(first.materializations, 1)
        self.assertEqual(second.materializations, 0)
        self.assertEqual(len(requests), 1)
        self.assertEqual(dict(second.plan.provider_generations), {"gateway": 2})

    def test_materializer_feedback_token_cannot_recompile(self):
        """A materializer-caused integration event is not a feedback loop."""
        reader = MutableReader(snapshot("gateway", generation=1))
        feedback_results = []
        holder = {}

        def materialize(request):
            """Echo the materializer token through the provider invalidation API."""
            feedback_results.append(holder["compiler"].invalidate("gateway", 2, "materialized config observed", request.feedback_token))

        compiler = LatticeAutoConfigCompiler({"gateway": reader}, materialize)
        holder["compiler"] = compiler
        run = compiler.drain()

        self.assertEqual(feedback_results, [False])
        self.assertEqual(run.attempts, 1)
        self.assertEqual(run.materializations, 1)
        self.assertFalse(run.pending)

    def test_every_attempt_fresh_reads_all_providers(self):
        """Independent invalidations still re-read the complete provider set."""
        gateway = MutableReader(snapshot("gateway", generation=1))
        cloud = MutableReader(snapshot("cloud", generation=1))
        compiler = LatticeAutoConfigCompiler({"gateway": gateway, "cloud": cloud})
        compiler.drain()

        gateway.value = snapshot("gateway", generation=2)
        self.assertTrue(compiler.invalidate("gateway", 2, "new gateway topology"))
        compiler.drain()

        self.assertEqual((gateway.calls, cloud.calls), (2, 2))


if __name__ == "__main__":
    unittest.main()
