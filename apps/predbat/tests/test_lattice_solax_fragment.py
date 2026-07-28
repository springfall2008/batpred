"""Tests for the pure SolaX Cloud Lattice fragment publisher."""

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
from lattice_ge_cloud_fragment import (  # noqa: E402
    GECloudDeviceSnapshot,
    GECloudFragmentPublisher,
)
from lattice_solax_fragment import (  # noqa: E402
    SolaxCloudFragmentPublisher,
    SolaxDeviceSnapshot,
    SolaxPlantSnapshot,
)


def plant(plant_id="1618699116555534337", name="Home"):
    """Build one explicit provider-local SolaX plant snapshot."""
    return SolaxPlantSnapshot(
        plant_id=plant_id,
        name=name,
    )


def device(
    serial="H1231231932123",
    plant_id="1618699116555534337",
    kind="inverter",
    online=1,
    model=14,
    synthetic=False,
    source_serial=None,
):
    """Build one explicit provider-local SolaX device snapshot."""
    return SolaxDeviceSnapshot(
        serial=serial,
        plant_id=plant_id,
        kind=kind,
        online=online,
        model=model,
        synthetic=synthetic,
        source_serial=source_serial,
    )


def publisher(enabled=True, state_store=None, provider_id="solax-cloud"):
    """Build one reference publisher and its in-memory durable store."""
    if state_store is None:
        state_store = InMemoryFragmentAdapterStateStore()
    return (
        SolaxCloudFragmentPublisher(
            provider_id,
            state_store,
            enabled=enabled,
        ),
        state_store,
    )


class TestSolaxCloudFragmentPublisher(unittest.TestCase):
    """SolaX discovery publishes immutable, REFERENCE-only snapshots."""

    def test_default_off_has_no_discovery_or_durable_side_effect(self):
        """Disabled construction never validates, publishes, or registers."""
        adapter, state_store = publisher(enabled=False)

        self.assertIsNone(adapter.lattice_fragment_adapter())
        self.assertFalse(adapter.ingest_discovery(-1, (object(),), (object(),)))
        self.assertFalse(adapter.set_liveness(True))
        self.assertFalse(adapter.remove())
        self.assertEqual(state_store.writes, 0)
        with self.assertRaises(FragmentAdapterReadError):
            adapter.read_state()

    def test_discovery_publishes_immutable_reference_only_snapshot(self):
        """Plant/device state is detached and carries no write authority."""
        adapter, state_store = publisher()
        plants = [plant()]
        devices = [
            device(),
            device(
                serial="TP123456123123",
                kind="battery",
                model=1,
            ),
        ]

        self.assertTrue(
            adapter.ingest_discovery(
                7,
                plants,
                devices,
                health=ProviderHealth.HEALTHY,
            )
        )
        plants.append(plant("MUTATED"))
        devices.append(device(serial="MUTATED"))
        snapshot = adapter.read_snapshot()
        nodes = snapshot.topology_fragment["nodes"]

        self.assertIs(adapter.lattice_fragment_adapter(), adapter)
        self.assertEqual(state_store.writes, 1)
        self.assertEqual(adapter.generation, 1)
        self.assertEqual(adapter.discovery_version, 7)
        self.assertEqual(len(adapter.semantic_fingerprint), 64)
        self.assertEqual(snapshot.health, ProviderHealth.HEALTHY)
        self.assertEqual(
            tuple(node["id"] for node in nodes),
            (
                "solax:plant:1618699116555534337",
                "solax:device:TP123456123123",
                "solax:device:H1231231932123",
            ),
        )
        self.assertEqual(
            nodes[1]["attributes"]["model"],
            "1",
        )
        self.assertEqual(snapshot.role_assignments, ())
        self.assertEqual(snapshot.config_projections, ())
        self.assertTrue(all(node["capabilities"] == () for node in nodes))
        self.assertTrue(all(alias.roles == frozenset((AliasRole.REFERENCE,)) for alias in snapshot.aliases))
        self.assertEqual(
            tuple((alias.kind, alias.value, alias.node_id) for alias in snapshot.identity_aliases),
            (
                (
                    "serial",
                    "H1231231932123",
                    "solax:device:H1231231932123",
                ),
                (
                    "serial",
                    "TP123456123123",
                    "solax:device:TP123456123123",
                ),
                (
                    "solax-plant-id",
                    "1618699116555534337",
                    "solax:plant:1618699116555534337",
                ),
            ),
        )
        self.assertEqual(
            tuple(
                (
                    relationship["from"],
                    relationship["to"],
                    relationship["type"],
                )
                for relationship in snapshot.topology_fragment["relationships"]
            ),
            (
                (
                    "solax:plant:1618699116555534337",
                    "solax:device:TP123456123123",
                    "contains",
                ),
                (
                    "solax:plant:1618699116555534337",
                    "solax:device:H1231231932123",
                    "contains",
                ),
            ),
        )

    def test_order_case_and_online_encoding_are_replay_stable(self):
        """Equivalent cloud snapshots do not invent a new generation."""
        adapter, state_store = publisher()
        first = (
            device(serial="inv-1", online=1),
            device(serial="bat-1", kind="battery", online=0),
        )
        replay = (
            device(serial="BAT-1", kind="battery", online=False),
            device(serial="INV-1", online=True),
        )

        self.assertTrue(
            adapter.ingest_discovery(
                3,
                (plant(),),
                first,
                health=True,
            )
        )
        self.assertFalse(
            adapter.ingest_discovery(
                3,
                (plant(),),
                replay,
                health=True,
            )
        )
        self.assertEqual(adapter.generation, 1)
        self.assertEqual(state_store.writes, 1)

    def test_versions_are_monotonic_and_reuse_collision_fails_closed(self):
        """Stale replay is ignored and same-version content reuse is rejected."""
        adapter, state_store = publisher()
        adapter.ingest_discovery(5, (plant(),), (device(),), health=True)
        before = adapter.read_state()

        self.assertFalse(
            adapter.ingest_discovery(
                4,
                (plant(name="Stale"),),
                (device(model=99),),
                health=False,
            )
        )
        with self.assertRaisesRegex(
            FragmentAdapterConflict,
            "reused SolaX discovery version 5",
        ):
            adapter.ingest_discovery(
                5,
                (plant(name="Different"),),
                (device(),),
            )

        self.assertEqual(adapter.read_state(), before)
        self.assertEqual(state_store.writes, 1)

    def test_collisions_and_unknown_plants_fail_before_publication(self):
        """Ambiguous provider identities cannot enter durable state."""
        adapter, state_store = publisher()

        with self.assertRaisesRegex(ValueError, "at least one"):
            adapter.ingest_discovery(1, (), (device(),))
        with self.assertRaisesRegex(ValueError, "name"):
            plant(name=1)
        with self.assertRaisesRegex(ValueError, "unknown plant"):
            adapter.ingest_discovery(
                1,
                (plant(),),
                (device(plant_id="other"),),
            )
        with self.assertRaisesRegex(
            FragmentAdapterConflict,
            "belongs to both plants",
        ):
            adapter.ingest_discovery(
                1,
                (plant(), plant("other")),
                (
                    device(),
                    device(plant_id="other"),
                ),
            )

        self.assertEqual(state_store.writes, 0)
        with self.assertRaises(FragmentAdapterReadError):
            adapter.read_state()

    def test_synthetic_battery_has_no_false_hardware_identity(self):
        """SolaX placeholder batteries remain provider-local references."""
        adapter, _state_store = publisher()
        synthetic = device(
            serial="H1231231932123_battery",
            kind="battery",
            synthetic=True,
            source_serial="H1231231932123",
        )

        adapter.ingest_discovery(
            1,
            (plant(),),
            (device(), synthetic),
            health=True,
        )
        snapshot = adapter.read_snapshot()
        identity_values = tuple((alias.kind, alias.value) for alias in snapshot.identity_aliases)

        self.assertIn(("serial", "H1231231932123"), identity_values)
        self.assertNotIn(("serial", "H1231231932123_BATTERY"), identity_values)
        self.assertEqual(
            snapshot.topology_fragment["nodes"][1]["id"],
            ("solax:synthetic:1618699116555534337:" "battery:H1231231932123"),
        )
        self.assertEqual(
            snapshot.topology_fragment["nodes"][1]["attributes"]["sourceSerial"],
            "H1231231932123",
        )

    def test_accepted_changes_trigger_common_fresh_read_composition(self):
        """Discovery/device/liveness changes invalidate the common compiler."""
        adapter, _state_store = publisher()
        adapter.ingest_discovery(
            1,
            (plant(),),
            (device(),),
            health=True,
        )
        reasons = []
        adapter.subscribe_invalidation(
            lambda provider_id, generation, reason, feedback_token: reasons.append(
                (provider_id, generation, reason, feedback_token),
            )
        )
        registry = FragmentAdapterRegistry(enabled=True)
        self.assertEqual(registry.discover((adapter,)), ("solax-cloud",))
        compiler = registry.create_compiler(
            InMemoryCompiledLatticeStateStore(),
        )

        first = compiler.drain()
        self.assertEqual(first.status, CompileStatus.FRESH)
        self.assertEqual(
            first.publication.provider_generations,
            (("solax-cloud", 1),),
        )

        self.assertTrue(
            adapter.ingest_discovery(
                2,
                (plant(),),
                (
                    device(),
                    device(serial="BAT-1", kind="battery"),
                ),
            )
        )
        changed = compiler.drain()
        self.assertEqual(changed.status, CompileStatus.FRESH)
        self.assertEqual(
            changed.publication.provider_generations,
            (("solax-cloud", 2),),
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
                    "solax-cloud",
                    2,
                    "SolaX discovery changed to version 2",
                    None,
                ),
                (
                    "solax-cloud",
                    3,
                    "SolaX liveness changed to offline",
                    None,
                ),
            ),
        )

    def test_liveness_is_independent_monotonic_and_preserves_topology(self):
        """Provider health changes retain exact discovery contents."""
        adapter, state_store = publisher()
        with self.assertRaisesRegex(FragmentAdapterReadError, "seeded"):
            adapter.set_liveness(True)
        adapter.ingest_discovery(1, (plant(),), (device(),))
        topology = adapter.read_snapshot().topology_fragment

        self.assertTrue(adapter.set_liveness(True))
        self.assertFalse(adapter.set_liveness(True))
        self.assertTrue(adapter.set_liveness(None))
        self.assertEqual(adapter.read_snapshot().health, ProviderHealth.DEGRADED)
        self.assertEqual(adapter.read_snapshot().topology_fragment, topology)
        self.assertEqual(adapter.generation, 3)
        self.assertEqual(state_store.writes, 3)

    def test_serial_identity_composes_with_ge_cloud_without_materialization(self):
        """Two cloud views join without creating config or control authority."""
        solax, _solax_store = publisher()
        solax.ingest_discovery(
            1,
            (plant(),),
            (device(),),
            health=True,
        )
        ge_cloud = GECloudFragmentPublisher(
            "ge-cloud",
            InMemoryFragmentAdapterStateStore(),
            enabled=True,
        )
        ge_cloud.ingest_discovery(
            1,
            (
                GECloudDeviceSnapshot(
                    serial="H1231231932123",
                    kind="battery-inverter",
                    online=True,
                ),
            ),
            health=True,
        )

        plan = compile_auto_config(
            (
                ge_cloud.read_snapshot(),
                solax.read_snapshot(),
            )
        )

        self.assertEqual(
            tuple(node["id"] for node in plan.topology["nodes"]),
            (
                "identity:serial:H1231231932123",
                "identity:solax-plant-id:1618699116555534337",
            ),
        )
        self.assertEqual(
            plan.provider_generations,
            (("ge-cloud", 1), ("solax-cloud", 1)),
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
        adapter.ingest_discovery(
            5,
            (plant(),),
            (device(),),
            health=True,
        )
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
            adapter.ingest_discovery(
                1,
                (plant(),),
                (device(),),
                health=True,
            )
        self.assertEqual(adapter.read_state(), before)
        self.assertEqual(state_store.writes, 2)
        self.assertEqual(
            invalidations[-1],
            ("solax-cloud", 2, "SolaX integration removed"),
        )

        restarted = SolaxCloudFragmentPublisher(
            "solax-cloud",
            state_store,
            enabled=True,
        )
        self.assertTrue(restarted.read_state().removed)
        with self.assertRaises(FragmentAdapterRemoved):
            restarted.read_snapshot()
        with self.assertRaises(FragmentAdapterRemoved):
            restarted.ingest_discovery(
                99,
                (plant(),),
                (device(),),
            )


if __name__ == "__main__":
    unittest.main()
