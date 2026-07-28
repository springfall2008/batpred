"""Tests for the pure Solis Lattice fragment publisher."""

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
from lattice_solax_fragment import (  # noqa: E402
    SolaxCloudFragmentPublisher,
    SolaxDeviceSnapshot,
    SolaxPlantSnapshot,
)
from lattice_solis_fragment import (  # noqa: E402
    SolisAccessPathSnapshot,
    SolisDeviceSnapshot,
    SolisFragmentPublisher,
)


def access_path(
    path_id="solis-cloud-oauth",
    channel="cloud",
    protocol_version="v1",
    cid_profile="tou-v2",
    auth_method="oauth",
    endpoint_id="solis-oauth-api",
):
    """Build one explicit Solis access path."""
    return SolisAccessPathSnapshot(
        path_id=path_id,
        channel=channel,
        protocol_version=protocol_version,
        cid_profile=cid_profile,
        auth_method=auth_method,
        endpoint_id=endpoint_id,
    )


def device(
    device_id="cloud-record-1",
    access_paths=None,
    serial="1910A12345",
    serial_verified=True,
    online=1,
    model="S6-EH1P",
    has_battery=True,
    station_id="station-7",
):
    """Build one explicitly grouped Solis device snapshot."""
    if access_paths is None:
        access_paths = (access_path(),)
    return SolisDeviceSnapshot(
        device_id=device_id,
        access_paths=access_paths,
        serial=serial,
        serial_verified=serial_verified,
        online=online,
        model=model,
        has_battery=has_battery,
        station_id=station_id,
    )


def publisher(enabled=True, state_store=None, provider_id="solis"):
    """Build one reference publisher and its in-memory durable store."""
    if state_store is None:
        state_store = InMemoryFragmentAdapterStateStore()
    return (
        SolisFragmentPublisher(
            provider_id,
            state_store,
            enabled=enabled,
        ),
        state_store,
    )


class TestSolisFragmentPublisher(unittest.TestCase):
    """Solis inputs publish immutable, REFERENCE-only provider snapshots."""

    def test_default_off_has_no_validation_or_durable_side_effect(self):
        """Disabled construction never validates, publishes, or registers."""
        adapter, state_store = publisher(enabled=False)

        self.assertIsNone(adapter.lattice_fragment_adapter())
        self.assertFalse(
            adapter.ingest_snapshot(-1, -1, (object(),)),
        )
        self.assertFalse(adapter.set_liveness(True))
        self.assertFalse(adapter.remove())
        self.assertEqual(state_store.writes, 0)
        with self.assertRaises(FragmentAdapterReadError):
            adapter.read_state()

    def test_snapshot_is_immutable_reference_only_and_keeps_metadata_local(self):
        """Structural state carries no configuration or control authority."""
        adapter, state_store = publisher()
        paths = [
            access_path(),
            access_path(
                path_id="solis-local-modbus",
                channel="local",
                protocol_version="modbus-v1",
                cid_profile="GS_fb00",
                auth_method=None,
                endpoint_id="192.0.2.4",
            ),
        ]
        devices = [device(access_paths=paths)]

        self.assertTrue(
            adapter.ingest_snapshot(
                4,
                9,
                devices,
                health=True,
            )
        )
        paths.append(access_path(path_id="mutated"))
        devices.append(device(device_id="mutated", serial="MUTATED"))
        snapshot = adapter.read_snapshot()
        document = snapshot.topology_fragment
        node = document["nodes"][0]

        self.assertIs(adapter.lattice_fragment_adapter(), adapter)
        self.assertEqual(state_store.writes, 1)
        self.assertEqual(adapter.generation, 1)
        self.assertEqual(adapter.input_versions, (4, 9))
        self.assertEqual(len(adapter.semantic_fingerprint), 64)
        self.assertEqual(snapshot.health, ProviderHealth.HEALTHY)
        self.assertEqual(document["docVersion"], 1)
        self.assertEqual(
            document["producer"]["inputVersions"],
            {"config": 9, "discovery": 4},
        )
        self.assertEqual(node["id"], "solis:device:cloud-record-1")
        self.assertEqual(node["kind"], "inverter")
        self.assertEqual(node["capabilities"], ())
        self.assertEqual(
            tuple(path["id"] for path in node["accessPaths"]),
            ("solis-cloud-oauth", "solis-local-modbus"),
        )
        self.assertEqual(snapshot.role_assignments, ())
        self.assertEqual(snapshot.config_projections, ())
        self.assertTrue(all(alias.roles == frozenset((AliasRole.REFERENCE,)) for alias in snapshot.aliases))
        self.assertEqual(
            tuple((alias.kind, alias.value, alias.node_id) for alias in snapshot.identity_aliases),
            (
                (
                    "serial",
                    "1910A12345",
                    "solis:device:cloud-record-1",
                ),
                (
                    "solis-provider-device-id",
                    "solis:cloud-record-1",
                    "solis:device:cloud-record-1",
                ),
            ),
        )
        identity_text = repr(snapshot.identity_aliases)
        self.assertNotIn("station-7", identity_text)
        self.assertNotIn("S6-EH1P", identity_text)
        self.assertNotIn("tou-v2", identity_text)
        self.assertNotIn("GS_fb00", identity_text)
        self.assertNotIn("192.0.2.4", identity_text)

    def test_local_cloud_and_cid_ambiguity_fail_closed(self):
        """Access metadata cannot silently correlate separate devices."""
        with self.assertRaisesRegex(ValueError, "requires hmac or oauth"):
            access_path(auth_method=None)
        with self.assertRaisesRegex(ValueError, "must not carry"):
            access_path(
                channel="local",
                auth_method="oauth",
            )
        with self.assertRaisesRegex(
            FragmentAdapterConflict,
            "duplicate access path",
        ):
            device(access_paths=(access_path(), access_path()))

        adapter, _state_store = publisher()
        shared_cid = "tou-v2"
        adapter.ingest_snapshot(
            1,
            1,
            (
                device(
                    device_id="cloud-a",
                    serial=None,
                    serial_verified=False,
                    access_paths=(
                        access_path(
                            path_id="cloud-a",
                            cid_profile=shared_cid,
                        ),
                    ),
                ),
                device(
                    device_id="cloud-b",
                    serial=None,
                    serial_verified=False,
                    access_paths=(
                        access_path(
                            path_id="local-b",
                            channel="local",
                            protocol_version="modbus-v1",
                            cid_profile=shared_cid,
                            auth_method=None,
                        ),
                    ),
                ),
            ),
        )
        plan = compile_auto_config((adapter.read_snapshot(),))

        self.assertEqual(len(plan.topology["nodes"]), 2)
        self.assertFalse(any(alias.kind in ("cid", "cid-profile", "protocol-version") for alias in adapter.read_snapshot().identity_aliases))

    def test_unverified_configured_serial_never_becomes_strong_identity(self):
        """A configured value is only a reference until discovery proves it."""
        adapter, _state_store = publisher()
        adapter.ingest_snapshot(
            1,
            1,
            (
                device(
                    serial="configured-only",
                    serial_verified=False,
                ),
            ),
        )
        snapshot = adapter.read_snapshot()

        self.assertEqual(
            tuple((alias.kind, alias.value) for alias in snapshot.identity_aliases),
            (
                (
                    "solis-provider-device-id",
                    "solis:cloud-record-1",
                ),
            ),
        )
        self.assertEqual(
            tuple(alias.name for alias in snapshot.aliases),
            (
                "configured-serial:CONFIGURED-ONLY",
                "device:cloud-record-1",
            ),
        )

    def test_verified_serial_and_provider_ids_must_not_collide(self):
        """One provider cannot assert the same durable identity twice."""
        adapter, state_store = publisher()
        with self.assertRaisesRegex(
            FragmentAdapterConflict,
            "duplicate device",
        ):
            adapter.ingest_snapshot(
                1,
                1,
                (
                    device(),
                    device(serial="OTHER"),
                ),
            )
        with self.assertRaisesRegex(
            FragmentAdapterConflict,
            "verified Solis serial",
        ):
            adapter.ingest_snapshot(
                1,
                1,
                (
                    device(),
                    device(
                        device_id="cloud-record-2",
                        serial="1910a12345",
                    ),
                ),
            )
        self.assertEqual(state_store.writes, 0)

    def test_discovery_and_config_versions_advance_durable_generation(self):
        """Either provider input can invalidate a fresh compiler read."""
        adapter, state_store = publisher()
        self.assertTrue(adapter.ingest_snapshot(1, 1, (device(),)))
        first = adapter.read_state()
        self.assertTrue(adapter.ingest_snapshot(2, 1, (device(),)))
        self.assertTrue(adapter.ingest_snapshot(2, 2, (device(),)))
        self.assertFalse(adapter.ingest_snapshot(2, 2, (device(),)))

        self.assertEqual(adapter.input_versions, (2, 2))
        self.assertEqual(adapter.generation, 3)
        self.assertEqual(state_store.writes, 3)
        self.assertNotEqual(
            first.semantic_fingerprint,
            adapter.semantic_fingerprint,
        )
        self.assertEqual(
            adapter.read_snapshot().topology_fragment["docVersion"],
            3,
        )

    def test_stale_mixed_and_same_version_reuse_are_fail_closed(self):
        """Input cursors cannot regress or be reused for changed contents."""
        adapter, state_store = publisher()
        adapter.ingest_snapshot(5, 8, (device(),), health=True)
        before = adapter.read_state()

        self.assertFalse(
            adapter.ingest_snapshot(
                4,
                7,
                (device(model="stale"),),
                health=False,
            )
        )
        with self.assertRaisesRegex(
            FragmentAdapterConflict,
            "mixed Solis input regression",
        ):
            adapter.ingest_snapshot(6, 7, (device(),))
        with self.assertRaisesRegex(
            FragmentAdapterConflict,
            "reused Solis discovery/config versions 5/8",
        ):
            adapter.ingest_snapshot(
                5,
                8,
                (device(model="changed"),),
            )

        self.assertEqual(adapter.read_state(), before)
        self.assertEqual(state_store.writes, 1)

    def test_all_changes_invalidate_common_compiler_with_fresh_reads(self):
        """Discovery, config, liveness, and removal all notify composition."""
        adapter, _state_store = publisher()
        adapter.ingest_snapshot(1, 1, (device(),), health=True)
        invalidations = []
        adapter.subscribe_invalidation(
            lambda provider_id, generation, reason, feedback_token: (
                invalidations.append(
                    (
                        provider_id,
                        generation,
                        reason,
                        feedback_token,
                    )
                )
            )
        )
        registry = FragmentAdapterRegistry(enabled=True)
        self.assertEqual(registry.discover((adapter,)), ("solis",))
        compiler = registry.create_compiler(
            InMemoryCompiledLatticeStateStore(),
        )

        first = compiler.drain()
        self.assertEqual(first.status, CompileStatus.FRESH)
        self.assertTrue(adapter.ingest_snapshot(2, 1, (device(),)))
        discovery = compiler.drain()
        self.assertEqual(discovery.status, CompileStatus.FRESH)
        self.assertTrue(adapter.ingest_snapshot(2, 2, (device(),)))
        config = compiler.drain()
        self.assertEqual(config.status, CompileStatus.FRESH)
        self.assertTrue(adapter.set_liveness(False))
        offline = compiler.drain()
        self.assertEqual(offline.status, CompileStatus.STALE)
        self.assertTrue(offline.pending)
        self.assertTrue(adapter.remove())
        removed = compiler.drain()
        self.assertEqual(removed.status, CompileStatus.STALE)

        self.assertEqual(
            tuple(item[:3] for item in invalidations),
            (
                (
                    "solis",
                    2,
                    "Solis inputs changed to discovery/config 2/1",
                ),
                (
                    "solis",
                    3,
                    "Solis inputs changed to discovery/config 2/2",
                ),
                (
                    "solis",
                    4,
                    "Solis liveness changed to offline",
                ),
                (
                    "solis",
                    5,
                    "Solis integration removed",
                ),
            ),
        )

    def test_restart_preserves_cursor_and_removal_is_irreversible(self):
        """Durable versions survive restart and a tombstone cannot be replayed."""
        adapter, state_store = publisher()
        adapter.ingest_snapshot(3, 4, (device(),), health=True)
        restarted = SolisFragmentPublisher(
            "solis",
            state_store,
            enabled=True,
        )
        self.assertEqual(restarted.input_versions, (3, 4))
        self.assertFalse(restarted.ingest_snapshot(3, 4, (device(),)))
        self.assertTrue(restarted.remove())

        after_removal = SolisFragmentPublisher(
            "solis",
            state_store,
            enabled=True,
        )
        self.assertTrue(after_removal.read_state().removed)
        with self.assertRaises(FragmentAdapterRemoved):
            after_removal.read_snapshot()
        with self.assertRaisesRegex(
            FragmentAdapterRemoved,
            "cannot re-enrol",
        ):
            after_removal.ingest_snapshot(99, 99, (device(),))

    def test_verified_serial_composes_gateway_ge_and_solax_only_explicitly(self):
        """Four proven views join while unverified Solis state stays separate."""
        serial = "1910A12345"
        solis, _solis_store = publisher()
        solis.ingest_snapshot(
            1,
            1,
            (
                device(serial=serial),
                device(
                    device_id="configured-shadow",
                    serial=serial,
                    serial_verified=False,
                ),
            ),
            health=True,
        )

        gateway = GatewayRetainedTopologyFragmentPublisher(
            "predbat-gateway",
            InMemoryFragmentAdapterStateStore(),
            enabled=True,
        )
        gateway.ingest_retained_topology(
            {
                "topologyVersion": "0.3.0",
                "scope": "fragment",
                "docVersion": 1,
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
                        "accessPaths": [],
                        "capabilities": [],
                    }
                ],
            },
            online=True,
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
                    serial=serial,
                    kind="battery-inverter",
                    online=True,
                ),
            ),
            health=True,
        )

        solax = SolaxCloudFragmentPublisher(
            "solax-cloud",
            InMemoryFragmentAdapterStateStore(),
            enabled=True,
        )
        solax.ingest_discovery(
            1,
            (SolaxPlantSnapshot(plant_id="plant-1"),),
            (
                SolaxDeviceSnapshot(
                    serial=serial,
                    plant_id="plant-1",
                    kind="inverter",
                    online=True,
                ),
            ),
            health=True,
        )

        plan = compile_auto_config(
            (
                gateway.read_snapshot(),
                ge_cloud.read_snapshot(),
                solax.read_snapshot(),
                solis.read_snapshot(),
            )
        )

        self.assertEqual(
            tuple(node["id"] for node in plan.topology["nodes"]),
            (
                "identity:serial:1910A12345",
                "identity:solax-plant-id:plant-1",
                ("identity:solis-provider-device-id:" "solis:configured-shadow"),
            ),
        )
        self.assertEqual(
            plan.provider_generations,
            (
                ("ge-cloud", 1),
                ("predbat-gateway", 1),
                ("solax-cloud", 1),
                ("solis", 1),
            ),
        )
        self.assertEqual(plan.role_assignments, ())
        self.assertEqual(plan.config_arguments, ())
        self.assertEqual(dict(plan.projected_config), {})
        self.assertFalse(plan.materialization_readiness.ready)


if __name__ == "__main__":
    unittest.main()
