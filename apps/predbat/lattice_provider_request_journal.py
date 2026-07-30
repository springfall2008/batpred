# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice provider request journal
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Detached, default-off durability boundary for provider write requests.

The journal is intended to wrap a future ``ProviderPath.executor``.  It records
an immutable ownership binding and stable provider request ID before the first
write, then fails closed after any ambiguous outcome.  It does not register a
component, perform transport I/O, or alter the current control path.
"""

# cspell:ignore CREAT fchmod fstat fsync getuid inodes lpr predbat RDONLY RDWR
# cspell:ignore readback WRONLY

import fcntl
import hashlib
import inspect
import json
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from enum import Enum

from lattice_provider_fallback import (
    ProviderExecutionResult,
    ProviderExecutionState,
    ProviderPathIdentity,
)


MAX_JOURNAL_BYTES = 131072
MAX_TEXT_BYTES = 256
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:/-]{0,254}[A-Za-z0-9])?$")


class ProviderRequestJournalError(ValueError):
    """Raised when journal input or durable state is unsafe or ambiguous."""


class ProviderRequestPhase(Enum):
    """Durable lifecycle of one provider write."""

    PREPARED = "PREPARED"
    IN_FLIGHT = "IN_FLIGHT"
    UNKNOWN = "UNKNOWN"
    APPLIED = "APPLIED"
    NOT_APPLIED = "NOT_APPLIED"


class ProviderOwnershipAltitude(Enum):
    """Whether ownership binds an aggregate or its exact leaf set."""

    AGGREGATE = "aggregate"
    LEAVES = "leaves"


class ProviderOutcomeCondition(Enum):
    """Transport-independent classification of provider evidence."""

    RESULT = "result"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    CONFLICT = "conflict"


@dataclass(frozen=True, order=True)
class ProviderWriterBinding:
    """Opaque writer identity and its authenticated document digest."""

    writer_id: str
    writer_digest: str

    def __post_init__(self):
        """Reject unbounded identities and non-canonical digests."""
        _require_identifier(self.writer_id, "writer_id")
        _require_digest(self.writer_digest, "writer_digest")


@dataclass(frozen=True)
class ProviderOwnershipBinding:
    """Exact immutable authority presented for one provider request."""

    canonical_scope: str
    document_version: str
    capability_ref: str
    altitude: ProviderOwnershipAltitude
    owned_nodes: tuple
    writers: tuple
    authenticated_origin: str
    lease_id: str
    lease_fence: int
    lease_expires_at_ms: int
    request_digest: str

    def __post_init__(self):
        """Require canonical ordering and complete lease-bound ownership."""
        _require_identifier(self.canonical_scope, "canonical_scope")
        _require_identifier(self.document_version, "document_version")
        _require_identifier(self.capability_ref, "capability_ref")
        if not isinstance(self.altitude, ProviderOwnershipAltitude):
            raise ProviderRequestJournalError(
                "altitude must be ProviderOwnershipAltitude",
            )
        _require_sorted_unique_identifiers(self.owned_nodes, "owned_nodes")
        if type(self.writers) is not tuple or not self.writers:
            raise ProviderRequestJournalError("writers must be a non-empty tuple")
        if any(not isinstance(item, ProviderWriterBinding) for item in self.writers):
            raise ProviderRequestJournalError(
                "writers must contain ProviderWriterBinding values",
            )
        if self.writers != tuple(sorted(set(self.writers))):
            raise ProviderRequestJournalError(
                "writers must be sorted and unique",
            )
        _require_identifier(self.authenticated_origin, "authenticated_origin")
        _require_identifier(self.lease_id, "lease_id")
        _require_positive_int(self.lease_fence, "lease_fence")
        _require_positive_int(self.lease_expires_at_ms, "lease_expires_at_ms")
        _require_digest(self.request_digest, "request_digest")

    def canonical_data(self):
        """Return the secret-free representation covered by the fingerprint."""
        return {
            "altitude": self.altitude.value,
            "authenticated_origin": self.authenticated_origin,
            "canonical_scope": self.canonical_scope,
            "capability_ref": self.capability_ref,
            "document_version": self.document_version,
            "lease_expires_at_ms": self.lease_expires_at_ms,
            "lease_fence": self.lease_fence,
            "lease_id": self.lease_id,
            "owned_nodes": list(self.owned_nodes),
            "request_digest": self.request_digest,
            "writers": [
                {
                    "writer_digest": item.writer_digest,
                    "writer_id": item.writer_id,
                }
                for item in self.writers
            ],
        }


@dataclass(frozen=True, repr=False)
class ProviderJournalRequest:
    """Provider body plus the exact metadata that may authorize its write."""

    path: ProviderPathIdentity
    operation: str
    ownership: ProviderOwnershipBinding
    provider_request_id: str
    provider_request: object

    def __post_init__(self):
        """Validate metadata without inspecting the opaque provider body."""
        if not isinstance(self.path, ProviderPathIdentity):
            raise ProviderRequestJournalError(
                "path must be ProviderPathIdentity",
            )
        _require_identifier(self.operation, "operation")
        if not isinstance(self.ownership, ProviderOwnershipBinding):
            raise ProviderRequestJournalError(
                "ownership must be ProviderOwnershipBinding",
            )
        _require_identifier(self.provider_request_id, "provider_request_id")

    @property
    def fingerprint(self):
        """Return the exact canonical request fingerprint."""
        return hashlib.sha256(self.metadata_json.encode("utf-8")).hexdigest()

    def metadata(self):
        """Return all identity fields and no provider request body."""
        return {
            "operation": self.operation,
            "ownership": self.ownership.canonical_data(),
            "path": {
                "path_id": self.path.path_id,
                "provider_id": self.path.provider_id,
                "target_id": self.path.target_id,
            },
            "provider_request_id": self.provider_request_id,
        }

    @property
    def metadata_json(self):
        """Return deeply immutable canonical request metadata."""
        return _canonical_json(self.metadata()).decode("ascii")


@dataclass(frozen=True, repr=False)
class ProviderWireRequest:
    """Opaque body paired with the durable provider request ID."""

    provider_request_id: str
    provider_request: object


@dataclass(frozen=True)
class ProviderReconcileRequest:
    """Body-free request for resolving one ambiguous provider write."""

    provider_request_id: str
    request_fingerprint: str


@dataclass(frozen=True)
class ProviderRequestEvidence:
    """Provider evidence tied to the exact stable request ID."""

    provider_request_id: str
    condition: ProviderOutcomeCondition
    result: object = None
    no_effect_proof: bool = False

    def __post_init__(self):
        """Reject evidence that could accidentally authorize fallback."""
        _require_identifier(self.provider_request_id, "provider_request_id")
        if not isinstance(self.condition, ProviderOutcomeCondition):
            raise ProviderRequestJournalError(
                "condition must be ProviderOutcomeCondition",
            )
        if type(self.no_effect_proof) is not bool:
            raise ProviderRequestJournalError(
                "no_effect_proof must be a boolean",
            )
        if self.condition is ProviderOutcomeCondition.RESULT:
            if not isinstance(self.result, ProviderExecutionResult):
                raise ProviderRequestJournalError(
                    "RESULT evidence requires ProviderExecutionResult",
                )
            return
        if self.result is not None or self.no_effect_proof:
            raise ProviderRequestJournalError(
                "{} evidence cannot carry a result or proof".format(
                    self.condition.value,
                ),
            )

    @classmethod
    def observed(cls, provider_request_id, result, no_effect_proof=False):
        """Build evidence from an exact provider observation."""
        return cls(
            provider_request_id,
            ProviderOutcomeCondition.RESULT,
            result,
            no_effect_proof,
        )

    @classmethod
    def unavailable(cls, provider_request_id):
        """Build an unavailable observation."""
        return cls(provider_request_id, ProviderOutcomeCondition.UNAVAILABLE)

    @classmethod
    def timeout(cls, provider_request_id):
        """Build a timeout observation."""
        return cls(provider_request_id, ProviderOutcomeCondition.TIMEOUT)

    @classmethod
    def conflict(cls, provider_request_id):
        """Build a conflict observation."""
        return cls(provider_request_id, ProviderOutcomeCondition.CONFLICT)


@dataclass(frozen=True)
class ProviderRequestRecord:
    """Secret-free durable projection of one request."""

    revision: int
    phase: ProviderRequestPhase
    provider_request_id: str
    request_fingerprint: str
    metadata_json: str
    no_effect_proof: bool = False
    diagnostic_code: str = ""
    diagnostic_digest: str = ""

    def __post_init__(self):
        """Validate record bounds and terminal truth claims."""
        _require_positive_int(self.revision, "revision")
        if not isinstance(self.phase, ProviderRequestPhase):
            raise ProviderRequestJournalError(
                "phase must be ProviderRequestPhase",
            )
        _require_identifier(self.provider_request_id, "provider_request_id")
        _require_digest(self.request_fingerprint, "request_fingerprint")
        if not isinstance(self.metadata_json, str):
            raise ProviderRequestJournalError("metadata_json must be a string")
        try:
            metadata = json.loads(self.metadata_json)
        except json.JSONDecodeError as error:
            raise ProviderRequestJournalError(
                "metadata_json must be valid JSON",
            ) from error
        if _canonical_json(metadata).decode("ascii") != self.metadata_json:
            raise ProviderRequestJournalError(
                "metadata_json must be canonical",
            )
        if hashlib.sha256(self.metadata_json.encode("utf-8")).hexdigest() != self.request_fingerprint:
            raise ProviderRequestJournalError(
                "record metadata_json does not match request_fingerprint",
            )
        if type(self.no_effect_proof) is not bool:
            raise ProviderRequestJournalError(
                "no_effect_proof must be a boolean",
            )
        if self.phase is ProviderRequestPhase.NOT_APPLIED:
            if not self.no_effect_proof:
                raise ProviderRequestJournalError(
                    "NOT_APPLIED requires explicit no-effect proof",
                )
        elif self.no_effect_proof:
            raise ProviderRequestJournalError(
                "{} cannot carry no-effect proof".format(self.phase.value),
            )
        if self.diagnostic_code:
            _require_identifier(self.diagnostic_code, "diagnostic_code")
            _require_digest(self.diagnostic_digest, "diagnostic_digest")
        elif self.diagnostic_digest:
            raise ProviderRequestJournalError(
                "diagnostic_digest requires diagnostic_code",
            )

    def advanced(self, phase, code="", detail="", no_effect_proof=False):
        """Return the next immutable record with bounded opaque diagnostics."""
        digest = _digest_text(detail) if code else ""
        advanced = ProviderRequestRecord(
            revision=self.revision + 1,
            phase=phase,
            provider_request_id=self.provider_request_id,
            request_fingerprint=self.request_fingerprint,
            metadata_json=self.metadata_json,
            no_effect_proof=no_effect_proof,
            diagnostic_code=code,
            diagnostic_digest=digest,
        )
        _validate_record_transition(self, advanced)
        return advanced


class FileProviderRequestJournal:
    """Multi-record, mode-0600 file journal with atomic exact readback."""

    def __init__(self, path):
        """Bind to an explicit path reserved for provider requests."""
        if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path or not os.path.basename(path):
            raise ProviderRequestJournalError(
                "journal path must be an absolute normalized file path",
            )
        self._path = path
        self._directory = os.path.dirname(path)
        self._name = os.path.basename(path)
        self._lock_name = "{}.lock".format(self._name)

    def load(self, provider_request_id):
        """Read one authenticated record by stable provider request ID."""
        _require_identifier(provider_request_id, "provider_request_id")
        directory_fd = self._open_private_directory()
        try:
            self._validate_existing_lock(directory_fd)
            records = self._load_records_at(directory_fd)
            return records.get(provider_request_id)
        finally:
            os.close(directory_fd)

    def _open_private_directory(self):
        """Open and validate the owned mode-0700 parent without following it."""
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            directory_fd = os.open(self._directory, flags)
        except OSError as error:
            raise ProviderRequestJournalError(
                "journal parent cannot be opened safely",
            ) from error
        try:
            status = os.fstat(directory_fd)
            if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid() or stat.S_IMODE(status.st_mode) != 0o700:
                raise ProviderRequestJournalError(
                    "journal parent must be an owned mode-0700 directory",
                )
            return directory_fd
        except BaseException:
            os.close(directory_fd)
            raise

    @staticmethod
    def _validate_private_regular(file_fd, label):
        """Require an owned, singly-linked, exact mode-0600 regular file."""
        status = os.fstat(file_fd)
        if not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid() or status.st_nlink != 1 or stat.S_IMODE(status.st_mode) != 0o600:
            raise ProviderRequestJournalError(
                "{} must be an owned singly-linked mode-0600 regular file".format(
                    label,
                ),
            )

    def _open_existing(self, directory_fd, name, label, flags=os.O_RDONLY):
        """Open and validate an existing file relative to the anchored parent."""
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            file_fd = os.open(name, flags, dir_fd=directory_fd)
        except OSError as error:
            raise ProviderRequestJournalError(
                "{} cannot be opened safely".format(label),
            ) from error
        try:
            self._validate_private_regular(file_fd, label)
            return file_fd
        except BaseException:
            os.close(file_fd)
            raise

    def _validate_existing_lock(self, directory_fd):
        """Reject an unsafe pre-existing lock even on read-only journal access."""
        try:
            lock_fd = self._open_existing(
                directory_fd,
                self._lock_name,
                "journal lock",
                os.O_RDWR,
            )
        except ProviderRequestJournalError as error:
            try:
                os.stat(
                    self._lock_name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            raise error
        os.close(lock_fd)

    def _load_records_at(self, directory_fd):
        """Read and authenticate the journal through the anchored parent."""
        try:
            file_fd = self._open_existing(
                directory_fd,
                self._name,
                "journal",
            )
        except ProviderRequestJournalError as error:
            try:
                os.stat(
                    self._name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return {}
            raise error
        try:
            with os.fdopen(file_fd, "rb") as handle:
                file_fd = -1
                payload = handle.read(MAX_JOURNAL_BYTES + 1)
        finally:
            if file_fd >= 0:
                os.close(file_fd)
        if len(payload) > MAX_JOURNAL_BYTES:
            raise ProviderRequestJournalError("journal exceeds size limit")
        return _decode_records(payload)

    def _open_lock(self, directory_fd):
        """Create or open the lock without accepting unsafe existing inodes."""
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            lock_fd = os.open(
                self._lock_name,
                flags,
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            return self._open_existing(
                directory_fd,
                self._lock_name,
                "journal lock",
                os.O_RDWR,
            )
        except OSError as error:
            raise ProviderRequestJournalError(
                "journal lock cannot be created safely",
            ) from error
        try:
            os.fchmod(lock_fd, 0o600)
            self._validate_private_regular(lock_fd, "journal lock")
            return lock_fd
        except BaseException:
            os.close(lock_fd)
            try:
                os.unlink(self._lock_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            raise

    def _create_temporary(self, directory_fd):
        """Create a private unique temporary file in the anchored parent."""
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        for unused in range(100):
            name = ".provider-request-{}".format(secrets.token_hex(16))
            try:
                file_fd = os.open(
                    name,
                    flags,
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue
            try:
                os.fchmod(file_fd, 0o600)
                self._validate_private_regular(file_fd, "temporary journal")
                return file_fd, name
            except BaseException:
                os.close(file_fd)
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                raise
        raise ProviderRequestJournalError(
            "could not allocate a unique temporary journal",
        )

    def commit(self, expected_revision, record):
        """Compare, atomically replace, fsync, and exactly read back a record."""
        if not isinstance(record, ProviderRequestRecord):
            raise ProviderRequestJournalError(
                "record must be ProviderRequestRecord",
            )
        directory_fd = self._open_private_directory()
        try:
            lock_fd = self._open_lock(directory_fd)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                self._validate_private_regular(lock_fd, "journal lock")
                records = self._load_records_at(directory_fd)
                current = records.get(record.provider_request_id)
                current_revision = None if current is None else current.revision
                if current_revision != expected_revision:
                    raise ProviderRequestJournalError(
                        "journal compare-and-store collision",
                    )
                if current is None:
                    if record.revision != 1 or record.phase is not ProviderRequestPhase.PREPARED:
                        raise ProviderRequestJournalError(
                            "new journal records must start at revision 1 PREPARED",
                        )
                else:
                    _validate_record_transition(current, record)
                records[record.provider_request_id] = record
                payload = _encode_records(records)
                temporary_fd, temporary_name = self._create_temporary(
                    directory_fd,
                )
                try:
                    with os.fdopen(temporary_fd, "wb") as handle:
                        temporary_fd = -1
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(
                        temporary_name,
                        self._name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                    )
                    os.fsync(directory_fd)
                finally:
                    if temporary_fd >= 0:
                        os.close(temporary_fd)
                    try:
                        os.unlink(temporary_name, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
                readback = self._load_records_at(directory_fd).get(
                    record.provider_request_id,
                )
                if readback != record:
                    raise ProviderRequestJournalError(
                        "journal exact readback failed",
                    )
                return readback
            finally:
                os.close(lock_fd)
        finally:
            os.close(directory_fd)


class DurableProviderRequestExecutor:
    """Default-off executor wrapper enforcing durable no-double-write rules."""

    def __init__(
        self,
        executor,
        journal,
        reconciler=None,
        enabled=False,
        clock_ms=None,
    ):
        """Inject provider execution, reconciliation, storage, and clock seams."""
        self._enabled = enabled
        if type(enabled) is not bool:
            raise ProviderRequestJournalError("enabled must be a boolean")
        self._executor = executor
        self._journal = journal
        self._reconciler = reconciler
        self._clock_ms = clock_ms if clock_ms is not None else lambda: int(time.time() * 1000)

    async def __call__(self, request):
        """Execute once or reconcile by stable ID, never unsafe-fallback."""
        if not self._enabled:
            return ProviderExecutionResult.unknown(
                "provider request journal disabled",
            )
        if not isinstance(request, ProviderJournalRequest):
            return ProviderExecutionResult.unknown("invalid journal request")
        try:
            record = self._journal.load(request.provider_request_id)
        except Exception:
            return ProviderExecutionResult.unknown("journal unavailable")
        if record is not None and not isinstance(record, ProviderRequestRecord):
            return ProviderExecutionResult.unknown("invalid journal record")

        if record is not None:
            if not self._matches(record, request):
                return self._quarantine_collision(record)
            if record.phase in (
                ProviderRequestPhase.APPLIED,
                ProviderRequestPhase.NOT_APPLIED,
            ):
                return self._replay_terminal(record)
            if record.phase in (
                ProviderRequestPhase.IN_FLIGHT,
                ProviderRequestPhase.UNKNOWN,
            ):
                return await self._reconcile(record)

        try:
            now_ms = self._clock_ms()
            if type(now_ms) is not int:
                raise ProviderRequestJournalError("clock must return an integer")
            if request.ownership.lease_expires_at_ms <= now_ms:
                return ProviderExecutionResult.unknown("ownership lease expired")
        except Exception:
            return ProviderExecutionResult.unknown("clock unavailable")

        if record is None:
            record = ProviderRequestRecord(
                revision=1,
                phase=ProviderRequestPhase.PREPARED,
                provider_request_id=request.provider_request_id,
                request_fingerprint=request.fingerprint,
                metadata_json=request.metadata_json,
            )
            try:
                record = self._commit_exact(None, record)
            except Exception:
                return ProviderExecutionResult.unknown("prepare not durable")

        in_flight = record.advanced(ProviderRequestPhase.IN_FLIGHT)
        try:
            in_flight = self._commit_exact(record.revision, in_flight)
        except Exception:
            return ProviderExecutionResult.unknown("in-flight marker not durable")

        wire_request = ProviderWireRequest(
            request.provider_request_id,
            request.provider_request,
        )
        try:
            evidence = self._executor(wire_request)
            if inspect.isawaitable(evidence):
                evidence = await evidence
        except BaseException:
            raise
        return self._finalize_evidence(in_flight, evidence)

    @staticmethod
    def _matches(record, request):
        """Require exact immutable identity before using an existing record."""
        return record.provider_request_id == request.provider_request_id and record.request_fingerprint == request.fingerprint and record.metadata_json == request.metadata_json

    def _quarantine_collision(self, record):
        """Make a path-level collision permanently non-fallback-safe."""
        if record.phase in (
            ProviderRequestPhase.PREPARED,
            ProviderRequestPhase.IN_FLIGHT,
        ):
            unknown = record.advanced(
                ProviderRequestPhase.UNKNOWN,
                "identity-collision",
                "provider request journal identity collision",
            )
            try:
                self._commit_exact(record.revision, unknown)
            except Exception:
                pass
        return ProviderExecutionResult.unknown(
            "provider request identity collision",
        )

    async def _reconcile(self, record):
        """Resolve an ambiguous request by exact stable ID without resending."""
        if self._reconciler is None:
            return self._persist_unknown(record, "reconciler-unavailable")
        reconcile_request = ProviderReconcileRequest(
            record.provider_request_id,
            record.request_fingerprint,
        )
        try:
            evidence = self._reconciler(reconcile_request)
            if inspect.isawaitable(evidence):
                evidence = await evidence
        except BaseException:
            raise
        return self._finalize_evidence(record, evidence)

    def _finalize_evidence(self, record, evidence):
        """Persist authenticated evidence before exposing a terminal result."""
        if not isinstance(evidence, ProviderRequestEvidence):
            return self._persist_unknown(record, "invalid-evidence")
        if evidence.provider_request_id != record.provider_request_id:
            return self._persist_unknown(record, "evidence-id-mismatch")
        if evidence.condition is not ProviderOutcomeCondition.RESULT:
            return self._persist_unknown(
                record,
                "provider-{}".format(evidence.condition.value),
            )

        result = evidence.result
        if result.state is ProviderExecutionState.APPLIED:
            phase = ProviderRequestPhase.APPLIED
            no_effect_proof = False
        elif result.state is ProviderExecutionState.NOT_APPLIED and result.fallback_safe and evidence.no_effect_proof:
            phase = ProviderRequestPhase.NOT_APPLIED
            no_effect_proof = True
        else:
            return self._persist_unknown(record, "effect-unknown")

        terminal = record.advanced(
            phase,
            "provider-evidence",
            result.detail,
            no_effect_proof=no_effect_proof,
        )
        try:
            durable = self._commit_exact(record.revision, terminal)
        except Exception:
            return ProviderExecutionResult.unknown(
                "terminal evidence not durable",
            )
        if durable.phase is ProviderRequestPhase.APPLIED:
            return ProviderExecutionResult.applied(
                result.applied_value,
                "provider evidence durably applied",
            )
        return ProviderExecutionResult.not_applied(
            fallback_safe=True,
            detail="provider no-effect proof durable",
        )

    def _persist_unknown(self, record, code):
        """Persist ambiguity when possible and always block fallback."""
        if record.phase is not ProviderRequestPhase.UNKNOWN:
            unknown = record.advanced(
                ProviderRequestPhase.UNKNOWN,
                code,
                code,
            )
            try:
                self._commit_exact(record.revision, unknown)
            except Exception:
                pass
        return ProviderExecutionResult.unknown(code)

    def _commit_exact(self, expected_revision, record):
        """Require every store implementation to return the exact readback."""
        readback = self._journal.commit(expected_revision, record)
        if readback != record:
            raise ProviderRequestJournalError(
                "journal store did not return exact readback",
            )
        return readback

    @staticmethod
    def _replay_terminal(record):
        """Project durable terminal evidence without replaying a write."""
        if record.phase is ProviderRequestPhase.APPLIED:
            return ProviderExecutionResult.applied(
                detail="durable provider result replayed",
            )
        return ProviderExecutionResult.not_applied(
            fallback_safe=True,
            detail="durable no-effect proof replayed",
        )


def _require_identifier(value, name):
    """Require bounded printable canonical identity text."""
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_TEXT_BYTES or not _IDENTIFIER.fullmatch(value):
        raise ProviderRequestJournalError(
            "{} must be a canonical bounded identifier".format(name),
        )


def _require_digest(value, name):
    """Require a canonical SHA-256 digest."""
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ProviderRequestJournalError(
            "{} must be a lowercase SHA-256 digest".format(name),
        )


def _require_positive_int(value, name):
    """Reject booleans, zero, negatives, and non-integers."""
    if type(value) is not int or value <= 0:
        raise ProviderRequestJournalError(
            "{} must be a positive integer".format(name),
        )


def _require_sorted_unique_identifiers(values, name):
    """Require a non-empty canonical tuple with no duplicates."""
    if type(values) is not tuple or not values:
        raise ProviderRequestJournalError(
            "{} must be a non-empty tuple".format(name),
        )
    for value in values:
        _require_identifier(value, name)
    if values != tuple(sorted(set(values))):
        raise ProviderRequestJournalError(
            "{} must be sorted and unique".format(name),
        )


def _validate_record_transition(current, replacement):
    """Enforce immutable identity, exact revision, and the legal phase graph."""
    if not isinstance(current, ProviderRequestRecord) or not isinstance(
        replacement,
        ProviderRequestRecord,
    ):
        raise ProviderRequestJournalError(
            "journal transition requires ProviderRequestRecord values",
        )
    if replacement.provider_request_id != current.provider_request_id or replacement.request_fingerprint != current.request_fingerprint or replacement.metadata_json != current.metadata_json:
        raise ProviderRequestJournalError(
            "journal transition cannot change immutable request identity",
        )
    if replacement.revision != current.revision + 1:
        raise ProviderRequestJournalError(
            "journal transition revision must advance by one",
        )
    legal = {
        ProviderRequestPhase.PREPARED: {
            ProviderRequestPhase.IN_FLIGHT,
            ProviderRequestPhase.UNKNOWN,
        },
        ProviderRequestPhase.IN_FLIGHT: {
            ProviderRequestPhase.UNKNOWN,
            ProviderRequestPhase.APPLIED,
            ProviderRequestPhase.NOT_APPLIED,
        },
        ProviderRequestPhase.UNKNOWN: {
            ProviderRequestPhase.APPLIED,
            ProviderRequestPhase.NOT_APPLIED,
        },
        ProviderRequestPhase.APPLIED: set(),
        ProviderRequestPhase.NOT_APPLIED: set(),
    }
    if replacement.phase not in legal[current.phase]:
        raise ProviderRequestJournalError(
            "illegal journal phase transition {} -> {}".format(
                current.phase.value,
                replacement.phase.value,
            ),
        )


def _canonical_json(value):
    """Encode canonical bounded UTF-8 JSON."""
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ProviderRequestJournalError("value is not canonical JSON") from error
    if len(payload) > MAX_JOURNAL_BYTES:
        raise ProviderRequestJournalError("canonical JSON exceeds size limit")
    return payload


def _digest_json(value):
    """Hash canonical JSON without retaining its source body."""
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _digest_text(value):
    """Return a bounded diagnostic hash without persisting diagnostic text."""
    if not isinstance(value, str):
        value = ""
    return hashlib.sha256(value.encode("utf-8")[:MAX_TEXT_BYTES]).hexdigest()


def _record_data(record):
    """Return the strict JSON projection of one record."""
    return {
        "diagnostic_code": record.diagnostic_code,
        "diagnostic_digest": record.diagnostic_digest,
        "metadata_json": record.metadata_json,
        "no_effect_proof": record.no_effect_proof,
        "phase": record.phase.value,
        "provider_request_id": record.provider_request_id,
        "request_fingerprint": record.request_fingerprint,
        "revision": record.revision,
    }


def _encode_records(records):
    """Encode the bounded record map with an integrity digest."""
    if type(records) is not dict:
        raise ProviderRequestJournalError("records must be a dict")
    data = {
        "records": {key: _record_data(records[key]) for key in sorted(records)},
        "schema": 1,
    }
    envelope = {"data": data, "integrity": _digest_json(data)}
    return _canonical_json(envelope)


def _decode_records(payload):
    """Decode, authenticate, and strictly reconstruct the record map."""
    try:
        envelope = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderRequestJournalError("invalid journal JSON") from error
    if type(envelope) is not dict or set(envelope) != {"data", "integrity"} or type(envelope["data"]) is not dict or envelope["integrity"] != _digest_json(envelope["data"]):
        raise ProviderRequestJournalError("journal integrity check failed")
    data = envelope["data"]
    if set(data) != {"records", "schema"} or data["schema"] != 1:
        raise ProviderRequestJournalError("unsupported journal schema")
    if type(data["records"]) is not dict:
        raise ProviderRequestJournalError("journal records must be a dict")
    expected = {
        "diagnostic_code",
        "diagnostic_digest",
        "metadata_json",
        "no_effect_proof",
        "phase",
        "provider_request_id",
        "request_fingerprint",
        "revision",
    }
    records = {}
    for key, record_data in data["records"].items():
        _require_identifier(key, "journal record key")
        if type(record_data) is not dict or set(record_data) != expected:
            raise ProviderRequestJournalError("invalid journal record shape")
        try:
            phase = ProviderRequestPhase(record_data["phase"])
        except (TypeError, ValueError) as error:
            raise ProviderRequestJournalError("invalid journal phase") from error
        record = ProviderRequestRecord(
            revision=record_data["revision"],
            phase=phase,
            provider_request_id=record_data["provider_request_id"],
            request_fingerprint=record_data["request_fingerprint"],
            metadata_json=record_data["metadata_json"],
            no_effect_proof=record_data["no_effect_proof"],
            diagnostic_code=record_data["diagnostic_code"],
            diagnostic_digest=record_data["diagnostic_digest"],
        )
        if key != record.provider_request_id:
            raise ProviderRequestJournalError(
                "journal record key does not match provider_request_id",
            )
        records[key] = record
    return records
