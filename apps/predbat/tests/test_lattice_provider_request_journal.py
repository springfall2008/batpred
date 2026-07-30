"""Tests for the detached Lattice provider request journal."""

# cspell:ignore getuid hardlinked lpr overclaim predbat readback xiaozhi

import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_provider_fallback import (
    ProviderExecutionResult,
    ProviderExecutionState,
    ProviderPath,
    ProviderPathIdentity,
    ProviderPathKind,
)
from lattice_provider_request_journal import (
    DurableProviderRequestExecutor,
    FileProviderRequestJournal,
    ProviderJournalRequest,
    ProviderOutcomeCondition,
    ProviderOwnershipAltitude,
    ProviderOwnershipBinding,
    ProviderRequestEvidence,
    ProviderRequestJournalError,
    ProviderRequestPhase,
    ProviderRequestRecord,
    ProviderWriterBinding,
)


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


class MemoryJournal:
    """Compare-and-store journal exposing durable phase transitions."""

    def __init__(self, record=None):
        self.records = {}
        if record is not None:
            self.records[record.provider_request_id] = record
        self.record = record
        self.commits = []
        self.fail_phases = set()

    def load(self, provider_request_id):
        return self.records.get(provider_request_id)

    def commit(self, expected_revision, record):
        if record.phase in self.fail_phases:
            raise OSError("injected durable write failure")
        current = self.records.get(record.provider_request_id)
        actual = None if current is None else current.revision
        if actual != expected_revision:
            raise ProviderRequestJournalError("collision")
        self.records[record.provider_request_id] = record
        self.record = record
        self.commits.append(record)
        return record


class Exploding:
    """Object that fails on any disabled-path inspection."""

    def __getattribute__(self, name):
        raise AssertionError("disabled value inspected: {}".format(name))


class WrongReadbackJournal(MemoryJournal):
    """Store that exposes a successful write without exact readback."""

    def commit(self, expected_revision, record):
        super().commit(expected_revision, record)
        if record.phase is ProviderRequestPhase.APPLIED:
            return prepared_record(
                request(provider_request_id=record.provider_request_id),
                ProviderRequestPhase.IN_FLIGHT,
            )
        return record


def ownership(request_digest=DIGEST_A, expires=9999999999999):
    """Build canonical immutable provider ownership."""
    return ProviderOwnershipBinding(
        canonical_scope="site/home",
        document_version="v1",
        capability_ref="cap/battery-control",
        altitude=ProviderOwnershipAltitude.LEAVES,
        owned_nodes=("battery/inverter", "battery/soc"),
        writers=(
            ProviderWriterBinding("writer/gateway", DIGEST_A),
            ProviderWriterBinding("writer/saas", DIGEST_B),
        ),
        authenticated_origin="origin/saas",
        lease_id="lease/123",
        lease_fence=7,
        lease_expires_at_ms=expires,
        request_digest=request_digest,
    )


def request(
    body=None,
    request_digest=DIGEST_A,
    expires=9999999999999,
    provider_request_id="lpr-schedule-001",
):
    """Build one exact journal request with an opaque body."""
    return ProviderJournalRequest(
        path=ProviderPathIdentity(
            "battery-main",
            "xiaozhi",
            "gateway-local",
        ),
        operation="schedule/apply",
        ownership=ownership(
            request_digest=request_digest,
            expires=expires,
        ),
        provider_request_id=provider_request_id,
        provider_request=body if body is not None else {"secret": "do-not-store"},
    )


def prepared_record(journal_request, phase=ProviderRequestPhase.PREPARED):
    """Build an existing durable record for restart tests."""
    prepared = ProviderRequestRecord(
        revision=1,
        phase=ProviderRequestPhase.PREPARED,
        provider_request_id=journal_request.provider_request_id,
        request_fingerprint=journal_request.fingerprint,
        metadata_json=journal_request.metadata_json,
    )
    if phase is ProviderRequestPhase.PREPARED:
        return prepared
    if phase is ProviderRequestPhase.IN_FLIGHT:
        return prepared.advanced(phase)
    if phase is ProviderRequestPhase.UNKNOWN:
        return prepared.advanced(phase, "restart", "restart")
    raise AssertionError("prepared_record cannot create {}".format(phase.value))


def terminal_record(journal_request, phase):
    """Build a legal durable terminal record."""
    in_flight = prepared_record(journal_request).advanced(
        ProviderRequestPhase.IN_FLIGHT,
    )
    return in_flight.advanced(
        phase,
        "provider-evidence",
        "terminal",
        no_effect_proof=phase is ProviderRequestPhase.NOT_APPLIED,
    )


class TestStrictModels(unittest.TestCase):
    """Validate exact canonical ownership and record invariants."""

    def test_request_identity_is_exact_stable_and_body_free(self):
        first = request({"secret": "one"})
        second = request({"secret": "two"})
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.provider_request_id, second.provider_request_id)
        self.assertNotIn("secret", repr(first))
        self.assertNotIn("one", repr(first))

        changed = request(request_digest=DIGEST_B)
        self.assertNotEqual(first.fingerprint, changed.fingerprint)
        self.assertEqual(first.provider_request_id, changed.provider_request_id)
        metadata = json.loads(first.metadata_json)
        self.assertEqual(metadata["operation"], "schedule/apply")
        self.assertEqual(
            metadata["path"],
            {
                "path_id": "gateway-local",
                "provider_id": "xiaozhi",
                "target_id": "battery-main",
            },
        )
        self.assertEqual(
            metadata["provider_request_id"],
            first.provider_request_id,
        )
        self.assertEqual(
            metadata["ownership"],
            ownership().canonical_data(),
        )

    def test_ownership_requires_sorted_exact_sets_and_valid_lease(self):
        invalid = (
            lambda: ProviderOwnershipBinding(
                **dict(
                    ownership().__dict__,
                    owned_nodes=("battery/soc", "battery/inverter"),
                )
            ),
            lambda: ProviderOwnershipBinding(
                **dict(
                    ownership().__dict__,
                    writers=tuple(reversed(ownership().writers)),
                )
            ),
            lambda: ProviderOwnershipBinding(**dict(ownership().__dict__, lease_fence=True)),
            lambda: ProviderOwnershipBinding(**dict(ownership().__dict__, request_digest="not-a-digest")),
        )
        for factory in invalid:
            with self.subTest(factory=factory):
                with self.assertRaises(ProviderRequestJournalError):
                    factory()

    def test_not_applied_record_requires_no_effect_proof(self):
        journal_request = request()
        with self.assertRaises(ProviderRequestJournalError):
            ProviderRequestRecord(
                revision=2,
                phase=ProviderRequestPhase.NOT_APPLIED,
                provider_request_id=journal_request.provider_request_id,
                request_fingerprint=journal_request.fingerprint,
                metadata_json=journal_request.metadata_json,
            )

    def test_failure_evidence_cannot_overclaim_a_result(self):
        with self.assertRaises(ProviderRequestJournalError):
            ProviderRequestEvidence(
                "lpr-request",
                ProviderOutcomeCondition.TIMEOUT,
                result=ProviderExecutionResult.not_applied(True),
            )


class TestDurableExecutor(unittest.IsolatedAsyncioTestCase):
    """Exercise prepare, write, recovery, and fail-closed fallback behavior."""

    async def test_disabled_returns_before_request_clock_storage_or_executor(self):
        wrapper = DurableProviderRequestExecutor(
            Exploding(),
            Exploding(),
            reconciler=Exploding(),
            enabled=False,
            clock_ms=Exploding(),
        )
        result = await wrapper(Exploding())
        self.assertIs(result.state, ProviderExecutionState.UNKNOWN)
        self.assertFalse(result.fallback_safe)

    async def test_prepared_and_in_flight_are_durable_before_first_write(self):
        journal = MemoryJournal()
        observed = []

        def execute(wire_request):
            observed.append(
                (
                    wire_request.provider_request_id,
                    journal.record.phase,
                )
            )
            return ProviderRequestEvidence.observed(
                wire_request.provider_request_id,
                ProviderExecutionResult.applied(2400, "secret response"),
            )

        journal_request = request()
        path = ProviderPath(
            target_id="battery-main",
            provider_id="xiaozhi",
            path_id="gateway-local",
            kind=ProviderPathKind.GATEWAY_LOCAL,
            executor=DurableProviderRequestExecutor(
                execute,
                journal,
                enabled=True,
                clock_ms=lambda: 1000,
            ),
        )
        result = await path.executor(journal_request)

        self.assertIs(result.state, ProviderExecutionState.APPLIED)
        self.assertEqual(result.applied_value, 2400)
        self.assertEqual(
            [item.phase for item in journal.commits],
            [
                ProviderRequestPhase.PREPARED,
                ProviderRequestPhase.IN_FLIGHT,
                ProviderRequestPhase.APPLIED,
            ],
        )
        self.assertEqual(
            observed,
            [
                (
                    journal_request.provider_request_id,
                    ProviderRequestPhase.IN_FLIGHT,
                )
            ],
        )

    async def test_terminal_replay_never_resends(self):
        journal = MemoryJournal()
        calls = []

        def execute(wire_request):
            calls.append(wire_request.provider_request_id)
            return ProviderRequestEvidence.observed(
                wire_request.provider_request_id,
                ProviderExecutionResult.applied(),
            )

        wrapper = DurableProviderRequestExecutor(
            execute,
            journal,
            enabled=True,
            clock_ms=lambda: 1000,
        )
        journal_request = request()
        first = await wrapper(journal_request)
        second = await wrapper(journal_request)
        self.assertIs(first.state, ProviderExecutionState.APPLIED)
        self.assertIs(second.state, ProviderExecutionState.APPLIED)
        self.assertEqual(calls, [journal_request.provider_request_id])

    async def test_not_applied_is_safe_only_with_explicit_no_effect_proof(self):
        for provider_safe, proof, expected_state, fallback_safe in (
            (True, False, ProviderExecutionState.UNKNOWN, False),
            (False, True, ProviderExecutionState.UNKNOWN, False),
            (True, True, ProviderExecutionState.NOT_APPLIED, True),
        ):
            with self.subTest(provider_safe=provider_safe, proof=proof):
                journal = MemoryJournal()

                def execute(wire_request):
                    return ProviderRequestEvidence.observed(
                        wire_request.provider_request_id,
                        ProviderExecutionResult.not_applied(provider_safe),
                        no_effect_proof=proof,
                    )

                result = await DurableProviderRequestExecutor(
                    execute,
                    journal,
                    enabled=True,
                    clock_ms=lambda: 1000,
                )(request())
                self.assertIs(result.state, expected_state)
                self.assertIs(result.fallback_safe, fallback_safe)
                self.assertIs(
                    journal.record.phase,
                    (ProviderRequestPhase.NOT_APPLIED if provider_safe and proof else ProviderRequestPhase.UNKNOWN),
                )

    async def test_unavailable_timeout_conflict_and_unknown_never_fallback(self):
        factories = (
            ProviderRequestEvidence.unavailable,
            ProviderRequestEvidence.timeout,
            ProviderRequestEvidence.conflict,
            lambda provider_request_id: ProviderRequestEvidence.observed(
                provider_request_id,
                ProviderExecutionResult.unknown("ambiguous"),
            ),
        )
        for factory in factories:
            with self.subTest(factory=factory):
                journal = MemoryJournal()

                def execute(wire_request):
                    return factory(wire_request.provider_request_id)

                result = await DurableProviderRequestExecutor(
                    execute,
                    journal,
                    enabled=True,
                    clock_ms=lambda: 1000,
                )(request())
                self.assertIs(result.state, ProviderExecutionState.UNKNOWN)
                self.assertFalse(result.fallback_safe)
                self.assertIs(
                    journal.record.phase,
                    ProviderRequestPhase.UNKNOWN,
                )

    async def test_restart_after_in_flight_reconciles_same_id_without_resend(self):
        journal_request = request()
        journal = MemoryJournal(
            prepared_record(journal_request, ProviderRequestPhase.IN_FLIGHT),
        )
        executed = []
        reconciled = []

        def execute(wire_request):
            executed.append(wire_request)
            raise AssertionError("in-flight request was resent")

        def reconcile(reconcile_request):
            reconciled.append(reconcile_request)
            return ProviderRequestEvidence.observed(
                reconcile_request.provider_request_id,
                ProviderExecutionResult.applied(),
            )

        result = await DurableProviderRequestExecutor(
            execute,
            journal,
            reconciler=reconcile,
            enabled=True,
            clock_ms=lambda: 1000,
        )(journal_request)
        self.assertIs(result.state, ProviderExecutionState.APPLIED)
        self.assertEqual(executed, [])
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(
            reconciled[0].provider_request_id,
            journal_request.provider_request_id,
        )

    async def test_expired_in_flight_still_reconciles_same_id(self):
        journal_request = request(expires=1500)
        journal = MemoryJournal(
            prepared_record(journal_request, ProviderRequestPhase.IN_FLIGHT),
        )
        reconciled = []

        def reconcile(reconcile_request):
            reconciled.append(reconcile_request.provider_request_id)
            return ProviderRequestEvidence.observed(
                reconcile_request.provider_request_id,
                ProviderExecutionResult.applied(),
            )

        result = await DurableProviderRequestExecutor(
            lambda unused: self.fail("expired in-flight request was resent"),
            journal,
            reconciler=reconcile,
            enabled=True,
            clock_ms=Exploding(),
        )(journal_request)
        self.assertIs(result.state, ProviderExecutionState.APPLIED)
        self.assertEqual(
            reconciled,
            [journal_request.provider_request_id],
        )

    async def test_expired_terminal_replays_without_clock_or_executor(self):
        journal_request = request(expires=1500)
        terminal = terminal_record(
            journal_request,
            ProviderRequestPhase.APPLIED,
        )
        journal = MemoryJournal(terminal)
        result = await DurableProviderRequestExecutor(
            Exploding(),
            journal,
            enabled=True,
            clock_ms=Exploding(),
        )(journal_request)
        self.assertIs(result.state, ProviderExecutionState.APPLIED)
        self.assertEqual(journal.record, terminal)

    async def test_unknown_restart_also_reconciles_and_never_falls_back(self):
        journal_request = request()
        journal = MemoryJournal(
            prepared_record(journal_request, ProviderRequestPhase.UNKNOWN),
        )
        reconciled = []

        def reconcile(reconcile_request):
            reconciled.append(reconcile_request.provider_request_id)
            return ProviderRequestEvidence.timeout(
                reconcile_request.provider_request_id,
            )

        result = await DurableProviderRequestExecutor(
            lambda unused: self.fail("request was resent"),
            journal,
            reconciler=reconcile,
            enabled=True,
            clock_ms=lambda: 1000,
        )(journal_request)
        self.assertIs(result.state, ProviderExecutionState.UNKNOWN)
        self.assertFalse(result.fallback_safe)
        self.assertEqual(
            reconciled,
            [journal_request.provider_request_id],
        )

    async def test_base_exception_and_cancellation_leave_in_flight_recoverable(self):
        for failure in (RuntimeError("boom"), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                journal = MemoryJournal()

                def execute(unused):
                    raise failure

                wrapper = DurableProviderRequestExecutor(
                    execute,
                    journal,
                    enabled=True,
                    clock_ms=lambda: 1000,
                )
                with self.assertRaises(type(failure)):
                    await wrapper(request())
                self.assertIs(
                    journal.record.phase,
                    ProviderRequestPhase.IN_FLIGHT,
                )

    async def test_terminal_persistence_failure_returns_unknown(self):
        journal = MemoryJournal()
        journal.fail_phases.add(ProviderRequestPhase.APPLIED)

        def execute(wire_request):
            return ProviderRequestEvidence.observed(
                wire_request.provider_request_id,
                ProviderExecutionResult.applied(),
            )

        result = await DurableProviderRequestExecutor(
            execute,
            journal,
            enabled=True,
            clock_ms=lambda: 1000,
        )(request())
        self.assertIs(result.state, ProviderExecutionState.UNKNOWN)
        self.assertFalse(result.fallback_safe)
        self.assertIs(journal.record.phase, ProviderRequestPhase.IN_FLIGHT)

    async def test_wrong_terminal_readback_returns_unknown(self):
        journal = WrongReadbackJournal()

        async def execute(wire_request):
            await asyncio.sleep(0)
            return ProviderRequestEvidence.observed(
                wire_request.provider_request_id,
                ProviderExecutionResult.applied(),
            )

        result = await DurableProviderRequestExecutor(
            execute,
            journal,
            enabled=True,
            clock_ms=lambda: 1000,
        )(request())
        self.assertIs(result.state, ProviderExecutionState.UNKNOWN)
        self.assertFalse(result.fallback_safe)

    async def test_collision_quarantines_without_executor_or_fallback(self):
        original = request(request_digest=DIGEST_A)
        colliding = request(request_digest=DIGEST_B)
        journal = MemoryJournal(prepared_record(original))
        calls = []

        result = await DurableProviderRequestExecutor(
            lambda wire_request: calls.append(wire_request),
            journal,
            enabled=True,
            clock_ms=lambda: 1000,
        )(colliding)
        self.assertIs(result.state, ProviderExecutionState.UNKNOWN)
        self.assertFalse(result.fallback_safe)
        self.assertEqual(calls, [])
        self.assertIs(journal.record.phase, ProviderRequestPhase.UNKNOWN)

    async def test_terminal_collision_preserves_durable_evidence(self):
        for phase in (
            ProviderRequestPhase.APPLIED,
            ProviderRequestPhase.NOT_APPLIED,
        ):
            with self.subTest(phase=phase):
                original = request(request_digest=DIGEST_A)
                colliding = request(request_digest=DIGEST_B)
                terminal = terminal_record(original, phase)
                journal = MemoryJournal(terminal)
                result = await DurableProviderRequestExecutor(
                    Exploding(),
                    journal,
                    enabled=True,
                    clock_ms=Exploding(),
                )(colliding)
                self.assertIs(result.state, ProviderExecutionState.UNKNOWN)
                self.assertFalse(result.fallback_safe)
                self.assertEqual(journal.record, terminal)

    async def test_expired_new_request_stops_before_prepare_or_executor(self):
        journal = MemoryJournal()
        result = await DurableProviderRequestExecutor(
            Exploding(),
            journal,
            enabled=True,
            clock_ms=lambda: 2000,
        )(request(body=Exploding(), expires=1500))
        self.assertIs(result.state, ProviderExecutionState.UNKNOWN)
        self.assertIn("expired", result.detail)
        self.assertEqual(journal.commits, [])


class TestFileJournal(unittest.TestCase):
    """Verify the mechanically separate file journal and opaque persistence."""

    def test_mode_atomic_readback_and_no_request_or_response_body(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "provider-request-journal.json")
            journal = FileProviderRequestJournal(path)
            journal_request = request({"secret": "provider-body"})
            record = prepared_record(journal_request)
            readback = journal.commit(None, record)
            self.assertEqual(readback, record)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(os.stat(path).st_uid, os.getuid())
            self.assertEqual(os.stat(path).st_nlink, 1)
            lock_status = os.stat("{}.lock".format(path))
            self.assertEqual(stat.S_IMODE(lock_status.st_mode), 0o600)
            self.assertEqual(lock_status.st_uid, os.getuid())
            self.assertEqual(lock_status.st_nlink, 1)
            with open(path, "r", encoding="utf-8") as handle:
                payload = handle.read()
            self.assertNotIn("provider-body", payload)
            self.assertNotIn("secret response", payload)
            self.assertNotIn("secret", payload)
            self.assertEqual(journal.load(record.provider_request_id), record)

            second_request = request(
                request_digest=DIGEST_B,
                provider_request_id="lpr-schedule-002",
            )
            second_record = prepared_record(second_request)
            journal.commit(None, second_record)
            self.assertEqual(journal.load(record.provider_request_id), record)
            self.assertEqual(
                journal.load(second_record.provider_request_id),
                second_record,
            )

    def test_integrity_tamper_and_compare_collision_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "provider-request-journal.json")
            journal = FileProviderRequestJournal(path)
            journal_request = request()
            record = prepared_record(journal_request)
            journal.commit(None, record)
            with self.assertRaises(ProviderRequestJournalError):
                journal.commit(None, record)

            with open(path, "r", encoding="utf-8") as handle:
                envelope = json.load(handle)
            envelope["data"]["records"][record.provider_request_id]["revision"] = 99
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(envelope, handle)
            os.chmod(path, 0o600)
            with self.assertRaises(ProviderRequestJournalError):
                journal.load(record.provider_request_id)

    def test_commit_enforces_new_record_and_phase_transition_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "provider-request-journal.json")
            journal = FileProviderRequestJournal(path)
            journal_request = request()
            prepared = prepared_record(journal_request)
            applied = terminal_record(
                journal_request,
                ProviderRequestPhase.APPLIED,
            )

            with self.assertRaises(ProviderRequestJournalError):
                journal.commit(None, applied)
            journal.commit(None, prepared)
            changed_request = request(request_digest=DIGEST_B)
            with self.assertRaises(ProviderRequestJournalError):
                journal.commit(
                    prepared.revision,
                    ProviderRequestRecord(
                        revision=prepared.revision + 1,
                        phase=ProviderRequestPhase.IN_FLIGHT,
                        provider_request_id=changed_request.provider_request_id,
                        request_fingerprint=changed_request.fingerprint,
                        metadata_json=changed_request.metadata_json,
                    ),
                )
            with self.assertRaises(ProviderRequestJournalError):
                journal.commit(
                    prepared.revision,
                    ProviderRequestRecord(
                        revision=prepared.revision + 1,
                        phase=ProviderRequestPhase.APPLIED,
                        provider_request_id=prepared.provider_request_id,
                        request_fingerprint=prepared.request_fingerprint,
                        metadata_json=prepared.metadata_json,
                    ),
                )

            in_flight = prepared.advanced(ProviderRequestPhase.IN_FLIGHT)
            journal.commit(prepared.revision, in_flight)
            journal.commit(in_flight.revision, applied)
            with self.assertRaises(ProviderRequestJournalError):
                journal.commit(
                    applied.revision,
                    ProviderRequestRecord(
                        revision=applied.revision + 1,
                        phase=ProviderRequestPhase.UNKNOWN,
                        provider_request_id=applied.provider_request_id,
                        request_fingerprint=applied.request_fingerprint,
                        metadata_json=applied.metadata_json,
                    ),
                )
            self.assertEqual(
                journal.load(applied.provider_request_id),
                applied,
            )

    def test_hardlinked_journal_and_lock_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "provider-request-journal.json")
            journal = FileProviderRequestJournal(path)
            journal_request = request()
            prepared = prepared_record(journal_request)
            journal.commit(None, prepared)

            journal_alias = "{}.alias".format(path)
            os.link(path, journal_alias)
            with self.assertRaises(ProviderRequestJournalError):
                journal.load(prepared.provider_request_id)
            os.unlink(journal_alias)

            lock_path = "{}.lock".format(path)
            lock_alias = "{}.alias".format(lock_path)
            os.link(lock_path, lock_alias)
            with self.assertRaises(ProviderRequestJournalError):
                journal.commit(
                    prepared.revision,
                    prepared.advanced(ProviderRequestPhase.IN_FLIGHT),
                )

    def test_insecure_or_foreign_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            insecure = os.path.join(directory, "insecure")
            os.mkdir(insecure, 0o700)
            os.chmod(insecure, 0o755)
            journal = FileProviderRequestJournal(
                os.path.join(insecure, "journal.json"),
            )
            with self.assertRaises(ProviderRequestJournalError):
                journal.load("lpr-schedule-001")

            secure = os.path.join(directory, "secure")
            os.mkdir(secure, 0o700)
            journal = FileProviderRequestJournal(
                os.path.join(secure, "journal.json"),
            )
            with mock.patch(
                "lattice_provider_request_journal.os.getuid",
                return_value=os.getuid() + 1,
            ):
                with self.assertRaises(ProviderRequestJournalError):
                    journal.load("lpr-schedule-001")

    def test_symlink_and_broad_permissions_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target = os.path.join(directory, "target.json")
            link = os.path.join(directory, "journal.json")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("{}")
            os.chmod(target, 0o600)
            os.symlink(target, link)
            with self.assertRaises(ProviderRequestJournalError):
                FileProviderRequestJournal(link).load("lpr-schedule-001")

            os.unlink(link)
            os.chmod(target, 0o644)
            with self.assertRaises(ProviderRequestJournalError):
                FileProviderRequestJournal(target).load("lpr-schedule-001")


if __name__ == "__main__":
    unittest.main()
