"""Tests for the pure GivEnergy Cloud Lattice fragment publisher."""

# cspell:ignore autoconfig

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
    FragmentAdapterConflict,
    FragmentAdapterReadError,
    FragmentAdapterRegistry,
    FragmentAdapterRemoved,
    InMemoryFragmentAdapterStateStore,
)
from lattice_gateway_fragment import (  # noqa: E402
    GatewayRetainedTopologyFragmentPublisher,
)
from lattice_ge_cloud_fragment import (  # noqa: E402
    GECloudDeviceSnapshot,
    GECloudFragmentPublisher,
)


def device(
    serial="SA2243G277",
    kind="battery-inverter",
    online=True,
    model="All-In-One",
    site_id=61435,
    uuid=None,
):
    """Build one explicit provider-local cloud device snapshot."""
    return GECloudDeviceSnapshot(
        serial=serial,
        kind=kind,
        online=online,
        model=model,
        site_id=site_id,
        uuid=uuid,
    )


def publisher(enabled=True, state_store=None, provider_id="ge-cloud"):
    """Build one reference publisher and its in-memory durable store."""
    if state_store is None:
        state_store = InMemoryFragmentAdapterStateStore()
    return (
        GECloudFragmentPublisher(
            provider_id,
            state_store,
            enabled=enabled,
        ),
        state_store,
    )


def gateway_topology(serial="SA2243G277", doc_version=1):
    """Build a compact Gateway view of the same physical inverter."""
    return {
        "topologyVersion": "0.3.0",
        "scope": "fragment",
        "docVersion": doc_version,
        "producer": {
            "name": "PredBat Gateway",
            "provider": "predbat-gateway",
            "authority": 10,
        },
        "nodes": [
            {
                "id": "gateway-inverter",
                "kind": "inverter",
                "attributes": {"serial": serial},
                "accessPaths": [
                    {
                        "id": "gateway-mqtt",
                        "provider": "predbat-gateway",
                        "preference": 10,
                    }
                ],
                "capabilities": [],
            }
        ],
    }


class TestGECloudFragmentPublisher(unittest.TestCase):
    """Cloud discovery publishes immutable, REFERENCE-only snapshots."""

    def test_default_off_has_no_discovery_or_durable_side_effect(self):
        """Disabled construction never validates, publishes, or registers."""
        adapter, state_store = publisher(enabled=False)

        self.assertIsNone(adapter.lattice_fragment_adapter())
        self.assertFalse(adapter.ingest_discovery(-1, (object(),)))
        self.assertFalse(adapter.set_liveness(True))
        self.assertFalse(adapter.remove())
        self.assertEqual(state_store.writes, 0)
        with self.assertRaises(FragmentAdapterReadError):
            adapter.read_state()

    def test_discovery_publishes_immutable_reference_only_snapshot(self):
        """Explicit device state is detached and carries no write authority."""
        adapter, state_store = publisher()
        devices = [
            device(serial="sa2243g277"),
            device(
                serial="evc-1",
                kind="ev-charger",
                online=None,
                model="EVC",
                uuid="evc-uuid-1",
            ),
        ]

        self.assertTrue(
            adapter.ingest_discovery(
                7,
                devices,
                health=ProviderHealth.HEALTHY,
            )
        )
        devices.append(device(serial="MUTATED"))
        snapshot = adapter.read_snapshot()
        nodes = snapshot.topology_fragment["nodes"]

        self.assertIs(adapter.lattice_fragment_adapter(), adapter)
        self.assertEqual(state_store.writes, 1)
        self.assertEqual(adapter.generation, 1)
        self.assertEqual(adapter.discovery_version, 7)
        self.assertEqual(len(adapter.semantic_fingerprint), 64)
        self.assertEqual(snapshot.health, ProviderHealth.HEALTHY)
        self.assertEqual(nodes[1]["attributes"]["siteId"], "61435")
        self.assertEqual(
            tuple(node["id"] for node in nodes),
            ("ge-cloud:EVC-1", "ge-cloud:SA2243G277"),
        )
        self.assertEqual(snapshot.role_assignments, ())
        self.assertEqual(snapshot.config_projections, ())
        self.assertTrue(all(alias.roles == frozenset((AliasRole.REFERENCE,)) for alias in snapshot.aliases))
        self.assertTrue(all(node["capabilities"] == () for node in nodes))
        self.assertEqual(
            tuple((alias.kind, alias.value, alias.node_id) for alias in snapshot.identity_aliases),
            (
                ("ge-cloud-uuid", "evc-uuid-1", "ge-cloud:EVC-1"),
                ("serial", "EVC-1", "ge-cloud:EVC-1"),
                ("serial", "SA2243G277", "ge-cloud:SA2243G277"),
            ),
        )

    def test_device_order_and_serial_case_are_replay_stable(self):
        """Equivalent provider snapshots do not invent new generations."""
        adapter, state_store = publisher()
        first = (
            device(serial="inv-2", kind="pv-inverter"),
            device(serial="inv-1"),
        )
        replay = (
            device(serial="INV-1"),
            device(serial="INV-2", kind="pv_inverter"),
        )

        self.assertTrue(adapter.ingest_discovery(3, first, health=True))
        self.assertFalse(adapter.ingest_discovery(3, replay, health=True))
        self.assertEqual(adapter.generation, 1)
        self.assertEqual(state_store.writes, 1)

    def test_versions_are_monotonic_and_reuse_collision_fails_closed(self):
        """Stale replay is ignored and same-version content reuse is rejected."""
        adapter, state_store = publisher()
        adapter.ingest_discovery(5, (device(),), health=True)
        before = adapter.read_state()

        self.assertFalse(
            adapter.ingest_discovery(
                4,
                (device(model="Newer-looking stale data"),),
                health=False,
            )
        )
        with self.assertRaisesRegex(
            FragmentAdapterConflict,
            "reused GE Cloud discovery version 5",
        ):
            adapter.ingest_discovery(
                5,
                (device(model="Different"),),
            )

        self.assertEqual(adapter.read_state(), before)
        self.assertEqual(state_store.writes, 1)

    def test_serial_and_uuid_collisions_fail_before_publication(self):
        """Ambiguous strong identities cannot enter durable provider state."""
        adapter, state_store = publisher()

        with self.assertRaisesRegex(ValueError, "at least one"):
            adapter.ingest_discovery(1, ())
        with self.assertRaisesRegex(ValueError, "online"):
            device(online=1)
        with self.assertRaisesRegex(
            FragmentAdapterConflict,
            "duplicate serial SA2243G277",
        ):
            adapter.ingest_discovery(
                1,
                (device(), device(serial="sa2243g277")),
            )
        with self.assertRaisesRegex(
            FragmentAdapterConflict,
            "belongs to both INV-1 and INV-2",
        ):
            adapter.ingest_discovery(
                1,
                (
                    device(serial="INV-1", uuid="shared-uuid"),
                    device(serial="INV-2", uuid="shared-uuid"),
                ),
            )

        self.assertEqual(state_store.writes, 0)
        with self.assertRaises(FragmentAdapterReadError):
            adapter.read_state()

    def test_discovery_and_liveness_changes_trigger_fresh_read_composition(self):
        """Every accepted integration change invalidates the common compiler."""
        adapter, _state_store = publisher()
        adapter.ingest_discovery(1, (device(),), health=True)
        reasons = []
        adapter.subscribe_invalidation(
            lambda provider_id, generation, reason, feedback_token: reasons.append(
                (provider_id, generation, reason, feedback_token),
            )
        )
        registry = FragmentAdapterRegistry(enabled=True)
        self.assertEqual(registry.discover((adapter,)), ("ge-cloud",))
        compiler = registry.create_compiler(
            InMemoryCompiledLatticeStateStore(),
        )

        first = compiler.drain()
        self.assertEqual(first.status, CompileStatus.FRESH)
        self.assertEqual(
            first.publication.provider_generations,
            (("ge-cloud", 1),),
        )

        self.assertTrue(
            adapter.ingest_discovery(
                2,
                (
                    device(),
                    device(serial="PV-1", kind="pv-inverter"),
                ),
            )
        )
        changed = compiler.drain()
        self.assertEqual(changed.status, CompileStatus.FRESH)
        self.assertEqual(
            changed.publication.provider_generations,
            (("ge-cloud", 2),),
        )
        self.assertEqual(
            tuple(node["id"] for node in changed.publication.plan.topology["nodes"]),
            ("identity:serial:PV-1", "identity:serial:SA2243G277"),
        )

        self.assertTrue(adapter.set_liveness(False))
        offline = compiler.drain()
        self.assertEqual(offline.status, CompileStatus.STALE)
        self.assertTrue(offline.pending)
        self.assertEqual(adapter.generation, 3)
        self.assertEqual(
            tuple(reasons),
            (
                (
                    "ge-cloud",
                    2,
                    "GE Cloud discovery changed to version 2",
                    None,
                ),
                (
                    "ge-cloud",
                    3,
                    "GE Cloud liveness changed to offline",
                    None,
                ),
            ),
        )

    def test_liveness_is_independent_monotonic_and_preserves_topology(self):
        """Provider health changes retain exact discovery contents."""
        adapter, state_store = publisher()
        with self.assertRaisesRegex(FragmentAdapterReadError, "seeded"):
            adapter.set_liveness(True)
        adapter.ingest_discovery(1, (device(),))
        topology = adapter.read_snapshot().topology_fragment

        self.assertTrue(adapter.set_liveness(True))
        self.assertFalse(adapter.set_liveness(True))
        self.assertTrue(adapter.set_liveness(None))
        self.assertEqual(adapter.read_snapshot().health, ProviderHealth.DEGRADED)
        self.assertEqual(adapter.read_snapshot().topology_fragment, topology)
        self.assertEqual(adapter.generation, 3)
        self.assertEqual(state_store.writes, 3)

    def test_serial_identity_composes_with_gateway_without_materialization(self):
        """Cloud and Gateway references join without creating control roles."""
        cloud, _cloud_store = publisher()
        cloud.ingest_discovery(1, (device(),), health=True)
        gateway = GatewayRetainedTopologyFragmentPublisher(
            "predbat-gateway",
            InMemoryFragmentAdapterStateStore(),
            enabled=True,
        )
        gateway.ingest_retained_topology(
            gateway_topology(),
            online=True,
        )

        plan = compile_auto_config(
            (
                gateway.read_snapshot(),
                cloud.read_snapshot(),
            )
        )

        self.assertEqual(len(plan.topology["nodes"]), 1)
        self.assertEqual(
            plan.topology["nodes"][0]["id"],
            "identity:serial:SA2243G277",
        )
        self.assertEqual(
            plan.provider_generations,
            (("ge-cloud", 1), ("predbat-gateway", 1)),
        )
        self.assertEqual(plan.role_assignments, ())
        self.assertEqual(plan.primary_targets, ())
        self.assertEqual(plan.control_targets, ())
        self.assertEqual(plan.config_arguments, ())
        self.assertEqual(dict(plan.projected_config), {})
        self.assertFalse(plan.materialization_readiness.ready)

    def test_removal_is_durable_invalidating_and_irreversible_by_replay(self):
        """Ordinary discovery replay cannot re-enrol a removed integration."""
        adapter, state_store = publisher()
        adapter.ingest_discovery(5, (device(),), health=True)
        invalidations = []
        adapter.subscribe_invalidation(lambda provider_id, generation, reason, feedback_token: (invalidations.append((provider_id, generation, reason))))

        self.assertTrue(adapter.remove())
        self.assertFalse(adapter.remove())
        before = adapter.read_state()
        self.assertTrue(before.removed)
        with self.assertRaises(FragmentAdapterRemoved):
            adapter.read_snapshot()
        with self.assertRaisesRegex(
            FragmentAdapterRemoved,
            "discovery cannot re-enrol",
        ):
            adapter.ingest_discovery(1, (device(),), health=True)
        self.assertEqual(adapter.read_state(), before)
        self.assertEqual(state_store.writes, 2)
        self.assertEqual(
            invalidations[-1],
            ("ge-cloud", 2, "GE Cloud integration removed"),
        )

        restarted = GECloudFragmentPublisher(
            "ge-cloud",
            state_store,
            enabled=True,
        )
        self.assertTrue(restarted.read_state().removed)
        with self.assertRaises(FragmentAdapterRemoved):
            restarted.read_snapshot()
        with self.assertRaises(FragmentAdapterRemoved):
            restarted.ingest_discovery(99, (device(),))


if __name__ == "__main__":
    unittest.main()
