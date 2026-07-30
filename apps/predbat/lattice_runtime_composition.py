# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice shadow runtime composition
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Detached, default-off composition for the durable Lattice shadow runtime.

This module joins the already-pure fragment, compiler, persistence, target and
shadow-comparison seams.  It is deliberately absent from ``COMPONENT_LIST``.
Even when explicitly enabled it keeps the atomic materializer disabled, so a
runtime reconciliation can publish compiler state and diagnostics but cannot
write the generated overlay or any live PredBat configuration.
"""

# cspell:ignore autoconfig codepoint fspath noncharacter

import hashlib
import os
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from lattice_atomic_materializer import (
    AtomicConfigMaterializer,
    MaterializationStatus,
)
from lattice_autoconfig import CompileStatus
from lattice_autoconfig_shadow import (
    LegacyAutoConfigObservation,
    ShadowCanaryGates,
    ShadowComparisonLimits,
    compare_autoconfig_shadow,
)
from lattice_compiled_publication import CompiledLatticePublication
from lattice_durable_runtime_stores import (
    FileAtomicMaterializationStateStore,
    FileCompiledLatticeStateStore,
    FileFragmentAdapterStateStore,
    FileOpaqueTargetJournal,
)
from lattice_fragment_adapters import FragmentAdapterRegistry
from lattice_whole_config_target import GeneratedOverlayWholeConfigTarget


MAX_FRAGMENT_SOURCES = 128
MAX_OBSERVATION_ISSUES = 32
MAX_OBSERVATION_CAUSES = 32
MAX_OBSERVATION_DIVERGENCES = 32
MAX_OBSERVATION_BLOCKERS = 32
MAX_OBSERVATION_CAPABILITIES = 64
MAX_OBSERVATION_CODE_BYTES = 64
MAX_OBSERVATION_SOURCE_BYTES = 128
MAX_OBSERVATION_DETAIL_BYTES = 256
MAX_OBSERVATION_PATH_BYTES = 256
MAX_OBSERVATION_SUMMARY_BYTES = 160
MAX_OBSERVATION_TOTAL_TEXT_BYTES = 96 * 1024


class LatticeRuntimePhase(Enum):
    """Observable lifecycle phase for the detached composition."""

    DISABLED = "disabled"
    SHADOW_READY = "shadow_ready"


@dataclass(frozen=True)
class LatticeRuntimeConfig:
    """Explicit gate and paths for one process-local shadow composition."""

    enabled: bool = False
    state_directory: Optional[object] = None
    overlay_path: Optional[object] = None
    allowed_keys: tuple = ()
    canary_gates: ShadowCanaryGates = ShadowCanaryGates()
    comparison_limits: ShadowComparisonLimits = ShadowComparisonLimits()

    def __post_init__(self):
        """Validate the gate without touching disabled-path caller objects."""
        if type(self.enabled) is not bool:
            raise ValueError("runtime enabled gate must be a boolean")
        if not isinstance(self.canary_gates, ShadowCanaryGates):
            raise ValueError("canary_gates must be ShadowCanaryGates")
        if not isinstance(self.comparison_limits, ShadowComparisonLimits):
            raise ValueError(
                "comparison_limits must be ShadowComparisonLimits",
            )


@dataclass(frozen=True)
class RuntimeFragmentSource:
    """Brand-neutral factory for one existing integration fragment source."""

    provider_id: str
    factory: Callable

    def __post_init__(self):
        """Validate one deterministic source declaration."""
        if (
            not _is_bounded_text(
                self.provider_id,
                MAX_OBSERVATION_SOURCE_BYTES,
            )
            or self.provider_id != self.provider_id.strip()
        ):
            raise ValueError("fragment source provider_id is invalid")
        if not callable(self.factory):
            raise ValueError("fragment source factory must be callable")


@dataclass(frozen=True)
class RuntimeIssueObservation:
    """One bounded compiler issue without an unbounded exception payload."""

    code: str
    detail: str
    provider_id: Optional[str]
    text_sanitized: bool
    text_truncated: bool

    def __post_init__(self):
        """Validate one already-sanitized issue."""
        _require_bounded_text(
            self.code,
            "runtime issue code",
            MAX_OBSERVATION_CODE_BYTES,
        )
        _require_bounded_text(
            self.detail,
            "runtime issue detail",
            MAX_OBSERVATION_DETAIL_BYTES,
        )
        if self.provider_id is not None:
            _require_bounded_text(
                self.provider_id,
                "runtime issue provider_id",
                MAX_OBSERVATION_SOURCE_BYTES,
            )
        for name in ("text_sanitized", "text_truncated"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(
                    "runtime issue {} flag is invalid".format(name),
                )


@dataclass(frozen=True)
class RuntimeInvalidationObservation:
    """One bounded invalidation identity with explicit reason truncation."""

    source_type: str
    source_id: str
    generation: int
    reason: str
    text_sanitized: bool
    text_truncated: bool

    def __post_init__(self):
        """Validate one already-sanitized invalidation."""
        _require_bounded_text(
            self.source_type,
            "runtime cause source_type",
            MAX_OBSERVATION_CODE_BYTES,
        )
        _require_bounded_text(
            self.source_id,
            "runtime cause source_id",
            MAX_OBSERVATION_SOURCE_BYTES,
        )
        if type(self.generation) is not int or self.generation < 0:
            raise ValueError("runtime cause generation is invalid")
        _require_bounded_text(
            self.reason,
            "runtime cause reason",
            MAX_OBSERVATION_DETAIL_BYTES,
        )
        for name in ("text_sanitized", "text_truncated"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(
                    "runtime cause {} flag is invalid".format(name),
                )


@dataclass(frozen=True)
class RuntimeDivergenceObservation:
    """One bounded shadow divergence with no raw comparison object."""

    path: str
    reason: str
    lattice_summary: str
    legacy_summary: str
    text_sanitized: bool
    text_truncated: bool

    def __post_init__(self):
        """Validate one already-sanitized shadow divergence."""
        _require_bounded_text(
            self.path,
            "runtime divergence path",
            MAX_OBSERVATION_PATH_BYTES,
        )
        _require_bounded_text(
            self.reason,
            "runtime divergence reason",
            MAX_OBSERVATION_CODE_BYTES,
        )
        for name in ("lattice_summary", "legacy_summary"):
            _require_bounded_text(
                getattr(self, name),
                "runtime divergence {}".format(name),
                MAX_OBSERVATION_SUMMARY_BYTES,
                allow_empty=True,
            )
        for name in ("text_sanitized", "text_truncated"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(
                    "runtime divergence {} flag is invalid".format(name),
                )


@dataclass(frozen=True)
class RuntimeShadowObservation:
    """Bounded projection of the pure shadow-comparison report."""

    hub_id: str
    matches: bool
    provider_input_total: int
    publication_cause_total: int
    divergence_total: int
    divergences: tuple
    divergences_truncated: bool
    comparison_truncated: bool
    canary_ready: bool
    canary_blocker_total: int
    canary_blockers: tuple
    canary_blockers_truncated: bool
    required_capability_total: int
    required_capabilities: tuple
    required_capabilities_truncated: bool
    text_sanitized: bool
    text_truncated: bool

    def __post_init__(self):
        """Validate cardinalities and every bounded retained string."""
        _require_bounded_text(
            self.hub_id,
            "runtime shadow hub_id",
            MAX_OBSERVATION_SOURCE_BYTES,
        )
        for name in (
            "matches",
            "divergences_truncated",
            "comparison_truncated",
            "canary_ready",
            "canary_blockers_truncated",
            "required_capabilities_truncated",
            "text_sanitized",
            "text_truncated",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(
                    "runtime shadow {} is invalid".format(name),
                )
        for name in (
            "provider_input_total",
            "publication_cause_total",
            "divergence_total",
            "canary_blocker_total",
            "required_capability_total",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(
                    "runtime shadow {} is invalid".format(name),
                )
        if type(self.divergences) is not tuple or any(type(item) is not RuntimeDivergenceObservation for item in self.divergences):
            raise ValueError("runtime shadow divergences are invalid")
        if len(self.divergences) > MAX_OBSERVATION_DIVERGENCES or self.divergence_total < len(self.divergences):
            raise ValueError("runtime shadow divergence cardinality is invalid")
        if not self.divergences_truncated and self.divergence_total > len(self.divergences):
            raise ValueError("omitted runtime divergences require truncation")
        for values, total, truncated, maximum, name, byte_limit in (
            (
                self.canary_blockers,
                self.canary_blocker_total,
                self.canary_blockers_truncated,
                MAX_OBSERVATION_BLOCKERS,
                "canary blockers",
                MAX_OBSERVATION_DETAIL_BYTES,
            ),
            (
                self.required_capabilities,
                self.required_capability_total,
                self.required_capabilities_truncated,
                MAX_OBSERVATION_CAPABILITIES,
                "required capabilities",
                MAX_OBSERVATION_SOURCE_BYTES,
            ),
        ):
            if type(values) is not tuple or len(values) > maximum or total < len(values):
                raise ValueError(
                    "runtime shadow {} cardinality is invalid".format(name),
                )
            if any(not _is_bounded_text(item, byte_limit) for item in values):
                raise ValueError(
                    "runtime shadow {} are invalid".format(name),
                )
            if truncated != (total > len(values)):
                raise ValueError(
                    "runtime shadow {} truncation is invalid".format(name),
                )


@dataclass(frozen=True)
class LatticeRuntimeObservation:
    """Immutable bounded result of one disabled or shadow reconciliation."""

    phase: LatticeRuntimePhase
    provider_ids: tuple
    attempts: int
    pending: bool
    status: Optional[CompileStatus]
    publication_version: Optional[int]
    publication_digest: Optional[str]
    published: bool
    compiler_materializations: int
    issues: tuple
    issue_total: int
    issues_truncated: bool
    invalidation_causes: tuple
    invalidation_cause_total: int
    invalidation_causes_truncated: bool
    telemetry_text_sanitized: bool
    telemetry_text_truncated: bool
    materialization_status: MaterializationStatus
    shadow: Optional[RuntimeShadowObservation] = None

    @property
    def telemetry_text_bytes(self):
        """Return the exact retained public string footprint."""
        return _observation_text_bytes(self)

    def __post_init__(self):
        """Reject contradictory or mutable public observations."""
        if not isinstance(self.phase, LatticeRuntimePhase):
            raise ValueError("runtime observation phase is invalid")
        if type(self.provider_ids) is not tuple or self.provider_ids != tuple(sorted(set(self.provider_ids))):
            raise ValueError("runtime observation providers are invalid")
        if len(self.provider_ids) > MAX_FRAGMENT_SOURCES or any(
            not _is_bounded_text(
                provider_id,
                MAX_OBSERVATION_SOURCE_BYTES,
            )
            for provider_id in self.provider_ids
        ):
            raise ValueError("runtime observation providers are invalid")
        if type(self.attempts) is not int or self.attempts < 0:
            raise ValueError("runtime observation attempts are invalid")
        for name in ("pending", "published"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(
                    "runtime observation {} is invalid".format(name),
                )
        if self.status is not None and not isinstance(self.status, CompileStatus):
            raise ValueError("runtime observation status is invalid")
        if self.publication_version is not None and (type(self.publication_version) is not int or self.publication_version < 1):
            raise ValueError("runtime publication version is invalid")
        if self.publication_digest is not None and not _is_bounded_text(
            self.publication_digest,
            MAX_OBSERVATION_CODE_BYTES,
        ):
            raise ValueError("runtime publication digest is invalid")
        if (self.publication_version is None) != (self.publication_digest is None):
            raise ValueError("runtime publication identity is incomplete")
        if type(self.compiler_materializations) is not int or self.compiler_materializations < 0:
            raise ValueError("runtime materialization count is invalid")
        if type(self.issues) is not tuple or any(type(issue) is not RuntimeIssueObservation for issue in self.issues):
            raise ValueError("runtime issues are invalid")
        if len(self.issues) > MAX_OBSERVATION_ISSUES or type(self.issue_total) is not int or self.issue_total < len(self.issues):
            raise ValueError("runtime issue cardinality is invalid")
        if type(self.issues_truncated) is not bool or self.issues_truncated != (self.issue_total > len(self.issues)):
            raise ValueError("runtime issue truncation is invalid")
        if type(self.invalidation_causes) is not tuple or any(type(cause) is not RuntimeInvalidationObservation for cause in self.invalidation_causes):
            raise ValueError("runtime invalidation causes are invalid")
        if len(self.invalidation_causes) > MAX_OBSERVATION_CAUSES or type(self.invalidation_cause_total) is not int or self.invalidation_cause_total < len(self.invalidation_causes):
            raise ValueError("runtime cause cardinality is invalid")
        if type(self.invalidation_causes_truncated) is not bool or self.invalidation_causes_truncated != (self.invalidation_cause_total > len(self.invalidation_causes)):
            raise ValueError("runtime cause truncation is invalid")
        for name in (
            "telemetry_text_sanitized",
            "telemetry_text_truncated",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(
                    "runtime {} flag is invalid".format(name),
                )
        if not isinstance(
            self.materialization_status,
            MaterializationStatus,
        ):
            raise ValueError("runtime materialization status is invalid")
        if self.shadow is not None and type(self.shadow) is not RuntimeShadowObservation:
            raise ValueError("runtime shadow observation is invalid")
        expected_text_sanitized = any(item.text_sanitized for item in self.issues) or any(item.text_sanitized for item in self.invalidation_causes) or (self.shadow is not None and self.shadow.text_sanitized)
        if self.telemetry_text_sanitized != expected_text_sanitized:
            raise ValueError("runtime text sanitization truth is invalid")
        expected_text_truncated = any(item.text_truncated for item in self.issues) or any(item.text_truncated for item in self.invalidation_causes) or (self.shadow is not None and self.shadow.text_truncated)
        if self.telemetry_text_truncated != expected_text_truncated:
            raise ValueError("runtime text truncation truth is invalid")
        if self.telemetry_text_bytes > MAX_OBSERVATION_TOTAL_TEXT_BYTES:
            raise ValueError("runtime observation exceeds its text budget")
        if self.phase is LatticeRuntimePhase.DISABLED:
            if (
                self.provider_ids
                or self.attempts
                or self.pending
                or self.status is not None
                or self.publication_version is not None
                or self.published
                or self.compiler_materializations
                or self.issues
                or self.issue_total
                or self.issues_truncated
                or self.invalidation_causes
                or self.invalidation_cause_total
                or self.invalidation_causes_truncated
                or self.telemetry_text_sanitized
                or self.telemetry_text_truncated
                or self.shadow is not None
            ):
                raise ValueError(
                    "disabled runtime observation must have zero effect",
                )
        if self.materialization_status is not MaterializationStatus.DISABLED:
            raise ValueError(
                "shadow runtime must keep materialization disabled",
            )


class LatticeShadowRuntime:
    """Detached owner of one immutable, process-lifetime shadow composition."""

    def __init__(
        self,
        config,
        phase,
        registry=None,
        compiler=None,
        materializer=None,
    ):
        """Create one internally composed disabled or shadow runtime."""
        self._config = config
        self._phase = phase
        self._registry = registry
        self._compiler = compiler
        self._materializer = materializer
        self._previous_publication = compiler.publication if compiler is not None else None

    @property
    def enabled(self):
        """Return whether the explicit shadow feature gate was enabled."""
        return self._phase is LatticeRuntimePhase.SHADOW_READY

    @property
    def phase(self):
        """Return the immutable lifecycle phase."""
        return self._phase

    @property
    def provider_ids(self):
        """Return the sealed provider membership in deterministic order."""
        if self._registry is None:
            return ()
        return self._registry.provider_ids

    @property
    def materializer_enabled(self):
        """Expose the invariant that target mutation remains hard-disabled."""
        return bool(
            self._materializer is not None and self._materializer.enabled,
        )

    def _disabled_observation(self):
        """Return a deterministic observation without touching any seam."""
        return LatticeRuntimeObservation(
            phase=LatticeRuntimePhase.DISABLED,
            provider_ids=(),
            attempts=0,
            pending=False,
            status=None,
            publication_version=None,
            publication_digest=None,
            published=False,
            compiler_materializations=0,
            issues=(),
            issue_total=0,
            issues_truncated=False,
            invalidation_causes=(),
            invalidation_cause_total=0,
            invalidation_causes_truncated=False,
            telemetry_text_sanitized=False,
            telemetry_text_truncated=False,
            materialization_status=MaterializationStatus.DISABLED,
        )

    def drain(self):
        """Drain pending compiler work without observing or mutating a target."""
        return self.reconcile()

    def reconcile(self, legacy_observation=None):
        """Drain and optionally compare one detached legacy observation."""
        if not self.enabled:
            return self._disabled_observation()
        if legacy_observation is not None and not isinstance(
            legacy_observation,
            LegacyAutoConfigObservation,
        ):
            raise ValueError(
                "legacy_observation must be LegacyAutoConfigObservation",
            )

        previous = self._previous_publication
        run = self._compiler.drain()
        publication = run.publication
        materialization = self._materializer.recover()
        if materialization.status is not MaterializationStatus.DISABLED or materialization.state is not None or materialization.target_written:
            raise RuntimeError(
                "shadow runtime materializer crossed its disabled boundary",
            )
        if run.materializations:
            raise RuntimeError(
                "shadow compiler unexpectedly invoked a materializer",
            )

        shadow = None
        if legacy_observation is not None and publication is not None:
            comparison_previous = previous
            if comparison_previous is not None and comparison_previous.lattice_version >= publication.lattice_version:
                comparison_previous = None
            shadow = _bounded_shadow_observation(
                compare_autoconfig_shadow(
                    publication,
                    legacy_observation,
                    gates=self._config.canary_gates,
                    run=run,
                    previous_publication=comparison_previous,
                    limits=self._config.comparison_limits,
                )
            )

        if publication is not None:
            self._previous_publication = publication
        return _runtime_observation(
            self._phase,
            self.provider_ids,
            run,
            publication,
            materialization.status,
            shadow,
        )


def _is_unicode_noncharacter(codepoint):
    """Return whether one scalar is permanently reserved as a noncharacter."""
    return 0xFDD0 <= codepoint <= 0xFDEF or codepoint & 0xFFFF in (0xFFFE, 0xFFFF)


def _escaped_codepoint(codepoint):
    """Return one deterministic ASCII escape for an unsafe codepoint."""
    if codepoint <= 0xFF:
        return "\\x{:02x}".format(codepoint)
    if codepoint <= 0xFFFF:
        return "\\u{:04x}".format(codepoint)
    return "\\U{:08x}".format(codepoint)


def _canonical_safe_text(value, allow_empty=False):
    """Return deterministic printable scalar text plus sanitization truth."""
    sanitized = False
    if type(value) is not str or (not allow_empty and not value):
        value = "<invalid-text>"
        sanitized = True
    safe = []
    for character in value:
        codepoint = ord(character)
        if _is_unicode_noncharacter(codepoint) or not character.isprintable():
            safe.append(_escaped_codepoint(codepoint))
            sanitized = True
        else:
            safe.append(character)
    return "".join(safe), sanitized


def _safe_utf8_bytes(value):
    """Encode canonical safe text through the only telemetry byte seam."""
    return value.encode("utf-8", errors="strict")


def _is_bounded_text(value, maximum, allow_empty=False):
    """Return whether exact text is safe and fits an explicit UTF-8 bound."""
    safe, sanitized = _canonical_safe_text(
        value,
        allow_empty=allow_empty,
    )
    return not sanitized and safe == value and len(_safe_utf8_bytes(safe)) <= maximum


def _require_bounded_text(
    value,
    name,
    maximum,
    allow_empty=False,
):
    """Require one exact string within an explicit UTF-8 byte bound."""
    if not _is_bounded_text(value, maximum, allow_empty=allow_empty):
        raise ValueError("{} is invalid".format(name))
    return value


def _bounded_text(value, maximum, allow_empty=False):
    """Return safe text plus sanitization and truncation truth."""
    value, sanitized = _canonical_safe_text(
        value,
        allow_empty=allow_empty,
    )
    encoded = _safe_utf8_bytes(value)
    if len(encoded) <= maximum:
        return value, sanitized, False
    digest = hashlib.sha256(encoded).hexdigest()[:16]
    suffix = "...[sha256:{}]".format(digest)
    suffix_bytes = suffix.encode("ascii")
    prefix_bytes = encoded[: max(0, maximum - len(suffix_bytes))]
    prefix = prefix_bytes.decode("utf-8", errors="ignore")
    bounded = prefix + suffix
    while len(_safe_utf8_bytes(bounded)) > maximum and prefix:
        prefix = prefix[:-1]
        bounded = prefix + suffix
    return bounded, sanitized, True


def _bounded_issue(issue):
    """Project one compiler issue into bounded public telemetry."""
    code, code_sanitized, code_truncated = _bounded_text(
        getattr(issue, "code", None),
        MAX_OBSERVATION_CODE_BYTES,
    )
    detail, detail_sanitized, detail_truncated = _bounded_text(
        getattr(issue, "detail", None),
        MAX_OBSERVATION_DETAIL_BYTES,
    )
    provider_id = getattr(issue, "provider_id", None)
    provider_sanitized = False
    provider_truncated = False
    if provider_id is not None:
        provider_id, provider_sanitized, provider_truncated = _bounded_text(
            provider_id,
            MAX_OBSERVATION_SOURCE_BYTES,
        )
    return RuntimeIssueObservation(
        code,
        detail,
        provider_id,
        code_sanitized or detail_sanitized or provider_sanitized,
        code_truncated or detail_truncated or provider_truncated,
    )


def _bounded_cause(cause):
    """Project one invalidation cause into bounded public telemetry."""
    source_type, type_sanitized, type_truncated = _bounded_text(
        getattr(cause, "source_type", None),
        MAX_OBSERVATION_CODE_BYTES,
    )
    source_id, source_sanitized, source_truncated = _bounded_text(
        getattr(cause, "source_id", None),
        MAX_OBSERVATION_SOURCE_BYTES,
    )
    generation = getattr(cause, "generation", -1)
    if type(generation) is not int or generation < 0:
        generation = 0
    reason, reason_sanitized, reason_truncated = _bounded_text(
        getattr(cause, "reason", None),
        MAX_OBSERVATION_DETAIL_BYTES,
    )
    return RuntimeInvalidationObservation(
        source_type,
        source_id,
        generation,
        reason,
        type_sanitized or source_sanitized or reason_sanitized,
        type_truncated or source_truncated or reason_truncated,
    )


def _bounded_divergence(divergence):
    """Project one shadow divergence into bounded public telemetry."""
    values = []
    sanitized = False
    truncated = False
    for value, maximum, allow_empty in (
        (getattr(divergence, "path", None), MAX_OBSERVATION_PATH_BYTES, False),
        (
            getattr(getattr(divergence, "reason", None), "value", None),
            MAX_OBSERVATION_CODE_BYTES,
            False,
        ),
        (
            getattr(divergence, "lattice_summary", None),
            MAX_OBSERVATION_SUMMARY_BYTES,
            True,
        ),
        (
            getattr(divergence, "legacy_summary", None),
            MAX_OBSERVATION_SUMMARY_BYTES,
            True,
        ),
    ):
        bounded, was_sanitized, was_truncated = _bounded_text(
            value,
            maximum,
            allow_empty=allow_empty,
        )
        values.append(bounded)
        sanitized = sanitized or was_sanitized
        truncated = truncated or was_truncated
    return RuntimeDivergenceObservation(
        values[0],
        values[1],
        values[2],
        values[3],
        sanitized,
        truncated,
    )


def _bounded_string_tuple(values, maximum_count, maximum_bytes):
    """Bound one deterministic string tuple and retain exact cardinality."""
    retained = []
    text_sanitized = False
    text_truncated = False
    for value in tuple(values)[:maximum_count]:
        bounded, was_sanitized, was_truncated = _bounded_text(
            value,
            maximum_bytes,
        )
        retained.append(bounded)
        text_sanitized = text_sanitized or was_sanitized
        text_truncated = text_truncated or was_truncated
    return tuple(retained), text_sanitized, text_truncated


def _bounded_shadow_observation(report):
    """Remove the raw shadow report while preserving bounded diagnostic truth."""
    hub_id, hub_sanitized, hub_truncated = _bounded_text(
        report.hub_id,
        MAX_OBSERVATION_SOURCE_BYTES,
    )
    divergences = tuple(_bounded_divergence(item) for item in report.divergences[:MAX_OBSERVATION_DIVERGENCES])
    blockers, blockers_text_sanitized, blockers_text_truncated = _bounded_string_tuple(
        report.canary.blockers,
        MAX_OBSERVATION_BLOCKERS,
        MAX_OBSERVATION_DETAIL_BYTES,
    )
    capabilities, capabilities_text_sanitized, capabilities_text_truncated = _bounded_string_tuple(
        report.required_capabilities,
        MAX_OBSERVATION_CAPABILITIES,
        MAX_OBSERVATION_SOURCE_BYTES,
    )
    return RuntimeShadowObservation(
        hub_id=hub_id,
        matches=report.matches,
        provider_input_total=len(report.provider_inputs),
        publication_cause_total=len(report.invalidation_causes),
        divergence_total=report.divergence_count,
        divergences=divergences,
        divergences_truncated=(report.truncated or report.divergence_count > len(divergences)),
        comparison_truncated=report.truncated,
        canary_ready=report.canary.ready,
        canary_blocker_total=len(report.canary.blockers),
        canary_blockers=blockers,
        canary_blockers_truncated=(len(report.canary.blockers) > len(blockers)),
        required_capability_total=len(report.required_capabilities),
        required_capabilities=capabilities,
        required_capabilities_truncated=(len(report.required_capabilities) > len(capabilities)),
        text_sanitized=(hub_sanitized or any(item.text_sanitized for item in divergences) or blockers_text_sanitized or capabilities_text_sanitized),
        text_truncated=(hub_truncated or any(item.text_truncated for item in divergences) or blockers_text_truncated or capabilities_text_truncated),
    )


def _observation_text_bytes(observation):
    """Count every caller-visible string byte in one observation."""
    values = list(observation.provider_ids)
    if observation.publication_digest is not None:
        values.append(observation.publication_digest)
    for issue in observation.issues:
        values.extend((issue.code, issue.detail))
        if issue.provider_id is not None:
            values.append(issue.provider_id)
    for cause in observation.invalidation_causes:
        values.extend(
            (cause.source_type, cause.source_id, cause.reason),
        )
    if observation.shadow is not None:
        values.append(observation.shadow.hub_id)
        values.extend(observation.shadow.canary_blockers)
        values.extend(observation.shadow.required_capabilities)
        for divergence in observation.shadow.divergences:
            values.extend(
                (
                    divergence.path,
                    divergence.reason,
                    divergence.lattice_summary,
                    divergence.legacy_summary,
                )
            )
    return sum(len(_safe_utf8_bytes(value)) for value in values)


def _runtime_observation(
    phase,
    provider_ids,
    run,
    publication,
    materialization_status,
    shadow,
):
    """Build one value-bounded public runtime observation."""
    if publication is not None and not isinstance(
        publication,
        CompiledLatticePublication,
    ):
        raise RuntimeError("compiler returned an invalid publication")
    issues = tuple(_bounded_issue(issue) for issue in run.issues[:MAX_OBSERVATION_ISSUES])
    causes = tuple(_bounded_cause(cause) for cause in run.causes[:MAX_OBSERVATION_CAUSES])
    return LatticeRuntimeObservation(
        phase=phase,
        provider_ids=provider_ids,
        attempts=run.attempts,
        pending=run.pending,
        status=run.status,
        publication_version=(publication.lattice_version if publication is not None else None),
        publication_digest=(publication.digest if publication is not None else None),
        published=run.published,
        compiler_materializations=run.materializations,
        issues=issues,
        issue_total=len(run.issues),
        issues_truncated=len(run.issues) > len(issues),
        invalidation_causes=causes,
        invalidation_cause_total=len(run.causes),
        invalidation_causes_truncated=len(run.causes) > len(causes),
        telemetry_text_sanitized=(any(item.text_sanitized for item in issues) or any(item.text_sanitized for item in causes) or (shadow is not None and shadow.text_sanitized)),
        telemetry_text_truncated=(any(item.text_truncated for item in issues) or any(item.text_truncated for item in causes) or (shadow is not None and shadow.text_truncated)),
        materialization_status=materialization_status,
        shadow=shadow,
    )


def _enabled_paths(config):
    """Validate and detach enabled-only filesystem configuration."""
    state_directory = _canonical_absolute_path(
        config.state_directory,
        "state_directory",
    )
    overlay_path = _canonical_absolute_path(
        config.overlay_path,
        "overlay_path",
    )
    if _paths_overlap(state_directory, overlay_path):
        raise ValueError(
            "runtime state_directory and overlay_path cannot overlap",
        )
    return state_directory, overlay_path


def _canonical_absolute_path(value, name):
    """Require one caller-supplied, symlink-resolved absolute path."""
    try:
        path = os.fspath(value)
    except TypeError as exc:
        raise ValueError(
            "enabled shadow runtime requires explicit filesystem paths",
        ) from exc
    if type(path) is not str or not path or "\x00" in path or not os.path.isabs(path) or os.path.normpath(path) != path or os.path.realpath(path) != path:
        raise ValueError("runtime {} must be canonical and absolute".format(name))
    return path


def _paths_overlap(left, right):
    """Return whether either canonical path contains the other."""
    common = os.path.commonpath((left, right))
    return common == left or common == right


def _validated_sources(sources):
    """Bound, sort and collision-check an already-detached source tuple."""
    if type(sources) is not tuple:
        raise ValueError("fragment sources must be a tuple")
    if len(sources) > MAX_FRAGMENT_SOURCES:
        raise ValueError("fragment sources exceed the runtime limit")
    if not sources:
        raise ValueError(
            "enabled shadow runtime requires at least one fragment source",
        )
    if any(type(source) is not RuntimeFragmentSource for source in sources):
        raise ValueError(
            "fragment sources must contain RuntimeFragmentSource values",
        )
    detached = tuple(
        sorted(sources, key=lambda source: source.provider_id),
    )
    provider_ids = tuple(source.provider_id for source in detached)
    if provider_ids != tuple(sorted(set(provider_ids))):
        raise ValueError("fragment source provider_ids must be unique")
    return detached


def _fragment_state_path(state_directory, provider_id):
    """Derive a private value-free filename for one provider-owned state."""
    binding = hashlib.sha256(_safe_utf8_bytes(provider_id)).hexdigest()
    return os.path.join(
        state_directory,
        "fragments",
        "{}.json".format(binding),
    )


def _runtime_state_paths(state_directory, sources):
    """Return every reserved durable state identity before construction."""
    records = (
        os.path.join(state_directory, "compiled.json"),
        os.path.join(state_directory, "materializer.json"),
        os.path.join(state_directory, "materializer.json.fence"),
        os.path.join(state_directory, "target-journal.json"),
    ) + tuple(_fragment_state_path(state_directory, source.provider_id) for source in sources)
    locks = tuple("{}.lock".format(path) for path in records)
    return records + locks


def _path_uses_atomic_temp_namespace(candidate, target):
    """Return whether candidate equals or descends from a target temp name."""
    directory = os.path.dirname(target)
    basename = os.path.basename(target)
    try:
        relative = os.path.relpath(candidate, directory)
    except ValueError:
        return False
    if relative == os.pardir or relative.startswith("{}{}".format(os.pardir, os.sep)):
        return False
    candidate_basename = relative.split(os.sep, 1)[0]
    return candidate_basename.startswith(".{}.".format(basename)) and candidate_basename.endswith(".tmp")


def _preflight_runtime_namespaces(
    state_directory,
    overlay_path,
    sources,
):
    """Reject every durable, lock and atomic-temp namespace collision."""
    reserved = _runtime_state_paths(state_directory, sources)
    overlay_namespaces = (overlay_path, "{}.lock".format(overlay_path))
    for overlay_namespace in overlay_namespaces:
        if _paths_overlap(state_directory, overlay_namespace):
            raise ValueError(
                "runtime overlay namespace overlaps durable state",
            )
        if overlay_namespace in reserved:
            raise ValueError(
                "runtime overlay namespace collides with durable state",
            )
    for state_path in (state_directory,) + reserved:
        if _path_uses_atomic_temp_namespace(state_path, overlay_path):
            raise ValueError(
                "runtime durable state collides with overlay temp namespace",
            )
    for reserved_path in reserved:
        if _path_uses_atomic_temp_namespace(overlay_path, reserved_path):
            raise ValueError(
                "runtime overlay_path collides with durable temp namespace",
            )


def build_lattice_shadow_runtime(
    config=None,
    fragment_sources=(),
    override_reader=None,
):
    """Build one default-off detached composition from generic sources."""
    if config is None:
        config = LatticeRuntimeConfig()
    if not isinstance(config, LatticeRuntimeConfig):
        raise ValueError("config must be LatticeRuntimeConfig")
    if not config.enabled:
        return LatticeShadowRuntime(
            config,
            LatticeRuntimePhase.DISABLED,
        )

    state_directory, overlay_path = _enabled_paths(config)
    sources = _validated_sources(fragment_sources)
    _preflight_runtime_namespaces(
        state_directory,
        overlay_path,
        sources,
    )
    if override_reader is not None and not callable(override_reader):
        raise ValueError("override_reader must be callable")

    materializer_store = FileAtomicMaterializationStateStore(
        os.path.join(state_directory, "materializer.json"),
    )
    target_journal = FileOpaqueTargetJournal(
        os.path.join(state_directory, "target-journal.json"),
    )
    target = GeneratedOverlayWholeConfigTarget(
        overlay_path,
        target_journal,
        config.allowed_keys,
    )
    materializer = AtomicConfigMaterializer(
        state_store=materializer_store,
        target=target,
        enabled=False,
    )
    compiled_store = FileCompiledLatticeStateStore(
        os.path.join(state_directory, "compiled.json"),
    )

    components = []
    for source in sources:
        state_store = FileFragmentAdapterStateStore(
            _fragment_state_path(
                state_directory,
                source.provider_id,
            )
        )
        component = source.factory(state_store)
        adapter_factory = getattr(
            component,
            FragmentAdapterRegistry.DISCOVERY_METHOD,
            None,
        )
        if not callable(adapter_factory):
            raise ValueError(
                "fragment source factory returned an invalid component",
            )
        components.append(component)

    registry = FragmentAdapterRegistry(enabled=True)
    discovered = registry.discover(tuple(components))
    expected = tuple(source.provider_id for source in sources)
    if tuple(sorted(discovered)) != expected:
        raise ValueError(
            "fragment source factory did not expose its declared provider",
        )
    compiler = registry.create_compiler(
        compiled_store,
        override_reader=override_reader,
    )
    return LatticeShadowRuntime(
        config,
        LatticeRuntimePhase.SHADOW_READY,
        registry=registry,
        compiler=compiler,
        materializer=materializer,
    )
