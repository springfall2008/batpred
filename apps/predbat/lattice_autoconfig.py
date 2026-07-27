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

# cspell:ignore autoconfig

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
class ProviderSnapshot:
    """One integration's immutable generation of health, topology, and aliases."""

    provider_id: str
    generation: int
    health: ProviderHealth
    topology_fragment: Mapping
    aliases: tuple = ()
    identity_aliases: tuple = ()

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
        object.__setattr__(self, "provider_id", self.provider_id.strip())
        object.__setattr__(self, "topology_fragment", _freeze(copy.deepcopy(dict(self.topology_fragment))))
        object.__setattr__(self, "aliases", aliases)
        object.__setattr__(self, "identity_aliases", identity_aliases)


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
class AutoConfigPlan:
    """Deterministic immutable result of compiling all usable fragments."""

    digest: str
    topology: Mapping
    aliases: tuple
    identity_aliases: tuple
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
    payload = {
        "provider": snapshot.provider_id,
        "generation": snapshot.generation,
        "health": snapshot.health.value,
        "fragment": snapshot.topology_fragment,
        "aliases": aliases,
        "identity_aliases": identity_aliases,
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


def _field_provenance(snapshots, bindings, identity_bindings, primary_target, control_target, topology_snapshot, canonical_by_node):
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


def compile_auto_config(snapshots):
    """Compile usable provider snapshots into one deterministic immutable plan."""
    snapshots = tuple(sorted(snapshots, key=lambda item: item.provider_id))
    if not snapshots:
        raise AutoConfigCompileError("no usable provider snapshots")
    provider_ids = [snapshot.provider_id for snapshot in snapshots]
    if len(provider_ids) != len(set(provider_ids)):
        raise AutoConfigCompileError("duplicate provider snapshots are not allowed")

    documents = []
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

    targets = {}
    for role in (AliasRole.PRIMARY, AliasRole.CONTROL):
        role_targets = sorted({binding.node_id for binding in bindings if role.value in binding.roles})
        if len(role_targets) > 1:
            raise AutoConfigCompileError("ambiguous {} target: {}".format(role.value, ", ".join(role_targets)))
        targets[role] = role_targets[0] if role_targets else None

    topology_snapshot = merge_topologies(documents)
    fields = []
    for binding in bindings:
        source = FieldProvenance(
            "/aliases/{}/node_id".format(binding.qualified_name),
            binding.provider_id,
            binding.generation,
            "/aliases/{}".format(binding.qualified_name.split(":", 1)[1]),
        )
        fields.append(AutoConfigField("alias.{}".format(binding.qualified_name), binding.node_id, (source,)))
    for name, role in (("primary_target", AliasRole.PRIMARY), ("control_target", AliasRole.CONTROL)):
        target = targets[role]
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
        "fields": [{"name": field.name, "value": field.value} for field in fields],
    }
    digest = hashlib.sha256(_canonical_json(semantic).encode("utf-8")).hexdigest()
    provenance = _field_provenance(
        snapshots,
        bindings,
        identity_bindings,
        targets[AliasRole.PRIMARY],
        targets[AliasRole.CONTROL],
        topology_snapshot,
        canonical_by_node,
    )
    return AutoConfigPlan(
        digest=digest,
        topology=_freeze(topology_snapshot.site),
        aliases=bindings,
        identity_aliases=identity_bindings,
        fields=fields,
        provenance=provenance,
        provider_generations=tuple((snapshot.provider_id, snapshot.generation) for snapshot in snapshots),
        warnings=tuple(topology_snapshot.warnings),
    )


class LatticeAutoConfigCompiler:
    """Thread-safe invalidation, compilation, and last-known-good coordinator."""

    def __init__(self, readers=None, materializer=None):
        """Create an idle compiler over provider snapshot readers."""
        self._readers = {}
        self._materializer = materializer
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
        if unavailable_active:
            detail = "previously active provider(s) unavailable: {}".format(", ".join(unavailable_active))
            return None, issues + (CompileIssue("active_provider_unavailable", detail), CompileIssue("compile_failed", detail))
        try:
            plan = compile_auto_config(snapshots)
        except (AutoConfigCompileError, TopologyValidationError, TypeError, ValueError) as exc:
            return None, issues + (CompileIssue("compile_failed", str(exc)),)
        return plan, issues

    def _materialize_if_changed(self, plan):
        """Hand a changed plan to the injected materializer exactly once."""
        with self._lock:
            if self._materializer is None or (self._active_plan is not None and plan.digest == self._active_plan.digest):
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
