# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice fragment adapter registry
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Generic durable integration adapters for compiled Lattice fragments.

This module is deliberately additive and default-off.  Nothing discovers or
registers production integrations unless a caller explicitly enables a
``FragmentAdapterRegistry`` and asks it to create a compiler.

An integration owns one ``DurableFragmentAdapter`` and its durable state store.
The adapter atomically binds every generation to one semantic fingerprint and
one immutable ``ProviderSnapshot``.  The registry only discovers the common
``lattice_fragment_adapter()`` surface; it has no provider or brand allow-list.
"""

# cspell:ignore autoconfig idempotently unsubscribers

import hashlib
import math
import threading
from dataclasses import dataclass
from functools import partial
from itertools import islice
from types import MappingProxyType
from typing import Optional, Protocol

from lattice_autoconfig import (
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
    _fingerprint_snapshot,
    _plain,
)
from lattice_compiled_publication import CompiledLatticeCompiler


class FragmentAdapterError(RuntimeError):
    """Base error for fail-closed fragment adapter operations."""


class FragmentAdapterReadError(FragmentAdapterError):
    """A durable fragment could not be read or validated."""


class FragmentAdapterConflict(FragmentAdapterError):
    """An atomic fragment publication lost to a different durable state."""


class FragmentAdapterRemoved(FragmentAdapterReadError):
    """A registered integration has published a durable removal tombstone."""


_MAX_SNAPSHOT_DEPTH = 32
_MAX_SNAPSHOT_CONTAINER_ITEMS = 65536
_MAX_SNAPSHOT_TOTAL_ITEMS = 1000000
_MAX_SNAPSHOT_SEQUENCE_ITEMS = 32768
_MAX_SNAPSHOT_STRING_CHARS = 16384
_MAX_SNAPSHOT_INTEGER = (1 << 63) - 1
_MAX_SNAPSHOT_FLOAT = 1.0e308
_MAX_ALIAS_ROLES = 3
_MAX_PROJECTION_TRANSFORMS = 64


def _validate_provider_id(provider_id):
    """Normalize one provider-owned stable identifier."""
    if type(provider_id) is not str or not provider_id.strip():
        raise ValueError("provider_id must be a non-empty string")
    return provider_id.strip()


def _validate_generation(generation):
    """Validate one monotonically increasing integration generation."""
    if type(generation) is not int or generation < 0:
        raise ValueError("generation must be a non-negative integer")


def _validate_reason(reason):
    """Normalize one auditable invalidation reason."""
    if type(reason) is not str or not reason.strip():
        raise ValueError("reason must be a non-empty string")
    return reason.strip()


def _semantic_fingerprint(snapshot, removed=False):
    """Return the compiler's generation-bound semantic safety fingerprint."""
    fingerprint = _fingerprint_snapshot(snapshot)
    if removed:
        return hashlib.sha256("removed:{}".format(fingerprint).encode("utf-8")).hexdigest()
    return fingerprint


def _bounded_snapshot_string(value, *, non_empty=False):
    """Return one exact bounded string without invoking subclass hooks."""
    if type(value) is not str:
        raise ValueError("snapshot text must be a string")
    if len(value) > _MAX_SNAPSHOT_STRING_CHARS:
        raise ValueError("snapshot text exceeds its size bound")
    if non_empty and not value.strip():
        raise ValueError("snapshot text must not be empty")
    return value


def _bounded_snapshot_integer(value, *, non_negative=False):
    """Return one exact bounded integer."""
    if type(value) is not int:
        raise ValueError("snapshot integer must be an integer")
    if abs(value) > _MAX_SNAPSHOT_INTEGER:
        raise ValueError("snapshot integer exceeds its size bound")
    if non_negative and value < 0:
        raise ValueError("snapshot integer must not be negative")
    return value


def _bounded_snapshot_float(value):
    """Return one exact finite bounded float."""
    if type(value) is not float:
        raise ValueError("snapshot float must be a float")
    if not math.isfinite(value) or abs(value) > _MAX_SNAPSHOT_FLOAT:
        raise ValueError("snapshot float must be finite and bounded")
    return value


def _bounded_exact_tuple(value, maximum, budget=None):
    """Collect one exact tuple through a max+1 bounded iterator."""
    if type(value) is not tuple:
        raise ValueError("snapshot sequence must be a tuple")
    items = tuple(islice(iter(value), maximum + 1))
    if len(items) > maximum:
        raise ValueError("snapshot sequence exceeds its item bound")
    if budget is not None:
        _consume_snapshot_budget(
            budget,
            len(items),
        )
    return items


def _consume_snapshot_budget(budget, count=1):
    """Account for recursively reconstructed snapshot values."""
    budget[0] += count
    if budget[0] > _MAX_SNAPSHOT_TOTAL_ITEMS:
        raise ValueError("snapshot exceeds its total item bound")


def _copy_bounded_snapshot_json(value, budget, depth=0):
    """Copy exact frozen JSON into safe built-in containers with hard bounds."""
    if depth > _MAX_SNAPSHOT_DEPTH:
        raise ValueError("snapshot exceeds its nesting bound")
    _consume_snapshot_budget(budget)

    if type(value) is MappingProxyType:
        entries = tuple(
            islice(
                iter(value.items()),
                _MAX_SNAPSHOT_CONTAINER_ITEMS + 1,
            )
        )
        if len(entries) > _MAX_SNAPSHOT_CONTAINER_ITEMS:
            raise ValueError("snapshot mapping exceeds its item bound")
        copied = {}
        for key, item in entries:
            key = _bounded_snapshot_string(key)
            _consume_snapshot_budget(budget)
            copied[key] = _copy_bounded_snapshot_json(
                item,
                budget,
                depth + 1,
            )
        return copied

    if type(value) is tuple:
        items = tuple(
            islice(
                iter(value),
                _MAX_SNAPSHOT_CONTAINER_ITEMS + 1,
            )
        )
        if len(items) > _MAX_SNAPSHOT_CONTAINER_ITEMS:
            raise ValueError("snapshot sequence exceeds its item bound")
        return tuple(
            _copy_bounded_snapshot_json(
                item,
                budget,
                depth + 1,
            )
            for item in items
        )

    if type(value) is str:
        return _bounded_snapshot_string(value)
    if type(value) is bool or value is None:
        return value
    if type(value) is int:
        return _bounded_snapshot_integer(value)
    if type(value) is float:
        return _bounded_snapshot_float(value)
    raise ValueError("snapshot contains an unsupported JSON value")


def _validated_alias(alias, budget):
    """Reconstruct one exact provider alias."""
    if type(alias) is not ProviderAlias:
        raise ValueError("snapshot alias has an invalid type")
    name = alias.name
    node_id = alias.node_id
    roles_value = alias.roles
    name = _bounded_snapshot_string(name, non_empty=True)
    node_id = _bounded_snapshot_string(node_id, non_empty=True)
    if type(roles_value) is not frozenset:
        raise ValueError("snapshot alias roles must be a frozenset")
    roles = tuple(
        islice(
            iter(roles_value),
            _MAX_ALIAS_ROLES + 1,
        )
    )
    if not roles or len(roles) > _MAX_ALIAS_ROLES or any(type(role) is not AliasRole for role in roles):
        raise ValueError("snapshot alias roles are invalid")
    _consume_snapshot_budget(
        budget,
        len(roles),
    )
    clean = ProviderAlias(
        name=name,
        node_id=node_id,
        roles=frozenset(roles),
    )
    if (name, node_id, frozenset(roles)) != (
        clean.name,
        clean.node_id,
        clean.roles,
    ):
        raise ValueError("snapshot alias is not canonical")
    return clean


def _validated_identity_alias(alias, _budget):
    """Reconstruct one exact provider identity assertion."""
    if type(alias) is not ProviderIdentityAlias:
        raise ValueError("snapshot identity alias has an invalid type")
    kind = alias.kind
    value = alias.value
    node_id = alias.node_id
    clean = ProviderIdentityAlias(
        kind=_bounded_snapshot_string(
            kind,
            non_empty=True,
        ),
        value=_bounded_snapshot_string(
            value,
            non_empty=True,
        ),
        node_id=_bounded_snapshot_string(
            node_id,
            non_empty=True,
        ),
    )
    if (kind, value, node_id) != (
        clean.kind,
        clean.value,
        clean.node_id,
    ):
        raise ValueError("snapshot identity alias is not canonical")
    return clean


def _validated_role_assignment(assignment, _budget):
    """Reconstruct one exact provider role assignment."""
    if type(assignment) is not ProviderRoleAssignment:
        raise ValueError("snapshot role assignment has an invalid type")
    role = assignment.role
    group = assignment.group
    index = assignment.index
    node_id = assignment.node_id
    if type(role) is not AliasRole:
        raise ValueError("snapshot role assignment has an invalid role")
    clean = ProviderRoleAssignment(
        role=role,
        group=_bounded_snapshot_string(
            group,
            non_empty=True,
        ),
        index=_bounded_snapshot_integer(
            index,
            non_negative=True,
        ),
        node_id=_bounded_snapshot_string(
            node_id,
            non_empty=True,
        ),
    )
    if (role, group, index, node_id) != (
        clean.role,
        clean.group,
        clean.index,
        clean.node_id,
    ):
        raise ValueError("snapshot role assignment is not canonical")
    return clean


def _validated_optional_snapshot_string(value):
    """Reconstruct one optional exact non-empty string."""
    if value is None:
        return None
    return _bounded_snapshot_string(value, non_empty=True)


def _validated_projection_constant(value):
    """Reconstruct one bounded exact JSON scalar projection value."""
    if type(value) is str:
        return _bounded_snapshot_string(value)
    if type(value) is bool:
        return value
    if type(value) is int:
        return _bounded_snapshot_integer(value)
    if type(value) is float:
        return _bounded_snapshot_float(value)
    raise ValueError("snapshot projection constant has an invalid type")


def _validated_projection_value(value, _budget):
    """Reconstruct one exact provider projection value."""
    if type(value) is not ProviderProjectionValue:
        raise ValueError("snapshot projection value has an invalid type")
    node_id = value.node_id
    kind = value.kind
    raw_value = value.value
    capability = value.capability
    identity_kind = value.identity_kind
    identity_value = value.identity_value
    access_path_id = value.access_path_id
    if type(kind) is not ProjectionValueKind:
        raise ValueError("snapshot projection value has an invalid kind")

    if kind is ProjectionValueKind.ENTITY:
        clean_value = _bounded_snapshot_string(
            raw_value,
            non_empty=True,
        )
    elif kind is ProjectionValueKind.CONSTANT:
        clean_value = _validated_projection_constant(raw_value)
    else:
        if raw_value is not None:
            raise ValueError("snapshot None projection carries a value")
        clean_value = None

    clean = ProviderProjectionValue(
        node_id=_bounded_snapshot_string(
            node_id,
            non_empty=True,
        ),
        kind=kind,
        value=clean_value,
        capability=_validated_optional_snapshot_string(
            capability,
        ),
        identity_kind=_validated_optional_snapshot_string(
            identity_kind,
        ),
        identity_value=_validated_optional_snapshot_string(
            identity_value,
        ),
        access_path_id=_validated_optional_snapshot_string(
            access_path_id,
        ),
    )
    if (
        node_id,
        kind,
        raw_value,
        capability,
        identity_kind,
        identity_value,
        access_path_id,
    ) != (
        clean.node_id,
        clean.kind,
        clean.value,
        clean.capability,
        clean.identity_kind,
        clean.identity_value,
        clean.access_path_id,
    ):
        raise ValueError("snapshot projection value is not canonical")
    return clean


def _validated_config_projection(projection, budget):
    """Reconstruct one exact provider configuration projection."""
    if type(projection) is not ProviderConfigProjection:
        raise ValueError("snapshot projection has an invalid type")
    argument = projection.argument
    role = projection.role
    group = projection.group
    routing = projection.routing
    cardinality = projection.cardinality
    raw_values = projection.values
    required = projection.required
    raw_transforms = projection.transforms
    if type(role) is not AliasRole:
        raise ValueError("snapshot projection has an invalid role")
    if type(routing) is not ProjectionRouting:
        raise ValueError("snapshot projection has invalid routing")
    if type(cardinality) is not ProjectionCardinality:
        raise ValueError("snapshot projection has invalid cardinality")
    if type(required) is not bool:
        raise ValueError("snapshot projection required must be boolean")

    values = tuple(
        _validated_projection_value(
            value,
            budget,
        )
        for value in _bounded_exact_tuple(
            raw_values,
            _MAX_SNAPSHOT_SEQUENCE_ITEMS,
            budget,
        )
    )
    transforms = tuple(
        _bounded_snapshot_string(
            transform,
            non_empty=True,
        )
        for transform in _bounded_exact_tuple(
            raw_transforms,
            _MAX_PROJECTION_TRANSFORMS,
            budget,
        )
    )
    clean = ProviderConfigProjection(
        argument=_bounded_snapshot_string(
            argument,
            non_empty=True,
        ),
        role=role,
        group=_bounded_snapshot_string(
            group,
            non_empty=True,
        ),
        routing=routing,
        cardinality=cardinality,
        values=values,
        required=required,
        transforms=transforms,
    )
    if (
        argument,
        role,
        group,
        routing,
        cardinality,
        raw_values,
        required,
        raw_transforms,
    ) != (
        clean.argument,
        clean.role,
        clean.group,
        clean.routing,
        clean.cardinality,
        clean.values,
        clean.required,
        clean.transforms,
    ):
        raise ValueError("snapshot projection is not canonical")
    return clean


def _validated_provider_snapshot(snapshot):
    """Return a bounded exact reconstruction of one provider snapshot."""
    if type(snapshot) is not ProviderSnapshot:
        raise ValueError("snapshot must be ProviderSnapshot")
    raw_provider_id = snapshot.provider_id
    raw_generation = snapshot.generation
    health = snapshot.health
    raw_topology_fragment = snapshot.topology_fragment
    raw_aliases = snapshot.aliases
    raw_identity_aliases = snapshot.identity_aliases
    raw_role_assignments = snapshot.role_assignments
    raw_config_projections = snapshot.config_projections
    provider_id = _bounded_snapshot_string(
        raw_provider_id,
        non_empty=True,
    )
    generation = _bounded_snapshot_integer(
        raw_generation,
        non_negative=True,
    )
    if type(health) is not ProviderHealth:
        raise ValueError("snapshot health must be ProviderHealth")

    budget = [0]
    topology_fragment = _copy_bounded_snapshot_json(
        raw_topology_fragment,
        budget,
    )
    aliases = tuple(
        _validated_alias(
            alias,
            budget,
        )
        for alias in _bounded_exact_tuple(
            raw_aliases,
            _MAX_SNAPSHOT_SEQUENCE_ITEMS,
            budget,
        )
    )
    identity_aliases = tuple(
        _validated_identity_alias(
            alias,
            budget,
        )
        for alias in _bounded_exact_tuple(
            raw_identity_aliases,
            _MAX_SNAPSHOT_SEQUENCE_ITEMS,
            budget,
        )
    )
    role_assignments = tuple(
        _validated_role_assignment(
            assignment,
            budget,
        )
        for assignment in _bounded_exact_tuple(
            raw_role_assignments,
            _MAX_SNAPSHOT_SEQUENCE_ITEMS,
            budget,
        )
    )
    config_projections = tuple(
        _validated_config_projection(
            projection,
            budget,
        )
        for projection in _bounded_exact_tuple(
            raw_config_projections,
            _MAX_SNAPSHOT_SEQUENCE_ITEMS,
            budget,
        )
    )
    clean = ProviderSnapshot(
        provider_id=provider_id,
        generation=generation,
        health=health,
        topology_fragment=topology_fragment,
        aliases=aliases,
        identity_aliases=identity_aliases,
        role_assignments=role_assignments,
        config_projections=config_projections,
    )
    if raw_provider_id != clean.provider_id:
        raise ValueError("snapshot provider_id is not canonical")
    return clean


@dataclass(frozen=True)
class FragmentAdapterState:
    """One integration-owned durable fragment cursor and immutable value.

    ``semantic_fingerprint`` intentionally uses the compiler's exact
    generation-bound fingerprint.  The pair therefore detects both generation
    regression and reuse of one generation for different safety-relevant
    content.
    """

    provider_id: str
    generation: int
    semantic_fingerprint: str
    snapshot: ProviderSnapshot
    removed: bool = False

    def __post_init__(self):
        """Validate that the durable cursor exactly binds its snapshot."""
        provider_id = _validate_provider_id(self.provider_id)
        _validate_generation(self.generation)
        if type(self.semantic_fingerprint) is not str:
            raise ValueError("semantic_fingerprint must be a string")
        if type(self.snapshot) is not ProviderSnapshot:
            raise ValueError("snapshot must be ProviderSnapshot")
        if type(self.snapshot.provider_id) is not str:
            raise ValueError("snapshot provider_id must be a string")
        if type(self.snapshot.generation) is not int:
            raise ValueError("snapshot generation must be an integer")
        if self.snapshot.provider_id != provider_id:
            raise ValueError("snapshot provider_id does not match durable state")
        if self.snapshot.generation != self.generation:
            raise ValueError("snapshot generation does not match durable state")
        if type(self.removed) is not bool:
            raise ValueError("removed must be a boolean")
        fingerprint_failed = False
        try:
            expected = _semantic_fingerprint(
                self.snapshot,
                self.removed,
            )
        except Exception:
            fingerprint_failed = True
        if fingerprint_failed:
            raise ValueError("snapshot semantic fingerprint is invalid")
        if self.semantic_fingerprint != expected:
            raise ValueError("semantic_fingerprint does not match the immutable snapshot")
        object.__setattr__(self, "provider_id", provider_id)


def _validated_fragment_state(state):
    """Revalidate one exact durable state without subclass dispatch."""
    if type(state) is not FragmentAdapterState:
        raise ValueError("state must be FragmentAdapterState")
    raw_provider_id = state.provider_id
    provider_id = _validate_provider_id(raw_provider_id)
    if raw_provider_id != provider_id:
        raise ValueError("durable provider_id is not canonical")
    _validate_generation(state.generation)
    if type(state.semantic_fingerprint) is not str:
        raise ValueError("semantic_fingerprint must be a string")
    if type(state.removed) is not bool:
        raise ValueError("removed must be a boolean")
    snapshot = _validated_provider_snapshot(state.snapshot)
    if snapshot.provider_id != provider_id:
        raise ValueError("snapshot provider_id does not match durable state")
    if snapshot.generation != state.generation:
        raise ValueError("snapshot generation does not match durable state")
    expected = _semantic_fingerprint(
        snapshot,
        state.removed,
    )
    if state.semantic_fingerprint != expected:
        raise ValueError("semantic_fingerprint does not match the immutable snapshot")
    return FragmentAdapterState(
        provider_id=provider_id,
        generation=state.generation,
        semantic_fingerprint=expected,
        snapshot=snapshot,
        removed=state.removed,
    )


def _compiler_fragment_snapshot(adapter):
    """Fresh-read one compiler input, translating only durable removals."""
    validation_failed = False
    try:
        state = _validated_fragment_state(adapter.read_state())
        provider_id = _validate_provider_id(
            getattr(adapter, "provider_id", None),
        )
        if state.provider_id != provider_id:
            raise ValueError("adapter state provider mismatch")
        if not state.removed:
            snapshot = state.snapshot
        else:
            snapshot = ProviderSnapshot(
                provider_id=state.provider_id,
                generation=state.generation,
                health=ProviderHealth.HEALTHY,
                topology_fragment={
                    "topologyVersion": "0.3.0",
                    "scope": "fragment",
                    "docVersion": state.generation,
                    "producer": {
                        "name": "PredBat fragment tombstone",
                        "provider": state.provider_id,
                        "authority": 0,
                    },
                    "nodes": [],
                    "relationships": [],
                },
                aliases=(),
                identity_aliases=(),
                role_assignments=(),
                config_projections=(),
            )
    except Exception:
        validation_failed = True
    if validation_failed:
        raise FragmentAdapterReadError(
            "compiler fragment state failed validation",
        )
    return snapshot


class FragmentAdapterStateStore:
    """Required atomic durable-store protocol owned by one integration."""

    def load(self):
        """Return the current ``FragmentAdapterState`` or ``None``."""
        raise NotImplementedError

    def compare_and_store(self, expected, replacement):
        """Atomically store replacement only when current state equals expected."""
        raise NotImplementedError


class FragmentPublisher(Protocol):
    """Structural integration protocol discovered without provider knowledge."""

    provider_id: str

    def read_state(self) -> FragmentAdapterState:
        """Fresh-read the integration-owned durable state."""
        ...

    def read_snapshot(self) -> ProviderSnapshot:
        """Fresh-read the current immutable provider snapshot."""
        ...

    def subscribe_invalidation(self, listener):
        """Attach the registry's invalidation sink and return an unsubscribe."""
        ...


class FragmentPublishingComponent(Protocol):
    """Structural component discovery surface used by the generic registry."""

    def lattice_fragment_adapter(self) -> Optional[FragmentPublisher]:
        """Return this component's fragment publisher, if it has one."""
        ...


class InMemoryFragmentAdapterStateStore(FragmentAdapterStateStore):
    """Thread-safe reference state store for tests; not production durability."""

    def __init__(self, state=None):
        """Create a store optionally seeded with one validated state."""
        if state is not None and not isinstance(state, FragmentAdapterState):
            raise ValueError("initial state must be FragmentAdapterState or None")
        self._lock = threading.RLock()
        self._state = state
        self._writes = 0

    @property
    def writes(self):
        """Return the number of successful atomic writes."""
        with self._lock:
            return self._writes

    def load(self):
        """Return the immutable current state."""
        with self._lock:
            return self._state

    def compare_and_store(self, expected, replacement):
        """Atomically compare the complete cursor and install replacement."""
        if replacement is not None and not isinstance(
            replacement,
            FragmentAdapterState,
        ):
            raise ValueError("replacement must be FragmentAdapterState or None")
        with self._lock:
            if self._state != expected:
                return False
            self._state = replacement
            self._writes += 1
            return True


class DurableFragmentAdapter:
    """Generic publisher over one integration-owned atomic state store."""

    def __init__(self, provider_id, state_store):
        """Restore one provider cursor without performing discovery or writes."""
        self.provider_id = _validate_provider_id(provider_id)
        if not callable(getattr(state_store, "load", None)) or not callable(getattr(state_store, "compare_and_store", None)):
            raise ValueError("state_store must provide load and compare_and_store")
        self._state_store = state_store
        self._lock = threading.RLock()
        self._listeners = []
        self._validate_loaded_state(self._load())

    def _load(self):
        """Load durable state and convert store faults into adapter faults."""
        load_failed = False
        try:
            state = self._state_store.load()
        except Exception:
            load_failed = True
        if load_failed:
            raise FragmentAdapterReadError(
                "durable fragment load failed",
            )
        return state

    def _validate_loaded_state(self, state):
        """Validate one store result and its provider ownership."""
        if state is None:
            return None
        validation_failed = False
        try:
            state = _validated_fragment_state(state)
            if state.provider_id != self.provider_id:
                raise ValueError("durable state provider mismatch")
        except Exception:
            validation_failed = True
        if validation_failed:
            raise FragmentAdapterReadError(
                "durable fragment state failed validation",
            )
        return state

    def read_state(self):
        """Fresh-read the complete immutable durable fragment state."""
        with self._lock:
            state = self._validate_loaded_state(self._load())
            if state is None:
                raise FragmentAdapterReadError("provider {} has no durable fragment".format(self.provider_id))
            return state

    def read_snapshot(self):
        """Fresh-read one immutable snapshot or fail closed on removal."""
        state = self.read_state()
        if state.removed:
            raise FragmentAdapterRemoved(
                "provider {} was removed at generation {}".format(
                    self.provider_id,
                    state.generation,
                )
            )
        return state.snapshot

    def subscribe_invalidation(self, listener):
        """Subscribe the compiler-facing invalidation sink."""
        if not callable(listener):
            raise ValueError("invalidation listener must be callable")
        with self._lock:
            if listener in self._listeners:
                raise ValueError("invalidation listener is already subscribed")
            self._listeners.append(listener)
        closed = [False]

        def unsubscribe():
            """Detach this exact listener idempotently."""
            with self._lock:
                if closed[0]:
                    return
                closed[0] = True
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe

    def publish(
        self,
        snapshot,
        reason,
        feedback_token=None,
        removed=False,
    ):
        """Atomically publish a newer fragment and notify every subscriber.

        Invalidation is announced before the durable CAS.  Consequently a
        failed or conflicting store write leaves the compiler pending and its
        fresh reader behind the requested generation, which fails closed.
        A subscriber may return ``False`` only to suppress publication-origin
        feedback; that token then causes neither persistence nor recompilation.
        """
        reason = _validate_reason(reason)
        candidate_failed = False
        try:
            snapshot = _validated_provider_snapshot(snapshot)
            provider_id = _validate_provider_id(snapshot.provider_id)
            _validate_generation(snapshot.generation)
            if type(removed) is not bool:
                raise ValueError("removed must be a boolean")
            if provider_id != self.provider_id:
                raise ValueError("snapshot provider_id does not match adapter")
            candidate = FragmentAdapterState(
                self.provider_id,
                snapshot.generation,
                _semantic_fingerprint(snapshot, removed),
                snapshot,
                removed,
            )
        except Exception:
            candidate_failed = True
        if candidate_failed:
            raise ValueError(
                "fragment snapshot failed validation",
            )

        with self._lock:
            current = self._validate_loaded_state(self._load())
            if current is not None:
                if candidate.generation < current.generation:
                    raise ValueError(
                        "fragment generation {} regressed from {}".format(
                            candidate.generation,
                            current.generation,
                        )
                    )
                if candidate.generation == current.generation:
                    if candidate.semantic_fingerprint != (current.semantic_fingerprint):
                        raise ValueError("fragment generation {} was reused with different " "content".format(candidate.generation))
                    return False
            listeners = tuple(self._listeners)

            for listener in listeners:
                accepted = listener(
                    self.provider_id,
                    candidate.generation,
                    reason,
                    feedback_token,
                )
                if accepted is False:
                    return False

            try:
                committed = self._state_store.compare_and_store(
                    current,
                    candidate,
                )
            except Exception as exc:
                raise FragmentAdapterConflict(
                    "durable fragment publication failed: {}: {}".format(
                        type(exc).__name__,
                        exc,
                    )
                ) from exc
            if committed is not True:
                winner = self._validate_loaded_state(self._load())
                if winner == candidate:
                    return False
                raise FragmentAdapterConflict("durable fragment cursor changed before atomic publication")
            return True

    def remove(self, generation, reason, feedback_token=None):
        """Publish a durable removal tombstone and invalidate the compiler."""
        _validate_generation(generation)
        current = self.read_state()
        snapshot = current.snapshot
        tombstone = ProviderSnapshot(
            self.provider_id,
            generation,
            ProviderHealth.OFFLINE,
            _plain(snapshot.topology_fragment),
            snapshot.aliases,
            snapshot.identity_aliases,
            snapshot.role_assignments,
            snapshot.config_projections,
        )
        return self.publish(
            tombstone,
            reason,
            feedback_token=feedback_token,
            removed=True,
        )


class FragmentAdapterRegistry:
    """Default-off brand-neutral discovery and frozen compiler registry."""

    DISCOVERY_METHOD = "lattice_fragment_adapter"

    def __init__(self, enabled=False):
        """Create an empty registry; disabled is the safe default."""
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        self._enabled = enabled
        self._lock = threading.RLock()
        self._adapters = {}
        self._compiler = None
        self._unsubscribers = ()
        self._sealed = False

    @property
    def enabled(self):
        """Return whether explicit fragment discovery is enabled."""
        return self._enabled

    @property
    def provider_ids(self):
        """Return registered provider identities in deterministic order."""
        with self._lock:
            return tuple(sorted(self._adapters))

    @property
    def readers(self):
        """Return an immutable provider-reader mapping for inspection/tests."""
        with self._lock:
            return MappingProxyType({provider_id: adapter.read_snapshot for provider_id, adapter in self._adapters.items()})

    def _validate_adapter(self, adapter):
        """Validate the common adapter surface and its durable current state."""
        validation_failed = False
        try:
            provider_id = _validate_provider_id(
                getattr(adapter, "provider_id", None),
            )
            for method_name in (
                "read_state",
                "read_snapshot",
                "subscribe_invalidation",
            ):
                if not callable(getattr(adapter, method_name, None)):
                    raise ValueError("fragment adapter method is missing")
            state = _validated_fragment_state(adapter.read_state())
            if state.provider_id != provider_id:
                raise ValueError("adapter state provider mismatch")
        except Exception:
            validation_failed = True
        if validation_failed:
            raise ValueError(
                "fragment adapter failed validation",
            )
        return provider_id

    def discover(self, components):
        """Discover every component implementing the common publisher surface."""
        if not self._enabled:
            return ()
        candidates = []
        for component in tuple(components):
            factory = getattr(component, self.DISCOVERY_METHOD, None)
            if factory is None:
                continue
            if not callable(factory):
                raise ValueError("{} must be callable".format(self.DISCOVERY_METHOD))
            adapter = factory()
            if adapter is not None:
                candidates.append(adapter)

        validated = []
        seen = set()
        for adapter in candidates:
            provider_id = self._validate_adapter(adapter)
            if provider_id in seen:
                raise ValueError("provider {} was discovered more than once".format(provider_id))
            seen.add(provider_id)
            validated.append((provider_id, adapter))

        with self._lock:
            if self._sealed:
                raise RuntimeError("fragment registry membership is sealed")
            duplicate = sorted(provider_id for provider_id, _adapter in validated if provider_id in self._adapters)
            if duplicate:
                raise ValueError("provider {} is already registered".format(duplicate[0]))
            for provider_id, adapter in validated:
                self._adapters[provider_id] = adapter
            return tuple(provider_id for provider_id, _adapter in validated)

    def register(self, adapter):
        """Register one explicitly supplied generic adapter before sealing."""
        if not self._enabled:
            return False
        provider_id = self._validate_adapter(adapter)
        with self._lock:
            if self._sealed:
                raise RuntimeError("fragment registry membership is sealed")
            if provider_id in self._adapters:
                raise ValueError("provider {} is already registered".format(provider_id))
            self._adapters[provider_id] = adapter
            return True

    def unregister(self, provider_id):
        """Remove only pre-bind registration; runtime removal is a tombstone."""
        if not self._enabled:
            return False
        provider_id = _validate_provider_id(provider_id)
        with self._lock:
            if self._sealed:
                raise RuntimeError("runtime unregister is unsafe; publish a durable removal " "tombstone")
            if provider_id not in self._adapters:
                raise KeyError("unknown provider {}".format(provider_id))
            del self._adapters[provider_id]
            return True

    def create_compiler(self, state_store, override_reader=None):
        """Freeze membership and create the sole compiled-Lattice coordinator."""
        if not self._enabled:
            raise RuntimeError("fragment adapter registry is disabled")
        with self._lock:
            if self._sealed:
                raise RuntimeError("fragment registry membership is already sealed")
            if not self._adapters:
                raise RuntimeError("cannot create a compiler without fragment adapters")
            for adapter in self._adapters.values():
                self._validate_adapter(adapter)
            readers = {
                provider_id: partial(
                    _compiler_fragment_snapshot,
                    adapter,
                )
                for provider_id, adapter in self._adapters.items()
            }
            compiler = CompiledLatticeCompiler(
                readers,
                state_store=state_store,
                override_reader=override_reader,
            )

            unsubscribers = []
            try:
                for provider_id, adapter in sorted(self._adapters.items()):

                    def invalidate(
                        source_id,
                        generation,
                        reason,
                        feedback_token,
                        expected_id=provider_id,
                    ):
                        """Forward this adapter's invalidation to the compiler."""
                        if source_id != expected_id:
                            raise FragmentAdapterError(
                                "adapter {} emitted invalidation for {}".format(
                                    expected_id,
                                    source_id,
                                )
                            )
                        accepted = compiler.invalidate(
                            source_id,
                            generation,
                            reason,
                            feedback_token,
                        )
                        if feedback_token is not None and accepted is False:
                            return False
                        return True

                    unsubscribe = adapter.subscribe_invalidation(invalidate)
                    if not callable(unsubscribe):
                        raise ValueError("subscribe_invalidation must return an " "unsubscribe callable")
                    unsubscribers.append(unsubscribe)
            except Exception:
                for unsubscribe in reversed(unsubscribers):
                    unsubscribe()
                raise

            self._compiler = compiler
            self._unsubscribers = tuple(unsubscribers)
            self._sealed = True
            return compiler
