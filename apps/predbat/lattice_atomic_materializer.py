# -----------------------------------------------------------------------------
# Predbat Home Battery System - atomic Lattice auto-config materializer
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Default-off atomic materialization of immutable Lattice auto-config plans.

The materializer first wins a durable secret-free reservation, then asks the
target to retain the secret-bearing preimage and candidate behind an opaque
handle.  Every target mutation is fenced, recoverable by reservation key, and
sealed before the materializer reports a terminal result.
"""

# cspell:ignore autoconfig dedupe preimage readback

import hashlib
import json
import math
import re
import threading
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from lattice_autoconfig import (
    AutoConfigPlan,
    FieldProvenance,
    MaterializationReadiness,
    MaterializationRequest,
    ProjectedConfigArgument,
    _canonical_json as _compiler_canonical_json,
    _semantic_topology,
)


MAX_SEQUENCE = (1 << 63) - 1
MAX_CONFIG_INTEGER = MAX_SEQUENCE
MAX_CONFIG_ENTRIES = 256
MAX_CONFIG_KEY_LENGTH = 128
MAX_CONFIG_VALUE_BYTES = 16384
MAX_CONFIG_BYTES = 262144
MAX_LIST_ITEMS = 256
MAX_VALUE_DEPTH = 4
MAX_FEEDBACK_TOKEN_LENGTH = 256
MAX_PROVENANCE_ENTRIES = 4096
MAX_PROVENANCE_STRING_LENGTH = 1024
MAX_PROVIDER_ID_LENGTH = 256
MAX_OPAQUE_HANDLE_LENGTH = 256
MAX_SEMANTIC_ITEMS = 4096
MAX_SEMANTIC_DEPTH = 12
MAX_SEMANTIC_NODES = 16384
MAX_SEMANTIC_STRING_LENGTH = 16384
MAX_SEMANTIC_BYTES = 1048576
MAX_RESERVATION_RETRIES = 32

_CONFIG_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_HANDLE = re.compile(r"^[A-Za-z0-9_.:@-]+$")
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))


class AtomicMaterializationError(RuntimeError):
    """Base error for fail-closed materialization."""


class MaterializationValidationError(AtomicMaterializationError):
    """An immutable request or durable record is unsafe."""


class MaterializationStorageError(AtomicMaterializationError):
    """Durable reservation or CAS state could not be established exactly."""


class MaterializationTargetError(AtomicMaterializationError):
    """The target lifecycle could not be acknowledged exactly."""


class MaterializationPhase(Enum):
    """Secret-free durable coordinator phase."""

    RESERVED = "reserved"
    PREPARED = "prepared"
    APPLYING = "applying"
    APPLIED = "applied"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    UNKNOWN = "unknown"


class TargetOperationPhase(Enum):
    """Target-owned durable operation lifecycle."""

    PREPARED = "prepared"
    APPLYING = "applying"
    APPLIED = "applied"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    SEALED = "sealed"
    UNKNOWN = "unknown"


class TargetTerminalPhase(Enum):
    """Meaning of one acknowledged sealed target operation."""

    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    UNKNOWN = "unknown"


class MaterializationStatus(Enum):
    """Caller-visible result of apply, recovery, or rollback."""

    DISABLED = "disabled"
    APPLIED = "applied"
    UNCHANGED = "unchanged"
    NOT_APPLIED = "not_applied"
    UNKNOWN = "unknown"
    SUPERSEDED = "superseded"
    ROLLED_BACK = "rolled_back"


class AtomicMaterializationStateStore:
    """Injected durable fencing and exact compare-and-store protocol."""

    def allocate_fence(self):
        """Durably allocate a globally increasing bounded positive fence."""
        raise NotImplementedError

    def load(self):
        """Return the current exact state or ``None``."""
        raise NotImplementedError

    def compare_and_store(self, expected, replacement):
        """Atomically replace exact ``expected`` state."""
        raise NotImplementedError


class WholeConfigTarget:
    """Target-owned durable, fenced, secret-bearing transaction protocol."""

    def prepare(self, fence, projection_digest, projection):
        """Create one PREPARED operation for a winning reservation."""
        raise NotImplementedError

    def lookup(self, fence, projection_digest):
        """Recover a durable operation by its reservation key."""
        raise NotImplementedError

    def apply(self, operation):
        """Run PREPARED -> APPLYING -> APPLIED with a commit fence recheck."""
        raise NotImplementedError

    def rollback(self, operation):
        """Run sealed APPLIED -> ROLLING_BACK -> ROLLED_BACK."""
        raise NotImplementedError

    def seal(self, operation):
        """Seal a stable operation and reject later apply replay."""
        raise NotImplementedError

    def abort(self, fence, projection_digest):
        """Seal an unprepared/prepared reservation as ROLLED_BACK."""
        raise NotImplementedError

    def invalidate(self, operation_key):
        """Fence an operation and acknowledge one SEALED terminal result."""
        raise NotImplementedError


@dataclass(frozen=True, repr=False)
class PublishedMaterializationRequest:
    """Materialization handoff plus its immutable publication cursor."""

    request: MaterializationRequest
    publication_digest: str
    publication_version: int

    def __post_init__(self):
        """Validate non-secret envelope fields without traversing the plan."""
        if type(self.request) is not MaterializationRequest:
            raise ValueError("request type is invalid")
        if type(self.request.plan) is not AutoConfigPlan:
            raise ValueError("plan type is invalid")
        _validate_digest(self.publication_digest, "publication_digest")
        if self.publication_digest != self.request.plan.digest:
            raise ValueError("publication digest binding is invalid")
        _validate_sequence(
            self.publication_version,
            "publication_version",
            minimum=1,
        )
        _validate_feedback_token(self.request.feedback_token)

    def __repr__(self):
        """Exclude projection values and feedback tokens from diagnostics."""
        return ("PublishedMaterializationRequest(" "plan_digest={!r}, publication_version={!r})").format(self.publication_digest, self.publication_version)


@dataclass(frozen=True)
class TargetOperationKey:
    """Bounded durable key shared by reservation state and target journal."""

    fence: int
    projection_digest: str

    def __post_init__(self):
        """Validate a reservation lookup key."""
        _validate_sequence(self.fence, "fence", minimum=1)
        _validate_digest(self.projection_digest, "projection_digest")


@dataclass(frozen=True)
class TargetOperationSnapshot:
    """Secret-free target journal observation."""

    key: TargetOperationKey
    phase: TargetOperationPhase
    handle: object = None
    preimage_digest: object = None
    candidate_digest: object = None
    changed: object = None
    terminal_phase: object = None

    def __post_init__(self):
        """Validate phase-specific opaque target metadata."""
        if type(self.key) is not TargetOperationKey:
            raise ValueError("target operation key type is invalid")
        TargetOperationKey.__post_init__(self.key)
        if not isinstance(self.phase, TargetOperationPhase):
            raise ValueError("target operation phase is invalid")
        prepared_values = (
            self.handle,
            self.preimage_digest,
            self.candidate_digest,
            self.changed,
        )
        has_prepared_values = all(value is not None for value in prepared_values)
        has_partial_values = any(value is not None for value in prepared_values)
        if has_partial_values and not has_prepared_values:
            raise ValueError("target operation metadata is partial")
        if has_prepared_values:
            _validate_handle(self.handle)
            _validate_digest(self.preimage_digest, "preimage_digest")
            _validate_digest(self.candidate_digest, "candidate_digest")
            if type(self.changed) is not bool:
                raise ValueError("target changed flag is invalid")
            if self.changed != (self.preimage_digest != self.candidate_digest):
                raise ValueError("target changed flag contradicts digests")
        elif self.phase is not TargetOperationPhase.SEALED:
            raise ValueError("unprepared target operation must be sealed")

        if self.phase is TargetOperationPhase.SEALED:
            if not isinstance(self.terminal_phase, TargetTerminalPhase):
                raise ValueError("sealed target operation lacks terminal phase")
            if not has_prepared_values and self.terminal_phase is not TargetTerminalPhase.ROLLED_BACK:
                raise ValueError("unprepared seal must be rolled back")
        elif self.terminal_phase is not None:
            raise ValueError("unsealed target operation has terminal phase")


@dataclass(frozen=True)
class AtomicMaterializationState:
    """Bounded secret-free durable reservation and transaction checkpoint."""

    revision: int
    phase: MaterializationPhase
    plan_digest: str
    publication_digest: str
    publication_version: int
    projection_digest: str
    feedback_digest: str
    provenance_digest: str
    provenance_count: int
    reservation_key: TargetOperationKey
    prior_operation_key: object = None
    operation: object = None
    applied_plan_digest: object = None
    applied_publication_digest: object = None
    applied_publication_version: object = None
    integrity_digest: str = ""

    def __post_init__(self):
        """Verify every checkpoint field and its complete fingerprint."""
        _validate_sequence(self.revision, "revision", minimum=1)
        if not isinstance(self.phase, MaterializationPhase):
            raise ValueError("state phase is invalid")
        _validate_digest(self.plan_digest, "plan_digest")
        _validate_digest(self.publication_digest, "publication_digest")
        if self.plan_digest != self.publication_digest:
            raise ValueError("state plan/publication binding is invalid")
        _validate_sequence(
            self.publication_version,
            "publication_version",
            minimum=1,
        )
        _validate_digest(self.projection_digest, "projection_digest")
        _validate_digest(self.feedback_digest, "feedback_digest")
        _validate_digest(self.provenance_digest, "provenance_digest")
        _validate_sequence(
            self.provenance_count,
            "provenance_count",
            minimum=0,
            maximum=MAX_PROVENANCE_ENTRIES,
        )
        if type(self.reservation_key) is not TargetOperationKey:
            raise ValueError("reservation key type is invalid")
        TargetOperationKey.__post_init__(self.reservation_key)
        if self.reservation_key.projection_digest != self.projection_digest:
            raise ValueError("reservation projection binding is invalid")
        if self.prior_operation_key is not None:
            if type(self.prior_operation_key) is not TargetOperationKey:
                raise ValueError("prior operation key type is invalid")
            TargetOperationKey.__post_init__(self.prior_operation_key)
            if self.prior_operation_key.fence >= self.reservation_key.fence:
                raise ValueError("prior operation fence is not older")

        if self.phase is MaterializationPhase.RESERVED:
            if self.operation is not None:
                raise ValueError("reserved state cannot have target operation")
        else:
            if type(self.operation) is not TargetOperationSnapshot:
                raise ValueError("transaction state lacks target operation")
            TargetOperationSnapshot.__post_init__(self.operation)
            if self.operation.key != self.reservation_key:
                raise ValueError("target operation key binding is invalid")
            if self.phase is MaterializationPhase.PREPARED and self.operation.phase is not TargetOperationPhase.PREPARED:
                raise ValueError("PREPARED state target phase is invalid")
            if self.phase is MaterializationPhase.APPLYING and (
                self.operation.phase
                not in (
                    TargetOperationPhase.PREPARED,
                    TargetOperationPhase.APPLYING,
                )
            ):
                raise ValueError("APPLYING state target phase is invalid")
            if self.phase is MaterializationPhase.ROLLING_BACK and not (self.operation.phase is TargetOperationPhase.SEALED and self.operation.terminal_phase is TargetTerminalPhase.APPLIED):
                raise ValueError("ROLLING_BACK state target phase is invalid")

        if self.phase is MaterializationPhase.APPLIED:
            if self.operation.phase is not TargetOperationPhase.SEALED or self.operation.terminal_phase is not TargetTerminalPhase.APPLIED:
                raise ValueError("APPLIED requires sealed target APPLIED")
            expected_applied = (
                self.plan_digest,
                self.publication_digest,
                self.publication_version,
            )
        else:
            expected_applied = (None, None, None)
            if self.phase is MaterializationPhase.ROLLED_BACK and (self.operation.phase is not TargetOperationPhase.SEALED or self.operation.terminal_phase is not TargetTerminalPhase.ROLLED_BACK):
                raise ValueError("ROLLED_BACK requires sealed target ROLLED_BACK")
        if (
            self.applied_plan_digest,
            self.applied_publication_digest,
            self.applied_publication_version,
        ) != expected_applied:
            raise ValueError("state applied cursor is invalid")

        if self.integrity_digest != _state_integrity(self):
            raise ValueError("state integrity digest is invalid")

    @classmethod
    def reserve(
        cls,
        revision,
        envelope,
        projection_digest,
        feedback_digest,
        provenance_digest,
        provenance_count,
        fence,
        prior_operation_key,
    ):
        """Create one secret-free RESERVED checkpoint."""
        return _new_state(
            revision=revision,
            phase=MaterializationPhase.RESERVED,
            plan_digest=envelope.request.plan.digest,
            publication_digest=envelope.publication_digest,
            publication_version=envelope.publication_version,
            projection_digest=projection_digest,
            feedback_digest=feedback_digest,
            provenance_digest=provenance_digest,
            provenance_count=provenance_count,
            reservation_key=TargetOperationKey(
                fence,
                projection_digest,
            ),
            prior_operation_key=prior_operation_key,
            operation=None,
            applied_plan_digest=None,
            applied_publication_digest=None,
            applied_publication_version=None,
        )

    def with_operation(self, phase, operation):
        """Advance one reservation using acknowledged target metadata."""
        applied = phase is MaterializationPhase.APPLIED
        return _new_state(
            revision=_next_sequence(self.revision, "revision"),
            phase=phase,
            plan_digest=self.plan_digest,
            publication_digest=self.publication_digest,
            publication_version=self.publication_version,
            projection_digest=self.projection_digest,
            feedback_digest=self.feedback_digest,
            provenance_digest=self.provenance_digest,
            provenance_count=self.provenance_count,
            reservation_key=self.reservation_key,
            prior_operation_key=self.prior_operation_key,
            operation=operation,
            applied_plan_digest=self.plan_digest if applied else None,
            applied_publication_digest=(self.publication_digest if applied else None),
            applied_publication_version=(self.publication_version if applied else None),
        )

    def advance_publication(
        self,
        envelope,
        feedback_digest,
        provenance_digest,
        provenance_count,
    ):
        """Advance an unchanged sealed APPLIED publication cursor."""
        if self.phase is not MaterializationPhase.APPLIED:
            raise ValueError("publication advance requires APPLIED state")
        if envelope.request.plan.digest != self.plan_digest:
            raise ValueError("publication advance changes plan digest")
        return _new_state(
            revision=_next_sequence(self.revision, "revision"),
            phase=MaterializationPhase.APPLIED,
            plan_digest=self.plan_digest,
            publication_digest=envelope.publication_digest,
            publication_version=envelope.publication_version,
            projection_digest=self.projection_digest,
            feedback_digest=feedback_digest,
            provenance_digest=provenance_digest,
            provenance_count=provenance_count,
            reservation_key=self.reservation_key,
            prior_operation_key=self.prior_operation_key,
            operation=self.operation,
            applied_plan_digest=self.plan_digest,
            applied_publication_digest=envelope.publication_digest,
            applied_publication_version=envelope.publication_version,
        )


@dataclass(frozen=True)
class AtomicMaterializationResult:
    """Immutable outcome and durable state observation."""

    status: MaterializationStatus
    state: object
    target_written: bool = False

    def __post_init__(self):
        """Validate result shape."""
        if not isinstance(self.status, MaterializationStatus):
            raise ValueError("result status is invalid")
        if self.state is not None and type(self.state) is not AtomicMaterializationState:
            raise ValueError("result state type is invalid")
        if type(self.target_written) is not bool:
            raise ValueError("target_written is invalid")


def _validate_sequence(value, name, minimum=0, maximum=MAX_SEQUENCE):
    """Validate one bounded non-boolean integer before serialization."""
    if type(value) is not int or value < minimum or value > maximum:
        raise ValueError("{} is outside bounded integer range".format(name))
    return value


def _next_sequence(value, name):
    """Increment one bounded sequence without overflow."""
    _validate_sequence(value, name, minimum=0)
    if value == MAX_SEQUENCE:
        raise ValueError("{} exhausted".format(name))
    return value + 1


def _validate_digest(value, name):
    """Validate one lowercase SHA-256 digest."""
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError("{} is not a SHA-256 digest".format(name))
    return value


def _validate_handle(value):
    """Validate one bounded non-secret opaque handle."""
    if type(value) is not str or not value or len(value) > MAX_OPAQUE_HANDLE_LENGTH or _OPAQUE_HANDLE.fullmatch(value) is None:
        raise ValueError("target handle is invalid")
    return value


def _validate_feedback_token(value):
    """Validate one bounded feedback token without retaining it."""
    if type(value) is not str or not value or value != value.strip() or len(value) > MAX_FEEDBACK_TOKEN_LENGTH:
        raise ValueError("feedback token is invalid")
    return value


def _canonical_value(value, depth=0):
    """Detach one bounded JSON scalar or list value."""
    if depth > MAX_VALUE_DEPTH:
        raise ValueError("config value depth exceeds limit")
    if value is None or type(value) is bool:
        canonical = value
    elif type(value) is int:
        _validate_sequence(
            abs(value),
            "config integer",
            minimum=0,
            maximum=MAX_CONFIG_INTEGER,
        )
        canonical = value
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError("config float is not finite")
        canonical = value
    elif type(value) is str:
        canonical = value
    elif type(value) in (list, tuple):
        if len(value) > MAX_LIST_ITEMS:
            raise ValueError("config list exceeds item limit")
        canonical = tuple(_canonical_value(item, depth + 1) for item in value)
    else:
        raise ValueError("config value type is invalid")
    if len(_canonical_json(canonical).encode("utf-8")) > MAX_CONFIG_VALUE_BYTES:
        raise ValueError("config value exceeds byte limit")
    return canonical


def _plain_value(value):
    """Convert immutable tuples into canonical JSON arrays."""
    if type(value) is tuple:
        return [_plain_value(item) for item in value]
    return value


def _canonical_json(value):
    """Return deterministic strict JSON for already-bounded values."""
    if type(value) in (dict, _MAPPING_PROXY_TYPE):
        value = {key: _plain_value(item) for key, item in value.items()}
    else:
        value = _plain_value(value)
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _bounded_mapping_items(value, name, maximum):
    """Consume at most ``maximum + 1`` mapping items."""
    if type(value) not in (dict, _MAPPING_PROXY_TYPE):
        raise ValueError("{} mapping type is invalid".format(name))
    iteration_failed = False
    try:
        iterator = iter(value.items())
        items = []
        for _index in range(maximum + 1):
            try:
                items.append(next(iterator))
            except StopIteration:
                break
    except Exception:
        iteration_failed = True
        items = []
    if iteration_failed:
        raise ValueError("{} mapping iteration failed".format(name))
    if len(items) > maximum:
        raise ValueError("{} mapping exceeds item limit".format(name))
    return items


def _canonical_config(value, name):
    """Detach one bounded plain configuration without trusting Mapping.len."""
    canonical = {}
    for key, item in _bounded_mapping_items(
        value,
        name,
        MAX_CONFIG_ENTRIES,
    ):
        if type(key) is not str or not key or key != key.strip() or len(key) > MAX_CONFIG_KEY_LENGTH or _CONFIG_KEY.fullmatch(key) is None:
            raise ValueError("{} contains invalid config key".format(name))
        if key in canonical:
            raise ValueError("{} contains duplicate config key".format(name))
        canonical[key] = _canonical_value(item)
    canonical = dict(sorted(canonical.items()))
    if len(_canonical_json(canonical).encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ValueError("{} exceeds byte limit".format(name))
    return MappingProxyType(canonical)


def _configs_equal(left, right):
    """Compare configs without bool/int/float equality ambiguity."""
    return _canonical_json(left) == _canonical_json(right)


def _config_digest(config):
    """Digest one already-canonical complete config."""
    return hashlib.sha256(_canonical_json(config).encode("utf-8")).hexdigest()


def _feedback_digest(token):
    """Retain only a one-way audit fingerprint of a feedback token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _require_tuple(value, name, maximum=MAX_SEMANTIC_ITEMS):
    """Reject iterable coercion and bound an existing exact tuple."""
    if type(value) is not tuple:
        raise ValueError("{} container type is invalid".format(name))
    if len(value) > maximum:
        raise ValueError("{} exceeds item limit".format(name))
    return value


def _validate_provenance(value):
    """Validate one bounded exact provenance tuple."""
    provenance = _require_tuple(
        value,
        "provenance",
        MAX_PROVENANCE_ENTRIES,
    )
    for source in provenance:
        if type(source) is not FieldProvenance:
            raise ValueError("provenance element type is invalid")
        for item in (
            source.field_path,
            source.provider_id,
            source.source_path,
        ):
            if type(item) is not str or not item or len(item) > MAX_PROVENANCE_STRING_LENGTH:
                raise ValueError("provenance string is invalid")
        _validate_sequence(
            source.generation,
            "provenance generation",
            minimum=0,
        )
    return provenance


def _validate_provider_generations(value):
    """Validate the full bounded provider generation cursor."""
    generations = _require_tuple(
        value,
        "provider_generations",
        MAX_PROVENANCE_ENTRIES,
    )
    seen = set()
    for item in generations:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError("provider generation item is invalid")
        provider_id, generation = item
        if type(provider_id) is not str or not provider_id or len(provider_id) > MAX_PROVIDER_ID_LENGTH:
            raise ValueError("provider id is invalid")
        _validate_sequence(
            generation,
            "provider generation",
            minimum=0,
        )
        if provider_id in seen:
            raise ValueError("provider generation is duplicated")
        seen.add(provider_id)
    if generations != tuple(sorted(generations)):
        raise ValueError("provider generations are not sorted")
    return generations


def _provenance_payload(provenance):
    """Return deterministic bounded audit data for provenance."""
    return [
        {
            "field_path": source.field_path,
            "provider_id": source.provider_id,
            "generation": source.generation,
            "source_path": source.source_path,
        }
        for source in provenance
    ]


def _provenance_digest(provenance):
    """Retain provenance accountability without raw durable paths."""
    return hashlib.sha256(_canonical_json(_provenance_payload(provenance)).encode("utf-8")).hexdigest()


def _sanitize_semantic(value, name, budget, depth=0):
    """Recursively detach bounded compiler semantics before serialization."""
    if depth > MAX_SEMANTIC_DEPTH:
        raise ValueError("{} depth exceeds limit".format(name))
    budget[0] += 1
    if budget[0] > MAX_SEMANTIC_NODES:
        raise ValueError("{} node count exceeds limit".format(name))

    if type(value) in (dict, _MAPPING_PROXY_TYPE):
        sanitized = {}
        for key, item in _bounded_mapping_items(
            value,
            name,
            MAX_SEMANTIC_ITEMS,
        ):
            if type(key) is not str or not key or len(key) > MAX_SEMANTIC_STRING_LENGTH:
                raise ValueError("{} key is invalid".format(name))
            if key in sanitized:
                raise ValueError("{} key is duplicated".format(name))
            sanitized[key] = _sanitize_semantic(
                item,
                name,
                budget,
                depth + 1,
            )
        return dict(sorted(sanitized.items()))
    if type(value) in (tuple, list):
        if len(value) > MAX_SEMANTIC_ITEMS:
            raise ValueError("{} sequence exceeds item limit".format(name))
        return tuple(_sanitize_semantic(item, name, budget, depth + 1) for item in value)
    if type(value) is frozenset:
        if len(value) > MAX_SEMANTIC_ITEMS:
            raise ValueError("{} set exceeds item limit".format(name))
        return tuple(sorted(_sanitize_semantic(item, name, budget, depth + 1) for item in value))
    if isinstance(value, Enum):
        return _sanitize_semantic(value.value, name, budget, depth + 1)
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        _validate_sequence(
            abs(value),
            "{} integer".format(name),
            minimum=0,
        )
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("{} float is not finite".format(name))
        return value
    if type(value) is str:
        if len(value) > MAX_SEMANTIC_STRING_LENGTH:
            raise ValueError("{} string exceeds limit".format(name))
        return value
    raise ValueError("{} value type is invalid".format(name))


def _plan_semantic_digest(plan, projection):
    """Recompute the compiler digest from recursively bounded semantics."""
    aliases = _require_tuple(plan.aliases, "aliases")
    identity_aliases = _require_tuple(
        plan.identity_aliases,
        "identity_aliases",
    )
    role_assignments = _require_tuple(
        plan.role_assignments,
        "role_assignments",
    )
    primary_targets = _require_tuple(
        plan.primary_targets,
        "primary_targets",
    )
    control_targets = _require_tuple(
        plan.control_targets,
        "control_targets",
    )
    arguments = _require_tuple(
        plan.config_arguments,
        "config_arguments",
        MAX_CONFIG_ENTRIES,
    )
    fields = _require_tuple(plan.fields, "fields")
    topology = _sanitize_semantic(
        plan.topology,
        "topology",
        [0],
    )
    semantic = {
        "topology": _semantic_topology(topology),
        "aliases": [
            {
                "qualified_name": binding.qualified_name,
                "node_id": binding.node_id,
                "roles": binding.roles,
            }
            for binding in aliases
        ],
        "identity_aliases": [
            {
                "provider_id": binding.provider_id,
                "kind": binding.kind,
                "value": binding.value,
                "local_node_id": binding.local_node_id,
                "canonical_node_id": binding.canonical_node_id,
            }
            for binding in identity_aliases
        ],
        "role_assignments": [
            {
                "provider_id": binding.provider_id,
                "role": binding.role,
                "group": binding.group,
                "index": binding.index,
                "local_node_id": binding.local_node_id,
                "canonical_node_id": binding.canonical_node_id,
            }
            for binding in role_assignments
        ],
        "primary_targets": [
            {
                "group": target.group,
                "index": target.index,
                "node_id": target.node_id,
            }
            for target in primary_targets
        ],
        "control_targets": [
            {
                "group": target.group,
                "index": target.index,
                "node_id": target.node_id,
            }
            for target in control_targets
        ],
        "primary_target": plan.primary_target,
        "control_target": plan.control_target,
        "materialization_readiness": {
            "ready": plan.materialization_readiness.ready,
            "blockers": plan.materialization_readiness.blockers,
        },
        "config_arguments": [
            {
                "name": argument.name,
                "value": projection[argument.name],
                "cardinality": argument.cardinality,
                "required": argument.required,
                "transforms": argument.transforms,
            }
            for argument in arguments
        ],
        "projected_config": dict(projection),
        "fields": [{"name": field.name, "value": field.value} for field in fields],
    }
    semantic = _sanitize_semantic(
        semantic,
        "plan semantics",
        [0],
    )
    encoded = _compiler_canonical_json(semantic).encode("utf-8")
    if len(encoded) > MAX_SEMANTIC_BYTES:
        raise ValueError("plan semantics exceed byte limit")
    return hashlib.sha256(encoded).hexdigest()


def _operation_payload(operation):
    """Return deterministic secret-free target operation data."""
    if operation is None:
        return None
    return {
        "key": {
            "fence": operation.key.fence,
            "projection_digest": operation.key.projection_digest,
        },
        "phase": operation.phase.value,
        "handle": operation.handle,
        "preimage_digest": operation.preimage_digest,
        "candidate_digest": operation.candidate_digest,
        "changed": operation.changed,
        "terminal_phase": (operation.terminal_phase.value if operation.terminal_phase is not None else None),
    }


def _key_payload(key):
    """Return deterministic target key data."""
    if key is None:
        return None
    return {
        "fence": key.fence,
        "projection_digest": key.projection_digest,
    }


def _state_integrity(state):
    """Fingerprint every safety-relevant secret-free checkpoint field."""
    payload = {
        "revision": state.revision,
        "phase": state.phase.value,
        "plan_digest": state.plan_digest,
        "publication_digest": state.publication_digest,
        "publication_version": state.publication_version,
        "projection_digest": state.projection_digest,
        "feedback_digest": state.feedback_digest,
        "provenance_digest": state.provenance_digest,
        "provenance_count": state.provenance_count,
        "reservation_key": _key_payload(state.reservation_key),
        "prior_operation_key": _key_payload(state.prior_operation_key),
        "operation": _operation_payload(state.operation),
        "applied_plan_digest": state.applied_plan_digest,
        "applied_publication_digest": state.applied_publication_digest,
        "applied_publication_version": state.applied_publication_version,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _new_state(**values):
    """Construct and validate one fingerprinted exact base state."""
    _validate_sequence(values["revision"], "revision", minimum=1)
    if not isinstance(values["phase"], MaterializationPhase):
        raise ValueError("state phase is invalid")
    _validate_digest(values["plan_digest"], "plan_digest")
    _validate_digest(values["publication_digest"], "publication_digest")
    _validate_sequence(
        values["publication_version"],
        "publication_version",
        minimum=1,
    )
    _validate_digest(values["projection_digest"], "projection_digest")
    _validate_digest(values["feedback_digest"], "feedback_digest")
    _validate_digest(values["provenance_digest"], "provenance_digest")
    _validate_sequence(
        values["provenance_count"],
        "provenance_count",
        minimum=0,
        maximum=MAX_PROVENANCE_ENTRIES,
    )
    if type(values["reservation_key"]) is not TargetOperationKey:
        raise ValueError("reservation key type is invalid")
    TargetOperationKey.__post_init__(values["reservation_key"])
    if values["prior_operation_key"] is not None:
        if type(values["prior_operation_key"]) is not TargetOperationKey:
            raise ValueError("prior operation key type is invalid")
        TargetOperationKey.__post_init__(values["prior_operation_key"])
    if values["operation"] is not None:
        if type(values["operation"]) is not TargetOperationSnapshot:
            raise ValueError("target operation type is invalid")
        TargetOperationSnapshot.__post_init__(values["operation"])
    if values["applied_publication_version"] is not None:
        _validate_sequence(
            values["applied_publication_version"],
            "applied_publication_version",
            minimum=1,
        )
    values["integrity_digest"] = ""
    provisional = AtomicMaterializationState.__new__(AtomicMaterializationState)
    for name, value in values.items():
        object.__setattr__(provisional, name, value)
    values["integrity_digest"] = _state_integrity(provisional)
    return AtomicMaterializationState(**values)


def _validate_envelope(envelope):
    """Validate and fingerprint all input before any external seam call."""
    if type(envelope) is not PublishedMaterializationRequest:
        raise MaterializationValidationError("publication envelope type is invalid")
    failure_kind = None
    try:
        PublishedMaterializationRequest.__post_init__(envelope)
        plan = envelope.request.plan
        readiness = plan.materialization_readiness
        if type(readiness) is not MaterializationReadiness or not isinstance(readiness.ready, bool) or type(readiness.blockers) is not tuple:
            raise ValueError("readiness shape is invalid")
        if readiness.ready or readiness.blockers != ("atomic_materializer_missing",):
            raise ValueError("readiness blocker is invalid")
        _validate_digest(plan.digest, "plan_digest")
        projection = _canonical_config(
            plan.projected_config,
            "projected_config",
        )
        arguments = _require_tuple(
            plan.config_arguments,
            "config_arguments",
            MAX_CONFIG_ENTRIES,
        )
        if any(type(argument) is not ProjectedConfigArgument for argument in arguments):
            raise ValueError("config argument type is invalid")
        names = tuple(argument.name for argument in arguments)
        if names != tuple(sorted(set(names))):
            raise ValueError("config argument names are invalid")
        if names != tuple(projection):
            raise ValueError("config projection names are invalid")
        for argument in arguments:
            if not _configs_equal(
                {"value": argument.value},
                {"value": projection[argument.name]},
            ):
                raise ValueError("config argument binding is invalid")
        provenance = _validate_provenance(plan.provenance)
        _validate_provider_generations(plan.provider_generations)
        if _plan_semantic_digest(plan, projection) != plan.digest:
            raise ValueError("semantic digest binding is invalid")
    except Exception as exc:
        failure_kind = type(exc).__name__
        projection = None
        provenance = None
    if failure_kind is not None:
        raise MaterializationValidationError("publication envelope validation failed ({})".format(failure_kind))
    return (
        projection,
        _config_digest(projection),
        _feedback_digest(envelope.request.feedback_token),
        _provenance_digest(provenance),
        len(provenance),
    )


class AtomicConfigMaterializer:
    """Fail-closed two-phase durable reservation coordinator."""

    def __init__(self, state_store=None, target=None, enabled=False):
        """Create a default-off, explicitly injected coordinator."""
        if type(enabled) is not bool:
            raise ValueError("enabled flag is invalid")
        self._enabled = enabled
        self._state_store = state_store
        self._target = target
        self._lock = threading.RLock()
        if enabled:
            for owner, methods, name in (
                (
                    state_store,
                    ("allocate_fence", "load", "compare_and_store"),
                    "state_store",
                ),
                (
                    target,
                    (
                        "prepare",
                        "lookup",
                        "apply",
                        "rollback",
                        "seal",
                        "abort",
                        "invalidate",
                    ),
                    "target",
                ),
            ):
                if owner is None or any(not callable(getattr(owner, method, None)) for method in methods):
                    raise ValueError("{} atomic seam is invalid".format(name))

    @property
    def enabled(self):
        """Return the explicit feature gate."""
        return self._enabled

    def _result(self, status, state, target_written=False):
        """Build one immutable public result."""
        return AtomicMaterializationResult(
            status,
            state,
            target_written,
        )

    def _load_state(self):
        """Load and freshly validate one exact base checkpoint."""
        failure_kind = None
        try:
            state = self._state_store.load()
        except Exception as exc:
            failure_kind = type(exc).__name__
            state = None
        if failure_kind is not None:
            raise MaterializationStorageError("state load failed ({})".format(failure_kind))
        if state is None:
            return None
        if type(state) is not AtomicMaterializationState:
            raise MaterializationStorageError("state checkpoint type is invalid")
        validation_failed = False
        try:
            AtomicMaterializationState.__post_init__(state)
        except Exception:
            validation_failed = True
        if validation_failed:
            raise MaterializationStorageError("state checkpoint validation failed")
        return state

    def _compare_store(self, expected, replacement):
        """Return ``(stored, current)`` with exact ack-loss recovery."""
        failure_kind = None
        try:
            committed = self._state_store.compare_and_store(
                expected,
                replacement,
            )
        except Exception as exc:
            failure_kind = type(exc).__name__
            committed = None
        current = self._load_state()
        if current == replacement:
            return True, replacement
        if failure_kind is not None:
            raise MaterializationStorageError("state CAS failed ambiguously ({})".format(failure_kind))
        if committed is True:
            raise MaterializationStorageError("state CAS acknowledgement was not durable")
        if current == expected:
            raise MaterializationStorageError("state CAS rejected without competing checkpoint")
        return False, current

    def _allocate_fence(self, previous):
        """Allocate and validate a bounded globally monotonic fence."""
        failure_kind = None
        try:
            fence = self._state_store.allocate_fence()
        except Exception as exc:
            failure_kind = type(exc).__name__
            fence = None
        if failure_kind is not None:
            raise MaterializationStorageError("fence allocation failed ({})".format(failure_kind))
        try:
            _validate_sequence(fence, "fence", minimum=1)
        except ValueError:
            raise MaterializationStorageError("allocated fence is invalid")
        if previous is not None and fence <= previous.reservation_key.fence:
            raise MaterializationStorageError("allocated fence is not monotonic")
        return fence

    def _target_snapshot_call(self, name, *args, allow_none=False):
        """Call a target seam and validate a value-free snapshot result."""
        failure_kind = None
        try:
            snapshot = getattr(self._target, name)(*args)
        except Exception as exc:
            failure_kind = type(exc).__name__
            snapshot = None
        if failure_kind is not None:
            raise MaterializationTargetError("target {} failed ({})".format(name, failure_kind))
        if snapshot is None and allow_none:
            return None
        if type(snapshot) is not TargetOperationSnapshot:
            raise MaterializationTargetError("target {} returned invalid type".format(name))
        validation_failed = False
        try:
            TargetOperationSnapshot.__post_init__(snapshot)
        except Exception:
            validation_failed = True
        if validation_failed:
            raise MaterializationTargetError("target {} returned invalid snapshot".format(name))
        return snapshot

    def _lookup(self, key):
        """Lookup one exact target reservation key."""
        snapshot = self._target_snapshot_call(
            "lookup",
            key.fence,
            key.projection_digest,
            allow_none=True,
        )
        if snapshot is not None and snapshot.key != key:
            raise MaterializationTargetError("target lookup key mismatch")
        return snapshot

    def _require_sealed(self, snapshot, key, operation_name):
        """Validate one acknowledged stable terminal target result."""
        if snapshot.key != key:
            raise MaterializationTargetError("target {} key mismatch".format(operation_name))
        if snapshot.phase is not TargetOperationPhase.SEALED or not isinstance(
            snapshot.terminal_phase,
            TargetTerminalPhase,
        ):
            raise MaterializationTargetError("target {} was not sealed".format(operation_name))
        return snapshot

    def _same_operation_identity(self, left, right):
        """Compare immutable target identity independently of lifecycle phase."""
        if left is None or right is None:
            return left is right
        return (
            left.key,
            left.handle,
            left.preimage_digest,
            left.candidate_digest,
            left.changed,
        ) == (
            right.key,
            right.handle,
            right.preimage_digest,
            right.candidate_digest,
            right.changed,
        )

    def _invalidate(self, key):
        """Fence and seal one operation key."""
        snapshot = self._target_snapshot_call(
            "invalidate",
            key,
        )
        return self._require_sealed(snapshot, key, "invalidate")

    def _abort(self, key):
        """Abort and seal one possibly unprepared reservation."""
        snapshot = self._target_snapshot_call(
            "abort",
            key.fence,
            key.projection_digest,
        )
        return self._require_sealed(snapshot, key, "abort")

    def _operation_key_for_state(self, state):
        """Return the durable target key represented by a state."""
        if state is None:
            return None
        return state.reservation_key

    def _publication_order(
        self,
        envelope,
        state,
        feedback_digest,
        provenance_digest,
        provenance_count,
    ):
        """Compare an input publication with the durable observed cursor."""
        if envelope.publication_version < state.publication_version:
            return -1
        if envelope.publication_version > state.publication_version:
            return 1
        if envelope.publication_digest != state.publication_digest:
            raise MaterializationValidationError("publication version identity conflict")
        if feedback_digest != state.feedback_digest or provenance_digest != state.provenance_digest or provenance_count != state.provenance_count:
            raise MaterializationValidationError("publication audit identity conflict")
        return 0

    def _terminal_result(self, state):
        """Map one stable sealed durable state to its public result."""
        if state.phase is MaterializationPhase.APPLIED:
            return self._result(MaterializationStatus.UNCHANGED, state)
        if state.phase is MaterializationPhase.ROLLED_BACK:
            return self._result(MaterializationStatus.NOT_APPLIED, state)
        return self._result(MaterializationStatus.UNKNOWN, state)

    def _require_revision_headroom(self, state, steps=1):
        """Fail before a target seam if durable finalization cannot advance."""
        _validate_sequence(state.revision, "revision", minimum=1)
        if steps < 1 or state.revision > MAX_SEQUENCE - steps:
            raise MaterializationStorageError("state revision has no transition headroom")

    def _with_operation(self, state, phase, operation):
        """Build a value-free validated state transition."""
        transition_failed = False
        try:
            replacement = state.with_operation(phase, operation)
        except Exception:
            transition_failed = True
            replacement = None
        if transition_failed:
            raise MaterializationStorageError("state transition validation failed")
        return replacement

    def _advance_publication(
        self,
        state,
        envelope,
        feedback_digest,
        provenance_digest,
        provenance_count,
    ):
        """Build a value-free publication cursor transition."""
        transition_failed = False
        try:
            replacement = state.advance_publication(
                envelope,
                feedback_digest,
                provenance_digest,
                provenance_count,
            )
        except Exception:
            transition_failed = True
            replacement = None
        if transition_failed:
            raise MaterializationStorageError("publication state transition failed")
        return replacement

    def _persist_terminal(self, base_state, sealed):
        """Persist a sealed result despite same-key transition races."""
        if base_state.operation is not None and not self._same_operation_identity(
            base_state.operation,
            sealed,
        ):
            return self._result(
                MaterializationStatus.UNKNOWN,
                self._load_state(),
            )
        current = self._load_state()
        for _attempt in range(MAX_RESERVATION_RETRIES):
            if current is None:
                raise MaterializationStorageError("terminal reservation disappeared")
            if current.reservation_key != base_state.reservation_key:
                return self._result(
                    MaterializationStatus.SUPERSEDED,
                    current,
                )
            if sealed.terminal_phase is TargetTerminalPhase.APPLIED:
                phase = MaterializationPhase.APPLIED
                status = MaterializationStatus.APPLIED
            elif sealed.terminal_phase is TargetTerminalPhase.ROLLED_BACK:
                phase = MaterializationPhase.ROLLED_BACK
                status = MaterializationStatus.NOT_APPLIED
            else:
                phase = MaterializationPhase.UNKNOWN
                status = MaterializationStatus.UNKNOWN
            replacement = self._with_operation(
                current,
                phase,
                sealed,
            )
            stored, observed = self._compare_store(current, replacement)
            if stored:
                return self._result(
                    status,
                    replacement,
                    target_written=(sealed.changed is True and sealed.terminal_phase is TargetTerminalPhase.APPLIED),
                )
            current = observed
        raise MaterializationStorageError("terminal state CAS retry limit exceeded")

    def _settle(self, state, snapshot):
        """Seal/invalidate before any terminal materializer result."""
        self._require_revision_headroom(state)
        key = state.reservation_key
        if not self._same_operation_identity(state.operation, snapshot):
            try:
                self._invalidate(key)
            except MaterializationTargetError:
                pass
            return self._result(
                MaterializationStatus.UNKNOWN,
                self._load_state(),
            )
        try:
            if snapshot.phase is TargetOperationPhase.SEALED:
                sealed = self._require_sealed(
                    snapshot,
                    key,
                    "operation",
                )
            elif snapshot.phase in (
                TargetOperationPhase.APPLIED,
                TargetOperationPhase.ROLLED_BACK,
            ):
                sealed = self._target_snapshot_call(
                    "seal",
                    snapshot,
                )
                sealed = self._require_sealed(
                    sealed,
                    key,
                    "seal",
                )
            else:
                sealed = self._invalidate(key)
        except MaterializationTargetError:
            return self._result(
                MaterializationStatus.UNKNOWN,
                self._load_state(),
            )
        return self._persist_terminal(state, sealed)

    def _recover_state(self, state):
        """Recover one durable phase without racing a late same-fence commit."""
        if state.phase in (
            MaterializationPhase.APPLIED,
            MaterializationPhase.ROLLED_BACK,
        ):
            try:
                snapshot = self._lookup(state.reservation_key)
            except MaterializationTargetError:
                return self._result(MaterializationStatus.UNKNOWN, state)
            if snapshot is None or snapshot != state.operation or snapshot.phase is not TargetOperationPhase.SEALED:
                return self._result(MaterializationStatus.UNKNOWN, state)
            return self._terminal_result(state)

        self._require_revision_headroom(state)
        try:
            if state.prior_operation_key is not None:
                self._invalidate(state.prior_operation_key)
            snapshot = self._lookup(state.reservation_key)
            if snapshot is None:
                sealed = self._abort(state.reservation_key)
            elif state.operation is not None and not self._same_operation_identity(
                state.operation,
                snapshot,
            ):
                try:
                    self._invalidate(state.reservation_key)
                except MaterializationTargetError:
                    pass
                return self._result(
                    MaterializationStatus.UNKNOWN,
                    state,
                )
            elif snapshot.phase is TargetOperationPhase.SEALED:
                sealed = self._require_sealed(
                    snapshot,
                    state.reservation_key,
                    "lookup",
                )
            else:
                sealed = self._invalidate(state.reservation_key)
        except MaterializationTargetError:
            return self._result(MaterializationStatus.UNKNOWN, state)
        return self._persist_terminal(state, sealed)

    def recover(self):
        """Recover a reservation through target invalidation and sealing."""
        if not self._enabled:
            return self._result(MaterializationStatus.DISABLED, None)
        with self._lock:
            state = self._load_state()
            if state is None:
                return self._result(
                    MaterializationStatus.NOT_APPLIED,
                    None,
                )
            return self._recover_state(state)

    def _reserve(
        self,
        envelope,
        projection_digest,
        feedback_digest,
        provenance_digest,
        provenance_count,
    ):
        """Win a durable reservation before any target prepare call."""
        current = self._load_state()
        for _attempt in range(MAX_RESERVATION_RETRIES):
            if current is not None:
                order = self._publication_order(
                    envelope,
                    current,
                    feedback_digest,
                    provenance_digest,
                    provenance_count,
                )
                if order < 0:
                    return None, self._result(
                        MaterializationStatus.SUPERSEDED,
                        current,
                    )
                same_plan = envelope.request.plan.digest == current.plan_digest and projection_digest == current.projection_digest
                if current.phase is MaterializationPhase.APPLIED and same_plan:
                    if order == 0:
                        return None, self._result(
                            MaterializationStatus.UNCHANGED,
                            current,
                        )
                    advanced = self._advance_publication(
                        current,
                        envelope,
                        feedback_digest,
                        provenance_digest,
                        provenance_count,
                    )
                    stored, observed = self._compare_store(
                        current,
                        advanced,
                    )
                    if stored:
                        return None, self._result(
                            MaterializationStatus.UNCHANGED,
                            advanced,
                        )
                    current = observed
                    continue
                if order == 0 and current.phase not in (
                    MaterializationPhase.ROLLED_BACK,
                    MaterializationPhase.UNKNOWN,
                ):
                    return None, self._result(
                        MaterializationStatus.UNKNOWN,
                        current,
                    )

            fence = self._allocate_fence(current)
            reserved = AtomicMaterializationState.reserve(
                1,
                envelope,
                projection_digest,
                feedback_digest,
                provenance_digest,
                provenance_count,
                fence,
                self._operation_key_for_state(current),
            )
            stored, observed = self._compare_store(
                current,
                reserved,
            )
            if stored:
                return reserved, None
            current = observed
        raise MaterializationStorageError("reservation CAS retry limit exceeded")

    def _attach_operation(self, reserved, operation):
        """Attach target metadata only while the reservation remains current."""
        prepared = self._with_operation(
            reserved,
            MaterializationPhase.PREPARED,
            operation,
        )
        stored, current = self._compare_store(reserved, prepared)
        if stored:
            return prepared, None
        try:
            self._invalidate(reserved.reservation_key)
        except MaterializationTargetError:
            pass
        if current is not None:
            return None, self._result(
                MaterializationStatus.SUPERSEDED,
                current,
            )
        raise MaterializationStorageError("prepared reservation disappeared")

    def _mark_applying(self, prepared):
        """Durably announce APPLYING before invoking target.apply."""
        self._require_revision_headroom(prepared, steps=2)
        applying = self._with_operation(
            prepared,
            MaterializationPhase.APPLYING,
            prepared.operation,
        )
        stored, current = self._compare_store(prepared, applying)
        if stored:
            return applying, None
        try:
            self._invalidate(prepared.reservation_key)
        except MaterializationTargetError:
            pass
        return None, self._result(
            MaterializationStatus.SUPERSEDED,
            current,
        )

    def apply(self, envelope):
        """Reserve, prepare, apply, seal, and checkpoint one publication."""
        if not self._enabled:
            return self._result(MaterializationStatus.DISABLED, None)
        (
            projection,
            projection_digest,
            feedback_digest,
            provenance_digest,
            provenance_count,
        ) = _validate_envelope(envelope)
        with self._lock:
            reserved, terminal = self._reserve(
                envelope,
                projection_digest,
                feedback_digest,
                provenance_digest,
                provenance_count,
            )
            if terminal is not None:
                return terminal

            self._require_revision_headroom(reserved, steps=3)
            try:
                if reserved.prior_operation_key is not None:
                    self._invalidate(reserved.prior_operation_key)
            except MaterializationTargetError:
                return self._result(
                    MaterializationStatus.UNKNOWN,
                    reserved,
                )

            try:
                operation = self._target_snapshot_call(
                    "prepare",
                    reserved.reservation_key.fence,
                    reserved.projection_digest,
                    dict(projection),
                )
                if operation.key != reserved.reservation_key or operation.phase is not TargetOperationPhase.PREPARED:
                    raise MaterializationTargetError("target prepare acknowledgement is invalid")
            except MaterializationTargetError:
                return self._result(
                    MaterializationStatus.UNKNOWN,
                    reserved,
                )

            prepared, terminal = self._attach_operation(
                reserved,
                operation,
            )
            if terminal is not None:
                return terminal
            applying, terminal = self._mark_applying(prepared)
            if terminal is not None:
                return terminal

            self._require_revision_headroom(applying)
            try:
                snapshot = self._target_snapshot_call(
                    "apply",
                    operation,
                )
            except MaterializationTargetError:
                try:
                    snapshot = self._lookup(applying.reservation_key)
                except MaterializationTargetError:
                    snapshot = None
                if snapshot is None:
                    return self._result(
                        MaterializationStatus.UNKNOWN,
                        applying,
                    )
            return self._settle(applying, snapshot)

    def rollback(self):
        """Rollback an exact sealed APPLIED transaction and reseal it."""
        if not self._enabled:
            return self._result(MaterializationStatus.DISABLED, None)
        with self._lock:
            state = self._load_state()
            if state is None:
                return self._result(
                    MaterializationStatus.NOT_APPLIED,
                    None,
                )
            if state.phase not in (
                MaterializationPhase.APPLIED,
                MaterializationPhase.ROLLED_BACK,
            ):
                recovered = self._recover_state(state)
                state = recovered.state
                if (
                    recovered.status
                    in (
                        MaterializationStatus.UNKNOWN,
                        MaterializationStatus.SUPERSEDED,
                    )
                    or state is None
                ):
                    return recovered
            if state.phase is MaterializationPhase.ROLLED_BACK:
                return self._result(
                    MaterializationStatus.NOT_APPLIED,
                    state,
                )
            self._require_revision_headroom(state, steps=2)
            rolling = self._with_operation(
                state,
                MaterializationPhase.ROLLING_BACK,
                state.operation,
            )
            stored, current = self._compare_store(state, rolling)
            if not stored:
                return self._result(
                    MaterializationStatus.SUPERSEDED,
                    current,
                )
            try:
                snapshot = self._target_snapshot_call(
                    "rollback",
                    state.operation,
                )
            except MaterializationTargetError:
                try:
                    snapshot = self._lookup(rolling.reservation_key)
                except MaterializationTargetError:
                    snapshot = None
                if snapshot is None:
                    return self._result(
                        MaterializationStatus.UNKNOWN,
                        rolling,
                    )
            settled = self._settle(rolling, snapshot)
            if settled.status is MaterializationStatus.NOT_APPLIED:
                return self._result(
                    MaterializationStatus.ROLLED_BACK,
                    settled.state,
                    target_written=(settled.state.operation.changed is True),
                )
            return settled
