# -----------------------------------------------------------------------------
# Predbat Home Battery System - durable Lattice runtime stores
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Default-off durable filesystem primitives for the Lattice runtime.

The stores in this module implement existing injected protocols only.  They do
not register a component, start a compiler, mutate live configuration, or
interpret the target-owned opaque journal payload.
"""

# cspell:ignore autoconfig CLOEXEC CREAT dataclass EINVAL ENOTSUP EOPNOTSUPP
# cspell:ignore fchmod fspath fstat fsync geteuid NONBLOCK RONLY RDONLY RDWR
# cspell:ignore readback WRONLY

import base64
import errno
import fcntl
import hashlib
import hmac
import json
import math
import os
import secrets
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass, fields
from types import MappingProxyType

from lattice_atomic_materializer import (
    MAX_FEEDBACK_TOKEN_LENGTH,
    MAX_SEQUENCE,
    AtomicMaterializationState,
    AtomicMaterializationStateStore,
    MaterializationPhase,
    TargetOperationKey,
    TargetOperationPhase,
    TargetOperationSnapshot,
    TargetTerminalPhase,
    _canonical_config,
    _plan_semantic_digest,
)
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
    _plain,
)
from lattice_compiled_publication import (
    CompilationInvalidation,
    CompiledLatticeDiagnostics,
    CompiledLatticePublication,
    CompiledLatticeStateStore,
)
from lattice_fragment_adapters import (
    FragmentAdapterState,
    FragmentAdapterStateStore,
    _validated_fragment_state,
)
from lattice_topology import decode_topology


FORMAT_NAME = "predbat-lattice-state"
FORMAT_VERSION = 1
MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_OPAQUE_PAYLOAD_BYTES = 2 * 1024 * 1024
MAX_DECODE_DEPTH = 64
MAX_DECODE_NODES = 1000000
MAX_CONTAINER_ITEMS = 65536
MAX_STRING_CHARS = 1048576
MAX_INTEGER = (1 << 63) - 1
MAX_PLAN_ITEMS = 4096
MAX_PLAN_NODES = 16384
MAX_PLAN_DEPTH = 16
MAX_PLAN_TEXT = 16384

_MAPPING_PROXY_TYPE = type(MappingProxyType({}))
_SYNC_UNSUPPORTED = {
    errno.EINVAL,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
}

_ENUM_TYPES = {
    enum_type.__name__: enum_type
    for enum_type in (
        AliasRole,
        CompileStatus,
        MaterializationPhase,
        ProjectionCardinality,
        ProjectionRouting,
        ProjectionValueKind,
        ProviderHealth,
        TargetOperationPhase,
        TargetTerminalPhase,
    )
}

_DATACLASS_TYPES = {
    data_type.__name__: data_type
    for data_type in (
        AliasBinding,
        AutoConfigField,
        AutoConfigPlan,
        CompilationInvalidation,
        CompiledLatticeDiagnostics,
        CompiledLatticePublication,
        CompileIssue,
        FieldProvenance,
        FragmentAdapterState,
        IdentityBinding,
        IndexedRoleTarget,
        MaterializationReadiness,
        ProjectedConfigArgument,
        ProjectionCandidate,
        ProviderAlias,
        ProviderConfigProjection,
        ProviderIdentityAlias,
        ProviderProjectionValue,
        ProviderRoleAssignment,
        ProviderSnapshot,
        RoleAssignmentBinding,
        TargetOperationKey,
        TargetOperationSnapshot,
        AtomicMaterializationState,
    )
}


class DurableStateError(RuntimeError):
    """Base error for unavailable or unsafe durable state."""


class DurableStateCorrupt(DurableStateError):
    """A durable record is malformed, torn, oversized, or untrusted."""


def _encode_value(value, budget, depth=0):
    """Encode one exact whitelisted value into an unambiguous tagged tree."""
    if depth > MAX_DECODE_DEPTH:
        raise ValueError("durable value exceeds nesting bound")
    budget[0] += 1
    if budget[0] > MAX_DECODE_NODES:
        raise ValueError("durable value exceeds node bound")

    if value is None:
        return ["none"]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        if abs(value) > MAX_INTEGER:
            raise ValueError("durable integer exceeds bound")
        return ["int", str(value)]
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("durable float must be finite")
        return ["float", value.hex()]
    if type(value) is str:
        if len(value) > MAX_STRING_CHARS:
            raise ValueError("durable string exceeds bound")
        return ["str", value]
    if type(value) is bytes:
        if len(value) > MAX_OPAQUE_PAYLOAD_BYTES:
            raise ValueError("opaque payload exceeds bound")
        return ["bytes", base64.b64encode(value).decode("ascii")]
    if type(value) is tuple:
        if len(value) > MAX_CONTAINER_ITEMS:
            raise ValueError("durable tuple exceeds item bound")
        return [
            "tuple",
            [_encode_value(item, budget, depth + 1) for item in value],
        ]
    if type(value) is frozenset:
        if len(value) > MAX_CONTAINER_ITEMS:
            raise ValueError("durable frozenset exceeds item bound")
        encoded = [_encode_value(item, budget, depth + 1) for item in value]
        encoded.sort(key=_canonical_bytes)
        return ["frozenset", encoded]
    if type(value) in (dict, _MAPPING_PROXY_TYPE):
        if len(value) > MAX_CONTAINER_ITEMS:
            raise ValueError("durable mapping exceeds item bound")
        entries = []
        for key, item in value.items():
            if type(key) is not str or len(key) > MAX_STRING_CHARS:
                raise ValueError("durable mapping key is invalid")
            entries.append(
                [
                    key,
                    _encode_value(item, budget, depth + 1),
                ]
            )
        entries.sort(key=lambda entry: entry[0])
        return ["mapping", entries]
    if type(value) in _ENUM_TYPES.values():
        return ["enum", type(value).__name__, value.value]
    data_type = type(value)
    if data_type in _DATACLASS_TYPES.values():
        return [
            "dataclass",
            data_type.__name__,
            [
                [
                    field.name,
                    _encode_value(
                        getattr(value, field.name),
                        budget,
                        depth + 1,
                    ),
                ]
                for field in fields(data_type)
            ],
        ]
    raise ValueError(
        "durable value type {} is not allowed".format(
            type(value).__name__,
        )
    )


def _decode_value(value, budget, depth=0):
    """Decode and bound one exact tagged value without dynamic imports."""
    if depth > MAX_DECODE_DEPTH:
        raise ValueError("durable value exceeds nesting bound")
    budget[0] += 1
    if budget[0] > MAX_DECODE_NODES:
        raise ValueError("durable value exceeds node bound")
    if type(value) is not list or not value or type(value[0]) is not str:
        raise ValueError("durable value tag is invalid")
    tag = value[0]

    if tag == "none" and len(value) == 1:
        return None
    if tag == "bool" and len(value) == 2 and type(value[1]) is bool:
        return value[1]
    if tag == "int" and len(value) == 2 and type(value[1]) is str:
        if not value[1] or value[1] in ("+", "-"):
            raise ValueError("durable integer is invalid")
        parsed = int(value[1], 10)
        if str(parsed) != value[1] or abs(parsed) > MAX_INTEGER:
            raise ValueError("durable integer is non-canonical or oversized")
        return parsed
    if tag == "float" and len(value) == 2 and type(value[1]) is str:
        parsed = float.fromhex(value[1])
        if not math.isfinite(parsed) or parsed.hex() != value[1]:
            raise ValueError("durable float is non-canonical")
        return parsed
    if tag == "str" and len(value) == 2 and type(value[1]) is str:
        if len(value[1]) > MAX_STRING_CHARS:
            raise ValueError("durable string exceeds bound")
        return value[1]
    if tag == "bytes" and len(value) == 2 and type(value[1]) is str:
        try:
            decoded = base64.b64decode(
                value[1].encode("ascii"),
                validate=True,
            )
        except (UnicodeEncodeError, ValueError) as exc:
            raise ValueError("opaque payload encoding is invalid") from exc
        if len(decoded) > MAX_OPAQUE_PAYLOAD_BYTES or base64.b64encode(decoded).decode("ascii") != value[1]:
            raise ValueError("opaque payload is non-canonical or oversized")
        return decoded
    if tag in ("tuple", "frozenset") and len(value) == 2:
        items = value[1]
        if type(items) is not list or len(items) > MAX_CONTAINER_ITEMS:
            raise ValueError("durable sequence is invalid or oversized")
        decoded = tuple(_decode_value(item, budget, depth + 1) for item in items)
        if tag == "tuple":
            return decoded
        frozen = frozenset(decoded)
        if len(frozen) != len(decoded):
            raise ValueError("durable frozenset contains duplicates")
        canonical = sorted(
            (_encode_value(item, [0]) for item in frozen),
            key=_canonical_bytes,
        )
        if canonical != items:
            raise ValueError("durable frozenset ordering is non-canonical")
        return frozen
    if tag == "mapping" and len(value) == 2:
        entries = value[1]
        if type(entries) is not list or len(entries) > MAX_CONTAINER_ITEMS:
            raise ValueError("durable mapping is invalid or oversized")
        decoded = {}
        previous_key = None
        for entry in entries:
            if type(entry) is not list or len(entry) != 2 or type(entry[0]) is not str or len(entry[0]) > MAX_STRING_CHARS or entry[0] in decoded or (previous_key is not None and entry[0] <= previous_key):
                raise ValueError("durable mapping entry is invalid")
            decoded[entry[0]] = _decode_value(
                entry[1],
                budget,
                depth + 1,
            )
            previous_key = entry[0]
        return decoded
    if tag == "enum" and len(value) == 3:
        enum_type = _ENUM_TYPES.get(value[1])
        if enum_type is None or type(value[2]) is not str:
            raise ValueError("durable enum type is invalid")
        return enum_type(value[2])
    if tag == "dataclass" and len(value) == 3:
        data_type = _DATACLASS_TYPES.get(value[1])
        raw_fields = value[2]
        if data_type is None or type(raw_fields) is not list or len(raw_fields) > MAX_CONTAINER_ITEMS:
            raise ValueError("durable dataclass type is invalid")
        expected_names = [field.name for field in fields(data_type)]
        if len(raw_fields) != len(expected_names) or any(type(entry) is not list or len(entry) != 2 for entry in raw_fields) or [entry[0] for entry in raw_fields] != expected_names:
            raise ValueError("durable dataclass fields are invalid")
        arguments = {
            entry[0]: _decode_value(
                entry[1],
                budget,
                depth + 1,
            )
            for entry in raw_fields
        }
        if data_type is not ProviderSnapshot:
            arguments = {name: _freeze_json(item) for name, item in arguments.items()}
        return data_type(**arguments)
    raise ValueError("durable value tag is unknown or malformed")


def _canonical_bytes(value):
    """Return deterministic strict ASCII JSON bytes."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _freeze_json(value):
    """Recursively freeze decoded JSON mappings without touching typed data."""
    if type(value) is dict:
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if type(value) is tuple:
        return tuple(_freeze_json(item) for item in value)
    return value


def _encode_document(kind, value):
    """Wrap one encoded value in a versioned integrity envelope."""
    if type(kind) is not str or not kind:
        raise ValueError("durable record kind is invalid")
    payload = _encode_value(value, [0])
    signed = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "kind": kind,
        "payload": payload,
    }
    document = dict(signed)
    document["sha256"] = hashlib.sha256(
        _canonical_bytes(signed),
    ).hexdigest()
    encoded = _canonical_bytes(document)
    if len(encoded) > MAX_RECORD_BYTES:
        raise ValueError("durable record exceeds byte bound")
    return encoded


def _decode_document(kind, encoded):
    """Verify and decode one complete integrity envelope."""
    if type(encoded) is not bytes or len(encoded) > MAX_RECORD_BYTES:
        raise DurableStateCorrupt("durable record exceeds byte bound")
    try:
        document = json.loads(encoded.decode("ascii"))
    except (
        UnicodeDecodeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as exc:
        raise DurableStateCorrupt("durable record is not strict JSON") from exc
    if (
        type(document) is not dict
        or set(document)
        != {
            "format",
            "version",
            "kind",
            "payload",
            "sha256",
        }
        or type(document["format"]) is not str
        or document["format"] != FORMAT_NAME
        or type(document["version"]) is not int
        or document["version"] != FORMAT_VERSION
        or type(document["kind"]) is not str
        or document["kind"] != kind
        or type(document["sha256"]) is not str
    ):
        raise DurableStateCorrupt("durable record envelope is invalid")
    signed = {
        "format": document["format"],
        "version": document["version"],
        "kind": document["kind"],
        "payload": document["payload"],
    }
    try:
        expected_digest = hashlib.sha256(
            _canonical_bytes(signed),
        ).hexdigest()
        canonical_document = _canonical_bytes(document)
        integrity_matches = hmac.compare_digest(
            expected_digest,
            document["sha256"],
        )
    except (
        AttributeError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as exc:
        raise DurableStateCorrupt(
            "durable record canonicalization is invalid",
        ) from exc
    if not integrity_matches:
        raise DurableStateCorrupt("durable record integrity check failed")
    if encoded != canonical_document:
        raise DurableStateCorrupt("durable record encoding is non-canonical")
    try:
        return _decode_value(document["payload"], [0])
    except (
        AttributeError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as exc:
        raise DurableStateCorrupt("durable record payload is invalid") from exc


def _require_text(
    value,
    name,
    maximum=MAX_PLAN_TEXT,
    canonical=True,
):
    """Validate one exact bounded non-empty text field."""
    if type(value) is not str or not value or len(value) > maximum or (canonical and value != value.strip()):
        raise ValueError("{} is invalid".format(name))
    return value


def _require_integer(
    value,
    name,
    minimum=0,
    maximum=MAX_INTEGER,
):
    """Validate one exact bounded non-boolean integer."""
    if type(value) is not int or value < minimum or value > maximum:
        raise ValueError("{} is invalid".format(name))
    return value


def _require_digest(value, name):
    """Validate one exact lowercase SHA-256 digest."""
    _require_text(value, name, maximum=64)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("{} is invalid".format(name))
    return value


def _require_tuple(value, name, maximum=MAX_PLAN_ITEMS):
    """Validate one exact bounded tuple without iterable coercion."""
    if type(value) is not tuple or len(value) > maximum:
        raise ValueError("{} is invalid".format(name))
    return value


def _validate_json(value, name, budget, depth=0):
    """Validate one recursively frozen bounded strict JSON value."""
    if depth > MAX_PLAN_DEPTH:
        raise ValueError("{} exceeds depth bound".format(name))
    budget[0] += 1
    if budget[0] > MAX_PLAN_NODES:
        raise ValueError("{} exceeds node bound".format(name))
    if type(value) is _MAPPING_PROXY_TYPE:
        if len(value) > MAX_PLAN_ITEMS:
            raise ValueError("{} mapping is oversized".format(name))
        previous_key = None
        for key, item in value.items():
            if type(key) is not str or not key or len(key) > MAX_PLAN_TEXT or (previous_key is not None and key <= previous_key):
                raise ValueError(
                    "{} mapping key is invalid".format(name),
                )
            _validate_json(
                item,
                name,
                budget,
                depth + 1,
            )
            previous_key = key
        return value
    if type(value) is tuple:
        if len(value) > MAX_PLAN_ITEMS:
            raise ValueError("{} sequence is oversized".format(name))
        for item in value:
            _validate_json(
                item,
                name,
                budget,
                depth + 1,
            )
        return value
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        _require_integer(
            abs(value),
            "{} integer".format(name),
        )
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("{} float is invalid".format(name))
        return value
    if type(value) is str:
        if len(value) > MAX_PLAN_TEXT:
            raise ValueError("{} string is oversized".format(name))
        return value
    raise ValueError("{} has an invalid JSON type".format(name))


def _validate_provenance_entry(value, name):
    """Validate one exact bounded provenance coordinate."""
    if type(value) is not FieldProvenance:
        raise ValueError("{} entry type is invalid".format(name))
    _require_text(value.field_path, "{} field_path".format(name))
    _require_text(value.provider_id, "{} provider_id".format(name))
    _require_integer(
        value.generation,
        "{} generation".format(name),
    )
    _require_text(value.source_path, "{} source_path".format(name))
    return value


def _validate_provenance_tuple(
    value,
    name,
    sorted_unique=False,
):
    """Validate one exact bounded tuple of provenance coordinates."""
    provenance = _require_tuple(value, name)
    for item in provenance:
        _validate_provenance_entry(item, name)
    if sorted_unique:
        expected = tuple(
            sorted(
                set(provenance),
                key=lambda item: (
                    item.field_path,
                    item.provider_id,
                    item.generation,
                    item.source_path,
                ),
            )
        )
        if provenance != expected:
            raise ValueError(
                "{} is not sorted and unique".format(name),
            )
    return provenance


def _exact_equal(left, right):
    """Compare two whitelisted values without Python scalar coercion."""
    return _encode_value(left, [0]) == _encode_value(
        right,
        [0],
    )


def _validate_projection_semantics(argument, candidates):
    """Reconstruct the compiler's exact candidate selection for one argument."""
    flattened_candidate_provenance = tuple(source for candidate in candidates for slot_sources in candidate.provenance for source in slot_sources)
    if argument.candidate_provenance != flattened_candidate_provenance:
        raise ValueError(
            "config candidate provenance is not its exact flattening",
        )

    first = candidates[0]
    if argument.override_source is not None:
        values = argument.value if argument.cardinality == ProjectionCardinality.PER_INDEX.value else (argument.value,)
        if argument.cardinality == ProjectionCardinality.PER_INDEX.value:
            if type(argument.value) is not tuple or len(argument.value) != first.target_count:
                raise ValueError(
                    "config override cardinality is invalid",
                )
        elif type(argument.value) in (
            tuple,
            dict,
            _MAPPING_PROXY_TYPE,
        ):
            raise ValueError("scalar config override shape is invalid")
        if argument.required and any(value is None for value in values):
            raise ValueError("required config override contains None")
        expected_provenance = (
            FieldProvenance(
                "/projected_config/{}".format(argument.name),
                "user_override",
                0,
                argument.override_source,
            ),
        )
    else:
        selected_values = []
        selected_provenance = []
        for slot_index in range(first.target_count):
            slot_candidates = tuple(
                (
                    candidate,
                    candidate.slot_indexes.index(slot_index),
                )
                for candidate in candidates
                if slot_index in candidate.slot_indexes
            )
            if not slot_candidates:
                if argument.required:
                    raise ValueError(
                        "required config argument slot has no candidate",
                    )
                selected_values.append(None)
                continue
            highest_specificity = max(candidate.specificities[value_index] for candidate, value_index in slot_candidates)
            selected = tuple((candidate, value_index) for candidate, value_index in slot_candidates if candidate.specificities[value_index] == highest_specificity)
            if len({candidate.value_kinds[value_index] for candidate, value_index in selected}) != 1:
                raise ValueError(
                    "selected config candidate kinds conflict",
                )
            selected_value = selected[0][0].values[selected[0][1]]
            if any(
                not _exact_equal(
                    candidate.values[value_index],
                    selected_value,
                )
                for candidate, value_index in selected[1:]
            ):
                raise ValueError(
                    "selected config candidate values conflict",
                )
            selected_values.append(selected_value)
            selected_provenance.extend(source for candidate, value_index in selected for source in candidate.provenance[value_index])
        effective_value = tuple(selected_values) if argument.cardinality == ProjectionCardinality.PER_INDEX.value else selected_values[0]
        if not _exact_equal(argument.value, effective_value):
            raise ValueError(
                "config argument value differs from candidate selection",
            )
        expected_provenance = tuple(selected_provenance)

    if argument.provenance != expected_provenance:
        raise ValueError(
            "config argument provenance differs from candidate selection",
        )


def _provenance_sort_key(item):
    """Return the compiler's exact aggregate provenance ordering key."""
    return (
        item.field_path,
        item.provider_id,
        item.generation,
        item.source_path,
    )


def _derive_role_outputs(aliases, identities, assignments):
    """Rebuild exact legacy and indexed role outputs from compiler bindings."""
    legacy_by_role = {
        role: tuple(binding for binding in aliases if role.value in binding.roles)
        for role in (
            AliasRole.PRIMARY,
            AliasRole.CONTROL,
        )
    }
    legacy_assignments = tuple(binding for bindings in legacy_by_role.values() for binding in bindings)
    if assignments and legacy_assignments:
        raise ValueError(
            "legacy and indexed role assignments are mixed",
        )
    legacy_targets = {}
    if assignments:
        legacy_targets = {
            AliasRole.PRIMARY: None,
            AliasRole.CONTROL: None,
        }
    else:
        for role, role_aliases in legacy_by_role.items():
            if len(role_aliases) > 1:
                raise ValueError(
                    "legacy role assignment is ambiguous",
                )
            legacy_targets[role] = role_aliases[0].node_id if role_aliases else None

    correlated_providers = {}
    for binding in identities:
        correlated_providers.setdefault(
            binding.canonical_node_id,
            set(),
        ).add(binding.provider_id)
    assignments_by_slot = {}
    for binding in assignments:
        assignments_by_slot.setdefault(
            (
                binding.group,
                binding.role,
                binding.index,
            ),
            [],
        ).append(binding)
    indexed_targets = {
        AliasRole.PRIMARY: [],
        AliasRole.CONTROL: [],
    }
    for (
        group,
        role_value,
        index,
    ), slot_assignments in sorted(assignments_by_slot.items()):
        canonical_nodes = {assignment.canonical_node_id for assignment in slot_assignments}
        if len(canonical_nodes) != 1:
            raise ValueError(
                "indexed role target canonical nodes conflict",
            )
        canonical_node_id = next(iter(canonical_nodes))
        providers = {assignment.provider_id for assignment in slot_assignments}
        if len(providers) > 1 and not providers.issubset(
            correlated_providers.get(
                canonical_node_id,
                set(),
            )
        ):
            raise ValueError(
                "shared indexed target lacks identity correlation",
            )
        provenance = tuple(
            FieldProvenance(
                "/{}_targets/{}/{}/node_id".format(
                    role_value,
                    group,
                    index,
                ),
                assignment.provider_id,
                assignment.generation,
                "/role_assignments/{}/{}/{}".format(
                    role_value,
                    group,
                    index,
                ),
            )
            for assignment in slot_assignments
        )
        indexed_targets[AliasRole(role_value)].append(
            IndexedRoleTarget(
                group,
                index,
                canonical_node_id,
                provenance,
            )
        )
    return (
        legacy_targets[AliasRole.PRIMARY],
        legacy_targets[AliasRole.CONTROL],
        tuple(indexed_targets[AliasRole.PRIMARY]),
        tuple(indexed_targets[AliasRole.CONTROL]),
        bool(legacy_assignments),
    )


def _derive_plan_fields(
    aliases,
    primary_target,
    control_target,
    primary_targets,
    control_targets,
    arguments,
):
    """Rebuild the compiler's exact generated field tuple."""
    derived = []
    for binding in aliases:
        source = FieldProvenance(
            "/aliases/{}/node_id".format(
                binding.qualified_name,
            ),
            binding.provider_id,
            binding.generation,
            "/aliases/{}".format(
                binding.qualified_name.split(":", 1)[1],
            ),
        )
        derived.append(
            AutoConfigField(
                "alias.{}".format(binding.qualified_name),
                binding.node_id,
                (source,),
            )
        )
    for name, target, role in (
        (
            "primary_target",
            primary_target,
            AliasRole.PRIMARY,
        ),
        (
            "control_target",
            control_target,
            AliasRole.CONTROL,
        ),
    ):
        if target is None:
            continue
        sources = tuple(
            FieldProvenance(
                "/{}".format(name),
                binding.provider_id,
                binding.generation,
                "/aliases/{}".format(
                    binding.qualified_name.split(":", 1)[1],
                ),
            )
            for binding in aliases
            if binding.node_id == target and role.value in binding.roles
        )
        derived.append(
            AutoConfigField(
                name,
                target,
                sources,
            )
        )
    for name, targets in (
        ("primary_targets", primary_targets),
        ("control_targets", control_targets),
    ):
        for target in targets:
            derived.append(
                AutoConfigField(
                    "{}.{}.{}".format(
                        name,
                        target.group,
                        target.index,
                    ),
                    target.node_id,
                    target.provenance,
                )
            )
    for argument in arguments:
        derived.append(
            AutoConfigField(
                "config.{}".format(argument.name),
                argument.value,
                argument.provenance,
            )
        )
    return tuple(
        sorted(
            derived,
            key=lambda item: item.name,
        )
    )


def _validate_projection_topology_bindings(
    topology,
    identities,
    assignments,
    primary_targets,
    control_targets,
    arguments,
):
    """Bind candidates to exact selected targets and provider topology facts."""
    targets_by_role_group = {}
    for role, targets in (
        (AliasRole.PRIMARY.value, primary_targets),
        (AliasRole.CONTROL.value, control_targets),
    ):
        for target in targets:
            targets_by_role_group.setdefault(
                (
                    role,
                    target.group,
                ),
                [],
            ).append(target)
    targets_by_role_group = {
        key: tuple(
            sorted(
                targets,
                key=lambda item: item.index,
            )
        )
        for key, targets in targets_by_role_group.items()
    }

    nodes = {str(node["id"]): node for node in topology.get("nodes", ())}
    paths_by_node = {}
    capabilities_by_node = {}
    for node_id, node in nodes.items():
        paths = {}
        for path in node.get("accessPaths", ()):
            path_id = str(path["id"])
            provider_id = path.get("provider")
            if type(provider_id) is not str or path_id in paths:
                raise ValueError(
                    "topology access path ownership is ambiguous",
                )
            paths[path_id] = provider_id
        paths_by_node[node_id] = paths
        capabilities_by_node[node_id] = {
            (
                str(capability["capability"]),
                str(capability.get("accessPath", "")),
            )
            for capability in node.get("capabilities", ())
        }

    identity_assertions = {
        (
            binding.provider_id,
            binding.kind,
            binding.value,
            binding.local_node_id,
        )
        for binding in identities
    }
    local_by_canonical = {}
    for binding in identities:
        local_by_canonical.setdefault(
            (
                binding.provider_id,
                binding.canonical_node_id,
            ),
            set(),
        ).add(binding.local_node_id)
    assignments_by_slot = {}
    for binding in assignments:
        key = (
            binding.provider_id,
            binding.role,
            binding.group,
            binding.index,
        )
        assignments_by_slot.setdefault(
            key,
            [],
        ).append(binding)
    if any(len(slot_assignments) != 1 for slot_assignments in assignments_by_slot.values()):
        raise ValueError(
            "provider repeats an indexed role slot",
        )

    candidates_by_provider = {}
    for argument in arguments:
        for candidate in argument.candidates:
            targets = targets_by_role_group.get(
                (
                    candidate.role,
                    candidate.group,
                ),
                (),
            )
            if not targets:
                raise ValueError(
                    "candidate has no selected role/group targets",
                )
            expected_target_count = len(targets) if candidate.cardinality == ProjectionCardinality.PER_INDEX.value else 1
            if candidate.target_count != expected_target_count:
                raise ValueError(
                    "candidate target_count differs from selected targets",
                )
            if candidate.routing == ProjectionRouting.COORDINATOR.value and len({target.node_id for target in targets}) != 1:
                raise ValueError(
                    "coordinator candidate spans canonical targets",
                )

            targets_by_index = {target.index: target for target in targets}
            for value_index, slot_index in enumerate(
                candidate.slot_indexes,
            ):
                target = targets[0] if candidate.cardinality == ProjectionCardinality.SCALAR.value else targets_by_index.get(slot_index)
                if target is None:
                    raise ValueError(
                        "candidate slot is outside selected targets",
                    )
                owned_assignments = assignments_by_slot.get(
                    (
                        candidate.provider_id,
                        candidate.role,
                        candidate.group,
                        target.index,
                    ),
                    (),
                )
                if owned_assignments:
                    assignment = owned_assignments[0]
                    if assignment.canonical_node_id != target.node_id:
                        raise ValueError(
                            "candidate role assignment targets another node",
                        )
                    local_node_id = assignment.local_node_id
                else:
                    local_nodes = local_by_canonical.get(
                        (
                            candidate.provider_id,
                            target.node_id,
                        ),
                        set(),
                    )
                    if len(local_nodes) == 1:
                        local_node_id = next(iter(local_nodes))
                    else:
                        prefix = "provider:{}:".format(
                            candidate.provider_id,
                        )
                        local_node_id = target.node_id[len(prefix) :] if target.node_id.startswith(prefix) else None
                if not local_node_id:
                    raise ValueError(
                        "candidate local target cannot be proven",
                    )

                identity = candidate.identity_selectors[value_index]
                if (
                    identity is not None
                    and (
                        candidate.provider_id,
                        identity[0],
                        identity[1],
                        local_node_id,
                    )
                    not in identity_assertions
                ):
                    raise ValueError(
                        "candidate identity selector is not asserted",
                    )

                node_paths = paths_by_node.get(
                    target.node_id,
                    {},
                )
                node_capabilities = capabilities_by_node.get(
                    target.node_id,
                    set(),
                )
                capability = candidate.capabilities[value_index]
                access_path = candidate.access_path_ids[value_index]
                if (
                    access_path is not None
                    and node_paths.get(
                        access_path,
                    )
                    != candidate.provider_id
                ):
                    raise ValueError(
                        "candidate access path is not provider-owned",
                    )
                if capability is not None:
                    matching_paths = {path_id for capability_name, path_id in node_capabilities if capability_name == capability and node_paths.get(path_id) == candidate.provider_id}
                    if not matching_paths or (access_path is not None and access_path not in matching_paths):
                        raise ValueError(
                            "candidate capability is not provider-published",
                        )

            candidates_by_provider.setdefault(
                candidate.provider_id,
                [],
            ).append(candidate)

    for provider_candidates in candidates_by_provider.values():
        ordered = tuple(
            sorted(
                provider_candidates,
                key=lambda item: item.argument,
            )
        )
        if len({candidate.argument for candidate in ordered}) != len(ordered):
            raise ValueError(
                "provider repeats projection argument",
            )
        for expected_index, candidate in enumerate(ordered):
            for slot_provenance in candidate.provenance:
                source_parts = slot_provenance[0].source_path.split(
                    "/",
                )
                if int(source_parts[2]) != expected_index:
                    raise ValueError(
                        "candidate projection index differs from compiler order",
                    )


def _derive_plan_provenance(
    topology,
    aliases,
    identities,
    assignments,
    primary_target,
    control_target,
    primary_targets,
    control_targets,
    arguments,
    provider_generations,
):
    """Rebuild every aggregate provenance coordinate retained by the compiler."""
    generations = dict(provider_generations)
    topology_inputs = topology.get(
        "producer",
        {},
    ).get(
        "inputs",
        (),
    )
    topology_provider_ids = tuple(item.get("provider") for item in topology_inputs if type(item) is dict)
    if topology_provider_ids != tuple(provider_id for provider_id, _generation in provider_generations):
        raise ValueError(
            "topology inputs differ from provider cursor",
        )
    derived = [
        FieldProvenance(
            "/topology",
            provider_id,
            generation,
            "/",
        )
        for provider_id, generation in provider_generations
    ]
    for binding in aliases:
        derived.append(
            FieldProvenance(
                "/aliases/{}/node_id".format(
                    binding.qualified_name,
                ),
                binding.provider_id,
                binding.generation,
                "/aliases/{}".format(
                    binding.qualified_name.split(":", 1)[1],
                ),
            )
        )
    local_by_canonical = {}
    for binding in identities:
        derived.append(
            FieldProvenance(
                "/identity_aliases/{}:{}:{}/canonical_node_id".format(
                    binding.provider_id,
                    binding.kind,
                    binding.value,
                ),
                binding.provider_id,
                binding.generation,
                "/identity_aliases/{}:{}".format(
                    binding.kind,
                    binding.value,
                ),
            )
        )
        key = (
            binding.provider_id,
            binding.canonical_node_id,
        )
        previous = local_by_canonical.get(key)
        if previous is not None and previous != binding.local_node_id:
            raise ValueError(
                "canonical node has conflicting local identities",
            )
        local_by_canonical[key] = binding.local_node_id
    for binding in assignments:
        key = (
            binding.provider_id,
            binding.canonical_node_id,
        )
        previous = local_by_canonical.get(key)
        if previous is not None and previous != binding.local_node_id:
            raise ValueError(
                "canonical node has conflicting local identities",
            )
        local_by_canonical[key] = binding.local_node_id
    for field_path, target, role in (
        (
            "/primary_target",
            primary_target,
            AliasRole.PRIMARY,
        ),
        (
            "/control_target",
            control_target,
            AliasRole.CONTROL,
        ),
    ):
        if target is None:
            continue
        derived.extend(
            FieldProvenance(
                field_path,
                binding.provider_id,
                binding.generation,
                "/aliases/{}".format(
                    binding.qualified_name.split(":", 1)[1],
                ),
            )
            for binding in aliases
            if binding.node_id == target and role.value in binding.roles
        )
    for target in primary_targets + control_targets:
        derived.extend(target.provenance)

    for node in topology.get("nodes", ()):
        node_id = str(node["id"])
        path_providers = {}
        for path in node.get("accessPaths", ()):
            path_id = str(path["id"])
            provider_id = path.get("provider")
            if type(provider_id) is not str or provider_id not in generations or path_id in path_providers:
                raise ValueError(
                    "topology access path provenance is ambiguous",
                )
            path_providers[path_id] = provider_id
        for capability in node.get("capabilities", ()):
            capability_name = str(capability["capability"])
            access_path = str(
                capability.get("accessPath", ""),
            )
            provider_id = path_providers.get(access_path)
            if provider_id is None:
                raise ValueError(
                    "topology capability provenance is unavailable",
                )
            local_node_id = local_by_canonical.get(
                (
                    provider_id,
                    node_id,
                )
            )
            prefix = "provider:{}:".format(provider_id)
            if local_node_id is None and node_id.startswith(prefix):
                local_node_id = node_id[len(prefix) :]
            if not local_node_id:
                raise ValueError(
                    "topology capability local identity is unavailable",
                )
            derived.append(
                FieldProvenance(
                    "/topology/nodes/{}/capabilities/{}@{}".format(
                        node_id,
                        capability_name,
                        access_path,
                    ),
                    provider_id,
                    generations[provider_id],
                    "/nodes/{}/capabilities/{}@{}".format(
                        local_node_id,
                        capability_name,
                        access_path,
                    ),
                )
            )
    derived.extend(source for argument in arguments for source in (argument.candidate_provenance + argument.provenance))
    return tuple(
        sorted(
            set(derived),
            key=_provenance_sort_key,
        )
    )


def _validate_plan(plan):
    """Comprehensively validate one complete immutable compiler plan."""
    if type(plan) is not AutoConfigPlan:
        raise ValueError("compiled plan type is invalid")
    _require_digest(plan.digest, "plan digest")
    _validate_json(
        plan.topology,
        "plan topology",
        [0],
    )
    topology = decode_topology(_plain(plan.topology))
    if topology.get("scope") != "site":
        raise ValueError("compiled topology scope is invalid")

    aliases = _require_tuple(plan.aliases, "plan aliases")
    alias_names = set()
    for binding in aliases:
        if type(binding) is not AliasBinding:
            raise ValueError("plan alias type is invalid")
        _require_text(binding.qualified_name, "alias qualified_name")
        _require_text(binding.provider_id, "alias provider_id", maximum=256)
        _require_integer(binding.generation, "alias generation")
        _require_text(binding.node_id, "alias node_id")
        roles = _require_tuple(binding.roles, "alias roles", maximum=3)
        if (
            not roles
            or roles != tuple(sorted(set(roles)))
            or any(
                type(role) is not str
                or role
                not in {
                    AliasRole.REFERENCE.value,
                    AliasRole.PRIMARY.value,
                    AliasRole.CONTROL.value,
                }
                for role in roles
            )
            or not binding.qualified_name.startswith(
                binding.provider_id + ":",
            )
            or binding.qualified_name in alias_names
        ):
            raise ValueError("plan alias invariants are invalid")
        alias_names.add(binding.qualified_name)
    if aliases != tuple(
        sorted(
            aliases,
            key=lambda item: item.qualified_name,
        )
    ):
        raise ValueError("plan aliases are not sorted")

    identities = _require_tuple(
        plan.identity_aliases,
        "plan identity aliases",
    )
    for binding in identities:
        if type(binding) is not IdentityBinding:
            raise ValueError("plan identity alias type is invalid")
        _require_text(binding.provider_id, "identity provider_id", maximum=256)
        _require_integer(binding.generation, "identity generation")
        _require_text(binding.kind, "identity kind")
        _require_text(binding.value, "identity value")
        _require_text(binding.local_node_id, "identity local_node_id")
        _require_text(
            binding.canonical_node_id,
            "identity canonical_node_id",
        )
    if identities != tuple(
        sorted(
            identities,
            key=lambda item: (
                item.provider_id,
                item.kind,
                item.value,
                item.local_node_id,
            ),
        )
    ) or len(
        {
            (
                item.provider_id,
                item.kind,
                item.value,
                item.local_node_id,
            )
            for item in identities
        }
    ) != len(identities):
        raise ValueError("plan identity aliases are not sorted and unique")

    assignments = _require_tuple(
        plan.role_assignments,
        "plan role assignments",
    )
    for binding in assignments:
        if type(binding) is not RoleAssignmentBinding:
            raise ValueError("plan role assignment type is invalid")
        _require_text(binding.provider_id, "role provider_id", maximum=256)
        _require_integer(binding.generation, "role generation")
        if binding.role not in (
            AliasRole.PRIMARY.value,
            AliasRole.CONTROL.value,
        ):
            raise ValueError("plan role value is invalid")
        _require_text(binding.group, "role group")
        _require_integer(binding.index, "role index")
        _require_text(binding.local_node_id, "role local_node_id")
        _require_text(
            binding.canonical_node_id,
            "role canonical_node_id",
        )
    assignment_key = lambda item: (  # noqa: E731
        item.group,
        item.role,
        item.index,
        item.provider_id,
        item.local_node_id,
    )
    if assignments != tuple(sorted(assignments, key=assignment_key)) or len({assignment_key(item) for item in assignments}) != len(assignments):
        raise ValueError("plan role assignments are not sorted and unique")

    topology_node_ids = {str(node["id"]) for node in topology.get("nodes", ())}
    if any(binding.node_id not in topology_node_ids for binding in aliases) or any(binding.canonical_node_id not in topology_node_ids for binding in identities + assignments):
        raise ValueError(
            "plan binding targets a node outside topology",
        )
    canonical_by_local = {}
    for binding in identities:
        key = (
            binding.provider_id,
            binding.local_node_id,
        )
        previous = canonical_by_local.get(key)
        if previous is not None and previous != binding.canonical_node_id:
            raise ValueError(
                "local identity maps to conflicting canonical nodes",
            )
        canonical_by_local[key] = binding.canonical_node_id
    for binding in assignments:
        expected_canonical = canonical_by_local.get(
            (
                binding.provider_id,
                binding.local_node_id,
            ),
            "provider:{}:{}".format(
                binding.provider_id,
                binding.local_node_id,
            ),
        )
        if binding.canonical_node_id != expected_canonical:
            raise ValueError(
                "role assignment canonical node binding is invalid",
            )

    target_sets = (
        ("primary", plan.primary_targets),
        ("control", plan.control_targets),
    )
    for target_name, raw_targets in target_sets:
        targets = _require_tuple(
            raw_targets,
            "{} targets".format(target_name),
        )
        slots = set()
        for target in targets:
            if type(target) is not IndexedRoleTarget:
                raise ValueError(
                    "{} target type is invalid".format(target_name),
                )
            _require_text(
                target.group,
                "{} target group".format(target_name),
            )
            _require_integer(
                target.index,
                "{} target index".format(target_name),
            )
            _require_text(
                target.node_id,
                "{} target node_id".format(target_name),
            )
            _validate_provenance_tuple(
                target.provenance,
                "{} target provenance".format(target_name),
            )
            slot = (target.group, target.index)
            if slot in slots:
                raise ValueError(
                    "{} target slot is duplicated".format(target_name),
                )
            slots.add(slot)
        if targets != tuple(
            sorted(
                targets,
                key=lambda item: (
                    item.group,
                    item.index,
                ),
            )
        ):
            raise ValueError(
                "{} targets are not sorted".format(target_name),
            )
        indexes_by_group = {}
        for target in targets:
            indexes_by_group.setdefault(
                target.group,
                [],
            ).append(target.index)
        if any(indexes != list(range(len(indexes))) for indexes in indexes_by_group.values()):
            raise ValueError(
                "{} target indexes are not contiguous".format(
                    target_name,
                )
            )

    for target_name, target in (
        ("primary_target", plan.primary_target),
        ("control_target", plan.control_target),
    ):
        if target is not None:
            _require_text(target, target_name)

    readiness = plan.materialization_readiness
    if type(readiness) is not MaterializationReadiness:
        raise ValueError("plan readiness type is invalid")
    if type(readiness.ready) is not bool:
        raise ValueError("plan readiness flag is invalid")
    blockers = _require_tuple(
        readiness.blockers,
        "plan readiness blockers",
    )
    if any(_require_text(blocker, "plan readiness blocker") is None for blocker in blockers) or len(set(blockers)) != len(blockers) or readiness.ready != (not blockers):
        raise ValueError("plan readiness invariants are invalid")
    MaterializationReadiness.__post_init__(readiness)

    arguments = _require_tuple(
        plan.config_arguments,
        "plan config arguments",
        maximum=256,
    )
    argument_names = set()
    for argument in arguments:
        if type(argument) is not ProjectedConfigArgument:
            raise ValueError("plan config argument type is invalid")
        _require_text(argument.name, "config argument name", maximum=128)
        _validate_json(
            argument.value,
            "config argument value",
            [0],
        )
        if (
            argument.cardinality
            not in (
                ProjectionCardinality.SCALAR.value,
                ProjectionCardinality.PER_INDEX.value,
            )
            or type(argument.required) is not bool
        ):
            raise ValueError("config argument contract is invalid")
        transforms = _require_tuple(
            argument.transforms,
            "config argument transforms",
            maximum=64,
        )
        if any(
            _require_text(
                transform,
                "config argument transform",
            )
            is None
            for transform in transforms
        ) or len(
            set(transforms)
        ) != len(transforms):
            raise ValueError("config argument transforms are invalid")
        _validate_provenance_tuple(
            argument.provenance,
            "config argument provenance",
        )
        _validate_provenance_tuple(
            argument.candidate_provenance,
            "config candidate provenance",
        )
        candidates = _require_tuple(
            argument.candidates,
            "config argument candidates",
        )
        if not candidates:
            raise ValueError("config argument lacks candidates")
        for candidate in candidates:
            if type(candidate) is not ProjectionCandidate:
                raise ValueError("config candidate type is invalid")
            _require_text(candidate.argument, "candidate argument", maximum=128)
            _require_text(candidate.provider_id, "candidate provider_id", maximum=256)
            _require_integer(candidate.generation, "candidate generation")
            if candidate.argument != argument.name or candidate.role not in (
                AliasRole.PRIMARY.value,
                AliasRole.CONTROL.value,
            ):
                raise ValueError("config candidate ownership is invalid")
            _require_text(candidate.group, "candidate group")
            if candidate.routing not in (
                ProjectionRouting.LEAF.value,
                ProjectionRouting.COORDINATOR.value,
            ) or candidate.cardinality not in (
                ProjectionCardinality.SCALAR.value,
                ProjectionCardinality.PER_INDEX.value,
            ):
                raise ValueError("config candidate routing is invalid")
            if candidate.cardinality != argument.cardinality or type(candidate.required) is not bool or candidate.required != argument.required or candidate.transforms != transforms:
                raise ValueError("config candidate contract differs")
            values = _require_tuple(
                candidate.values,
                "candidate values",
            )
            slot_indexes = _require_tuple(
                candidate.slot_indexes,
                "candidate slot indexes",
            )
            value_kinds = _require_tuple(
                candidate.value_kinds,
                "candidate value kinds",
            )
            capabilities = _require_tuple(
                candidate.capabilities,
                "candidate capabilities",
            )
            identities_value = _require_tuple(
                candidate.identity_selectors,
                "candidate identities",
            )
            access_paths = _require_tuple(
                candidate.access_path_ids,
                "candidate access paths",
            )
            specificities = _require_tuple(
                candidate.specificities,
                "candidate specificities",
            )
            candidate_provenance = _require_tuple(
                candidate.provenance,
                "candidate provenance",
            )
            length = len(values)
            if length == 0 or any(
                len(items) != length
                for items in (
                    slot_indexes,
                    value_kinds,
                    capabilities,
                    identities_value,
                    access_paths,
                    specificities,
                    candidate_provenance,
                )
            ):
                raise ValueError("config candidate vectors differ")
            _require_integer(
                candidate.target_count,
                "candidate target_count",
                minimum=1,
                maximum=MAX_PLAN_ITEMS,
            )
            if (
                len(set(slot_indexes)) != len(slot_indexes)
                or any(type(index) is not int or index < 0 or index >= candidate.target_count for index in slot_indexes)
                or (candidate.cardinality == ProjectionCardinality.SCALAR.value and (candidate.target_count != 1 or slot_indexes != (0,)))
            ):
                raise ValueError("config candidate slots are invalid")
            source_projection_index = None
            for index, value in enumerate(values):
                _validate_json(
                    value,
                    "candidate value",
                    [0],
                )
                kind = value_kinds[index]
                if kind not in (
                    ProjectionValueKind.ENTITY.value,
                    ProjectionValueKind.CONSTANT.value,
                    ProjectionValueKind.NONE.value,
                ):
                    raise ValueError("candidate value kind is invalid")
                if (
                    (kind == ProjectionValueKind.NONE.value and value is not None)
                    or (kind == ProjectionValueKind.ENTITY.value and (type(value) is not str or not value.strip()))
                    or (kind == ProjectionValueKind.CONSTANT.value and (value is None or type(value) not in (str, int, float, bool)))
                    or (candidate.required and kind == ProjectionValueKind.NONE.value)
                ):
                    raise ValueError("candidate value shape is invalid")
                capability = capabilities[index]
                if capability is not None:
                    _require_text(capability, "candidate capability")
                if kind == ProjectionValueKind.ENTITY.value and capability is None:
                    raise ValueError(
                        "candidate entity capability is absent",
                    )
                identity = identities_value[index]
                if identity is not None:
                    if type(identity) is not tuple or len(identity) != 2:
                        raise ValueError("candidate identity is invalid")
                    _require_text(identity[0], "candidate identity kind")
                    _require_text(identity[1], "candidate identity value")
                access_path = access_paths[index]
                if access_path is not None:
                    _require_text(
                        access_path,
                        "candidate access_path_id",
                    )
                _require_integer(
                    specificities[index],
                    "candidate specificity",
                    maximum=2,
                )
                _validate_provenance_tuple(
                    candidate_provenance[index],
                    "candidate slot provenance",
                )
                expected_specificity = 2 if access_path is not None else (1 if identity is not None else 0)
                if specificities[index] != expected_specificity:
                    raise ValueError(
                        "candidate specificity does not match selectors",
                    )
                if len(candidate_provenance[index]) != 1:
                    raise ValueError(
                        "candidate slot provenance is not singular",
                    )
                source = candidate_provenance[index][0]
                expected_field_path = "/projected_config/{}".format(
                    argument.name,
                )
                if candidate.cardinality == ProjectionCardinality.PER_INDEX.value:
                    expected_field_path += "/{}".format(
                        slot_indexes[index],
                    )
                source_parts = source.source_path.split("/")
                if (
                    source.field_path != expected_field_path
                    or source.provider_id != candidate.provider_id
                    or source.generation != candidate.generation
                    or len(source_parts) != 5
                    or source_parts[0] != ""
                    or source_parts[1] != "config_projections"
                    or not source_parts[2].isascii()
                    or not source_parts[2].isdigit()
                    or source_parts[3] != "values"
                    or not source_parts[4].isascii()
                    or not source_parts[4].isdigit()
                    or str(int(source_parts[2])) != source_parts[2]
                    or str(int(source_parts[4])) != source_parts[4]
                    or int(source_parts[4]) != index
                ):
                    raise ValueError(
                        "candidate slot provenance binding is invalid",
                    )
                if source_projection_index is None:
                    source_projection_index = source_parts[2]
                elif source_projection_index != source_parts[2]:
                    raise ValueError(
                        "candidate provenance crosses projections",
                    )
        if candidates != tuple(
            sorted(
                candidates,
                key=lambda item: (
                    item.provider_id,
                    item.generation,
                ),
            )
        ) or len(
            {(item.provider_id, item.generation) for item in candidates}
        ) != len(candidates):
            raise ValueError("config candidates are not sorted and unique")
        first_candidate = candidates[0]
        contract = (
            first_candidate.role,
            first_candidate.group,
            first_candidate.routing,
            first_candidate.cardinality,
            first_candidate.required,
            first_candidate.transforms,
            first_candidate.target_count,
        )
        if any(
            (
                candidate.role,
                candidate.group,
                candidate.routing,
                candidate.cardinality,
                candidate.required,
                candidate.transforms,
                candidate.target_count,
            )
            != contract
            for candidate in candidates
        ):
            raise ValueError("config candidate contracts differ")
        if argument.cardinality == ProjectionCardinality.PER_INDEX.value and (type(argument.value) is not tuple or len(argument.value) != first_candidate.target_count):
            raise ValueError(
                "per-index config argument shape is invalid",
            )
        if argument.override_source is not None:
            _require_text(
                argument.override_source,
                "config override source",
            )
        if argument.name in argument_names:
            raise ValueError("config argument name is duplicated")
        _validate_projection_semantics(
            argument,
            candidates,
        )
        argument_names.add(argument.name)
    if arguments != tuple(sorted(arguments, key=lambda item: item.name)):
        raise ValueError("config arguments are not sorted")

    (
        expected_primary_target,
        expected_control_target,
        expected_primary_targets,
        expected_control_targets,
        has_legacy_assignments,
    ) = _derive_role_outputs(
        aliases,
        identities,
        assignments,
    )
    if plan.primary_target != expected_primary_target or plan.control_target != expected_control_target or plan.primary_targets != expected_primary_targets or plan.control_targets != expected_control_targets:
        raise ValueError(
            "plan targets differ from role bindings",
        )
    expected_blockers = []
    if not expected_primary_targets:
        expected_blockers.append(
            "indexed_primary_targets_missing",
        )
    if not expected_control_targets:
        expected_blockers.append(
            "indexed_control_targets_missing",
        )
    if has_legacy_assignments:
        expected_blockers.append(
            "legacy_role_assignments_present",
        )
    expected_blockers.append(
        "config_projection_bindings_missing",
    )
    if arguments:
        expected_blockers = [blocker for blocker in expected_blockers if blocker != "config_projection_bindings_missing"]
        expected_blockers.append(
            "atomic_materializer_missing",
        )
    expected_readiness = MaterializationReadiness(
        ready=False,
        blockers=tuple(expected_blockers),
    )
    if readiness != expected_readiness:
        raise ValueError(
            "plan readiness differs from compiler rules",
        )
    _validate_projection_topology_bindings(
        topology,
        identities,
        assignments,
        expected_primary_targets,
        expected_control_targets,
        arguments,
    )

    projected_config = plan.projected_config
    _validate_json(
        projected_config,
        "projected_config",
        [0],
    )
    if (
        type(projected_config) is not _MAPPING_PROXY_TYPE
        or set(projected_config) != argument_names
        or any(
            not _exact_equal(
                projected_config[argument.name],
                argument.value,
            )
            for argument in arguments
        )
    ):
        raise ValueError("projected_config binding is invalid")

    plan_fields = _require_tuple(plan.fields, "plan fields")
    field_names = set()
    for field in plan_fields:
        if type(field) is not AutoConfigField:
            raise ValueError("plan field type is invalid")
        _require_text(field.name, "plan field name")
        _validate_json(field.value, "plan field value", [0])
        _validate_provenance_tuple(
            field.provenance,
            "plan field provenance",
        )
        if field.name in field_names:
            raise ValueError("plan field name is duplicated")
        field_names.add(field.name)
    if plan_fields != tuple(sorted(plan_fields, key=lambda item: item.name)):
        raise ValueError("plan fields are not sorted")
    expected_fields = _derive_plan_fields(
        aliases,
        expected_primary_target,
        expected_control_target,
        expected_primary_targets,
        expected_control_targets,
        arguments,
    )
    if not _exact_equal(plan_fields, expected_fields):
        raise ValueError(
            "plan fields differ from compiler outputs",
        )

    plan_provenance = _validate_provenance_tuple(
        plan.provenance,
        "plan provenance",
        sorted_unique=True,
    )
    provider_generations = _require_tuple(
        plan.provider_generations,
        "plan provider generations",
    )
    provider_ids = set()
    for item in provider_generations:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError("plan provider generation is invalid")
        provider_id, generation = item
        _require_text(provider_id, "plan provider_id", maximum=256)
        _require_integer(generation, "plan provider generation")
        if provider_id in provider_ids:
            raise ValueError("plan provider is duplicated")
        provider_ids.add(provider_id)
    if provider_generations != tuple(sorted(provider_generations)):
        raise ValueError("plan provider generations are not sorted")
    generation_map = dict(provider_generations)
    for binding in aliases + identities + assignments:
        if generation_map.get(binding.provider_id) != binding.generation:
            raise ValueError(
                "plan binding generation is outside its cursor",
            )
    for argument in arguments:
        for candidate in argument.candidates:
            if generation_map.get(candidate.provider_id) != candidate.generation:
                raise ValueError(
                    "config candidate generation is outside its cursor",
                )
    expected_plan_provenance = _derive_plan_provenance(
        topology,
        aliases,
        identities,
        assignments,
        expected_primary_target,
        expected_control_target,
        expected_primary_targets,
        expected_control_targets,
        arguments,
        provider_generations,
    )
    if plan_provenance != expected_plan_provenance:
        raise ValueError(
            "plan provenance differs from compiler outputs",
        )
    all_provenance = list(plan.provenance)
    for target in plan.primary_targets + plan.control_targets:
        all_provenance.extend(target.provenance)
    for field in plan_fields:
        all_provenance.extend(field.provenance)
    for argument in arguments:
        all_provenance.extend(argument.provenance)
        all_provenance.extend(argument.candidate_provenance)
        for candidate in argument.candidates:
            for slot_provenance in candidate.provenance:
                all_provenance.extend(slot_provenance)
    for source in all_provenance:
        if source.provider_id == "user_override":
            if source.generation != 0:
                raise ValueError(
                    "user override provenance generation is invalid",
                )
        elif generation_map.get(source.provider_id) != source.generation:
            raise ValueError(
                "plan provenance is outside its provider cursor",
            )

    warnings = _require_tuple(plan.warnings, "plan warnings")
    for warning in warnings:
        _require_text(
            warning,
            "plan warning",
            maximum=MAX_PLAN_TEXT,
            canonical=False,
        )
    projection = _canonical_config(
        projected_config,
        "projected_config",
    )
    if _plan_semantic_digest(plan, projection) != plan.digest:
        raise ValueError("compiled plan semantic digest is invalid")
    return plan


def _validate_compiled_publication(value):
    """Comprehensively validate one complete compiled publication."""
    if type(value) is not CompiledLatticePublication:
        raise ValueError("compiled publication type is invalid")
    _require_integer(
        value.lattice_version,
        "lattice_version",
        minimum=1,
    )
    _require_digest(value.digest, "publication digest")
    plan = _validate_plan(value.plan)
    if plan.digest != value.digest:
        raise ValueError("publication plan digest binding is invalid")

    generations = _require_tuple(
        value.provider_generations,
        "publication provider generations",
    )
    generation_ids = set()
    for item in generations:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError("publication provider generation is invalid")
        provider_id, generation = item
        _require_text(provider_id, "publication provider_id", maximum=256)
        _require_integer(generation, "publication provider generation")
        if provider_id in generation_ids:
            raise ValueError("publication provider is duplicated")
        generation_ids.add(provider_id)
    if generations != tuple(sorted(generations)):
        raise ValueError("publication provider generations are not sorted")

    fingerprints = _require_tuple(
        value.provider_fingerprints,
        "publication provider fingerprints",
    )
    if len(fingerprints) != len(generations):
        raise ValueError("publication fingerprint cursor differs")
    for generation_item, item in zip(generations, fingerprints):
        if type(item) is not tuple or len(item) != 3:
            raise ValueError("publication fingerprint is invalid")
        provider_id, generation, digest = item
        if (provider_id, generation) != generation_item:
            raise ValueError("publication fingerprint cursor differs")
        _require_digest(digest, "publication provider fingerprint")

    requested = _require_tuple(
        value.provider_requested_generations,
        "publication requested generations",
    )
    requested_ids = set()
    for item in requested:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError("publication requested generation is invalid")
        provider_id, generation = item
        _require_text(provider_id, "requested provider_id", maximum=256)
        _require_integer(generation, "requested generation")
        if provider_id in requested_ids:
            raise ValueError("requested provider is duplicated")
        requested_ids.add(provider_id)
    if requested != tuple(sorted(requested)):
        raise ValueError("requested generations are not sorted")
    requested_map = dict(requested)
    if any(requested_map.get(provider_id, -1) < generation for provider_id, generation in generations):
        raise ValueError("requested cursor is behind observed cursor")

    if (value.user_override_generation is None and value.user_override_fingerprint is not None) or (value.user_override_generation is not None and value.user_override_fingerprint is None):
        raise ValueError("override cursor is partial")
    if value.user_override_generation is not None:
        _require_integer(
            value.user_override_generation,
            "override generation",
        )
        _require_digest(
            value.user_override_fingerprint,
            "override fingerprint",
        )
        if value.user_override_requested_generation is None:
            raise ValueError("override requested cursor is absent")
    if value.user_override_requested_generation is not None:
        _require_integer(
            value.user_override_requested_generation,
            "override requested generation",
        )
        if value.user_override_generation is not None and value.user_override_requested_generation < value.user_override_generation:
            raise ValueError("override requested cursor is behind")

    causes = _require_tuple(
        value.invalidation_causes,
        "publication invalidation causes",
    )
    cause_keys = []
    generation_map = dict(generations)
    for cause in causes:
        if type(cause) is not CompilationInvalidation:
            raise ValueError("publication invalidation cause type is invalid")
        if cause.source_type not in (
            "provider",
            "user_override",
        ):
            raise ValueError("invalidation source_type is invalid")
        _require_text(cause.source_id, "invalidation source_id", maximum=256)
        _require_integer(cause.generation, "invalidation generation")
        _require_text(cause.reason, "invalidation reason")
        if cause.source_type == "provider":
            if cause.source_id not in generation_map or cause.source_id not in requested_map or cause.generation > generation_map[cause.source_id] or cause.generation > requested_map[cause.source_id]:
                raise ValueError(
                    "provider invalidation is outside publication cursors",
                )
        elif (
            cause.source_id != "user-overrides"
            or value.user_override_generation is None
            or value.user_override_requested_generation is None
            or cause.generation > value.user_override_generation
            or cause.generation > value.user_override_requested_generation
        ):
            raise ValueError(
                "user override invalidation is outside publication cursors",
            )
        cause_keys.append(
            (
                cause.source_type,
                cause.source_id,
                cause.generation,
                cause.reason,
            )
        )
    if cause_keys != sorted(set(cause_keys)):
        raise ValueError("publication invalidations are not sorted and unique")

    feedback_token = _require_text(
        value.feedback_token,
        "publication feedback_token",
        maximum=MAX_FEEDBACK_TOKEN_LENGTH,
    )
    if feedback_token != "lattice-publication-{}-{}".format(
        value.lattice_version,
        value.digest[:16],
    ):
        raise ValueError(
            "publication feedback_token is not bound to version and digest",
        )
    diagnostics = value.diagnostics
    if type(diagnostics) is not CompiledLatticeDiagnostics:
        raise ValueError("publication diagnostics type is invalid")
    if type(diagnostics.status) is not CompileStatus or diagnostics.status not in (
        CompileStatus.FRESH,
        CompileStatus.DEGRADED,
    ):
        raise ValueError("publication diagnostics status is invalid")
    if type(diagnostics.stale) is not bool or type(diagnostics.degraded) is not bool or diagnostics.stale or diagnostics.degraded != (diagnostics.status is CompileStatus.DEGRADED):
        raise ValueError("publication diagnostic flags are invalid")
    issues = _require_tuple(
        diagnostics.issues,
        "publication diagnostic issues",
    )
    for issue in issues:
        if type(issue) is not CompileIssue:
            raise ValueError("publication diagnostic issue type is invalid")
        _require_text(issue.code, "diagnostic issue code")
        _require_text(
            issue.detail,
            "diagnostic issue detail",
            canonical=False,
        )
        if issue.provider_id is not None:
            _require_text(
                issue.provider_id,
                "diagnostic issue provider_id",
                maximum=256,
            )
    if type(diagnostics.readiness) is not MaterializationReadiness or diagnostics.readiness != plan.materialization_readiness:
        raise ValueError("publication readiness binding is invalid")
    CompiledLatticeDiagnostics.__post_init__(diagnostics)
    CompiledLatticePublication.__post_init__(value)
    if any(dict(generations).get(provider_id) != generation for provider_id, generation in plan.provider_generations):
        raise ValueError("publication cursor does not cover plan")
    return value


def _sync_directory(directory_fd):
    """Fsync an anchored directory where the platform supports it."""
    try:
        os.fsync(directory_fd)
    except OSError as exc:
        if exc.errno not in _SYNC_UNSUPPORTED:
            raise


class _FileRecord:
    """Locked, mode-0600, atomically replaced single-record file."""

    def __init__(self, path, kind, validator):
        """Bind one exact path, record kind, and decoded value validator."""
        path = os.fspath(path)
        if type(path) is not str or not path or "\x00" in path:
            raise ValueError("durable record path is invalid")
        self._path = os.path.abspath(path)
        self._directory_path = os.path.dirname(self._path)
        self._name = os.path.basename(self._path)
        if not self._name or self._name in (".", "..") or len(self._name.encode("utf-8")) > 128:
            raise ValueError("durable record filename is invalid")
        self._kind = kind
        self._validator = validator
        self._lock_name = self._name + ".lock"
        self._thread_lock = threading.RLock()
        os.makedirs(
            self._directory_path,
            mode=0o700,
            exist_ok=True,
        )
        if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NONBLOCK") or os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd or os.unlink not in os.supports_dir_fd:
            raise DurableStateError(
                "durable state requires anchored POSIX dir_fd support",
            )
        descriptor = self._open_directory()
        os.close(descriptor)

    def _open_directory(self):
        """Open and validate one private directory anchor."""
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            descriptor = os.open(self._directory_path, flags)
        except OSError as exc:
            raise DurableStateError(
                "durable state directory is unavailable or unsafe",
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            path_metadata = os.stat(
                self._directory_path,
                follow_symlinks=False,
            )
            if not stat.S_ISDIR(metadata.st_mode) or not stat.S_ISDIR(path_metadata.st_mode) or metadata.st_dev != path_metadata.st_dev or metadata.st_ino != path_metadata.st_ino or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
                raise DurableStateError(
                    "durable state directory must be private and owned",
                )
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    def _open_lock(self, directory_fd):
        """Open the non-symlink lock record and require private permissions."""
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
            raise DurableStateError(
                "durable lock could not be opened safely",
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.geteuid() or metadata.st_nlink != 1:
                raise DurableStateError(
                    "durable lock must be a mode-0600 regular file",
                )
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    @contextmanager
    def _exclusive(self):
        """Hold thread and process exclusion for a complete read/CAS/write."""
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

    def _read_bytes(self, directory_fd):
        """Read one bounded non-symlink private regular file."""
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
            return None
        except (NotImplementedError, OSError, TypeError) as exc:
            raise DurableStateError(
                "durable record could not be opened",
            ) from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_uid != os.geteuid() or metadata.st_nlink != 1 or metadata.st_size > MAX_RECORD_BYTES:
                raise DurableStateCorrupt(
                    "durable record metadata is unsafe",
                )
            chunks = []
            remaining = MAX_RECORD_BYTES + 1
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
            if len(encoded) > MAX_RECORD_BYTES:
                raise DurableStateCorrupt(
                    "durable record exceeds byte bound",
                )
            return encoded
        finally:
            os.close(descriptor)

    def _load_unlocked(self, directory_fd):
        """Load, decode, and validate the current record under exclusion."""
        encoded = self._read_bytes(directory_fd)
        if encoded is None:
            return None
        value = _decode_document(self._kind, encoded)
        try:
            validated = self._validator(value)
            if _encode_document(self._kind, validated) != encoded:
                raise ValueError(
                    "decoded durable record is not exact",
                )
            return validated
        except (
            AttributeError,
            OverflowError,
            RecursionError,
            TypeError,
            ValueError,
        ) as exc:
            raise DurableStateCorrupt(
                "durable record type or invariants are invalid",
            ) from exc

    def load(self):
        """Return the exact current value or ``None``."""
        with self._exclusive() as directory_fd:
            return self._load_unlocked(directory_fd)

    def _write_unlocked(self, directory_fd, value):
        """Atomically replace and sync one complete mode-0600 record."""
        encoded = _encode_document(self._kind, value)
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
            except (
                NotImplementedError,
                OSError,
                TypeError,
            ) as exc:
                raise DurableStateError(
                    "durable temporary record could not be created",
                ) from exc
            temporary_name = candidate
            break
        if descriptor is None or temporary_name is None:
            raise DurableStateError(
                "durable temporary name allocation is exhausted",
            )
        try:
            os.fchmod(descriptor, 0o600)
            offset = 0
            while offset < len(encoded):
                written = os.write(descriptor, encoded[offset:])
                if written <= 0:
                    raise DurableStateError(
                        "durable record write made no progress",
                    )
                offset += written
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.replace(
                    temporary_name,
                    self._name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                )
            except (
                NotImplementedError,
                OSError,
                TypeError,
            ) as exc:
                raise DurableStateError(
                    "durable record replace failed",
                ) from exc
            temporary_name = None
            _sync_directory(directory_fd)
            readback = self._read_bytes(directory_fd)
            if readback != encoded:
                raise DurableStateError(
                    "durable record exact readback failed",
                )
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

    def compare_and_store(self, expected, replacement):
        """Atomically store replacement when exact encoded state matches."""
        expected = self._validator(expected)
        replacement = self._validator(replacement)
        with self._exclusive() as directory_fd:
            current = self._load_unlocked(directory_fd)
            if _encode_document(self._kind, current) != _encode_document(self._kind, expected):
                return False
            self._write_unlocked(
                directory_fd,
                replacement,
            )
            return True


class FileFragmentAdapterStateStore(FragmentAdapterStateStore):
    """Filesystem-backed exact CAS store for one provider fragment."""

    def __init__(self, path):
        """Create an idle provider fragment store at one explicit path."""
        self._record = _FileRecord(
            path,
            "fragment-adapter-state",
            self._validate,
        )

    @staticmethod
    def _validate(value):
        """Reconstruct one exact fragment state or accept absence."""
        if value is None:
            return None
        return _validated_fragment_state(value)

    def load(self):
        """Return the current validated fragment state or ``None``."""
        return self._record.load()

    def compare_and_store(self, expected, replacement):
        """Atomically replace one exact provider fragment state."""
        return self._record.compare_and_store(
            expected,
            replacement,
        )


class FileCompiledLatticeStateStore(CompiledLatticeStateStore):
    """Filesystem-backed exact version CAS for compiled publications."""

    def __init__(self, path):
        """Create an idle compiled-publication store."""
        self._record = _FileRecord(
            path,
            "compiled-lattice-publication",
            self._validate,
        )

    @staticmethod
    def _validate(value):
        """Revalidate one exact publication or accept absence."""
        if value is None:
            return None
        return _validate_compiled_publication(value)

    def load(self):
        """Return the current validated publication or ``None``."""
        return self._record.load()

    def compare_and_publish(self, expected_version, publication):
        """Publish exactly expected_version + 1 under a filesystem CAS."""
        if type(expected_version) is not int or expected_version < 0 or expected_version >= MAX_SEQUENCE:
            raise ValueError(
                "expected_version must be a bounded non-negative integer",
            )
        publication = self._validate(publication)
        if publication is None or publication.lattice_version != expected_version + 1:
            raise ValueError(
                "publication version must be exactly expected_version + 1",
            )
        with self._record._exclusive() as directory_fd:
            current = self._record._load_unlocked(
                directory_fd,
            )
            current_version = current.lattice_version if current is not None else 0
            if current_version != expected_version:
                return False
            self._record._write_unlocked(
                directory_fd,
                publication,
            )
            return True


class FileAtomicMaterializationStateStore(
    AtomicMaterializationStateStore,
):
    """Secret-free materializer checkpoint plus durable fence allocator."""

    def __init__(self, path):
        """Create an idle state store with a separately durable fence."""
        self._record = _FileRecord(
            path,
            "atomic-materialization-state",
            self._validate,
        )
        self._fence_record = _FileRecord(
            os.fspath(path) + ".fence",
            "atomic-materialization-fence",
            self._validate_fence,
        )

    @staticmethod
    def _validate(value):
        """Revalidate one exact secret-free checkpoint or accept absence."""
        if value is None:
            return None
        if type(value) is not AtomicMaterializationState:
            raise ValueError(
                "materializer state must be AtomicMaterializationState or None",
            )
        AtomicMaterializationState.__post_init__(value)
        return value

    @staticmethod
    def _validate_fence(value):
        """Validate the optional positive globally increasing fence."""
        if value is None:
            return None
        if type(value) is not int or value < 1 or value > MAX_SEQUENCE:
            raise ValueError("materializer fence is invalid")
        return value

    def load(self):
        """Return the current validated secret-free checkpoint."""
        return self._record.load()

    def compare_and_store(self, expected, replacement):
        """Atomically replace one exact checkpoint."""
        return self._record.compare_and_store(
            expected,
            replacement,
        )

    def allocate_fence(self):
        """Durably allocate a monotonic positive fence before returning it."""
        with self._fence_record._exclusive() as directory_fd:
            fence = self._fence_record._load_unlocked(
                directory_fd,
            )
            if fence is None:
                fence = 0
            state_lock = self._record._open_lock(directory_fd)
            try:
                fcntl.flock(state_lock, fcntl.LOCK_EX)
                state = self._record._load_unlocked(
                    directory_fd,
                )
                if state is not None:
                    fence = max(
                        fence,
                        state.reservation_key.fence,
                    )
            finally:
                try:
                    fcntl.flock(state_lock, fcntl.LOCK_UN)
                finally:
                    os.close(state_lock)
            if fence >= MAX_SEQUENCE:
                raise DurableStateError(
                    "materializer fence sequence is exhausted",
                )
            allocated = fence + 1
            self._fence_record._write_unlocked(
                directory_fd,
                allocated,
            )
            return allocated


@dataclass(frozen=True, repr=False)
class OpaqueTargetJournalEntry:
    """One target-owned fenced opaque transaction journal revision."""

    revision: int
    key: TargetOperationKey
    payload: bytes
    payload_digest: str = ""

    def __post_init__(self):
        """Bind one bounded opaque byte payload to its fence and digest."""
        if type(self.revision) is not int or self.revision < 1 or self.revision > MAX_SEQUENCE:
            raise ValueError("target journal revision is invalid")
        if type(self.key) is not TargetOperationKey:
            raise ValueError("target journal key is invalid")
        TargetOperationKey.__post_init__(self.key)
        if type(self.payload) is not bytes or len(self.payload) > MAX_OPAQUE_PAYLOAD_BYTES:
            raise ValueError("target journal payload is invalid")
        digest = hashlib.sha256(self.payload).hexdigest()
        if self.payload_digest not in ("", digest):
            raise ValueError("target journal payload digest is invalid")
        object.__setattr__(self, "payload_digest", digest)

    def __repr__(self):
        """Exclude the target-owned secret-bearing payload."""
        return ("OpaqueTargetJournalEntry(revision={!r}, key={!r}, " "payload_digest={!r}, payload_bytes={!r})").format(
            self.revision,
            self.key,
            self.payload_digest,
            len(self.payload),
        )


_DATACLASS_TYPES[OpaqueTargetJournalEntry.__name__] = OpaqueTargetJournalEntry


class FileOpaqueTargetJournal:
    """Private fenced CAS store that never interprets target journal bytes."""

    def __init__(self, path):
        """Create an idle target-owned journal at one explicit path."""
        self._record = _FileRecord(
            path,
            "opaque-target-journal",
            self._validate,
        )

    @staticmethod
    def _validate(value):
        """Revalidate one exact opaque journal entry or accept absence."""
        if value is None:
            return None
        if type(value) is not OpaqueTargetJournalEntry:
            raise ValueError(
                "target journal must be OpaqueTargetJournalEntry or None",
            )
        OpaqueTargetJournalEntry.__post_init__(value)
        return value

    def load(self):
        """Return the current opaque entry or ``None``."""
        return self._record.load()

    def compare_and_store(self, expected, replacement):
        """CAS one next revision without allowing fence/key regression."""
        expected = self._validate(expected)
        replacement = self._validate(replacement)
        expected_revision = expected.revision if expected is not None else 0
        if replacement is None or replacement.revision != expected_revision + 1:
            raise ValueError(
                "target journal replacement must be the next revision",
            )
        if expected is not None and (replacement.key.fence < expected.key.fence or (replacement.key.fence == expected.key.fence and replacement.key != expected.key)):
            raise ValueError(
                "target journal replacement regresses its operation fence",
            )
        return self._record.compare_and_store(
            expected,
            replacement,
        )
