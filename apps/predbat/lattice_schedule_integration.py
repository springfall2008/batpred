"""Default-off integration seam between PredBat plans and Lattice authority.

This module deliberately owns no transport.  An ACTIVE integration must inject
complete, topology-bound target, lease, send, and result-query bindings.  Until
then ACTIVE is fail-closed, while OFF and SHADOW preserve legacy behaviour for
installations which have never armed Lattice schedule control.

ACTIVE must remain disabled until the gateway runtime atomically fences or
retires any retained legacy execution plan before accepting Lattice authority.
This client-side barrier prevents new overlapping writers but cannot prove that
device-side prerequisite by itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable

from const import MINUTE_WATT
from ha import run_async
from lattice_schedule_authority import (
    CompletePlan,
    EXECUTION_NATIVE,
    LatticeScheduleAuthority,
    PlanWindow,
    RESULT_APPLIED,
    RESULT_NOT_APPLIED,
    RESULT_UNKNOWN,
    ScheduleLease,
    ScheduleTarget,
    ScheduleValidationError,
    compile_absolute_schedule,
)
from utils import calc_percent_limit


MODE_OFF = "OFF"
MODE_SHADOW = "SHADOW"
MODE_ACTIVE = "ACTIVE"

_MARKER_MODULE = "lattice_schedule_integration"
_MARKER_KEY = "ever_armed"
_MARKER_SCHEMA = 1
_COORDINATOR_LOCK = threading.Lock()


@dataclass(frozen=True)
class ScheduleRuntimeBindings:
    """Complete runtime dependencies required before ACTIVE may send."""

    scope_id: str
    plan_builder: Callable[[Any], Any]
    resolve_target: Callable[[Any], Any]
    resolve_cancellation_target: Callable[[str, str], Any]
    acquire_lease: Callable[[Any, int], Any]
    send: Callable[[Any], Any]
    query_result: Callable[[str], Any]
    storage: Any = None

    @property
    def complete(self):
        """Return whether every safety-critical binding is present."""
        return (
            isinstance(self.scope_id, str)
            and bool(self.scope_id)
            and all(
                callable(value)
                for value in (
                    self.plan_builder,
                    self.resolve_target,
                    self.resolve_cancellation_target,
                    self.acquire_lease,
                    self.send,
                    self.query_result,
                )
            )
        )


@dataclass(frozen=True)
class ScheduleIntegrationDecision:
    """One shared decision for all legacy battery-control writers."""

    allow_legacy: bool
    mode: str
    state: str = RESULT_NOT_APPLIED
    command_id: str = ""
    detail: str = ""
    shadow_digest: str = ""
    _writer_permit: Any = field(default=None, repr=False, compare=False)


class LegacyWriterPermit:
    """One safely releasable admission held for a complete legacy write."""

    def __init__(self, gate):
        self._gate = gate
        self._released = False
        self._lock = threading.Lock()

    def release(self):
        """Release this writer exactly once."""
        with self._lock:
            if self._released:
                return
            self._released = True
        self._gate.release_writer()


class LegacyWriteGate:
    """Process-wide transition barrier and admitted-writer accounting."""

    def __init__(self):
        self._condition = threading.Condition()
        self._allowed = True
        self._reason = ""
        self._transition = None
        self._active_writers = 0

    def begin_transition(self, reason):
        """Close admission, serialize decisions, and drain admitted writers."""
        token = object()
        with self._condition:
            while self._transition is not None:
                self._condition.wait()
            self._transition = token
            self._allowed = False
            self._reason = str(reason)
            while self._active_writers:
                self._condition.wait()
            return token

    def finish_transition(self, token, allow_legacy, reason="", acquire_writer=False):
        """Atomically publish a decision and optionally admit its first writer."""
        with self._condition:
            if self._transition is not token:
                raise RuntimeError("legacy writer transition token is not current")
            permit = None
            self._allowed = bool(allow_legacy)
            self._reason = "" if allow_legacy else str(reason)
            if allow_legacy and acquire_writer:
                self._active_writers += 1
                permit = LegacyWriterPermit(self)
            self._transition = None
            self._condition.notify_all()
            return permit

    def release_writer(self):
        """Release one admitted writer and wake an exclusive transition."""
        with self._condition:
            if self._active_writers <= 0:
                raise RuntimeError("legacy writer permit underflow")
            self._active_writers -= 1
            if not self._active_writers:
                self._condition.notify_all()

    @property
    def allowed(self):
        """Return the current atomic writer permit."""
        with self._condition:
            return self._allowed

    @property
    def reason(self):
        """Return the diagnostic reason for a closed gate."""
        with self._condition:
            return self._reason

    @property
    def active_writers(self):
        """Return the number of complete legacy operations still admitted."""
        with self._condition:
            return self._active_writers


def _flag(value):
    """Interpret only explicit boolean-like values as enabled."""
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "enable", "enabled")
    return False


def _owner(subject):
    """Return the main PredBat object for a mixin or component."""
    base = getattr(subject, "base", None)
    if base is not None and callable(getattr(base, "get_arg", None)):
        return base
    return subject


def _get_arg(base, name, default=False):
    """Read one option without allowing test doubles to become truthy flags."""
    getter = getattr(base, "get_arg", None)
    if not callable(getter):
        return default
    try:
        return getter(name, default)
    except Exception:
        return default


def _now_utc(base):
    """Return PredBat's aware UTC clock, rejecting missing or invalid values."""
    value = getattr(base, "now_utc", None)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ScheduleValidationError("now_utc must be timezone-aware")
    return value.astimezone(timezone.utc)


def _invoke(callback, *args):
    """Invoke one injected binding and await it when necessary."""
    value = callback(*args)
    if inspect.isawaitable(value):
        return value

    async def completed():
        return value

    return completed()


def _storage_from(base, bindings=None):
    """Resolve only an async storage implementation, never an arbitrary mock."""
    if bindings is not None and bindings.storage is not None:
        candidate = bindings.storage
    else:
        components = getattr(base, "components", None)
        getter = getattr(components, "get_component", None)
        if not callable(getter):
            return None
        try:
            candidate = getter("storage")
        except Exception:
            return None
    if candidate is None:
        return None
    if not inspect.iscoroutinefunction(getattr(candidate, "load", None)):
        return None
    if not inspect.iscoroutinefunction(getattr(candidate, "save", None)):
        return None
    return candidate


def _window_bounds(window):
    """Validate and return one optimizer window's integer minute bounds."""
    if not isinstance(window, dict):
        raise ScheduleValidationError("optimizer window must be a mapping")
    start = window.get("start")
    end = window.get("end")
    if isinstance(start, bool) or not isinstance(start, int) or isinstance(end, bool) or not isinstance(end, int):
        raise ScheduleValidationError("optimizer window bounds must be integers")
    if start < 0 or end <= start:
        raise ScheduleValidationError("optimizer window must be non-empty")
    return start, end


def build_complete_plan(base, altitude_id="predbat-battery"):
    """Normalize PredBat's complete best plan for the pure v0.4 compiler."""
    now_utc = _now_utc(base)
    timeline_start = getattr(base, "midnight_utc", None)
    if not isinstance(timeline_start, datetime) or timeline_start.tzinfo is None or timeline_start.utcoffset() is None:
        timeline_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    timeline_start = timeline_start.astimezone(timezone.utc)

    charge_windows = tuple(getattr(base, "charge_window_best", ()) or ())
    charge_limits = tuple(getattr(base, "charge_limit_best", ()) or ())
    export_windows = tuple(getattr(base, "export_window_best", ()) or ())
    export_limits = tuple(getattr(base, "export_limits_best", ()) or ())
    if len(charge_windows) != len(charge_limits) or len(export_windows) != len(export_limits):
        raise ScheduleValidationError("optimizer plan windows and limits are incomplete")

    soc_max = getattr(base, "soc_max", 0)
    if not isinstance(soc_max, (int, float)) or isinstance(soc_max, bool) or soc_max <= 0:
        raise ScheduleValidationError("optimizer battery capacity is invalid")
    reserve = getattr(base, "reserve", 0)
    reserve_percent = calc_percent_limit(reserve, soc_max)
    charge_power = float(getattr(base, "battery_rate_max_charge", 0) * MINUTE_WATT)
    discharge_power = float(getattr(base, "battery_rate_max_export", getattr(base, "battery_rate_max_discharge", 0)) * MINUTE_WATT)

    windows = []
    latest_end = 0
    for index, window in enumerate(charge_windows):
        start, end = _window_bounds(window)
        latest_end = max(latest_end, end)
        target_soc = calc_percent_limit(charge_limits[index], soc_max)
        if target_soc == reserve_percent:
            windows.append(
                PlanWindow(
                    altitude_id,
                    start,
                    end,
                    "hold",
                    target_soc=float(target_soc),
                    reserve_soc=float(reserve_percent),
                    charge_power_limit=0.0,
                    enable=True,
                )
            )
        else:
            windows.append(
                PlanWindow(
                    altitude_id,
                    start,
                    end,
                    "force_charge",
                    target_soc=float(target_soc),
                    charge_power_limit=charge_power,
                    enable=True,
                )
            )

    for index, window in enumerate(export_windows):
        start, end = _window_bounds(window)
        latest_end = max(latest_end, end)
        target_soc = export_limits[index]
        if isinstance(target_soc, bool) or not isinstance(target_soc, (int, float)):
            raise ScheduleValidationError("optimizer export limit is invalid")
        windows.append(
            PlanWindow(
                altitude_id,
                start,
                end,
                "force_export",
                target_soc=float(target_soc),
                discharge_power_limit=discharge_power,
                enable=True,
            )
        )

    configured_horizon = getattr(base, "forecast_plan_hours", 8)
    if isinstance(configured_horizon, bool) or not isinstance(configured_horizon, (int, float)) or configured_horizon <= 0:
        raise ScheduleValidationError("optimizer plan horizon is invalid")
    now_offset = int((now_utc - timeline_start).total_seconds() // 60)
    horizon_minutes = max(latest_end, now_offset + int(configured_horizon * 60), now_offset + 1)
    timezone_name = str(getattr(base, "local_tz", "UTC"))
    return CompletePlan(
        altitude_id=altitude_id,
        timeline_start_utc=timeline_start,
        horizon_minutes=horizon_minutes,
        diagnostic_timezone=timezone_name,
        windows=tuple(windows),
        default_mode="self_use",
        execution=EXECUTION_NATIVE,
        is_complete=True,
    )


def stable_command_id(schedule, target, lease):
    """Bind replay identity to the exact schedule, target, and admission lease."""
    value = {
        "plan_digest": schedule.plan_digest,
        "target": {
            "provider": target.provider,
            "node_id": target.node_id,
            "access_path": target.access_path,
            "topology_version": target.topology_version,
            "doc_version": target.doc_version,
            "cap_ref": target.cap_ref,
            "altitude_id": target.altitude_id,
            "scope_id": target.scope_id,
            "offer": target.offer.fingerprint,
            "owned_node_ids": list(target.owned_node_ids),
        },
        "lease": {
            "origin_id": lease.origin_id,
            "controller_class": lease.controller_class,
            "lease_id": lease.lease_id,
            "scope_id": lease.scope_id,
            "fencing_token": lease.fencing_token,
            "expires_ts_ms": lease.expires_ts_ms,
        },
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "predbat-" + hashlib.sha256(encoded).hexdigest()[:48]


def stable_cancellation_command_id(expected_plan_digest, reason, target, lease):
    """Bind one ending transition to exact installed authority and admission."""
    value = {
        "transition": "CANCEL",
        "expected_plan_digest": expected_plan_digest,
        "reason": reason,
        "target": {
            "provider": target.provider,
            "node_id": target.node_id,
            "access_path": target.access_path,
            "topology_version": target.topology_version,
            "doc_version": target.doc_version,
            "cap_ref": target.cap_ref,
            "altitude_id": target.altitude_id,
            "scope_id": target.scope_id,
            "offer": target.offer.fingerprint,
            "owned_node_ids": list(target.owned_node_ids),
        },
        "lease": {
            "origin_id": lease.origin_id,
            "controller_class": lease.controller_class,
            "lease_id": lease.lease_id,
            "scope_id": lease.scope_id,
            "fencing_token": lease.fencing_token,
            "expires_ts_ms": lease.expires_ts_ms,
        },
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "predbat-cancel-" + hashlib.sha256(encoded).hexdigest()[:41]


class LatticeScheduleCoordinator:
    """Coordinate one shared legacy/Lattice writer decision for PredBat."""

    def __init__(self, base):
        self.base = base
        self.gate = LegacyWriteGate()
        self._marker_loaded = False
        self._marker = None
        self._marker_corrupt = False
        self._marker_load_failed = False
        self._authority = None
        self._authority_scope = ""
        self._authority_binding_key = None
        self.last_decision = ScheduleIntegrationDecision(True, MODE_OFF)

    def _mode(self):
        shadow = _flag(_get_arg(self.base, "lattice_schedule_shadow_enable", False))
        active = _flag(_get_arg(self.base, "lattice_control_enable", False)) and _flag(_get_arg(self.base, "lattice_schedule_control_enable", False))
        if active:
            return MODE_ACTIVE
        if shadow:
            return MODE_SHADOW
        return MODE_OFF

    def _bindings(self):
        value = getattr(self.base, "_lattice_schedule_bindings", None)
        return value if isinstance(value, ScheduleRuntimeBindings) else None

    def _storage(self):
        return _storage_from(self.base, self._bindings())

    async def _load_marker(self):
        if self._marker_loaded:
            return
        storage = self._storage()
        if storage is None:
            return
        try:
            marker = await storage.load(_MARKER_MODULE, _MARKER_KEY)
        except Exception:
            self._marker_load_failed = True
            marker = None
        if marker is None:
            return
        if not isinstance(marker, dict) or marker.get("schema_version") != _MARKER_SCHEMA or marker.get("ever_armed") is not True or not isinstance(marker.get("scope_id"), str) or not marker["scope_id"]:
            self._marker_corrupt = True
        else:
            self._marker = dict(marker)
        self._marker_loaded = True

    async def _arm(self, scope_id):
        await self._load_marker()
        if self._marker_corrupt or self._marker_load_failed:
            return False
        if self._marker is not None:
            return self._marker["scope_id"] == scope_id
        storage = self._storage()
        if storage is None:
            return False
        marker = {"schema_version": _MARKER_SCHEMA, "ever_armed": True, "scope_id": scope_id}
        try:
            saved = bool(await storage.save(_MARKER_MODULE, _MARKER_KEY, marker, format="json", expiry=None))
        except Exception:
            saved = False
        if saved:
            self._marker = marker
            self._marker_loaded = True
        return saved

    def _authority_for(self, scope_id, bindings=None):
        storage = self._storage()
        if storage is None:
            return None
        binding_key = (
            id(storage),
            id(bindings.send) if bindings is not None else None,
            id(bindings.query_result) if bindings is not None else None,
        )
        if self._authority is not None and self._authority_scope == scope_id and self._authority_binding_key == binding_key:
            return self._authority

        async def unavailable_send(_command):
            return None

        async def unavailable_query(_command_id):
            return None

        self._authority = LatticeScheduleAuthority(
            storage=storage,
            send=bindings.send if bindings is not None else unavailable_send,
            query_result=bindings.query_result if bindings is not None else unavailable_query,
            scope_id=scope_id,
        )
        self._authority_scope = scope_id
        self._authority_binding_key = binding_key
        return self._authority

    def _record(self, decision):
        self.last_decision = decision
        return decision

    def _suppress(self, mode, detail, state=RESULT_NOT_APPLIED, command_id=""):
        return self._record(ScheduleIntegrationDecision(False, mode, state, command_id, detail))

    async def _request_ending_transition(self, mode, authority, authority_decision, bindings, now_ts_ms):
        """Request one guarded cancellation without ever opening the legacy gate."""
        if mode != MODE_ACTIVE:
            return self._suppress(
                mode,
                "installed Lattice schedule requires an explicit ending transition",
                authority_decision.state,
                authority_decision.command_id,
            )
        if bindings is None or not bindings.complete:
            return self._suppress(
                mode,
                "complete Lattice schedule runtime bindings are unavailable for ending transition",
                authority_decision.state,
                authority_decision.command_id,
            )
        if not authority_decision.installed_plan_digest or bindings.scope_id != self._marker["scope_id"]:
            return self._suppress(
                mode,
                "installed Lattice authority cannot be bound to a safe ending transition",
                authority_decision.state,
                authority_decision.command_id,
            )
        try:
            target = await _invoke(
                bindings.resolve_cancellation_target,
                bindings.scope_id,
                authority_decision.installed_plan_digest,
            )
            lease = await _invoke(bindings.acquire_lease, target, now_ts_ms)
        except Exception as exc:
            return self._suppress(
                mode,
                "Lattice ending target or lease resolution failed: {}".format(exc),
                authority_decision.state,
                authority_decision.command_id,
            )
        if not isinstance(target, ScheduleTarget) or not isinstance(lease, ScheduleLease):
            return self._suppress(
                mode,
                "Lattice ending target or lease resolution returned an invalid type",
                authority_decision.state,
                authority_decision.command_id,
            )
        if target.scope_id != bindings.scope_id or lease.scope_id != bindings.scope_id:
            return self._suppress(
                mode,
                "Lattice ending target, lease, and configured scope differ",
                authority_decision.state,
                authority_decision.command_id,
            )
        command_id = stable_cancellation_command_id(
            authority_decision.installed_plan_digest,
            "VALIDITY_END",
            target,
            lease,
        )
        ending = await authority.cancel(
            target,
            lease,
            authority_decision.installed_plan_digest,
            "VALIDITY_END",
            now_ts_ms,
            command_id,
        )
        if ending.state == RESULT_APPLIED and not ending.legacy_suppressed:
            return None
        return self._suppress(
            mode,
            ending.detail or "Lattice ending transition has not proven authority release",
            ending.state,
            ending.command_id,
        )

    async def _prior_schedule_decision(self, mode, now_ts_ms):
        await self._load_marker()
        if self._marker_load_failed:
            return self._suppress(mode, "durable Lattice activation marker could not be loaded")
        if self._marker_corrupt:
            return self._suppress(mode, "durable Lattice activation marker is corrupt")
        if self._marker is None:
            return None
        authority = self._authority_for(self._marker["scope_id"], self._bindings())
        if authority is None:
            return self._suppress(mode, "durable Lattice authority storage is unavailable")
        decision = await authority.legacy_decision(now_ts_ms)
        if not decision.durable_state_present:
            return self._suppress(
                mode,
                "durable Lattice authority state is missing after activation",
                RESULT_UNKNOWN,
                decision.command_id,
            )
        if decision.ending_transition_required:
            return await self._request_ending_transition(
                mode,
                authority,
                decision,
                self._bindings(),
                now_ts_ms,
            )
        if decision.legacy_suppressed or decision.quarantined:
            return self._suppress(mode, decision.detail or "durable Lattice schedule owns this scope", decision.state, decision.command_id)
        return None

    async def _evaluate_before_plan(self, mode):
        """Evaluate plan authority while the exclusive transition is held."""
        await self._load_marker()
        if self._marker_load_failed:
            return self._suppress(mode, "durable Lattice activation marker could not be loaded")
        if self._marker_corrupt:
            return self._suppress(mode, "durable Lattice activation marker is corrupt")
        if self._marker is not None and self._storage() is None:
            return self._suppress(mode, "durable Lattice authority storage is unavailable")

        try:
            now_utc = _now_utc(self.base)
            now_ts_ms = int(now_utc.timestamp() * 1000)
        except Exception as exc:
            if mode == MODE_ACTIVE or self._marker is not None:
                return self._suppress(mode, str(exc))
            return self._record(ScheduleIntegrationDecision(True, mode, detail=str(exc)))

        prior = await self._prior_schedule_decision(mode, now_ts_ms)
        if prior is not None:
            if mode != MODE_ACTIVE or prior.state != RESULT_APPLIED:
                return prior
            # ACTIVE may atomically replace a still-valid installed schedule.
            # The shared gate remains closed throughout compilation and dispatch.

        if mode == MODE_OFF:
            return self._record(ScheduleIntegrationDecision(True, mode))

        if mode == MODE_SHADOW:
            try:
                complete_plan = build_complete_plan(self.base)
                schedule = compile_absolute_schedule(complete_plan, now_utc)
            except Exception as exc:
                return self._record(ScheduleIntegrationDecision(True, mode, detail=str(exc)))
            return self._record(ScheduleIntegrationDecision(True, mode, shadow_digest=schedule.plan_digest))

        bindings = self._bindings()
        if bindings is None or not bindings.complete:
            return self._suppress(mode, "complete Lattice schedule runtime bindings are unavailable")
        if self._storage() is None:
            return self._suppress(mode, "durable Lattice authority storage is unavailable")

        try:
            complete_plan = await _invoke(bindings.plan_builder, self.base)
            if not isinstance(complete_plan, CompletePlan):
                raise ScheduleValidationError("injected ACTIVE plan builder did not return CompletePlan")
            schedule = compile_absolute_schedule(complete_plan, now_utc)
        except Exception as exc:
            return self._suppress(mode, str(exc))

        try:
            target = await _invoke(bindings.resolve_target, schedule)
            lease = await _invoke(bindings.acquire_lease, target, now_ts_ms)
        except Exception as exc:
            return self._suppress(mode, "Lattice target or lease resolution failed: {}".format(exc))
        if not isinstance(target, ScheduleTarget) or not isinstance(lease, ScheduleLease):
            return self._suppress(mode, "Lattice target or lease resolution returned an invalid type")
        if target.scope_id != bindings.scope_id or lease.scope_id != bindings.scope_id:
            return self._suppress(mode, "Lattice target, lease, and configured scope differ")
        if not await self._arm(bindings.scope_id):
            return self._suppress(mode, "durable Lattice activation marker could not be persisted")

        authority = self._authority_for(bindings.scope_id, bindings)
        if authority is None:
            return self._suppress(mode, "durable Lattice authority storage is unavailable")
        command_id = stable_command_id(schedule, target, lease)
        authority_decision = await authority.dispatch(schedule, target, lease, now_ts_ms, command_id)
        return self._suppress(
            mode,
            authority_decision.detail or "Lattice schedule authority suppresses legacy writers",
            authority_decision.state,
            authority_decision.command_id,
        )

    async def _begin_transition(self, reason):
        """Enter the blocking writer barrier without blocking the event loop."""
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, self.gate.begin_transition, reason)
        try:
            return await asyncio.shield(future)
        except BaseException:
            token = await asyncio.shield(future)
            self.gate.finish_transition(token, False, "Lattice authority transition requester was cancelled")
            raise

    def _finish_transition(self, token, decision, acquire_writer):
        """Publish one decision and atomically attach its admitted writer."""
        permit = self.gate.finish_transition(
            token,
            decision.allow_legacy,
            decision.detail,
            acquire_writer=acquire_writer and decision.allow_legacy,
        )
        if permit is not None:
            return replace(decision, _writer_permit=permit)
        return decision

    async def before_plan(self):
        """Evaluate authority and atomically admit one complete plan writer."""
        token = await self._begin_transition("Lattice schedule authority is reconciling before plan execution")
        try:
            mode = self._mode()
            decision = await self._evaluate_before_plan(mode)
        except BaseException as exc:
            self.gate.finish_transition(token, False, "Lattice schedule decision failed: {}".format(exc))
            raise
        return self._finish_transition(token, decision, acquire_writer=True)

    async def _evaluate_legacy_writer(self, writer, mode):
        """Refresh durable ownership while the exclusive transition is held."""
        await self._load_marker()
        if self._marker_load_failed:
            return self._suppress(mode, "durable Lattice activation marker could not be loaded")
        if self._marker_corrupt:
            return self._suppress(mode, "durable Lattice activation marker is corrupt")
        if self._marker is not None and self._storage() is None:
            return self._suppress(mode, "durable Lattice authority storage is unavailable")
        try:
            now_ts_ms = int(_now_utc(self.base).timestamp() * 1000)
        except Exception as exc:
            if mode == MODE_ACTIVE or self._marker is not None:
                return self._suppress(mode, "{} clock is invalid: {}".format(writer, exc))
            return self._record(ScheduleIntegrationDecision(True, mode, detail=str(exc)))
        prior = await self._prior_schedule_decision(mode, now_ts_ms)
        if prior is not None:
            return prior
        if mode == MODE_ACTIVE:
            return self._suppress(mode, "{} is blocked while Lattice schedule control is ACTIVE".format(writer))
        return self._record(ScheduleIntegrationDecision(True, mode))

    async def acquire_legacy_writer(self, writer):
        """Atomically decide and admit one complete non-plan legacy writer."""
        token = await self._begin_transition("{} is waiting for a Lattice authority decision".format(writer))
        try:
            mode = self._mode()
            decision = await self._evaluate_legacy_writer(writer, mode)
        except BaseException as exc:
            self.gate.finish_transition(token, False, "{} authority decision failed: {}".format(writer, exc))
            raise
        decision = self._finish_transition(token, decision, acquire_writer=True)
        return decision._writer_permit

    async def legacy_permitted(self, writer):
        """Compatibility probe which never leaves an unowned writer permit."""
        permit = await self.acquire_legacy_writer(writer)
        if permit is None:
            return False
        permit.release()
        return True


def get_schedule_coordinator(subject):
    """Return the one coordinator shared by PredBat and all components."""
    base = _owner(subject)
    coordinator = getattr(base, "_lattice_schedule_coordinator", None)
    if isinstance(coordinator, LatticeScheduleCoordinator):
        return coordinator
    with _COORDINATOR_LOCK:
        coordinator = getattr(base, "_lattice_schedule_coordinator", None)
        if not isinstance(coordinator, LatticeScheduleCoordinator):
            coordinator = LatticeScheduleCoordinator(base)
            setattr(base, "_lattice_schedule_coordinator", coordinator)
    return coordinator


def before_legacy_plan(subject):
    """Synchronous guard used at the first line of Execute.execute_plan."""
    coordinator = get_schedule_coordinator(subject)
    return run_async(coordinator.before_plan())


def acquire_legacy_writer(subject, writer):
    """Synchronously acquire a permit held across one complete legacy writer."""
    coordinator = get_schedule_coordinator(subject)
    return run_async(coordinator.acquire_legacy_writer(writer))


async def acquire_legacy_writer_async(subject, writer):
    """Asynchronously acquire a permit without blocking the caller's event loop."""
    coordinator = get_schedule_coordinator(subject)
    return await coordinator.acquire_legacy_writer(writer)


def release_legacy_writer_permit(decision):
    """Release the plan permit attached to an allowed integration decision."""
    permit = getattr(decision, "_writer_permit", None)
    if permit is not None:
        permit.release()


def legacy_write_permitted(subject, writer):
    """Compatibility probe; complete writers must use a permit-holding wrapper."""
    coordinator = get_schedule_coordinator(subject)
    return bool(run_async(coordinator.legacy_permitted(writer)))


async def legacy_write_permitted_async(subject, writer):
    """Compatibility probe; complete writers must use a permit-holding wrapper."""
    coordinator = get_schedule_coordinator(subject)
    return bool(await coordinator.legacy_permitted(writer))


def lattice_plan_writer(method):
    """Hold one legacy permit across complete Execute plan execution."""

    @wraps(method)
    def guarded(subject, *args, **kwargs):
        decision = before_legacy_plan(subject)
        if not decision.allow_legacy:
            subject.log(
                "Info: Lattice schedule authority suppressed legacy plan execution: state={} command_id={} detail={}".format(
                    decision.state,
                    decision.command_id,
                    decision.detail,
                )
            )
            subject.set_charge_export_status(False, False, True)
            subject.isCharging = False
            subject.isExporting = False
            return "Lattice schedule", decision.detail
        try:
            return method(subject, *args, **kwargs)
        finally:
            release_legacy_writer_permit(decision)

    return guarded


def lattice_balance_writer(method):
    """Hold one legacy permit across complete inverter balancing."""

    @wraps(method)
    def guarded(subject, *args, **kwargs):
        permit = acquire_legacy_writer(subject, "inverter balancing")
        if permit is None:
            subject.log("Info: Lattice schedule authority suppressed legacy inverter balancing")
            return False
        try:
            return method(subject, *args, **kwargs)
        finally:
            permit.release()

    return guarded


def lattice_async_writer(writer):
    """Decorate one async Gateway writer with complete permit lifetime."""

    def decorate(method):
        @wraps(method)
        async def guarded(subject, *args, **kwargs):
            label = writer(subject, *args, **kwargs) if callable(writer) else writer
            permit = await acquire_legacy_writer_async(subject, label)
            if permit is None:
                subject.log("Info: GatewayMQTT: Lattice schedule authority suppressed {}".format(label))
                return None
            try:
                return await method(subject, *args, **kwargs)
            finally:
                permit.release()

        return guarded

    return decorate
