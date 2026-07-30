# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice provider-path fallback boundary
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Pure, default-off provider-path selection for future Lattice control.

The current production control path does not import this module.  It defines a
small fail-closed boundary for a later integration tranche:

* targets are resolved only through provider-qualified aliases;
* gateway-local paths always rank before vendor-cloud paths;
* per-target path health has an explicit freshness TTL and retry cooldown;
* executors return APPLIED, NOT_APPLIED, or UNKNOWN without truthy coercion;
* fallback is possible only after a definite, fallback-safe NOT_APPLIED.

There are no network, filesystem, MQTT, Home Assistant, inverter, or component
registration dependencies here.  Disabled dispatch returns before consulting
the clock, aliases, health ledger, path declarations, or executors.
"""

# cspell:ignore codepoint cooldown noncharacter

import asyncio
import inspect
import math
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


MAX_IDENTITY_BYTES = 128
MAX_DETAIL_BYTES = 256
_IDENTIFIER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:/-]{0,126}[A-Za-z0-9])?$")
_OBSERVED_AT_UNSET = object()


class ProviderFallbackValidationError(ValueError):
    """Raised when declarations cannot produce an unambiguous safe router."""


class ProviderExecutionState(Enum):
    """What an executor can honestly prove about one attempted write."""

    APPLIED = "APPLIED"
    NOT_APPLIED = "NOT_APPLIED"
    UNKNOWN = "UNKNOWN"


class ProviderPathKind(Enum):
    """The only access-path classes supported by the first selector."""

    GATEWAY_LOCAL = "gw-local"
    VENDOR_CLOUD = "vendor-cloud"


class ProviderPathHealthPhase(Enum):
    """Selection-relevant projection of one path's last observation."""

    UNOBSERVED = "unobserved"
    HEALTHY = "healthy"
    PROBE = "probe"
    COOLING_FALLBACK_SAFE = "cooling_fallback_safe"
    COOLING_BLOCKED = "cooling_blocked"


@dataclass(frozen=True, order=True)
class QualifiedTargetAlias:
    """One provider-scoped alias; unqualified lookup is intentionally absent."""

    provider_id: str
    alias: str

    def __post_init__(self):
        """Reject ambiguous, unbounded, or non-canonical identity text."""
        _require_identifier(self.provider_id, "provider_id", lowercase=True)
        _require_identifier(self.alias, "target alias")


@dataclass(frozen=True, order=True)
class ProviderPathIdentity:
    """Stable identity for health and execution observations."""

    target_id: str
    provider_id: str
    path_id: str

    def __post_init__(self):
        """Validate every component before it can become a ledger key."""
        _require_identifier(self.target_id, "target_id")
        _require_identifier(self.provider_id, "provider_id", lowercase=True)
        _require_identifier(self.path_id, "path_id")


@dataclass(frozen=True)
class ProviderPathHealthPolicy:
    """Freshness and retry bounds for one target-specific path."""

    ttl_seconds: float = 30.0
    cooldown_seconds: float = 60.0

    def __post_init__(self):
        """Require finite positive durations and reject bool-as-number."""
        _require_duration(self.ttl_seconds, "health ttl_seconds")
        _require_duration(self.cooldown_seconds, "health cooldown_seconds")


@dataclass(frozen=True)
class ProviderExecutionResult:
    """Strict executor result whose fallback claim is independently explicit."""

    state: ProviderExecutionState
    fallback_safe: bool = False
    verified: bool = False
    applied_value: object = None
    detail: str = ""

    def __post_init__(self):
        """Forbid combinations that overstate what the executor knows."""
        if not isinstance(self.state, ProviderExecutionState):
            raise ProviderFallbackValidationError(
                "execution state must be ProviderExecutionState",
            )
        if type(self.fallback_safe) is not bool:
            raise ProviderFallbackValidationError(
                "fallback_safe must be a boolean",
            )
        if type(self.verified) is not bool:
            raise ProviderFallbackValidationError("verified must be a boolean")
        _require_detail(self.detail)

        if self.state is ProviderExecutionState.APPLIED:
            if not self.verified:
                raise ProviderFallbackValidationError(
                    "APPLIED must be verified",
                )
            if self.fallback_safe:
                raise ProviderFallbackValidationError(
                    "APPLIED cannot be fallback-safe",
                )
            return

        if self.applied_value is not None:
            raise ProviderFallbackValidationError(
                "{} cannot carry applied_value".format(self.state.value),
            )
        if self.verified:
            raise ProviderFallbackValidationError(
                "{} cannot be verified".format(self.state.value),
            )
        if self.state is ProviderExecutionState.UNKNOWN and self.fallback_safe:
            raise ProviderFallbackValidationError(
                "UNKNOWN can never be fallback-safe",
            )

    @classmethod
    def applied(cls, applied_value=None, detail=""):
        """Build a verified APPLIED result."""
        return cls(
            ProviderExecutionState.APPLIED,
            verified=True,
            applied_value=applied_value,
            detail=detail,
        )

    @classmethod
    def not_applied(cls, fallback_safe=False, detail=""):
        """Build a definite NOT_APPLIED result with an explicit safety claim."""
        return cls(
            ProviderExecutionState.NOT_APPLIED,
            fallback_safe=fallback_safe,
            detail=detail,
        )

    @classmethod
    def unknown(cls, detail=""):
        """Build an UNKNOWN result that can never authorize fallback."""
        return cls(ProviderExecutionState.UNKNOWN, detail=detail)


@dataclass(frozen=True)
class ProviderPath:
    """One pure declaration joining a logical target to an injected executor."""

    target_id: str
    provider_id: str
    path_id: str
    kind: ProviderPathKind
    executor: Callable
    aliases: tuple = ()
    health_policy: ProviderPathHealthPolicy = field(
        default_factory=ProviderPathHealthPolicy,
    )

    def __post_init__(self):
        """Validate path identity, callable seam, aliases, and health policy."""
        ProviderPathIdentity(
            self.target_id,
            self.provider_id,
            self.path_id,
        )
        if not isinstance(self.kind, ProviderPathKind):
            raise ProviderFallbackValidationError(
                "path kind must be ProviderPathKind",
            )
        if not callable(self.executor):
            raise ProviderFallbackValidationError(
                "path executor must be callable",
            )
        if type(self.aliases) is not tuple:
            raise ProviderFallbackValidationError("path aliases must be a tuple")
        seen = set()
        for alias in self.aliases:
            qualified = QualifiedTargetAlias(self.provider_id, alias)
            if qualified in seen:
                raise ProviderFallbackValidationError(
                    "duplicate alias {} for provider {}".format(
                        alias,
                        self.provider_id,
                    ),
                )
            seen.add(qualified)
        if not isinstance(self.health_policy, ProviderPathHealthPolicy):
            raise ProviderFallbackValidationError(
                "health_policy must be ProviderPathHealthPolicy",
            )

    @property
    def identity(self):
        """Return the stable per-target path identity."""
        return ProviderPathIdentity(
            self.target_id,
            self.provider_id,
            self.path_id,
        )

    @property
    def qualified_aliases(self):
        """Return explicit aliases plus the provider-qualified target id."""
        aliases = [QualifiedTargetAlias(self.provider_id, self.target_id)]
        aliases.extend(QualifiedTargetAlias(self.provider_id, alias) for alias in self.aliases)
        return tuple(aliases)


@dataclass(frozen=True)
class ProviderPathHealthRecord:
    """Last accepted result for exactly one target/provider/path identity."""

    identity: ProviderPathIdentity
    result: ProviderExecutionResult
    observed_at: float

    def __post_init__(self):
        """Validate a self-contained immutable health observation."""
        if not isinstance(self.identity, ProviderPathIdentity):
            raise ProviderFallbackValidationError(
                "health identity must be ProviderPathIdentity",
            )
        if not isinstance(self.result, ProviderExecutionResult):
            raise ProviderFallbackValidationError(
                "health result must be ProviderExecutionResult",
            )
        _require_timestamp(self.observed_at, "health observed_at")


@dataclass(frozen=True)
class ProviderPathAttempt:
    """One bounded, provider-qualified attempt in dispatch order."""

    identity: ProviderPathIdentity
    kind: ProviderPathKind
    result: ProviderExecutionResult

    def __post_init__(self):
        """Validate retained attempt evidence."""
        if not isinstance(self.identity, ProviderPathIdentity):
            raise ProviderFallbackValidationError(
                "attempt identity must be ProviderPathIdentity",
            )
        if not isinstance(self.kind, ProviderPathKind):
            raise ProviderFallbackValidationError(
                "attempt kind must be ProviderPathKind",
            )
        if not isinstance(self.result, ProviderExecutionResult):
            raise ProviderFallbackValidationError(
                "attempt result must be ProviderExecutionResult",
            )


@dataclass(frozen=True)
class ProviderFallbackResult:
    """Final router decision with the complete ordered attempt evidence."""

    state: ProviderExecutionState
    fallback_safe: bool
    attempts: tuple = ()
    selected_path: Optional[ProviderPathIdentity] = None
    disabled: bool = False
    detail: str = ""

    def __post_init__(self):
        """Keep aggregate truth no stronger than its terminal evidence."""
        if not isinstance(self.state, ProviderExecutionState):
            raise ProviderFallbackValidationError(
                "fallback result state must be ProviderExecutionState",
            )
        if type(self.fallback_safe) is not bool:
            raise ProviderFallbackValidationError(
                "fallback result fallback_safe must be a boolean",
            )
        if type(self.attempts) is not tuple:
            raise ProviderFallbackValidationError(
                "fallback result attempts must be a tuple",
            )
        if any(not isinstance(attempt, ProviderPathAttempt) for attempt in self.attempts):
            raise ProviderFallbackValidationError(
                "fallback result attempts are invalid",
            )
        if self.selected_path is not None and not isinstance(self.selected_path, ProviderPathIdentity):
            raise ProviderFallbackValidationError(
                "selected_path must be ProviderPathIdentity",
            )
        if type(self.disabled) is not bool:
            raise ProviderFallbackValidationError(
                "disabled must be a boolean",
            )
        _require_detail(self.detail)
        if self.state is not ProviderExecutionState.NOT_APPLIED and self.fallback_safe:
            raise ProviderFallbackValidationError(
                "only NOT_APPLIED can be fallback-safe",
            )
        if self.disabled and (self.state is not ProviderExecutionState.UNKNOWN or self.fallback_safe or self.attempts or self.selected_path is not None):
            raise ProviderFallbackValidationError(
                "disabled result must fail closed without attempts",
            )


class ProviderPathHealthLedger:
    """Process-local health ledger with monotonic observation protection."""

    def __init__(self, clock=None):
        """Create an empty ledger around an injectable monotonic clock."""
        self._clock = clock or time.monotonic
        if not callable(self._clock):
            raise ProviderFallbackValidationError("clock must be callable")
        self._records = {}

    def now(self):
        """Read and validate the injected clock."""
        value = self._clock()
        _require_timestamp(value, "clock value")
        return float(value)

    def observe(self, identity, result, observed_at=_OBSERVED_AT_UNSET):
        """Accept a new or idempotent observation; reject time regressions."""
        if not isinstance(identity, ProviderPathIdentity):
            raise ProviderFallbackValidationError(
                "health identity must be ProviderPathIdentity",
            )
        if not isinstance(result, ProviderExecutionResult):
            raise ProviderFallbackValidationError(
                "health result must be ProviderExecutionResult",
            )
        timestamp = self.now() if observed_at is _OBSERVED_AT_UNSET else observed_at
        _require_timestamp(timestamp, "health observed_at")
        timestamp = float(timestamp)
        previous = self._records.get(identity)
        if previous is not None:
            if timestamp < previous.observed_at:
                raise ProviderFallbackValidationError(
                    "health observation time regressed",
                )
            if timestamp == previous.observed_at and result != previous.result:
                raise ProviderFallbackValidationError(
                    "health observation reused timestamp with different result",
                )
        record = ProviderPathHealthRecord(identity, result, timestamp)
        self._records[identity] = record
        return record

    def record(self, identity):
        """Return the immutable current observation for one exact identity."""
        if not isinstance(identity, ProviderPathIdentity):
            raise ProviderFallbackValidationError(
                "health identity must be ProviderPathIdentity",
            )
        return self._records.get(identity)

    def phase(self, path, now=None):
        """Project health into a deterministic selection decision."""
        if not isinstance(path, ProviderPath):
            raise ProviderFallbackValidationError(
                "health path must be ProviderPath",
            )
        timestamp = self.now() if now is None else now
        _require_timestamp(timestamp, "health decision time")
        timestamp = float(timestamp)
        record = self._records.get(path.identity)
        if record is None:
            return ProviderPathHealthPhase.UNOBSERVED
        if timestamp < record.observed_at:
            raise ProviderFallbackValidationError(
                "health decision time precedes observation",
            )

        age = timestamp - record.observed_at
        if record.result.state is ProviderExecutionState.APPLIED:
            if age <= path.health_policy.ttl_seconds:
                return ProviderPathHealthPhase.HEALTHY
            return ProviderPathHealthPhase.PROBE
        if age >= path.health_policy.cooldown_seconds:
            return ProviderPathHealthPhase.PROBE
        if record.result.state is ProviderExecutionState.NOT_APPLIED and record.result.fallback_safe:
            return ProviderPathHealthPhase.COOLING_FALLBACK_SAFE
        return ProviderPathHealthPhase.COOLING_BLOCKED


class LatticeProviderFallbackRouter:
    """Default-off serial selector that never falls through UNKNOWN."""

    def __init__(self, paths, enabled=False, health_ledger=None):
        """Validate an immutable routing table without registering it anywhere."""
        if type(enabled) is not bool:
            raise ProviderFallbackValidationError(
                "router enabled gate must be a boolean",
            )
        if type(paths) is not tuple:
            raise ProviderFallbackValidationError("paths must be a tuple")
        if any(not isinstance(path, ProviderPath) for path in paths):
            raise ProviderFallbackValidationError(
                "every path must be ProviderPath",
            )
        if health_ledger is None:
            health_ledger = ProviderPathHealthLedger()
        if not isinstance(health_ledger, ProviderPathHealthLedger):
            raise ProviderFallbackValidationError(
                "health_ledger must be ProviderPathHealthLedger",
            )

        identities = set()
        aliases = {}
        by_target = {}
        for path in paths:
            identity = path.identity
            if identity in identities:
                raise ProviderFallbackValidationError(
                    "provider path identity collision: {}".format(identity),
                )
            identities.add(identity)
            for alias in path.qualified_aliases:
                previous_target = aliases.get(alias)
                if previous_target is not None and previous_target != path.target_id:
                    raise ProviderFallbackValidationError(
                        "provider-qualified alias collision: {}".format(alias),
                    )
                aliases[alias] = path.target_id
            by_target.setdefault(path.target_id, []).append(path)

        self._enabled = enabled
        self._health = health_ledger
        self._aliases = aliases
        self._by_target = {target_id: tuple(sorted(target_paths, key=_path_rank)) for target_id, target_paths in by_target.items()}
        self._lock = None
        self._lock_loop = None

    @property
    def enabled(self):
        """Expose the immutable explicit gate."""
        return self._enabled

    @property
    def health_ledger(self):
        """Expose health observations for detached diagnostics and tests."""
        return self._health

    def ranked_paths(self, qualified_alias):
        """Resolve one qualified alias and return deterministic path order."""
        if not isinstance(qualified_alias, QualifiedTargetAlias):
            raise ProviderFallbackValidationError(
                "target lookup must be QualifiedTargetAlias",
            )
        target_id = self._aliases.get(qualified_alias)
        if target_id is None:
            return ()
        return self._by_target.get(target_id, ())

    async def dispatch(self, qualified_alias, request):
        """Execute ranked paths, falling through only explicit safe rejection."""
        if not self._enabled:
            return ProviderFallbackResult(
                ProviderExecutionState.UNKNOWN,
                fallback_safe=False,
                disabled=True,
                detail="provider fallback router is disabled",
            )

        try:
            lock = self._dispatch_lock()
        except Exception as exc:
            return ProviderFallbackResult(
                ProviderExecutionState.UNKNOWN,
                fallback_safe=False,
                detail=_safe_detail(
                    "dispatch lock failed: {}".format(type(exc).__name__),
                ),
            )

        async with lock:
            try:
                paths = self.ranked_paths(qualified_alias)
                decision_time = self._health.now()
            except Exception as exc:
                return ProviderFallbackResult(
                    ProviderExecutionState.UNKNOWN,
                    fallback_safe=False,
                    detail=_safe_detail(
                        "routing preflight failed: {}".format(
                            type(exc).__name__,
                        ),
                    ),
                )
            if not paths:
                return ProviderFallbackResult(
                    ProviderExecutionState.NOT_APPLIED,
                    fallback_safe=True,
                    detail="no declared provider path",
                )

            attempts = []
            skipped_safe = False
            for path in paths:
                try:
                    phase = self._health.phase(path, now=decision_time)
                except Exception as exc:
                    return ProviderFallbackResult(
                        ProviderExecutionState.UNKNOWN,
                        fallback_safe=False,
                        attempts=tuple(attempts),
                        selected_path=path.identity,
                        detail=_safe_detail(
                            "health decision failed: {}".format(
                                type(exc).__name__,
                            ),
                        ),
                    )
                if phase is ProviderPathHealthPhase.COOLING_FALLBACK_SAFE:
                    skipped_safe = True
                    continue
                if phase is ProviderPathHealthPhase.COOLING_BLOCKED:
                    return ProviderFallbackResult(
                        ProviderExecutionState.UNKNOWN,
                        fallback_safe=False,
                        attempts=tuple(attempts),
                        selected_path=path.identity,
                        detail="higher-ranked path is cooling after unsafe result",
                    )

                result = await _execute(path, request)
                try:
                    self._health.observe(path.identity, result)
                except Exception as exc:
                    attempt = ProviderPathAttempt(
                        path.identity,
                        path.kind,
                        ProviderExecutionResult.unknown(
                            "health persistence failed",
                        ),
                    )
                    attempts.append(attempt)
                    return ProviderFallbackResult(
                        ProviderExecutionState.UNKNOWN,
                        fallback_safe=False,
                        attempts=tuple(attempts),
                        selected_path=path.identity,
                        detail=_safe_detail(
                            "health persistence failed: {}".format(
                                type(exc).__name__,
                            ),
                        ),
                    )
                attempts.append(
                    ProviderPathAttempt(path.identity, path.kind, result),
                )
                if result.state is ProviderExecutionState.APPLIED:
                    return ProviderFallbackResult(
                        ProviderExecutionState.APPLIED,
                        fallback_safe=False,
                        attempts=tuple(attempts),
                        selected_path=path.identity,
                        detail=result.detail,
                    )
                if result.state is ProviderExecutionState.NOT_APPLIED and result.fallback_safe:
                    skipped_safe = True
                    continue
                return ProviderFallbackResult(
                    result.state,
                    fallback_safe=False,
                    attempts=tuple(attempts),
                    selected_path=path.identity,
                    detail=result.detail,
                )

            return ProviderFallbackResult(
                ProviderExecutionState.NOT_APPLIED,
                fallback_safe=skipped_safe,
                attempts=tuple(attempts),
                detail="all provider paths definitely declined",
            )

    def _dispatch_lock(self):
        """Lazily bind the serial lock to the active event loop."""
        loop = asyncio.get_running_loop()
        if self._lock_loop is loop:
            return self._lock
        if self._lock is not None and self._lock.locked():
            raise ProviderFallbackValidationError(
                "router cannot move an active dispatch across event loops",
            )
        self._lock = asyncio.Lock()
        self._lock_loop = loop
        return self._lock


async def _execute(path, request):
    """Invoke one executor and convert exceptions or invalid truth to UNKNOWN."""
    try:
        result = path.executor(request)
        if inspect.isawaitable(result):
            result = await result
    except BaseException as exc:
        if isinstance(
            exc,
            (asyncio.CancelledError, KeyboardInterrupt, SystemExit),
        ):
            raise
        return ProviderExecutionResult.unknown(
            _safe_detail("executor raised {}".format(type(exc).__name__)),
        )
    if not isinstance(result, ProviderExecutionResult):
        return ProviderExecutionResult.unknown(
            "executor returned invalid result",
        )
    return result


def _path_rank(path):
    """Rank local before cloud, then use stable provider/path identities."""
    kind_rank = {
        ProviderPathKind.GATEWAY_LOCAL: 0,
        ProviderPathKind.VENDOR_CLOUD: 1,
    }
    return (
        kind_rank[path.kind],
        path.provider_id.encode("ascii"),
        path.path_id.encode("ascii"),
    )


def _require_identifier(value, name, lowercase=False):
    """Require bounded canonical ASCII identity text."""
    if not isinstance(value, str):
        raise ProviderFallbackValidationError(
            "{} must be a string".format(name),
        )
    try:
        encoded = value.encode("ascii", "strict")
    except UnicodeEncodeError as exc:
        raise ProviderFallbackValidationError(
            "{} must be canonical ASCII".format(name),
        ) from exc
    if not encoded or len(encoded) > MAX_IDENTITY_BYTES or _IDENTIFIER.fullmatch(value) is None:
        raise ProviderFallbackValidationError(
            "{} must be a bounded canonical identifier".format(name),
        )
    if lowercase and value != value.lower():
        raise ProviderFallbackValidationError(
            "{} must be lowercase".format(name),
        )


def _require_duration(value, name):
    """Require a finite strictly-positive duration."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ProviderFallbackValidationError(
            "{} must be a finite positive number".format(name),
        )


def _require_timestamp(value, name):
    """Require a finite non-negative monotonic timestamp."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ProviderFallbackValidationError(
            "{} must be a finite non-negative number".format(name),
        )


def _require_detail(value):
    """Require bounded printable diagnostic text."""
    if not isinstance(value, str):
        raise ProviderFallbackValidationError("detail must be a string")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise ProviderFallbackValidationError(
            "detail must be valid UTF-8",
        ) from exc
    if len(encoded) > MAX_DETAIL_BYTES:
        raise ProviderFallbackValidationError("detail is too long")
    if any(ord(character) < 0x20 or ord(character) == 0x7F or _is_noncharacter(ord(character)) for character in value):
        raise ProviderFallbackValidationError(
            "detail contains unsafe characters",
        )


def _safe_detail(value):
    """Return a bounded printable ASCII diagnostic without raw exception text."""
    text = "".join(character if 0x20 <= ord(character) < 0x7F else "?" for character in str(value))
    encoded = text.encode("ascii")
    if len(encoded) <= MAX_DETAIL_BYTES:
        return text
    return encoded[:MAX_DETAIL_BYTES].decode("ascii")


def _is_noncharacter(codepoint):
    """Return whether one Unicode scalar is reserved as a noncharacter."""
    return 0xFDD0 <= codepoint <= 0xFDEF or codepoint & 0xFFFF in (0xFFFE, 0xFFFF)
