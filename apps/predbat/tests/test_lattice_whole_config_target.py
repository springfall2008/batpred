"""Tests for the detached Lattice generated-overlay whole-config target."""

# cspell:ignore autoconfig mispaired Noncanonical readback recoverably

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_atomic_materializer import (  # noqa: E402
    MAX_SEQUENCE,
    TargetOperationKey,
    TargetOperationPhase,
    TargetTerminalPhase,
    _canonical_config,
    _config_digest,
)
from lattice_durable_runtime_stores import (  # noqa: E402
    FileOpaqueTargetJournal,
    OpaqueTargetJournalEntry,
)
from lattice_whole_config_target import (  # noqa: E402
    GeneratedOverlayWholeConfigTarget,
    WholeConfigTargetConflict,
    WholeConfigTargetCorrupt,
    WholeConfigTargetError,
)


ALLOWED_KEYS = (
    "bar",
    "enabled",
    "foo",
    "list_value",
    "none_value",
)


def digest(config):
    """Return the materializer's exact canonical projection digest."""
    return _config_digest(
        _canonical_config(
            config,
            "test projection",
        )
    )


def exact_overlay(path, config):
    """Create one private exact canonical JSON overlay."""
    with open(path, "wb") as overlay:
        overlay.write(
            json.dumps(
                config,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
        )
    os.chmod(path, 0o600)


def read_overlay(path):
    """Read a test overlay as plain JSON."""
    with open(path, "rb") as overlay:
        encoded = overlay.read()
    return encoded, json.loads(encoded.decode("ascii"))


class FailingWriteTarget(GeneratedOverlayWholeConfigTarget):
    """Target with deterministic failures around the atomic replace."""

    def __init__(self, *args, **kwargs):
        """Create a target whose write fault defaults to disabled."""
        super().__init__(*args, **kwargs)
        self.fail_before_write = False
        self.fail_after_write = False
        self.fail_before_remove = False
        self.fail_after_remove = False

    def _write_overlay(self, directory_fd, config):
        """Cut before or after the complete replace."""
        if self.fail_before_write:
            raise OSError("secret-before-write")
        super()._write_overlay(directory_fd, config)
        if self.fail_after_write:
            raise OSError("secret-after-write")

    def _remove_overlay(self, directory_fd):
        """Cut before or after restoring exact absence."""
        if self.fail_before_remove:
            raise OSError("secret-before-remove")
        super()._remove_overlay(directory_fd)
        if self.fail_after_remove:
            raise OSError("secret-after-remove")


class BlockingWriteTarget(GeneratedOverlayWholeConfigTarget):
    """Target that pauses while holding its target-wide outer lock."""

    def __init__(self, *args, **kwargs):
        """Create write coordination events."""
        super().__init__(*args, **kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()

    def _write_overlay(self, directory_fd, config):
        """Block immediately before the atomic full-file write."""
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("test write release timed out")
        super()._write_overlay(directory_fd, config)


class RejectingJournal:
    """Journal wrapper that rejects one exact compare-and-store call."""

    def __init__(self, delegate, reject_call):
        """Wrap a real durable journal."""
        self.delegate = delegate
        self.reject_call = reject_call
        self.calls = 0

    def load(self):
        """Delegate exact load."""
        return self.delegate.load()

    def compare_and_store(self, expected, replacement):
        """Reject the selected CAS before any replacement."""
        self.calls += 1
        if self.calls == self.reject_call:
            return False
        return self.delegate.compare_and_store(
            expected,
            replacement,
        )


class BoundaryJournal:
    """Exact in-memory journal whose seed revision can sit near exhaustion."""

    def __init__(self, entry):
        """Create a journal at one exact current entry."""
        self.entry = entry
        self.writes = 0

    def load(self):
        """Return the exact current entry."""
        return self.entry

    def compare_and_store(self, expected, replacement):
        """CAS exact identity and one next bounded revision."""
        if self.entry != expected:
            return False
        if replacement.revision != expected.revision + 1:
            raise ValueError("boundary journal revision is not next")
        self.entry = replacement
        self.writes += 1
        return True


class MutatingJournal:
    """Journal wrapper that mutates the overlay after selected successful CAS calls."""

    def __init__(self, delegate, mutations):
        """Wrap a journal with call-numbered post-store callbacks."""
        self.delegate = delegate
        self.mutations = mutations
        self.calls = 0

    def load(self):
        """Delegate exact load."""
        return self.delegate.load()

    def compare_and_store(self, expected, replacement):
        """Run the selected mutation only after the replacement is durable."""
        stored = self.delegate.compare_and_store(
            expected,
            replacement,
        )
        self.calls += 1
        mutation = self.mutations.get(self.calls)
        if stored and mutation is not None:
            mutation()
        return stored


class GeneratedOverlayTargetTestCase(unittest.TestCase):
    """Exercise complete target lifecycle, recovery, and filesystem safety."""

    def setUp(self):
        """Create an isolated target with a real filesystem journal."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.overlay_path = os.path.join(
            self.temporary.name,
            "lattice-generated-overlay.json",
        )
        self.journal_path = os.path.join(
            self.temporary.name,
            "lattice-target.journal",
        )
        self.journal = FileOpaqueTargetJournal(
            self.journal_path,
        )
        self.target = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            self.journal,
            ALLOWED_KEYS,
        )

    def prepare(self, config, fence=1, target=None):
        """Prepare one exact projection."""
        target = self.target if target is None else target
        return target.prepare(
            fence,
            digest(config),
            config,
        )

    def boundary_target(self, revision):
        """Rebind the current exact payload to a chosen journal revision."""
        entry = self.journal.load()
        boundary = BoundaryJournal(
            OpaqueTargetJournalEntry(
                revision,
                entry.key,
                entry.payload,
            )
        )
        target = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            boundary,
            ALLOWED_KEYS,
        )
        return target, boundary

    def test_full_overlay_apply_restart_and_exact_absent_rollback(self):
        """Apply replaces the complete file and rollback restores absence."""
        projection = {
            "enabled": True,
            "foo": 7,
            "list_value": ["sensor.one", None],
        }
        prepared = self.prepare(projection)

        self.assertFalse(os.path.exists(self.overlay_path))
        self.assertEqual(
            prepared.phase,
            TargetOperationPhase.PREPARED,
        )
        self.assertEqual(
            prepared.preimage_digest,
            digest({}),
        )
        self.assertEqual(
            prepared.candidate_digest,
            digest(projection),
        )
        self.assertTrue(prepared.changed)
        self.assertNotIn("sensor.one", repr(self.journal.load()))
        self.assertNotIn("sensor.one", repr(self.target))

        applied = self.target.apply(prepared)
        self.assertEqual(
            applied.phase,
            TargetOperationPhase.APPLIED,
        )
        encoded, observed = read_overlay(self.overlay_path)
        self.assertEqual(observed, projection)
        self.assertEqual(
            encoded,
            b'{"enabled":true,"foo":7,"list_value":["sensor.one",null]}',
        )
        self.assertEqual(
            stat.S_IMODE(os.stat(self.overlay_path).st_mode),
            0o600,
        )

        sealed = self.target.seal(applied)
        self.assertEqual(
            sealed.terminal_phase,
            TargetTerminalPhase.APPLIED,
        )
        restarted = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            FileOpaqueTargetJournal(self.journal_path),
            tuple(reversed(ALLOWED_KEYS)),
        )
        self.assertEqual(
            restarted.lookup(1, digest(projection)),
            sealed,
        )

        rolled_back = restarted.rollback(sealed)
        self.assertEqual(
            rolled_back.phase,
            TargetOperationPhase.ROLLED_BACK,
        )
        self.assertFalse(os.path.exists(self.overlay_path))
        resealed = restarted.seal(rolled_back)
        self.assertEqual(
            resealed.terminal_phase,
            TargetTerminalPhase.ROLLED_BACK,
        )

    def test_apply_is_complete_replacement_and_rollback_is_exact_preimage(self):
        """Keys absent from the candidate are removed, then restored."""
        preimage = {
            "bar": 2,
            "foo": 1,
            "none_value": None,
        }
        exact_overlay(
            self.overlay_path,
            preimage,
        )
        before = read_overlay(self.overlay_path)[0]
        prepared = self.prepare(
            {
                "foo": 9,
            }
        )
        applied = self.target.apply(prepared)

        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 9},
        )
        sealed = self.target.seal(applied)
        rolled_back = self.target.rollback(sealed)
        self.assertEqual(
            rolled_back.phase,
            TargetOperationPhase.ROLLED_BACK,
        )
        self.assertEqual(
            read_overlay(self.overlay_path)[0],
            before,
        )

    def test_unchanged_candidate_preserves_exact_file_identity(self):
        """A type-exact unchanged full config performs no overlay write."""
        projection = {
            "enabled": False,
            "foo": 4,
        }
        exact_overlay(
            self.overlay_path,
            projection,
        )
        before = os.stat(self.overlay_path)
        prepared = self.prepare(projection)

        self.assertFalse(prepared.changed)
        applied = self.target.apply(prepared)
        after = os.stat(self.overlay_path)
        self.assertEqual(
            (after.st_dev, after.st_ino, after.st_mtime_ns),
            (before.st_dev, before.st_ino, before.st_mtime_ns),
        )
        self.assertEqual(
            self.target.seal(applied).terminal_phase,
            TargetTerminalPhase.APPLIED,
        )

    def test_absent_and_empty_projection_is_idempotent_without_file_creation(self):
        """Semantic empty-over-empty keeps the exact absent pre-image."""
        prepared = self.prepare({})
        self.assertFalse(prepared.changed)

        applied = self.target.apply(prepared)
        self.assertFalse(os.path.exists(self.overlay_path))
        sealed = self.target.seal(applied)
        rolled_back = self.target.rollback(sealed)
        self.assertFalse(os.path.exists(self.overlay_path))
        self.assertEqual(
            rolled_back.phase,
            TargetOperationPhase.ROLLED_BACK,
        )

    def test_external_mutation_after_prepare_fails_unknown_without_overwrite(self):
        """A fenced pre-image mismatch is quarantined, never blindly replaced."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare({"foo": 2})
        exact_overlay(
            self.overlay_path,
            {"foo": 99},
        )

        observed = self.target.apply(prepared)
        self.assertEqual(
            observed.phase,
            TargetOperationPhase.UNKNOWN,
        )
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 99},
        )
        sealed = self.target.invalidate(prepared.key)
        self.assertEqual(
            sealed.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 99},
        )

    def test_external_mutation_after_apply_blocks_rollback(self):
        """Rollback never overwrites a candidate mismatch."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare({"foo": 2})
        sealed = self.target.seal(self.target.apply(prepared))
        exact_overlay(
            self.overlay_path,
            {"foo": 77},
        )

        observed = self.target.rollback(sealed)
        self.assertEqual(
            observed.phase,
            TargetOperationPhase.SEALED,
        )
        self.assertEqual(
            observed.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 77},
        )
        quarantined = self.target.invalidate(sealed.key)
        self.assertEqual(
            quarantined.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )

    def test_seal_rechecks_applied_visibility_after_mutation(self):
        """APPLIED cannot be sealed after its exact candidate disappears."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare({"foo": 2})
        applied = self.target.apply(prepared)
        exact_overlay(
            self.overlay_path,
            {"foo": 88},
        )

        sealed = self.target.seal(applied)
        self.assertEqual(
            sealed.phase,
            TargetOperationPhase.SEALED,
        )
        self.assertEqual(
            sealed.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 88},
        )

    def test_seal_rechecks_prepared_and_rolled_back_visibility(self):
        """PREPARED/ROLLED_BACK seal only while the exact pre-image remains."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare({"foo": 2})
        exact_overlay(
            self.overlay_path,
            {"foo": 77},
        )
        sealed_prepared = self.target.seal(prepared)
        self.assertEqual(
            sealed_prepared.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )

        # Exercise the ROLLED_BACK boundary in an independent journal.
        with tempfile.TemporaryDirectory() as directory:
            overlay_path = os.path.join(directory, "generated.json")
            journal_path = os.path.join(directory, "target.journal")
            exact_overlay(overlay_path, {"foo": 1})
            target = GeneratedOverlayWholeConfigTarget(
                overlay_path,
                FileOpaqueTargetJournal(journal_path),
                ALLOWED_KEYS,
            )
            projection = {"foo": 2}
            operation = target.prepare(
                1,
                digest(projection),
                projection,
            )
            applied = target.apply(operation)
            sealed = target.seal(applied)
            rolled_back = target.rollback(sealed)
            exact_overlay(overlay_path, {"foo": 99})
            sealed_rollback = target.seal(rolled_back)
            self.assertEqual(
                sealed_rollback.terminal_phase,
                TargetTerminalPhase.UNKNOWN,
            )
            self.assertEqual(
                read_overlay(overlay_path)[1],
                {"foo": 99},
            )

    def test_apply_rechecks_visibility_after_applied_cas(self):
        """A mutation after the APPLIED CAS is durably downgraded to UNKNOWN."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare({"foo": 2})
        mutating = MutatingJournal(
            FileOpaqueTargetJournal(self.journal_path),
            {
                2: lambda: exact_overlay(
                    self.overlay_path,
                    {"foo": 77},
                )
            },
        )
        target = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            mutating,
            ALLOWED_KEYS,
        )

        observed = target.apply(prepared)

        self.assertEqual(
            observed.phase,
            TargetOperationPhase.UNKNOWN,
        )
        self.assertEqual(mutating.calls, 3)
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 77},
        )

    def test_prepare_rechecks_visibility_after_prepared_cas_mutation(self):
        """A mutation after PREPARED CAS is durably downgraded to UNKNOWN."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        mutating = MutatingJournal(
            FileOpaqueTargetJournal(self.journal_path),
            {
                1: lambda: exact_overlay(
                    self.overlay_path,
                    {"foo": 77},
                )
            },
        )
        target = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            mutating,
            ALLOWED_KEYS,
        )

        observed = self.prepare(
            {"foo": 2},
            target=target,
        )

        self.assertEqual(
            observed.phase,
            TargetOperationPhase.UNKNOWN,
        )
        self.assertEqual(mutating.calls, 2)
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 77},
        )

    def test_prepare_rechecks_read_failure_after_prepared_cas(self):
        """A read failure after PREPARED CAS is durably UNKNOWN, not acknowledged."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        original_read = self.target._read_overlay
        reads = [0]

        def fail_second_read(directory_fd):
            reads[0] += 1
            if reads[0] == 2:
                raise WholeConfigTargetCorrupt("injected-post-prepare-read")
            return original_read(directory_fd)

        with mock.patch.object(
            self.target,
            "_read_overlay",
            side_effect=fail_second_read,
        ):
            observed = self.prepare({"foo": 2})

        self.assertEqual(
            observed.phase,
            TargetOperationPhase.UNKNOWN,
        )
        self.assertEqual(self.journal.load().revision, 2)

    def test_rollback_rechecks_visibility_after_rolled_back_cas(self):
        """A mutation after ROLLED_BACK CAS is durably downgraded to UNKNOWN."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare({"foo": 2})
        sealed = self.target.seal(self.target.apply(prepared))
        mutating = MutatingJournal(
            FileOpaqueTargetJournal(self.journal_path),
            {
                2: lambda: exact_overlay(
                    self.overlay_path,
                    {"foo": 88},
                )
            },
        )
        target = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            mutating,
            ALLOWED_KEYS,
        )

        observed = target.rollback(sealed)

        self.assertEqual(
            observed.phase,
            TargetOperationPhase.UNKNOWN,
        )
        self.assertEqual(mutating.calls, 3)
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 88},
        )

    def test_seal_rechecks_visibility_after_sealed_cas(self):
        """A mutation after SEALED CAS consumes the reserved UNKNOWN revision."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare({"foo": 2})
        applied = self.target.apply(prepared)
        mutating = MutatingJournal(
            FileOpaqueTargetJournal(self.journal_path),
            {
                1: lambda: exact_overlay(
                    self.overlay_path,
                    {"foo": 99},
                )
            },
        )
        target = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            mutating,
            ALLOWED_KEYS,
        )

        observed = target.seal(applied)

        self.assertEqual(
            observed.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )
        self.assertEqual(mutating.calls, 2)
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 99},
        )

    def test_invalidate_rechecks_visibility_after_sealed_cas(self):
        """A mutation after invalidate's SEALED CAS is durably UNKNOWN."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare({"foo": 2})
        mutating = MutatingJournal(
            FileOpaqueTargetJournal(self.journal_path),
            {
                1: lambda: exact_overlay(
                    self.overlay_path,
                    {"foo": 66},
                )
            },
        )
        target = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            mutating,
            ALLOWED_KEYS,
        )

        observed = target.invalidate(prepared.key)

        self.assertEqual(
            observed.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )
        self.assertEqual(mutating.calls, 2)
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 66},
        )

    def test_post_stable_cas_read_errors_are_durably_unknown(self):
        """APPLIED and ROLLED_BACK post-CAS read errors cannot be acknowledged."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare({"foo": 2})
        original_read = self.target._read_overlay
        apply_reads = [0]

        def fail_fourth_apply_read(directory_fd):
            apply_reads[0] += 1
            if apply_reads[0] == 4:
                raise WholeConfigTargetCorrupt("injected-post-applied-read")
            return original_read(directory_fd)

        with mock.patch.object(
            self.target,
            "_read_overlay",
            side_effect=fail_fourth_apply_read,
        ):
            applied_unknown = self.target.apply(prepared)
        self.assertEqual(
            applied_unknown.phase,
            TargetOperationPhase.UNKNOWN,
        )

        with tempfile.TemporaryDirectory() as directory:
            overlay_path = os.path.join(directory, "generated.json")
            journal_path = os.path.join(directory, "target.journal")
            exact_overlay(overlay_path, {"foo": 1})
            target = GeneratedOverlayWholeConfigTarget(
                overlay_path,
                FileOpaqueTargetJournal(journal_path),
                ALLOWED_KEYS,
            )
            projection = {"foo": 2}
            operation = target.prepare(
                1,
                digest(projection),
                projection,
            )
            sealed = target.seal(target.apply(operation))
            original_rollback_read = target._read_overlay
            rollback_reads = [0]

            def fail_fifth_rollback_read(directory_fd):
                rollback_reads[0] += 1
                if rollback_reads[0] == 5:
                    raise WholeConfigTargetCorrupt("injected-post-rollback-read")
                return original_rollback_read(directory_fd)

            with mock.patch.object(
                target,
                "_read_overlay",
                side_effect=fail_fifth_rollback_read,
            ):
                rolled_back_unknown = target.rollback(sealed)
            self.assertEqual(
                rolled_back_unknown.phase,
                TargetOperationPhase.UNKNOWN,
            )

    def test_restart_lookup_downgrades_stale_sealed_applied_to_unknown(self):
        """A restart never blindly acknowledges a stale sealed APPLIED result."""
        prepared = self.prepare({"foo": 2})
        sealed = self.target.seal(self.target.apply(prepared))
        self.assertEqual(
            sealed.terminal_phase,
            TargetTerminalPhase.APPLIED,
        )
        os.unlink(self.overlay_path)

        restarted = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            FileOpaqueTargetJournal(self.journal_path),
            ALLOWED_KEYS,
        )
        observed = restarted.lookup(
            sealed.key.fence,
            sealed.key.projection_digest,
        )
        self.assertEqual(
            observed.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )
        recovered = restarted.invalidate(sealed.key)
        self.assertEqual(
            recovered.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )

    def test_restart_invalidate_downgrades_mutated_sealed_applied(self):
        """Invalidate also re-proves sealed APPLIED visibility after restart."""
        prepared = self.prepare({"foo": 2})
        sealed = self.target.seal(self.target.apply(prepared))
        exact_overlay(
            self.overlay_path,
            {"foo": 55},
        )
        restarted = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            FileOpaqueTargetJournal(self.journal_path),
            ALLOWED_KEYS,
        )

        observed = restarted.invalidate(sealed.key)
        self.assertEqual(
            observed.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 55},
        )

    def test_restart_lookup_downgrades_stale_sealed_rollback_to_unknown(self):
        """A sealed rollback is acknowledged only while its pre-image is exact."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare({"foo": 2})
        sealed = self.target.seal(self.target.apply(prepared))
        rolled_back = self.target.rollback(sealed)
        sealed_rollback = self.target.seal(rolled_back)
        exact_overlay(
            self.overlay_path,
            {"foo": 44},
        )

        restarted = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            FileOpaqueTargetJournal(self.journal_path),
            ALLOWED_KEYS,
        )
        observed = restarted.lookup(
            sealed_rollback.key.fence,
            sealed_rollback.key.projection_digest,
        )
        self.assertEqual(
            observed.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )

    def test_apply_crash_before_write_recovers_rolled_back(self):
        """APPLYING plus the exact pre-image cannot be mistaken for commit."""
        target = FailingWriteTarget(
            self.overlay_path,
            self.journal,
            ALLOWED_KEYS,
        )
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare(
            {"foo": 2},
            target=target,
        )
        target.fail_before_write = True
        with self.assertRaisesRegex(OSError, "secret-before-write"):
            target.apply(prepared)

        restarted = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            FileOpaqueTargetJournal(self.journal_path),
            ALLOWED_KEYS,
        )
        self.assertEqual(
            restarted.lookup(
                prepared.key.fence,
                prepared.key.projection_digest,
            ).phase,
            TargetOperationPhase.APPLYING,
        )
        recovered = restarted.invalidate(prepared.key)
        self.assertEqual(
            recovered.terminal_phase,
            TargetTerminalPhase.ROLLED_BACK,
        )
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 1},
        )

    def test_apply_crash_after_exact_replace_recovers_applied(self):
        """APPLYING plus the exact candidate is a durable committed outcome."""
        target = FailingWriteTarget(
            self.overlay_path,
            self.journal,
            ALLOWED_KEYS,
        )
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare(
            {"foo": 2},
            target=target,
        )
        target.fail_after_write = True
        with self.assertRaisesRegex(OSError, "secret-after-write"):
            target.apply(prepared)

        restarted = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            FileOpaqueTargetJournal(self.journal_path),
            ALLOWED_KEYS,
        )
        recovered = restarted.invalidate(prepared.key)
        self.assertEqual(
            recovered.terminal_phase,
            TargetTerminalPhase.APPLIED,
        )
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 2},
        )

    def test_rollback_crash_before_and_after_absence_restore_are_exact(self):
        """ROLLING_BACK classifies candidate and absent pre-image distinctly."""
        target = FailingWriteTarget(
            self.overlay_path,
            self.journal,
            ALLOWED_KEYS,
        )
        prepared = self.prepare(
            {"foo": 2},
            target=target,
        )
        sealed = target.seal(target.apply(prepared))
        target.fail_before_remove = True
        with self.assertRaisesRegex(OSError, "secret-before-remove"):
            target.rollback(sealed)

        restarted = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            FileOpaqueTargetJournal(self.journal_path),
            ALLOWED_KEYS,
        )
        still_applied = restarted.invalidate(sealed.key)
        self.assertEqual(
            still_applied.terminal_phase,
            TargetTerminalPhase.APPLIED,
        )

        restored_first = restarted.rollback(still_applied)
        restarted.seal(restored_first)
        # Use a new fenced operation whose pre-image is again absent.
        second = self.prepare(
            {"foo": 3},
            fence=2,
        )
        second_sealed = self.target.seal(self.target.apply(second))
        second_target = FailingWriteTarget(
            self.overlay_path,
            FileOpaqueTargetJournal(self.journal_path),
            ALLOWED_KEYS,
        )
        second_target.fail_after_remove = True
        with self.assertRaisesRegex(OSError, "secret-after-remove"):
            second_target.rollback(second_sealed)
        final_restart = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            FileOpaqueTargetJournal(self.journal_path),
            ALLOWED_KEYS,
        )
        restored = final_restart.invalidate(second.key)
        self.assertEqual(
            restored.terminal_phase,
            TargetTerminalPhase.ROLLED_BACK,
        )
        self.assertFalse(os.path.exists(self.overlay_path))

    def test_applying_journal_cas_failure_precedes_overlay_write(self):
        """No config mutation occurs unless APPLYING is durably acknowledged."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        rejecting = RejectingJournal(
            self.journal,
            reject_call=2,
        )
        target = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            rejecting,
            ALLOWED_KEYS,
        )
        prepared = self.prepare(
            {"foo": 2},
            target=target,
        )

        with self.assertRaises(WholeConfigTargetConflict):
            target.apply(prepared)
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 1},
        )
        self.assertEqual(
            FileOpaqueTargetJournal(self.journal_path).load().revision,
            1,
        )

    def test_apply_reserves_complete_revision_headroom_before_file_effect(self):
        """APPLYING reserves APPLIED, SEALED, and later UNKNOWN revisions."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare({"foo": 2})
        boundary_target, boundary = self.boundary_target(MAX_SEQUENCE - 3)
        before = read_overlay(self.overlay_path)[0]

        with self.assertRaisesRegex(
            WholeConfigTargetError,
            "insufficient lifecycle headroom",
        ):
            boundary_target.apply(prepared)
        self.assertEqual(boundary.writes, 0)
        self.assertEqual(
            read_overlay(self.overlay_path)[0],
            before,
        )
        self.assertEqual(
            boundary_target.lookup(
                prepared.key.fence,
                prepared.key.projection_digest,
            ).phase,
            TargetOperationPhase.PREPARED,
        )

        boundary_target, boundary = self.boundary_target(MAX_SEQUENCE - 4)
        applied = boundary_target.apply(prepared)
        self.assertEqual(
            applied.phase,
            TargetOperationPhase.APPLIED,
        )
        sealed = boundary_target.seal(applied)
        self.assertEqual(
            sealed.terminal_phase,
            TargetTerminalPhase.APPLIED,
        )
        self.assertEqual(
            boundary.entry.revision,
            MAX_SEQUENCE - 1,
        )
        exact_overlay(
            self.overlay_path,
            {"foo": 9},
        )
        unknown = boundary_target.lookup(
            prepared.key.fence,
            prepared.key.projection_digest,
        )
        self.assertEqual(
            unknown.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )
        self.assertEqual(
            boundary.entry.revision,
            MAX_SEQUENCE,
        )

    def test_rollback_reserves_complete_revision_headroom_before_file_effect(self):
        """ROLLING_BACK reserves rollback, seal, and later UNKNOWN revisions."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare({"foo": 2})
        sealed = self.target.seal(self.target.apply(prepared))
        boundary_target, boundary = self.boundary_target(MAX_SEQUENCE - 3)
        before = read_overlay(self.overlay_path)[0]

        with self.assertRaisesRegex(
            WholeConfigTargetError,
            "insufficient lifecycle headroom",
        ):
            boundary_target.rollback(sealed)
        self.assertEqual(boundary.writes, 0)
        self.assertEqual(
            read_overlay(self.overlay_path)[0],
            before,
        )
        self.assertEqual(
            boundary_target.lookup(
                sealed.key.fence,
                sealed.key.projection_digest,
            ).terminal_phase,
            TargetTerminalPhase.APPLIED,
        )

        boundary_target, boundary = self.boundary_target(MAX_SEQUENCE - 4)
        rolled_back = boundary_target.rollback(sealed)
        self.assertEqual(
            rolled_back.phase,
            TargetOperationPhase.ROLLED_BACK,
        )
        resealed = boundary_target.seal(rolled_back)
        self.assertEqual(
            resealed.terminal_phase,
            TargetTerminalPhase.ROLLED_BACK,
        )
        self.assertEqual(
            boundary.entry.revision,
            MAX_SEQUENCE - 1,
        )
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 1},
        )
        exact_overlay(
            self.overlay_path,
            {"foo": 9},
        )
        unknown = boundary_target.lookup(
            sealed.key.fence,
            sealed.key.projection_digest,
        )
        self.assertEqual(
            unknown.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )
        self.assertEqual(
            boundary.entry.revision,
            MAX_SEQUENCE,
        )

    def test_post_applied_cas_mutation_reaches_sealed_unknown_at_max(self):
        """MAX-4 covers apply, post-CAS UNKNOWN, and exact UNKNOWN sealing."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare({"foo": 2})
        _boundary_target, boundary = self.boundary_target(MAX_SEQUENCE - 4)
        mutating = MutatingJournal(
            boundary,
            {
                2: lambda: exact_overlay(
                    self.overlay_path,
                    {"foo": 77},
                )
            },
        )
        target = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            mutating,
            ALLOWED_KEYS,
        )

        unknown = target.apply(prepared)
        self.assertEqual(
            unknown.phase,
            TargetOperationPhase.UNKNOWN,
        )
        self.assertEqual(boundary.entry.revision, MAX_SEQUENCE - 1)
        sealed = target.invalidate(prepared.key)
        self.assertEqual(
            sealed.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )
        self.assertEqual(boundary.entry.revision, MAX_SEQUENCE)

    def test_post_rolled_back_cas_mutation_reaches_sealed_unknown_at_max(self):
        """MAX-4 covers rollback, post-CAS UNKNOWN, and exact UNKNOWN sealing."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare({"foo": 2})
        sealed_applied = self.target.seal(self.target.apply(prepared))
        _boundary_target, boundary = self.boundary_target(MAX_SEQUENCE - 4)
        mutating = MutatingJournal(
            boundary,
            {
                2: lambda: exact_overlay(
                    self.overlay_path,
                    {"foo": 88},
                )
            },
        )
        target = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            mutating,
            ALLOWED_KEYS,
        )

        unknown = target.rollback(sealed_applied)
        self.assertEqual(
            unknown.phase,
            TargetOperationPhase.UNKNOWN,
        )
        self.assertEqual(boundary.entry.revision, MAX_SEQUENCE - 1)
        sealed = target.invalidate(prepared.key)
        self.assertEqual(
            sealed.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )
        self.assertEqual(boundary.entry.revision, MAX_SEQUENCE)

    def test_prepare_requires_full_apply_and_seal_revision_budget(self):
        """A new PREPARED entry cannot consume the final lifecycle revisions."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        first = self.prepare({"foo": 1})
        sealed = self.target.seal(first)
        self.assertEqual(
            sealed.terminal_phase,
            TargetTerminalPhase.ROLLED_BACK,
        )
        boundary_target, boundary = self.boundary_target(MAX_SEQUENCE - 4)
        before = read_overlay(self.overlay_path)[0]

        with self.assertRaisesRegex(
            WholeConfigTargetError,
            "insufficient lifecycle headroom",
        ):
            boundary_target.prepare(
                2,
                digest({"foo": 2}),
                {"foo": 2},
            )
        self.assertEqual(boundary.writes, 0)
        self.assertEqual(
            read_overlay(self.overlay_path)[0],
            before,
        )

    def test_prepare_at_max_minus_five_reaches_post_apply_unknown_and_seals(self):
        """The complete prepared lifecycle safely consumes its five revisions."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        first = self.prepare({"foo": 1})
        self.target.seal(first)
        _boundary_target, boundary = self.boundary_target(MAX_SEQUENCE - 5)
        target = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            boundary,
            ALLOWED_KEYS,
        )
        projection = {"foo": 2}
        prepared = target.prepare(
            2,
            digest(projection),
            projection,
        )
        self.assertEqual(boundary.entry.revision, MAX_SEQUENCE - 4)

        mutating = MutatingJournal(
            boundary,
            {
                2: lambda: exact_overlay(
                    self.overlay_path,
                    {"foo": 99},
                )
            },
        )
        target = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            mutating,
            ALLOWED_KEYS,
        )
        unknown = target.apply(prepared)
        self.assertEqual(
            unknown.phase,
            TargetOperationPhase.UNKNOWN,
        )
        self.assertEqual(boundary.entry.revision, MAX_SEQUENCE - 1)
        sealed = target.invalidate(prepared.key)
        self.assertEqual(
            sealed.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )
        self.assertEqual(boundary.entry.revision, MAX_SEQUENCE)

    def test_seal_reserves_unknown_revision_before_terminal_cas(self):
        """Seal performs no CAS unless a later UNKNOWN downgrade remains possible."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare({"foo": 2})
        boundary_target, boundary = self.boundary_target(MAX_SEQUENCE - 1)

        with self.assertRaisesRegex(
            WholeConfigTargetError,
            "insufficient lifecycle headroom",
        ):
            boundary_target.seal(prepared)
        self.assertEqual(boundary.writes, 0)
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 1},
        )

        _boundary_target, boundary = self.boundary_target(MAX_SEQUENCE - 2)
        mutating = MutatingJournal(
            boundary,
            {
                1: lambda: exact_overlay(
                    self.overlay_path,
                    {"foo": 55},
                )
            },
        )
        target = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            mutating,
            ALLOWED_KEYS,
        )
        sealed = target.seal(prepared)
        self.assertEqual(
            sealed.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )
        self.assertEqual(boundary.entry.revision, MAX_SEQUENCE)
        self.assertEqual(boundary.writes, 2)

    def test_invalidate_reserves_unknown_revision_before_terminal_cas(self):
        """Invalidate performs no CAS unless post-CAS recheck can be recorded."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare({"foo": 2})
        boundary_target, boundary = self.boundary_target(MAX_SEQUENCE - 1)

        with self.assertRaisesRegex(
            WholeConfigTargetError,
            "insufficient lifecycle headroom",
        ):
            boundary_target.invalidate(prepared.key)
        self.assertEqual(boundary.writes, 0)
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 1},
        )

        _boundary_target, boundary = self.boundary_target(MAX_SEQUENCE - 2)
        mutating = MutatingJournal(
            boundary,
            {
                1: lambda: exact_overlay(
                    self.overlay_path,
                    {"foo": 66},
                )
            },
        )
        target = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            mutating,
            ALLOWED_KEYS,
        )
        sealed = target.invalidate(prepared.key)
        self.assertEqual(
            sealed.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )
        self.assertEqual(boundary.entry.revision, MAX_SEQUENCE)
        self.assertEqual(boundary.writes, 2)

    def test_unknown_recovery_has_reserved_revision_headroom(self):
        """Pre-image mismatch can still durably seal UNKNOWN at the boundary."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare({"foo": 2})
        exact_overlay(
            self.overlay_path,
            {"foo": 9},
        )
        boundary_target, boundary = self.boundary_target(MAX_SEQUENCE - 4)

        unknown = boundary_target.apply(prepared)
        self.assertEqual(
            unknown.phase,
            TargetOperationPhase.UNKNOWN,
        )
        sealed = boundary_target.invalidate(prepared.key)
        self.assertEqual(
            sealed.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )
        self.assertEqual(
            boundary.entry.revision,
            MAX_SEQUENCE - 1,
        )
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 9},
        )

    def test_outer_lock_orders_apply_and_invalidate_across_instances(self):
        """Invalidate cannot classify visibility while a commit is in flight."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        writer = BlockingWriteTarget(
            self.overlay_path,
            self.journal,
            ALLOWED_KEYS,
        )
        observer = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            FileOpaqueTargetJournal(self.journal_path),
            ALLOWED_KEYS,
        )
        prepared = self.prepare(
            {"foo": 2},
            target=writer,
        )
        results = {}

        apply_thread = threading.Thread(
            target=lambda: results.setdefault(
                "apply",
                writer.apply(prepared),
            )
        )
        invalidate_thread = threading.Thread(
            target=lambda: results.setdefault(
                "invalidate",
                observer.invalidate(prepared.key),
            )
        )
        apply_thread.start()
        self.assertTrue(writer.entered.wait(2))
        invalidate_thread.start()
        invalidate_thread.join(0.1)
        self.assertTrue(invalidate_thread.is_alive())
        writer.release.set()
        apply_thread.join(2)
        invalidate_thread.join(2)

        self.assertFalse(apply_thread.is_alive())
        self.assertFalse(invalidate_thread.is_alive())
        self.assertEqual(
            results["apply"].phase,
            TargetOperationPhase.APPLIED,
        )
        self.assertEqual(
            results["invalidate"].terminal_phase,
            TargetTerminalPhase.APPLIED,
        )

    def test_replay_stale_fence_and_unsettled_replacement_fail_closed(self):
        """Prepare binds idempotency to exact monotonically fenced journal keys."""
        first = self.prepare(
            {"foo": 1},
            fence=3,
        )
        for fence, config in (
            (3, {"foo": 1}),
            (3, {"foo": 2}),
            (2, {"foo": 3}),
            (4, {"foo": 4}),
        ):
            with self.assertRaises(WholeConfigTargetConflict):
                self.prepare(
                    config,
                    fence=fence,
                )

        self.target.seal(first)
        second = self.prepare(
            {"foo": 5},
            fence=4,
        )
        self.assertEqual(
            second.key,
            TargetOperationKey(4, digest({"foo": 5})),
        )
        with self.assertRaises(WholeConfigTargetConflict):
            self.target.invalidate(first.key)

    def test_abort_is_durable_and_cannot_regress_current_fence(self):
        """An unprepared reservation is tombstoned with no overlay mutation."""
        key = TargetOperationKey(
            5,
            digest({"foo": 1}),
        )
        aborted = self.target.abort(
            key.fence,
            key.projection_digest,
        )
        self.assertEqual(
            aborted.phase,
            TargetOperationPhase.SEALED,
        )
        self.assertEqual(
            aborted.terminal_phase,
            TargetTerminalPhase.ROLLED_BACK,
        )
        self.assertEqual(
            self.target.lookup(
                key.fence,
                key.projection_digest,
            ),
            aborted,
        )
        with self.assertRaises(WholeConfigTargetConflict):
            self.target.abort(
                4,
                digest({"foo": 2}),
            )

    def test_allowlist_and_protected_filename_checks_precede_target_effects(self):
        """Only explicit owned keys can enter the journal or overlay."""
        before = self.journal.load()
        for hostile in (
            {"not_owned": "secret-value"},
            {"apps": {"module": "predbat"}},
        ):
            with self.assertRaises(ValueError):
                self.prepare(hostile)
            self.assertEqual(
                self.journal.load(),
                before,
            )
            self.assertFalse(os.path.exists(self.overlay_path))

        for protected in (
            "apps.yaml",
            "APPS.YML",
            "secrets.yaml",
            "Secrets.yml",
        ):
            path = os.path.join(
                self.temporary.name,
                protected,
            )
            with self.assertRaises(ValueError):
                GeneratedOverlayWholeConfigTarget(
                    path,
                    self.journal,
                    ALLOWED_KEYS,
                )
            self.assertFalse(os.path.exists(path))

    def test_apps_and_secrets_markers_are_never_read_or_written(self):
        """A complete target lifecycle leaves live config marker files exact."""
        markers = {}
        for name in (
            "apps.yaml",
            "secrets.yaml",
        ):
            path = os.path.join(
                self.temporary.name,
                name,
            )
            payload = "{}-marker-secret".format(name).encode("ascii")
            with open(path, "wb") as marker:
                marker.write(payload)
            markers[path] = payload

        prepared = self.prepare({"foo": 2})
        applied = self.target.apply(prepared)
        sealed = self.target.seal(applied)
        rolled_back = self.target.rollback(sealed)
        self.target.seal(rolled_back)

        for path, payload in markers.items():
            with open(path, "rb") as marker:
                self.assertEqual(marker.read(), payload)

    def test_overlay_requires_canonical_private_regular_allowlisted_json(self):
        """Noncanonical, unsafe, and non-owned existing overlays fail closed."""
        hostile_payloads = (
            b'{"foo":1, "bar":2}',
            b'{"foo":1,"foo":2}',
            b'{"not_owned":1}',
            b'{"foo":NaN}',
            b"[]",
        )
        for payload in hostile_payloads:
            with self.subTest(payload=payload):
                with open(self.overlay_path, "wb") as overlay:
                    overlay.write(payload)
                os.chmod(self.overlay_path, 0o600)
                with self.assertRaises(WholeConfigTargetCorrupt):
                    self.prepare({"foo": 2})
                os.unlink(self.overlay_path)

        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        os.chmod(self.overlay_path, 0o644)
        with self.assertRaises(WholeConfigTargetCorrupt):
            self.prepare({"foo": 2})
        os.unlink(self.overlay_path)

        real = os.path.join(
            self.temporary.name,
            "real.json",
        )
        exact_overlay(real, {"foo": 1})
        os.symlink(real, self.overlay_path)
        with self.assertRaises(WholeConfigTargetError):
            self.prepare({"foo": 2})

    def test_target_binding_and_allowlist_binding_reject_mispaired_restart(self):
        """An opaque journal cannot be replayed against another target identity."""
        prepared = self.prepare({"foo": 1})
        self.assertIsNotNone(prepared)

        other_path = os.path.join(
            self.temporary.name,
            "other-generated-overlay.json",
        )
        wrong_path = GeneratedOverlayWholeConfigTarget(
            other_path,
            FileOpaqueTargetJournal(self.journal_path),
            ALLOWED_KEYS,
        )
        with self.assertRaises(WholeConfigTargetCorrupt):
            wrong_path.lookup(
                prepared.key.fence,
                prepared.key.projection_digest,
            )

        wrong_allowlist = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            FileOpaqueTargetJournal(self.journal_path),
            ("foo",),
        )
        with self.assertRaises(WholeConfigTargetCorrupt):
            wrong_allowlist.lookup(
                prepared.key.fence,
                prepared.key.projection_digest,
            )

    def test_journal_decode_rejects_absent_nonempty_overlay_state(self):
        """File absence can only encode the exact canonical empty config."""
        prepared = self.prepare({"foo": 1})
        entry = self.journal.load()
        document = json.loads(entry.payload.decode("ascii"))
        document["preimage"] = {
            "present": False,
            "config": {"foo": 99},
        }
        hostile = OpaqueTargetJournalEntry(
            entry.revision,
            entry.key,
            json.dumps(
                document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii"),
        )
        target = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            BoundaryJournal(hostile),
            ALLOWED_KEYS,
        )

        with self.assertRaises(WholeConfigTargetCorrupt):
            target.lookup(
                prepared.key.fence,
                prepared.key.projection_digest,
            )

    def test_hostile_snapshot_identity_cannot_apply_or_rollback(self):
        """Opaque handle/digest forgery never reaches an overlay write."""
        exact_overlay(
            self.overlay_path,
            {"foo": 1},
        )
        prepared = self.prepare({"foo": 2})
        hostile = replace(
            prepared,
            handle="lattice-target-forged",
        )
        with self.assertRaises(WholeConfigTargetConflict):
            self.target.apply(hostile)
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 1},
        )

        sealed = self.target.seal(prepared)
        with self.assertRaises(WholeConfigTargetConflict):
            self.target.rollback(
                replace(
                    sealed,
                    candidate_digest="0" * 64,
                    changed=True,
                )
            )
        self.assertEqual(
            read_overlay(self.overlay_path)[1],
            {"foo": 1},
        )

    def test_fault_messages_and_repr_do_not_expose_projection_values(self):
        """Diagnostics retain only value-free failure classes and counts."""
        secret_value = "raw-target-secret-value"
        target = FailingWriteTarget(
            self.overlay_path,
            self.journal,
            ALLOWED_KEYS,
        )
        prepared = self.prepare(
            {"foo": secret_value},
            target=target,
        )
        target.fail_before_write = True
        with self.assertRaises(OSError) as raised:
            target.apply(prepared)
        self.assertNotIn(secret_value, repr(prepared))
        self.assertNotIn(secret_value, repr(self.journal.load()))
        self.assertNotIn(secret_value, repr(target))
        # The injected test exception is deliberately hostile; production
        # materializer wrapping exposes only its exception class.
        self.assertNotIn(secret_value, type(raised.exception).__name__)

    def test_lock_and_directory_security_fail_closed(self):
        """Unsafe lock metadata and a symlinked target directory are rejected."""
        lock_path = self.overlay_path + ".lock"
        with open(lock_path, "wb") as lock:
            lock.write(b"")
        os.chmod(lock_path, 0o644)
        with self.assertRaises(WholeConfigTargetError):
            self.prepare({"foo": 1})

        real_directory = os.path.join(
            self.temporary.name,
            "real",
        )
        linked_directory = os.path.join(
            self.temporary.name,
            "linked",
        )
        os.mkdir(real_directory, 0o700)
        os.symlink(real_directory, linked_directory)
        with self.assertRaises(WholeConfigTargetError):
            GeneratedOverlayWholeConfigTarget(
                os.path.join(linked_directory, "overlay.json"),
                FileOpaqueTargetJournal(os.path.join(real_directory, "other.journal")),
                ALLOWED_KEYS,
            )

    def test_bool_fence_wrong_digest_and_mutable_allowlist_fail_before_journal(self):
        """Type-exact boundary validation precedes durable side effects."""
        before = self.journal.load()
        with self.assertRaises(ValueError):
            self.target.prepare(
                True,
                digest({"foo": 1}),
                {"foo": 1},
            )
        with self.assertRaises(ValueError):
            self.target.prepare(
                1,
                "0" * 64,
                {"foo": 1},
            )
        with self.assertRaises(ValueError):
            GeneratedOverlayWholeConfigTarget(
                self.overlay_path,
                self.journal,
                ["foo"],
            )
        self.assertEqual(
            self.journal.load(),
            before,
        )

    def test_failed_exact_readback_remains_recoverably_ambiguous(self):
        """Readback mismatch cannot be acknowledged as an applied operation."""
        prepared = self.prepare({"foo": 2})
        directory_fd = self.target._open_directory()
        try:
            preimage = self.target._read_overlay(directory_fd)
        finally:
            os.close(directory_fd)
        with mock.patch.object(
            self.target,
            "_read_overlay",
            side_effect=(
                preimage,
                WholeConfigTargetCorrupt("injected-readback"),
            ),
        ):
            with self.assertRaises(WholeConfigTargetCorrupt):
                self.target.apply(prepared)
        restarted = GeneratedOverlayWholeConfigTarget(
            self.overlay_path,
            FileOpaqueTargetJournal(self.journal_path),
            ALLOWED_KEYS,
        )
        recovered = restarted.invalidate(prepared.key)
        self.assertEqual(
            recovered.terminal_phase,
            TargetTerminalPhase.APPLIED,
        )


if __name__ == "__main__":
    unittest.main()
