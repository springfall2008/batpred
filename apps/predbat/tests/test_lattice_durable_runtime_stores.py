"""Tests for default-off durable Lattice runtime filesystem primitives."""

# cspell:ignore autoconfig CREAT fstat fsync hardlink mkfifo RDWR readback

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_atomic_materializer import (  # noqa: E402
    AtomicMaterializationState,
    TargetOperationKey,
    _canonical_config,
    _plan_semantic_digest,
    _validate_envelope,
)
from lattice_autoconfig import ProjectionValueKind  # noqa: E402
from lattice_compiled_publication import (  # noqa: E402
    CompilationInvalidation,
    CompiledLatticeCompiler,
)
from lattice_durable_runtime_stores import (  # noqa: E402
    MAX_OPAQUE_PAYLOAD_BYTES,
    MAX_RECORD_BYTES,
    DurableStateCorrupt,
    DurableStateError,
    FileAtomicMaterializationStateStore,
    FileCompiledLatticeStateStore,
    FileFragmentAdapterStateStore,
    FileOpaqueTargetJournal,
    OpaqueTargetJournalEntry,
    _encode_document,
    _FileRecord,
)
from lattice_fragment_adapters import (  # noqa: E402
    FragmentAdapterState,
    _semantic_fingerprint,
)
from tests.test_lattice_atomic_materializer import envelope  # noqa: E402
from tests.test_lattice_autoconfig import (  # noqa: E402
    MutableReader,
    config_projection,
    indexed_roles,
    projection_snapshot,
    projection_value,
    snapshot,
)
from tests.test_lattice_compiled_publication import (  # noqa: E402
    override_projection_snapshot,
)


class TestDurableRuntimeStores(unittest.TestCase):
    """Exercise exact restart, corruption, bounds, privacy, and CAS rules."""

    def test_fragment_restart_cas_corruption_and_permissions(self):
        """Fragment CAS survives restart and rejects unsafe persisted bytes."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "fragment.state")
            first_snapshot = snapshot("gateway", generation=1)
            first = FragmentAdapterState(
                "gateway",
                1,
                _semantic_fingerprint(first_snapshot),
                first_snapshot,
            )
            second_snapshot = snapshot("gateway", generation=2)
            second = FragmentAdapterState(
                "gateway",
                2,
                _semantic_fingerprint(second_snapshot),
                second_snapshot,
            )
            store = FileFragmentAdapterStateStore(path)

            self.assertTrue(store.compare_and_store(None, first))
            self.assertEqual(
                stat.S_IMODE(os.stat(path).st_mode),
                0o600,
            )
            self.assertEqual(
                stat.S_IMODE(os.stat(path + ".lock").st_mode),
                0o600,
            )
            restarted = FileFragmentAdapterStateStore(path)
            self.assertEqual(restarted.load(), first)
            self.assertFalse(
                restarted.compare_and_store(None, second),
            )
            self.assertTrue(
                restarted.compare_and_store(first, second),
            )
            self.assertEqual(restarted.load(), second)
            with mock.patch(
                "lattice_durable_runtime_stores._canonical_bytes",
                side_effect=RecursionError("hostile depth"),
            ):
                with self.assertRaises(DurableStateCorrupt):
                    restarted.load()

            hardlink = os.path.join(directory, "fragment.hardlink")
            os.link(path, hardlink)
            with self.assertRaises(DurableStateCorrupt):
                restarted.load()
            os.unlink(hardlink)

            symlink = os.path.join(directory, "fragment.symlink")
            os.symlink(path, symlink)
            with self.assertRaises(DurableStateError):
                FileFragmentAdapterStateStore(symlink).load()

            os.chmod(path, 0o644)
            with self.assertRaises(DurableStateCorrupt):
                restarted.load()
            os.chmod(path, 0o600)

            with open(path, "r+b") as record:
                encoded = record.read()
                record.seek(0)
                record.write(encoded[:-1])
                record.truncate()
            with self.assertRaises(DurableStateCorrupt):
                restarted.load()

            mismatch_path = os.path.join(directory, "mismatch.state")
            mismatch_store = FileFragmentAdapterStateStore(mismatch_path)
            with mock.patch(
                "lattice_durable_runtime_stores._FileRecord._read_bytes",
                side_effect=[None, b"mismatched-readback"],
            ):
                with self.assertRaises(DurableStateError):
                    mismatch_store.compare_and_store(None, first)
            self.assertEqual(
                FileFragmentAdapterStateStore(mismatch_path).load(),
                first,
            )

            error_path = os.path.join(directory, "error.state")
            error_store = FileFragmentAdapterStateStore(error_path)
            with mock.patch(
                "lattice_durable_runtime_stores._FileRecord._read_bytes",
                side_effect=[
                    None,
                    DurableStateError("readback unavailable"),
                ],
            ):
                with self.assertRaises(DurableStateError):
                    error_store.compare_and_store(None, first)
            self.assertEqual(
                FileFragmentAdapterStateStore(error_path).load(),
                first,
            )

            fsync_path = os.path.join(directory, "fsync.state")
            fsync_store = FileFragmentAdapterStateStore(fsync_path)
            with mock.patch(
                "lattice_durable_runtime_stores._sync_directory",
                side_effect=OSError("directory fsync failed"),
            ):
                with self.assertRaises(OSError):
                    fsync_store.compare_and_store(None, first)
            self.assertEqual(
                FileFragmentAdapterStateStore(fsync_path).load(),
                first,
            )

            zero_write_path = os.path.join(directory, "zero-write.state")
            zero_write_store = FileFragmentAdapterStateStore(
                zero_write_path,
            )
            with mock.patch(
                "lattice_durable_runtime_stores.os.write",
                return_value=0,
            ):
                with self.assertRaises(DurableStateError):
                    zero_write_store.compare_and_store(None, first)
            self.assertFalse(os.path.exists(zero_write_path))

            padded_path = os.path.join(directory, "padded.state")
            padded_store = FileFragmentAdapterStateStore(padded_path)
            self.assertTrue(
                padded_store.compare_and_store(None, first),
            )
            with open(padded_path, "rb") as record:
                document = json.loads(record.read().decode("ascii"))
            stack = [document["payload"]]
            padded = False
            while stack:
                node = stack.pop()
                if type(node) is not list:
                    continue
                if len(node) == 3 and node[0] == "dataclass" and node[1] == "FragmentAdapterState":
                    for field in node[2]:
                        if field[0] == "provider_id":
                            field[1] = ["str", " gateway "]
                            padded = True
                            break
                stack.extend(item for item in node if type(item) is list)
            self.assertTrue(padded)
            signed = {
                key: document[key]
                for key in (
                    "format",
                    "version",
                    "kind",
                    "payload",
                )
            }
            document["sha256"] = hashlib.sha256(
                json.dumps(
                    signed,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("ascii")
            ).hexdigest()
            with open(padded_path, "wb") as record:
                record.write(
                    json.dumps(
                        document,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                        allow_nan=False,
                    ).encode("ascii")
                )
            os.chmod(padded_path, 0o600)
            with self.assertRaises(DurableStateCorrupt):
                padded_store.load()

            anchor_directory = os.path.join(directory, "anchor")
            moved_directory = os.path.join(directory, "anchor-moved")
            anchor_path = os.path.join(
                anchor_directory,
                "state",
            )
            anchor_store = FileFragmentAdapterStateStore(
                anchor_path,
            )
            original_load = _FileRecord._load_unlocked
            swapped = [False]

            def swap_path(record, directory_fd):
                """Swap the pathname after the anchored read."""
                value = original_load(record, directory_fd)
                if record is anchor_store._record and not swapped[0]:
                    os.rename(
                        anchor_directory,
                        moved_directory,
                    )
                    os.mkdir(anchor_directory, 0o700)
                    swapped[0] = True
                return value

            with mock.patch.object(
                _FileRecord,
                "_load_unlocked",
                new=swap_path,
            ):
                self.assertTrue(
                    anchor_store.compare_and_store(None, first),
                )
            self.assertTrue(swapped[0])
            self.assertTrue(
                os.path.exists(
                    os.path.join(moved_directory, "state"),
                )
            )
            self.assertFalse(os.path.exists(anchor_path))

    def test_projection_declared_order_round_trips(self):
        """Durable validation preserves compiler-declared value order."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(
                directory,
                "compiled-reversed-slots.state",
            )
            provider_snapshot = projection_snapshot(
                "gateway",
                (
                    "A",
                    "B",
                ),
                indexed_roles(
                    (
                        "A",
                        "B",
                    )
                ),
                (
                    config_projection(
                        "ordered_values",
                        (
                            projection_value(
                                "B",
                                ProjectionValueKind.CONSTANT,
                                2,
                            ),
                            projection_value(
                                "A",
                                ProjectionValueKind.CONSTANT,
                                1,
                            ),
                        ),
                    ),
                ),
            )
            store = FileCompiledLatticeStateStore(path)
            compiler = CompiledLatticeCompiler(
                {
                    "gateway": MutableReader(
                        provider_snapshot,
                    )
                },
                state_store=store,
            )

            publication = compiler.drain().publication

            self.assertIsNotNone(publication)
            self.assertEqual(
                publication.plan.config_arguments[0].candidates[0].slot_indexes,
                (
                    1,
                    0,
                ),
            )
            self.assertEqual(
                publication.plan.config_arguments[0].value,
                (
                    1,
                    2,
                ),
            )
            self.assertEqual(
                FileCompiledLatticeStateStore(path).load(),
                publication,
            )

    def test_compiled_publication_restart_and_exact_version_cas(self):
        """Compiled publication round-trips with its full immutable plan."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "compiled.state")
            store = FileCompiledLatticeStateStore(path)
            compiler = CompiledLatticeCompiler(
                {
                    "gateway": MutableReader(
                        snapshot("gateway", generation=1),
                    )
                },
                state_store=store,
            )

            first = compiler.drain().publication

            self.assertIsNotNone(first)
            restarted = FileCompiledLatticeStateStore(path)
            self.assertEqual(restarted.load(), first)
            second = replace(
                first,
                lattice_version=2,
                feedback_token="lattice-publication-2-{}".format(
                    first.digest[:16],
                ),
            )
            self.assertFalse(
                restarted.compare_and_publish(0, first),
            )
            self.assertTrue(
                restarted.compare_and_publish(1, second),
            )
            self.assertEqual(
                restarted.load().lattice_version,
                2,
            )

            with open(path, "rb") as record:
                valid_encoded = record.read()

            rich_path = os.path.join(directory, "compiled-rich.state")
            rich_store = FileCompiledLatticeStateStore(rich_path)
            rich_compiler = CompiledLatticeCompiler(
                {
                    "gateway": MutableReader(
                        override_projection_snapshot(),
                    )
                },
                state_store=rich_store,
            )
            rich_publication = rich_compiler.drain().publication
            self.assertIsNotNone(rich_publication)
            self.assertEqual(
                FileCompiledLatticeStateStore(rich_path).load(),
                rich_publication,
            )

            rich_argument = rich_publication.plan.config_arguments[0]
            hostile_rich_arguments = (
                replace(
                    rich_argument,
                    candidates=(
                        replace(
                            rich_argument.candidates[0],
                            values=(999,),
                        ),
                    ),
                ),
                replace(
                    rich_argument,
                    provenance=(),
                ),
                replace(
                    rich_argument,
                    candidate_provenance=(),
                ),
            )
            for hostile_argument in hostile_rich_arguments:
                hostile_plan = replace(
                    rich_publication.plan,
                    config_arguments=(hostile_argument,),
                )
                hostile_publication = replace(
                    rich_publication,
                    plan=hostile_plan,
                )
                with open(rich_path, "wb") as record:
                    record.write(
                        _encode_document(
                            "compiled-lattice-publication",
                            hostile_publication,
                        )
                    )
                os.chmod(rich_path, 0o600)
                with self.assertRaises(DurableStateCorrupt):
                    rich_store.load()

            candidate = rich_argument.candidates[0]
            ghost_candidate = replace(
                candidate,
                role="control",
                group="ghost-group",
                routing="coordinator",
                capabilities=("ghost.capability",),
                identity_selectors=(("serial", "ghost-identity"),),
                access_path_ids=("ghost-path",),
                specificities=(2,),
            )
            ghost_candidate_plan = replace(
                rich_publication.plan,
                config_arguments=(
                    replace(
                        rich_argument,
                        candidates=(ghost_candidate,),
                    ),
                ),
            )
            ghost_candidate_publication = replace(
                rich_publication,
                plan=ghost_candidate_plan,
            )
            with open(rich_path, "wb") as record:
                record.write(
                    _encode_document(
                        "compiled-lattice-publication",
                        ghost_candidate_publication,
                    )
                )
            os.chmod(rich_path, 0o600)
            with self.assertRaises(DurableStateCorrupt):
                rich_store.load()

            old_projection_source = candidate.provenance[0][0]
            for hostile_projection_index in (1, 999):
                hostile_projection_source = replace(
                    old_projection_source,
                    source_path=(
                        "/config_projections/{}/values/0".format(
                            hostile_projection_index,
                        )
                    ),
                )
                hostile_candidate = replace(
                    candidate,
                    provenance=((hostile_projection_source,),),
                )
                hostile_argument = replace(
                    rich_argument,
                    provenance=(hostile_projection_source,),
                    candidate_provenance=(hostile_projection_source,),
                    candidates=(hostile_candidate,),
                )
                hostile_fields = tuple(
                    replace(
                        field,
                        provenance=(hostile_projection_source,),
                    )
                    if field.name == "config.{}".format(rich_argument.name)
                    else field
                    for field in rich_publication.plan.fields
                )
                hostile_provenance = tuple(
                    sorted(
                        (hostile_projection_source if source == old_projection_source else source for source in (rich_publication.plan.provenance)),
                        key=lambda source: (
                            source.field_path,
                            source.provider_id,
                            source.generation,
                            source.source_path,
                        ),
                    )
                )
                hostile_projection_plan = replace(
                    rich_publication.plan,
                    config_arguments=(hostile_argument,),
                    fields=hostile_fields,
                    provenance=hostile_provenance,
                )
                hostile_projection_publication = replace(
                    rich_publication,
                    plan=hostile_projection_plan,
                )
                with open(rich_path, "wb") as record:
                    record.write(
                        _encode_document(
                            "compiled-lattice-publication",
                            hostile_projection_publication,
                        )
                    )
                os.chmod(rich_path, 0o600)
                with self.assertRaises(DurableStateCorrupt):
                    rich_store.load()

            reduced_provenance_plan = replace(
                rich_publication.plan,
                provenance=(rich_argument.candidate_provenance[0],),
            )
            reduced_provenance_publication = replace(
                rich_publication,
                plan=reduced_provenance_plan,
            )
            with open(rich_path, "wb") as record:
                record.write(
                    _encode_document(
                        "compiled-lattice-publication",
                        reduced_provenance_publication,
                    )
                )
            os.chmod(rich_path, 0o600)
            with self.assertRaises(DurableStateCorrupt):
                rich_store.load()

            ghost_target_plan = replace(
                second.plan,
                primary_target="ghost-primary",
                control_target="ghost-control",
                materialization_readiness=replace(
                    second.plan.materialization_readiness,
                    ready=True,
                    blockers=(),
                ),
            )
            ghost_target_plan = replace(
                ghost_target_plan,
                digest=_plan_semantic_digest(
                    ghost_target_plan,
                    _canonical_config(
                        ghost_target_plan.projected_config,
                        "projected_config",
                    ),
                ),
            )
            ghost_target_publication = replace(
                second,
                digest=ghost_target_plan.digest,
                plan=ghost_target_plan,
                diagnostics=replace(
                    second.diagnostics,
                    readiness=(ghost_target_plan.materialization_readiness),
                ),
                feedback_token=(
                    "lattice-publication-{}-{}".format(
                        second.lattice_version,
                        ghost_target_plan.digest[:16],
                    )
                ),
            )
            with open(path, "wb") as record:
                record.write(
                    _encode_document(
                        "compiled-lattice-publication",
                        ghost_target_publication,
                    )
                )
            os.chmod(path, 0o600)
            with self.assertRaises(DurableStateCorrupt):
                restarted.load()

            hostile_plans = (
                replace(
                    second.plan,
                    warnings="warning-is-not-a-tuple",
                ),
                replace(
                    second.plan,
                    provider_generations=(("gateway", True),),
                ),
                replace(
                    second.plan,
                    provenance="provenance-is-not-a-tuple",
                ),
            )
            for hostile_plan in hostile_plans:
                hostile_publication = replace(
                    second,
                    plan=hostile_plan,
                )
                with open(path, "wb") as record:
                    record.write(
                        _encode_document(
                            "compiled-lattice-publication",
                            hostile_publication,
                        )
                    )
                os.chmod(path, 0o600)
                with self.assertRaises(DurableStateCorrupt):
                    restarted.load()

            hostile_publications = (
                replace(
                    second,
                    invalidation_causes=(
                        CompilationInvalidation(
                            "provider",
                            "ghost",
                            1,
                            "ghost provider",
                        ),
                    ),
                ),
                replace(
                    second,
                    invalidation_causes=(
                        CompilationInvalidation(
                            "provider",
                            "gateway",
                            2,
                            "generation above cursor",
                        ),
                    ),
                ),
                replace(
                    second,
                    invalidation_causes=(
                        CompilationInvalidation(
                            "user_override",
                            "user-overrides",
                            1,
                            "override without cursor",
                        ),
                    ),
                ),
                replace(
                    second,
                    user_override_generation=1,
                    user_override_fingerprint="f" * 64,
                    user_override_requested_generation=1,
                    invalidation_causes=(
                        CompilationInvalidation(
                            "user_override",
                            "user-overrides",
                            2,
                            "override generation above cursor",
                        ),
                    ),
                ),
                replace(
                    second,
                    feedback_token="arbitrary-feedback-token",
                ),
            )
            for hostile_publication in hostile_publications:
                with open(path, "wb") as record:
                    record.write(
                        _encode_document(
                            "compiled-lattice-publication",
                            hostile_publication,
                        )
                    )
                os.chmod(path, 0o600)
                with self.assertRaises(DurableStateCorrupt):
                    restarted.load()

            base_document = json.loads(
                valid_encoded.decode("ascii"),
            )
            for hostile_version in (True, 1.0):
                document = json.loads(
                    valid_encoded.decode("ascii"),
                )
                document["version"] = hostile_version
                signed = {
                    key: document[key]
                    for key in (
                        "format",
                        "version",
                        "kind",
                        "payload",
                    )
                }
                document["sha256"] = hashlib.sha256(
                    json.dumps(
                        signed,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                        allow_nan=False,
                    ).encode("ascii")
                ).hexdigest()
                with open(path, "wb") as record:
                    record.write(
                        json.dumps(
                            document,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=True,
                            allow_nan=False,
                        ).encode("ascii")
                    )
                os.chmod(path, 0o600)
                with self.assertRaises(DurableStateCorrupt):
                    restarted.load()

            self.assertEqual(
                base_document["version"],
                1,
            )
            rewritten = valid_encoded.replace(
                first.digest.encode("ascii"),
                ("0" * 64).encode("ascii"),
                1,
            )
            self.assertNotEqual(valid_encoded, rewritten)
            with open(path, "wb") as record:
                record.write(rewritten)
            os.chmod(path, 0o600)
            with self.assertRaises(DurableStateCorrupt):
                restarted.load()

    def test_materializer_state_is_secret_free_and_fences_survive_restart(
        self,
    ):
        """Coordinator checkpoints retain digests, never raw target values."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "materializer.state")
            store = FileAtomicMaterializationStateStore(path)
            request = envelope(
                token="raw-feedback-secret",
            )
            (
                _projection,
                projection_digest,
                feedback_digest,
                provenance_digest,
                provenance_count,
            ) = _validate_envelope(request)
            first_fence = store.allocate_fence()
            state = AtomicMaterializationState.reserve(
                1,
                request,
                projection_digest,
                feedback_digest,
                provenance_digest,
                provenance_count,
                first_fence,
                None,
            )

            self.assertTrue(
                store.compare_and_store(None, state),
            )
            with open(path, "rb") as record:
                encoded = record.read()
            self.assertNotIn(b"raw-feedback-secret", encoded)
            self.assertNotIn(b"api_key", encoded)

            restarted = FileAtomicMaterializationStateStore(path)
            self.assertEqual(restarted.load(), state)
            self.assertEqual(
                restarted.allocate_fence(),
                first_fence + 1,
            )
            second_restart = FileAtomicMaterializationStateStore(path)
            self.assertEqual(
                second_restart.allocate_fence(),
                first_fence + 2,
            )

    def test_opaque_target_journal_is_bounded_fenced_and_private(self):
        """The target alone owns bounded bytes behind an exact fenced CAS."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "target.journal")
            store = FileOpaqueTargetJournal(path)
            payload = b'{"preimage":{"api_key":"target-secret"}}'
            first = OpaqueTargetJournalEntry(
                1,
                TargetOperationKey(7, "1" * 64),
                payload,
            )

            self.assertNotIn("target-secret", repr(first))
            self.assertTrue(
                store.compare_and_store(None, first),
            )
            self.assertEqual(
                FileOpaqueTargetJournal(path).load(),
                first,
            )
            with open(path, "rb") as record:
                encoded = record.read()
            self.assertNotIn(payload, encoded)
            self.assertEqual(
                stat.S_IMODE(os.stat(path).st_mode),
                0o600,
            )

            with self.assertRaises(ValueError):
                store.compare_and_store(
                    first,
                    OpaqueTargetJournalEntry(
                        2,
                        TargetOperationKey(6, "2" * 64),
                        b"regressed",
                    ),
                )
            with self.assertRaises(ValueError):
                store.compare_and_store(
                    first,
                    OpaqueTargetJournalEntry(
                        3,
                        TargetOperationKey(8, "2" * 64),
                        b"skipped",
                    ),
                )
            second = OpaqueTargetJournalEntry(
                2,
                TargetOperationKey(8, "2" * 64),
                b"next",
            )
            self.assertTrue(
                store.compare_and_store(first, second),
            )
            with self.assertRaises(ValueError):
                OpaqueTargetJournalEntry(
                    3,
                    TargetOperationKey(9, "3" * 64),
                    b"x" * (MAX_OPAQUE_PAYLOAD_BYTES + 1),
                )

    def test_torn_and_oversized_records_fail_closed(self):
        """Missing is empty, while torn and oversized records are corruption."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "fragment.state")
            store = FileFragmentAdapterStateStore(path)

            self.assertIsNone(store.load())
            with open(path, "wb") as record:
                record.write(b'{"format":"predbat-lattice-state"')
            os.chmod(path, 0o600)
            with self.assertRaises(DurableStateCorrupt):
                store.load()

            with open(path, "wb") as record:
                record.truncate(MAX_RECORD_BYTES + 1)
            os.chmod(path, 0o600)
            with self.assertRaises(DurableStateCorrupt):
                store.load()

            payload = ["none"]
            for _depth in range(70):
                payload = ["tuple", [payload]]
            signed = {
                "format": "predbat-lattice-state",
                "version": 1,
                "kind": "fragment-adapter-state",
                "payload": payload,
            }
            canonical_signed = json.dumps(
                signed,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
            document = dict(signed)
            document["sha256"] = hashlib.sha256(
                canonical_signed,
            ).hexdigest()
            encoded = json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
            with open(path, "wb") as record:
                record.write(encoded)
            os.chmod(path, 0o600)
            with self.assertRaises(DurableStateCorrupt):
                store.load()

            os.unlink(path)
            os.mkfifo(path, 0o600)
            with self.assertRaises(DurableStateCorrupt):
                store.load()
            os.unlink(path)

            fifo_lock_path = os.path.join(
                directory,
                "fifo-lock.state",
            )
            fifo_lock_store = FileFragmentAdapterStateStore(
                fifo_lock_path,
            )
            os.mkfifo(fifo_lock_path + ".lock", 0o600)
            with self.assertRaises(DurableStateError):
                fifo_lock_store.load()

            fault_path = os.path.join(
                directory,
                "fault-lock.state",
            )
            fault_store = FileFragmentAdapterStateStore(
                fault_path,
            )
            directory_fd = fault_store._record._open_directory()
            seed_descriptor = os.open(
                fault_path + ".lock",
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            fault_descriptor = os.dup(seed_descriptor)
            try:
                with mock.patch(
                    "lattice_durable_runtime_stores.os.open",
                    return_value=fault_descriptor,
                ), mock.patch(
                    "lattice_durable_runtime_stores.os.fstat",
                    side_effect=RuntimeError("injected fstat failure"),
                ):
                    with self.assertRaises(RuntimeError):
                        fault_store._record._open_lock(
                            directory_fd,
                        )
                with self.assertRaises(OSError):
                    os.fstat(fault_descriptor)
            finally:
                os.close(seed_descriptor)
                os.close(directory_fd)

            unsafe_directory = os.path.join(directory, "unsafe")
            os.mkdir(unsafe_directory, 0o700)
            os.chmod(unsafe_directory, 0o770)
            with self.assertRaises(DurableStateError):
                FileFragmentAdapterStateStore(
                    os.path.join(unsafe_directory, "state"),
                )

            real_directory = os.path.join(directory, "real")
            linked_directory = os.path.join(directory, "linked")
            os.mkdir(real_directory, 0o700)
            os.symlink(real_directory, linked_directory)
            with self.assertRaises(DurableStateError):
                FileFragmentAdapterStateStore(
                    os.path.join(linked_directory, "state"),
                )


if __name__ == "__main__":
    unittest.main()
