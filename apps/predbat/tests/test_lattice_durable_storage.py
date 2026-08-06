"""Focused tests for production durable Lattice state stores."""

# cspell:ignore aiofiles autoconfig fsync

import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_autoconfig import (  # noqa: E402
    AliasRole,
    ProjectionCardinality,
    ProjectionRouting,
    ProjectionValueKind,
    ProviderAlias,
    ProviderConfigProjection,
    ProviderHealth,
    ProviderIdentityAlias,
    ProviderProjectionValue,
    ProviderRoleAssignment,
    ProviderSnapshot,
)
from lattice_compiled_publication import (  # noqa: E402
    CompiledLatticeCompiler,
    InMemoryCompiledLatticeStateStore,
)
from lattice_durable_storage import (  # noqa: E402
    LatticeDurableReadError,
    LatticeDurableWriteError,
    PredBatCompiledLatticeStateStore,
    PredBatFragmentAdapterStateStore,
    decode_compiled_lattice_publication,
    decode_fragment_adapter_state,
    encode_compiled_lattice_publication,
    encode_fragment_adapter_state,
)
from lattice_fragment_adapters import (  # noqa: E402
    FragmentAdapterState,
    _semantic_fingerprint,
)
from tests.test_lattice_autoconfig import snapshot  # noqa: E402

try:  # pragma: no cover - depends on the optional aiofiles test environment
    from storage import StorageLocalFiles  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover
    StorageLocalFiles = None


class MemoryStorage:
    """Thread-safe async storage double with controllable write outcomes."""

    def __init__(self):
        """Create empty non-expiring storage."""
        self.values = {}
        self.lock = threading.RLock()
        self.write_mode = "ok"
        self.saves = 0

    async def load(self, module, filename):
        """Return a detached stored value."""
        with self.lock:
            value = self.values.get((module, filename))
            if value is None:
                return None
            return json.loads(json.dumps(value, allow_nan=False))

    async def age(self, module, filename):
        """Distinguish a present unreadable value from a missing key."""
        with self.lock:
            return 0.0 if (module, filename) in self.values else None

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Save, fail before save, or raise after an ambiguous successful save."""
        del format, expiry
        with self.lock:
            self.saves += 1
            if self.write_mode == "false_before":
                return False
            self.values[(module, filename)] = json.loads(json.dumps(data, allow_nan=False))
            if self.write_mode == "raise_after":
                raise OSError("ambiguous fsync result")
            return True


def rich_fragment_state(provider_id="gateway.local", generation=3, removed=False):
    """Build one fragment exercising every persisted provider contract type."""
    node_id = "INV-1"
    provider_snapshot = ProviderSnapshot(
        provider_id=provider_id,
        generation=generation,
        health=ProviderHealth.DEGRADED,
        topology_fragment={
            "topologyVersion": "0.3.0",
            "scope": "fragment",
            "docVersion": generation,
            "producer": {
                "name": "Gateway",
                "provider": provider_id,
                "authority": 10,
            },
            "nodes": [{"id": node_id, "kind": "inverter"}],
            "relationships": [],
        },
        aliases=(
            ProviderAlias(
                "primary",
                node_id,
                frozenset((AliasRole.REFERENCE, AliasRole.PRIMARY)),
            ),
        ),
        identity_aliases=(ProviderIdentityAlias("serial", "ABC123", node_id),),
        role_assignments=(ProviderRoleAssignment(AliasRole.PRIMARY, "battery", 0, node_id),),
        config_projections=(
            ProviderConfigProjection(
                "battery_power",
                AliasRole.PRIMARY,
                "battery",
                ProjectionRouting.LEAF,
                ProjectionCardinality.PER_INDEX,
                (
                    ProviderProjectionValue(
                        node_id,
                        ProjectionValueKind.ENTITY,
                        "sensor.gateway_battery_power",
                        capability="battery.power",
                        identity_kind="serial",
                        identity_value="ABC123",
                        access_path_id="gateway-mqtt",
                    ),
                ),
                required=True,
                transforms=("watts",),
            ),
        ),
    )
    return FragmentAdapterState(
        provider_id,
        generation,
        _semantic_fingerprint(provider_snapshot, removed),
        provider_snapshot,
        removed,
    )


def compiled_publication():
    """Build one real compiled publication through the production compiler."""
    provider_snapshot = snapshot("gateway", generation=1)
    compiler = CompiledLatticeCompiler(
        {"gateway": lambda: provider_snapshot},
        state_store=InMemoryCompiledLatticeStateStore(),
    )
    assert compiler.invalidate("gateway", 1, "initial durable discovery")
    run = compiler.drain()
    assert run.publication is not None
    return run.publication


class TestLatticeDurableCodecs(unittest.TestCase):
    """Verify strict, lossless codecs for both durable roots."""

    def test_fragment_round_trip_preserves_immutable_contract(self):
        """All provider enums, tuples, sets and mappings survive a round trip."""
        state = rich_fragment_state()
        restored = decode_fragment_adapter_state(copy.deepcopy(encode_fragment_adapter_state(state)))

        self.assertEqual(restored, state)
        self.assertEqual(restored.semantic_fingerprint, state.semantic_fingerprint)
        self.assertIsInstance(restored.snapshot.topology_fragment, type(state.snapshot.topology_fragment))
        self.assertIsInstance(restored.snapshot.aliases[0].roles, frozenset)

    def test_fragment_tombstone_round_trip(self):
        """Removal state remains removed with its removal-bound fingerprint."""
        state = rich_fragment_state(removed=True)
        restored = decode_fragment_adapter_state(encode_fragment_adapter_state(state))

        self.assertEqual(restored, state)
        self.assertTrue(restored.removed)

    def test_compiled_publication_round_trip(self):
        """A complete plan and durable cursor restore exactly."""
        publication = compiled_publication()
        restored = decode_compiled_lattice_publication(copy.deepcopy(encode_compiled_lattice_publication(publication)))

        self.assertEqual(restored, publication)
        self.assertEqual(restored.digest, publication.plan.digest)
        self.assertEqual(restored.provider_fingerprints, publication.provider_fingerprints)

    def test_unknown_root_type_is_rejected(self):
        """A whitelisted but incorrect durable root cannot cross store kinds."""
        encoded = encode_fragment_adapter_state(rich_fragment_state())
        with self.assertRaisesRegex(ValueError, "wrong root type"):
            decode_compiled_lattice_publication(encoded)

    def test_unknown_dataclass_and_non_finite_values_are_rejected(self):
        """The decoder and encoder accept no open-ended object types."""
        encoded = encode_fragment_adapter_state(rich_fragment_state())
        encoded["name"] = "ArbitraryCode"
        with self.assertRaisesRegex(ValueError, "unsupported persisted dataclass"):
            decode_fragment_adapter_state(encoded)

        state = rich_fragment_state()
        object.__setattr__(state.snapshot.config_projections[0].values[0], "value", float("nan"))
        with self.assertRaisesRegex(ValueError, "non-finite"):
            encode_fragment_adapter_state(state)


class TestPredBatFragmentAdapterStateStore(unittest.TestCase):
    """Verify restart durability, CAS and journal recovery for fragments."""

    def test_cas_restart_and_stale_expected(self):
        """Only the exact durable cursor can be replaced across restarts."""
        storage = MemoryStorage()
        store = PredBatFragmentAdapterStateStore(storage, "gateway.local")
        first = rich_fragment_state(generation=1)
        second = rich_fragment_state(generation=2)

        self.assertIsNone(store.load())
        self.assertTrue(store.compare_and_store(None, first))
        self.assertFalse(store.compare_and_store(None, second))
        self.assertTrue(store.compare_and_store(first, second))

        restarted = PredBatFragmentAdapterStateStore(storage, "gateway.local")
        self.assertEqual(restarted.load(), second)

    def test_provider_ids_use_separate_journals(self):
        """Provider names that sanitize alike cannot alias durable state."""
        storage = MemoryStorage()
        dotted = PredBatFragmentAdapterStateStore(storage, "gateway.local")
        slashed = PredBatFragmentAdapterStateStore(storage, "gateway/local")
        dotted_state = rich_fragment_state("gateway.local", generation=1)
        slashed_state = rich_fragment_state("gateway/local", generation=1)

        self.assertTrue(dotted.compare_and_store(None, dotted_state))
        self.assertTrue(slashed.compare_and_store(None, slashed_state))
        self.assertEqual(dotted.load(), dotted_state)
        self.assertEqual(slashed.load(), slashed_state)

    def test_write_failure_retains_previous_slot(self):
        """A failed inactive-slot write never replaces the durable winner."""
        storage = MemoryStorage()
        store = PredBatFragmentAdapterStateStore(storage, "gateway.local")
        first = rich_fragment_state(generation=1)
        second = rich_fragment_state(generation=2)
        self.assertTrue(store.compare_and_store(None, first))

        storage.write_mode = "false_before"
        with self.assertRaises(LatticeDurableWriteError):
            store.compare_and_store(first, second)
        storage.write_mode = "ok"

        self.assertEqual(
            PredBatFragmentAdapterStateStore(storage, "gateway.local").load(),
            first,
        )

    def test_ambiguous_success_is_recovered(self):
        """An exception after the exact slot write is treated as committed."""
        storage = MemoryStorage()
        store = PredBatFragmentAdapterStateStore(storage, "gateway.local")
        state = rich_fragment_state(generation=1)
        storage.write_mode = "raise_after"

        self.assertTrue(store.compare_and_store(None, state))
        storage.write_mode = "ok"
        self.assertEqual(
            PredBatFragmentAdapterStateStore(storage, "gateway.local").load(),
            state,
        )

    def test_one_corrupt_slot_falls_back_but_two_fail_closed(self):
        """The prior slot survives one torn write; no valid journal fails closed."""
        storage = MemoryStorage()
        store = PredBatFragmentAdapterStateStore(storage, "gateway.local")
        first = rich_fragment_state(generation=1)
        second = rich_fragment_state(generation=2)
        self.assertTrue(store.compare_and_store(None, first))
        self.assertTrue(store.compare_and_store(first, second))

        keys = list(storage.values)
        newest = max(keys, key=lambda key: storage.values[key]["sequence"])
        storage.values[newest]["checksum"] = "corrupt"
        self.assertEqual(store.load(), first)

        for key in keys:
            storage.values[key]["checksum"] = "corrupt"
        with self.assertRaises(LatticeDurableReadError):
            store.load()

    def test_duplicate_sequence_with_different_content_fails_closed(self):
        """One storage revision cannot ambiguously name two different values."""
        storage = MemoryStorage()
        store = PredBatFragmentAdapterStateStore(storage, "gateway.local")
        first = rich_fragment_state(generation=1)
        second = rich_fragment_state(generation=2)
        self.assertTrue(store.compare_and_store(None, first))
        self.assertTrue(store.compare_and_store(first, second))

        values = list(storage.values.values())
        older = min(values, key=lambda item: item["sequence"])
        newer = max(values, key=lambda item: item["sequence"])
        older["sequence"] = newer["sequence"]
        content = {key: older[key] for key in ("schema", "kind", "sequence", "payload")}
        older["checksum"] = hashlib.sha256(
            json.dumps(
                content,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

        with self.assertRaisesRegex(LatticeDurableReadError, "reused"):
            store.load()

    def test_same_expected_thread_race_has_one_winner(self):
        """Two publishing threads cannot both commit the same cursor."""
        storage = MemoryStorage()
        store = PredBatFragmentAdapterStateStore(storage, "gateway.local")
        first = rich_fragment_state(generation=1)
        candidates = (
            rich_fragment_state(generation=2),
            rich_fragment_state(generation=3),
        )
        self.assertTrue(store.compare_and_store(None, first))
        barrier = threading.Barrier(3)
        results = []

        def race(candidate):
            """Attempt one CAS after both workers are ready."""
            barrier.wait()
            results.append(store.compare_and_store(first, candidate))

        threads = [threading.Thread(target=race, args=(item,)) for item in candidates]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(results), [False, True])
        self.assertIn(store.load(), candidates)

    def test_two_store_wrappers_share_the_same_cas_lock(self):
        """Separate wrappers over one component still serialize one journal key."""
        storage = MemoryStorage()
        first_store = PredBatFragmentAdapterStateStore(storage, "gateway.local")
        second_store = PredBatFragmentAdapterStateStore(storage, "gateway.local")
        first = rich_fragment_state(generation=1)
        candidates = (
            rich_fragment_state(generation=2),
            rich_fragment_state(generation=3),
        )
        self.assertTrue(first_store.compare_and_store(None, first))
        barrier = threading.Barrier(3)
        results = []

        def race(store, candidate):
            """Attempt one CAS through an independently constructed wrapper."""
            barrier.wait()
            results.append(store.compare_and_store(first, candidate))

        threads = [
            threading.Thread(target=race, args=(store, candidate))
            for store, candidate in zip(
                (first_store, second_store),
                candidates,
            )
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(results), [False, True])
        self.assertIn(first_store.load(), candidates)

    @unittest.skipIf(StorageLocalFiles is None, "aiofiles is not installed")
    def test_real_local_storage_restart_round_trip(self):
        """The journal survives actual JSON serialization and a fresh backend."""
        temp_dir = tempfile.mkdtemp()
        logs = []
        try:
            storage = StorageLocalFiles(temp_dir, logs.append)
            store = PredBatFragmentAdapterStateStore(storage, "gateway.local")
            state = rich_fragment_state(generation=1)
            self.assertTrue(store.compare_and_store(None, state))

            restarted_storage = StorageLocalFiles(temp_dir, logs.append)
            restarted = PredBatFragmentAdapterStateStore(
                restarted_storage,
                "gateway.local",
            )
            self.assertEqual(restarted.load(), state)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @unittest.skipIf(StorageLocalFiles is None, "aiofiles is not installed")
    def test_real_local_storage_corrupt_metadata_fails_closed(self):
        """Malformed sidecar metadata is not mistaken for an empty journal."""
        temp_dir = tempfile.mkdtemp()
        logs = []
        try:
            storage = StorageLocalFiles(temp_dir, logs.append)
            store = PredBatFragmentAdapterStateStore(storage, "gateway.local")
            self.assertTrue(
                store.compare_and_store(
                    None,
                    rich_fragment_state(generation=1),
                )
            )
            provider_key = hashlib.sha256(b"gateway.local").hexdigest()
            meta_path = storage._meta_path(
                "lattice_autoconfig",
                "fragment_{}_1".format(provider_key),
            )
            with open(meta_path, "w", encoding="utf-8") as metadata:
                metadata.write("{broken")

            with self.assertRaisesRegex(LatticeDurableReadError, "unreadable"):
                PredBatFragmentAdapterStateStore(
                    StorageLocalFiles(temp_dir, logs.append),
                    "gateway.local",
                )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_store_rejects_active_event_loop(self):
        """Synchronous persistence cannot silently block an asyncio owner loop."""
        storage = MemoryStorage()

        async def construct():
            return PredBatFragmentAdapterStateStore(storage, "gateway.local")

        with self.assertRaisesRegex(RuntimeError, "outside an active asyncio"):
            asyncio.run(construct())


class TestPredBatCompiledLatticeStateStore(unittest.TestCase):
    """Verify compiled publication version CAS and restart behavior."""

    def test_publication_round_trip_and_version_guard(self):
        """Only version one can replace an empty compiled journal."""
        storage = MemoryStorage()
        store = PredBatCompiledLatticeStateStore(storage)
        publication = compiled_publication()

        self.assertTrue(store.compare_and_publish(0, publication))
        self.assertFalse(store.compare_and_publish(0, publication))
        self.assertEqual(
            PredBatCompiledLatticeStateStore(storage).load(),
            publication,
        )
        with self.assertRaisesRegex(ValueError, "exactly expected_version"):
            store.compare_and_publish(1, publication)


if __name__ == "__main__":
    unittest.main()
