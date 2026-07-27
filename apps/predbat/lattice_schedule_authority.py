"""Pure v0.4 schedule compilation and durable BatPred authority decisions.

This module deliberately has no connection to ``PredBat.execute_plan`` or to a
live transport.  A later, separately gated integration may adapt
``ScheduleCommand.to_wire`` to the v0.4 protobuf and inject transport callbacks.
"""

# cspell:ignore READBACK readback

import asyncio
import hashlib
import inspect
import json
import math
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


RESULT_APPLIED = "APPLIED"
RESULT_NOT_APPLIED = "NOT_APPLIED"
RESULT_UNKNOWN = "UNKNOWN"
FALLBACK_SAFE = "SAFE"
FALLBACK_BLOCKED = "BLOCKED"
EXECUTION_NATIVE = "NATIVE"
EXECUTION_CONTROLLER_STEPPED = "CONTROLLER_STEPPED"
VERIFICATION_ACCEPTED_ONLY = "ACCEPTED_ONLY"
VERIFICATION_DURABLE_STORE_READBACK = "DURABLE_STORE_READBACK"
VERIFICATION_NATIVE_TARGET_READBACK = "NATIVE_TARGET_READBACK"
MAX_SCHEDULE_SLOTS = 8
_STATE_PENDING = "PENDING"
_STORAGE_MODULE = "lattice_schedule_authority"
_SCHEMA_VERSION = 1
_MAX_EPOCH_MS = (1 << 53) - 1
_MAX_UINT32 = (1 << 32) - 1
_MAX_UINT64 = (1 << 64) - 1
_LOAD_FAILED = object()
_SCHEDULE_FIELDS = (
    "TARGET_SOC",
    "RESERVE_SOC",
    "CHARGE_POWER_LIMIT",
    "DISCHARGE_POWER_LIMIT",
)
_SLOT_FIELDS = (
    "target_soc",
    "reserve_soc",
    "charge_power_limit",
    "discharge_power_limit",
    "enable",
)
_REJECTION_REASONS = (
    "MALFORMED",
    "STALE_DOCUMENT",
    "UNSUPPORTED_OFFER",
    "INVALID_SCHEDULE",
    "UNSUPPORTED_MODE",
    "AUTHENTICATED_ORIGIN_MISMATCH",
    "LEASE_EXPIRED",
    "STALE_FENCE",
    "LEASE_CONFLICT",
    "SCOPE_MISMATCH",
    "SCOPE_QUARANTINED",
    "EXECUTION_FAILED",
    "EXECUTION_AMBIGUOUS",
    "VERIFICATION_FAILED",
    "INTERNAL",
    "SCOPE_BUSY",
)
_COMMAND_IDENTITY_FIELDS = (
    "plan_digest",
    "slot_count",
    "valid_from_ts_ms",
    "valid_until_ts_ms",
    "fencing_token",
    "lease_id",
    "lease_expires_ts_ms",
    "origin_id",
    "controller_class",
    "doc_version",
    "cap_ref",
    "node_id",
    "altitude_id",
    "owned_node_ids",
    "offer_fingerprint",
)


class ScheduleValidationError(ValueError):
    """Reject a plan that cannot be represented as one safe v0.4 schedule."""


@dataclass(frozen=True)
class PlanWindow:
    """One normalized segment of a complete plan at one topology altitude."""

    altitude_id: str
    start_minute: int
    end_minute: int
    mode: str
    target_soc: Optional[float] = None
    reserve_soc: Optional[float] = None
    charge_power_limit: Optional[float] = None
    discharge_power_limit: Optional[float] = None
    enable: Optional[bool] = None


@dataclass(frozen=True)
class CompletePlan:
    """A complete UTC timeline whose uncovered intervals use ``default_mode``."""

    altitude_id: str
    timeline_start_utc: datetime
    horizon_minutes: int
    diagnostic_timezone: str
    windows: tuple[PlanWindow, ...]
    default_mode: Optional[str] = "self_use"
    execution: str = EXECUTION_CONTROLLER_STEPPED
    is_complete: bool = True


@dataclass(frozen=True)
class AbsoluteScheduleSlot:
    """One half-open v0.4 schedule slot expressed as UTC epoch milliseconds."""

    start_ts_ms: int
    end_ts_ms: int
    mode: str
    target_soc: Optional[float] = None
    reserve_soc: Optional[float] = None
    charge_power_limit: Optional[float] = None
    discharge_power_limit: Optional[float] = None
    enable: Optional[bool] = None

    def to_wire(self):
        """Return the v0.4 field names while preserving explicit zero and false."""
        value = {
            "start_ts_ms": self.start_ts_ms,
            "end_ts_ms": self.end_ts_ms,
            "mode": self.mode,
        }
        for field in ("target_soc", "reserve_soc", "charge_power_limit", "discharge_power_limit", "enable"):
            field_value = getattr(self, field)
            if field_value is not None:
                value[field] = field_value
        return value


@dataclass(frozen=True)
class AbsoluteScheduleIntent:
    """One immutable v0.4 absolute schedule bound to a compiler altitude."""

    altitude_id: str
    slots: tuple[AbsoluteScheduleSlot, ...]
    valid_from_ts_ms: int
    valid_until_ts_ms: int
    diagnostic_timezone: str
    execution: str
    default_mode: Optional[str] = None

    def to_wire(self):
        """Return the v0.4 ``absolute_schedule_intent`` object."""
        value = {
            "slots": [slot.to_wire() for slot in self.slots],
            "valid_from_ts_ms": self.valid_from_ts_ms,
            "valid_until_ts_ms": self.valid_until_ts_ms,
            "diagnostic_timezone": self.diagnostic_timezone,
            "execution": self.execution,
        }
        if self.default_mode is not None:
            value["default_mode"] = self.default_mode
        return value

    @property
    def plan_digest(self):
        """Return the canonical SHA-256 digest used by applied receipts."""
        encoded = json.dumps(self.to_wire(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ResolvedScheduleOffer:
    """Immutable constraints from the retained provider-local schedule offer."""

    max_slots: int
    supported_modes: tuple[str, ...]
    slot_fields: tuple[str, ...]
    execution_modes: tuple[str, ...]
    gap_policy: str

    def __post_init__(self):
        """Normalize semantic sets and reject ambiguous duplicate constraints."""
        fields = ("supported_modes", "slot_fields", "execution_modes")
        for field in fields:
            value = getattr(self, field)
            if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
                raise ScheduleValidationError("{} must be an immutable string tuple".format(field))
            if len(set(value)) != len(value):
                raise ScheduleValidationError("{} contains duplicates".format(field))
            object.__setattr__(self, field, tuple(sorted(value)))

    @property
    def fingerprint(self):
        """Return a stable identity for the retained offer constraints."""
        value = {
            "max_slots": self.max_slots,
            "supported_modes": list(self.supported_modes),
            "slot_fields": list(self.slot_fields),
            "execution_modes": list(self.execution_modes),
            "gap_policy": self.gap_policy,
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ScheduleTarget:
    """Provider-local v0.4 schedule offer selected at one topology altitude."""

    provider: str
    node_id: str
    access_path: str
    topology_version: str
    doc_version: int
    cap_ref: int
    altitude_id: str
    scope_id: str
    offer: ResolvedScheduleOffer
    owned_node_ids: tuple[str, ...]

    def __post_init__(self):
        """Normalize and validate the exact provider-local owned target set."""
        if not isinstance(self.owned_node_ids, tuple) or not self.owned_node_ids:
            raise ScheduleValidationError("owned_node_ids must be a non-empty immutable tuple")
        if any(not isinstance(node_id, str) or not node_id or _utf8_length(node_id) > 96 for node_id in self.owned_node_ids):
            raise ScheduleValidationError("owned_node_ids contains an invalid node id")
        if len(set(self.owned_node_ids)) != len(self.owned_node_ids):
            raise ScheduleValidationError("owned_node_ids contains duplicates")
        object.__setattr__(self, "owned_node_ids", tuple(sorted(self.owned_node_ids)))


@dataclass(frozen=True)
class ScheduleLease:
    """Authenticated controller identity and its gateway-issued fence."""

    origin_id: str
    controller_class: str
    lease_id: str
    scope_id: str
    fencing_token: int
    expires_ts_ms: int


@dataclass(frozen=True)
class ScheduleCommand:
    """Protocol-neutral representation of one v0.4 schedule command."""

    command_id: str
    target: ScheduleTarget
    schedule: AbsoluteScheduleIntent
    lease: ScheduleLease

    def to_wire(self):
        """Return the exact v0.4 control-field shape for a protobuf adapter."""
        return {
            "command_id": self.command_id,
            "doc_version": self.target.doc_version,
            "cap_ref": self.target.cap_ref,
            "absolute_schedule_intent": self.schedule.to_wire(),
            "controller": {
                "origin_id": self.lease.origin_id,
                "controller_class": self.lease.controller_class,
            },
            "lease": {
                "lease_id": self.lease.lease_id,
                "scope_id": self.lease.scope_id,
                "fencing_token": self.lease.fencing_token,
                "expires_ts_ms": self.lease.expires_ts_ms,
            },
        }


@dataclass(frozen=True)
class ScheduleClamp:
    """One bounded field clamp reported by the applied-schedule receipt."""

    slot_index: int
    field: str
    requested: float
    applied: float


@dataclass(frozen=True)
class ScheduleAck:
    """Bounded terminal result returned by the gateway schedule ledger."""

    command_id: str
    state: str
    scope_id: str
    fencing_token: int = 0
    lease_id: str = ""
    lease_expires_ts_ms: int = 0
    origin_id: str = ""
    controller_class: str = ""
    plan_digest: str = ""
    accepted_slot_count: int = 0
    durably_accepted: bool = False
    valid_from_ts_ms: int = 0
    valid_until_ts_ms: int = 0
    verification: str = ""
    clamps: tuple[ScheduleClamp, ...] = ()
    clamp_count: int = 0
    clamps_truncated: bool = False
    verified: bool = False
    reason: str = ""
    fallback: str = ""
    detail: str = ""

    def canonical(self):
        """Return stable content used to detect conflicting duplicate results."""
        return (
            self.command_id,
            self.state,
            self.scope_id,
            self.fencing_token,
            self.lease_id,
            self.lease_expires_ts_ms,
            self.origin_id,
            self.controller_class,
            self.plan_digest,
            self.accepted_slot_count,
            self.durably_accepted,
            self.valid_from_ts_ms,
            self.valid_until_ts_ms,
            self.verification,
            self.clamps,
            self.clamp_count,
            self.clamps_truncated,
            self.verified,
            self.reason,
            self.fallback,
            self.detail,
        )


@dataclass(frozen=True)
class AuthorityDecision:
    """Safety decision exposed to a future gated legacy/Lattice selector."""

    state: str
    command_id: str = ""
    legacy_suppressed: bool = False
    fallback_allowed: bool = False
    quarantined: bool = False
    detail: str = ""


def _is_int(value):
    """Return true only for an integer that is not a boolean."""
    return isinstance(value, int) and not isinstance(value, bool)


def _utf8_length(value):
    """Return the encoded byte length used by the embedded profile."""
    return len(value.encode("utf-8"))


def _validate_text(value, label, max_bytes):
    """Validate one required bounded UTF-8 string."""
    if not isinstance(value, str) or not value:
        raise ScheduleValidationError("{} is required".format(label))
    if _utf8_length(value) > max_bytes:
        raise ScheduleValidationError("{} exceeds {} UTF-8 bytes".format(label, max_bytes))


def _epoch_ms(value, label):
    """Convert an aware UTC datetime to one positive safe epoch millisecond."""
    timezone_key = getattr(getattr(value, "tzinfo", None), "key", None)
    is_utc_zone = getattr(value, "tzinfo", None) is timezone.utc or timezone_key in ("UTC", "Etc/UTC")
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0) or not is_utc_zone:
        raise ScheduleValidationError("{} must be a timezone-aware UTC datetime".format(label))
    try:
        milliseconds = int(value.timestamp() * 1000)
    except (OverflowError, OSError, ValueError) as exc:
        raise ScheduleValidationError("{} is outside the supported epoch range".format(label)) from exc
    if milliseconds <= 0 or milliseconds > _MAX_EPOCH_MS:
        raise ScheduleValidationError("{} must be a positive safe epoch millisecond".format(label))
    return milliseconds


def _validate_timezone(value):
    """Validate an IANA diagnostic timezone without using it for conversion."""
    _validate_text(value, "diagnostic_timezone", 47)
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleValidationError("diagnostic_timezone must be an IANA timezone name") from exc


def _validate_optional_number(value, label, minimum=None, maximum=None):
    """Validate an optional finite numeric schedule refinement."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ScheduleValidationError("{} must be finite".format(label))
    if minimum is not None and value < minimum:
        raise ScheduleValidationError("{} is below {}".format(label, minimum))
    if maximum is not None and value > maximum:
        raise ScheduleValidationError("{} is above {}".format(label, maximum))


def _same_slot_payload(left, right):
    """Return true when adjacent slots can be losslessly coalesced."""
    return (
        left.mode,
        left.target_soc,
        left.reserve_soc,
        left.charge_power_limit,
        left.discharge_power_limit,
        left.enable,
    ) == (
        right.mode,
        right.target_soc,
        right.reserve_soc,
        right.charge_power_limit,
        right.discharge_power_limit,
        right.enable,
    )


def compile_absolute_schedule(plan, now_utc):
    """Compile a complete plan into one ordered v0.4 UTC schedule.

    Window offsets are elapsed minutes from ``timeline_start_utc``.  The
    diagnostic timezone is validated but never used to reinterpret instants, so
    midnight and DST gap/fold transitions cannot collapse distinct slots.
    """
    if not isinstance(plan, CompletePlan):
        raise ScheduleValidationError("plan must be CompletePlan")
    if plan.is_complete is not True:
        raise ScheduleValidationError("partial plans cannot become an atomic schedule")
    _validate_text(plan.altitude_id, "altitude_id", 96)
    _validate_timezone(plan.diagnostic_timezone)
    if plan.execution not in (EXECUTION_NATIVE, EXECUTION_CONTROLLER_STEPPED):
        raise ScheduleValidationError("execution must be NATIVE or CONTROLLER_STEPPED")
    if not _is_int(plan.horizon_minutes) or plan.horizon_minutes <= 0:
        raise ScheduleValidationError("horizon_minutes must be a positive integer")
    if plan.default_mode is not None:
        _validate_text(plan.default_mode, "default_mode", 24)

    valid_from_ts_ms = _epoch_ms(plan.timeline_start_utc, "timeline_start_utc")
    try:
        valid_until = plan.timeline_start_utc + timedelta(minutes=plan.horizon_minutes)
    except (OverflowError, ValueError) as exc:
        raise ScheduleValidationError("timeline end is outside the supported epoch range") from exc
    valid_until_ts_ms = _epoch_ms(valid_until, "timeline end")
    now_ts_ms = _epoch_ms(now_utc, "now_utc")
    if valid_until_ts_ms <= now_ts_ms:
        raise ScheduleValidationError("plan validity has expired")

    if not isinstance(plan.windows, (tuple, list)):
        raise ScheduleValidationError("windows must be a sequence")
    slots = []
    for index, window in enumerate(plan.windows):
        if not isinstance(window, PlanWindow):
            raise ScheduleValidationError("window {} must be PlanWindow".format(index))
        if window.altitude_id != plan.altitude_id:
            raise ScheduleValidationError("window {} crosses topology altitude".format(index))
        if not _is_int(window.start_minute) or not _is_int(window.end_minute):
            raise ScheduleValidationError("window {} offsets must be integers".format(index))
        if window.start_minute < 0 or window.start_minute >= window.end_minute or window.end_minute > plan.horizon_minutes:
            raise ScheduleValidationError("window {} is outside the non-empty plan horizon".format(index))
        _validate_text(window.mode, "window {} mode".format(index), 24)
        _validate_optional_number(window.target_soc, "window {} target_soc".format(index), 0, 100)
        _validate_optional_number(window.reserve_soc, "window {} reserve_soc".format(index), 0, 100)
        _validate_optional_number(window.charge_power_limit, "window {} charge_power_limit".format(index), 0)
        _validate_optional_number(window.discharge_power_limit, "window {} discharge_power_limit".format(index), 0)
        if window.enable is not None and not isinstance(window.enable, bool):
            raise ScheduleValidationError("window {} enable must be boolean".format(index))

        try:
            start = plan.timeline_start_utc + timedelta(minutes=window.start_minute)
            end = plan.timeline_start_utc + timedelta(minutes=window.end_minute)
        except (OverflowError, ValueError) as exc:
            raise ScheduleValidationError("window {} is outside the supported epoch range".format(index)) from exc
        slots.append(
            AbsoluteScheduleSlot(
                _epoch_ms(start, "window {} start".format(index)),
                _epoch_ms(end, "window {} end".format(index)),
                window.mode,
                window.target_soc,
                window.reserve_soc,
                window.charge_power_limit,
                window.discharge_power_limit,
                window.enable,
            )
        )

    slots.sort(key=lambda item: (item.start_ts_ms, item.end_ts_ms))
    coalesced = []
    has_gap = False
    previous_end = valid_from_ts_ms
    for index, slot in enumerate(slots):
        if slot.start_ts_ms < previous_end:
            raise ScheduleValidationError("window {} overlaps another plan window".format(index))
        if slot.start_ts_ms > previous_end:
            has_gap = True
        if coalesced and coalesced[-1].end_ts_ms == slot.start_ts_ms and _same_slot_payload(coalesced[-1], slot):
            coalesced[-1] = replace(coalesced[-1], end_ts_ms=slot.end_ts_ms)
        else:
            coalesced.append(slot)
        previous_end = slot.end_ts_ms
    if previous_end < valid_until_ts_ms or not coalesced:
        has_gap = True
    if has_gap and plan.default_mode is None:
        raise ScheduleValidationError("schedule gaps require default_mode")
    if len(coalesced) > MAX_SCHEDULE_SLOTS:
        raise ScheduleValidationError("schedule has {} slots; embedded maximum is {}".format(len(coalesced), MAX_SCHEDULE_SLOTS))

    return AbsoluteScheduleIntent(
        altitude_id=plan.altitude_id,
        slots=tuple(coalesced),
        valid_from_ts_ms=valid_from_ts_ms,
        valid_until_ts_ms=valid_until_ts_ms,
        diagnostic_timezone=plan.diagnostic_timezone,
        execution=plan.execution,
        default_mode=plan.default_mode if has_gap else None,
    )


def _valid_schedule_intent(schedule, now_ts_ms):
    """Return whether a supplied intent satisfies the compiler's safe subset."""
    if not isinstance(schedule, AbsoluteScheduleIntent):
        return False
    try:
        _validate_text(schedule.altitude_id, "altitude_id", 96)
        _validate_timezone(schedule.diagnostic_timezone)
        if schedule.execution not in (EXECUTION_NATIVE, EXECUTION_CONTROLLER_STEPPED):
            return False
        if schedule.default_mode is not None:
            _validate_text(schedule.default_mode, "default_mode", 24)
    except ScheduleValidationError:
        return False
    if (
        not _is_int(schedule.valid_from_ts_ms)
        or not _is_int(schedule.valid_until_ts_ms)
        or schedule.valid_from_ts_ms <= 0
        or schedule.valid_from_ts_ms >= schedule.valid_until_ts_ms
        or schedule.valid_until_ts_ms > _MAX_EPOCH_MS
        or schedule.valid_until_ts_ms <= now_ts_ms
        or not isinstance(schedule.slots, tuple)
        or len(schedule.slots) > MAX_SCHEDULE_SLOTS
    ):
        return False

    previous_end = schedule.valid_from_ts_ms
    has_gap = not schedule.slots
    for slot in schedule.slots:
        if not isinstance(slot, AbsoluteScheduleSlot):
            return False
        try:
            _validate_text(slot.mode, "slot mode", 24)
            _validate_optional_number(slot.target_soc, "slot target_soc", 0, 100)
            _validate_optional_number(slot.reserve_soc, "slot reserve_soc", 0, 100)
            _validate_optional_number(slot.charge_power_limit, "slot charge_power_limit", 0)
            _validate_optional_number(slot.discharge_power_limit, "slot discharge_power_limit", 0)
        except ScheduleValidationError:
            return False
        if slot.enable is not None and not isinstance(slot.enable, bool):
            return False
        if not _is_int(slot.start_ts_ms) or not _is_int(slot.end_ts_ms) or slot.start_ts_ms < schedule.valid_from_ts_ms or slot.start_ts_ms >= slot.end_ts_ms or slot.end_ts_ms > schedule.valid_until_ts_ms or slot.start_ts_ms < previous_end:
            return False
        if slot.start_ts_ms > previous_end:
            has_gap = True
        previous_end = slot.end_ts_ms
    if previous_end < schedule.valid_until_ts_ms:
        has_gap = True
    return not has_gap or schedule.default_mode is not None


def _valid_resolved_offer(offer):
    """Return whether retained offer constraints match the v0.4 safe subset."""
    if (
        not isinstance(offer, ResolvedScheduleOffer)
        or not _is_int(offer.max_slots)
        or not 1 <= offer.max_slots <= MAX_SCHEDULE_SLOTS
        or not isinstance(offer.supported_modes, tuple)
        or not offer.supported_modes
        or any(not isinstance(mode, str) for mode in offer.supported_modes)
        or len(set(offer.supported_modes)) != len(offer.supported_modes)
        or not isinstance(offer.slot_fields, tuple)
        or any(not isinstance(field, str) for field in offer.slot_fields)
        or len(set(offer.slot_fields)) != len(offer.slot_fields)
        or any(field not in _SLOT_FIELDS for field in offer.slot_fields)
        or not isinstance(offer.execution_modes, tuple)
        or not offer.execution_modes
        or any(not isinstance(execution, str) for execution in offer.execution_modes)
        or len(set(offer.execution_modes)) != len(offer.execution_modes)
        or any(execution not in (EXECUTION_NATIVE, EXECUTION_CONTROLLER_STEPPED) for execution in offer.execution_modes)
        or offer.gap_policy not in ("DEFAULT_MODE", "REJECT")
    ):
        return False
    try:
        for mode in offer.supported_modes:
            _validate_text(mode, "supported mode", 24)
    except ScheduleValidationError:
        return False
    return True


def _offer_supports_schedule(offer, schedule):
    """Check the complete intent against its retained provider-local offer."""
    if not _valid_resolved_offer(offer) or len(schedule.slots) > offer.max_slots or schedule.execution not in offer.execution_modes:
        return False
    supported_modes = set(offer.supported_modes)
    if schedule.default_mode is not None and schedule.default_mode not in supported_modes:
        return False
    previous_end = schedule.valid_from_ts_ms
    has_gap = not schedule.slots
    allowed_fields = set(offer.slot_fields)
    for slot in schedule.slots:
        if slot.mode not in supported_modes:
            return False
        if slot.start_ts_ms > previous_end:
            has_gap = True
        previous_end = slot.end_ts_ms
        for field in _SLOT_FIELDS:
            if getattr(slot, field) is not None and field not in allowed_fields:
                return False
    if previous_end < schedule.valid_until_ts_ms:
        has_gap = True
    if offer.gap_policy == "REJECT" and has_gap:
        return False
    if offer.gap_policy == "DEFAULT_MODE" and has_gap and schedule.default_mode is None:
        return False
    return True


def _valid_target(target, schedule, scope_id):
    """Return whether provider-local coordinates safely bind this schedule."""
    return (
        isinstance(target, ScheduleTarget)
        and target.provider == "predbat-gateway"
        and target.access_path == "gw-local"
        and target.topology_version == "0.4.0"
        and _is_int(target.doc_version)
        and 0 < target.doc_version <= _MAX_UINT32
        and _is_int(target.cap_ref)
        and 0 < target.cap_ref <= _MAX_UINT32
        and isinstance(target.node_id, str)
        and bool(target.node_id)
        and target.node_id in target.owned_node_ids
        and target.altitude_id == schedule.altitude_id
        and target.scope_id == scope_id
        and _offer_supports_schedule(target.offer, schedule)
    )


def _valid_lease(lease, scope_id, now_ts_ms):
    """Return whether the controller lease is well-formed and currently live."""
    return (
        isinstance(lease, ScheduleLease)
        and isinstance(lease.origin_id, str)
        and 0 < _utf8_length(lease.origin_id) <= 36
        and lease.controller_class in ("AUTOMATION", "MANUAL", "SAFETY", "GRID")
        and isinstance(lease.lease_id, str)
        and 0 < _utf8_length(lease.lease_id) <= 36
        and lease.scope_id == scope_id
        and _is_int(lease.fencing_token)
        and 0 < lease.fencing_token <= _MAX_UINT64
        and _is_int(lease.expires_ts_ms)
        and lease.expires_ts_ms > now_ts_ms
        and lease.expires_ts_ms <= _MAX_EPOCH_MS
    )


def _record_for(command):
    """Build the durable pre-dispatch reservation for one command."""
    return {
        "command_id": command.command_id,
        "state": _STATE_PENDING,
        "plan_digest": command.schedule.plan_digest,
        "slot_count": len(command.schedule.slots),
        "valid_from_ts_ms": command.schedule.valid_from_ts_ms,
        "valid_until_ts_ms": command.schedule.valid_until_ts_ms,
        "fencing_token": command.lease.fencing_token,
        "lease_id": command.lease.lease_id,
        "lease_expires_ts_ms": command.lease.expires_ts_ms,
        "origin_id": command.lease.origin_id,
        "controller_class": command.lease.controller_class,
        "doc_version": command.target.doc_version,
        "cap_ref": command.target.cap_ref,
        "node_id": command.target.node_id,
        "altitude_id": command.target.altitude_id,
        "owned_node_ids": list(command.target.owned_node_ids),
        "offer_fingerprint": command.target.offer.fingerprint,
        "applied_plan_digest": "",
        "verification": "",
        "clamps": [],
        "clamp_count": 0,
        "clamps_truncated": False,
        "fallback": "",
        "reason": "",
        "durably_accepted": False,
        "verified": False,
    }


def _valid_digest(value):
    """Return whether a receipt digest is exactly 32-byte lowercase hex."""
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _valid_clamp_values(slot_index, field, requested, applied, slot_count):
    """Validate one clamp against the bounded applied-schedule receipt."""
    return (
        _is_int(slot_index)
        and 0 <= slot_index < slot_count
        and field in _SCHEDULE_FIELDS
        and not isinstance(requested, bool)
        and isinstance(requested, (int, float))
        and math.isfinite(requested)
        and not isinstance(applied, bool)
        and isinstance(applied, (int, float))
        and math.isfinite(applied)
    )


def _valid_ack_receipt(ack, expected):
    """Validate the complete bounded v0.4 applied-schedule receipt."""
    if (
        not _valid_digest(ack.plan_digest)
        or not _is_int(ack.accepted_slot_count)
        or ack.accepted_slot_count != expected["slot_count"]
        or ack.durably_accepted is not True
        or not isinstance(ack.verified, bool)
        or ack.valid_from_ts_ms != expected["valid_from_ts_ms"]
        or ack.valid_until_ts_ms != expected["valid_until_ts_ms"]
        or ack.verification not in (VERIFICATION_ACCEPTED_ONLY, VERIFICATION_DURABLE_STORE_READBACK, VERIFICATION_NATIVE_TARGET_READBACK)
        or not isinstance(ack.clamps, tuple)
        or len(ack.clamps) > MAX_SCHEDULE_SLOTS
        or not _is_int(ack.clamp_count)
        or ack.clamp_count < len(ack.clamps)
        or not isinstance(ack.clamps_truncated, bool)
        or ack.clamps_truncated is not (ack.clamp_count > len(ack.clamps))
    ):
        return False
    has_readback = ack.verification in (VERIFICATION_DURABLE_STORE_READBACK, VERIFICATION_NATIVE_TARGET_READBACK)
    if ack.verified is not has_readback:
        return False
    return all(isinstance(clamp, ScheduleClamp) and _valid_clamp_values(clamp.slot_index, clamp.field, clamp.requested, clamp.applied, ack.accepted_slot_count) for clamp in ack.clamps)


def _valid_stored_receipt(record):
    """Validate a JSON-compatible durable copy of an applied receipt."""
    verification = record.get("verification")
    clamps = record.get("clamps")
    clamp_count = record.get("clamp_count")
    clamps_truncated = record.get("clamps_truncated")
    if (
        not _valid_digest(record.get("applied_plan_digest"))
        or verification not in (VERIFICATION_ACCEPTED_ONLY, VERIFICATION_DURABLE_STORE_READBACK, VERIFICATION_NATIVE_TARGET_READBACK)
        or not isinstance(clamps, list)
        or len(clamps) > MAX_SCHEDULE_SLOTS
        or not _is_int(clamp_count)
        or clamp_count < len(clamps)
        or not isinstance(clamps_truncated, bool)
        or clamps_truncated is not (clamp_count > len(clamps))
    ):
        return False
    has_readback = verification in (VERIFICATION_DURABLE_STORE_READBACK, VERIFICATION_NATIVE_TARGET_READBACK)
    if record["verified"] is not has_readback:
        return False
    for clamp in clamps:
        if not isinstance(clamp, dict) or set(clamp) != {"slot_index", "field", "requested", "applied"}:
            return False
        if not _valid_clamp_values(clamp["slot_index"], clamp["field"], clamp["requested"], clamp["applied"], record["slot_count"]):
            return False
    return True


def _valid_command_record(record):
    """Reject corrupt durable command state before it can influence a writer."""
    if not isinstance(record, dict):
        return False
    if not isinstance(record.get("command_id"), str) or not record["command_id"]:
        return False
    if record.get("state") not in (_STATE_PENDING, RESULT_APPLIED, RESULT_NOT_APPLIED, RESULT_UNKNOWN):
        return False
    if not _valid_digest(record.get("plan_digest")):
        return False
    if not _valid_digest(record.get("offer_fingerprint")):
        return False
    if not _is_int(record.get("slot_count")) or record["slot_count"] < 0:
        return False
    for field in ("valid_from_ts_ms", "valid_until_ts_ms", "fencing_token", "lease_expires_ts_ms", "doc_version", "cap_ref"):
        if not _is_int(record.get(field)) or record[field] <= 0:
            return False
    if record["valid_until_ts_ms"] > _MAX_EPOCH_MS or record["lease_expires_ts_ms"] > _MAX_EPOCH_MS or record["fencing_token"] > _MAX_UINT64:
        return False
    if record["doc_version"] > _MAX_UINT32 or record["cap_ref"] > _MAX_UINT32:
        return False
    if record["slot_count"] > MAX_SCHEDULE_SLOTS or record["valid_from_ts_ms"] >= record["valid_until_ts_ms"]:
        return False
    for field in ("lease_id", "origin_id", "controller_class", "node_id", "altitude_id"):
        if not isinstance(record.get(field), str) or not record[field]:
            return False
    owned_node_ids = record.get("owned_node_ids")
    if (
        not isinstance(owned_node_ids, list)
        or not owned_node_ids
        or any(not isinstance(node_id, str) or not node_id or _utf8_length(node_id) > 96 for node_id in owned_node_ids)
        or owned_node_ids != sorted(set(owned_node_ids))
        or record["node_id"] not in owned_node_ids
    ):
        return False
    if record.get("fallback") not in ("", FALLBACK_SAFE, FALLBACK_BLOCKED):
        return False
    if not isinstance(record.get("reason"), str):
        return False
    if not isinstance(record.get("durably_accepted"), bool) or not isinstance(record.get("verified"), bool):
        return False
    if record["state"] == RESULT_APPLIED:
        return record["durably_accepted"] and record["fallback"] == "" and record["reason"] == "" and _valid_stored_receipt(record)
    if record.get("applied_plan_digest") != "" or record.get("verification") != "" or record.get("clamps") != [] or record.get("clamp_count") != 0 or record.get("clamps_truncated") is not False:
        return False
    if record["state"] == RESULT_NOT_APPLIED:
        return not record["durably_accepted"] and record["fallback"] in (FALLBACK_SAFE, FALLBACK_BLOCKED) and bool(record["reason"])
    if record["state"] == RESULT_UNKNOWN:
        return not record["durably_accepted"] and record["fallback"] == FALLBACK_BLOCKED and record["reason"] == "EXECUTION_AMBIGUOUS"
    return not record["durably_accepted"] and record["fallback"] == "" and record["reason"] == ""


def _valid_snapshot(snapshot, scope_id):
    """Return whether the complete persisted scope snapshot is internally valid."""
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != _SCHEMA_VERSION or snapshot.get("scope_id") != scope_id:
        return False
    high_water = snapshot.get("high_water_token")
    if not _is_int(high_water) or high_water < 0 or high_water > _MAX_UINT64:
        return False
    command = snapshot.get("command")
    installed = snapshot.get("installed")
    if installed is not None and (not _valid_command_record(installed) or installed["state"] != RESULT_APPLIED or installed["fencing_token"] > high_water):
        return False
    if command is None:
        return installed is None
    return _valid_command_record(command) and command["fencing_token"] <= high_water


def _same_command_identity(left, right):
    """Return whether two durable records name the same exact command."""
    return all(left[field] == right[field] for field in _COMMAND_IDENTITY_FIELDS)


def _decision_from_record(record, now_ts_ms=None):
    """Derive legacy suppression and fallback from one validated record."""
    state = record["state"]
    if state == RESULT_APPLIED:
        if now_ts_ms is not None and record["valid_until_ts_ms"] <= now_ts_ms:
            return AuthorityDecision(RESULT_NOT_APPLIED, record["command_id"], detail="durable Lattice schedule validity has expired")
        return AuthorityDecision(state, record["command_id"], legacy_suppressed=True)
    if state == RESULT_NOT_APPLIED:
        return AuthorityDecision(state, record["command_id"], fallback_allowed=record["fallback"] == FALLBACK_SAFE, detail=record["reason"])
    return AuthorityDecision(RESULT_UNKNOWN, record["command_id"], quarantined=True, detail=record.get("reason") or "command result unresolved")


def _decision_from_snapshot(snapshot, now_ts_ms):
    """Preserve suppression while a previously installed schedule may act."""
    record = snapshot.get("command")
    decision = AuthorityDecision(RESULT_NOT_APPLIED, detail="no durable Lattice acceptance") if record is None else _decision_from_record(record, now_ts_ms)
    installed = snapshot.get("installed")
    if installed is not None and installed["valid_until_ts_ms"] > now_ts_ms:
        return replace(decision, legacy_suppressed=True, fallback_allowed=False)
    return decision


class LatticeScheduleAuthority:
    """Serialize and durably reconcile one default-off schedule-control scope.

    ``send`` and ``query_result`` are injected seams.  They may return a
    ``ScheduleAck`` directly or deliver it synchronously through
    :meth:`accept_result`.  The class never invokes a legacy or device writer.
    """

    def __init__(self, storage, send, query_result, scope_id, storage_key="schedule", command_id_factory=None):
        """Create one single-process authority instance for a canonical scope."""
        _validate_text(scope_id, "scope_id", 47)
        _validate_text(storage_key, "storage_key", 96)
        self._storage = storage
        self._send = send
        self._query_result = query_result
        self._scope_id = scope_id
        self._storage_key = storage_key
        self._command_id_factory = command_id_factory or (lambda: str(uuid.uuid4()))
        self._lock = None
        self._lock_loop = None
        self._expected = None
        self._accepted_result = None
        self._conflicting_result = False

    def _current_lock(self):
        """Return a lock bound to the active event loop.

        PredBat may construct components before its async runner exists.  Tests
        and lifecycle restarts may also use a new loop after the old one has
        closed, so an idle lock is safely rebound when the loop changes.
        """
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            if self._lock is not None and self._lock.locked():
                raise RuntimeError("schedule authority cannot move an active lock between event loops")
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    async def _load(self):
        """Load this scope's durable snapshot."""
        if self._storage is None:
            return _LOAD_FAILED
        try:
            return await self._storage.load(_STORAGE_MODULE, self._storage_key)
        except Exception:
            return _LOAD_FAILED

    async def _save(self, snapshot):
        """Persist a full scope transition before exposing its consequence."""
        if self._storage is None:
            return False
        try:
            return bool(await self._storage.save(_STORAGE_MODULE, self._storage_key, snapshot, format="json", expiry=None))
        except Exception:
            return False

    @staticmethod
    async def _invoke(callback, *args):
        """Invoke a synchronous or asynchronous injected seam."""
        value = callback(*args)
        if inspect.isawaitable(value):
            return await value
        return value

    def _matches_expected(self, ack, expected):
        """Validate exact correlation and v0.4 result-state invariants."""
        if not isinstance(ack, ScheduleAck):
            return False
        if ack.command_id != expected["command_id"] or ack.scope_id != self._scope_id:
            return False
        if ack.fencing_token != 0 and (not _is_int(ack.fencing_token) or not 0 < ack.fencing_token <= _MAX_UINT64):
            return False
        if ack.lease_expires_ts_ms != 0 and (not _is_int(ack.lease_expires_ts_ms) or not 0 < ack.lease_expires_ts_ms <= _MAX_EPOCH_MS):
            return False
        if any(not isinstance(value, str) for value in (ack.lease_id, ack.origin_id, ack.controller_class)):
            return False
        optional_coordinates = (
            (ack.fencing_token, 0, expected["fencing_token"]),
            (ack.lease_id, "", expected["lease_id"]),
            (ack.lease_expires_ts_ms, 0, expected["lease_expires_ts_ms"]),
            (ack.origin_id, "", expected["origin_id"]),
            (ack.controller_class, "", expected["controller_class"]),
        )
        if any(value != absent and value != wanted for value, absent, wanted in optional_coordinates):
            return False
        if ack.state == RESULT_APPLIED:
            return (
                ack.fencing_token == expected["fencing_token"]
                and ack.lease_id == expected["lease_id"]
                and ack.lease_expires_ts_ms == expected["lease_expires_ts_ms"]
                and ack.origin_id == expected["origin_id"]
                and ack.controller_class == expected["controller_class"]
                and _valid_ack_receipt(ack, expected)
                and ack.fallback == ""
                and ack.reason == ""
                and ack.detail == ""
            )
        receipt_absent = (
            ack.plan_digest == ""
            and ack.accepted_slot_count == 0
            and ack.durably_accepted is False
            and ack.valid_from_ts_ms == 0
            and ack.valid_until_ts_ms == 0
            and ack.verification == ""
            and ack.clamps == ()
            and ack.clamp_count == 0
            and ack.clamps_truncated is False
            and ack.verified is False
        )
        rejection_valid = receipt_absent and ack.reason in _REJECTION_REASONS and ack.fallback in (FALLBACK_SAFE, FALLBACK_BLOCKED) and isinstance(ack.detail, str) and 0 < _utf8_length(ack.detail) <= 96
        blocked_reasons = (
            "AUTHENTICATED_ORIGIN_MISMATCH",
            "LEASE_EXPIRED",
            "STALE_FENCE",
            "LEASE_CONFLICT",
            "SCOPE_MISMATCH",
            "SCOPE_QUARANTINED",
            "SCOPE_BUSY",
            "EXECUTION_AMBIGUOUS",
            "INTERNAL",
        )
        if ack.state == RESULT_NOT_APPLIED:
            return rejection_valid and (ack.reason not in blocked_reasons or ack.fallback == FALLBACK_BLOCKED)
        if ack.state == RESULT_UNKNOWN:
            return rejection_valid and ack.reason == "EXECUTION_AMBIGUOUS" and ack.fallback == FALLBACK_BLOCKED
        return False

    def accept_result(self, ack):
        """Accept an exact active result, tolerating only identical duplicates."""
        expected = self._expected
        if expected is None or not isinstance(ack, ScheduleAck) or ack.command_id != expected["command_id"]:
            return False
        if not self._matches_expected(ack, expected):
            self._conflicting_result = True
            return False
        if self._accepted_result is None:
            self._accepted_result = ack
            return True
        if self._accepted_result.canonical() == ack.canonical():
            return True
        self._conflicting_result = True
        return False

    def _begin_correlation(self, expected):
        """Arm exact result correlation before invoking a transport seam."""
        self._expected = expected
        self._accepted_result = None
        self._conflicting_result = False

    def _end_correlation(self):
        """Return the correlated result and clear transient correlation state."""
        result = None if self._conflicting_result else self._accepted_result
        conflict = self._conflicting_result
        self._expected = None
        self._accepted_result = None
        self._conflicting_result = False
        return result, conflict

    async def _exchange(self, callback, argument, expected):
        """Run one transport action with correlation armed before invocation."""
        self._begin_correlation(expected)
        try:
            returned = await self._invoke(callback, argument)
            if returned is not None:
                self.accept_result(returned)
        except Exception:
            returned = None
        result, conflict = self._end_correlation()
        return result, conflict

    async def _persist_terminal(self, snapshot, ack, now_ts_ms):
        """Persist a validated terminal ACK and return its safe decision."""
        previous_snapshot = snapshot
        record = dict(snapshot["command"])
        record.update(
            {
                "state": ack.state,
                "applied_plan_digest": ack.plan_digest,
                "verification": ack.verification,
                "clamps": [
                    {
                        "slot_index": clamp.slot_index,
                        "field": clamp.field,
                        "requested": clamp.requested,
                        "applied": clamp.applied,
                    }
                    for clamp in ack.clamps
                ],
                "clamp_count": ack.clamp_count,
                "clamps_truncated": ack.clamps_truncated,
                "fallback": ack.fallback,
                "reason": ack.reason,
                "durably_accepted": bool(ack.durably_accepted),
                "verified": bool(ack.verified),
            }
        )
        snapshot = dict(snapshot)
        snapshot["command"] = record
        if ack.state == RESULT_APPLIED:
            snapshot["installed"] = dict(record)
        if not await self._save(snapshot):
            return replace(
                _decision_from_snapshot(previous_snapshot, now_ts_ms),
                state=RESULT_UNKNOWN,
                command_id=record["command_id"],
                fallback_allowed=False,
                quarantined=True,
                detail="terminal result persistence failed",
            )
        return _decision_from_snapshot(snapshot, now_ts_ms)

    async def _mark_unknown(self, snapshot, detail, now_ts_ms):
        """Durably quarantine an ambiguous command whenever storage permits."""
        record = dict(snapshot["command"])
        record.update(
            {
                "state": RESULT_UNKNOWN,
                "fallback": FALLBACK_BLOCKED,
                "reason": "EXECUTION_AMBIGUOUS",
                "durably_accepted": False,
                "verified": False,
            }
        )
        updated = dict(snapshot)
        updated["command"] = record
        await self._save(updated)
        return replace(
            _decision_from_snapshot(updated, now_ts_ms),
            state=RESULT_UNKNOWN,
            command_id=record["command_id"],
            quarantined=True,
            detail=detail,
        )

    async def _reconcile_snapshot(self, snapshot, now_ts_ms):
        """Query the gateway ledger by command ID without replaying the command."""
        record = snapshot["command"]
        result, conflict = await self._exchange(self._query_result, record["command_id"], record)
        if conflict:
            return await self._mark_unknown(snapshot, "conflicting reconciliation result", now_ts_ms)
        if result is None:
            return await self._mark_unknown(snapshot, "durable result unavailable", now_ts_ms)
        return await self._persist_terminal(snapshot, result, now_ts_ms)

    async def legacy_decision(self, now_ts_ms):
        """Read the durable state used by a future legacy-suppression gate."""
        async with self._current_lock():
            if not _is_int(now_ts_ms) or now_ts_ms <= 0 or now_ts_ms > _MAX_EPOCH_MS:
                return AuthorityDecision(RESULT_UNKNOWN, quarantined=True, detail="now_ts_ms is invalid")
            snapshot = await self._load()
            if snapshot is None:
                return AuthorityDecision(RESULT_NOT_APPLIED, detail="no durable Lattice acceptance")
            if not _valid_snapshot(snapshot, self._scope_id):
                return AuthorityDecision(RESULT_UNKNOWN, quarantined=True, detail="corrupt persisted authority state")
            record = snapshot.get("command")
            if record is None:
                return AuthorityDecision(RESULT_NOT_APPLIED, detail="no durable Lattice acceptance")
            return _decision_from_snapshot(snapshot, now_ts_ms)

    async def reconcile(self, now_ts_ms):
        """Resume a pending/UNKNOWN command after restart without republishing."""
        async with self._current_lock():
            if not _is_int(now_ts_ms) or now_ts_ms <= 0 or now_ts_ms > _MAX_EPOCH_MS:
                return AuthorityDecision(RESULT_UNKNOWN, quarantined=True, detail="now_ts_ms is invalid")
            snapshot = await self._load()
            if snapshot is None:
                return AuthorityDecision(RESULT_NOT_APPLIED, detail="no command to reconcile")
            if not _valid_snapshot(snapshot, self._scope_id):
                return AuthorityDecision(RESULT_UNKNOWN, quarantined=True, detail="corrupt persisted authority state")
            record = snapshot.get("command")
            if record is None:
                return AuthorityDecision(RESULT_NOT_APPLIED, detail="no command to reconcile")
            if record["state"] in (_STATE_PENDING, RESULT_UNKNOWN):
                return await self._reconcile_snapshot(snapshot, now_ts_ms)
            return _decision_from_snapshot(snapshot, now_ts_ms)

    async def dispatch(self, schedule, target, lease, now_ts_ms, command_id=None):
        """Reserve, send, and reconcile one schedule without any legacy write."""
        async with self._current_lock():
            if not _is_int(now_ts_ms) or now_ts_ms <= 0 or now_ts_ms > _MAX_EPOCH_MS:
                return AuthorityDecision(RESULT_UNKNOWN, quarantined=True, detail="now_ts_ms is invalid")
            snapshot = await self._load()
            if snapshot is None:
                snapshot = {
                    "schema_version": _SCHEMA_VERSION,
                    "scope_id": self._scope_id,
                    "high_water_token": 0,
                    "command": None,
                    "installed": None,
                }
            elif not _valid_snapshot(snapshot, self._scope_id):
                return AuthorityDecision(RESULT_UNKNOWN, quarantined=True, detail="corrupt persisted authority state")
            if not _valid_schedule_intent(schedule, now_ts_ms):
                return replace(
                    _decision_from_snapshot(snapshot, now_ts_ms),
                    state=RESULT_NOT_APPLIED,
                    fallback_allowed=False,
                    detail="schedule is invalid",
                )
            if not _valid_target(target, schedule, self._scope_id):
                return replace(
                    _decision_from_snapshot(snapshot, now_ts_ms),
                    state=RESULT_NOT_APPLIED,
                    fallback_allowed=False,
                    detail="unsafe topology schedule target",
                )
            if not _valid_lease(lease, self._scope_id, now_ts_ms):
                return replace(
                    _decision_from_snapshot(snapshot, now_ts_ms),
                    state=RESULT_NOT_APPLIED,
                    fallback_allowed=False,
                    detail="lease is invalid or expired",
                )
            try:
                resolved_command_id = command_id or self._command_id_factory()
            except Exception:
                return replace(
                    _decision_from_snapshot(snapshot, now_ts_ms),
                    state=RESULT_UNKNOWN,
                    fallback_allowed=False,
                    quarantined=True,
                    detail="command_id generation failed",
                )
            if not isinstance(resolved_command_id, str) or not resolved_command_id or _utf8_length(resolved_command_id) > 63:
                return replace(
                    _decision_from_snapshot(snapshot, now_ts_ms),
                    state=RESULT_NOT_APPLIED,
                    fallback_allowed=False,
                    detail="command_id is invalid",
                )
            command = ScheduleCommand(resolved_command_id, target, schedule, lease)
            expected = _record_for(command)

            previous = snapshot.get("command")
            if previous and previous["command_id"] == resolved_command_id:
                if not _same_command_identity(previous, expected):
                    return replace(
                        _decision_from_snapshot(snapshot, now_ts_ms),
                        state=RESULT_UNKNOWN,
                        command_id=resolved_command_id,
                        fallback_allowed=False,
                        quarantined=True,
                        detail="command_id conflicts with durable command",
                    )
            if previous and previous["state"] in (_STATE_PENDING, RESULT_UNKNOWN):
                previous_decision = await self._reconcile_snapshot(snapshot, now_ts_ms)
                if previous_decision.state == RESULT_UNKNOWN:
                    return previous_decision
                snapshot = await self._load()
                if snapshot is None or not _valid_snapshot(snapshot, self._scope_id):
                    return replace(
                        previous_decision,
                        state=RESULT_UNKNOWN,
                        command_id=resolved_command_id,
                        fallback_allowed=False,
                        quarantined=True,
                        detail="reconciled state could not be reloaded",
                    )
                previous = snapshot.get("command")
            if previous and _same_command_identity(previous, expected):
                return _decision_from_snapshot(snapshot, now_ts_ms)
            if lease.fencing_token < snapshot["high_water_token"]:
                return replace(
                    _decision_from_snapshot(snapshot, now_ts_ms),
                    state=RESULT_NOT_APPLIED,
                    command_id=resolved_command_id,
                    fallback_allowed=False,
                    detail="STALE_FENCE",
                )

            previous_snapshot = snapshot
            snapshot = {
                "schema_version": _SCHEMA_VERSION,
                "scope_id": self._scope_id,
                "high_water_token": max(snapshot["high_water_token"], lease.fencing_token),
                "command": expected,
                "installed": snapshot.get("installed"),
            }
            if not await self._save(snapshot):
                return replace(
                    _decision_from_snapshot(previous_snapshot, now_ts_ms),
                    state=RESULT_UNKNOWN,
                    command_id=resolved_command_id,
                    fallback_allowed=False,
                    quarantined=True,
                    detail="command reservation persistence failed",
                )

            result, conflict = await self._exchange(self._send, command, expected)
            if conflict:
                return await self._mark_unknown(snapshot, "conflicting command result", now_ts_ms)
            if result is None:
                unknown = await self._mark_unknown(snapshot, "command result unavailable", now_ts_ms)
                reloaded = await self._load()
                if reloaded is None or not _valid_snapshot(reloaded, self._scope_id):
                    return unknown
                return await self._reconcile_snapshot(reloaded, now_ts_ms)
            return await self._persist_terminal(snapshot, result, now_ts_ms)
