"""Tests for pure Lattice fragment auto-configuration compilation."""

# cspell:ignore autoconfig

import os
import sys
import threading
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_autoconfig import (
    AliasRole,
    AutoConfigCompileError,
    CompileStatus,
    LatticeAutoConfigCompiler,
    MaterializationReadiness,
    ProviderAlias,
    ProviderHealth,
    ProviderIdentityAlias,
    ProviderRoleAssignment,
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


def snapshot(
    provider,
    generation=1,
    node_id="INV1",
    kind="inverter",
    health=ProviderHealth.HEALTHY,
    aliases=(),
    identity_aliases=(),
    role_assignments=(),
):
    """Build one typed provider snapshot."""
    return ProviderSnapshot(
        provider,
        generation,
        health,
        fragment(provider, generation, node_id=node_id, kind=kind),
        aliases,
        identity_aliases,
        role_assignments,
    )


def ready_plan():
    """Copy a compiled shadow plan into a future-materializer test harness."""
    plan = compile_auto_config((snapshot("gateway"),))
    return replace(
        plan,
        materialization_readiness=MaterializationReadiness(True, ()),
    )


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

    def test_materialization_readiness_rejects_inconsistent_state(self):
        """Readiness is derived exactly from a normalized blocker tuple."""
        readiness = MaterializationReadiness(
            False,
            [" config_projection_bindings_missing "],
        )

        self.assertEqual(
            readiness.blockers,
            ("config_projection_bindings_missing",),
        )
        with self.assertRaisesRegex(ValueError, "absence of blockers"):
            MaterializationReadiness(True, ("projection_missing",))
        with self.assertRaisesRegex(ValueError, "absence of blockers"):
            MaterializationReadiness(False, ())
        with self.assertRaisesRegex(ValueError, "unique"):
            MaterializationReadiness(False, ("projection_missing", "projection_missing"))
        with self.assertRaisesRegex(ValueError, "non-empty"):
            MaterializationReadiness(False, (" ",))
        with self.assertRaisesRegex(ValueError, "iterable of strings"):
            MaterializationReadiness(False, "projection_missing")

    def test_order_independent_digest_and_provider_qualified_aliases(self):
        """Input order and shared local alias names cannot alter a plan."""
        gateway_alias = ProviderAlias("battery", "INV1", frozenset((AliasRole.REFERENCE, AliasRole.PRIMARY, AliasRole.CONTROL)))
        cloud_alias = ProviderAlias("battery", "INV1")
        gateway = snapshot("gateway", aliases=(gateway_alias,), identity_aliases=(ProviderIdentityAlias("serial", "SER123", "INV1"),))
        cloud = snapshot("cloud", aliases=(cloud_alias,), identity_aliases=(ProviderIdentityAlias("serial", "SER123", "INV1"),))

        left = compile_auto_config((gateway, cloud))
        right = compile_auto_config((cloud, gateway))

        self.assertEqual(left.digest, right.digest)
        self.assertEqual([binding.qualified_name for binding in left.aliases], ["cloud:battery", "gateway:battery"])
        self.assertEqual(dict(left.provider_generations), {"cloud": 1, "gateway": 1})
        self.assertEqual({field.name for field in left.fields}, {"alias.cloud:battery", "alias.gateway:battery", "control_target", "primary_target"})
        self.assertEqual(left.primary_target, "identity:serial:SER123")
        self.assertEqual(left.control_target, "identity:serial:SER123")
        self.assertEqual(left.primary_targets, ())
        self.assertEqual(left.control_targets, ())
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
        with self.assertRaisesRegex(AutoConfigCompileError, "ambiguous legacy primary"):
            compile_auto_config((snapshot("gateway", node_id="INV1", aliases=(primary_a,)), snapshot("cloud", node_id="INV2", aliases=(primary_b,))))

        control_a = ProviderAlias("control", "INV1", frozenset((AliasRole.CONTROL,)))
        control_b = ProviderAlias("control", "INV2", frozenset((AliasRole.CONTROL,)))
        with self.assertRaisesRegex(AutoConfigCompileError, "ambiguous legacy control"):
            compile_auto_config((snapshot("gateway", node_id="INV1", aliases=(control_a,)), snapshot("cloud", node_id="INV2", aliases=(control_b,))))

    def test_alias_must_target_provider_local_identity(self):
        """An alias cannot smuggle a target owned only by another provider."""
        bad = ProviderAlias("battery", "INV2")
        with self.assertRaisesRegex(AutoConfigCompileError, "unknown provider-local node"):
            compile_auto_config((snapshot("gateway", node_id="INV1", aliases=(bad,)), snapshot("cloud", node_id="INV2")))

    def test_indexed_roles_are_order_independent_and_contiguous(self):
        """Indexed targets sort by group, role, and index regardless of input order."""
        first = snapshot(
            "a",
            node_id="A",
            role_assignments=(
                ProviderRoleAssignment(AliasRole.CONTROL, "battery", 1, "A"),
                ProviderRoleAssignment(AliasRole.PRIMARY, "battery", 0, "A"),
            ),
        )
        second = snapshot(
            "z",
            node_id="Z",
            role_assignments=(
                ProviderRoleAssignment(AliasRole.PRIMARY, "battery", 1, "Z"),
                ProviderRoleAssignment(AliasRole.CONTROL, "battery", 0, "Z"),
            ),
        )

        left = compile_auto_config((second, first))
        right = compile_auto_config((first, second))

        self.assertEqual(left.digest, right.digest)
        self.assertEqual(
            [(target.group, target.index, target.node_id) for target in left.primary_targets],
            [
                ("battery", 0, "provider:a:A"),
                ("battery", 1, "provider:z:Z"),
            ],
        )
        self.assertEqual(
            [(target.group, target.index, target.node_id) for target in left.control_targets],
            [
                ("battery", 0, "provider:z:Z"),
                ("battery", 1, "provider:a:A"),
            ],
        )
        self.assertEqual(
            [(item.group, item.role, item.index) for item in left.role_assignments],
            sorted((item.group, item.role, item.index) for item in left.role_assignments),
        )
        self.assertEqual(
            {field.name for field in left.fields if field.name.startswith(("primary_targets", "control_targets"))},
            {
                "primary_targets.battery.0",
                "primary_targets.battery.1",
                "control_targets.battery.0",
                "control_targets.battery.1",
            },
        )
        indexed_fields = [field for field in left.fields if field.name.startswith(("primary_targets", "control_targets"))]
        self.assertTrue(all(field.provenance for field in indexed_fields))
        self.assertTrue(all(item in left.provenance for field in indexed_fields for item in field.provenance))
        self.assertFalse(left.materialization_readiness.ready)
        self.assertEqual(left.materialization_readiness.blockers, ("config_projection_bindings_missing",))
        with self.assertRaises(AttributeError):
            left.primary_targets[0].node_id = "changed"

    def test_provider_role_assignment_rejects_reference_and_negative_index(self):
        """Only indexed primary/control assignments with non-negative indices exist."""
        with self.assertRaisesRegex(ValueError, "PRIMARY or CONTROL"):
            ProviderRoleAssignment(AliasRole.REFERENCE, "battery", 0, "INV1")
        with self.assertRaisesRegex(ValueError, "non-negative"):
            ProviderRoleAssignment(AliasRole.PRIMARY, "battery", -1, "INV1")

    def test_indexed_role_indices_must_be_contiguous_per_group_and_role(self):
        """A gap in one role sequence fails without affecting another sequence."""
        assignments = (
            ProviderRoleAssignment(AliasRole.PRIMARY, "battery", 0, "INV1"),
            ProviderRoleAssignment(AliasRole.PRIMARY, "battery", 2, "INV1"),
        )
        with self.assertRaisesRegex(AutoConfigCompileError, "indices must be contiguous"):
            compile_auto_config((snapshot("gateway", role_assignments=assignments),))

    def test_correlated_providers_may_share_one_index(self):
        """Explicit strong identity correlation permits duplicate provider assertions."""
        gateway_identity = ProviderIdentityAlias("serial", "SER123", "gw")
        cloud_identity = ProviderIdentityAlias("serial", "SER123", "cloud")
        gateway_role = ProviderRoleAssignment(AliasRole.PRIMARY, "battery", 0, "gw")
        cloud_role = ProviderRoleAssignment(AliasRole.PRIMARY, "battery", 0, "cloud")
        plan = compile_auto_config(
            (
                snapshot("gateway", node_id="gw", identity_aliases=(gateway_identity,), role_assignments=(gateway_role,)),
                snapshot("cloud", node_id="cloud", identity_aliases=(cloud_identity,), role_assignments=(cloud_role,)),
            )
        )

        self.assertEqual(len(plan.primary_targets), 1)
        self.assertEqual(plan.primary_targets[0].node_id, "identity:serial:SER123")
        self.assertEqual({item.provider_id for item in plan.primary_targets[0].provenance}, {"gateway", "cloud"})

    def test_uncorrelated_providers_conflicting_at_one_index_fail_closed(self):
        """Equal role slots cannot select unrelated provider-local nodes."""
        gateway_role = ProviderRoleAssignment(AliasRole.PRIMARY, "battery", 0, "gw")
        cloud_role = ProviderRoleAssignment(AliasRole.PRIMARY, "battery", 0, "cloud")
        with self.assertRaisesRegex(AutoConfigCompileError, "conflicting primary target"):
            compile_auto_config(
                (
                    snapshot("gateway", node_id="gw", role_assignments=(gateway_role,)),
                    snapshot("cloud", node_id="cloud", role_assignments=(cloud_role,)),
                )
            )

    def test_same_node_may_fill_multiple_indices(self):
        """An EMS aggregate can fan one canonical node out over several indices."""
        assignments = (
            ProviderRoleAssignment(AliasRole.PRIMARY, "ems", 0, "EMS"),
            ProviderRoleAssignment(AliasRole.PRIMARY, "ems", 1, "EMS"),
        )
        plan = compile_auto_config((snapshot("ge-cloud", node_id="EMS", role_assignments=assignments),))

        self.assertEqual([target.node_id for target in plan.primary_targets], ["provider:ge-cloud:EMS", "provider:ge-cloud:EMS"])

    def test_legacy_and_indexed_role_assignments_cannot_mix(self):
        """A plan must use exactly one target-addressing model."""
        legacy = ProviderAlias("battery", "INV1", frozenset((AliasRole.PRIMARY,)))
        indexed = ProviderRoleAssignment(AliasRole.CONTROL, "battery", 0, "INV1")
        with self.assertRaisesRegex(AutoConfigCompileError, "cannot be mixed"):
            compile_auto_config((snapshot("gateway", aliases=(legacy,), role_assignments=(indexed,)),))

    def test_multiple_legacy_assignments_are_ambiguous_even_when_correlated(self):
        """The singular compatibility field represents exactly one assertion."""
        gateway_alias = ProviderAlias("battery", "gw", frozenset((AliasRole.PRIMARY,)))
        cloud_alias = ProviderAlias("battery", "cloud", frozenset((AliasRole.PRIMARY,)))
        gateway_identity = ProviderIdentityAlias("serial", "SER123", "gw")
        cloud_identity = ProviderIdentityAlias("serial", "SER123", "cloud")
        with self.assertRaisesRegex(AutoConfigCompileError, "ambiguous legacy primary"):
            compile_auto_config(
                (
                    snapshot("gateway", node_id="gw", aliases=(gateway_alias,), identity_aliases=(gateway_identity,)),
                    snapshot("cloud", node_id="cloud", aliases=(cloud_alias,), identity_aliases=(cloud_identity,)),
                )
            )

    def test_reference_only_plan_is_explicitly_shadow_only(self):
        """Reference discovery compiles but exposes every write blocker."""
        reference = ProviderAlias("battery", "INV1")
        plan = compile_auto_config((snapshot("gateway", aliases=(reference,)),))

        self.assertFalse(plan.materialization_readiness.ready)
        self.assertEqual(
            plan.materialization_readiness.blockers,
            (
                "indexed_primary_targets_missing",
                "indexed_control_targets_missing",
                "config_projection_bindings_missing",
            ),
        )
        self.assertIsNone(plan.primary_target)
        self.assertIsNone(plan.control_target)


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
        self.assertEqual(requests, [])
        self.assertEqual(run.materializations, 0)
        self.assertFalse(run.plan.materialization_readiness.ready)
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
        self.assertEqual(len(requests), 0)
        self.assertEqual(
            set(dict(failed.plan.provider_generations)),
            {"gateway", "cloud"},
        )
        self.assertIn(
            "active_provider_unavailable",
            {issue.code for issue in failed.issues},
        )
        self.assertTrue(failed.pending)

    def test_shadow_only_plan_never_invokes_materializer(self):
        """A not-ready plan remains observable without reaching a write callback."""
        reader = MutableReader(snapshot("gateway", generation=1))
        requests = []

        def materialize(request):
            """Record any unsafe hand-off to make the test fail."""
            requests.append(request)

        compiler = LatticeAutoConfigCompiler({"gateway": reader}, materialize)
        run = compiler.drain()

        self.assertEqual(run.status, CompileStatus.FRESH)
        self.assertIsNotNone(run.plan)
        self.assertFalse(run.plan.materialization_readiness.ready)
        self.assertEqual(run.materializations, 0)
        self.assertFalse(run.pending)
        self.assertEqual(requests, [])

    def test_unchanged_digest_skips_materialization(self):
        """A newer generation with identical semantics updates provenance only."""
        reader = MutableReader(snapshot("gateway", generation=1))
        requests = []
        compiler = LatticeAutoConfigCompiler({"gateway": reader}, requests.append)
        first = compiler.drain()

        reader.value = snapshot("gateway", generation=2)
        self.assertTrue(compiler.invalidate("gateway", 2, "heartbeat refresh"))
        second = compiler.drain()

        self.assertEqual(first.materializations, 0)
        self.assertEqual(second.materializations, 0)
        self.assertEqual(len(requests), 0)
        self.assertEqual(first.plan.digest, second.plan.digest)
        self.assertEqual(dict(second.plan.provider_generations), {"gateway": 2})

    def test_shadow_plan_cannot_create_materializer_feedback(self):
        """A blocked hand-off cannot cause an integration feedback loop."""
        reader = MutableReader(snapshot("gateway", generation=1))
        feedback_results = []
        holder = {}

        def materialize(request):
            """Echo the materializer token through the provider invalidation API."""
            feedback_results.append(holder["compiler"].invalidate("gateway", 2, "materialized config observed", request.feedback_token))

        compiler = LatticeAutoConfigCompiler({"gateway": reader}, materialize)
        holder["compiler"] = compiler
        run = compiler.drain()

        self.assertEqual(feedback_results, [])
        self.assertEqual(run.attempts, 1)
        self.assertEqual(run.materializations, 0)
        self.assertFalse(run.pending)

    def test_ready_plan_retries_after_materializer_failure(self):
        """A failed hand-off is not treated as a successful materialization."""
        requests = []

        def materialize(request):
            """Fail the first hand-off and accept the retry."""
            requests.append(request)
            if len(requests) == 1:
                raise RuntimeError("temporary write failure")

        compiler = LatticeAutoConfigCompiler(materializer=materialize)
        plan = ready_plan()

        count, issues = compiler._materialize_if_changed(plan)
        self.assertEqual(count, 0)
        self.assertEqual([issue.code for issue in issues], ["materialization_failed"])

        count, issues = compiler._materialize_if_changed(plan)
        self.assertEqual(count, 1)
        self.assertEqual(issues, ())
        self.assertEqual(len(requests), 2)

    def test_ready_plan_unchanged_digest_skips_materialization(self):
        """Bookkeeping-only generation changes do not repeat a ready hand-off."""
        requests = []
        compiler = LatticeAutoConfigCompiler(materializer=requests.append)
        plan = ready_plan()

        count, issues = compiler._materialize_if_changed(plan)
        self.assertEqual((count, issues), (1, ()))
        compiler._active_plan = plan
        newer_generation = replace(
            plan,
            provider_generations=(("gateway", 2),),
        )

        count, issues = compiler._materialize_if_changed(newer_generation)
        self.assertEqual((count, issues), (0, ()))
        self.assertEqual(len(requests), 1)

    def test_ready_plan_suppresses_materializer_feedback_token(self):
        """A ready hand-off cannot invalidate itself through its feedback token."""
        feedback_results = []
        holder = {}

        def materialize(request):
            """Echo the hand-off token through the provider invalidation API."""
            feedback_results.append(
                holder["compiler"].invalidate(
                    "gateway",
                    2,
                    "materialized config observed",
                    request.feedback_token,
                )
            )

        compiler = LatticeAutoConfigCompiler(
            {"gateway": MutableReader(snapshot("gateway"))},
            materialize,
        )
        holder["compiler"] = compiler

        count, issues = compiler._materialize_if_changed(ready_plan())

        self.assertEqual((count, issues), (1, ()))
        self.assertEqual(feedback_results, [False])
        self.assertTrue(
            compiler.invalidate(
                "gateway",
                2,
                "independent provider change",
            )
        )

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
