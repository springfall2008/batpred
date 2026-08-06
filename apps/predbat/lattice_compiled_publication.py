# -----------------------------------------------------------------------------
# Predbat Home Battery System - compiled Lattice publication
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Durable immutable publication for compiled Lattice auto-configuration.

The component in this module is deliberately additive and shadow-only.  It
uses the pure compiler's existing single-flight invalidation state machine,
but replaces its future materializer hand-off with one atomic publication.
It has no integration registry, MQTT, Home Assistant, configuration-write, or
other production runtime dependency.
"""

# cspell:ignore autoconfig

import hashlib
import threading
from dataclasses import dataclass
from typing import Optional

from lattice_autoconfig import (
    AutoConfigCompileError,
    AutoConfigPlan,
    CompileIssue,
    CompileRun,
    CompileStatus,
    LatticeAutoConfigCompiler,
    MaterializationReadiness,
    UserConfigOverride,
    _canonical_json,
    _plain,
    compile_auto_config,
)
from lattice_topology import TopologyValidationError


@dataclass(frozen=True)
class CompilationInvalidation:
    """One accepted input cause retained until a successful compile."""

    source_type: str
    source_id: str
    generation: int
    reason: str

    def __post_init__(self):
        """Validate and normalize one auditable invalidation cause."""
        if self.source_type not in ("provider", "user_override"):
            raise ValueError("invalidation source_type must be provider or user_override")
        if not isinstance(self.source_id, str) or not self.source_id.strip():
            raise ValueError("invalidation source_id must be a non-empty string")
        if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation < 0:
            raise ValueError("invalidation generation must be a non-negative integer")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError("invalidation reason must be a non-empty string")
        object.__setattr__(self, "source_id", self.source_id.strip())
        object.__setattr__(self, "reason", self.reason.strip())


@dataclass(frozen=True)
class UserOverrideSnapshot:
    """One durable generation of explicit runtime user overrides."""

    generation: int
    overrides: tuple

    def __post_init__(self):
        """Detach ordering from the reader and reject ambiguous arguments."""
        if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation < 0:
            raise ValueError("override generation must be a non-negative integer")
        if isinstance(self.overrides, (str, bytes)):
            raise ValueError("overrides must be an iterable of UserConfigOverride values")
        try:
            overrides = tuple(self.overrides)
        except TypeError as exc:
            raise ValueError("overrides must be an iterable of UserConfigOverride values") from exc
        if any(not isinstance(item, UserConfigOverride) for item in overrides):
            raise ValueError("overrides must contain UserConfigOverride values")
        arguments = [item.argument for item in overrides]
        if len(arguments) != len(set(arguments)):
            raise ValueError("override arguments must be unique")
        object.__setattr__(
            self,
            "overrides",
            tuple(sorted(overrides, key=lambda item: item.argument)),
        )


@dataclass(frozen=True)
class CompiledLatticeDiagnostics:
    """Immutable status and diagnostics for one compiler observation."""

    status: CompileStatus
    stale: bool
    degraded: bool
    issues: tuple
    readiness: Optional[MaterializationReadiness]

    def __post_init__(self):
        """Reject contradictory status flags or mutable diagnostic inputs."""
        if not isinstance(self.status, CompileStatus):
            raise ValueError("diagnostic status must be a CompileStatus")
        if not isinstance(self.stale, bool) or not isinstance(self.degraded, bool):
            raise ValueError("diagnostic flags must be booleans")
        if self.stale != (self.status is CompileStatus.STALE):
            raise ValueError("stale flag must match STALE status")
        if self.degraded != (self.status is CompileStatus.DEGRADED):
            raise ValueError("degraded flag must match DEGRADED status")
        issues = tuple(self.issues)
        if any(not isinstance(issue, CompileIssue) for issue in issues):
            raise ValueError("diagnostic issues must contain CompileIssue values")
        if self.readiness is not None and not isinstance(
            self.readiness,
            MaterializationReadiness,
        ):
            raise ValueError("diagnostic readiness must be MaterializationReadiness or None")
        object.__setattr__(self, "issues", issues)


@dataclass(frozen=True)
class CompiledLatticePublication:
    """One immutable version with its full durable provider input cursor."""

    lattice_version: int
    digest: str
    provider_generations: tuple
    provider_fingerprints: tuple
    provider_requested_generations: tuple
    user_override_generation: Optional[int]
    user_override_fingerprint: Optional[str]
    user_override_requested_generation: Optional[int]
    invalidation_causes: tuple
    feedback_token: str
    diagnostics: CompiledLatticeDiagnostics
    plan: AutoConfigPlan

    def __post_init__(self):
        """Validate publication integrity and normalize deterministic tuples."""
        if not isinstance(self.lattice_version, int) or isinstance(self.lattice_version, bool) or self.lattice_version < 1:
            raise ValueError("lattice_version must be a positive integer")
        if not isinstance(self.digest, str) or not self.digest:
            raise ValueError("publication digest must be a non-empty string")
        if not isinstance(self.plan, AutoConfigPlan):
            raise ValueError("publication plan must be an AutoConfigPlan")
        if self.plan.digest != self.digest:
            raise ValueError("publication digest must match its plan")

        provider_generations = tuple(self.provider_generations)
        if any(not isinstance(provider_id, str) or not provider_id or not isinstance(generation, int) or isinstance(generation, bool) or generation < 0 for provider_id, generation in provider_generations):
            raise ValueError("publication provider generations are invalid")
        if provider_generations != tuple(sorted(set(provider_generations))):
            raise ValueError("publication provider generations must be unique and sorted")
        durable_generations = dict(provider_generations)
        plan_generations = tuple(self.plan.provider_generations)
        if any(durable_generations.get(provider_id) != generation for provider_id, generation in plan_generations):
            raise ValueError("plan provider generations must be a consistent subset of the durable cursor")

        provider_fingerprints = tuple(self.provider_fingerprints)
        fingerprint_keys = []
        for item in provider_fingerprints:
            if not isinstance(item, tuple) or len(item) != 3:
                raise ValueError("provider fingerprints must contain provider/generation/digest tuples")
            provider_id, generation, fingerprint = item
            if not isinstance(provider_id, str) or not provider_id or not isinstance(generation, int) or isinstance(generation, bool) or generation < 0 or not isinstance(fingerprint, str) or not fingerprint:
                raise ValueError("publication provider fingerprint is invalid")
            fingerprint_keys.append((provider_id, generation))
        if tuple(fingerprint_keys) != provider_generations:
            raise ValueError("publication provider fingerprints must match provider generations")

        provider_requested_generations = tuple(self.provider_requested_generations)
        if any(not isinstance(provider_id, str) or not provider_id or not isinstance(generation, int) or isinstance(generation, bool) or generation < 0 for provider_id, generation in provider_requested_generations):
            raise ValueError("publication provider requested generations are invalid")
        if provider_requested_generations != tuple(sorted(set(provider_requested_generations))):
            raise ValueError("publication provider requested generations must be unique and sorted")
        requested_by_provider = dict(provider_requested_generations)
        if any(requested_by_provider.get(provider_id, -1) < generation for provider_id, generation in provider_generations):
            raise ValueError("provider requested generations must cover the observed cursor")

        if (self.user_override_generation is None) != (self.user_override_fingerprint is None):
            raise ValueError("override generation and fingerprint must both be present or absent")
        if self.user_override_generation is not None:
            if not isinstance(self.user_override_generation, int) or isinstance(self.user_override_generation, bool) or self.user_override_generation < 0 or not isinstance(self.user_override_fingerprint, str) or not self.user_override_fingerprint:
                raise ValueError("publication override generation is invalid")
        if self.user_override_generation is not None and self.user_override_requested_generation is None:
            raise ValueError("observed override cursor requires a requested generation")
        if self.user_override_requested_generation is not None:
            if not isinstance(self.user_override_requested_generation, int) or isinstance(self.user_override_requested_generation, bool) or self.user_override_requested_generation < 0:
                raise ValueError("publication override requested generation is invalid")
            if self.user_override_generation is not None and self.user_override_requested_generation < self.user_override_generation:
                raise ValueError("override requested generation must cover the observed cursor")

        causes = tuple(sorted(set(self.invalidation_causes), key=_cause_sort_key))
        if any(not isinstance(cause, CompilationInvalidation) for cause in causes):
            raise ValueError("publication invalidation causes must contain CompilationInvalidation values")
        if not isinstance(self.feedback_token, str) or not self.feedback_token.strip():
            raise ValueError("publication feedback_token must be a non-empty string")
        if not isinstance(self.diagnostics, CompiledLatticeDiagnostics):
            raise ValueError("publication diagnostics must be CompiledLatticeDiagnostics")
        if self.diagnostics.status not in (
            CompileStatus.FRESH,
            CompileStatus.DEGRADED,
        ):
            raise ValueError("only successful compile diagnostics may be published")
        if self.diagnostics.stale:
            raise ValueError("a successful immutable publication cannot be stale")
        if self.diagnostics.readiness != getattr(
            self.plan,
            "materialization_readiness",
            None,
        ):
            raise ValueError("publication readiness must match its plan")

        object.__setattr__(self, "provider_generations", provider_generations)
        object.__setattr__(self, "provider_fingerprints", provider_fingerprints)
        object.__setattr__(self, "provider_requested_generations", provider_requested_generations)
        object.__setattr__(self, "invalidation_causes", causes)
        object.__setattr__(self, "feedback_token", self.feedback_token.strip())


@dataclass(frozen=True)
class CompiledLatticeRun(CompileRun):
    """One drain result plus durable publication and live diagnostics."""

    publication: Optional[CompiledLatticePublication]
    published: bool
    diagnostics: CompiledLatticeDiagnostics
    causes: tuple

    def __post_init__(self):
        """Validate the additive publication-specific run fields."""
        if self.publication is not None and not isinstance(
            self.publication,
            CompiledLatticePublication,
        ):
            raise ValueError("run publication must be CompiledLatticePublication or None")
        if not isinstance(self.published, bool):
            raise ValueError("run published flag must be a boolean")
        if not isinstance(self.diagnostics, CompiledLatticeDiagnostics):
            raise ValueError("run diagnostics must be CompiledLatticeDiagnostics")
        causes = tuple(sorted(set(self.causes), key=_cause_sort_key))
        if any(not isinstance(cause, CompilationInvalidation) for cause in causes):
            raise ValueError("run causes must contain CompilationInvalidation values")
        object.__setattr__(self, "causes", causes)


class CompiledLatticeStateStore:
    """Injected durable compare-and-swap publication store contract."""

    def load(self):
        """Return the current CompiledLatticePublication or None."""
        raise NotImplementedError

    def compare_and_publish(self, expected_version, publication):
        """Atomically publish when the durable version equals expected_version."""
        raise NotImplementedError


class InMemoryCompiledLatticeStateStore(CompiledLatticeStateStore):
    """Thread-safe reference store for tests; deliberately not durable."""

    def __init__(self, publication=None):
        """Create a store optionally seeded with a validated publication."""
        if publication is not None and not isinstance(
            publication,
            CompiledLatticePublication,
        ):
            raise ValueError("initial publication must be CompiledLatticePublication or None")
        self._lock = threading.RLock()
        self._publication = publication
        self._writes = 0

    @property
    def writes(self):
        """Return the number of successful atomic publications."""
        with self._lock:
            return self._writes

    def load(self):
        """Return the immutable current publication."""
        with self._lock:
            return self._publication

    def compare_and_publish(self, expected_version, publication):
        """Atomically publish exactly the next monotonic version."""
        if not isinstance(expected_version, int) or isinstance(expected_version, bool) or expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        if not isinstance(publication, CompiledLatticePublication):
            raise ValueError("publication must be a CompiledLatticePublication")
        if publication.lattice_version != expected_version + 1:
            raise ValueError("publication version must be exactly expected_version + 1")
        with self._lock:
            current_version = self._publication.lattice_version if self._publication is not None else 0
            if current_version != expected_version:
                return False
            self._publication = publication
            self._writes += 1
            return True


def _cause_sort_key(cause):
    """Return deterministic ordering for generic compiler invalidations."""
    return (
        cause.source_type,
        cause.source_id,
        cause.generation,
        cause.reason,
    )


def _fingerprint_overrides(snapshot):
    """Bind one override generation to its exact immutable contents."""
    payload = {
        "generation": snapshot.generation,
        "overrides": [
            {
                "argument": item.argument,
                "value": _plain(item.value),
                "source_path": item.source_path,
            }
            for item in snapshot.overrides
        ],
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _same_durable_input(left, right):
    """Compare successful durable input cursors and observable diagnostics."""
    return (
        left.digest == right.digest
        and left.provider_generations == right.provider_generations
        and left.provider_fingerprints == right.provider_fingerprints
        and left.provider_requested_generations == right.provider_requested_generations
        and left.user_override_generation == right.user_override_generation
        and left.user_override_fingerprint == right.user_override_fingerprint
        and left.user_override_requested_generation == right.user_override_requested_generation
        and left.diagnostics == right.diagnostics
    )


def _causes_represented(causes, publication):
    """Return whether one publication already audits every local cause."""
    return set(causes).issubset(set(publication.invalidation_causes))


class CompiledLatticeCompiler(LatticeAutoConfigCompiler):
    """Dedicated shadow compiler that owns durable Lattice publication."""

    _OVERRIDE_SOURCE_ID = "user-overrides"

    def __init__(
        self,
        readers=None,
        state_store=None,
        override_reader=None,
        atomic_materializer=False,
        allow_provider_failover=False,
    ):
        """Create a publisher over registered fragment and override readers."""
        if state_store is None:
            raise ValueError("a durable compiled-Lattice state_store is required")
        if not callable(getattr(state_store, "load", None)) or not callable(getattr(state_store, "compare_and_publish", None)):
            raise ValueError("state_store must provide load and compare_and_publish")
        if override_reader is not None and not callable(override_reader):
            raise ValueError("override_reader must be callable")

        super().__init__(
            readers=readers,
            materializer=None,
            atomic_materializer=atomic_materializer,
            allow_provider_failover=allow_provider_failover,
        )
        self._state_store = state_store
        self._override_reader = override_reader
        self._override_requested_generation = -1
        self._override_observed_generation = -1
        self._override_generation_fingerprints = {}
        self._pending_causes = set()
        self._candidate_issues = ()
        self._candidate_override_generation = None
        self._candidate_override_fingerprint = None
        self._publication = None
        self._drain_local = threading.local()

        loaded = self._state_store.load()
        if loaded is not None and not isinstance(
            loaded,
            CompiledLatticePublication,
        ):
            raise ValueError("state_store.load must return CompiledLatticePublication or None")
        if loaded is not None:
            if (loaded.user_override_generation is not None or loaded.user_override_requested_generation is not None) and self._override_reader is None:
                raise ValueError("a published user-override input requires override_reader")
            with self._lock:
                self._install_publication(loaded)

    @property
    def publication(self):
        """Return the last durable immutable compiled-Lattice publication."""
        with self._lock:
            return self._publication

    @property
    def diagnostics(self):
        """Return current live diagnostics without mutating the publication."""
        with self._lock:
            readiness = self._publication.plan.materialization_readiness if self._publication is not None else None
            return CompiledLatticeDiagnostics(
                status=self._status,
                stale=self._status is CompileStatus.STALE,
                degraded=self._status is CompileStatus.DEGRADED,
                issues=self._issues,
                readiness=readiness,
            )

    def invalidate(
        self,
        provider_id,
        generation,
        reason,
        feedback_token=None,
    ):
        """Invalidate any registered provider and retain its audit cause."""
        with self._lock:
            accepted = super().invalidate(
                provider_id,
                generation,
                reason,
                feedback_token,
            )
            if accepted:
                self._pending_causes.add(
                    CompilationInvalidation(
                        "provider",
                        provider_id,
                        generation,
                        reason,
                    )
                )
            return accepted

    def invalidate_user_overrides(
        self,
        generation,
        reason,
        feedback_token=None,
    ):
        """Invalidate the durable user-override input monotonically."""
        if self._override_reader is None:
            raise RuntimeError("user override reader is not configured")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise ValueError("generation must be a non-negative integer")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        with self._lock:
            if feedback_token is not None and feedback_token in self._feedback_tokens:
                return False
            previous = max(
                self._override_requested_generation,
                self._override_observed_generation,
            )
            if generation <= previous:
                return False
            self._override_requested_generation = generation
            self._pending_causes.add(
                CompilationInvalidation(
                    "user_override",
                    self._OVERRIDE_SOURCE_ID,
                    generation,
                    reason,
                )
            )
            if self._compiling:
                self._follow_up = True
            else:
                self._pending = True
            return True

    def _read_user_overrides(self):
        """Fresh-read and validate the complete optional override snapshot."""
        if self._override_reader is None:
            self._candidate_override_generation = None
            self._candidate_override_fingerprint = None
            return (), ()
        try:
            snapshot = self._override_reader()
            if not isinstance(snapshot, UserOverrideSnapshot):
                raise ValueError("override reader must return UserOverrideSnapshot")
            fingerprint = _fingerprint_overrides(snapshot)
            with self._lock:
                required_generation = self._override_requested_generation
                previous_generation = self._override_observed_generation
                previous_fingerprint = self._override_generation_fingerprints.get(snapshot.generation)
                if snapshot.generation < previous_generation:
                    raise ValueError(
                        "override generation {} regressed from {}".format(
                            snapshot.generation,
                            previous_generation,
                        )
                    )
                if previous_fingerprint is not None and previous_fingerprint != fingerprint:
                    raise ValueError("override generation {} was reused with different content".format(snapshot.generation))
                if snapshot.generation < required_generation:
                    raise ValueError(
                        "override generation {} is behind invalidation {}".format(
                            snapshot.generation,
                            required_generation,
                        )
                    )
                self._override_observed_generation = snapshot.generation
                self._override_requested_generation = max(
                    required_generation,
                    snapshot.generation,
                )
                self._override_generation_fingerprints[snapshot.generation] = fingerprint
                self._candidate_override_generation = snapshot.generation
                self._candidate_override_fingerprint = fingerprint
            return snapshot.overrides, ()
        except (TypeError, ValueError) as exc:
            return (), (
                CompileIssue(
                    "user_overrides_invalid",
                    str(exc),
                    self._OVERRIDE_SOURCE_ID,
                ),
            )
        except Exception as exc:
            return (), (
                CompileIssue(
                    "user_overrides_read_failed",
                    "{}: {}".format(type(exc).__name__, exc),
                    self._OVERRIDE_SOURCE_ID,
                ),
            )

    def _compile_attempt(self):
        """Fresh-read every input and compile exactly one candidate plan."""
        snapshots, provider_issues = self._read_all()
        overrides, override_issues = self._read_user_overrides()
        issues = provider_issues + override_issues
        if override_issues:
            detail = "durable user overrides are unavailable or invalid"
            self._candidate_issues = issues + (CompileIssue("compile_failed", detail),)
            return None, self._candidate_issues

        with self._lock:
            active_providers = set(dict(self._active_plan.provider_generations)) if self._active_plan is not None else set()
        usable_providers = {snapshot.provider_id for snapshot in snapshots}
        unavailable_active = sorted(active_providers - usable_providers)
        if unavailable_active and not self._allow_provider_failover:
            detail = "previously active provider(s) unavailable: {}".format(", ".join(unavailable_active))
            self._candidate_issues = issues + (
                CompileIssue("active_provider_unavailable", detail),
                CompileIssue("compile_failed", detail),
            )
            return None, self._candidate_issues

        try:
            plan = compile_auto_config(
                snapshots,
                overrides,
                atomic_materializer=self._atomic_materializer,
            )
        except (
            AutoConfigCompileError,
            TopologyValidationError,
            TypeError,
            ValueError,
        ) as exc:
            self._candidate_issues = issues + (CompileIssue("compile_failed", str(exc)),)
            return None, self._candidate_issues
        self._candidate_issues = issues
        return plan, issues

    def _materialize_if_changed(self, plan):
        """Publish each changed durable input cursor without materializing."""
        with self._lock:
            causes = tuple(sorted(self._pending_causes, key=_cause_sort_key))
            self._drain_local.hook_called = True
            self._drain_local.causes = causes

            expected_version = self._publication.lattice_version if self._publication is not None else 0
            lattice_version = expected_version + 1
            feedback_token = "lattice-publication-{}-{}".format(
                lattice_version,
                plan.digest[:16],
            )
            status = CompileStatus.DEGRADED if self._candidate_issues else CompileStatus.FRESH
            provider_generations, provider_fingerprints, provider_requested_generations = self._durable_provider_cursor()
            diagnostics = CompiledLatticeDiagnostics(
                status=status,
                stale=False,
                degraded=status is CompileStatus.DEGRADED,
                issues=self._candidate_issues,
                readiness=plan.materialization_readiness,
            )
            publication = CompiledLatticePublication(
                lattice_version=lattice_version,
                digest=plan.digest,
                provider_generations=provider_generations,
                provider_fingerprints=provider_fingerprints,
                provider_requested_generations=provider_requested_generations,
                user_override_generation=self._candidate_override_generation,
                user_override_fingerprint=self._candidate_override_fingerprint,
                user_override_requested_generation=(self._override_requested_generation if self._override_reader is not None and self._override_requested_generation >= 0 else None),
                invalidation_causes=causes,
                feedback_token=feedback_token,
                diagnostics=diagnostics,
                plan=plan,
            )
            if self._publication is not None and _same_durable_input(self._publication, publication) and _causes_represented(causes, self._publication):
                self._active_plan = self._publication.plan
                self._pending_causes.clear()
                return 0, ()
            self._feedback_tokens.append(feedback_token)

            try:
                committed = self._state_store.compare_and_publish(
                    expected_version,
                    publication,
                )
            except Exception as exc:
                recovered = self._recover_ambiguous_publication(publication)
                if recovered:
                    self._pending_causes.clear()
                    self._drain_local.published = True
                    return 0, ()
                detail = "{}: {}".format(type(exc).__name__, exc)
                return 0, (
                    CompileIssue("publication_failed", detail),
                    CompileIssue(
                        "compile_failed",
                        "compiled-Lattice publication failed",
                    ),
                )

            if committed is not True:
                try:
                    current = self._load_current_publication()
                except Exception as exc:
                    detail = "{}: {}".format(type(exc).__name__, exc)
                    return 0, (
                        CompileIssue("publication_failed", detail),
                        CompileIssue(
                            "compile_failed",
                            "compiled-Lattice publication state could not be reloaded",
                        ),
                    )
                if current is not None and _same_durable_input(current, publication):
                    if _causes_represented(causes, current):
                        self._pending_causes.clear()
                    else:
                        self._pending = True
                    return 0, ()
                return 0, (
                    CompileIssue(
                        "publication_conflict",
                        "durable lattice_version changed before atomic publication",
                    ),
                    CompileIssue(
                        "compile_failed",
                        "compiled-Lattice publication conflicted",
                    ),
                )

            self._install_publication(publication)
            self._pending_causes.clear()
            self._drain_local.published = True
            return 0, ()

    def _recover_ambiguous_publication(self, candidate):
        """Recover only when the store contains the exact attempted publication."""
        try:
            current = self._load_current_publication()
        except Exception:
            return False
        return current == candidate

    def _durable_provider_cursor(self):
        """Return prior-or-fresh fingerprints for current registered readers."""
        provider_generations = []
        provider_fingerprints = []
        provider_requested_generations = []
        for provider_id in sorted(self._readers):
            generation = self._observed_generations.get(provider_id)
            if generation is not None:
                fingerprint = self._generation_fingerprints.get((provider_id, generation))
                if fingerprint is not None:
                    provider_generations.append((provider_id, generation))
                    provider_fingerprints.append((provider_id, generation, fingerprint))
            requested_generation = self._requested_generations.get(provider_id)
            if requested_generation is not None and requested_generation >= 0:
                provider_requested_generations.append((provider_id, requested_generation))
        return (
            tuple(provider_generations),
            tuple(provider_fingerprints),
            tuple(provider_requested_generations),
        )

    def _load_current_publication(self):
        """Load and install the durable publication after a CAS conflict."""
        current = self._state_store.load()
        if current is not None and not isinstance(
            current,
            CompiledLatticePublication,
        ):
            raise ValueError("state_store.load must return CompiledLatticePublication or None")
        if current is not None:
            self._install_publication(current)
        return current

    def _install_publication(self, publication):
        """Restore one validated publication into compiler safety state."""
        self._publication = publication
        self._active_plan = publication.plan
        self._status = publication.diagnostics.status
        self._issues = publication.diagnostics.issues
        for provider_id, generation, fingerprint in publication.provider_fingerprints:
            self._observed_generations[provider_id] = generation
            self._generation_fingerprints[(provider_id, generation)] = fingerprint
        for provider_id, generation in publication.provider_requested_generations:
            self._requested_generations[provider_id] = generation
        if publication.user_override_generation is not None:
            generation = publication.user_override_generation
            self._override_observed_generation = generation
            self._override_generation_fingerprints[generation] = publication.user_override_fingerprint
        if publication.user_override_requested_generation is not None:
            self._override_requested_generation = publication.user_override_requested_generation
        self._feedback_tokens.append(publication.feedback_token)

    def drain(self):
        """Drain once, preserving publication-plan identity in the result."""
        self._drain_local.published = False
        self._drain_local.hook_called = False
        with self._lock:
            self._drain_local.causes = tuple(sorted(self._pending_causes, key=_cause_sort_key))
        run = super().drain()
        if not self._drain_local.hook_called:
            with self._lock:
                self._drain_local.causes = tuple(sorted(self._pending_causes, key=_cause_sort_key))
        with self._lock:
            publication = self._publication
            if publication is not None:
                self._active_plan = publication.plan
                active_plan = publication.plan
            else:
                active_plan = run.plan
        diagnostics = CompiledLatticeDiagnostics(
            status=run.status,
            stale=run.status is CompileStatus.STALE,
            degraded=run.status is CompileStatus.DEGRADED,
            issues=run.issues,
            readiness=(publication.plan.materialization_readiness if publication is not None else None),
        )
        return CompiledLatticeRun(
            run.attempts,
            run.status,
            active_plan,
            run.issues,
            run.invalidations,
            run.materializations,
            run.pending,
            publication,
            bool(getattr(self._drain_local, "published", False)),
            diagnostics,
            tuple(getattr(self._drain_local, "causes", ())),
        )
