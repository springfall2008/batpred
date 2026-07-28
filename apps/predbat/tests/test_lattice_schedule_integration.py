"""Tests for the default-off live Lattice schedule integration seam."""

# cspell:words READBACK

import asyncio
import copy
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import predbat as _predbat  # noqa: F401 - establish package import order before execute
from execute import Execute
from gateway import GatewayMQTT
from lattice_schedule_authority import (
    FALLBACK_SAFE,
    RESULT_APPLIED,
    RESULT_NOT_APPLIED,
    RESULT_UNKNOWN,
    VERIFICATION_DURABLE_STORE_READBACK,
    ResolvedScheduleOffer,
    ScheduleAck,
    ScheduleLease,
    ScheduleTarget,
)
from lattice_schedule_integration import (
    MODE_ACTIVE,
    MODE_OFF,
    MODE_SHADOW,
    LatticeScheduleCoordinator,
    ScheduleRuntimeBindings,
    before_legacy_plan,
    build_complete_plan,
    get_schedule_coordinator,
    lattice_async_writer,
    lattice_balance_writer,
    release_legacy_writer_permit,
)


NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
SCOPE = "hub:GW-1:battery"
ALTITUDE = "predbat-battery"


class FakeStorage:
    """Asynchronous module/key storage shared across coordinator restarts."""

    def __init__(self):
        self.values = {}
        self.save_count = 0
        self.fail_saves = False
        self.fail_loads = False

    async def load(self, module, filename):
        if self.fail_loads:
            raise OSError("load failed")
        return copy.deepcopy(self.values.get((module, filename)))

    async def save(self, module, filename, data, **_kwargs):
        self.save_count += 1
        if self.fail_saves:
            return False
        self.values[(module, filename)] = copy.deepcopy(data)
        return True


class FakeComponents:
    """Expose only the storage component used by the seam."""

    def __init__(self, storage):
        self.storage = storage

    def get_component(self, name):
        return self.storage if name == "storage" else None


class FakeBase:
    """Minimal complete PredBat plan and configuration fixture."""

    def __init__(self, storage=None):
        self.options = {
            "lattice_control_enable": False,
            "lattice_schedule_control_enable": False,
            "lattice_schedule_shadow_enable": False,
        }
        self.now_utc = NOW
        self.midnight_utc = NOW.replace(hour=0, minute=0)
        self.local_tz = timezone.utc
        self.forecast_plan_hours = 8
        self.charge_window_best = [{"start": 13 * 60, "end": 14 * 60}]
        self.charge_limit_best = [8.0]
        self.export_window_best = [{"start": 17 * 60, "end": 18 * 60}]
        self.export_limits_best = [20.0]
        self.soc_max = 10.0
        self.reserve = 1.0
        self.battery_rate_max_charge = 50.0
        self.battery_rate_max_discharge = 50.0
        self.battery_rate_max_export = 50.0
        self.components = FakeComponents(storage) if storage is not None else None
        self.logs = []
        self.status_calls = []

    def get_arg(self, name, default=None, *args, **kwargs):
        return self.options.get(name, default)

    def log(self, message):
        self.logs.append(message)

    def set_charge_export_status(self, *args):
        self.status_calls.append(args)


def resolved_target():
    """Return a topology-bound schedule target compatible with the fixture."""
    return ScheduleTarget(
        provider="predbat-gateway",
        node_id="GW-1",
        access_path="gw-local",
        topology_version="0.4.0",
        doc_version=12,
        cap_ref=41,
        altitude_id=ALTITUDE,
        scope_id=SCOPE,
        offer=ResolvedScheduleOffer(
            max_slots=8,
            supported_modes=("self_use", "force_charge", "force_export", "hold"),
            slot_fields=("target_soc", "reserve_soc", "charge_power_limit", "discharge_power_limit", "enable"),
            execution_modes=("NATIVE",),
            gap_policy="DEFAULT_MODE",
        ),
        owned_node_ids=("GW-1",),
    )


def lease(fence=10, expiry=None):
    """Return one fresh AUTOMATION ownership lease."""
    return ScheduleLease(
        origin_id="predbat",
        controller_class="AUTOMATION",
        lease_id="lease-{}".format(fence),
        scope_id=SCOPE,
        fencing_token=fence,
        expires_ts_ms=int((expiry or (NOW + timedelta(minutes=30))).timestamp() * 1000),
    )


def applied(command):
    """Return an exact durable APPLIED receipt."""
    return ScheduleAck(
        command_id=command.command_id,
        state=RESULT_APPLIED,
        scope_id=SCOPE,
        fencing_token=command.lease.fencing_token,
        lease_id=command.lease.lease_id,
        lease_expires_ts_ms=command.lease.expires_ts_ms,
        origin_id=command.lease.origin_id,
        controller_class=command.lease.controller_class,
        plan_digest=command.schedule.plan_digest,
        accepted_slot_count=len(command.schedule.slots),
        durably_accepted=True,
        valid_from_ts_ms=command.schedule.valid_from_ts_ms,
        valid_until_ts_ms=command.schedule.valid_until_ts_ms,
        verification=VERIFICATION_DURABLE_STORE_READBACK,
        verified=True,
    )


def cancellation_applied(command, **overrides):
    """Return exact durable proof that an ending transition released authority."""
    value = {
        "command_id": command.command_id,
        "state": RESULT_APPLIED,
        "scope_id": SCOPE,
        "fencing_token": command.lease.fencing_token,
        "lease_id": command.lease.lease_id,
        "lease_expires_ts_ms": command.lease.expires_ts_ms,
        "origin_id": command.lease.origin_id,
        "controller_class": command.lease.controller_class,
        "cancelled_plan_digest": command.cancellation.expected_plan_digest,
        "writer_exclusion_released": True,
        "verification": VERIFICATION_DURABLE_STORE_READBACK,
        "verified": True,
    }
    value.update(overrides)
    return ScheduleAck(**value)


def rejected(command):
    """Return one definite safe-to-fallback rejection."""
    return ScheduleAck(
        command_id=command.command_id,
        state=RESULT_NOT_APPLIED,
        scope_id=SCOPE,
        fencing_token=command.lease.fencing_token,
        reason="UNSUPPORTED_OFFER",
        fallback=FALLBACK_SAFE,
        detail="schedule was not applied",
    )


def unknown(command):
    """Return one ambiguous result which must keep all legacy writers closed."""
    return ScheduleAck(
        command_id=command.command_id,
        state=RESULT_UNKNOWN,
        scope_id=SCOPE,
        fencing_token=command.lease.fencing_token,
        reason="EXECUTION_AMBIGUOUS",
        fallback="BLOCKED",
        detail="write completion is ambiguous",
    )


def install_bindings(
    base,
    storage,
    send,
    fence=10,
    expiry=None,
    query=None,
    plan_builder=build_complete_plan,
    cancellation_target=None,
):
    """Inject all otherwise-unavailable runtime seams."""
    target = resolved_target()
    current_lease = lease(fence, expiry)
    base._lattice_schedule_bindings = ScheduleRuntimeBindings(
        scope_id=SCOPE,
        plan_builder=plan_builder,
        resolve_target=lambda _schedule: target,
        resolve_cancellation_target=(cancellation_target or (lambda _scope_id, _expected_plan_digest: target)),
        acquire_lease=lambda _target, _now: current_lease,
        send=send,
        query_result=query or (lambda _command_id: None),
        storage=storage,
    )


def activate(base):
    """Enable both required ACTIVE gates."""
    base.options["lattice_control_enable"] = True
    base.options["lattice_schedule_control_enable"] = True


def test_off_preserves_legacy_without_runtime_bindings():
    base = FakeBase()
    decision = before_legacy_plan(base)
    assert decision.allow_legacy is True
    assert decision.mode == MODE_OFF
    assert get_schedule_coordinator(base).gate.allowed is True
    release_legacy_writer_permit(decision)


def test_shadow_compiles_digest_but_never_sends_or_persists_marker():
    storage = FakeStorage()
    base = FakeBase(storage)
    base.options["lattice_schedule_shadow_enable"] = True
    sends = []
    install_bindings(base, storage, sends.append)

    decision = before_legacy_plan(base)

    assert decision.allow_legacy is True
    assert decision.mode == MODE_SHADOW
    assert len(decision.shadow_digest) == 64
    assert sends == []
    assert ("lattice_schedule_integration", "ever_armed") not in storage.values
    assert ("lattice_schedule_authority", "schedule") not in storage.values
    release_legacy_writer_permit(decision)


def test_active_without_complete_bindings_fails_closed_before_any_write():
    base = FakeBase()
    activate(base)

    decision = before_legacy_plan(base)

    assert decision.allow_legacy is False
    assert decision.mode == MODE_ACTIVE
    assert "bindings are unavailable" in decision.detail
    assert get_schedule_coordinator(base).gate.allowed is False


def test_safe_rejection_keeps_all_legacy_writers_closed_and_same_command_is_not_resent():
    storage = FakeStorage()
    base = FakeBase(storage)
    activate(base)
    commands = []

    def send(command):
        commands.append(command)
        return rejected(command)

    install_bindings(base, storage, send)
    first = before_legacy_plan(base)
    second = before_legacy_plan(base)

    assert first.allow_legacy is False
    assert second.allow_legacy is False
    assert first.state == RESULT_NOT_APPLIED
    assert second.state == RESULT_NOT_APPLIED
    assert first.command_id == second.command_id
    assert len(commands) == 1
    assert storage.values[("lattice_schedule_integration", "ever_armed")]["ever_armed"] is True
    assert asyncio.run(get_schedule_coordinator(base).legacy_permitted("gateway legacy command")) is False
    assert get_schedule_coordinator(base).gate.allowed is False


def test_active_requires_an_injected_complete_plan_builder():
    storage = FakeStorage()
    base = FakeBase(storage)
    activate(base)
    install_bindings(base, storage, rejected, plan_builder=None)

    decision = before_legacy_plan(base)

    assert decision.allow_legacy is False
    assert "bindings are unavailable" in decision.detail
    assert ("lattice_schedule_integration", "ever_armed") not in storage.values


def test_active_gate_is_closed_before_storage_plan_target_lease_and_send_work():
    events = []

    class ObservedStorage(FakeStorage):
        async def load(self, module, filename):
            events.append(("storage-load", get_schedule_coordinator(base).gate.allowed))
            return await super().load(module, filename)

        async def save(self, module, filename, data, **kwargs):
            events.append(("storage-save", get_schedule_coordinator(base).gate.allowed))
            return await super().save(module, filename, data, **kwargs)

    storage = ObservedStorage()
    base = FakeBase(storage)
    activate(base)
    target = resolved_target()
    current_lease = lease()

    def plan_builder(subject):
        events.append(("plan-builder", get_schedule_coordinator(subject).gate.allowed))
        return build_complete_plan(subject)

    def resolve_target(_schedule):
        events.append(("target", get_schedule_coordinator(base).gate.allowed))
        return target

    def acquire_lease(_target, _now):
        events.append(("lease", get_schedule_coordinator(base).gate.allowed))
        return current_lease

    def send(command):
        events.append(("send", get_schedule_coordinator(base).gate.allowed))
        return rejected(command)

    base._lattice_schedule_bindings = ScheduleRuntimeBindings(
        scope_id=SCOPE,
        plan_builder=plan_builder,
        resolve_target=resolve_target,
        resolve_cancellation_target=lambda _scope_id, _digest: target,
        acquire_lease=acquire_lease,
        send=send,
        query_result=lambda _command_id: None,
        storage=storage,
    )

    decision = before_legacy_plan(base)

    assert decision.allow_legacy is False
    assert events
    assert events[0][0] == "storage-load"
    assert {"storage-load", "storage-save", "plan-builder", "target", "lease", "send"}.issubset({name for name, _allowed in events})
    assert all(allowed is False for _name, allowed in events)


def test_missing_clock_is_exact_legacy_off_shadow_diagnostic_and_active_closed():
    off = FakeBase()
    del off.now_utc
    off_decision = before_legacy_plan(off)

    shadow = FakeBase()
    del shadow.now_utc
    shadow.options["lattice_schedule_shadow_enable"] = True
    shadow_decision = before_legacy_plan(shadow)

    active = FakeBase()
    del active.now_utc
    activate(active)
    active_decision = before_legacy_plan(active)

    assert off_decision.allow_legacy is True
    assert off_decision.mode == MODE_OFF
    assert shadow_decision.allow_legacy is True
    assert shadow_decision.mode == MODE_SHADOW
    assert "timezone-aware" in shadow_decision.detail
    assert active_decision.allow_legacy is False
    assert active_decision.mode == MODE_ACTIVE
    assert get_schedule_coordinator(active).gate.allowed is False
    release_legacy_writer_permit(off_decision)
    release_legacy_writer_permit(shadow_decision)


def test_storage_absence_is_not_cached_and_later_existing_marker_is_observed():
    storage = FakeStorage()
    armed = FakeBase(storage)
    activate(armed)
    install_bindings(armed, storage, applied)
    assert before_legacy_plan(armed).state == RESULT_APPLIED

    restarted = FakeBase()
    first = before_legacy_plan(restarted)
    release_legacy_writer_permit(first)
    restarted.components = FakeComponents(storage)
    second = before_legacy_plan(restarted)

    assert first.mode == MODE_OFF
    assert first.allow_legacy is True
    assert second.mode == MODE_OFF
    assert second.allow_legacy is False
    assert second.state == RESULT_APPLIED


def test_present_storage_marker_absence_is_not_cached_across_external_activation():
    """A later durable activation is observed even when storage existed at boot."""
    storage = FakeStorage()
    restarted = FakeBase(storage)
    first = before_legacy_plan(restarted)
    assert first.allow_legacy is True
    release_legacy_writer_permit(first)

    armed = FakeBase(storage)
    activate(armed)
    install_bindings(armed, storage, applied)
    assert before_legacy_plan(armed).state == RESULT_APPLIED

    second = before_legacy_plan(restarted)
    assert second.allow_legacy is False
    assert second.state == RESULT_APPLIED


def test_armed_marker_without_authority_state_is_unknown_and_fail_closed():
    """Missing command state after activation is not inferred as non-application."""
    storage = FakeStorage()
    storage.values[("lattice_schedule_integration", "ever_armed")] = {
        "schema_version": 1,
        "ever_armed": True,
        "scope_id": SCOPE,
    }
    restarted = FakeBase(storage)

    decision = before_legacy_plan(restarted)

    assert decision.allow_legacy is False
    assert decision.state == RESULT_UNKNOWN
    assert "state is missing" in decision.detail
    assert get_schedule_coordinator(restarted).gate.allowed is False


def test_invalid_restart_clock_loads_marker_before_non_plan_writer_decision():
    """Every non-plan writer stays closed when durable ownership cannot be timed."""
    storage = FakeStorage()
    armed = FakeBase(storage)
    activate(armed)
    install_bindings(armed, storage, applied)
    assert before_legacy_plan(armed).state == RESULT_APPLIED

    restarted = FakeBase(storage)
    del restarted.now_utc
    coordinator = get_schedule_coordinator(restarted)

    assert asyncio.run(coordinator.legacy_permitted("gateway legacy command")) is False
    assert coordinator.gate.allowed is False
    assert coordinator._marker["scope_id"] == SCOPE


def test_active_transition_drains_decorated_writer_before_dispatch():
    """Activation cannot overlap a complete legacy method already in flight."""
    storage = FakeStorage()
    base = FakeBase(storage)
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    commands = []

    @lattice_balance_writer
    def blocking_writer(subject):
        entered.set()
        assert release.wait(timeout=2)
        completed.set()
        return True

    writer_thread = threading.Thread(target=blocking_writer, args=(base,))
    writer_thread.start()
    assert entered.wait(timeout=2)
    assert get_schedule_coordinator(base).gate.active_writers == 1

    activate(base)
    install_bindings(base, storage, lambda command: commands.append(command) or rejected(command))
    activation_result = []
    activation_thread = threading.Thread(target=lambda: activation_result.append(before_legacy_plan(base)))
    activation_thread.start()

    deadline = time.monotonic() + 2
    while get_schedule_coordinator(base).gate.allowed and time.monotonic() < deadline:
        time.sleep(0.001)
    assert get_schedule_coordinator(base).gate.allowed is False
    assert activation_thread.is_alive()
    assert commands == []

    release.set()
    writer_thread.join(timeout=2)
    activation_thread.join(timeout=2)

    assert completed.is_set()
    assert not writer_thread.is_alive()
    assert not activation_thread.is_alive()
    assert len(commands) == 1
    assert activation_result[0].allow_legacy is False
    assert get_schedule_coordinator(base).gate.active_writers == 0


def test_queued_writer_samples_active_mode_inside_exclusive_transition():
    """A writer queued while OFF cannot retain that stale mode across activation."""
    base = FakeBase()
    coordinator = get_schedule_coordinator(base)
    first = asyncio.run(coordinator.acquire_legacy_writer("first writer"))
    assert first is not None

    queued = []
    queued_thread = threading.Thread(target=lambda: queued.append(asyncio.run(coordinator.acquire_legacy_writer("queued writer"))))
    queued_thread.start()
    deadline = time.monotonic() + 2
    while coordinator.gate.allowed and time.monotonic() < deadline:
        time.sleep(0.001)
    assert coordinator.gate.allowed is False

    activate(base)
    first.release()
    queued_thread.join(timeout=2)

    assert not queued_thread.is_alive()
    assert queued == [None]
    assert coordinator.gate.active_writers == 0
    assert coordinator.gate.allowed is False


def test_decorated_writer_exception_releases_permit():
    """A failed legacy method cannot strand ACTIVE behind an admitted writer."""
    base = FakeBase()

    @lattice_balance_writer
    def broken_writer(_subject):
        raise RuntimeError("write failed")

    try:
        broken_writer(base)
    except RuntimeError as exc:
        assert str(exc) == "write failed"
    else:
        raise AssertionError("legacy writer exception was swallowed")

    assert get_schedule_coordinator(base).gate.active_writers == 0


def test_applied_schedule_survives_mode_downgrade_restart_and_lease_expiry():
    storage = FakeStorage()
    active = FakeBase(storage)
    activate(active)
    commands = []

    def send(command):
        commands.append(command)
        return applied(command)

    install_bindings(active, storage, send, expiry=NOW + timedelta(minutes=15))
    accepted = before_legacy_plan(active)
    assert accepted.allow_legacy is False
    assert accepted.state == RESULT_APPLIED

    restarted = FakeBase(storage)
    restarted.now_utc = NOW + timedelta(minutes=30)
    restarted.midnight_utc = restarted.now_utc.replace(hour=0, minute=0)
    downgraded = before_legacy_plan(restarted)

    assert downgraded.mode == MODE_OFF
    assert downgraded.allow_legacy is False
    assert downgraded.state == RESULT_APPLIED
    assert "validity has expired" not in downgraded.detail


def test_shadow_still_suppresses_a_durable_prior_applied_schedule():
    storage = FakeStorage()
    active = FakeBase(storage)
    activate(active)
    install_bindings(active, storage, applied)
    assert before_legacy_plan(active).allow_legacy is False

    shadow = FakeBase(storage)
    shadow.options["lattice_schedule_shadow_enable"] = True
    decision = before_legacy_plan(shadow)

    assert decision.mode == MODE_SHADOW
    assert decision.allow_legacy is False
    assert decision.state == RESULT_APPLIED


def test_valid_until_reopens_legacy_after_restart():
    storage = FakeStorage()
    active = FakeBase(storage)
    activate(active)
    install_bindings(active, storage, applied)
    assert before_legacy_plan(active).allow_legacy is False

    expired = FakeBase(storage)
    expired.now_utc = NOW.replace(hour=20)
    expired.midnight_utc = expired.now_utc.replace(hour=0, minute=0)
    sends = []
    install_bindings(
        expired,
        storage,
        sends.append,
        fence=11,
        expiry=expired.now_utc + timedelta(minutes=30),
    )
    decision = before_legacy_plan(expired)

    assert decision.mode == MODE_OFF
    assert decision.allow_legacy is False
    assert "ending transition" in decision.detail
    assert sends == []


def test_active_expiry_cancels_with_verified_release_before_replacement():
    """ACTIVE keeps the gate shut across guarded cancellation and replacement."""
    storage = FakeStorage()
    initial = FakeBase(storage)
    activate(initial)
    install_bindings(initial, storage, applied)
    assert before_legacy_plan(initial).state == RESULT_APPLIED
    installed_digest = storage.values[("lattice_schedule_authority", "schedule")]["installed"]["applied_plan_digest"]

    expired = FakeBase(storage)
    expired.now_utc = NOW.replace(hour=20)
    expired.midnight_utc = expired.now_utc.replace(hour=0, minute=0)
    activate(expired)
    commands = []
    gate_states = []
    ending_requests = []

    def cancellation_target(scope_id, expected_plan_digest):
        ending_requests.append((scope_id, expected_plan_digest))
        return resolved_target()

    def send(command):
        commands.append(command)
        gate_states.append(get_schedule_coordinator(expired).gate.allowed)
        if hasattr(command, "cancellation"):
            return cancellation_applied(command)
        return applied(command)

    install_bindings(
        expired,
        storage,
        send,
        fence=11,
        expiry=expired.now_utc + timedelta(minutes=30),
        cancellation_target=cancellation_target,
    )
    decision = before_legacy_plan(expired)

    assert decision.state == RESULT_APPLIED
    assert decision.allow_legacy is False
    assert gate_states == [False, False]
    assert ending_requests == [(SCOPE, installed_digest)]
    assert len(commands) == 2
    assert "cancellation" in commands[0].to_wire()["schedule_transition"]
    assert commands[0].cancellation.expected_plan_digest == installed_digest
    assert "replacement" in commands[1].to_wire()["schedule_transition"]
    durable = storage.values[("lattice_schedule_authority", "schedule")]
    assert durable["installed"]["command_id"] == commands[1].command_id


def test_unproven_applied_cancellation_stays_unknown_and_never_replaces():
    """Bare, mismatched, or acceptance-only APPLIED cannot reopen either writer."""
    mutations = [
        {"cancelled_plan_digest": "f" * 64},
        {"writer_exclusion_released": False},
        {
            "verification": "ACCEPTED_ONLY",
            "verified": False,
        },
    ]
    for mutation in mutations:
        storage = FakeStorage()
        initial = FakeBase(storage)
        activate(initial)
        install_bindings(initial, storage, applied)
        assert before_legacy_plan(initial).state == RESULT_APPLIED
        installed_digest = storage.values[("lattice_schedule_authority", "schedule")]["installed"]["applied_plan_digest"]

        expired = FakeBase(storage)
        expired.now_utc = NOW.replace(hour=20)
        expired.midnight_utc = expired.now_utc.replace(hour=0, minute=0)
        activate(expired)
        commands = []

        def send(command):
            commands.append(command)
            return cancellation_applied(command, **mutation)

        install_bindings(
            expired,
            storage,
            send,
            fence=11,
            expiry=expired.now_utc + timedelta(minutes=30),
        )
        decision = before_legacy_plan(expired)

        assert decision.state == RESULT_UNKNOWN
        assert decision.allow_legacy is False
        assert get_schedule_coordinator(expired).gate.allowed is False
        assert len(commands) == 1
        durable = storage.values[("lattice_schedule_authority", "schedule")]
        assert durable["installed"]["applied_plan_digest"] == installed_digest


def test_restart_reconciles_same_cancellation_id_without_republishing():
    """Power loss queries the ending command, then sends only the new schedule."""
    storage = FakeStorage()
    initial = FakeBase(storage)
    activate(initial)
    install_bindings(initial, storage, applied)
    assert before_legacy_plan(initial).state == RESULT_APPLIED

    expiry = NOW.replace(hour=20, minute=30)
    interrupted = FakeBase(storage)
    interrupted.now_utc = NOW.replace(hour=20)
    interrupted.midnight_utc = interrupted.now_utc.replace(hour=0, minute=0)
    activate(interrupted)
    cancellation_commands = []

    def lose_cancellation_result(command):
        cancellation_commands.append(command)
        return None

    install_bindings(
        interrupted,
        storage,
        lose_cancellation_result,
        fence=11,
        expiry=expiry,
        query=lambda _command_id: None,
    )
    unresolved = before_legacy_plan(interrupted)
    assert unresolved.state == RESULT_UNKNOWN
    assert unresolved.allow_legacy is False
    assert len(cancellation_commands) == 1

    restarted = FakeBase(storage)
    restarted.now_utc = interrupted.now_utc
    restarted.midnight_utc = interrupted.midnight_utc
    activate(restarted)
    queried = []
    sent_after_restart = []

    def query(command_id):
        queried.append(command_id)
        return cancellation_applied(cancellation_commands[0])

    def send(command):
        sent_after_restart.append(command)
        return applied(command)

    install_bindings(
        restarted,
        storage,
        send,
        fence=11,
        expiry=expiry,
        query=query,
    )
    decision = before_legacy_plan(restarted)

    assert decision.state == RESULT_APPLIED
    assert decision.allow_legacy is False
    assert queried == [cancellation_commands[0].command_id]
    assert len(sent_after_restart) == 1
    assert "replacement" in sent_after_restart[0].to_wire()["schedule_transition"]


def test_corrupt_installed_authority_never_reopens_live_legacy_gate():
    """A corrupt restart snapshot is a write barrier, not evidence of release."""
    storage = FakeStorage()
    initial = FakeBase(storage)
    activate(initial)
    install_bindings(initial, storage, applied)
    assert before_legacy_plan(initial).state == RESULT_APPLIED
    storage.values[("lattice_schedule_authority", "schedule")]["installed"]["applied_plan_digest"] = "corrupt"

    restarted = FakeBase(storage)
    decision = before_legacy_plan(restarted)

    assert decision.state == RESULT_UNKNOWN
    assert decision.allow_legacy is False
    assert get_schedule_coordinator(restarted).gate.allowed is False
    assert "corrupt" in decision.detail


def test_marker_load_failure_is_fail_closed_when_durable_state_is_unknowable():
    storage = FakeStorage()
    storage.fail_loads = True
    restarted = FakeBase(storage)

    decision = before_legacy_plan(restarted)

    assert decision.mode == MODE_OFF
    assert decision.allow_legacy is False
    assert "could not be loaded" in decision.detail


def test_unknown_replacement_preserves_prior_installed_schedule_suppression():
    storage = FakeStorage()
    base = FakeBase(storage)
    activate(base)
    install_bindings(base, storage, applied)
    assert before_legacy_plan(base).state == RESULT_APPLIED

    base.charge_limit_best = [9.0]
    base._lattice_schedule_coordinator = LatticeScheduleCoordinator(base)
    install_bindings(base, storage, unknown, fence=11)
    replacement = before_legacy_plan(base)

    assert replacement.allow_legacy is False
    assert replacement.state == RESULT_UNKNOWN
    durable = storage.values[("lattice_schedule_authority", "schedule")]
    assert durable["installed"]["state"] == RESULT_APPLIED


def test_rejected_replacement_preserves_prior_installed_schedule_suppression():
    storage = FakeStorage()
    base = FakeBase(storage)
    activate(base)
    install_bindings(base, storage, applied)
    assert before_legacy_plan(base).state == RESULT_APPLIED

    base.charge_limit_best = [9.0]
    base._lattice_schedule_coordinator = LatticeScheduleCoordinator(base)
    install_bindings(base, storage, rejected, fence=11)
    replacement = before_legacy_plan(base)

    assert replacement.allow_legacy is False
    assert replacement.state == RESULT_NOT_APPLIED
    durable = storage.values[("lattice_schedule_authority", "schedule")]
    assert durable["installed"]["state"] == RESULT_APPLIED


def test_execute_and_balance_stop_at_shared_gate_before_touching_writer_state():
    base = FakeBase()
    activate(base)

    result = Execute.execute_plan(base)
    balanced = Execute.balance_inverters(base)

    assert result[0] == "Lattice schedule"
    assert balanced is False
    assert base.status_calls == [(False, False, True)]


def test_gateway_plan_republish_and_commands_share_closed_gate():
    base = FakeBase()
    activate(base)
    gateway = GatewayMQTT.__new__(GatewayMQTT)
    gateway.base = base
    gateway.log = base.log
    gateway._mqtt_connected = True
    gateway._mqtt_client = SimpleNamespace(publish=AsyncMock())
    gateway.topic_schedule = "device/schedule"
    gateway.topic_command = "device/command"
    gateway._last_published_plan = None
    gateway._pending_plan = None
    gateway._plan_version = 0
    gateway._last_plan_entries = [{"mode": 1}]
    gateway._last_plan_timezone = "UTC"
    gateway._last_plan_publish_time = 0
    gateway._last_plan_data = None
    gateway._command_id = 0
    gateway.build_execution_plan = lambda *_args, **_kwargs: b"legacy-plan"
    gateway.build_command = lambda *_args, **_kwargs: "{}"

    asyncio.run(gateway.publish_plan([{"mode": 1}], "UTC"))
    asyncio.run(gateway._republish_plan_if_stale())
    asyncio.run(gateway.publish_command("set_charge_rate", power_w=3000))

    gateway._mqtt_client.publish.assert_not_awaited()
    assert gateway._command_id == 0


def test_async_writer_wait_does_not_block_event_loop_and_releases_in_order():
    """A queued Gateway-style writer waits in an executor while the loop runs."""
    base = FakeBase()
    entered = asyncio.Event()
    release = asyncio.Event()
    order = []

    @lattice_async_writer("test async legacy writer")
    async def blocking_writer(subject, name):
        order.append(("enter", name))
        if name == "first":
            entered.set()
            await release.wait()
        order.append(("exit", name))

    async def scenario():
        first = asyncio.create_task(blocking_writer(base, "first"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        second = asyncio.create_task(blocking_writer(base, "second"))
        await asyncio.sleep(0.02)
        assert not second.done()
        assert order == [("enter", "first")]
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=2)

    asyncio.run(scenario())

    assert order == [
        ("enter", "first"),
        ("exit", "first"),
        ("enter", "second"),
        ("exit", "second"),
    ]
    assert get_schedule_coordinator(base).gate.active_writers == 0


def test_complete_plan_adapter_rejects_incomplete_limit_arrays():
    base = FakeBase()
    base.charge_limit_best = []
    try:
        build_complete_plan(base)
    except Exception as exc:
        assert "incomplete" in str(exc)
    else:
        raise AssertionError("incomplete optimizer plan was accepted")
