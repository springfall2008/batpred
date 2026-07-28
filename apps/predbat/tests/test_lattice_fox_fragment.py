"""Tests for the pure Fox Cloud Lattice fragment publisher."""

# cspell:ignore autoconfig invalidatable

import os
import subprocess
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
from lattice_fox_fragment import (  # noqa: E402
    FoxCloudFragmentPublisher,
    FoxDeviceSnapshot,
    FoxStationSnapshot,
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


def station(station_id="SITE-1", name="Home"):
    """Build one explicit Fox station snapshot."""
    return FoxStationSnapshot(station_id=station_id, name=name)


def device(
    serial="60KE8020479C034",
    station_id="SITE-1",
    device_type="H3",
    online=True,
    has_battery=True,
    has_pv=True,
    third_party_generation=False,
    scheduler_supported=True,
):
    """Build one explicit Fox hardware snapshot."""
    return FoxDeviceSnapshot(
        serial=serial,
        station_id=station_id,
        device_type=device_type,
        online=online,
        has_battery=has_battery,
        has_pv=has_pv,
        third_party_generation=third_party_generation,
        scheduler_supported=scheduler_supported,
    )


def publisher(enabled=True, state_store=None, provider_id="fox-cloud"):
    """Build one publisher and its in-memory durable store."""
    if state_store is None:
        state_store = InMemoryFragmentAdapterStateStore()
    return (
        FoxCloudFragmentPublisher(
            provider_id,
            state_store,
            enabled=enabled,
        ),
        state_store,
    )


class TestFoxCloudFragmentPublisher(unittest.TestCase):
    """Fox discovery remains pure, reference-only, and invalidatable."""

    def test_default_off_has_no_validation_or_durable_side_effect(self):
        """Disabled construction never imports live Fox or writes state."""
        adapter, state_store = publisher(enabled=False)

        module_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), ".."),
        )
        isolated = subprocess.run(
            (
                sys.executable,
                "-c",
                "import sys; import lattice_fox_fragment; " "assert 'fox' not in sys.modules",
            ),
            cwd=module_dir,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            isolated.returncode,
            0,
            isolated.stderr,
        )
        self.assertIsNone(adapter.lattice_fragment_adapter())
        self.assertFalse(adapter.ingest_discovery(-1, (object(),), (object(),)))
        self.assertFalse(adapter.set_liveness(True))
        self.assertFalse(adapter.remove())
        self.assertEqual(state_store.writes, 0)
        with self.assertRaises(FragmentAdapterReadError):
            adapter.read_state()

    def test_discovery_is_immutable_reference_only_and_station_grouped(self):
        """Snapshot topology carries observations but no authority."""
        adapter, state_store = publisher()
        stations = [station()]
        devices = [
            device(serial="60ke8020479c034"),
            device(
                serial="PV-ONLY",
                has_battery=False,
                scheduler_supported=False,
            ),
        ]

        self.assertTrue(
            adapter.ingest_discovery(
                7,
                stations,
                devices,
                health=ProviderHealth.HEALTHY,
            )
        )
        stations.append(station("MUTATED"))
        devices.append(device(serial="MUTATED"))
        snapshot = adapter.read_snapshot()
        topology = snapshot.topology_fragment

        self.assertIs(adapter.lattice_fragment_adapter(), adapter)
        self.assertEqual(state_store.writes, 1)
        self.assertEqual(adapter.generation, 1)
        self.assertEqual(adapter.discovery_version, 7)
        self.assertEqual(len(adapter.semantic_fingerprint), 64)
        self.assertEqual(snapshot.health, ProviderHealth.HEALTHY)
        self.assertEqual(
            tuple(node["id"] for node in topology["nodes"]),
            (
                "fox:station:SITE-1",
                "fox:device:60KE8020479C034",
                "fox:device:PV-ONLY",
            ),
        )
        self.assertEqual(
            tuple((edge["from"], edge["to"], edge["type"]) for edge in topology["relationships"]),
            (
                (
                    "fox:station:SITE-1",
                    "fox:device:60KE8020479C034",
                    "contains",
                ),
                (
                    "fox:station:SITE-1",
                    "fox:device:PV-ONLY",
                    "contains",
                ),
            ),
        )
        self.assertEqual(snapshot.role_assignments, ())
        self.assertEqual(snapshot.config_projections, ())
        self.assertTrue(all(alias.roles == frozenset((AliasRole.REFERENCE,)) for alias in snapshot.aliases))
        self.assertTrue(all(node["capabilities"] == () for node in topology["nodes"]))
        self.assertEqual(
            tuple((alias.kind, alias.value) for alias in snapshot.identity_aliases),
            (
                ("fox-station-id", "SITE-1"),
                ("serial", "60KE8020479C034"),
                ("serial", "PV-ONLY"),
            ),
        )

    def test_order_case_and_explicit_features_are_replay_stable(self):
        """Equivalent provider observations do not invent generations."""
        adapter, state_store = publisher()
        first = (
            device(serial="INV-2"),
            device(serial="inv-1"),
        )
        replay = (
            device(serial="INV-1"),
            device(serial="INV-2"),
        )

        self.assertTrue(adapter.ingest_discovery(3, (station(),), first))
        self.assertFalse(adapter.ingest_discovery(3, (station(),), replay))
        self.assertEqual(adapter.generation, 1)
        self.assertEqual(state_store.writes, 1)
        with self.assertRaisesRegex(ValueError, "has_pv"):
            device(has_pv=1)

    def test_versions_are_monotonic_and_reuse_collision_fails_closed(self):
        """Stale replay is ignored and same-version mutation is rejected."""
        adapter, state_store = publisher()
        adapter.ingest_discovery(5, (station(),), (device(),), health=True)
        before = adapter.read_state()

        self.assertFalse(
            adapter.ingest_discovery(
                4,
                (station(name="Stale"),),
                (device(online=False),),
                health=False,
            )
        )
        with self.assertRaisesRegex(
            FragmentAdapterConflict,
            "reused Fox discovery version 5",
        ):
            adapter.ingest_discovery(
                5,
                (station(),),
                (device(device_type="Different"),),
            )

        self.assertEqual(adapter.read_state(), before)
        self.assertEqual(state_store.writes, 1)

    def test_station_serial_and_membership_collisions_fail_before_write(self):
        """Ambiguous provider identity never enters durable state."""
        adapter, state_store = publisher()

        with self.assertRaisesRegex(ValueError, "at least one"):
            adapter.ingest_discovery(1, (), (device(),))
        with self.assertRaisesRegex(
            FragmentAdapterConflict,
            "duplicate station SITE-1",
        ):
            adapter.ingest_discovery(
                1,
                (station(), station()),
                (device(),),
            )
        with self.assertRaisesRegex(ValueError, "unknown station OTHER"):
            adapter.ingest_discovery(
                1,
                (station(),),
                (device(station_id="OTHER"),),
            )
        with self.assertRaisesRegex(
            FragmentAdapterConflict,
            "belongs to both stations SITE-1 and SITE-2",
        ):
            adapter.ingest_discovery(
                1,
                (station(), station("SITE-2")),
                (
                    device(),
                    device(
                        serial="60ke8020479c034",
                        station_id="SITE-2",
                    ),
                ),
            )

        self.assertEqual(state_store.writes, 0)
        with self.assertRaises(FragmentAdapterReadError):
            adapter.read_state()

    def test_accepted_changes_trigger_common_fresh_read_compilation(self):
        """Discovery and liveness both invalidate the shared compiler."""
        adapter, _state_store = publisher()
        adapter.ingest_discovery(1, (station(),), (device(),), health=True)
        reasons = []
        adapter.subscribe_invalidation(
            lambda provider_id, generation, reason, feedback_token: reasons.append(
                (provider_id, generation, reason, feedback_token),
            )
        )
        registry = FragmentAdapterRegistry(enabled=True)
        self.assertEqual(registry.discover((adapter,)), ("fox-cloud",))
        compiler = registry.create_compiler(
            InMemoryCompiledLatticeStateStore(),
        )

        first = compiler.drain()
        self.assertEqual(first.status, CompileStatus.FRESH)
        self.assertEqual(
            first.publication.provider_generations,
            (("fox-cloud", 1),),
        )

        self.assertTrue(
            adapter.ingest_discovery(
                2,
                (station(),),
                (device(), device(serial="INV-2")),
            )
        )
        changed = compiler.drain()
        self.assertEqual(changed.status, CompileStatus.FRESH)
        self.assertEqual(
            changed.publication.provider_generations,
            (("fox-cloud", 2),),
        )
        self.assertTrue(adapter.set_liveness(False))
        offline = compiler.drain()
        self.assertEqual(offline.status, CompileStatus.STALE)
        self.assertTrue(offline.pending)
        self.assertEqual(
            tuple(reasons),
            (
                (
                    "fox-cloud",
                    2,
                    "Fox discovery changed to version 2",
                    None,
                ),
                (
                    "fox-cloud",
                    3,
                    "Fox liveness changed to offline",
                    None,
                ),
            ),
        )

    def test_liveness_is_monotonic_and_preserves_exact_topology(self):
        """Health changes advance generation without rediscovery."""
        adapter, state_store = publisher()
        with self.assertRaisesRegex(FragmentAdapterReadError, "seeded"):
            adapter.set_liveness(True)
        adapter.ingest_discovery(1, (station(),), (device(),))
        topology = adapter.read_snapshot().topology_fragment

        self.assertTrue(adapter.set_liveness(True))
        self.assertFalse(adapter.set_liveness(True))
        self.assertTrue(adapter.set_liveness(None))
        self.assertEqual(
            adapter.read_snapshot().health,
            ProviderHealth.DEGRADED,
        )
        self.assertEqual(adapter.read_snapshot().topology_fragment, topology)
        self.assertEqual(adapter.generation, 3)
        self.assertEqual(state_store.writes, 3)

    def test_serial_composes_with_ge_without_materialization(self):
        """Explicit strong serial joins cloud views but grants no role."""
        fox, _fox_store = publisher()
        fox.ingest_discovery(1, (station(),), (device(),), health=True)
        ge = GECloudFragmentPublisher(
            "ge-cloud",
            InMemoryFragmentAdapterStateStore(),
            enabled=True,
        )
        ge.ingest_discovery(
            1,
            (
                GECloudDeviceSnapshot(
                    serial="60KE8020479C034",
                    kind="battery-inverter",
                    online=True,
                ),
            ),
            health=True,
        )

        plan = compile_auto_config((fox.read_snapshot(), ge.read_snapshot()))

        device_nodes = tuple(node for node in plan.topology["nodes"] if node["kind"] == "inverter")
        self.assertEqual(len(device_nodes), 1)
        self.assertEqual(
            device_nodes[0]["id"],
            "identity:serial:60KE8020479C034",
        )
        self.assertEqual(plan.role_assignments, ())
        self.assertEqual(plan.config_arguments, ())
        self.assertFalse(plan.materialization_readiness.ready)

    def test_removal_is_durable_invalidating_and_irreversible(self):
        """Ambient discovery replay cannot resurrect a removed provider."""
        adapter, state_store = publisher()
        adapter.ingest_discovery(5, (station(),), (device(),), health=True)
        invalidations = []
        adapter.subscribe_invalidation(
            lambda provider_id, generation, reason, feedback_token: (
                invalidations.append(
                    (provider_id, generation, reason),
                )
            )
        )

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
                99,
                (station(),),
                (device(),),
                health=True,
            )
        self.assertEqual(adapter.read_state(), before)
        self.assertEqual(state_store.writes, 2)
        self.assertEqual(
            invalidations[-1],
            ("fox-cloud", 2, "Fox integration removed"),
        )

        restarted = FoxCloudFragmentPublisher(
            "fox-cloud",
            state_store,
            enabled=True,
        )
        self.assertTrue(restarted.read_state().removed)
        with self.assertRaises(FragmentAdapterRemoved):
            restarted.read_snapshot()
        with self.assertRaises(FragmentAdapterRemoved):
            restarted.ingest_discovery(
                100,
                (station(),),
                (device(),),
            )


if __name__ == "__main__":
    unittest.main()
