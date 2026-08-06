# cspell:ignore journalled
# -----------------------------------------------------------------------------
# Predbat Home Battery System - durable Lattice state storage
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Crash-tolerant synchronous stores for durable Lattice state.

The compiler and fragment publishers intentionally expose synchronous store
contracts.  PredBat's shared storage component is asynchronous, so callers must
run these stores on a worker thread rather than an active asyncio event loop.

Each logical value is journalled across two non-expiring JSON slots.  A write
replaces only the inactive slot and is accepted only after an exact read-back,
leaving the previous valid slot available after an interrupted storage write.
"""

# cspell:ignore autoconfig checksummed fsync

import asyncio
import hashlib
import json
import math
import os
import threading
from dataclasses import fields, is_dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from lattice_autoconfig import (
    AliasBinding,
    AliasRole,
    AutoConfigField,
    AutoConfigPlan,
    CompileIssue,
    CompileStatus,
    FieldProvenance,
    IdentityBinding,
    IndexedRoleTarget,
    MaterializationReadiness,
    ProjectedConfigArgument,
    ProjectionCandidate,
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
    RoleAssignmentBinding,
    UserConfigOverride,
)
from lattice_compiled_publication import (
    CompilationInvalidation,
    CompiledLatticeDiagnostics,
    CompiledLatticePublication,
    CompiledLatticeStateStore,
)
from lattice_fragment_adapters import FragmentAdapterState, FragmentAdapterStateStore


_STORAGE_MODULE = "lattice_autoconfig"
_SCHEMA = "predbat-lattice-state/v1"
_LOCKS_GUARD = threading.Lock()
_LOCKS = {}


class LatticeDurableStorageError(RuntimeError):
    """Base error for durable Lattice persistence."""


class LatticeDurableReadError(LatticeDurableStorageError):
    """Persisted Lattice state is unavailable or invalid."""


class LatticeDurableWriteError(LatticeDurableStorageError):
    """A durable Lattice journal write could not be verified."""


_DATACLASS_TYPES = {
    cls.__name__: cls
    for cls in (
        ProviderAlias,
        ProviderIdentityAlias,
        ProviderRoleAssignment,
        ProviderProjectionValue,
        ProviderConfigProjection,
        UserConfigOverride,
        ProviderSnapshot,
        AliasBinding,
        IdentityBinding,
        RoleAssignmentBinding,
        IndexedRoleTarget,
        MaterializationReadiness,
        FieldProvenance,
        AutoConfigField,
        ProjectionCandidate,
        ProjectedConfigArgument,
        AutoConfigPlan,
        CompileIssue,
        CompilationInvalidation,
        CompiledLatticeDiagnostics,
        CompiledLatticePublication,
        FragmentAdapterState,
    )
}
_ENUM_TYPES = {
    cls.__name__: cls
    for cls in (
        ProviderHealth,
        AliasRole,
        ProjectionValueKind,
        ProjectionRouting,
        ProjectionCardinality,
        CompileStatus,
    )
}


def _canonical_json(value):
    """Return deterministic strict JSON for checksums and persisted values."""
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _encode_node(value):
    """Encode only explicitly supported immutable Lattice value types."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats cannot be persisted")
        return value
    if isinstance(value, Enum):
        enum_name = type(value).__name__
        if _ENUM_TYPES.get(enum_name) is not type(value):
            raise ValueError("unsupported enum {}".format(enum_name))
        return {"type": "enum", "name": enum_name, "value": value.value}
    if is_dataclass(value) and not isinstance(value, type):
        class_name = type(value).__name__
        if _DATACLASS_TYPES.get(class_name) is not type(value):
            raise ValueError("unsupported dataclass {}".format(class_name))
        return {
            "type": "dataclass",
            "name": class_name,
            "fields": {field.name: _encode_node(getattr(value, field.name)) for field in fields(value)},
        }
    if isinstance(value, Mapping):
        items = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("persisted mapping keys must be strings")
            items.append([key, _encode_node(item)])
        return {"type": "mapping", "items": sorted(items)}
    if isinstance(value, tuple):
        return {"type": "tuple", "items": [_encode_node(item) for item in value]}
    if isinstance(value, frozenset):
        encoded = [_encode_node(item) for item in value]
        return {
            "type": "frozenset",
            "items": sorted(encoded, key=_canonical_json),
        }
    if isinstance(value, list):
        return {"type": "list", "items": [_encode_node(item) for item in value]}
    raise ValueError("unsupported persisted value {}".format(type(value).__name__))


def _exact_keys(value, expected, description):
    """Reject missing and forward-unknown persistence fields."""
    if not isinstance(value, dict) or set(value) != set(expected):
        raise ValueError("{} fields are invalid".format(description))


def _mutable_plain(value):
    """Thaw decoded JSON for constructors that defensively deepcopy inputs."""
    if isinstance(value, Mapping):
        return {key: _mutable_plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_mutable_plain(item) for item in value]
    if isinstance(value, frozenset):
        return {_mutable_plain(item) for item in value}
    return value


def _decode_node(value):
    """Decode one node through the explicit type whitelist."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floats cannot be restored")
        return value
    if not isinstance(value, dict):
        raise ValueError("persisted nodes must be tagged objects or JSON scalars")
    node_type = value.get("type")
    if node_type == "enum":
        _exact_keys(value, ("type", "name", "value"), "enum")
        enum_type = _ENUM_TYPES.get(value["name"])
        if enum_type is None:
            raise ValueError("unsupported persisted enum {}".format(value["name"]))
        return enum_type(value["value"])
    if node_type == "dataclass":
        _exact_keys(value, ("type", "name", "fields"), "dataclass")
        class_type = _DATACLASS_TYPES.get(value["name"])
        if class_type is None:
            raise ValueError("unsupported persisted dataclass {}".format(value["name"]))
        encoded_fields = value["fields"]
        expected_fields = tuple(field.name for field in fields(class_type))
        _exact_keys(encoded_fields, expected_fields, value["name"])
        arguments = {name: _decode_node(encoded_fields[name]) for name in expected_fields}
        if class_type is ProviderSnapshot:
            arguments["topology_fragment"] = _mutable_plain(arguments["topology_fragment"])
        elif class_type is UserConfigOverride:
            arguments["value"] = _mutable_plain(arguments["value"])
        return class_type(**arguments)
    if node_type in ("tuple", "list", "frozenset"):
        _exact_keys(value, ("type", "items"), node_type)
        if not isinstance(value["items"], list):
            raise ValueError("{} items must be a list".format(node_type))
        items = tuple(_decode_node(item) for item in value["items"])
        if node_type == "tuple":
            return items
        if node_type == "frozenset":
            return frozenset(items)
        return list(items)
    if node_type == "mapping":
        _exact_keys(value, ("type", "items"), "mapping")
        if not isinstance(value["items"], list):
            raise ValueError("mapping items must be a list")
        decoded = {}
        for pair in value["items"]:
            if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str) or pair[0] in decoded:
                raise ValueError("persisted mapping items are invalid")
            decoded[pair[0]] = _decode_node(pair[1])
        return MappingProxyType(decoded)
    raise ValueError("unsupported persisted node type {!r}".format(node_type))


def encode_fragment_adapter_state(state):
    """Encode a fragment state into strict plain JSON data."""
    if state is not None and not isinstance(state, FragmentAdapterState):
        raise ValueError("fragment state must be FragmentAdapterState or None")
    return _encode_node(state)


def decode_fragment_adapter_state(payload):
    """Decode and validate a fragment state from plain JSON data."""
    state = _decode_node(payload)
    if state is not None and not isinstance(state, FragmentAdapterState):
        raise ValueError("persisted fragment payload has the wrong root type")
    return state


def encode_compiled_lattice_publication(publication):
    """Encode one compiled publication into strict plain JSON data."""
    if publication is not None and not isinstance(
        publication,
        CompiledLatticePublication,
    ):
        raise ValueError("compiled publication must be CompiledLatticePublication or None")
    return _encode_node(publication)


def decode_compiled_lattice_publication(payload):
    """Decode and validate one compiled publication from plain JSON data."""
    publication = _decode_node(payload)
    if publication is not None and not isinstance(
        publication,
        CompiledLatticePublication,
    ):
        raise ValueError("persisted compiled payload has the wrong root type")
    return publication


def _journal_lock(storage, key):
    """Return the shared in-process lock for one durable storage key."""
    identity = (id(storage), _STORAGE_MODULE, key)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(identity, threading.RLock())


class _TwoSlotJournal:
    """Synchronous two-slot journal over PredBat's asynchronous storage API."""

    def __init__(self, storage, key, kind, encoder, decoder):
        """Bind one logical value and verify its persisted state immediately."""
        for method_name in ("load", "save", "age"):
            if not callable(getattr(storage, method_name, None)):
                raise ValueError("storage must provide async {}".format(method_name))
        if kind not in ("fragment", "compiled"):
            raise ValueError("unsupported journal kind")
        self._storage = storage
        self._key = key
        self._kind = kind
        self._encoder = encoder
        self._decoder = decoder
        self._lock = _journal_lock(storage, key)
        self.load()

    def _call(self, method_name, *args, **kwargs):
        """Run one storage coroutine only from a synchronous worker thread."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("durable Lattice storage must run outside an active asyncio event loop")
        method = getattr(self._storage, method_name)
        return asyncio.run(method(*args, **kwargs))

    def _slot_name(self, slot):
        """Return one deterministic physical journal key."""
        return "{}_{}".format(self._key, slot)

    def _local_slot_exists(self, slot_name):
        """Detect an unreadable local slot without weakening storage abstraction."""
        backend = getattr(self._storage, "backend", self._storage)
        meta_path = getattr(backend, "_meta_path", None)
        data_path = getattr(backend, "_data_path", None)
        if not callable(meta_path) or not callable(data_path):
            return None
        return os.path.exists(meta_path(_STORAGE_MODULE, slot_name)) or os.path.exists(data_path(_STORAGE_MODULE, slot_name, "json"))

    def _checksum(self, envelope):
        """Hash all safety-relevant envelope fields except the checksum."""
        content = {
            "schema": envelope["schema"],
            "kind": envelope["kind"],
            "sequence": envelope["sequence"],
            "payload": envelope["payload"],
        }
        return hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()

    def _envelope(self, sequence, value):
        """Create one checksummed plain-data envelope."""
        envelope = {
            "schema": _SCHEMA,
            "kind": self._kind,
            "sequence": sequence,
            "payload": self._encoder(value),
        }
        envelope["checksum"] = self._checksum(envelope)
        return envelope

    def _decode_envelope(self, raw, slot_name):
        """Strictly validate and decode one persisted envelope."""
        _exact_keys(
            raw,
            ("schema", "kind", "sequence", "payload", "checksum"),
            "journal envelope",
        )
        if raw["schema"] != _SCHEMA:
            raise LatticeDurableReadError("unsupported durable Lattice schema in {}".format(slot_name))
        if raw["kind"] != self._kind:
            raise LatticeDurableReadError("durable Lattice kind mismatch in {}".format(slot_name))
        sequence = raw["sequence"]
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise LatticeDurableReadError("durable Lattice sequence is invalid in {}".format(slot_name))
        if not isinstance(raw["checksum"], str) or raw["checksum"] != self._checksum(raw):
            raise LatticeDurableReadError("durable Lattice checksum mismatch in {}".format(slot_name))
        try:
            value = self._decoder(raw["payload"])
        except (TypeError, ValueError) as exc:
            raise LatticeDurableReadError(
                "durable Lattice payload is invalid in {}: {}".format(
                    slot_name,
                    exc,
                )
            ) from exc
        return sequence, value, raw

    def _read_slot(self, slot):
        """Read one slot, distinguishing a missing key from unreadable data."""
        slot_name = self._slot_name(slot)
        try:
            raw = self._call("load", _STORAGE_MODULE, slot_name)
        except Exception as exc:
            raise LatticeDurableReadError(
                "durable Lattice read failed for {}: {}: {}".format(
                    slot_name,
                    type(exc).__name__,
                    exc,
                )
            ) from exc
        if raw is None:
            try:
                age = self._call("age", _STORAGE_MODULE, slot_name)
            except Exception as exc:
                raise LatticeDurableReadError(
                    "durable Lattice age check failed for {}: {}: {}".format(
                        slot_name,
                        type(exc).__name__,
                        exc,
                    )
                ) from exc
            if age is None:
                exists = self._local_slot_exists(slot_name)
                if exists:
                    raise LatticeDurableReadError(
                        "durable Lattice slot {} exists but is unreadable".format(
                            slot_name,
                        )
                    )
                return None
            raise LatticeDurableReadError("durable Lattice slot {} exists but is unreadable".format(slot_name))
        if not isinstance(raw, dict):
            raise LatticeDurableReadError("durable Lattice envelope in {} must be a mapping".format(slot_name))
        try:
            return self._decode_envelope(raw, slot_name)
        except LatticeDurableReadError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise LatticeDurableReadError(
                "durable Lattice envelope is invalid in {}: {}".format(
                    slot_name,
                    exc,
                )
            ) from exc

    def _scan(self):
        """Return the newest valid slot, tolerating one interrupted inactive slot."""
        valid = []
        errors = []
        for slot in (0, 1):
            try:
                restored = self._read_slot(slot)
            except LatticeDurableReadError as exc:
                errors.append(exc)
                continue
            if restored is not None:
                valid.append((restored[0], slot, restored[1], restored[2]))
        if not valid:
            if errors:
                raise errors[0]
            return 0, None, None, None
        if len(valid) == 2 and valid[0][0] == valid[1][0] and valid[0][3] != valid[1][3]:
            raise LatticeDurableReadError("durable Lattice sequence {} was reused with different content".format(valid[0][0]))
        return max(valid, key=lambda item: item[0])

    def load(self):
        """Fresh-read the newest verified durable value."""
        with self._lock:
            _sequence, _slot, value, _raw = self._scan()
            return value

    def compare_and_store(self, expected, replacement):
        """Atomically compare, write the inactive slot, and verify the result."""
        with self._lock:
            sequence, active_slot, current, _raw = self._scan()
            if current != expected:
                return False
            next_sequence = sequence + 1
            next_slot = 1 if active_slot in (None, 0) else 0
            candidate = self._envelope(next_sequence, replacement)
            slot_name = self._slot_name(next_slot)
            write_error = None
            try:
                saved = self._call(
                    "save",
                    _STORAGE_MODULE,
                    slot_name,
                    candidate,
                    format="json",
                    expiry=None,
                )
                if saved is not True:
                    write_error = "storage.save returned {!r}".format(saved)
            except Exception as exc:
                write_error = "{}: {}".format(type(exc).__name__, exc)

            try:
                winner = self._read_slot(next_slot)
            except LatticeDurableReadError as exc:
                winner = None
                if write_error is None:
                    write_error = str(exc)
            if winner is not None and winner[0] == next_sequence and winner[2] == candidate:
                return True
            raise LatticeDurableWriteError(
                "durable Lattice write could not be verified for {}: {}".format(
                    slot_name,
                    write_error or "read-back differed from candidate",
                )
            )


class PredBatFragmentAdapterStateStore(FragmentAdapterStateStore):
    """Production fragment state store backed by PredBat storage."""

    def __init__(self, storage, provider_id):
        """Bind one provider to its collision-resistant durable journal."""
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("provider_id must be a non-empty string")
        self.provider_id = provider_id.strip()
        provider_key = hashlib.sha256(self.provider_id.encode("utf-8")).hexdigest()
        self._journal = _TwoSlotJournal(
            storage,
            "fragment_{}".format(provider_key),
            "fragment",
            encode_fragment_adapter_state,
            decode_fragment_adapter_state,
        )

    def load(self):
        """Fresh-read this provider's current durable fragment state."""
        state = self._journal.load()
        if state is not None and state.provider_id != self.provider_id:
            raise LatticeDurableReadError("durable fragment belongs to provider {}".format(state.provider_id))
        return state

    def compare_and_store(self, expected, replacement):
        """Atomically install a complete fragment state or tombstone."""
        for value, description in (
            (expected, "expected"),
            (replacement, "replacement"),
        ):
            if value is not None and not isinstance(value, FragmentAdapterState):
                raise ValueError("{} must be FragmentAdapterState or None".format(description))
            if value is not None and value.provider_id != self.provider_id:
                raise ValueError("{} belongs to provider {}".format(description, value.provider_id))
        return self._journal.compare_and_store(expected, replacement)


class PredBatCompiledLatticeStateStore(CompiledLatticeStateStore):
    """Production compiled-publication store backed by PredBat storage."""

    def __init__(self, storage):
        """Restore the sole compiled-Lattice publication journal."""
        self._journal = _TwoSlotJournal(
            storage,
            "compiled",
            "compiled",
            encode_compiled_lattice_publication,
            decode_compiled_lattice_publication,
        )

    def load(self):
        """Fresh-read the current compiled publication."""
        return self._journal.load()

    def compare_and_publish(self, expected_version, publication):
        """Atomically publish exactly the next compiled Lattice version."""
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        if not isinstance(publication, CompiledLatticePublication):
            raise ValueError("publication must be a CompiledLatticePublication")
        if publication.lattice_version != expected_version + 1:
            raise ValueError("publication version must be exactly expected_version + 1")
        current = self.load()
        current_version = current.lattice_version if current is not None else 0
        if current_version != expected_version:
            return False
        return self._journal.compare_and_store(current, publication)
