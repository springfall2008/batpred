# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice generated-overlay target
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Pure, detached whole-config target for a Lattice-owned generated overlay.

This module implements the target protocol consumed by the default-off atomic
materializer.  It does not register runtime code, construct a compiler, or
read/write PredBat's ``apps.yaml`` or ``secrets.yaml``.  The target owns one
explicit JSON overlay whose complete contents are replaced atomically.
"""

# cspell:ignore autoconfig CLOEXEC CREAT EINVAL ENOTSUP EOPNOTSUPP fchmod
# cspell:ignore fspath fstat fsync geteuid NONBLOCK NOFOLLOW RDONLY RDWR
# cspell:ignore readback ROLLEDBACK RONLY WRONLY

import errno
import fcntl
import hashlib
import json
import os
import secrets
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass, replace
from types import MappingProxyType

from lattice_atomic_materializer import (
    MAX_CONFIG_BYTES,
    MAX_SEQUENCE,
    TargetOperationKey,
    TargetOperationPhase,
    TargetOperationSnapshot,
    TargetTerminalPhase,
    WholeConfigTarget,
    _canonical_config,
    _canonical_json,
    _config_digest,
    _configs_equal,
    _validate_handle,
)
from lattice_durable_runtime_stores import (
    MAX_OPAQUE_PAYLOAD_BYTES,
    OpaqueTargetJournalEntry,
)


TARGET_FORMAT = "predbat-lattice-generated-overlay-target"
TARGET_VERSION = 1
_PROTECTED_FILENAMES = frozenset(
    (
        "apps.yaml",
        "apps.yml",
        "secrets.yaml",
        "secrets.yml",
    )
)
_SYNC_UNSUPPORTED = frozenset(
    (
        errno.EINVAL,
        getattr(errno, "ENOTSUP", errno.EINVAL),
        getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    )
)


class WholeConfigTargetError(RuntimeError):
    """Base error for unavailable, corrupt, or conflicting target state."""


class WholeConfigTargetCorrupt(WholeConfigTargetError):
    """The overlay or target-owned journal is malformed or unsafe."""


class WholeConfigTargetConflict(WholeConfigTargetError):
    """A target transition lost its exact journal compare-and-store."""


@dataclass(frozen=True, repr=False)
class _OverlayState:
    """Exact overlay visibility, including the distinction of file absence."""

    present: bool
    config: MappingProxyType

    def __post_init__(self):
        """Validate one exact bounded overlay observation."""
        if type(self.present) is not bool:
            raise ValueError("overlay presence flag is invalid")
        canonical = _canonical_config(
            self.config,
            "generated overlay",
        )
        if not self.present and canonical:
            raise ValueError("absent generated overlay must be empty")
        if not _configs_equal(canonical, self.config):
            raise ValueError("generated overlay is not canonical")
        object.__setattr__(self, "config", canonical)


@dataclass(frozen=True, repr=False)
class _TargetRecord:
    """Target-private journal state; repr deliberately excludes configs."""

    key: TargetOperationKey
    phase: TargetOperationPhase
    prepared: bool
    target_binding: str
    allowlist_binding: str
    handle: object = None
    preimage: object = None
    candidate: object = None
    terminal_phase: object = None

    def __post_init__(self):
        """Validate phase and exact target-owned state."""
        if type(self.key) is not TargetOperationKey:
            raise ValueError("target record key is invalid")
        TargetOperationKey.__post_init__(self.key)
        if not isinstance(self.phase, TargetOperationPhase):
            raise ValueError("target record phase is invalid")
        for binding in (
            self.target_binding,
            self.allowlist_binding,
        ):
            if type(binding) is not str or len(binding) != 64 or any(character not in "0123456789abcdef" for character in binding):
                raise ValueError("target record binding is invalid")
        if type(self.prepared) is not bool:
            raise ValueError("target record prepared flag is invalid")

        if not self.prepared:
            if self.phase is not TargetOperationPhase.SEALED or self.terminal_phase is not TargetTerminalPhase.ROLLED_BACK or self.handle is not None or self.preimage is not None or self.candidate is not None:
                raise ValueError("unprepared target record is invalid")
            return

        _validate_handle(self.handle)
        if type(self.preimage) is not _OverlayState or type(self.candidate) is not _OverlayState:
            raise ValueError("target record state is invalid")
        _OverlayState.__post_init__(self.preimage)
        _OverlayState.__post_init__(self.candidate)
        if self.key.projection_digest != _config_digest(self.candidate.config):
            raise ValueError("target record candidate digest is invalid")
        if self.phase is TargetOperationPhase.SEALED:
            if not isinstance(self.terminal_phase, TargetTerminalPhase):
                raise ValueError("sealed target record lacks terminal phase")
        elif self.terminal_phase is not None:
            raise ValueError("unsealed target record has terminal phase")


class GeneratedOverlayWholeConfigTarget(WholeConfigTarget):
    """Durable fenced target for one complete allowlisted generated overlay."""

    def __init__(self, overlay_path, journal, allowed_keys):
        """Bind an explicit overlay path, opaque journal, and immutable key set."""
        path = os.fspath(overlay_path)
        if type(path) is not str or not path or "\x00" in path:
            raise ValueError("generated overlay path is invalid")
        self._path = os.path.abspath(path)
        self._directory_path = os.path.dirname(self._path)
        self._name = os.path.basename(self._path)
        if not self._name or self._name in (".", "..") or self._name.lower() in _PROTECTED_FILENAMES or len(self._name.encode("utf-8")) > 128:
            raise ValueError("generated overlay filename is invalid")
        if not self._name.endswith(".json"):
            raise ValueError("generated overlay must be an explicit JSON file")
        if not callable(getattr(journal, "load", None)) or not callable(getattr(journal, "compare_and_store", None)):
            raise ValueError("target journal does not implement load/CAS")
        self._journal = journal
        self._allowed_keys = self._validate_allowlist(allowed_keys)
        self._target_binding = hashlib.sha256(os.fsencode(self._path)).hexdigest()
        self._allowlist_binding = hashlib.sha256(_canonical_json(self._allowed_keys).encode("utf-8")).hexdigest()
        self._lock_name = self._name + ".lock"
        self._thread_lock = threading.RLock()

        os.makedirs(
            self._directory_path,
            mode=0o700,
            exist_ok=True,
        )
        if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK") or os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd or os.unlink not in os.supports_dir_fd:
            raise WholeConfigTargetError("generated overlay requires anchored POSIX dir_fd support")
        descriptor = self._open_directory()
        os.close(descriptor)

    @staticmethod
    def _validate_allowlist(allowed_keys):
        """Return a sorted exact tuple of target-owned config keys."""
        if type(allowed_keys) not in (tuple, frozenset):
            raise ValueError("generated overlay allowlist must be a tuple or frozenset")
        keys = tuple(allowed_keys)
        if not keys or any(type(key) is not str for key in keys):
            raise ValueError("generated overlay allowlist is invalid")
        canonical = _canonical_config(
            {key: None for key in keys},
            "generated overlay allowlist",
        )
        if len(canonical) != len(keys):
            raise ValueError("generated overlay allowlist contains duplicates")
        return tuple(sorted(canonical))

    def __repr__(self):
        """Expose no path, projection, pre-image, or journal payload."""
        return "GeneratedOverlayWholeConfigTarget(allowlisted_keys={!r})".format(len(self._allowed_keys))

    def _open_directory(self):
        """Open and validate the private non-symlink directory anchor."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            descriptor = os.open(self._directory_path, flags)
        except OSError as exc:
            raise WholeConfigTargetError("generated overlay directory is unavailable or unsafe") from exc
        try:
            metadata = os.fstat(descriptor)
            path_metadata = os.stat(
                self._directory_path,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(metadata.st_mode) or not stat.S_ISDIR(path_metadata.st_mode) or metadata.st_dev != path_metadata.st_dev or metadata.st_ino != path_metadata.st_ino or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
                raise WholeConfigTargetError("generated overlay directory must be private and owned")
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    def _open_lock(self, directory_fd):
        """Open the target-wide mode-0600 exclusion record."""
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            descriptor = os.open(
                self._lock_name,
                flags,
                0o600,
                dir_fd=directory_fd,
            )
        except (NotImplementedError, OSError, TypeError) as exc:
            raise WholeConfigTargetError("generated overlay lock could not be opened safely") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.geteuid() or metadata.st_nlink != 1:
                raise WholeConfigTargetError("generated overlay lock must be a private regular file")
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    @contextmanager
    def _exclusive(self):
        """Serialize every journal/overlay transition across target instances."""
        with self._thread_lock:
            directory_fd = self._open_directory()
            try:
                descriptor = self._open_lock(directory_fd)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    yield directory_fd
                finally:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    finally:
                        os.close(descriptor)
            finally:
                os.close(directory_fd)

    @staticmethod
    def _sync_directory(directory_fd):
        """Fsync an anchored directory where supported."""
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in _SYNC_UNSUPPORTED:
                raise

    def _validate_owned_config(self, value, name):
        """Canonicalize one config and reject keys outside target ownership."""
        config = _canonical_config(value, name)
        if any(key not in self._allowed_keys for key in config):
            raise ValueError("{} contains a non-owned key".format(name))
        return config

    def _encode_overlay(self, config):
        """Return the exact canonical complete overlay bytes."""
        config = self._validate_owned_config(
            config,
            "generated overlay",
        )
        encoded = _canonical_json(config).encode("ascii")
        if len(encoded) > MAX_CONFIG_BYTES:
            raise ValueError("generated overlay exceeds its byte bound")
        return encoded

    @staticmethod
    def _unique_object(pairs):
        """Reject duplicate JSON keys while decoding an overlay."""
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("generated overlay contains duplicate keys")
            result[key] = value
        return result

    def _decode_overlay(self, encoded):
        """Decode an exact canonical allowlisted overlay."""
        try:
            decoded = json.loads(
                encoded.decode("ascii"),
                object_pairs_hook=self._unique_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON value")),
            )
            config = self._validate_owned_config(
                decoded,
                "generated overlay",
            )
            if self._encode_overlay(config) != encoded:
                raise ValueError("generated overlay is not canonical")
            return config
        except (
            AttributeError,
            RecursionError,
            TypeError,
            UnicodeError,
            ValueError,
        ) as exc:
            raise WholeConfigTargetCorrupt("generated overlay content is invalid") from exc

    def _read_overlay(self, directory_fd):
        """Read one bounded private regular overlay without following links."""
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            descriptor = os.open(
                self._name,
                flags,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            return _OverlayState(
                False,
                MappingProxyType({}),
            )
        except (NotImplementedError, OSError, TypeError) as exc:
            raise WholeConfigTargetError("generated overlay could not be opened safely") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.geteuid() or metadata.st_nlink != 1 or metadata.st_size > MAX_CONFIG_BYTES:
                raise WholeConfigTargetCorrupt("generated overlay metadata is unsafe")
            chunks = []
            remaining = MAX_CONFIG_BYTES + 1
            while remaining:
                chunk = os.read(
                    descriptor,
                    min(remaining, 65536),
                )
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            encoded = b"".join(chunks)
            if len(encoded) > MAX_CONFIG_BYTES:
                raise WholeConfigTargetCorrupt("generated overlay exceeds its byte bound")
            return _OverlayState(
                True,
                self._decode_overlay(encoded),
            )
        finally:
            os.close(descriptor)

    def _write_overlay(self, directory_fd, config):
        """Atomically replace, fsync, and exactly read back the full overlay."""
        encoded = self._encode_overlay(config)
        descriptor = None
        temporary_name = None
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        for _attempt in range(32):
            candidate = ".{}.{}.tmp".format(
                self._name,
                secrets.token_hex(12),
            )
            try:
                descriptor = os.open(
                    candidate,
                    flags,
                    0o600,
                    dir_fd=directory_fd,
                )
            except FileExistsError:
                continue
            except (NotImplementedError, OSError, TypeError) as exc:
                raise WholeConfigTargetError("generated overlay temporary could not be created") from exc
            temporary_name = candidate
            break
        if descriptor is None or temporary_name is None:
            raise WholeConfigTargetError("generated overlay temporary allocation is exhausted")
        try:
            os.fchmod(descriptor, 0o600)
            offset = 0
            while offset < len(encoded):
                written = os.write(
                    descriptor,
                    encoded[offset:],
                )
                if written <= 0:
                    raise WholeConfigTargetError("generated overlay write made no progress")
                offset += written
            os.fsync(descriptor)
            closing_descriptor = descriptor
            descriptor = None
            os.close(closing_descriptor)
            try:
                os.replace(
                    temporary_name,
                    self._name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
            except (NotImplementedError, OSError, TypeError) as exc:
                raise WholeConfigTargetError("generated overlay replace failed") from exc
            temporary_name = None
            self._sync_directory(directory_fd)
            readback = self._read_overlay(directory_fd)
            if not readback.present or self._encode_overlay(readback.config) != encoded:
                raise WholeConfigTargetError("generated overlay exact readback failed")
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_name is not None:
                try:
                    os.unlink(
                        temporary_name,
                        dir_fd=directory_fd,
                    )
                except (FileNotFoundError, OSError):
                    pass

    def _remove_overlay(self, directory_fd):
        """Remove an overlay to restore an exact absent pre-image."""
        try:
            os.unlink(
                self._name,
                dir_fd=directory_fd,
            )
        except FileNotFoundError:
            pass
        except (NotImplementedError, OSError, TypeError) as exc:
            raise WholeConfigTargetError("generated overlay removal failed") from exc
        self._sync_directory(directory_fd)
        if self._read_overlay(directory_fd).present:
            raise WholeConfigTargetError("generated overlay absence readback failed")

    @staticmethod
    def _state_equal(left, right):
        """Compare exact visibility and type-exact canonical config."""
        return left.present == right.present and _configs_equal(left.config, right.config)

    @staticmethod
    def _key_document(key):
        """Return one canonical journal key document."""
        return {
            "fence": key.fence,
            "projection_digest": key.projection_digest,
        }

    @staticmethod
    def _state_document(state):
        """Return one canonical secret-bearing overlay state document."""
        return {
            "present": state.present,
            "config": json.loads(_canonical_json(state.config)),
        }

    def _record_document(self, record):
        """Build the target-owned journal document."""
        return {
            "format": TARGET_FORMAT,
            "version": TARGET_VERSION,
            "key": self._key_document(record.key),
            "phase": record.phase.value,
            "prepared": record.prepared,
            "target_binding": record.target_binding,
            "allowlist_binding": record.allowlist_binding,
            "handle": record.handle,
            "preimage": (None if record.preimage is None else self._state_document(record.preimage)),
            "candidate": (None if record.candidate is None else self._state_document(record.candidate)),
            "terminal_phase": (None if record.terminal_phase is None else record.terminal_phase.value),
        }

    def _encode_record(self, record):
        """Encode one exact target-private record without repr exposure."""
        _TargetRecord.__post_init__(record)
        encoded = json.dumps(
            self._record_document(record),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        if len(encoded) > MAX_OPAQUE_PAYLOAD_BYTES:
            raise ValueError("target journal payload exceeds its byte bound")
        return encoded

    @staticmethod
    def _require_exact_keys(value, expected, name):
        """Require one plain mapping with exactly the named fields."""
        if type(value) is not dict or set(value) != set(expected):
            raise ValueError("{} fields are invalid".format(name))
        return value

    def _decode_key(self, value):
        """Decode and reconstruct one exact target key."""
        value = self._require_exact_keys(
            value,
            ("fence", "projection_digest"),
            "target key",
        )
        key = TargetOperationKey(
            value["fence"],
            value["projection_digest"],
        )
        TargetOperationKey.__post_init__(key)
        return key

    def _decode_state(self, value, name):
        """Decode and bind one exact allowlisted overlay state."""
        value = self._require_exact_keys(
            value,
            ("present", "config"),
            name,
        )
        state = _OverlayState(
            value["present"],
            self._validate_owned_config(
                value["config"],
                name,
            ),
        )
        _OverlayState.__post_init__(state)
        return state

    def _decode_record(self, entry):
        """Decode, reconstruct, and exactly re-encode one journal head."""
        if type(entry) is not OpaqueTargetJournalEntry:
            raise WholeConfigTargetCorrupt("target journal entry type is invalid")
        try:
            document = json.loads(
                entry.payload.decode("ascii"),
                object_pairs_hook=self._unique_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON value")),
            )
            document = self._require_exact_keys(
                document,
                (
                    "format",
                    "version",
                    "key",
                    "phase",
                    "prepared",
                    "target_binding",
                    "allowlist_binding",
                    "handle",
                    "preimage",
                    "candidate",
                    "terminal_phase",
                ),
                "target journal",
            )
            if document["format"] != TARGET_FORMAT or type(document["version"]) is not int or document["version"] != TARGET_VERSION:
                raise ValueError("target journal format is invalid")
            key = self._decode_key(document["key"])
            if key != entry.key:
                raise ValueError("target journal key binding is invalid")
            phase = TargetOperationPhase(document["phase"])
            terminal_phase = None if document["terminal_phase"] is None else TargetTerminalPhase(document["terminal_phase"])
            prepared = document["prepared"]
            preimage = (
                None
                if document["preimage"] is None
                else self._decode_state(
                    document["preimage"],
                    "target preimage",
                )
            )
            candidate = (
                None
                if document["candidate"] is None
                else self._decode_state(
                    document["candidate"],
                    "target candidate",
                )
            )
            record = _TargetRecord(
                key=key,
                phase=phase,
                prepared=prepared,
                target_binding=document["target_binding"],
                allowlist_binding=document["allowlist_binding"],
                handle=document["handle"],
                preimage=preimage,
                candidate=candidate,
                terminal_phase=terminal_phase,
            )
            _TargetRecord.__post_init__(record)
            if record.target_binding != self._target_binding or record.allowlist_binding != self._allowlist_binding or self._encode_record(record) != entry.payload:
                raise ValueError("target journal binding is invalid")
            return record
        except (
            AttributeError,
            KeyError,
            RecursionError,
            TypeError,
            UnicodeError,
            ValueError,
        ) as exc:
            raise WholeConfigTargetCorrupt("target journal payload is invalid") from exc

    def _load_head(self):
        """Load and validate the current target-private journal head."""
        try:
            entry = self._journal.load()
        except Exception as exc:
            raise WholeConfigTargetError("target journal load failed ({})".format(type(exc).__name__))
        if entry is None:
            return None, None
        return entry, self._decode_record(entry)

    def _store_record(self, expected, record):
        """CAS exactly one next opaque revision."""
        self._require_revision_headroom(expected, 1)
        revision = 1 if expected is None else expected.revision + 1
        try:
            replacement = OpaqueTargetJournalEntry(
                revision,
                record.key,
                self._encode_record(record),
            )
            stored = self._journal.compare_and_store(
                expected,
                replacement,
            )
        except Exception as exc:
            raise WholeConfigTargetError("target journal CAS failed ({})".format(type(exc).__name__))
        if type(stored) is not bool:
            raise WholeConfigTargetError("target journal CAS acknowledgement is invalid")
        if not stored:
            raise WholeConfigTargetConflict("target journal CAS lost an exact transition")
        return replacement, record

    @staticmethod
    def _require_revision_headroom(entry, steps):
        """Require enough journal revisions for a complete remaining phase."""
        if type(steps) is not int or steps < 1:
            raise ValueError("target journal headroom step count is invalid")
        revision = 0 if entry is None else entry.revision
        if type(revision) is not int or revision < 0 or revision > MAX_SEQUENCE - steps:
            raise WholeConfigTargetError("target journal revision has insufficient lifecycle headroom")

    @staticmethod
    def _snapshot(record):
        """Return the secret-free protocol view of one private record."""
        if not record.prepared:
            return TargetOperationSnapshot(
                key=record.key,
                phase=TargetOperationPhase.SEALED,
                terminal_phase=TargetTerminalPhase.ROLLED_BACK,
            )
        return TargetOperationSnapshot(
            key=record.key,
            phase=record.phase,
            handle=record.handle,
            preimage_digest=_config_digest(record.preimage.config),
            candidate_digest=_config_digest(record.candidate.config),
            changed=not _configs_equal(
                record.preimage.config,
                record.candidate.config,
            ),
            terminal_phase=record.terminal_phase,
        )

    @staticmethod
    def _validate_operation(operation):
        """Revalidate an exact secret-free snapshot from a caller."""
        if type(operation) is not TargetOperationSnapshot:
            raise ValueError("target operation type is invalid")
        TargetOperationSnapshot.__post_init__(operation)
        return operation

    @staticmethod
    def _same_identity(operation, record):
        """Bind an opaque caller snapshot to the exact private operation."""
        current = GeneratedOverlayWholeConfigTarget._snapshot(record)
        return (
            operation.key,
            operation.handle,
            operation.preimage_digest,
            operation.candidate_digest,
            operation.changed,
        ) == (
            current.key,
            current.handle,
            current.preimage_digest,
            current.candidate_digest,
            current.changed,
        )

    def _transition(self, entry, record, phase, terminal_phase=None):
        """Persist one exact lifecycle transition."""
        replacement = replace(
            record,
            phase=phase,
            terminal_phase=terminal_phase,
        )
        return self._store_record(
            entry,
            replacement,
        )

    def _reconcile_sealed(self, directory_fd, entry, record):
        """Re-prove a sealed terminal result against exact current visibility."""
        if record.phase is not TargetOperationPhase.SEALED:
            raise ValueError("target record is not sealed")
        if not record.prepared or record.terminal_phase is TargetTerminalPhase.UNKNOWN:
            return entry, record
        try:
            current = self._read_overlay(directory_fd)
            expected = record.candidate if record.terminal_phase is TargetTerminalPhase.APPLIED else record.preimage
            matches_terminal = self._state_equal(current, expected)
        except Exception:
            matches_terminal = False
        if matches_terminal:
            return entry, record
        self._require_revision_headroom(entry, 1)
        return self._transition(
            entry,
            record,
            TargetOperationPhase.SEALED,
            TargetTerminalPhase.UNKNOWN,
        )

    def _reconcile_stable(self, directory_fd, entry, record):
        """Re-prove any stable phase immediately before acknowledging it."""
        if record.phase is TargetOperationPhase.SEALED:
            return self._reconcile_sealed(
                directory_fd,
                entry,
                record,
            )
        if record.phase not in (
            TargetOperationPhase.PREPARED,
            TargetOperationPhase.APPLIED,
            TargetOperationPhase.ROLLED_BACK,
        ):
            return entry, record
        expected = record.candidate if record.phase is TargetOperationPhase.APPLIED else record.preimage
        try:
            current = self._read_overlay(directory_fd)
            matches_stable = self._state_equal(current, expected)
        except Exception:
            matches_stable = False
        if matches_stable:
            return entry, record
        self._require_revision_headroom(entry, 1)
        return self._transition(
            entry,
            record,
            TargetOperationPhase.UNKNOWN,
        )

    def prepare(self, fence, projection_digest, projection):
        """Capture an exact pre-image after a winning materializer reservation."""
        key = TargetOperationKey(
            fence,
            projection_digest,
        )
        TargetOperationKey.__post_init__(key)
        candidate_config = self._validate_owned_config(
            projection,
            "target projection",
        )
        if _config_digest(candidate_config) != projection_digest:
            raise ValueError("target projection digest is invalid")

        with self._exclusive() as directory_fd:
            entry, current = self._load_head()
            if current is not None:
                if key.fence <= current.key.fence:
                    raise WholeConfigTargetConflict("target prepare fence is stale or replayed")
                if current.phase is not TargetOperationPhase.SEALED:
                    raise WholeConfigTargetConflict("target prepare cannot replace an unsettled operation")
                entry, current = self._reconcile_sealed(
                    directory_fd,
                    entry,
                    current,
                )
                if current.terminal_phase is TargetTerminalPhase.UNKNOWN:
                    raise WholeConfigTargetConflict("target prepare cannot replace an unknown operation")
            self._require_revision_headroom(entry, 5)
            preimage = self._read_overlay(directory_fd)
            if _configs_equal(preimage.config, candidate_config):
                candidate = _OverlayState(
                    preimage.present,
                    candidate_config,
                )
            else:
                candidate = _OverlayState(
                    True,
                    candidate_config,
                )
            record = _TargetRecord(
                key=key,
                phase=TargetOperationPhase.PREPARED,
                prepared=True,
                target_binding=self._target_binding,
                allowlist_binding=self._allowlist_binding,
                handle="lattice-target-{}-{}".format(
                    key.fence,
                    key.projection_digest[:24],
                ),
                preimage=preimage,
                candidate=candidate,
            )
            entry, record = self._store_record(
                entry,
                record,
            )
            _entry, record = self._reconcile_stable(
                directory_fd,
                entry,
                record,
            )
            return self._snapshot(record)

    def lookup(self, fence, projection_digest):
        """Recover only the current exact reservation key."""
        key = TargetOperationKey(
            fence,
            projection_digest,
        )
        TargetOperationKey.__post_init__(key)
        with self._exclusive() as directory_fd:
            entry, record = self._load_head()
            if record is None or record.key != key:
                return None
            _entry, record = self._reconcile_stable(
                directory_fd,
                entry,
                record,
            )
            return self._snapshot(record)

    def apply(self, operation):
        """Atomically replace the complete overlay behind a durable apply phase."""
        operation = self._validate_operation(operation)
        with self._exclusive() as directory_fd:
            entry, record = self._load_head()
            if record is None or record.key != operation.key:
                return operation
            if not self._same_identity(operation, record):
                raise WholeConfigTargetConflict("target apply operation identity is invalid")
            if record.phase is not TargetOperationPhase.PREPARED:
                _entry, record = self._reconcile_stable(
                    directory_fd,
                    entry,
                    record,
                )
                return self._snapshot(record)
            self._require_revision_headroom(entry, 4)
            entry, record = self._transition(
                entry,
                record,
                TargetOperationPhase.APPLYING,
            )
            current = self._read_overlay(directory_fd)
            if not self._state_equal(current, record.preimage):
                _entry, record = self._transition(
                    entry,
                    record,
                    TargetOperationPhase.UNKNOWN,
                )
                return self._snapshot(record)
            if not self._state_equal(record.preimage, record.candidate):
                self._write_overlay(
                    directory_fd,
                    record.candidate.config,
                )
            readback = self._read_overlay(directory_fd)
            if not self._state_equal(readback, record.candidate):
                _entry, record = self._transition(
                    entry,
                    record,
                    TargetOperationPhase.UNKNOWN,
                )
                return self._snapshot(record)
            entry, record = self._transition(
                entry,
                record,
                TargetOperationPhase.APPLIED,
            )
            _entry, record = self._reconcile_stable(
                directory_fd,
                entry,
                record,
            )
            return self._snapshot(record)

    def rollback(self, operation):
        """Restore the exact pre-image of the current sealed APPLIED operation."""
        operation = self._validate_operation(operation)
        with self._exclusive() as directory_fd:
            entry, record = self._load_head()
            if record is None or record.key != operation.key:
                return operation
            if not self._same_identity(operation, record):
                raise WholeConfigTargetConflict("target rollback operation identity is invalid")
            if record.phase is not TargetOperationPhase.SEALED or record.terminal_phase is not TargetTerminalPhase.APPLIED:
                _entry, record = self._reconcile_stable(
                    directory_fd,
                    entry,
                    record,
                )
                return self._snapshot(record)
            entry, record = self._reconcile_sealed(
                directory_fd,
                entry,
                record,
            )
            if record.terminal_phase is not TargetTerminalPhase.APPLIED:
                return self._snapshot(record)
            self._require_revision_headroom(entry, 4)
            entry, record = self._transition(
                entry,
                record,
                TargetOperationPhase.ROLLING_BACK,
            )
            current = self._read_overlay(directory_fd)
            if not self._state_equal(current, record.candidate):
                _entry, record = self._transition(
                    entry,
                    record,
                    TargetOperationPhase.UNKNOWN,
                )
                return self._snapshot(record)
            if not self._state_equal(record.preimage, record.candidate):
                if record.preimage.present:
                    self._write_overlay(
                        directory_fd,
                        record.preimage.config,
                    )
                else:
                    self._remove_overlay(directory_fd)
            readback = self._read_overlay(directory_fd)
            if not self._state_equal(readback, record.preimage):
                _entry, record = self._transition(
                    entry,
                    record,
                    TargetOperationPhase.UNKNOWN,
                )
                return self._snapshot(record)
            entry, record = self._transition(
                entry,
                record,
                TargetOperationPhase.ROLLED_BACK,
            )
            _entry, record = self._reconcile_stable(
                directory_fd,
                entry,
                record,
            )
            return self._snapshot(record)

    def seal(self, operation):
        """Seal one stable exact operation and reject later apply replay."""
        operation = self._validate_operation(operation)
        with self._exclusive() as directory_fd:
            entry, record = self._load_head()
            if record is None or record.key != operation.key:
                raise WholeConfigTargetConflict("target seal operation is not current")
            if not self._same_identity(operation, record):
                raise WholeConfigTargetConflict("target seal operation identity is invalid")
            if record.phase is TargetOperationPhase.SEALED:
                _entry, record = self._reconcile_sealed(
                    directory_fd,
                    entry,
                    record,
                )
                return self._snapshot(record)
            if record.phase is TargetOperationPhase.APPLIED:
                expected = record.candidate
                success_terminal = TargetTerminalPhase.APPLIED
            elif record.phase in (
                TargetOperationPhase.PREPARED,
                TargetOperationPhase.ROLLED_BACK,
            ):
                expected = record.preimage
                success_terminal = TargetTerminalPhase.ROLLED_BACK
            else:
                raise WholeConfigTargetConflict("target seal operation is unstable")
            try:
                current = self._read_overlay(directory_fd)
                terminal = success_terminal if self._state_equal(current, expected) else TargetTerminalPhase.UNKNOWN
            except Exception:
                terminal = TargetTerminalPhase.UNKNOWN
            self._require_revision_headroom(entry, 2)
            entry, record = self._transition(
                entry,
                record,
                TargetOperationPhase.SEALED,
                terminal,
            )
            _entry, record = self._reconcile_sealed(
                directory_fd,
                entry,
                record,
            )
            return self._snapshot(record)

    def _terminal_visibility(self, directory_fd, record):
        """Classify one unsettled record from exact durable visibility."""
        try:
            current = self._read_overlay(directory_fd)
        except Exception:
            return TargetTerminalPhase.UNKNOWN
        matches_preimage = self._state_equal(
            current,
            record.preimage,
        )
        matches_candidate = self._state_equal(
            current,
            record.candidate,
        )

        if record.phase is TargetOperationPhase.PREPARED:
            return TargetTerminalPhase.ROLLED_BACK if matches_preimage else TargetTerminalPhase.UNKNOWN
        if record.phase is TargetOperationPhase.UNKNOWN:
            return TargetTerminalPhase.UNKNOWN
        if record.phase is TargetOperationPhase.APPLYING:
            if matches_candidate and (not matches_preimage or record.phase is TargetOperationPhase.APPLYING):
                return TargetTerminalPhase.APPLIED
            if matches_preimage:
                return TargetTerminalPhase.ROLLED_BACK
            return TargetTerminalPhase.UNKNOWN
        if record.phase is TargetOperationPhase.APPLIED:
            return TargetTerminalPhase.APPLIED if matches_candidate else TargetTerminalPhase.UNKNOWN
        if record.phase is TargetOperationPhase.ROLLING_BACK:
            if matches_preimage:
                return TargetTerminalPhase.ROLLED_BACK
            if matches_candidate:
                return TargetTerminalPhase.APPLIED
            return TargetTerminalPhase.UNKNOWN
        if record.phase is TargetOperationPhase.ROLLED_BACK:
            return TargetTerminalPhase.ROLLED_BACK if matches_preimage else TargetTerminalPhase.UNKNOWN
        return TargetTerminalPhase.UNKNOWN

    def _invalidate_current(self, directory_fd, entry, record):
        """Fence and seal the current record from exact overlay visibility."""
        if record.phase is TargetOperationPhase.SEALED:
            _entry, record = self._reconcile_sealed(
                directory_fd,
                entry,
                record,
            )
            return self._snapshot(record)
        if not record.prepared:
            return self._snapshot(record)
        terminal = self._terminal_visibility(
            directory_fd,
            record,
        )
        terminal_headroom = 1 if terminal is TargetTerminalPhase.UNKNOWN else 2
        self._require_revision_headroom(entry, terminal_headroom)
        entry, record = self._transition(
            entry,
            record,
            TargetOperationPhase.SEALED,
            terminal,
        )
        _entry, record = self._reconcile_sealed(
            directory_fd,
            entry,
            record,
        )
        return self._snapshot(record)

    def _tombstone(self, key):
        """Build a durable unprepared ROLLED_BACK fence."""
        return _TargetRecord(
            key=key,
            phase=TargetOperationPhase.SEALED,
            prepared=False,
            target_binding=self._target_binding,
            allowlist_binding=self._allowlist_binding,
            terminal_phase=TargetTerminalPhase.ROLLED_BACK,
        )

    def abort(self, fence, projection_digest):
        """Tombstone an unprepared reservation or invalidate its current record."""
        key = TargetOperationKey(
            fence,
            projection_digest,
        )
        TargetOperationKey.__post_init__(key)
        with self._exclusive() as directory_fd:
            entry, record = self._load_head()
            if record is not None and record.key == key:
                return self._invalidate_current(
                    directory_fd,
                    entry,
                    record,
                )
            if record is not None and key.fence < record.key.fence:
                raise WholeConfigTargetConflict("target abort key is no longer current")
            tombstone = self._tombstone(key)
            self._store_record(
                entry,
                tombstone,
            )
            return self._snapshot(tombstone)

    def invalidate(self, operation_key):
        """Fence one current operation and classify only exact durable visibility."""
        if type(operation_key) is not TargetOperationKey:
            raise ValueError("target invalidation key type is invalid")
        TargetOperationKey.__post_init__(operation_key)
        with self._exclusive() as directory_fd:
            entry, record = self._load_head()
            if record is not None and record.key == operation_key:
                return self._invalidate_current(
                    directory_fd,
                    entry,
                    record,
                )
            if record is not None and operation_key.fence < record.key.fence:
                raise WholeConfigTargetConflict("target invalidation key is no longer current")
            tombstone = self._tombstone(operation_key)
            self._store_record(
                entry,
                tombstone,
            )
            return self._snapshot(tombstone)
