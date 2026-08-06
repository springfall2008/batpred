# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice auto-configuration compiler
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Pure Lattice fragment compilation and invalidation state machine.

This module deliberately has no component registry, MQTT, Home Assistant, or
configuration-write dependency.  Integrations expose snapshot readers and
invalidate their monotonically increasing generations.  A later, separately
gated runtime component can supply a materializer.
"""

# cspell:ignore autoconfig popleft

import copy
import hashlib
import json
import threading
from collections import deque
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional

from lattice_topology import TopologyValidationError, decode_topology, merge_topologies


class ProviderHealth(Enum):
    """Health reported with a provider's fragment snapshot."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class AliasRole(Enum):
    """Semantic role assigned to a provider-qualified node alias."""

    REFERENCE = "reference"
    PRIMARY = "primary"
    CONTROL = "control"


class ProjectionValueKind(Enum):
    """Kind of one provider-published PredBat configuration value."""

    ENTITY = "entity"
    CONSTANT = "constant"
    NONE = "none"


class ProjectionRouting(Enum):
    """Relationship between projected values and selected indexed targets."""

    LEAF = "leaf"
    COORDINATOR = "coordinator"


class ProjectionCardinality(Enum):
    """Shape of one projected PredBat configuration argument."""

    SCALAR = "scalar"
    PER_INDEX = "per_index"


class CompileStatus(Enum):
    """Observable compiler state after a drain."""

    IDLE = "idle"
    FRESH = "fresh"
    DEGRADED = "degraded"
    STALE = "stale"


class AutoConfigCompileError(ValueError):
    """Raised internally when a candidate plan cannot be compiled safely."""


@dataclass(frozen=True)
class ProviderAlias:
    """A provider-local alias for one topology node."""

    name: str
    node_id: str
    roles: frozenset = frozenset((AliasRole.REFERENCE,))

    def __post_init__(self):
        """Validate and normalise alias identity and roles."""
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("alias name must be a non-empty string")
        if not isinstance(self.node_id, str) or not self.node_id.strip():
            raise ValueError("alias node_id must be a non-empty string")
        roles = frozenset(self.roles)
        if not roles or any(not isinstance(role, AliasRole) for role in roles):
            raise ValueError("alias roles must contain AliasRole values")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "node_id", self.node_id.strip())
        object.__setattr__(self, "roles", roles)


@dataclass(frozen=True)
class ProviderIdentityAlias:
    """A provider assertion that correlates a local node to a stable identity."""

    kind: str
    value: str
    node_id: str

    def __post_init__(self):
        """Validate and normalise the stable identity assertion."""
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("identity alias kind must be a non-empty string")
        if not isinstance(self.value, str) or not self.value.strip():
            raise ValueError("identity alias value must be a non-empty string")
        if not isinstance(self.node_id, str) or not self.node_id.strip():
            raise ValueError("identity alias node_id must be a non-empty string")
        object.__setattr__(self, "kind", self.kind.strip().lower())
        object.__setattr__(self, "value", self.value.strip())
        object.__setattr__(self, "node_id", self.node_id.strip())


@dataclass(frozen=True)
class ProviderRoleAssignment:
    """A provider-local indexed primary or control role assertion."""

    role: AliasRole
    group: str
    index: int
    node_id: str

    def __post_init__(self):
        """Validate and normalise one indexed role assertion."""
        if self.role not in (AliasRole.PRIMARY, AliasRole.CONTROL):
            raise ValueError("role assignment must be PRIMARY or CONTROL")
        if not isinstance(self.group, str) or not self.group.strip():
            raise ValueError("role assignment group must be a non-empty string")
        if not isinstance(self.index, int) or isinstance(self.index, bool) or self.index < 0:
            raise ValueError("role assignment index must be a non-negative integer")
        if not isinstance(self.node_id, str) or not self.node_id.strip():
            raise ValueError("role assignment node_id must be a non-empty string")
        object.__setattr__(self, "group", self.group.strip())
        object.__setattr__(self, "node_id", self.node_id.strip())


@dataclass(frozen=True)
class ProviderProjectionValue:
    """One ordered provider-local entity, constant, or explicit None value."""

    node_id: str
    kind: ProjectionValueKind
    value: object = None
    capability: Optional[str] = None
    identity_kind: Optional[str] = None
    identity_value: Optional[str] = None
    access_path_id: Optional[str] = None

    def __post_init__(self):
        """Validate the value and normalize its optional source selectors."""
        if not isinstance(self.node_id, str) or not self.node_id.strip():
            raise ValueError("projection value node_id must be a non-empty string")
        if not isinstance(self.kind, ProjectionValueKind):
            raise ValueError("projection value kind must be a ProjectionValueKind")
        if self.kind is ProjectionValueKind.ENTITY:
            if not isinstance(self.value, str) or not self.value.strip():
                raise ValueError("projection entity value must be a non-empty string")
            if not isinstance(self.capability, str) or not self.capability.strip():
                raise ValueError("projection entity value must name a capability")
            value = self.value.strip()
        elif self.kind is ProjectionValueKind.CONSTANT:
            if not isinstance(self.value, (str, int, float, bool)) or self.value is None:
                raise ValueError("projection constant must be a non-None JSON scalar")
            value = self.value
            _canonical_json(value)
        else:
            if self.value is not None:
                raise ValueError("projection None value must carry value=None")
            value = None

        capability = self.capability
        if capability is not None:
            if not isinstance(capability, str) or not capability.strip():
                raise ValueError("projection capability must be a non-empty string")
            capability = capability.strip()

        identity_kind = self.identity_kind
        identity_value = self.identity_value
        if (identity_kind is None) != (identity_value is None):
            raise ValueError("projection identity selector requires both kind and value")
        if identity_kind is not None:
            if not isinstance(identity_kind, str) or not identity_kind.strip():
                raise ValueError("projection identity kind must be a non-empty string")
            if not isinstance(identity_value, str) or not identity_value.strip():
                raise ValueError("projection identity value must be a non-empty string")
            identity_kind = identity_kind.strip().lower()
            identity_value = identity_value.strip()

        access_path_id = self.access_path_id
        if access_path_id is not None:
            if not isinstance(access_path_id, str) or not access_path_id.strip():
                raise ValueError("projection access_path_id must be a non-empty string")
            access_path_id = access_path_id.strip()

        object.__setattr__(self, "node_id", self.node_id.strip())
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "capability", capability)
        object.__setattr__(self, "identity_kind", identity_kind)
        object.__setattr__(self, "identity_value", identity_value)
        object.__setattr__(self, "access_path_id", access_path_id)


@dataclass(frozen=True)
class ProviderConfigProjection:
    """Generic provider contract for one PredBat configuration argument."""

    argument: str
    role: AliasRole
    group: str
    routing: ProjectionRouting
    cardinality: ProjectionCardinality
    values: tuple
    required: bool = True
    transforms: tuple = ()

    def __post_init__(self):
        """Validate and normalize one immutable projection declaration."""
        if not isinstance(self.argument, str) or not self.argument.strip():
            raise ValueError("projection argument must be a non-empty string")
        if self.role not in (AliasRole.PRIMARY, AliasRole.CONTROL):
            raise ValueError("projection role must be PRIMARY or CONTROL")
        if not isinstance(self.group, str) or not self.group.strip():
            raise ValueError("projection group must be a non-empty string")
        if not isinstance(self.routing, ProjectionRouting):
            raise ValueError("projection routing must be a ProjectionRouting")
        if not isinstance(self.cardinality, ProjectionCardinality):
            raise ValueError("projection cardinality must be a ProjectionCardinality")
        if not isinstance(self.required, bool):
            raise ValueError("projection required must be a boolean")
        values = tuple(self.values)
        if not values or any(not isinstance(value, ProviderProjectionValue) for value in values):
            raise ValueError("projection values must contain ProviderProjectionValue values")
        if self.cardinality is ProjectionCardinality.SCALAR and len(values) != 1:
            raise ValueError("scalar projection must contain exactly one value")
        transforms = tuple(self.transforms)
        if any(not isinstance(transform, str) or not transform.strip() for transform in transforms):
            raise ValueError("projection transforms must be non-empty strings")
        transforms = tuple(transform.strip() for transform in transforms)
        if len(transforms) != len(set(transforms)):
            raise ValueError("projection transforms must be unique")
        object.__setattr__(self, "argument", self.argument.strip())
        object.__setattr__(self, "group", self.group.strip())
        object.__setattr__(self, "values", values)
        object.__setattr__(self, "transforms", transforms)


@dataclass(frozen=True)
class UserConfigOverride:
    """Explicit user-owned final value for one projected configuration argument."""

    argument: str
    value: object
    source_path: str

    def __post_init__(self):
        """Detach the override value and retain its explicit source path."""
        if not isinstance(self.argument, str) or not self.argument.strip():
            raise ValueError("override argument must be a non-empty string")
        if not isinstance(self.source_path, str) or not self.source_path.strip():
            raise ValueError("override source_path must be a non-empty string")
        value = _freeze(copy.deepcopy(self.value))
        object.__setattr__(self, "argument", self.argument.strip())
        object.__setattr__(self, "value", value)
        object.__setattr__(self, "source_path", self.source_path.strip())


@dataclass(frozen=True)
class ProviderSnapshot:
    """One integration's immutable generation of health, topology, and aliases."""

    provider_id: str
    generation: int
    health: ProviderHealth
    topology_fragment: Mapping
    aliases: tuple = ()
    identity_aliases: tuple = ()
    role_assignments: tuple = ()
    config_projections: tuple = ()

    def __post_init__(self):
        """Validate scalar fields and detach caller-owned mutable data."""
        if not isinstance(self.provider_id, str) or not self.provider_id.strip():
            raise ValueError("provider_id must be a non-empty string")
        if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation < 0:
            raise ValueError("generation must be a non-negative integer")
        if not isinstance(self.health, ProviderHealth):
            raise ValueError("health must be a ProviderHealth")
        if not isinstance(self.topology_fragment, Mapping):
            raise ValueError("topology_fragment must be a mapping")
        aliases = tuple(self.aliases)
        if any(not isinstance(alias, ProviderAlias) for alias in aliases):
            raise ValueError("aliases must contain ProviderAlias values")
        identity_aliases = tuple(self.identity_aliases)
        if any(not isinstance(alias, ProviderIdentityAlias) for alias in identity_aliases):
            raise ValueError("identity_aliases must contain ProviderIdentityAlias values")
        role_assignments = tuple(self.role_assignments)
        if any(not isinstance(assignment, ProviderRoleAssignment) for assignment in role_assignments):
            raise ValueError("role_assignments must contain ProviderRoleAssignment values")
        config_projections = tuple(self.config_projections)
        if any(not isinstance(projection, ProviderConfigProjection) for projection in config_projections):
            raise ValueError("config_projections must contain ProviderConfigProjection values")
        object.__setattr__(self, "provider_id", self.provider_id.strip())
        object.__setattr__(self, "topology_fragment", _freeze(copy.deepcopy(dict(self.topology_fragment))))
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(self, "identity_aliases", identity_aliases)
        object.__setattr__(self, "role_assignments", role_assignments)
        object.__setattr__(self, "config_projections", config_projections)


@dataclass(frozen=True)
class AliasBinding:
    """A compiled provider-qualified alias."""

    qualified_name: str
    provider_id: str
    generation: int
    node_id: str
    roles: tuple


@dataclass(frozen=True)
class IdentityBinding:
    """One qualified stable-identity assertion in the compiled plan."""

    provider_id: str
    generation: int
    kind: str
    value: str
    local_node_id: str
    canonical_node_id: str


@dataclass(frozen=True)
class RoleAssignmentBinding:
    """One normalized provider assertion for an indexed plan role."""

    provider_id: str
    generation: int
    role: str
    group: str
    index: int
    local_node_id: str
    canonical_node_id: str


@dataclass(frozen=True)
class IndexedRoleTarget:
    """One deterministic indexed primary or control target."""

    group: str
    index: int
    node_id: str
    provenance: tuple


@dataclass(frozen=True)
class MaterializationReadiness:
    """Fail-closed decision exposed to a future config materializer."""

    ready: bool
    blockers: tuple

    def __post_init__(self):
        """Normalise blockers and reject contradictory readiness state."""
        if not isinstance(self.ready, bool):
            raise ValueError("materialization readiness must be a boolean")
        if isinstance(self.blockers, str):
            raise ValueError("materialization blockers must be an iterable of strings")
        try:
            blockers = tuple(self.blockers)
        except TypeError as exc:
            raise ValueError("materialization blockers must be an iterable of strings") from exc
        if any(not isinstance(blocker, str) for blocker in blockers):
            raise ValueError("materialization blockers must be non-empty strings")
        blockers = tuple(blocker.strip() for blocker in blockers)
        if any(not blocker for blocker in blockers):
            raise ValueError("materialization blockers must be non-empty strings")
        if len(blockers) != len(set(blockers)):
            raise ValueError("materialization blockers must be unique")
        if self.ready != (not blockers):
            raise ValueError("materialization readiness must equal absence of blockers")
        object.__setattr__(self, "blockers", blockers)


@dataclass(frozen=True)
class FieldProvenance:
    """Source coordinates for one generated plan field."""

    field_path: str
    provider_id: str
    generation: int
    source_path: str


@dataclass(frozen=True)
class AutoConfigField:
    """One generated auto-configuration field and its provenance."""

    name: str
    value: object
    provenance: tuple


@dataclass(frozen=True)
class ProjectionCandidate:
    """One normalized provider candidate for a projected argument."""

    argument: str
    provider_id: str
    generation: int
    role: str
    group: str
    routing: str
    cardinality: str
    required: bool
    values: tuple
    slot_indexes: tuple
    target_count: int
    value_kinds: tuple
    capabilities: tuple
    identity_selectors: tuple
    access_path_ids: tuple
    transforms: tuple
    specificities: tuple
    provenance: tuple


@dataclass(frozen=True)
class ProjectedConfigArgument:
    """Effective immutable PredBat argument plus all provider candidates."""

    name: str
    value: object
    cardinality: str
    required: bool
    transforms: tuple
    provenance: tuple
    candidate_provenance: tuple
    candidates: tuple
    override_source: Optional[str] = None


@dataclass(frozen=True)
class AutoConfigPlan:
    """Deterministic immutable result of compiling all usable fragments."""

    digest: str
    topology: Mapping
    aliases: tuple
    identity_aliases: tuple
    role_assignments: tuple
    primary_targets: tuple
    control_targets: tuple
    primary_target: Optional[str]
    control_target: Optional[str]
    materialization_readiness: MaterializationReadiness
    config_arguments: tuple
    projected_config: Mapping
    fields: tuple
    provenance: tuple
    provider_generations: tuple
    warnings: tuple


@dataclass(frozen=True)
class CompileIssue:
    """A provider-local or global diagnostic from one compile attempt."""

    code: str
    detail: str
    provider_id: Optional[str] = None


@dataclass(frozen=True)
class Invalidation:
    """One accepted provider cause retained across compile coalescing."""

    provider_id: str
    generation: int
    reason: str


@dataclass(frozen=True)
class MaterializationRequest:
    """A pure hand-off to a future materializer."""

    plan: AutoConfigPlan
    feedback_token: str


@dataclass(frozen=True)
class CompileRun:
    """Summary returned after draining one compile and one optional follow-up."""

    attempts: int
    status: CompileStatus
    plan: Optional[AutoConfigPlan]
    issues: tuple
    invalidations: tuple
    materializations: int
    pending: bool


def _freeze(value):
    """Recursively detach and freeze JSON-like values."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ValueError("unsupported non-JSON value {!r}".format(type(value).__name__))


def _plain(value):
    """Convert frozen JSON-like values and enums to canonical plain data."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple, frozenset)):
        items = [_plain(item) for item in value]
        return sorted(items) if isinstance(value, frozenset) else items
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ValueError("unsupported canonical value {!r}".format(type(value).__name__))


def _canonical_json(value):
    """Encode a value using the compiler's deterministic canonical form."""
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _semantic_topology(site):
    """Remove producer bookkeeping that cannot change materialized config."""
    topology = _plain(site)
    topology.pop("docVersion", None)
    topology.pop("producer", None)
    return topology


def _projection_payload(projection):
    """Return one provider projection in deterministic plain form."""
    return {
        "argument": projection.argument,
        "role": projection.role.value,
        "group": projection.group,
        "routing": projection.routing.value,
        "cardinality": projection.cardinality.value,
        "required": projection.required,
        "transforms": projection.transforms,
        "values": [
            {
                "node_id": value.node_id,
                "kind": value.kind.value,
                "value": value.value,
                "capability": value.capability,
                "identity_kind": value.identity_kind,
                "identity_value": value.identity_value,
                "access_path_id": value.access_path_id,
            }
            for value in projection.values
        ],
    }


def _fingerprint_snapshot(snapshot):
    """Bind a provider generation to exactly one health/fragment/alias value."""
    aliases = [
        {
            "name": alias.name,
            "node_id": alias.node_id,
            "roles": sorted(role.value for role in alias.roles),
        }
        for alias in sorted(snapshot.aliases, key=lambda item: (item.name, item.node_id, tuple(sorted(role.value for role in item.roles))))
    ]
    identity_aliases = [
        {
            "kind": alias.kind,
            "value": alias.value,
            "node_id": alias.node_id,
        }
        for alias in sorted(snapshot.identity_aliases, key=lambda item: (item.kind, item.value, item.node_id))
    ]
    role_assignments = [
        {
            "role": assignment.role.value,
            "group": assignment.group,
            "index": assignment.index,
            "node_id": assignment.node_id,
        }
        for assignment in sorted(snapshot.role_assignments, key=lambda item: (item.group, item.role.value, item.index, item.node_id))
    ]
    config_projections = [
        _projection_payload(projection)
        for projection in sorted(
            snapshot.config_projections,
            key=lambda item: (
                item.argument,
                item.group,
                item.role.value,
                item.routing.value,
                item.cardinality.value,
            ),
        )
    ]
    payload = {
        "provider": snapshot.provider_id,
        "generation": snapshot.generation,
        "health": snapshot.health.value,
        "fragment": snapshot.topology_fragment,
        "aliases": aliases,
        "identity_aliases": identity_aliases,
        "role_assignments": role_assignments,
        "config_projections": config_projections,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _node_identity(node):
    """Return safety-relevant identity fields for collision detection."""
    attributes = node.get("attributes") if isinstance(node.get("attributes"), Mapping) else {}
    return {
        "kind": node.get("kind"),
        "deviceType": node.get("deviceType"),
        "serial": attributes.get("serial"),
        "manufacturer": attributes.get("manufacturer"),
        "model": attributes.get("model"),
    }


def _identity_conflicts(left, right):
    """Return conflicting non-empty identity fields."""
    return tuple(sorted(key for key in left if left.get(key) not in (None, "") and right.get(key) not in (None, "") and left[key] != right[key]))


def _document_node_ids(document, provider_id):
    """Validate node identities within a fragment and return their ids."""
    nodes = document.get("nodes", ())
    seen = {}
    for node in nodes:
        node_id = str(node["id"])
        if node_id in seen:
            raise AutoConfigCompileError("provider {} repeats node identity {}".format(provider_id, node_id))
        seen[node_id] = _node_identity(node)
    return seen


def _find_group(parent, node):
    """Return a union-find root with path compression."""
    root = node
    while parent[root] != root:
        root = parent[root]
    while parent[node] != node:
        next_node = parent[node]
        parent[node] = root
        node = next_node
    return root


def _join_groups(parent, left, right):
    """Deterministically join two union-find identity groups."""
    left_root = _find_group(parent, left)
    right_root = _find_group(parent, right)
    if left_root == right_root:
        return
    if left_root < right_root:
        parent[right_root] = left_root
    else:
        parent[left_root] = right_root


def _correlate_identities(snapshots, documents, provider_nodes):
    """Correlate provider-local node ids through qualified stable assertions."""
    parent = {}
    for snapshot in snapshots:
        for node_id in provider_nodes[snapshot.provider_id]:
            node_key = (snapshot.provider_id, node_id)
            parent[node_key] = node_key

    assertions = {}
    identity_owners = {}
    for snapshot in snapshots:
        provider_assertions = set()
        for alias in sorted(snapshot.identity_aliases, key=lambda item: (item.kind, item.value, item.node_id)):
            node_key = (snapshot.provider_id, alias.node_id)
            if node_key not in parent:
                raise AutoConfigCompileError("identity alias {}:{}:{} targets unknown provider-local node {}".format(snapshot.provider_id, alias.kind, alias.value, alias.node_id))
            assertion_key = (snapshot.provider_id, alias.kind, alias.value)
            if assertion_key in provider_assertions:
                raise AutoConfigCompileError("identity alias collision for {}:{}:{}".format(*assertion_key))
            provider_assertions.add(assertion_key)
            identity_key = (alias.kind, alias.value)
            previous = identity_owners.get(identity_key)
            if previous is not None:
                _join_groups(parent, previous, node_key)
            else:
                identity_owners[identity_key] = node_key
            assertions[(snapshot.provider_id, alias.kind, alias.value, alias.node_id)] = node_key

    groups = {}
    for node_key in parent:
        groups.setdefault(_find_group(parent, node_key), []).append(node_key)
    group_assertions = {}
    for assertion_key, node_key in assertions.items():
        group_assertions.setdefault(_find_group(parent, node_key), []).append(assertion_key)

    canonical_by_node = {}
    canonical_owners = {}
    for root, members in sorted(groups.items()):
        providers = [member[0] for member in members]
        if len(providers) != len(set(providers)):
            raise AutoConfigCompileError("one provider correlates multiple local nodes in identity group {}".format(sorted(members)))

        aliases_by_kind = {}
        for _provider_id, kind, value, _node_id in group_assertions.get(root, ()):
            aliases_by_kind.setdefault(kind, set()).add(value)
        conflicts = {kind: values for kind, values in aliases_by_kind.items() if len(values) > 1}
        if conflicts:
            detail = ", ".join("{}={}".format(kind, sorted(values)) for kind, values in sorted(conflicts.items()))
            raise AutoConfigCompileError("strong identity aliases conflict ({})".format(detail))

        identities = [provider_nodes[provider_id][node_id] for provider_id, node_id in members]
        for index, left in enumerate(identities):
            for right in identities[index + 1 :]:
                identity_conflicts = _identity_conflicts(left, right)
                if identity_conflicts:
                    raise AutoConfigCompileError("identity collision in correlated group {} ({})".format(sorted(members), ", ".join(identity_conflicts)))

        stable_aliases = sorted((kind, next(iter(values))) for kind, values in aliases_by_kind.items())
        if stable_aliases:
            kind, value = min(stable_aliases, key=lambda item: (item[0] != "serial", item[0], item[1]))
            canonical_node_id = "identity:{}:{}".format(kind, value)
        else:
            provider_id, node_id = members[0]
            canonical_node_id = "provider:{}:{}".format(provider_id, node_id)
        previous_group = canonical_owners.get(canonical_node_id)
        if previous_group is not None and previous_group != root:
            raise AutoConfigCompileError("canonical identity collision for {}".format(canonical_node_id))
        canonical_owners[canonical_node_id] = root
        for member in members:
            canonical_by_node[member] = canonical_node_id

    normalized_documents = []
    for snapshot, document in zip(snapshots, documents):
        normalized = copy.deepcopy(document)
        local_map = {node_id: canonical_by_node[(snapshot.provider_id, node_id)] for node_id in provider_nodes[snapshot.provider_id]}
        for node in normalized.get("nodes", ()):
            node["id"] = local_map[str(node["id"])]
        for relationship in normalized.get("relationships", ()):
            for endpoint in ("from", "to"):
                endpoint_id = str(relationship.get(endpoint))
                if endpoint_id in local_map:
                    relationship[endpoint] = local_map[endpoint_id]
        normalized_documents.append(normalized)

    identity_bindings = tuple(
        sorted(
            (
                IdentityBinding(
                    provider_id=provider_id,
                    generation=next(snapshot.generation for snapshot in snapshots if snapshot.provider_id == provider_id),
                    kind=kind,
                    value=value,
                    local_node_id=node_id,
                    canonical_node_id=canonical_by_node[(provider_id, node_id)],
                )
                for provider_id, kind, value, node_id in assertions
            ),
            key=lambda item: (item.provider_id, item.kind, item.value, item.local_node_id),
        )
    )
    return tuple(normalized_documents), canonical_by_node, identity_bindings


def _compile_roles(snapshots, bindings, identity_bindings, provider_nodes, canonical_by_node):
    """Validate legacy/indexed roles and return deterministic target outputs."""
    legacy_by_role = {role: tuple(binding for binding in bindings if role.value in binding.roles) for role in (AliasRole.PRIMARY, AliasRole.CONTROL)}
    legacy_assignments = tuple(binding for role_bindings in legacy_by_role.values() for binding in role_bindings)

    role_bindings = []
    qualified_assignments = set()
    generation_by_provider = {snapshot.provider_id: snapshot.generation for snapshot in snapshots}
    for snapshot in snapshots:
        assignments = sorted(snapshot.role_assignments, key=lambda item: (item.group, item.role.value, item.index, item.node_id))
        for assignment in assignments:
            if assignment.node_id not in provider_nodes[snapshot.provider_id]:
                raise AutoConfigCompileError(
                    "role assignment {}:{}:{} targets unknown provider-local node {}".format(
                        assignment.group,
                        assignment.role.value,
                        assignment.index,
                        assignment.node_id,
                    )
                )
            qualified_key = (snapshot.provider_id, assignment.group, assignment.role.value, assignment.index)
            if qualified_key in qualified_assignments:
                raise AutoConfigCompileError(
                    "role assignment collision for {}:{}:{}:{}".format(
                        snapshot.provider_id,
                        assignment.group,
                        assignment.role.value,
                        assignment.index,
                    )
                )
            qualified_assignments.add(qualified_key)
            role_bindings.append(
                RoleAssignmentBinding(
                    provider_id=snapshot.provider_id,
                    generation=snapshot.generation,
                    role=assignment.role.value,
                    group=assignment.group,
                    index=assignment.index,
                    local_node_id=assignment.node_id,
                    canonical_node_id=canonical_by_node[(snapshot.provider_id, assignment.node_id)],
                )
            )
    role_bindings = tuple(
        sorted(
            role_bindings,
            key=lambda item: (item.group, item.role, item.index, item.provider_id, item.local_node_id),
        )
    )

    if role_bindings and legacy_assignments:
        raise AutoConfigCompileError("legacy and indexed role assignments cannot be mixed")

    legacy_targets = {}
    if not role_bindings:
        for role, role_aliases in legacy_by_role.items():
            if len(role_aliases) > 1:
                raise AutoConfigCompileError("ambiguous legacy {} assignments".format(role.value))
            legacy_targets[role] = role_aliases[0].node_id if role_aliases else None
    else:
        legacy_targets = {AliasRole.PRIMARY: None, AliasRole.CONTROL: None}

    indices_by_group_role = {}
    assignments_by_slot = {}
    for binding in role_bindings:
        group_role = (binding.group, binding.role)
        indices_by_group_role.setdefault(group_role, set()).add(binding.index)
        assignments_by_slot.setdefault((binding.group, binding.role, binding.index), []).append(binding)
    for (group, role), indices in sorted(indices_by_group_role.items()):
        ordered = sorted(indices)
        expected = list(range(ordered[-1] + 1))
        if ordered != expected:
            raise AutoConfigCompileError(
                "{} {} indices must be contiguous from zero; got {}".format(
                    group,
                    role,
                    ordered,
                )
            )

    correlated_providers = {}
    for binding in identity_bindings:
        correlated_providers.setdefault(binding.canonical_node_id, set()).add(binding.provider_id)

    indexed_targets = {AliasRole.PRIMARY: [], AliasRole.CONTROL: []}
    for (group, role_value, index), assignments in sorted(assignments_by_slot.items()):
        canonical_nodes = {assignment.canonical_node_id for assignment in assignments}
        if len(canonical_nodes) != 1:
            raise AutoConfigCompileError(
                "conflicting {} target for {} index {}: {}".format(
                    role_value,
                    group,
                    index,
                    sorted(canonical_nodes),
                )
            )
        canonical_node_id = next(iter(canonical_nodes))
        providers = {assignment.provider_id for assignment in assignments}
        if len(providers) > 1 and not providers.issubset(correlated_providers.get(canonical_node_id, set())):
            raise AutoConfigCompileError(
                "providers sharing {} {} index {} require explicit strong identity correlation".format(
                    group,
                    role_value,
                    index,
                )
            )
        provenance = tuple(
            FieldProvenance(
                "/{}_targets/{}/{}/node_id".format(role_value, group, index),
                assignment.provider_id,
                generation_by_provider[assignment.provider_id],
                "/role_assignments/{}/{}/{}".format(role_value, group, index),
            )
            for assignment in assignments
        )
        indexed_targets[AliasRole(role_value)].append(
            IndexedRoleTarget(
                group=group,
                index=index,
                node_id=canonical_node_id,
                provenance=provenance,
            )
        )

    primary_targets = tuple(indexed_targets[AliasRole.PRIMARY])
    control_targets = tuple(indexed_targets[AliasRole.CONTROL])
    blockers = []
    if not primary_targets:
        blockers.append("indexed_primary_targets_missing")
    if not control_targets:
        blockers.append("indexed_control_targets_missing")
    if legacy_assignments:
        blockers.append("legacy_role_assignments_present")
    blockers.append("config_projection_bindings_missing")
    readiness = MaterializationReadiness(ready=False, blockers=tuple(blockers))
    return role_bindings, primary_targets, control_targets, legacy_targets, readiness


def _projection_sort_key(projection):
    """Return the stable ordering key for provider projection declarations."""
    return (
        projection.argument,
        projection.group,
        projection.role.value,
        projection.routing.value,
        projection.cardinality.value,
        _canonical_json(_projection_payload(projection)),
    )


def _projection_topology_sources(document, provider_id):
    """Index provider-owned access paths and capability-to-path bindings."""
    access_paths = {}
    capabilities = set()
    for node in document.get("nodes", ()):
        node_id = str(node["id"])
        for access_path in node.get("accessPaths", ()):
            path_id = str(access_path["id"])
            owner = access_path.get("provider", provider_id)
            access_paths[(node_id, path_id)] = owner
        for capability in node.get("capabilities", ()):
            capabilities.add(
                (
                    node_id,
                    str(capability["capability"]),
                    str(capability["accessPath"]),
                )
            )
    return access_paths, capabilities


def _projection_override_value(override, candidate):
    """Validate and return an override in the provider candidate's shape."""
    value = override.value
    if candidate.cardinality == ProjectionCardinality.PER_INDEX.value:
        if not isinstance(value, tuple):
            raise AutoConfigCompileError("override {} must be an ordered per-index sequence".format(override.argument))
        if len(value) != candidate.target_count:
            raise AutoConfigCompileError(
                "override {} cardinality mismatch: expected {}, got {}".format(
                    override.argument,
                    candidate.target_count,
                    len(value),
                )
            )
        values = value
    else:
        if isinstance(value, (tuple, Mapping)):
            raise AutoConfigCompileError("override {} must be a scalar value".format(override.argument))
        values = (value,)
    for item in values:
        if not isinstance(item, (str, int, float, bool)) and item is not None:
            raise AutoConfigCompileError("override {} contains a non-scalar value".format(override.argument))
        _canonical_json(item)
    if candidate.required and any(item is None for item in values):
        raise AutoConfigCompileError("override {} leaves a required slot empty".format(override.argument))
    return value


def _projection_slot_indexes(projection, snapshot, targets, canonical_by_node):
    """Map provider-local projection values onto aggregate target slots."""
    if projection.cardinality is ProjectionCardinality.SCALAR:
        canonical_node_id = canonical_by_node[
            (
                snapshot.provider_id,
                projection.values[0].node_id,
            )
        ]
        target = targets[0]
        if canonical_node_id != target.node_id:
            raise AutoConfigCompileError(
                "projection {} slot 0 is unrelated to selected canonical target {}".format(
                    projection.argument,
                    target.node_id,
                )
            )
        return (0,)

    targets_by_index = {target.index: target for target in targets}
    owned_by_local_node = {}
    for assignment in sorted(
        snapshot.role_assignments,
        key=lambda item: (
            item.group,
            item.role.value,
            item.index,
            item.node_id,
        ),
    ):
        if assignment.role is not projection.role or assignment.group != projection.group:
            continue
        target = targets_by_index.get(assignment.index)
        if target is None:
            continue
        canonical_node_id = canonical_by_node[
            (
                snapshot.provider_id,
                assignment.node_id,
            )
        ]
        if target.node_id == canonical_node_id:
            owned_by_local_node.setdefault(
                assignment.node_id,
                deque(),
            ).append(assignment.index)

    available_by_node = {}
    for target in targets:
        available_by_node.setdefault(target.node_id, deque()).append(target.index)
    unowned_value_counts = {}
    for value in projection.values:
        if value.node_id in owned_by_local_node:
            continue
        canonical_node_id = canonical_by_node[
            (
                snapshot.provider_id,
                value.node_id,
            )
        ]
        unowned_value_counts[canonical_node_id] = unowned_value_counts.get(canonical_node_id, 0) + 1

    slot_indexes = []
    used_slots = set()
    for value in projection.values:
        canonical_node_id = canonical_by_node[
            (
                snapshot.provider_id,
                value.node_id,
            )
        ]
        available = owned_by_local_node.get(value.node_id)
        if available is None:
            available = available_by_node.get(canonical_node_id)
            while available and available[0] in used_slots:
                available.popleft()
            if available and unowned_value_counts[canonical_node_id] != len(available):
                raise AutoConfigCompileError(
                    "projection {} value for {} is ambiguous across aggregate slots {}".format(
                        projection.argument,
                        value.node_id,
                        tuple(available),
                    )
                )
            unowned_value_counts[canonical_node_id] -= 1
        if not available:
            raise AutoConfigCompileError(
                "projection {} value for {} is unrelated to an available selected target".format(
                    projection.argument,
                    value.node_id,
                )
            )
        target_index = available.popleft()
        if target_index in used_slots:
            raise AutoConfigCompileError(
                "projection {} repeats aggregate slot {}".format(
                    projection.argument,
                    target_index,
                )
            )
        slot_indexes.append(target_index)
        used_slots.add(target_index)
    return tuple(slot_indexes)


def _compile_config_projections(
    snapshots,
    primary_targets,
    control_targets,
    provider_nodes,
    canonical_by_node,
    documents_by_provider,
    user_overrides,
):
    """Compile provider projection candidates into effective PredBat arguments."""
    targets_by_role_group = {}
    for role, targets in (
        (AliasRole.PRIMARY, primary_targets),
        (AliasRole.CONTROL, control_targets),
    ):
        for target in targets:
            targets_by_role_group.setdefault((role, target.group), []).append(target)
    targets_by_role_group = {key: tuple(sorted(targets, key=lambda item: item.index)) for key, targets in targets_by_role_group.items()}

    candidates_by_argument = {}
    for snapshot in snapshots:
        access_paths, capabilities = _projection_topology_sources(
            documents_by_provider[snapshot.provider_id],
            snapshot.provider_id,
        )
        identity_assertions = {(alias.kind, alias.value, alias.node_id) for alias in snapshot.identity_aliases}
        seen_arguments = set()
        for projection_index, projection in enumerate(sorted(snapshot.config_projections, key=_projection_sort_key)):
            if projection.argument in seen_arguments:
                raise AutoConfigCompileError(
                    "provider {} repeats projection argument {}".format(
                        snapshot.provider_id,
                        projection.argument,
                    )
                )
            seen_arguments.add(projection.argument)
            targets = targets_by_role_group.get(
                (projection.role, projection.group),
                (),
            )
            if not targets:
                raise AutoConfigCompileError(
                    "projection {} has no selected {} targets in group {}".format(
                        projection.argument,
                        projection.role.value,
                        projection.group,
                    )
                )
            if projection.routing is ProjectionRouting.COORDINATOR and len({target.node_id for target in targets}) != 1:
                raise AutoConfigCompileError("coordinator projection {} requires one canonical target".format(projection.argument))
            for value in projection.values:
                if value.node_id not in provider_nodes[snapshot.provider_id]:
                    raise AutoConfigCompileError(
                        "projection {} targets unknown provider-local node {}".format(
                            projection.argument,
                            value.node_id,
                        )
                    )
            slot_indexes = _projection_slot_indexes(
                projection,
                snapshot,
                targets,
                canonical_by_node,
            )

            values = []
            kinds = []
            specificities = []
            provenance = []
            for value_index, value in enumerate(projection.values):
                if value.kind is ProjectionValueKind.ENTITY:
                    matching_capabilities = {
                        access_path_id
                        for (
                            node_id,
                            capability,
                            access_path_id,
                        ) in capabilities
                        if node_id == value.node_id and capability == value.capability
                    }
                    if not matching_capabilities:
                        raise AutoConfigCompileError(
                            "projection {} capability {} is not published by node {}".format(
                                projection.argument,
                                value.capability,
                                value.node_id,
                            )
                        )
                    if value.access_path_id is not None and value.access_path_id not in matching_capabilities:
                        raise AutoConfigCompileError(
                            "projection {} capability {} is not bound to access path {}".format(
                                projection.argument,
                                value.capability,
                                value.access_path_id,
                            )
                        )
                target_index = slot_indexes[value_index]

                specificity = 0
                if value.identity_kind is not None:
                    identity = (
                        value.identity_kind,
                        value.identity_value,
                        value.node_id,
                    )
                    if identity not in identity_assertions:
                        raise AutoConfigCompileError(
                            "projection {} identity selector is not asserted by provider {}".format(
                                projection.argument,
                                snapshot.provider_id,
                            )
                        )
                    specificity = 1
                if value.access_path_id is not None:
                    owner = access_paths.get((value.node_id, value.access_path_id))
                    if owner != snapshot.provider_id:
                        raise AutoConfigCompileError(
                            "projection {} access path {} is not provider-owned".format(
                                projection.argument,
                                value.access_path_id,
                            )
                        )
                    specificity = 2
                if projection.required and value.kind is ProjectionValueKind.NONE:
                    raise AutoConfigCompileError(
                        "projection {} leaves required slot {} empty".format(
                            projection.argument,
                            target_index,
                        )
                    )

                field_path = "/projected_config/{}".format(projection.argument)
                if projection.cardinality is ProjectionCardinality.PER_INDEX:
                    field_path += "/{}".format(target_index)
                values.append(value.value)
                kinds.append(value.kind.value)
                specificities.append(specificity)
                provenance.append(
                    (
                        FieldProvenance(
                            field_path,
                            snapshot.provider_id,
                            snapshot.generation,
                            "/config_projections/{}/values/{}".format(
                                projection_index,
                                value_index,
                            ),
                        ),
                    )
                )

            candidate = ProjectionCandidate(
                argument=projection.argument,
                provider_id=snapshot.provider_id,
                generation=snapshot.generation,
                role=projection.role.value,
                group=projection.group,
                routing=projection.routing.value,
                cardinality=projection.cardinality.value,
                required=projection.required,
                values=tuple(values),
                slot_indexes=slot_indexes,
                target_count=(len(targets) if projection.cardinality is ProjectionCardinality.PER_INDEX else 1),
                value_kinds=tuple(kinds),
                capabilities=tuple(value.capability for value in projection.values),
                identity_selectors=tuple(
                    (
                        value.identity_kind,
                        value.identity_value,
                    )
                    if value.identity_kind is not None
                    else None
                    for value in projection.values
                ),
                access_path_ids=tuple(value.access_path_id for value in projection.values),
                transforms=projection.transforms,
                specificities=tuple(specificities),
                provenance=tuple(provenance),
            )
            candidates_by_argument.setdefault(
                projection.argument,
                [],
            ).append(candidate)

    overrides = {}
    for override in tuple(user_overrides):
        if not isinstance(override, UserConfigOverride):
            raise ValueError("user_overrides must contain UserConfigOverride values")
        if override.argument in overrides:
            raise AutoConfigCompileError("duplicate user override for {}".format(override.argument))
        overrides[override.argument] = override
    unknown_overrides = sorted(set(overrides) - set(candidates_by_argument))
    if unknown_overrides:
        raise AutoConfigCompileError("user override has no provider candidate: {}".format(", ".join(unknown_overrides)))

    arguments = []
    projected_config = {}
    for argument, unordered_candidates in sorted(candidates_by_argument.items()):
        candidates = tuple(
            sorted(
                unordered_candidates,
                key=lambda item: (item.provider_id, item.generation),
            )
        )
        metadata = {
            (
                candidate.role,
                candidate.group,
                candidate.routing,
                candidate.cardinality,
                candidate.required,
                candidate.transforms,
                candidate.target_count,
            )
            for candidate in candidates
        }
        if len(metadata) != 1:
            raise AutoConfigCompileError("conflicting projection contract for {}".format(argument))
        first = candidates[0]
        candidate_provenance = tuple(source for candidate in candidates for slot_sources in candidate.provenance for source in slot_sources)
        override = overrides.get(argument)
        selected_values = []
        selected_provenance = []
        if override is None:
            for index in range(first.target_count):
                slot_candidates = tuple(
                    (
                        candidate,
                        candidate.slot_indexes.index(index),
                    )
                    for candidate in candidates
                    if index in candidate.slot_indexes
                )
                if not slot_candidates:
                    if first.required:
                        raise AutoConfigCompileError(
                            "projection {} cardinality mismatch: required slot {} has no provider candidate".format(
                                argument,
                                index,
                            )
                        )
                    selected_values.append(None)
                    continue
                highest_specificity = max(candidate.specificities[value_index] for candidate, value_index in slot_candidates)
                selected = tuple((candidate, value_index) for candidate, value_index in slot_candidates if candidate.specificities[value_index] == highest_specificity)
                kinds = {candidate.value_kinds[value_index] for candidate, value_index in selected}
                if len(kinds) != 1:
                    raise AutoConfigCompileError(
                        "projection {} type mismatch at slot {}".format(
                            argument,
                            index,
                        )
                    )
                canonical_values = {_canonical_json(candidate.values[value_index]) for candidate, value_index in selected}
                if len(canonical_values) != 1:
                    raise AutoConfigCompileError(
                        "ambiguous multi-provider projection {} at slot {}".format(
                            argument,
                            index,
                        )
                    )
                selected_values.append(selected[0][0].values[selected[0][1]])
                selected_provenance.extend(source for candidate, value_index in selected for source in candidate.provenance[value_index])
            if first.cardinality == ProjectionCardinality.PER_INDEX.value:
                effective_value = tuple(selected_values)
            else:
                effective_value = selected_values[0]
            override_source = None
        else:
            effective_value = _projection_override_value(override, first)
            override_source = override.source_path
            selected_provenance = [
                FieldProvenance(
                    "/projected_config/{}".format(argument),
                    "user_override",
                    0,
                    override.source_path,
                )
            ]

        effective_value = _freeze(effective_value)
        projected_config[argument] = effective_value
        arguments.append(
            ProjectedConfigArgument(
                name=argument,
                value=effective_value,
                cardinality=first.cardinality,
                required=first.required,
                transforms=first.transforms,
                provenance=tuple(selected_provenance),
                candidate_provenance=candidate_provenance,
                candidates=candidates,
                override_source=override_source,
            )
        )
    return (
        tuple(arguments),
        _freeze(projected_config),
    )


def _field_provenance(
    snapshots,
    bindings,
    identity_bindings,
    primary_target,
    control_target,
    primary_targets,
    control_targets,
    topology_snapshot,
    canonical_by_node,
):
    """Build deterministic source coordinates for every generated field."""
    provenance = []
    for snapshot in snapshots:
        provenance.append(FieldProvenance("/topology", snapshot.provider_id, snapshot.generation, "/"))
    for binding in bindings:
        provenance.append(
            FieldProvenance(
                "/aliases/{}/node_id".format(binding.qualified_name),
                binding.provider_id,
                binding.generation,
                "/aliases/{}".format(binding.qualified_name.split(":", 1)[1]),
            )
        )
    for binding in identity_bindings:
        provenance.append(
            FieldProvenance(
                "/identity_aliases/{}:{}:{}/canonical_node_id".format(binding.provider_id, binding.kind, binding.value),
                binding.provider_id,
                binding.generation,
                "/identity_aliases/{}:{}".format(binding.kind, binding.value),
            )
        )
    for field_path, target, role in (("/primary_target", primary_target, AliasRole.PRIMARY), ("/control_target", control_target, AliasRole.CONTROL)):
        if target is None:
            continue
        for binding in bindings:
            if binding.node_id == target and role.value in binding.roles:
                provenance.append(
                    FieldProvenance(
                        field_path,
                        binding.provider_id,
                        binding.generation,
                        "/aliases/{}".format(binding.qualified_name.split(":", 1)[1]),
                    )
                )
    for target in primary_targets + control_targets:
        provenance.extend(target.provenance)
    generations = {snapshot.provider_id: snapshot.generation for snapshot in snapshots}
    local_by_canonical = {(provider_id, canonical_node_id): local_node_id for (provider_id, local_node_id), canonical_node_id in canonical_by_node.items()}
    for key, source in topology_snapshot.provenance.items():
        local_node_id = local_by_canonical[(source.provider, source.node_id)]
        provenance.append(
            FieldProvenance(
                "/topology/nodes/{}/capabilities/{}@{}".format(key[0], key[1], key[2]),
                source.provider,
                generations[source.provider],
                "/nodes/{}/capabilities/{}@{}".format(local_node_id, source.capability, source.access_path),
            )
        )
    return tuple(sorted(provenance, key=lambda item: (item.field_path, item.provider_id, item.generation, item.source_path)))


def compile_auto_config(
    snapshots,
    user_overrides=(),
    atomic_materializer=False,
):
    """Compile usable provider snapshots into one deterministic immutable plan."""
    if not isinstance(atomic_materializer, bool):
        raise ValueError("atomic_materializer must be a boolean")
    snapshots = tuple(sorted(snapshots, key=lambda item: item.provider_id))
    if not snapshots:
        raise AutoConfigCompileError("no usable provider snapshots")
    provider_ids = [snapshot.provider_id for snapshot in snapshots]
    if len(provider_ids) != len(set(provider_ids)):
        raise AutoConfigCompileError("duplicate provider snapshots are not allowed")

    documents = []
    documents_by_provider = {}
    provider_nodes = {}
    for snapshot in snapshots:
        document = decode_topology(_plain(snapshot.topology_fragment))
        if document["scope"] != "fragment":
            raise AutoConfigCompileError("provider {} topology scope must be fragment".format(snapshot.provider_id))
        document_provider = document["producer"]["provider"]
        if document_provider != snapshot.provider_id:
            raise AutoConfigCompileError("provider {} supplied fragment owned by {}".format(snapshot.provider_id, document_provider))
        nodes = _document_node_ids(document, snapshot.provider_id)
        provider_nodes[snapshot.provider_id] = nodes
        documents_by_provider[snapshot.provider_id] = document
        documents.append(document)
    documents, canonical_by_node, identity_bindings = _correlate_identities(snapshots, documents, provider_nodes)

    bindings = []
    qualified = set()
    for snapshot in snapshots:
        for alias in sorted(snapshot.aliases, key=lambda item: (item.name, item.node_id, tuple(sorted(role.value for role in item.roles)))):
            qualified_name = "{}:{}".format(snapshot.provider_id, alias.name)
            if qualified_name in qualified:
                raise AutoConfigCompileError("alias collision for {}".format(qualified_name))
            qualified.add(qualified_name)
            if alias.node_id not in provider_nodes[snapshot.provider_id]:
                raise AutoConfigCompileError("alias {} targets unknown provider-local node {}".format(qualified_name, alias.node_id))
            bindings.append(
                AliasBinding(
                    qualified_name=qualified_name,
                    provider_id=snapshot.provider_id,
                    generation=snapshot.generation,
                    node_id=canonical_by_node[(snapshot.provider_id, alias.node_id)],
                    roles=tuple(sorted(role.value for role in alias.roles)),
                )
            )
    bindings = tuple(sorted(bindings, key=lambda item: item.qualified_name))

    role_bindings, primary_targets, control_targets, legacy_targets, readiness = _compile_roles(
        snapshots,
        bindings,
        identity_bindings,
        provider_nodes,
        canonical_by_node,
    )
    primary_target = legacy_targets[AliasRole.PRIMARY]
    control_target = legacy_targets[AliasRole.CONTROL]

    topology_snapshot = merge_topologies(documents)
    config_arguments, projected_config = _compile_config_projections(
        snapshots,
        primary_targets,
        control_targets,
        provider_nodes,
        canonical_by_node,
        documents_by_provider,
        user_overrides,
    )
    if config_arguments:
        blockers = tuple(blocker for blocker in readiness.blockers if blocker != "config_projection_bindings_missing")
        if not atomic_materializer:
            blockers += ("atomic_materializer_missing",)
        readiness = MaterializationReadiness(
            ready=not blockers,
            blockers=blockers,
        )

    fields = []
    for binding in bindings:
        source = FieldProvenance(
            "/aliases/{}/node_id".format(binding.qualified_name),
            binding.provider_id,
            binding.generation,
            "/aliases/{}".format(binding.qualified_name.split(":", 1)[1]),
        )
        fields.append(AutoConfigField("alias.{}".format(binding.qualified_name), binding.node_id, (source,)))
    for name, target, role in (
        ("primary_target", primary_target, AliasRole.PRIMARY),
        ("control_target", control_target, AliasRole.CONTROL),
    ):
        if target is None:
            continue
        sources = tuple(
            FieldProvenance(
                "/{}".format(name),
                binding.provider_id,
                binding.generation,
                "/aliases/{}".format(binding.qualified_name.split(":", 1)[1]),
            )
            for binding in bindings
            if binding.node_id == target and role.value in binding.roles
        )
        fields.append(AutoConfigField(name, target, sources))
    for name, targets in (("primary_targets", primary_targets), ("control_targets", control_targets)):
        for target in targets:
            fields.append(
                AutoConfigField(
                    "{}.{}.{}".format(name, target.group, target.index),
                    target.node_id,
                    target.provenance,
                )
            )
    for argument in config_arguments:
        fields.append(
            AutoConfigField(
                "config.{}".format(argument.name),
                argument.value,
                argument.provenance,
            )
        )
    fields = tuple(sorted(fields, key=lambda item: item.name))

    semantic = {
        "topology": _semantic_topology(topology_snapshot.site),
        "aliases": [
            {
                "qualified_name": binding.qualified_name,
                "node_id": binding.node_id,
                "roles": binding.roles,
            }
            for binding in bindings
        ],
        "identity_aliases": [
            {
                "provider_id": binding.provider_id,
                "kind": binding.kind,
                "value": binding.value,
                "local_node_id": binding.local_node_id,
                "canonical_node_id": binding.canonical_node_id,
            }
            for binding in identity_bindings
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
            for binding in role_bindings
        ],
        "primary_targets": [{"group": target.group, "index": target.index, "node_id": target.node_id} for target in primary_targets],
        "control_targets": [{"group": target.group, "index": target.index, "node_id": target.node_id} for target in control_targets],
        "primary_target": primary_target,
        "control_target": control_target,
        "materialization_readiness": {
            "ready": readiness.ready,
            "blockers": readiness.blockers,
        },
        "config_arguments": [
            {
                "name": argument.name,
                "value": argument.value,
                "cardinality": argument.cardinality,
                "required": argument.required,
                "transforms": argument.transforms,
            }
            for argument in config_arguments
        ],
        "projected_config": projected_config,
        "fields": [{"name": field.name, "value": field.value} for field in fields],
    }
    digest = hashlib.sha256(_canonical_json(semantic).encode("utf-8")).hexdigest()
    base_provenance = _field_provenance(
        snapshots,
        bindings,
        identity_bindings,
        primary_target,
        control_target,
        primary_targets,
        control_targets,
        topology_snapshot,
        canonical_by_node,
    )
    projection_provenance = tuple(source for argument in config_arguments for source in (argument.candidate_provenance + argument.provenance))
    provenance = tuple(
        sorted(
            set(base_provenance + projection_provenance),
            key=lambda item: (
                item.field_path,
                item.provider_id,
                item.generation,
                item.source_path,
            ),
        )
    )
    return AutoConfigPlan(
        digest=digest,
        topology=_freeze(topology_snapshot.site),
        aliases=bindings,
        identity_aliases=identity_bindings,
        role_assignments=role_bindings,
        primary_targets=primary_targets,
        control_targets=control_targets,
        primary_target=primary_target,
        control_target=control_target,
        materialization_readiness=readiness,
        config_arguments=config_arguments,
        projected_config=projected_config,
        fields=fields,
        provenance=provenance,
        provider_generations=tuple((snapshot.provider_id, snapshot.generation) for snapshot in snapshots),
        warnings=tuple(topology_snapshot.warnings),
    )


class LatticeAutoConfigCompiler:
    """Thread-safe invalidation, compilation, and last-known-good coordinator."""

    def __init__(
        self,
        readers=None,
        materializer=None,
        atomic_materializer=False,
        allow_provider_failover=False,
    ):
        """Create an idle compiler over provider snapshot readers."""
        if not isinstance(atomic_materializer, bool):
            raise ValueError("atomic_materializer must be a boolean")
        if not isinstance(allow_provider_failover, bool):
            raise ValueError("allow_provider_failover must be a boolean")
        self._readers = {}
        self._materializer = materializer
        self._atomic_materializer = atomic_materializer
        self._allow_provider_failover = allow_provider_failover
        self._lock = threading.RLock()
        self._compiling = False
        self._pending = False
        self._follow_up = False
        self._requested_generations = {}
        self._observed_generations = {}
        self._generation_fingerprints = {}
        self._invalidations = set()
        self._feedback_tokens = deque(maxlen=64)
        self._token_counter = 0
        self._active_plan = None
        self._status = CompileStatus.IDLE
        self._issues = ()
        for provider_id, reader in (readers or {}).items():
            self.register_provider(provider_id, reader)

    @property
    def active_plan(self):
        """Return the last-known-good immutable plan."""
        with self._lock:
            return self._active_plan

    @property
    def status(self):
        """Return the current observable compile status."""
        with self._lock:
            return self._status

    def register_provider(self, provider_id, reader):
        """Register one integration's side-effect-free snapshot reader."""
        if not isinstance(provider_id, str) or not provider_id.strip():
            raise ValueError("provider_id must be a non-empty string")
        if not callable(reader):
            raise ValueError("provider reader must be callable")
        provider_id = provider_id.strip()
        with self._lock:
            if self._compiling:
                raise RuntimeError("cannot register a provider during compilation")
            if provider_id in self._readers:
                raise ValueError("provider {} is already registered".format(provider_id))
            self._readers[provider_id] = reader
            self._pending = True

    def invalidate(self, provider_id, generation, reason, feedback_token=None):
        """Accept a newer provider generation and schedule recompilation.

        Returns ``False`` for a stale/replayed generation or a materializer
        feedback event.  Unknown providers fail closed instead of silently
        creating an unobservable input.
        """
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise ValueError("generation must be a non-negative integer")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        with self._lock:
            if provider_id not in self._readers:
                raise KeyError("unknown provider {}".format(provider_id))
            if feedback_token is not None and feedback_token in self._feedback_tokens:
                return False
            previous = max(self._requested_generations.get(provider_id, -1), self._observed_generations.get(provider_id, -1))
            if generation <= previous:
                return False
            self._requested_generations[provider_id] = generation
            self._invalidations.add(Invalidation(provider_id, generation, reason.strip()))
            if self._compiling:
                self._follow_up = True
            else:
                self._pending = True
            return True

    def _read_all(self):
        """Fresh-read every registered provider, isolating local failures."""
        with self._lock:
            readers = tuple(sorted(self._readers.items()))
        usable = []
        issues = []
        for provider_id, reader in readers:
            try:
                snapshot = reader()
                if not isinstance(snapshot, ProviderSnapshot):
                    raise ValueError("reader must return ProviderSnapshot")
                if snapshot.provider_id != provider_id:
                    raise ValueError("reader returned provider {}".format(snapshot.provider_id))
                fingerprint = _fingerprint_snapshot(snapshot)
                with self._lock:
                    required_generation = self._requested_generations.get(provider_id, -1)
                    previous_generation = self._observed_generations.get(provider_id, -1)
                    previous_fingerprint = self._generation_fingerprints.get((provider_id, snapshot.generation))
                    if snapshot.generation < previous_generation:
                        raise ValueError("snapshot generation {} regressed from {}".format(snapshot.generation, previous_generation))
                    if previous_fingerprint is not None and previous_fingerprint != fingerprint:
                        raise ValueError("snapshot generation {} was reused with different content".format(snapshot.generation))
                    if snapshot.generation < required_generation:
                        raise ValueError("snapshot generation {} is behind invalidation {}".format(snapshot.generation, required_generation))
                    self._observed_generations[provider_id] = snapshot.generation
                    self._requested_generations[provider_id] = max(required_generation, snapshot.generation)
                    self._generation_fingerprints[(provider_id, snapshot.generation)] = fingerprint
                if snapshot.health is ProviderHealth.OFFLINE:
                    issues.append(CompileIssue("provider_offline", "provider is offline", provider_id))
                    continue
                decode_topology(_plain(snapshot.topology_fragment))
                if snapshot.health is ProviderHealth.DEGRADED:
                    issues.append(CompileIssue("provider_degraded", "provider reports degraded health", provider_id))
                usable.append(snapshot)
            except (AutoConfigCompileError, TopologyValidationError, TypeError, ValueError) as exc:
                issues.append(CompileIssue("provider_invalid", str(exc), provider_id))
            except Exception as exc:
                issues.append(CompileIssue("provider_read_failed", "{}: {}".format(type(exc).__name__, exc), provider_id))
        return tuple(usable), tuple(issues)

    def _compile_attempt(self):
        """Compile one fresh all-provider snapshot read."""
        snapshots, issues = self._read_all()
        with self._lock:
            active_providers = set(dict(self._active_plan.provider_generations)) if self._active_plan is not None else set()
        usable_providers = {snapshot.provider_id for snapshot in snapshots}
        unavailable_active = sorted(active_providers - usable_providers)
        if unavailable_active and not self._allow_provider_failover:
            detail = "previously active provider(s) unavailable: {}".format(", ".join(unavailable_active))
            return None, issues + (CompileIssue("active_provider_unavailable", detail), CompileIssue("compile_failed", detail))
        try:
            plan = compile_auto_config(
                snapshots,
                atomic_materializer=self._atomic_materializer,
            )
        except (AutoConfigCompileError, TopologyValidationError, TypeError, ValueError) as exc:
            return None, issues + (CompileIssue("compile_failed", str(exc)),)
        return plan, issues

    def _materialize_if_changed(self, plan):
        """Hand a changed plan to the injected materializer exactly once."""
        with self._lock:
            if not plan.materialization_readiness.ready or self._materializer is None or (self._active_plan is not None and plan.digest == self._active_plan.digest):
                return 0, ()
            self._token_counter += 1
            feedback_token = "lattice-autoconfig-{}".format(self._token_counter)
            self._feedback_tokens.append(feedback_token)
            try:
                self._materializer(MaterializationRequest(plan, feedback_token))
            except Exception as exc:
                return 0, (CompileIssue("materialization_failed", "{}: {}".format(type(exc).__name__, exc)),)
            return 1, ()

    def drain(self):
        """Run one pending compile plus at most one guaranteed follow-up."""
        with self._lock:
            if self._compiling or not self._pending:
                return CompileRun(0, self._status, self._active_plan, self._issues, (), 0, self._pending)
            self._compiling = True
            self._pending = False
            self._follow_up = False
            invalidations = tuple(sorted(self._invalidations, key=lambda item: (item.provider_id, item.generation, item.reason)))
            self._invalidations.clear()

        attempts = 0
        materializations = 0
        aggregate_issues = ()
        final_issues = ()
        candidate = None
        try:
            while attempts < 2:
                attempts += 1
                plan, issues = self._compile_attempt()
                aggregate_issues += issues

                with self._lock:
                    follow_up = self._follow_up
                    self._follow_up = False
                    if follow_up:
                        invalidations += tuple(sorted(self._invalidations, key=lambda item: (item.provider_id, item.generation, item.reason)))
                        self._invalidations.clear()
                    if follow_up and attempts < 2:
                        continue
                    if follow_up:
                        self._pending = True
                        superseded = CompileIssue("compile_superseded", "candidate superseded by an invalidation during the bounded follow-up")
                        aggregate_issues += (superseded,)
                        final_issues = issues + (superseded,)
                        candidate = None
                    else:
                        final_issues = issues
                        candidate = plan
                    break

            with self._lock:
                # Linearize the final candidate decision and materializer call
                # with invalidation.  RLock permits same-thread token feedback.
                if self._follow_up:
                    self._pending = True
                    invalidations += tuple(sorted(self._invalidations, key=lambda item: (item.provider_id, item.generation, item.reason)))
                    self._invalidations.clear()
                    self._follow_up = False
                    superseded = CompileIssue("compile_superseded", "candidate superseded before materialization")
                    aggregate_issues += (superseded,)
                    final_issues += (superseded,)
                    candidate = None
                if candidate is not None:
                    count, materializer_issues = self._materialize_if_changed(candidate)
                    materializations += count
                    aggregate_issues += materializer_issues
                    final_issues += materializer_issues
                    if not materializer_issues:
                        self._active_plan = candidate
                if self._follow_up:
                    self._pending = True
                    invalidations += tuple(sorted(self._invalidations, key=lambda item: (item.provider_id, item.generation, item.reason)))
                    self._invalidations.clear()
                    self._follow_up = False

                fatal_codes = ("compile_failed", "compile_superseded", "materialization_failed")
                fatal = any(issue.code in fatal_codes for issue in final_issues)
                if fatal:
                    # Retry is caller/backoff-driven.  It deliberately does not
                    # consume another attempt inside this drain.
                    self._pending = True
                if self._active_plan is None or fatal:
                    self._status = CompileStatus.STALE
                elif final_issues:
                    self._status = CompileStatus.DEGRADED
                else:
                    self._status = CompileStatus.FRESH
                self._issues = aggregate_issues
                self._compiling = False
                result = CompileRun(
                    attempts,
                    self._status,
                    self._active_plan,
                    aggregate_issues,
                    tuple(sorted(set(invalidations), key=lambda item: (item.provider_id, item.generation, item.reason))),
                    materializations,
                    self._pending,
                )
            return result
        except Exception:
            with self._lock:
                self._compiling = False
                if self._follow_up:
                    self._pending = True
                    self._follow_up = False
            raise
