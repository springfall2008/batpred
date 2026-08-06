"""Tests for the pure Gateway retained-topology Lattice fragment publisher."""

# cspell:ignore autoconfig materializable

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_autoconfig import (  # noqa: E402
    AliasRole,
    CompileStatus,
    ProviderHealth,
    compile_auto_config,
)
from lattice_compiled_publication import (  # noqa: E402
    InMemoryCompiledLatticeStateStore,
)
from lattice_fragment_adapters import (  # noqa: E402
    FragmentAdapterReadError,
    FragmentAdapterRegistry,
    FragmentAdapterRemoved,
    InMemoryFragmentAdapterStateStore,
)
from lattice_gateway_fragment import (  # noqa: E402
    GatewayCapabilityProjection,
    GatewayRetainedTopologyFragmentPublisher,
    GatewayTopologyProjection,
)
from lattice_topology import TopologyValidationError  # noqa: E402
from gateway_autoconfig import compile_gateway_auto_config  # noqa: E402


def topology(
    doc_version=1,
    provider="predbat-gateway",
    node_id="GW-INV-1",
    capability="battery.target_soc",
    online_binding=True,
):
    """Build one compact provider-owned retained Gateway fragment."""
    offer = {
        "capability": capability,
        "accessPath": "gateway-mqtt",
        "ref": 41,
        "shape": "setpoint",
        "read": {"protocol": "mqtt", "address": "state"},
    }
    if online_binding:
        offer["control"] = {"protocol": "mqtt", "address": "setpoint"}
    return {
        "topologyVersion": "0.3.0",
        "scope": "fragment",
        "docVersion": doc_version,
        "producer": {
            "name": "PredBat Gateway",
            "provider": provider,
            "authority": 10,
        },
        "nodes": [
            {
                "id": node_id,
                "kind": "inverter",
                "attributes": {"serial": node_id},
                "accessPaths": [
                    {
                        "id": "gateway-mqtt",
                        "provider": provider,
                        "preference": 10,
                    }
                ],
                "capabilities": [offer],
            }
        ],
    }


def publisher(enabled=True, state_store=None):
    """Build one reference publisher and its in-memory durable store."""
    if state_store is None:
        state_store = InMemoryFragmentAdapterStateStore()
    return (
        GatewayRetainedTopologyFragmentPublisher(
            "predbat-gateway",
            state_store,
            enabled=enabled,
        ),
        state_store,
    )


class TestGatewayRetainedTopologyFragmentPublisher(unittest.TestCase):
    """Gateway inputs publish immutable, REFERENCE-only provider snapshots."""

    def test_default_off_has_no_discovery_or_durable_side_effect(self):
        """Disabled construction never parses, publishes, or registers."""
        adapter, state_store = publisher(enabled=False)

        self.assertIsNone(adapter.lattice_fragment_adapter())
        self.assertFalse(adapter.ingest_retained_topology(b"{not json"))
        self.assertFalse(adapter.set_liveness(True))
        self.assertFalse(adapter.remove())
        self.assertEqual(state_store.writes, 0)
        with self.assertRaises(FragmentAdapterReadError):
            adapter.read_state()

    def test_seeded_store_failure_is_not_hidden_as_absent_discovery(self):
        """A registered provider read fault fails closed instead of disappearing."""

        class FailingAfterSeedStore(InMemoryFragmentAdapterStateStore):
            """Make durable reads fail only after a valid seed."""

            def __init__(self):
                super().__init__()
                self.fail_reads = False

            def load(self):
                if self.fail_reads:
                    raise OSError("durable Gateway fragment unavailable")
                return super().load()

        state_store = FailingAfterSeedStore()
        adapter, _state_store = publisher(state_store=state_store)
        adapter.ingest_retained_topology(topology(), online=True)
        state_store.fail_reads = True

        with self.assertRaisesRegex(
            FragmentAdapterReadError,
            "durable Gateway fragment unavailable",
        ):
            adapter.lattice_fragment_adapter()
        with self.assertRaisesRegex(
            FragmentAdapterReadError,
            "durable Gateway fragment unavailable",
        ):
            adapter.set_liveness(False)

    def test_retained_topology_publishes_immutable_reference_snapshot(self):
        """A retained fragment becomes detached provider-local metadata."""
        adapter, state_store = publisher()
        document = topology()

        self.assertTrue(adapter.ingest_retained_topology(document, online=True))
        document["nodes"][0]["id"] = "MUTATED"
        snapshot = adapter.read_snapshot()

        self.assertIs(adapter.lattice_fragment_adapter(), adapter)
        self.assertEqual(state_store.writes, 1)
        self.assertEqual(adapter.generation, 1)
        self.assertEqual(len(adapter.semantic_fingerprint), 64)
        self.assertEqual(snapshot.health, ProviderHealth.HEALTHY)
        self.assertEqual(snapshot.topology_fragment["nodes"][0]["id"], "GW-INV-1")
        self.assertEqual(snapshot.role_assignments, ())
        self.assertEqual(snapshot.config_projections, ())
        self.assertEqual(len(snapshot.aliases), 1)
        self.assertEqual(snapshot.aliases[0].roles, frozenset((AliasRole.REFERENCE,)))
        self.assertEqual(
            tuple((alias.kind, alias.value) for alias in snapshot.identity_aliases),
            (
                ("lattice-node-id", "GW-INV-1"),
                ("serial", "GW-INV-1"),
            ),
        )

    def test_pure_plan_publishes_materializable_roles_and_config(self):
        """A plan generation compiles real Gateway PredBat arguments."""
        adapter, _state_store = publisher()
        adapter.ingest_retained_topology(topology(), online=True)
        plan = compile_gateway_auto_config(
            (
                {
                    "serial": "GW-INV-1",
                    "kind": "inverter",
                    "battery_present": True,
                    "battery_capacity_wh": 9600,
                },
            )
        )

        self.assertTrue(adapter.ingest_auto_config(plan))
        snapshot = adapter.read_snapshot()
        compiled = compile_auto_config((snapshot,))

        self.assertTrue(snapshot.role_assignments)
        self.assertTrue(snapshot.config_projections)
        self.assertEqual(compiled.primary_targets[0].index, 0)
        self.assertEqual(compiled.control_targets[0].index, 0)
        self.assertEqual(compiled.projected_config["num_inverters"], 1)
        self.assertEqual(
            compiled.projected_config["battery_power"],
            ("sensor.predbat_gateway_-inv-1_battery_power",),
        )
        self.assertEqual(
            snapshot.identity_aliases[-1].value,
            "GW-INV-1",
        )

    def test_projection_metadata_retains_provider_local_capability_coordinates(self):
        """Projection inspection exposes refs but no materialization authority."""
        adapter, _state_store = publisher()
        adapter.ingest_retained_topology(topology(), online=None)

        projection = adapter.projection_metadata

        self.assertIsInstance(projection, GatewayTopologyProjection)
        self.assertEqual(projection.provider_id, "predbat-gateway")
        self.assertEqual(projection.health, ProviderHealth.DEGRADED)
        self.assertEqual(projection.document_version, 1)
        self.assertEqual(projection.node_ids, ("GW-INV-1",))
        self.assertEqual(
            projection.capabilities,
            (
                GatewayCapabilityProjection(
                    node_id="GW-INV-1",
                    capability="battery.target_soc",
                    access_path_id="gateway-mqtt",
                    cap_ref=41,
                    readable=True,
                    controllable=True,
                ),
            ),
        )

    def test_projection_metadata_requires_valid_bindings(self):
        """Malformed binding keys do not claim readable or controllable paths."""
        adapter, _state_store = publisher()
        document = topology()
        offer = document["nodes"][0]["capabilities"][0]
        offer["read"] = None
        offer["control"] = {"protocol": "mqtt"}

        adapter.ingest_retained_topology(document, online=True)

        self.assertEqual(
            adapter.projection_metadata.capabilities,
            (
                GatewayCapabilityProjection(
                    node_id="GW-INV-1",
                    capability="battery.target_soc",
                    access_path_id="gateway-mqtt",
                    cap_ref=41,
                    readable=False,
                    controllable=False,
                ),
            ),
        )

    def test_removed_nodes_publish_no_reference_or_projection_metadata(self):
        """A node tombstone cannot survive as an alias, identity, or capability."""
        adapter, _state_store = publisher()
        document = topology()
        document["nodes"][0]["removed"] = True

        adapter.ingest_retained_topology(document, online=True)
        snapshot = adapter.read_snapshot()
        projection = adapter.projection_metadata

        self.assertEqual(snapshot.aliases, ())
        self.assertEqual(snapshot.identity_aliases, ())
        self.assertEqual(projection.node_ids, ())
        self.assertEqual(projection.capabilities, ())

    def test_serial_and_stable_node_id_correlate_mixed_provider_views(self):
        """A provider with only the stable node id still joins a serial view."""
        gateway, _gateway_store = publisher()
        gateway.ingest_retained_topology(topology(), online=True)
        cloud_document = topology(provider="cloud")
        del cloud_document["nodes"][0]["attributes"]["serial"]
        cloud = GatewayRetainedTopologyFragmentPublisher(
            "cloud",
            InMemoryFragmentAdapterStateStore(),
            enabled=True,
        )
        cloud.ingest_retained_topology(cloud_document, online=True)

        plan = compile_auto_config(
            (
                gateway.read_snapshot(),
                cloud.read_snapshot(),
            )
        )

        self.assertEqual(len(plan.topology["nodes"]), 1)
        self.assertEqual(plan.topology["nodes"][0]["id"], "identity:serial:GW-INV-1")

    def test_retained_topology_version_matrix_is_explicit(self):
        """Retained topology stays on v0.2/v0.3 while v0.4 remains separate."""
        for version in ("0.2.0", "0.3.0"):
            with self.subTest(version=version):
                adapter, _state_store = publisher()
                document = topology()
                document["topologyVersion"] = version
                if version == "0.2.0":
                    del document["docVersion"]
                self.assertTrue(adapter.ingest_retained_topology(document))

        adapter, _state_store = publisher()
        document = topology()
        document["topologyVersion"] = "0.4.0"
        with self.assertRaisesRegex(
            TopologyValidationError,
            "unsupported topologyVersion '0.4.0'",
        ):
            adapter.ingest_retained_topology(document)

    def test_topology_and_liveness_changes_invalidate_for_fresh_recompile(self):
        """Every accepted input change notifies the common registry compiler."""
        adapter, _state_store = publisher()
        adapter.ingest_retained_topology(topology(), online=True)
        registry = FragmentAdapterRegistry(enabled=True)
        self.assertEqual(registry.discover((adapter,)), ("predbat-gateway",))
        compiler = registry.create_compiler(
            InMemoryCompiledLatticeStateStore(),
        )
        first = compiler.drain()
        self.assertEqual(first.status, CompileStatus.FRESH)
        plan = first.publication.plan
        self.assertFalse(plan.materialization_readiness.ready)
        self.assertEqual(plan.role_assignments, ())
        self.assertEqual(plan.primary_targets, ())
        self.assertEqual(plan.control_targets, ())
        self.assertEqual(plan.config_arguments, ())
        self.assertEqual(dict(plan.projected_config), {})

        self.assertTrue(adapter.set_liveness(False))
        offline = compiler.drain()
        self.assertEqual(offline.status, CompileStatus.STALE)
        self.assertTrue(offline.pending)
        self.assertEqual(offline.publication, first.publication)
        self.assertEqual(adapter.generation, 2)

        self.assertTrue(
            adapter.ingest_retained_topology(
                topology(doc_version=2, capability="battery.charge_power_limit"),
                online=True,
            )
        )
        changed = compiler.drain()
        self.assertEqual(changed.status, CompileStatus.FRESH)
        self.assertEqual(
            changed.publication.provider_generations,
            (("predbat-gateway", 3),),
        )

    def test_duplicate_stale_and_reused_versions_do_not_recompile(self):
        """Retained replay is idempotent and explicit generations fail closed."""
        adapter, state_store = publisher()
        reasons = []
        adapter.subscribe_invalidation(lambda provider_id, generation, reason, feedback_token: reasons.append((provider_id, generation, reason, feedback_token)))
        first = topology(doc_version=5)
        adapter.ingest_retained_topology(first, online=True)

        self.assertFalse(adapter.ingest_retained_topology(first, online=True))
        self.assertFalse(
            adapter.ingest_retained_topology(
                topology(doc_version=4),
                online=True,
            )
        )
        with self.assertRaisesRegex(TopologyValidationError, "reused docVersion"):
            adapter.ingest_retained_topology(
                topology(doc_version=5, capability="battery.charge_power_limit"),
                online=True,
            )
        self.assertEqual(adapter.generation, 1)
        self.assertEqual(state_store.writes, 1)
        self.assertEqual(len(reasons), 1)

    def test_legacy_retained_changes_use_adapter_owned_monotonic_generation(self):
        """Unversioned v0.2 arrival changes remain monotonic and replay-safe."""
        adapter, _state_store = publisher()
        first = topology(doc_version=1)
        second = topology(doc_version=1, capability="battery.charge_power_limit")
        for document in (first, second):
            document["topologyVersion"] = "0.2.0"
            del document["docVersion"]

        self.assertTrue(adapter.ingest_retained_topology(first, online=True))
        self.assertFalse(adapter.ingest_retained_topology(first, online=True))
        self.assertTrue(adapter.ingest_retained_topology(second, online=True))
        self.assertEqual(adapter.generation, 2)
        self.assertEqual(adapter.projection_metadata.document_version, 0)

    def test_invalid_or_foreign_fragment_preserves_durable_state(self):
        """Malformed ownership and site documents cannot replace good state."""
        adapter, state_store = publisher()
        adapter.ingest_retained_topology(topology(), online=True)
        before = adapter.read_state()

        with self.assertRaisesRegex(TopologyValidationError, "owned by cloud"):
            adapter.ingest_retained_topology(
                topology(doc_version=2, provider="cloud"),
                online=True,
            )
        site = topology(doc_version=2)
        site["scope"] = "site"
        with self.assertRaisesRegex(TopologyValidationError, "scope"):
            adapter.ingest_retained_topology(site, online=True)

        self.assertEqual(adapter.read_state(), before)
        self.assertEqual(state_store.writes, 1)

    def test_liveness_is_independent_idempotent_and_requires_topology(self):
        """LWT health changes preserve exact topology and skip duplicates."""
        unseeded, _store = publisher()
        with self.assertRaisesRegex(FragmentAdapterReadError, "seeded"):
            unseeded.set_liveness(True)

        adapter, state_store = publisher()
        adapter.ingest_retained_topology(topology(), online=None)
        before_fragment = adapter.read_snapshot().topology_fragment

        self.assertTrue(adapter.set_liveness(True))
        self.assertFalse(adapter.set_liveness(True))
        self.assertTrue(adapter.set_liveness(False))
        self.assertEqual(adapter.read_snapshot().health, ProviderHealth.OFFLINE)
        self.assertEqual(adapter.read_snapshot().topology_fragment, before_fragment)
        self.assertEqual(adapter.generation, 3)
        self.assertEqual(state_store.writes, 3)

    def test_topology_change_without_liveness_preserves_current_health(self):
        """An independent retained update does not invent a liveness change."""
        adapter, _state_store = publisher()
        adapter.ingest_retained_topology(topology(), online=True)

        adapter.ingest_retained_topology(
            topology(
                doc_version=2,
                capability="battery.charge_power_limit",
            )
        )

        self.assertEqual(adapter.read_snapshot().health, ProviderHealth.HEALTHY)
        self.assertEqual(adapter.generation, 2)

    def test_removal_is_durable_invalidating_and_restart_visible(self):
        """Integration removal publishes a tombstone rather than disappearing."""
        adapter, state_store = publisher()
        adapter.ingest_retained_topology(topology(), online=True)
        invalidations = []
        adapter.subscribe_invalidation(lambda provider_id, generation, reason, feedback_token: invalidations.append((provider_id, generation, reason)))

        self.assertTrue(adapter.remove())
        self.assertFalse(adapter.remove())
        self.assertTrue(adapter.read_state().removed)
        self.assertTrue(adapter.projection_metadata.removed)
        with self.assertRaises(FragmentAdapterRemoved):
            adapter.read_snapshot()
        self.assertEqual(
            invalidations[-1],
            ("predbat-gateway", 2, "gateway integration removed"),
        )

        restarted = GatewayRetainedTopologyFragmentPublisher(
            "predbat-gateway",
            state_store,
            enabled=True,
        )
        self.assertTrue(restarted.read_state().removed)
        self.assertTrue(restarted.projection_metadata.removed)
        with self.assertRaises(FragmentAdapterRemoved):
            restarted.read_snapshot()

    def test_removal_cannot_be_reversed_by_retained_replay(self):
        """Ambient retained topology cannot re-enrol a removed integration."""
        adapter, state_store = publisher()
        adapter.ingest_retained_topology(topology(doc_version=5), online=True)
        adapter.remove()
        before = adapter.read_state()

        with self.assertRaisesRegex(
            FragmentAdapterRemoved,
            "retained topology cannot re-enrol",
        ):
            adapter.ingest_retained_topology(topology(doc_version=1), online=True)

        self.assertEqual(adapter.read_state(), before)
        self.assertTrue(adapter.read_state().removed)
        self.assertEqual(state_store.writes, 2)


if __name__ == "__main__":
    unittest.main()
