# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice auto-configuration shadow comparison
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Pure, default-off comparison of Lattice and legacy auto-configuration.

This module is intentionally observational.  It accepts immutable compiler
output and a detached legacy observation, returns immutable telemetry, and has
no registry, persistence, materializer, configuration-write, or control-write
dependency.
"""

# cspell:ignore autoconfig

import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional

from lattice_autoconfig import CompileStatus, MaterializationReadiness
from lattice_compiled_publication import (
    CompiledLatticeDiagnostics,
    CompiledLatticePublication,
    CompiledLatticeRun,
)


DEFAULT_COMPILER_VERSION = "lattice-autoconfig/1"


class _MissingValue:
    """Private marker that preserves absent versus explicit-None semantics."""

    __slots__ = ()


_MISSING = _MissingValue()


class ShadowDivergenceReason(Enum):
    """Bounded reason vocabulary for one semantic disagreement."""

    MISSING_LEGACY = "missing_legacy"
    MISSING_LATTICE = "missing_lattice"
    TYPE_MISMATCH = "type_mismatch"
    VALUE_MISMATCH = "value_mismatch"
    COMPARISON_LIMIT = "comparison_limit"


@dataclass(frozen=True)
class ShadowComparisonLimits:
    """Hard bounds for deterministic, safe shadow telemetry."""

    max_divergences: int = 64
    max_nodes: int = 8192
    max_depth: int = 32
    max_path_length: int = 256
    max_summary_length: int = 160

    def __post_init__(self):
        """Reject non-positive or impractically small comparison bounds."""
        for name in (
            "max_divergences",
            "max_nodes",
            "max_depth",
            "max_path_length",
            "max_summary_length",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("{} must be a positive integer".format(name))
        if self.max_path_length < 24:
            raise ValueError("max_path_length must be at least 24")
        if self.max_summary_length < 16:
            raise ValueError("max_summary_length must be at least 16")


@dataclass(frozen=True)
class LegacyAutoConfigObservation:
    """Detached point-in-time legacy auto-config observation for one hub."""

    hub_id: str
    projected_config: Mapping
    primary_target: object = _MISSING
    control_target: object = _MISSING
    primary_targets: object = _MISSING
    control_targets: object = _MISSING
    routing: object = _MISSING
    provenance: object = _MISSING
    capabilities: frozenset = frozenset()

    def __post_init__(self):
        """Validate identity and recursively detach all caller-owned inputs."""
        if not isinstance(self.hub_id, str) or not self.hub_id.strip():
            raise ValueError("legacy observation hub_id must be a non-empty string")
        if not isinstance(self.projected_config, Mapping):
            raise ValueError("legacy projected_config must be a mapping")
        capabilities = frozenset(self.capabilities)
        if any(not isinstance(item, str) or not item.strip() for item in capabilities):
            raise ValueError("legacy capabilities must be non-empty strings")
        capabilities = frozenset(item.strip() for item in capabilities)
        object.__setattr__(self, "hub_id", self.hub_id.strip())
        object.__setattr__(
            self,
            "projected_config",
            _freeze(dict(self.projected_config)),
        )
        for name in (
            "primary_target",
            "control_target",
            "primary_targets",
            "control_targets",
            "routing",
            "provenance",
        ):
            value = getattr(self, name)
            if value is not _MISSING:
                object.__setattr__(self, name, _freeze(value))
        object.__setattr__(self, "capabilities", capabilities)


@dataclass(frozen=True)
class ShadowCanaryGates:
    """Explicit allowlists and health requirements; all default closed."""

    enabled: bool = False
    hub_ids: frozenset = frozenset()
    provider_ids: frozenset = frozenset()
    capability_ids: frozenset = frozenset()
    allow_stale: bool = False
    allow_degraded: bool = False
    require_materialization_ready: bool = True

    def __post_init__(self):
        """Normalize immutable allowlists and validate every switch."""
        for name in (
            "enabled",
            "allow_stale",
            "allow_degraded",
            "require_materialization_ready",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError("{} must be a boolean".format(name))
        for name in ("hub_ids", "provider_ids", "capability_ids"):
            values = frozenset(getattr(self, name))
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError("{} must contain non-empty strings".format(name))
            object.__setattr__(
                self,
                name,
                frozenset(value.strip() for value in values),
            )


@dataclass(frozen=True)
class ShadowDivergence:
    """One deterministic, bounded disagreement."""

    path: str
    reason: ShadowDivergenceReason
    lattice_summary: str
    legacy_summary: str

    def __post_init__(self):
        """Keep telemetry scalar, non-empty, and safe to serialize."""
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("divergence path must be a non-empty string")
        if not isinstance(self.reason, ShadowDivergenceReason):
            raise ValueError("divergence reason must be ShadowDivergenceReason")
        if not isinstance(self.lattice_summary, str) or not isinstance(
            self.legacy_summary,
            str,
        ):
            raise ValueError("divergence summaries must be strings")


@dataclass(frozen=True)
class ShadowInvalidationCause:
    """Serializable integration or override invalidation identity."""

    source_type: str
    source_id: str
    generation: int
    reason: str

    def __post_init__(self):
        """Validate one compiler-supplied cause without broadening its shape."""
        if self.source_type not in ("provider", "user_override"):
            raise ValueError("shadow cause source_type is invalid")
        if not isinstance(self.source_id, str) or not self.source_id:
            raise ValueError("shadow cause source_id must be a non-empty string")
        if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation < 0:
            raise ValueError("shadow cause generation must be non-negative")
        if not isinstance(self.reason, str) or not self.reason:
            raise ValueError("shadow cause reason must be a non-empty string")


@dataclass(frozen=True)
class ShadowProviderInput:
    """Current provider cursor plus change information from the prior version."""

    provider_id: str
    generation: Optional[int]
    requested_generation: int
    fingerprint: Optional[str]
    observation_pending: bool
    previous_generation: Optional[int]
    previous_fingerprint: Optional[str]
    generation_changed: Optional[bool]
    fingerprint_changed: Optional[bool]

    def __post_init__(self):
        """Reject incomplete or contradictory provider cursor telemetry."""
        if not isinstance(self.provider_id, str) or not self.provider_id:
            raise ValueError("shadow provider_id must be a non-empty string")
        if not isinstance(self.requested_generation, int) or isinstance(self.requested_generation, bool) or self.requested_generation < 0:
            raise ValueError("requested_generation must be a non-negative integer")
        if (self.generation is None) != (self.fingerprint is None):
            raise ValueError("current provider cursor must be wholly present or absent")
        if self.generation is not None:
            if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation < 0:
                raise ValueError("generation must be non-negative")
            if not isinstance(self.fingerprint, str) or not self.fingerprint:
                raise ValueError("shadow provider fingerprint must be non-empty")
        if self.generation is not None and self.requested_generation < self.generation:
            raise ValueError("requested_generation must cover generation")
        if not isinstance(self.observation_pending, bool):
            raise ValueError("observation_pending must be a boolean")
        if self.observation_pending != (self.generation is None or self.requested_generation > self.generation):
            raise ValueError("observation_pending must match requested and observed generations")
        if (self.previous_generation is None) != (self.previous_fingerprint is None):
            raise ValueError("previous provider cursor must be wholly present or absent")
        if self.previous_generation is not None:
            if not isinstance(self.previous_generation, int) or isinstance(self.previous_generation, bool) or self.previous_generation < 0:
                raise ValueError("previous_generation must be non-negative")
            if not isinstance(self.previous_fingerprint, str) or not self.previous_fingerprint:
                raise ValueError("previous_fingerprint must be non-empty")
            if self.generation_changed != (self.generation != self.previous_generation):
                raise ValueError("generation_changed must match provider cursors")
            if self.fingerprint_changed != (self.fingerprint != self.previous_fingerprint):
                raise ValueError("fingerprint_changed must match provider cursors")
        elif self.generation_changed is not None or self.fingerprint_changed is not None:
            raise ValueError("change flags require a previous provider cursor")


@dataclass(frozen=True)
class ShadowRecompileTelemetry:
    """Observation of invalidation coalescing and the bounded drain."""

    triggered: bool
    attempts: int
    coalesced: bool
    bounded_follow_up_used: bool
    follow_up_pending: bool
    causes: tuple

    def __post_init__(self):
        """Validate the compiler's bounded-run summary."""
        if not isinstance(self.attempts, int) or isinstance(self.attempts, bool) or self.attempts < 0:
            raise ValueError("recompile attempts must be a non-negative integer")
        for name in (
            "triggered",
            "coalesced",
            "bounded_follow_up_used",
            "follow_up_pending",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError("{} must be a boolean".format(name))
        causes = tuple(self.causes)
        if any(not isinstance(item, ShadowInvalidationCause) for item in causes):
            raise ValueError("recompile causes must contain ShadowInvalidationCause values")
        if self.triggered != bool(causes):
            raise ValueError("recompile triggered must match cause presence")
        if self.coalesced != (len(causes) > 1):
            raise ValueError("recompile coalesced must match cause count")
        if self.bounded_follow_up_used != (self.attempts > 1):
            raise ValueError("bounded_follow_up_used must match attempts")
        object.__setattr__(self, "causes", causes)


@dataclass(frozen=True)
class ShadowGateDecision:
    """One explicit canary-readiness gate result."""

    name: str
    passed: bool
    blockers: tuple

    def __post_init__(self):
        """Validate one deterministic decision."""
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("gate decision name must be a non-empty string")
        if not isinstance(self.passed, bool):
            raise ValueError("gate decision passed must be a boolean")
        blockers = tuple(self.blockers)
        if any(not isinstance(item, str) or not item for item in blockers):
            raise ValueError("gate blockers must be non-empty strings")
        if self.passed != (not blockers):
            raise ValueError("gate decision must pass exactly when blockers are empty")
        object.__setattr__(self, "blockers", blockers)


@dataclass(frozen=True)
class ShadowCanaryReadiness:
    """Fail-closed aggregate decision for a future canary stage."""

    ready: bool
    blockers: tuple
    decisions: tuple

    def __post_init__(self):
        """Reject contradictory or unordered readiness output."""
        blockers = tuple(self.blockers)
        decisions = tuple(self.decisions)
        if any(not isinstance(item, str) or not item for item in blockers):
            raise ValueError("canary blockers must be non-empty strings")
        if any(not isinstance(item, ShadowGateDecision) for item in decisions):
            raise ValueError("canary decisions must contain ShadowGateDecision values")
        if self.ready != (not blockers):
            raise ValueError("canary readiness must equal absence of blockers")
        flattened = tuple(blocker for decision in decisions for blocker in decision.blockers)
        if blockers != flattened:
            raise ValueError("canary blockers must follow gate decision order")
        object.__setattr__(self, "blockers", blockers)
        object.__setattr__(self, "decisions", decisions)


@dataclass(frozen=True)
class AutoConfigShadowReport:
    """Complete immutable shadow comparison and canary-readiness telemetry."""

    compiler_version: str
    lattice_version: int
    publication_digest: str
    hub_id: str
    status: CompileStatus
    stale: bool
    degraded: bool
    materialization_readiness: MaterializationReadiness
    provider_inputs: tuple
    invalidation_causes: tuple
    recompile: ShadowRecompileTelemetry
    required_capabilities: tuple
    matches: bool
    divergences: tuple
    divergence_count: int
    truncated: bool
    canary: ShadowCanaryReadiness

    def __post_init__(self):
        """Validate telemetry integrity without referring to mutable inputs."""
        if not isinstance(self.compiler_version, str) or not self.compiler_version.strip():
            raise ValueError("compiler_version must be a non-empty string")
        if not isinstance(self.lattice_version, int) or isinstance(self.lattice_version, bool) or self.lattice_version < 1:
            raise ValueError("lattice_version must be a positive integer")
        if not isinstance(self.publication_digest, str) or not self.publication_digest:
            raise ValueError("publication_digest must be a non-empty string")
        if not isinstance(self.hub_id, str) or not self.hub_id:
            raise ValueError("hub_id must be a non-empty string")
        if not isinstance(self.status, CompileStatus):
            raise ValueError("shadow status must be CompileStatus")
        if self.stale != (self.status is CompileStatus.STALE):
            raise ValueError("shadow stale flag must match status")
        if self.degraded != (self.status is CompileStatus.DEGRADED):
            raise ValueError("shadow degraded flag must match status")
        provider_inputs = tuple(self.provider_inputs)
        invalidation_causes = tuple(self.invalidation_causes)
        divergences = tuple(self.divergences)
        if any(not isinstance(item, ShadowProviderInput) for item in provider_inputs):
            raise ValueError("provider_inputs must contain ShadowProviderInput values")
        if any(not isinstance(item, ShadowInvalidationCause) for item in invalidation_causes):
            raise ValueError("invalidation_causes must contain ShadowInvalidationCause values")
        if any(not isinstance(item, ShadowDivergence) for item in divergences):
            raise ValueError("divergences must contain ShadowDivergence values")
        if not isinstance(self.materialization_readiness, MaterializationReadiness):
            raise ValueError("materialization_readiness must be MaterializationReadiness")
        if not isinstance(self.recompile, ShadowRecompileTelemetry):
            raise ValueError("recompile must be ShadowRecompileTelemetry")
        required_capabilities = tuple(self.required_capabilities)
        if required_capabilities != tuple(sorted(set(required_capabilities))):
            raise ValueError("required_capabilities must be unique and sorted")
        if not isinstance(self.divergence_count, int) or self.divergence_count < len(divergences):
            raise ValueError("divergence_count cannot be smaller than retained divergences")
        if self.matches != (self.divergence_count == 0):
            raise ValueError("matches must equal absence of divergences")
        if not self.truncated and self.divergence_count > len(divergences):
            raise ValueError("omitted divergences require truncated=True")
        if not isinstance(self.canary, ShadowCanaryReadiness):
            raise ValueError("canary must be ShadowCanaryReadiness")
        object.__setattr__(self, "compiler_version", self.compiler_version.strip())
        object.__setattr__(self, "provider_inputs", provider_inputs)
        object.__setattr__(self, "invalidation_causes", invalidation_causes)
        object.__setattr__(self, "divergences", divergences)
        object.__setattr__(self, "required_capabilities", required_capabilities)


def _freeze(value):
    """Recursively detach JSON-like telemetry data."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("shadow values cannot contain non-finite numbers")
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ValueError("unsupported shadow value {!r}".format(type(value).__name__))


def _plain(value):
    """Return canonical JSON-compatible data for summaries."""
    if value is _MISSING:
        return "<missing>"
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ValueError("unsupported shadow value {!r}".format(type(value).__name__))


def _routing_document(plan):
    """Project compiler routing decisions into deterministic plain data."""
    routing = {}
    for argument in plan.config_arguments:
        routing[argument.name] = tuple(
            {
                "provider_id": candidate.provider_id,
                "generation": candidate.generation,
                "role": candidate.role,
                "group": candidate.group,
                "routing": candidate.routing,
                "cardinality": candidate.cardinality,
                "slot_indexes": candidate.slot_indexes,
                "target_count": candidate.target_count,
                "capabilities": candidate.capabilities,
                "access_path_ids": candidate.access_path_ids,
            }
            for candidate in argument.candidates
        )
    return routing


def _provenance_document(plan):
    """Group source coordinates by generated field path."""
    grouped = {}
    for source in plan.provenance:
        grouped.setdefault(source.field_path, []).append(
            {
                "provider_id": source.provider_id,
                "generation": source.generation,
                "source_path": source.source_path,
            }
        )
    return {
        field_path: tuple(
            sorted(
                sources,
                key=lambda item: (
                    item["provider_id"],
                    item["generation"],
                    item["source_path"],
                ),
            )
        )
        for field_path, sources in sorted(grouped.items())
    }


def compiled_autoconfig_shadow_document(publication):
    """Return the immutable Lattice side of the shadow comparison."""
    if not isinstance(publication, CompiledLatticePublication):
        raise ValueError("publication must be CompiledLatticePublication")
    plan = publication.plan
    document = {
        "projected_config": plan.projected_config,
        "targets": {
            "primary_target": plan.primary_target,
            "control_target": plan.control_target,
            "primary_targets": tuple(
                {
                    "group": target.group,
                    "index": target.index,
                    "node_id": target.node_id,
                }
                for target in plan.primary_targets
            ),
            "control_targets": tuple(
                {
                    "group": target.group,
                    "index": target.index,
                    "node_id": target.node_id,
                }
                for target in plan.control_targets
            ),
        },
        "routing": _routing_document(plan),
        "provenance": _provenance_document(plan),
    }
    return _freeze(document)


def _legacy_document(observation):
    """Return a comparison document while retaining absent sentinels."""
    return {
        "projected_config": observation.projected_config,
        "targets": {
            "primary_target": observation.primary_target,
            "control_target": observation.control_target,
            "primary_targets": observation.primary_targets,
            "control_targets": observation.control_targets,
        },
        "routing": observation.routing,
        "provenance": observation.provenance,
    }


def _pointer(path, key):
    """Append one escaped JSON-pointer segment."""
    escaped = str(key).replace("~", "~0").replace("/", "~1")
    return "{}/{}".format(path, escaped) if path else "/{}".format(escaped)


def _bounded_text(value, maximum):
    """Keep the start and a digest of overlong deterministic telemetry."""
    if len(value) <= maximum:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    prefix_length = maximum - len(digest) - 2
    return "{}#{}".format(value[:prefix_length], digest)


def _summary(value, limit):
    """Return a bounded canonical scalar summary."""
    if value is _MISSING:
        return "<missing>"
    encoded = json.dumps(
        _plain(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return _bounded_text(encoded, limit)


def _category(value):
    """Classify values using JSON semantics, keeping bool separate from number."""
    if value is _MISSING:
        return "missing"
    if isinstance(value, Mapping):
        return "mapping"
    if isinstance(value, (list, tuple)):
        return "sequence"
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


class _Comparison:
    """Bounded deterministic recursive comparison state."""

    def __init__(self, limits, optional_none_paths):
        self.limits = limits
        self.optional_none_paths = frozenset(optional_none_paths)
        self.divergences = []
        self.divergence_count = 0
        self.nodes = 0
        self.limit_recorded = False

    def _missing_equivalent(self, path, present):
        """Apply narrow, field-aware absent-value equivalence rules."""
        if path in self.optional_none_paths:
            if present is None:
                return True
            if isinstance(present, (list, tuple)) and all(item is None for item in present):
                return True
        if path in (
            "/targets/primary_target",
            "/targets/control_target",
        ):
            return present is None
        if path in (
            "/targets/primary_targets",
            "/targets/control_targets",
        ):
            return isinstance(present, (list, tuple)) and not present
        if path in ("/routing", "/provenance"):
            return isinstance(present, Mapping) and not present
        return False

    def _add(self, path, reason, lattice, legacy):
        """Count every observed divergence and retain only the bounded prefix."""
        self.divergence_count += 1
        if len(self.divergences) >= self.limits.max_divergences:
            return
        bounded_path = _bounded_text(path or "/", self.limits.max_path_length)
        self.divergences.append(
            ShadowDivergence(
                bounded_path,
                reason,
                _summary(lattice, self.limits.max_summary_length),
                _summary(legacy, self.limits.max_summary_length),
            )
        )

    def compare(self, lattice, legacy, path="", depth=0):
        """Compare one subtree without mutating either input."""
        self.nodes += 1
        if self.nodes > self.limits.max_nodes or depth > self.limits.max_depth:
            if not self.limit_recorded:
                self.limit_recorded = True
                self._add(
                    path,
                    ShadowDivergenceReason.COMPARISON_LIMIT,
                    lattice,
                    legacy,
                )
            return

        if lattice is _MISSING or legacy is _MISSING:
            present = legacy if lattice is _MISSING else lattice
            if self._missing_equivalent(path, present):
                return
            reason = ShadowDivergenceReason.MISSING_LATTICE if lattice is _MISSING else ShadowDivergenceReason.MISSING_LEGACY
            self._add(path, reason, lattice, legacy)
            return

        lattice_category = _category(lattice)
        legacy_category = _category(legacy)
        if lattice_category != legacy_category:
            self._add(
                path,
                ShadowDivergenceReason.TYPE_MISMATCH,
                lattice,
                legacy,
            )
            return

        if lattice_category == "mapping":
            keys = sorted(set(lattice) | set(legacy))
            for key in keys:
                self.compare(
                    lattice.get(key, _MISSING),
                    legacy.get(key, _MISSING),
                    _pointer(path, key),
                    depth + 1,
                )
                if self.limit_recorded:
                    break
            return

        if lattice_category == "sequence":
            maximum = max(len(lattice), len(legacy))
            for index in range(maximum):
                self.compare(
                    lattice[index] if index < len(lattice) else _MISSING,
                    legacy[index] if index < len(legacy) else _MISSING,
                    _pointer(path, index),
                    depth + 1,
                )
                if self.limit_recorded:
                    break
            return

        if lattice != legacy:
            self._add(
                path,
                ShadowDivergenceReason.VALUE_MISMATCH,
                lattice,
                legacy,
            )


def _cause_telemetry(causes):
    """Normalize compiler invalidation causes for external telemetry."""
    normalized = {
        ShadowInvalidationCause(
            item.source_type,
            item.source_id,
            item.generation,
            item.reason,
        )
        for item in causes
    }
    return tuple(
        sorted(
            normalized,
            key=lambda item: (
                item.source_type,
                item.source_id,
                item.generation,
                item.reason,
            ),
        )
    )


def _provider_inputs(publication, previous_publication):
    """Build current cursors and explicit generation/fingerprint deltas."""
    current_fingerprints = {provider_id: (generation, fingerprint) for provider_id, generation, fingerprint in publication.provider_fingerprints}
    requested = dict(publication.provider_requested_generations)
    previous = {}
    if previous_publication is not None:
        previous = {provider_id: (generation, fingerprint) for provider_id, generation, fingerprint in previous_publication.provider_fingerprints}
    inputs = []
    for provider_id in sorted(set(current_fingerprints) | set(requested)):
        current_cursor = current_fingerprints.get(provider_id)
        generation = current_cursor[0] if current_cursor is not None else None
        fingerprint = current_cursor[1] if current_cursor is not None else None
        requested_generation = requested[provider_id]
        previous_cursor = previous.get(provider_id)
        inputs.append(
            ShadowProviderInput(
                provider_id=provider_id,
                generation=generation,
                requested_generation=requested_generation,
                fingerprint=fingerprint,
                observation_pending=(generation is None or requested_generation > generation),
                previous_generation=(previous_cursor[0] if previous_cursor is not None else None),
                previous_fingerprint=(previous_cursor[1] if previous_cursor is not None else None),
                generation_changed=(generation != previous_cursor[0] if previous_cursor is not None else None),
                fingerprint_changed=(fingerprint != previous_cursor[1] if previous_cursor is not None else None),
            )
        )
    return tuple(inputs)


def _required_capabilities(plan):
    """Return capabilities used by effective provider projection candidates."""
    capabilities = set()
    for argument in plan.config_arguments:
        for candidate in argument.candidates:
            capabilities.update(capability for capability in candidate.capabilities if capability is not None)
    return tuple(sorted(capabilities))


def _decision(name, blockers):
    """Build one normalized decision from deterministic blockers."""
    blockers = tuple(blockers)
    return ShadowGateDecision(name, not blockers, blockers)


def _canary_readiness(
    publication,
    diagnostics,
    observation,
    gates,
    matches,
    required_capabilities,
    recompile,
):
    """Evaluate all explicit gates without invoking either configuration path."""
    providers = tuple(provider_id for provider_id, _generation in publication.provider_generations)
    provider_denied = tuple(provider_id for provider_id in providers if provider_id not in gates.provider_ids)
    capability_denied = tuple(capability for capability in required_capabilities if capability not in gates.capability_ids)
    capability_unavailable = tuple(capability for capability in required_capabilities if capability not in observation.capabilities)
    readiness_blockers = ()
    if gates.require_materialization_ready and not publication.plan.materialization_readiness.ready:
        readiness_blockers = tuple("materialization:{}".format(blocker) for blocker in publication.plan.materialization_readiness.blockers)
    observed_generations = dict(publication.provider_generations)
    unsettled_blockers = tuple(
        "provider_generation_unobserved:{}:{}".format(
            provider_id,
            requested_generation,
        )
        for provider_id, requested_generation in publication.provider_requested_generations
        if observed_generations.get(provider_id, -1) < requested_generation
    )
    if publication.user_override_requested_generation is not None and (publication.user_override_generation is None or publication.user_override_generation < publication.user_override_requested_generation):
        unsettled_blockers += (
            "user_override_generation_unobserved:{}".format(
                publication.user_override_requested_generation,
            ),
        )
    if recompile.follow_up_pending:
        unsettled_blockers += ("compiler_follow_up_pending",)

    decisions = (
        _decision(
            "shadow_enabled",
            () if gates.enabled else ("shadow_disabled",),
        ),
        _decision(
            "hub_allowed",
            () if observation.hub_id in gates.hub_ids else ("hub_not_allowed:{}".format(observation.hub_id),),
        ),
        _decision(
            "providers_allowed",
            tuple("provider_not_allowed:{}".format(provider_id) for provider_id in provider_denied),
        ),
        _decision(
            "capabilities_allowed",
            tuple("capability_not_allowed:{}".format(capability) for capability in capability_denied),
        ),
        _decision(
            "capabilities_available",
            tuple("capability_unavailable:{}".format(capability) for capability in capability_unavailable),
        ),
        _decision(
            "shadow_match",
            () if matches else ("shadow_divergence",),
        ),
        _decision(
            "compiler_fresh",
            () if not diagnostics.stale or gates.allow_stale else ("compiler_stale",),
        ),
        _decision(
            "compiler_healthy",
            () if not diagnostics.degraded or gates.allow_degraded else ("compiler_degraded",),
        ),
        _decision(
            "compiler_settled",
            unsettled_blockers,
        ),
        _decision(
            "materialization_ready",
            readiness_blockers,
        ),
    )
    blockers = tuple(blocker for decision in decisions for blocker in decision.blockers)
    return ShadowCanaryReadiness(not blockers, blockers, decisions)


def compare_autoconfig_shadow(
    publication,
    legacy_observation,
    gates=None,
    diagnostics=None,
    run=None,
    previous_publication=None,
    compiler_version=DEFAULT_COMPILER_VERSION,
    limits=None,
):
    """Compare both paths and return read-only telemetry plus closed gates."""
    if not isinstance(publication, CompiledLatticePublication):
        raise ValueError("publication must be CompiledLatticePublication")
    if not isinstance(legacy_observation, LegacyAutoConfigObservation):
        raise ValueError("legacy_observation must be LegacyAutoConfigObservation")
    if gates is None:
        gates = ShadowCanaryGates()
    if not isinstance(gates, ShadowCanaryGates):
        raise ValueError("gates must be ShadowCanaryGates")
    if run is not None:
        if not isinstance(run, CompiledLatticeRun):
            raise ValueError("run must be CompiledLatticeRun or None")
        if run.publication != publication:
            raise ValueError("run publication must match publication")
    if diagnostics is None:
        diagnostics = run.diagnostics if run is not None else publication.diagnostics
    if not isinstance(diagnostics, CompiledLatticeDiagnostics):
        raise ValueError("diagnostics must be CompiledLatticeDiagnostics")
    if previous_publication is not None:
        if not isinstance(previous_publication, CompiledLatticePublication):
            raise ValueError("previous_publication must be CompiledLatticePublication or None")
        if previous_publication.lattice_version >= publication.lattice_version:
            raise ValueError("previous_publication must have an earlier version")
    if not isinstance(compiler_version, str) or not compiler_version.strip():
        raise ValueError("compiler_version must be a non-empty string")
    if limits is None:
        limits = ShadowComparisonLimits()
    if not isinstance(limits, ShadowComparisonLimits):
        raise ValueError("limits must be ShadowComparisonLimits")

    optional_none_paths = {"/projected_config/{}".format(argument.name.replace("~", "~0").replace("/", "~1")) for argument in publication.plan.config_arguments if not argument.required}
    comparison = _Comparison(limits, optional_none_paths)
    comparison.compare(
        compiled_autoconfig_shadow_document(publication),
        _legacy_document(legacy_observation),
    )

    publication_causes = _cause_telemetry(publication.invalidation_causes)
    run_causes = _cause_telemetry(run.causes) if run is not None else publication_causes
    attempts = run.attempts if run is not None else 0
    pending = run.pending if run is not None else False
    recompile = ShadowRecompileTelemetry(
        triggered=bool(run_causes),
        attempts=attempts,
        coalesced=len(run_causes) > 1,
        bounded_follow_up_used=attempts > 1,
        follow_up_pending=pending,
        causes=run_causes,
    )
    required_capabilities = _required_capabilities(publication.plan)
    matches = comparison.divergence_count == 0
    canary = _canary_readiness(
        publication,
        diagnostics,
        legacy_observation,
        gates,
        matches,
        required_capabilities,
        recompile,
    )
    return AutoConfigShadowReport(
        compiler_version=compiler_version,
        lattice_version=publication.lattice_version,
        publication_digest=publication.digest,
        hub_id=legacy_observation.hub_id,
        status=diagnostics.status,
        stale=diagnostics.stale,
        degraded=diagnostics.degraded,
        materialization_readiness=publication.plan.materialization_readiness,
        provider_inputs=_provider_inputs(publication, previous_publication),
        invalidation_causes=publication_causes,
        recompile=recompile,
        required_capabilities=required_capabilities,
        matches=matches,
        divergences=tuple(comparison.divergences),
        divergence_count=comparison.divergence_count,
        truncated=(comparison.limit_recorded or comparison.divergence_count > len(comparison.divergences)),
        canary=canary,
    )
