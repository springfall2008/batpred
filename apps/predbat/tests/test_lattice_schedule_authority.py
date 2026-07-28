"""Tests for pure v0.4 plan compilation and durable schedule authority."""

# cspell:ignore READBACK unsuppress

import asyncio
import copy
import os
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_schedule_authority import (
    EXECUTION_NATIVE,
    FALLBACK_BLOCKED,
    FALLBACK_SAFE,
    RESULT_APPLIED,
    RESULT_NOT_APPLIED,
    RESULT_UNKNOWN,
    VERIFICATION_ACCEPTED_ONLY,
    VERIFICATION_DURABLE_STORE_READBACK,
    AbsoluteScheduleIntent,
    AbsoluteScheduleSlot,
    CompletePlan,
    LatticeScheduleAuthority,
    PlanWindow,
    ResolvedScheduleOffer,
    ScheduleAck,
    ScheduleClamp,
    ScheduleLease,
    ScheduleTarget,
    ScheduleValidationError,
    compile_absolute_schedule,
)


UTC = timezone.utc
SCOPE = "site:GW-1:battery.mode"
ALTITUDE = "site:GW-1"
ORIGIN = datetime(2026, 7, 27, 22, 30, tzinfo=UTC)
NOW_MS = int((ORIGIN - timedelta(minutes=5)).timestamp() * 1000)


def plan(windows, horizon=180, timezone_name="Europe/London", default_mode="self_use", origin=ORIGIN, complete=True):
    """Build a complete normalized plan fixture."""
    return CompletePlan(
        altitude_id=ALTITUDE,
        timeline_start_utc=origin,
        horizon_minutes=horizon,
        diagnostic_timezone=timezone_name,
        windows=tuple(windows),
        default_mode=default_mode,
        execution=EXECUTION_NATIVE,
        is_complete=complete,
    )


def window(start, end, mode, altitude=ALTITUDE, **kwargs):
    """Build one normalized plan window fixture."""
    return PlanWindow(altitude, start, end, mode, **kwargs)


def target(**overrides):
    """Build exact provider-local topology coordinates."""
    resolved_offer = ResolvedScheduleOffer(
        max_slots=8,
        supported_modes=("self_use", "force_charge", "force_export", "hold"),
        slot_fields=("target_soc", "reserve_soc", "charge_power_limit", "discharge_power_limit", "enable"),
        execution_modes=("NATIVE", "CONTROLLER_STEPPED"),
        gap_policy="DEFAULT_MODE",
    )
    value = {
        "provider": "predbat-gateway",
        "node_id": "GW-1",
        "access_path": "gw-local",
        "topology_version": "0.4.0",
        "doc_version": 12,
        "cap_ref": 41,
        "altitude_id": ALTITUDE,
        "scope_id": SCOPE,
        "offer": resolved_offer,
        "owned_node_ids": ("GW-1",),
    }
    value.update(overrides)
    return ScheduleTarget(**value)


def lease(fence=10, expiry=NOW_MS + 3_600_000, **overrides):
    """Build one currently valid controller lease."""
    value = {
        "origin_id": "predbat",
        "controller_class": "AUTOMATION",
        "lease_id": "lease-a",
        "scope_id": SCOPE,
        "fencing_token": fence,
        "expires_ts_ms": expiry,
    }
    value.update(overrides)
    return ScheduleLease(**value)


def applied(command, verified=True, **overrides):
    """Build an exact durably accepted schedule result."""
    value = {
        "command_id": command.command_id,
        "state": RESULT_APPLIED,
        "scope_id": command.lease.scope_id,
        "fencing_token": command.lease.fencing_token,
        "lease_id": command.lease.lease_id,
        "lease_expires_ts_ms": command.lease.expires_ts_ms,
        "origin_id": command.lease.origin_id,
        "controller_class": command.lease.controller_class,
        "plan_digest": command.schedule.plan_digest,
        "accepted_slot_count": len(command.schedule.slots),
        "durably_accepted": True,
        "valid_from_ts_ms": command.schedule.valid_from_ts_ms,
        "valid_until_ts_ms": command.schedule.valid_until_ts_ms,
        "verification": VERIFICATION_DURABLE_STORE_READBACK if verified else VERIFICATION_ACCEPTED_ONLY,
        "verified": verified,
    }
    value.update(overrides)
    return ScheduleAck(**value)


def cancellation_applied(command, **overrides):
    """Build exact durable/native proof that cancellation released authority."""
    value = {
        "command_id": command.command_id,
        "state": RESULT_APPLIED,
        "scope_id": command.lease.scope_id,
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


def rejected(command, fallback=FALLBACK_SAFE, reason="UNSUPPORTED_OFFER", **overrides):
    """Build one definite NOT_APPLIED schedule result."""
    value = {
        "command_id": command.command_id,
        "state": RESULT_NOT_APPLIED,
        "scope_id": command.lease.scope_id,
        "fencing_token": command.lease.fencing_token,
        "reason": reason,
        "fallback": fallback,
        "detail": "schedule was not applied",
    }
    value.update(overrides)
    return ScheduleAck(**value)


def unknown(command, **overrides):
    """Build one ambiguous result that must quarantine its scope."""
    value = {
        "command_id": command.command_id,
        "state": RESULT_UNKNOWN,
        "scope_id": command.lease.scope_id,
        "fencing_token": command.lease.fencing_token,
        "reason": "EXECUTION_AMBIGUOUS",
        "fallback": FALLBACK_BLOCKED,
        "detail": "write completion is ambiguous",
    }
    value.update(overrides)
    return ScheduleAck(**value)


class FakeStorage:
    """In-memory asynchronous storage with controllable save failures."""

    def __init__(self, snapshot=None, fail_saves=None, raise_load=False, raise_saves=None):
        """Create a store with optional durable state and failing save numbers."""
        self.snapshot = copy.deepcopy(snapshot)
        self.fail_saves = set(fail_saves or ())
        self.raise_load = raise_load
        self.raise_saves = set(raise_saves or ())
        self.save_count = 0

    async def load(self, module, filename):
        """Return a defensive snapshot copy."""
        if self.raise_load:
            raise OSError("load failed")
        return copy.deepcopy(self.snapshot)

    async def save(self, module, filename, data, **kwargs):
        """Persist a defensive copy unless this save is scripted to fail."""
        self.save_count += 1
        if self.save_count in self.raise_saves:
            raise OSError("save failed")
        if self.save_count in self.fail_saves:
            return False
        self.snapshot = copy.deepcopy(data)
        return True


class Harness:
    """Wire the authority to scripted send and ledger-query seams."""

    def __init__(self, send_results=(), query_results=(), storage=None, command_id_factory=None):
        """Create a deterministic harness."""
        self.send_results = list(send_results)
        self.query_results = list(query_results)
        self.storage = storage or FakeStorage()
        self.sent = []
        self.queries = []
        self.authority = LatticeScheduleAuthority(
            self.storage,
            self.send,
            self.query,
            SCOPE,
            command_id_factory=command_id_factory or (lambda: "cmd-1"),
        )

    async def send(self, command):
        """Capture a command and return the next scripted result."""
        self.sent.append(command)
        result = self.send_results.pop(0) if self.send_results else None
        return result(command) if callable(result) else result

    async def query(self, command_id):
        """Capture a ledger lookup and return the next scripted result."""
        self.queries.append(command_id)
        result = self.query_results.pop(0) if self.query_results else None
        if callable(result):
            command = self.sent[-1] if self.sent else None
            return result(command)
        return result


class TestAbsoluteScheduleCompiler(unittest.TestCase):
    """Exercise calendar-correct atomic conversion."""

    def test_midnight_and_multiple_days_remain_distinct_utc_instants(self):
        """Elapsed offsets cross midnight and day two without HH:MM aliasing."""
        compiled = compile_absolute_schedule(
            plan(
                [
                    window(0, 90, "force_charge", target_soc=90),
                    window(1500, 1530, "force_export", reserve_soc=20),
                ],
                horizon=1560,
            ),
            ORIGIN,
        )
        self.assertEqual(compiled.slots[0].start_ts_ms, int(ORIGIN.timestamp() * 1000))
        self.assertEqual(compiled.slots[0].end_ts_ms, int((ORIGIN + timedelta(minutes=90)).timestamp() * 1000))
        self.assertEqual(compiled.slots[1].start_ts_ms, int((ORIGIN + timedelta(minutes=1500)).timestamp() * 1000))
        self.assertGreater(compiled.slots[1].start_ts_ms, compiled.slots[0].start_ts_ms + 24 * 60 * 60 * 1000)

    def test_dst_timezone_is_diagnostic_only(self):
        """The London spring gap cannot reinterpret producer-resolved UTC offsets."""
        spring = datetime(2026, 3, 29, 0, 0, tzinfo=UTC)
        london = compile_absolute_schedule(plan([window(60, 120, "force_charge")], horizon=180, origin=spring), spring)
        utc = compile_absolute_schedule(plan([window(60, 120, "force_charge")], horizon=180, timezone_name="UTC", origin=spring), spring)
        self.assertEqual(london.slots[0].start_ts_ms, utc.slots[0].start_ts_ms)
        self.assertEqual(london.slots[0].end_ts_ms, utc.slots[0].end_ts_ms)
        self.assertNotEqual(london.plan_digest, utc.plan_digest)

    def test_adjacent_identical_slots_coalesce_and_preserve_zero_false(self):
        """Lossless coalescing reduces slot pressure without dropping presence."""
        compiled = compile_absolute_schedule(
            plan(
                [
                    window(0, 30, "force_charge", charge_power_limit=0, enable=False),
                    window(30, 60, "force_charge", charge_power_limit=0, enable=False),
                ],
                horizon=60,
                default_mode=None,
            ),
            ORIGIN,
        )
        self.assertEqual(len(compiled.slots), 1)
        self.assertEqual(compiled.slots[0].to_wire()["charge_power_limit"], 0)
        self.assertIs(compiled.slots[0].to_wire()["enable"], False)
        self.assertNotIn("default_mode", compiled.to_wire())

    def test_overlap_rejects_the_entire_plan(self):
        """No partial or last-writer-wins schedule escapes an overlap."""
        with self.assertRaisesRegex(ScheduleValidationError, "overlaps"):
            compile_absolute_schedule(plan([window(0, 60, "force_charge"), window(30, 90, "force_export")]), ORIGIN)

    def test_gap_requires_an_explicit_default_mode(self):
        """Uncovered validity is never assigned an implicit receiver behavior."""
        with self.assertRaisesRegex(ScheduleValidationError, "gaps require"):
            compile_absolute_schedule(plan([window(30, 60, "force_charge")], default_mode=None), ORIGIN)

    def test_windows_cannot_cross_topology_altitudes(self):
        """A plan cannot accidentally dispatch controls at two graph levels."""
        with self.assertRaisesRegex(ScheduleValidationError, "crosses topology altitude"):
            compile_absolute_schedule(plan([window(0, 60, "force_charge", altitude="site:other")]), ORIGIN)

    def test_partial_and_expired_plans_are_rejected(self):
        """Only a complete future-bearing plan can become an atomic command."""
        with self.assertRaisesRegex(ScheduleValidationError, "partial"):
            compile_absolute_schedule(plan([], complete=False), ORIGIN)
        with self.assertRaisesRegex(ScheduleValidationError, "expired"):
            compile_absolute_schedule(plan([], origin=ORIGIN - timedelta(hours=4)), ORIGIN)

    def test_naive_time_and_invalid_timezone_are_rejected(self):
        """Invalid time inputs fail closed before any epoch conversion."""
        with self.assertRaisesRegex(ScheduleValidationError, "aware UTC"):
            compile_absolute_schedule(plan([], origin=ORIGIN.replace(tzinfo=None)), ORIGIN)
        with self.assertRaisesRegex(ScheduleValidationError, "aware UTC"):
            compile_absolute_schedule(plan([], origin=datetime(2026, 1, 1, tzinfo=ZoneInfo("Europe/London"))), ORIGIN)
        with self.assertRaisesRegex(ScheduleValidationError, "IANA"):
            compile_absolute_schedule(plan([], timezone_name="../London"), ORIGIN)

    def test_more_than_embedded_slot_limit_is_rejected(self):
        """The compiler never truncates a complete plan to the embedded limit."""
        windows = [window(index * 10, index * 10 + 5, "force_charge", target_soc=index) for index in range(9)]
        with self.assertRaisesRegex(ScheduleValidationError, "embedded maximum"):
            compile_absolute_schedule(plan(windows, horizon=100), ORIGIN)


class TestLatticeScheduleAuthority(unittest.TestCase):
    """Exercise durable dispatch, reconciliation, and legacy decisions."""

    def setUp(self):
        """Compile one representative schedule for each authority test."""
        self.schedule = compile_absolute_schedule(
            plan([window(0, 60, "force_charge", target_soc=90), window(60, 120, "self_use")], horizon=120, default_mode=None),
            ORIGIN - timedelta(minutes=5),
        )

    def dispatch(self, harness, **kwargs):
        """Run one deterministic dispatch."""
        return asyncio.run(harness.authority.dispatch(self.schedule, kwargs.get("target", target()), kwargs.get("lease", lease()), kwargs.get("now_ts_ms", NOW_MS), kwargs.get("command_id")))

    def test_applied_suppresses_legacy_only_after_local_durable_save(self):
        """A durably accepted receipt plus local persistence activates suppression."""
        harness = Harness(send_results=[applied])
        decision = self.dispatch(harness)
        self.assertEqual(decision.state, RESULT_APPLIED)
        self.assertTrue(decision.legacy_suppressed)
        self.assertFalse(decision.fallback_allowed)
        durable = asyncio.run(harness.authority.legacy_decision(NOW_MS))
        self.assertTrue(durable.legacy_suppressed)
        self.assertTrue(durable.durable_state_present)
        self.assertEqual(len(harness.sent), 1)

    def test_applied_receipt_save_failure_never_allows_fallback(self):
        """Physical acceptance with failed local persistence remains ambiguous."""
        harness = Harness(send_results=[applied], storage=FakeStorage(fail_saves={2}))
        decision = self.dispatch(harness)
        self.assertEqual(decision.state, RESULT_UNKNOWN)
        self.assertTrue(decision.legacy_suppressed)
        self.assertFalse(decision.fallback_allowed)
        self.assertTrue(decision.quarantined)

    def test_clamped_applied_receipt_keeps_requested_and_applied_digests(self):
        """A valid clamp may change the receiver's canonical applied digest."""
        applied_digest = "a" * 64
        clamp = ScheduleClamp(0, "TARGET_SOC", 101.0, 100.0)
        harness = Harness(
            send_results=[
                lambda command: applied(
                    command,
                    plan_digest=applied_digest,
                    clamps=(clamp,),
                    clamp_count=1,
                    clamps_truncated=False,
                )
            ]
        )
        decision = self.dispatch(harness)
        self.assertEqual(decision.state, RESULT_APPLIED)
        self.assertTrue(decision.legacy_suppressed)
        self.assertEqual(harness.storage.snapshot["command"]["plan_digest"], self.schedule.plan_digest)
        self.assertEqual(harness.storage.snapshot["command"]["applied_plan_digest"], applied_digest)
        self.assertEqual(harness.storage.snapshot["command"]["clamps"][0]["field"], "TARGET_SOC")

    def test_malformed_applied_receipts_quarantine(self):
        """Invalid digest, verification, or clamp invariants never suppress legacy."""
        valid_clamp = ScheduleClamp(0, "TARGET_SOC", 101.0, 100.0)
        mutations = [
            {"plan_digest": "bad"},
            {"verification": "MAGIC"},
            {"verification": VERIFICATION_ACCEPTED_ONLY, "verified": True},
            {"clamps": [valid_clamp], "clamp_count": 1},
            {"clamps": (valid_clamp,), "clamp_count": 1, "clamps_truncated": True},
            {"clamps": (ScheduleClamp(2, "TARGET_SOC", 101.0, 100.0),), "clamp_count": 1},
            {"clamps": (ScheduleClamp(0, "MAGIC", 101.0, 100.0),), "clamp_count": 1},
            {"clamps": (ScheduleClamp(0, "TARGET_SOC", float("nan"), 100.0),), "clamp_count": 1},
            {
                "clamps": tuple(ScheduleClamp(0, "TARGET_SOC", 101.0, 100.0) for _ in range(9)),
                "clamp_count": 9,
                "clamps_truncated": False,
            },
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                harness = Harness(send_results=[lambda command, mutation=mutation: applied(command, **mutation)], query_results=[None])
                decision = self.dispatch(harness)
                self.assertEqual(decision.state, RESULT_UNKNOWN)
                self.assertTrue(decision.quarantined)
                self.assertTrue(decision.legacy_suppressed)
                self.assertFalse(decision.fallback_allowed)

    def test_truncated_clamp_detail_list_is_valid(self):
        """A bounded receipt may report fewer details than its total clamp count."""
        clamp = ScheduleClamp(0, "TARGET_SOC", 101.0, 100.0)
        harness = Harness(
            send_results=[
                lambda command: applied(
                    command,
                    plan_digest="b" * 64,
                    clamps=(clamp,),
                    clamp_count=3,
                    clamps_truncated=True,
                )
            ]
        )
        decision = self.dispatch(harness)
        self.assertEqual(decision.state, RESULT_APPLIED)
        self.assertTrue(decision.legacy_suppressed)
        self.assertEqual(harness.storage.snapshot["command"]["clamp_count"], 3)
        self.assertTrue(harness.storage.snapshot["command"]["clamps_truncated"])

    def test_only_fallback_safe_not_applied_allows_fallback(self):
        """Definite SAFE rejection is the sole fallback boundary."""
        safe = Harness(send_results=[lambda command: rejected(command, FALLBACK_SAFE)])
        blocked = Harness(send_results=[lambda command: rejected(command, FALLBACK_BLOCKED, "STALE_FENCE")])
        self.assertTrue(self.dispatch(safe).fallback_allowed)
        self.assertFalse(self.dispatch(blocked).fallback_allowed)

    def test_rejected_replacement_cannot_unsuppress_installed_schedule(self):
        """A failed replacement does not prove the prior device plan stopped."""
        first = Harness(send_results=[applied])
        self.assertEqual(self.dispatch(first).state, RESULT_APPLIED)
        changed = compile_absolute_schedule(
            plan([window(0, 30, "force_export")], horizon=120),
            ORIGIN - timedelta(minutes=5),
        )
        replacement = Harness(storage=first.storage, send_results=[lambda command: rejected(command, FALLBACK_SAFE)])
        decision = asyncio.run(replacement.authority.dispatch(changed, target(), lease(fence=11), NOW_MS, "cmd-2"))
        self.assertEqual(decision.state, RESULT_NOT_APPLIED)
        self.assertTrue(decision.legacy_suppressed)
        self.assertFalse(decision.fallback_allowed)
        self.assertEqual(first.storage.snapshot["installed"]["command_id"], "cmd-1")
        self.assertEqual(first.storage.snapshot["command"]["command_id"], "cmd-2")

    def test_unknown_quarantines_and_never_allows_fallback(self):
        """UNKNOWN remains an explicit double-write barrier."""
        harness = Harness(send_results=[unknown])
        decision = self.dispatch(harness)
        self.assertEqual(decision.state, RESULT_UNKNOWN)
        self.assertTrue(decision.quarantined)
        self.assertFalse(decision.fallback_allowed)
        self.assertTrue(decision.legacy_suppressed)

    def test_lost_result_reconciles_by_id_without_republishing(self):
        """An absent immediate result performs one ledger lookup only."""
        harness = Harness(send_results=[None], query_results=[applied])
        decision = self.dispatch(harness)
        self.assertEqual(decision.state, RESULT_APPLIED)
        self.assertEqual(len(harness.sent), 1)
        self.assertEqual(harness.queries, ["cmd-1"])

    def test_restart_reconciles_pending_without_sending_again(self):
        """A new authority instance resumes the durable command by ID."""
        first = Harness(send_results=[None], query_results=[None])
        self.assertEqual(self.dispatch(first).state, RESULT_UNKNOWN)
        restarted = Harness(storage=first.storage, query_results=[lambda _command: applied(first.sent[0])])
        decision = asyncio.run(restarted.authority.reconcile(NOW_MS))
        self.assertEqual(decision.state, RESULT_APPLIED)
        self.assertEqual(restarted.sent, [])
        self.assertEqual(restarted.queries, ["cmd-1"])

    def test_identical_duplicate_result_is_idempotent(self):
        """Two byte-equivalent logical results do not create a conflict."""
        harness = Harness()

        async def send(command):
            """Deliver the same terminal result twice."""
            result = applied(command)
            self.assertTrue(harness.authority.accept_result(result))
            self.assertTrue(harness.authority.accept_result(result))

        harness._send = send
        harness.authority._send = send
        decision = self.dispatch(harness)
        self.assertEqual(decision.state, RESULT_APPLIED)

    def test_conflicting_duplicate_result_quarantines(self):
        """Two terminal claims for one command are treated as ambiguous."""
        harness = Harness()

        async def send(command):
            """Deliver mutually inconsistent terminal results."""
            self.assertTrue(harness.authority.accept_result(applied(command)))
            self.assertFalse(harness.authority.accept_result(unknown(command)))

        harness.authority._send = send
        decision = self.dispatch(harness)
        self.assertEqual(decision.state, RESULT_UNKNOWN)
        self.assertTrue(decision.quarantined)

    def test_result_digest_scope_fence_and_validity_mismatch_quarantine(self):
        """Every applied receipt coordinate must match the admitted command."""
        mutations = [
            {"scope_id": "site:other"},
            {"fencing_token": 11},
            {"lease_id": "lease-other"},
            {"lease_expires_ts_ms": NOW_MS + 7_200_000},
            {"origin_id": "operator"},
            {"controller_class": "MANUAL"},
            {"valid_until_ts_ms": 1},
            {"accepted_slot_count": 1},
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                harness = Harness(send_results=[lambda command, mutation=mutation: applied(command, **mutation)], query_results=[None])
                decision = self.dispatch(harness)
                self.assertEqual(decision.state, RESULT_UNKNOWN)
                self.assertFalse(decision.fallback_allowed)

    def test_rejection_coordinates_may_be_absent_but_not_conflicting(self):
        """Optional rejection echoes are checked whenever the protobuf carries them."""
        absent = Harness(
            send_results=[
                lambda command: rejected(
                    command,
                    fencing_token=0,
                    lease_id="",
                    lease_expires_ts_ms=0,
                    origin_id="",
                    controller_class="",
                )
            ]
        )
        conflict = Harness(send_results=[lambda command: rejected(command, lease_id="lease-other")], query_results=[None])
        blocked_claims_safe = Harness(
            send_results=[lambda command: rejected(command, FALLBACK_SAFE, "STALE_FENCE")],
            query_results=[None],
        )
        self.assertTrue(self.dispatch(absent).fallback_allowed)
        self.assertEqual(self.dispatch(conflict).state, RESULT_UNKNOWN)
        self.assertEqual(self.dispatch(blocked_claims_safe).state, RESULT_UNKNOWN)

    def test_stale_fence_is_blocked_before_transport(self):
        """A lower token cannot follow a durable high-water token."""
        first = Harness(send_results=[applied])
        self.assertEqual(self.dispatch(first, lease=lease(fence=10)).state, RESULT_APPLIED)
        second = Harness(storage=first.storage, send_results=[applied])
        changed = compile_absolute_schedule(plan([window(0, 30, "force_export")], horizon=120), ORIGIN - timedelta(minutes=5))
        decision = asyncio.run(second.authority.dispatch(changed, target(), lease(fence=9), NOW_MS, "cmd-2"))
        self.assertEqual(decision.state, RESULT_NOT_APPLIED)
        self.assertEqual(decision.detail, "STALE_FENCE")
        self.assertFalse(decision.fallback_allowed)
        self.assertEqual(second.sent, [])

    def test_same_plan_and_fence_is_not_resent(self):
        """A repeated accepted intent reuses durable acceptance after restart."""
        first = Harness(send_results=[applied])
        self.assertEqual(self.dispatch(first).state, RESULT_APPLIED)
        restarted = Harness(storage=first.storage, send_results=[applied])
        decision = self.dispatch(restarted, command_id="cmd-2")
        self.assertTrue(decision.legacy_suppressed)
        self.assertEqual(restarted.sent, [])

    def test_command_id_reuse_with_different_plan_quarantines(self):
        """One command ID can never identify two plan digests."""
        first = Harness(send_results=[applied])
        self.assertEqual(self.dispatch(first).state, RESULT_APPLIED)
        changed = compile_absolute_schedule(plan([window(0, 30, "force_export")], horizon=120), ORIGIN - timedelta(minutes=5))
        restarted = Harness(storage=first.storage, send_results=[applied])
        decision = asyncio.run(restarted.authority.dispatch(changed, target(), lease(), NOW_MS, "cmd-1"))
        self.assertEqual(decision.state, RESULT_UNKNOWN)
        self.assertEqual(restarted.sent, [])

    def test_same_command_id_with_changed_fence_quarantines(self):
        """Command identity includes the accepted fence, not only the plan digest."""
        first = Harness(send_results=[applied])
        self.assertEqual(self.dispatch(first).state, RESULT_APPLIED)
        restarted = Harness(storage=first.storage, send_results=[applied])
        decision = self.dispatch(restarted, lease=lease(fence=11), command_id="cmd-1")
        self.assertEqual(decision.state, RESULT_UNKNOWN)
        self.assertEqual(restarted.sent, [])

    def test_same_command_id_with_changed_owned_set_quarantines(self):
        """Command identity retains every provider-local node owned by the scope."""
        first = Harness(send_results=[applied])
        self.assertEqual(self.dispatch(first).state, RESULT_APPLIED)
        restarted = Harness(storage=first.storage, send_results=[applied])
        expanded = target(owned_node_ids=("GW-2", "GW-1"))
        decision = self.dispatch(restarted, target=expanded, command_id="cmd-1")
        self.assertEqual(decision.state, RESULT_UNKNOWN)
        self.assertTrue(decision.legacy_suppressed)
        self.assertEqual(restarted.sent, [])

    def test_expired_ownership_no_longer_suppresses_legacy(self):
        """Clock expiry requests an ending transition but cannot release authority."""
        first = Harness(send_results=[applied])
        self.assertEqual(self.dispatch(first).state, RESULT_APPLIED)
        expired = asyncio.run(first.authority.legacy_decision(self.schedule.valid_until_ts_ms))
        invalid_clock = asyncio.run(first.authority.legacy_decision(0))
        self.assertEqual(expired.state, RESULT_APPLIED)
        self.assertTrue(expired.legacy_suppressed)
        self.assertTrue(expired.ending_transition_required)
        self.assertFalse(expired.fallback_allowed)
        self.assertEqual(invalid_clock.state, RESULT_UNKNOWN)
        self.assertTrue(invalid_clock.quarantined)

    def test_guarded_verified_cancellation_is_the_only_authority_release(self):
        """Matching durable readback proof ends exclusion after validity expiry."""
        first = Harness(send_results=[applied])
        self.assertEqual(self.dispatch(first).state, RESULT_APPLIED)
        installed_digest = first.storage.snapshot["installed"]["applied_plan_digest"]

        ending = Harness(storage=first.storage, send_results=[cancellation_applied])
        decision = asyncio.run(
            ending.authority.cancel(
                target(),
                lease(fence=11, expiry=self.schedule.valid_until_ts_ms + 3_600_000),
                installed_digest,
                "VALIDITY_END",
                self.schedule.valid_until_ts_ms,
                "cmd-end",
            )
        )

        self.assertEqual(decision.state, RESULT_APPLIED)
        self.assertFalse(decision.legacy_suppressed)
        self.assertFalse(decision.ending_transition_required)
        self.assertFalse(decision.fallback_allowed)
        self.assertIsNone(ending.storage.snapshot["installed"])
        restarted = Harness(storage=ending.storage)
        self.assertFalse(asyncio.run(restarted.authority.legacy_decision(self.schedule.valid_until_ts_ms + 1)).legacy_suppressed)
        self.assertEqual(ending.sent[0].to_wire()["schedule_transition"]["cancellation"]["expected_plan_digest"], installed_digest)

    def test_applied_replacement_holds_exclusion_continuously_after_expiry(self):
        """A definite replacement swaps the installed digest without a write gap."""
        first = Harness(send_results=[applied])
        self.assertEqual(self.dispatch(first).state, RESULT_APPLIED)
        transition_now = self.schedule.valid_until_ts_ms
        new_origin = datetime.fromtimestamp(transition_now / 1000, UTC)
        replacement_schedule = compile_absolute_schedule(
            plan(
                [window(0, 60, "force_export")],
                horizon=120,
                origin=new_origin,
            ),
            new_origin,
        )
        replacement = Harness(storage=first.storage)

        async def send_with_old_authority_reserved(command):
            """Observe that reservation preserves the old installed authority."""
            self.assertEqual(replacement.storage.snapshot["installed"]["command_id"], "cmd-1")
            return applied(command)

        replacement.authority._send = send_with_old_authority_reserved
        decision = asyncio.run(
            replacement.authority.dispatch(
                replacement_schedule,
                target(),
                lease(fence=11, expiry=transition_now + 3_600_000),
                transition_now,
                "cmd-replace",
            )
        )
        self.assertEqual(decision.state, RESULT_APPLIED)
        self.assertTrue(decision.legacy_suppressed)
        self.assertFalse(decision.ending_transition_required)
        self.assertEqual(
            replacement.storage.snapshot["installed"]["command_id"],
            "cmd-replace",
        )

    def test_bare_or_mismatched_applied_cancellation_cannot_release(self):
        """APPLIED without exact durable/native release evidence quarantines."""
        mutations = [
            {"cancelled_plan_digest": "f" * 64},
            {"writer_exclusion_released": False},
            {
                "verification": VERIFICATION_ACCEPTED_ONLY,
                "verified": False,
            },
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                first = Harness(send_results=[applied])
                self.assertEqual(self.dispatch(first).state, RESULT_APPLIED)
                installed_digest = first.storage.snapshot["installed"]["applied_plan_digest"]
                ending = Harness(
                    storage=first.storage,
                    send_results=[lambda command, mutation=mutation: cancellation_applied(command, **mutation)],
                )
                decision = asyncio.run(
                    ending.authority.cancel(
                        target(),
                        lease(fence=11),
                        installed_digest,
                        "OPERATOR",
                        NOW_MS,
                        "cmd-end",
                    )
                )
                self.assertEqual(decision.state, RESULT_UNKNOWN)
                self.assertTrue(decision.quarantined)
                self.assertTrue(decision.legacy_suppressed)
                self.assertEqual(
                    ending.storage.snapshot["installed"]["applied_plan_digest"],
                    installed_digest,
                )

        first = Harness(send_results=[applied])
        self.assertEqual(self.dispatch(first).state, RESULT_APPLIED)
        installed_digest = first.storage.snapshot["installed"]["applied_plan_digest"]
        ending = Harness(
            storage=first.storage,
            send_results=[
                lambda command: ScheduleAck(
                    command_id=command.command_id,
                    state=RESULT_APPLIED,
                    scope_id=command.lease.scope_id,
                )
            ],
        )
        bare = asyncio.run(
            ending.authority.cancel(
                target(),
                lease(fence=11),
                installed_digest,
                "OPERATOR",
                NOW_MS,
                "cmd-bare",
            )
        )
        self.assertEqual(bare.state, RESULT_UNKNOWN)
        self.assertTrue(bare.legacy_suppressed)
        self.assertIsNotNone(ending.storage.snapshot["installed"])

    def test_cancellation_digest_guard_rejects_before_transport(self):
        """A delayed cancellation cannot remove a different installed digest."""
        first = Harness(send_results=[applied])
        self.assertEqual(self.dispatch(first).state, RESULT_APPLIED)
        ending = Harness(storage=first.storage, send_results=[cancellation_applied])
        decision = asyncio.run(
            ending.authority.cancel(
                target(),
                lease(fence=11),
                "e" * 64,
                "SUPERSEDED",
                NOW_MS,
                "cmd-stale-cancel",
            )
        )
        self.assertEqual(decision.state, RESULT_NOT_APPLIED)
        self.assertEqual(decision.detail, "PLAN_DIGEST_MISMATCH")
        self.assertTrue(decision.legacy_suppressed)
        self.assertEqual(ending.sent, [])

    def test_cancellation_requires_the_installed_gateway_route(self):
        """Provider/access hints cannot redirect an authority-ending command."""
        for mutation in (
            {"provider": "cloud-provider"},
            {"access_path": "cloud-api"},
        ):
            with self.subTest(mutation=mutation):
                first = Harness(send_results=[applied])
                self.assertEqual(self.dispatch(first).state, RESULT_APPLIED)
                installed_digest = first.storage.snapshot["installed"]["applied_plan_digest"]
                ending = Harness(storage=first.storage, send_results=[cancellation_applied])
                decision = asyncio.run(
                    ending.authority.cancel(
                        target(**mutation),
                        lease(fence=11),
                        installed_digest,
                        "OPERATOR",
                        NOW_MS,
                        "cmd-end",
                    )
                )
                self.assertEqual(decision.state, RESULT_NOT_APPLIED)
                self.assertTrue(decision.legacy_suppressed)
                self.assertEqual(decision.detail, "unsafe topology cancellation target")
                self.assertEqual(ending.sent, [])

    def test_unknown_quarantine_timestamp_is_durable_and_paired(self):
        """UNKNOWN keeps its first quarantine time and corrupt pairs fail closed."""
        ambiguous = Harness(send_results=[unknown])
        decision = self.dispatch(ambiguous)
        self.assertEqual(decision.state, RESULT_UNKNOWN)
        self.assertEqual(ambiguous.storage.snapshot["quarantine_since_ts_ms"], NOW_MS)

        restarted = Harness(storage=ambiguous.storage, query_results=[None])
        reconciled = asyncio.run(restarted.authority.reconcile(NOW_MS + 1))
        self.assertEqual(reconciled.state, RESULT_UNKNOWN)
        self.assertEqual(restarted.storage.snapshot["quarantine_since_ts_ms"], NOW_MS)

        missing_timestamp = copy.deepcopy(restarted.storage.snapshot)
        missing_timestamp["quarantine_since_ts_ms"] = None
        corrupt_unknown = Harness(storage=FakeStorage(missing_timestamp))
        corrupt_decision = asyncio.run(corrupt_unknown.authority.legacy_decision(NOW_MS + 2))
        self.assertEqual(corrupt_decision.state, RESULT_UNKNOWN)
        self.assertTrue(corrupt_decision.quarantined)
        self.assertTrue(corrupt_decision.legacy_suppressed)

        stray_timestamp = copy.deepcopy(restarted.storage.snapshot)
        stray_timestamp["command"]["state"] = RESULT_NOT_APPLIED
        stray_timestamp["command"]["fallback"] = FALLBACK_BLOCKED
        stray_timestamp["command"]["reason"] = "SCOPE_BUSY"
        corrupt_terminal = Harness(storage=FakeStorage(stray_timestamp))
        corrupt_decision = asyncio.run(corrupt_terminal.authority.legacy_decision(NOW_MS + 2))
        self.assertEqual(corrupt_decision.state, RESULT_UNKNOWN)
        self.assertTrue(corrupt_decision.quarantined)
        self.assertTrue(corrupt_decision.legacy_suppressed)

    def test_authority_release_rejections_are_always_blocked(self):
        """Cancellation release failures never accept a SAFE fallback claim."""
        first = Harness(send_results=[applied])
        self.assertEqual(self.dispatch(first).state, RESULT_APPLIED)
        installed_digest = first.storage.snapshot["installed"]["applied_plan_digest"]

        blocked = Harness(
            storage=first.storage,
            send_results=[
                lambda command: rejected(
                    command,
                    reason="AUTHORITY_RELEASE_UNCONFIRMED",
                    fallback=FALLBACK_BLOCKED,
                )
            ],
        )
        decision = asyncio.run(
            blocked.authority.cancel(
                target(),
                lease(fence=11),
                installed_digest,
                "OPERATOR",
                NOW_MS,
                "cmd-blocked",
            )
        )
        self.assertEqual(decision.state, RESULT_NOT_APPLIED)
        self.assertFalse(decision.quarantined)
        self.assertTrue(decision.legacy_suppressed)
        self.assertEqual(blocked.storage.snapshot["command"]["fallback"], FALLBACK_BLOCKED)

        unsafe = Harness(
            storage=blocked.storage,
            send_results=[
                lambda command: rejected(
                    command,
                    reason="PLAN_DIGEST_MISMATCH",
                    fallback=FALLBACK_SAFE,
                )
            ],
            query_results=[None],
        )
        decision = asyncio.run(
            unsafe.authority.cancel(
                target(),
                lease(fence=12),
                installed_digest,
                "OPERATOR",
                NOW_MS,
                "cmd-unsafe",
            )
        )
        self.assertEqual(decision.state, RESULT_UNKNOWN)
        self.assertTrue(decision.quarantined)
        self.assertTrue(decision.legacy_suppressed)
        self.assertEqual(unsafe.storage.snapshot["command"]["fallback"], FALLBACK_BLOCKED)

    def test_restart_reconciles_unknown_cancellation_without_republishing(self):
        """Power loss queries the exact cancellation ID and never sends twice."""
        first = Harness(send_results=[applied])
        self.assertEqual(self.dispatch(first).state, RESULT_APPLIED)
        installed_digest = first.storage.snapshot["installed"]["applied_plan_digest"]
        ending = Harness(
            storage=first.storage,
            send_results=[None],
            query_results=[None],
        )
        unknown_decision = asyncio.run(
            ending.authority.cancel(
                target(),
                lease(fence=11),
                installed_digest,
                "VALIDITY_END",
                NOW_MS,
                "cmd-end",
            )
        )
        self.assertEqual(unknown_decision.state, RESULT_UNKNOWN)
        self.assertTrue(unknown_decision.legacy_suppressed)
        self.assertEqual(len(ending.sent), 1)

        restarted = Harness(
            storage=ending.storage,
            query_results=[lambda _command: cancellation_applied(ending.sent[0])],
        )
        reconciled = asyncio.run(restarted.authority.reconcile(NOW_MS))
        self.assertEqual(reconciled.state, RESULT_APPLIED)
        self.assertFalse(reconciled.legacy_suppressed)
        self.assertEqual(restarted.sent, [])
        self.assertEqual(restarted.queries, ["cmd-end"])
        self.assertIsNone(restarted.storage.snapshot["installed"])

    def test_corrupt_pending_cancellation_snapshot_fails_closed(self):
        """A restored cancellation must still name the installed plan exactly."""
        first = Harness(send_results=[applied])
        self.assertEqual(self.dispatch(first).state, RESULT_APPLIED)
        installed_digest = first.storage.snapshot["installed"]["applied_plan_digest"]
        ending = Harness(
            storage=first.storage,
            send_results=[None],
            query_results=[None],
        )
        self.assertEqual(
            asyncio.run(
                ending.authority.cancel(
                    target(),
                    lease(fence=11),
                    installed_digest,
                    "VALIDITY_END",
                    NOW_MS,
                    "cmd-end",
                )
            ).state,
            RESULT_UNKNOWN,
        )
        ending.storage.snapshot["command"]["plan_digest"] = "d" * 64

        restarted = Harness(storage=ending.storage)
        decision = asyncio.run(restarted.authority.legacy_decision(NOW_MS))
        self.assertEqual(decision.state, RESULT_UNKNOWN)
        self.assertTrue(decision.quarantined)
        self.assertTrue(decision.legacy_suppressed)
        self.assertEqual(restarted.sent, [])

    def test_lease_expiry_does_not_unsuppress_an_installed_schedule(self):
        """Lease expiry permits ownership change but does not cancel device effect."""
        short_expiry = NOW_MS + 10 * 60 * 1000
        harness = Harness(send_results=[applied])
        self.assertEqual(self.dispatch(harness, lease=lease(expiry=short_expiry)).state, RESULT_APPLIED)
        after_lease = asyncio.run(harness.authority.legacy_decision(short_expiry))
        self.assertEqual(after_lease.state, RESULT_APPLIED)
        self.assertTrue(after_lease.legacy_suppressed)
        self.assertFalse(after_lease.fallback_allowed)

    def test_empty_default_schedule_round_trips_durable_state(self):
        """A zero-slot schedule with an explicit default remains restart-safe."""
        empty_schedule = compile_absolute_schedule(plan([], horizon=120), ORIGIN - timedelta(minutes=5))
        harness = Harness(send_results=[applied])
        decision = asyncio.run(harness.authority.dispatch(empty_schedule, target(), lease(), NOW_MS, "cmd-empty"))
        self.assertEqual(decision.state, RESULT_APPLIED)
        restarted = Harness(storage=harness.storage)
        self.assertTrue(asyncio.run(restarted.authority.legacy_decision(NOW_MS)).legacy_suppressed)

    def test_invalid_time_target_and_lease_never_send(self):
        """Unsafe preconditions are rejected before durable reservation or send."""
        cases = [
            {"now_ts_ms": 0},
            {"target": target(altitude_id="site:other")},
            {"lease": lease(expiry=NOW_MS)},
            {"lease": lease(scope_id="site:other")},
        ]
        for case in cases:
            with self.subTest(case=case):
                harness = Harness()
                decision = self.dispatch(harness, **case)
                self.assertFalse(decision.fallback_allowed)
                self.assertEqual(harness.sent, [])

    def test_retained_offer_constraints_are_enforced_before_send(self):
        """BatPred never knowingly sends outside the selected offer surface."""
        base_offer = target().offer
        unsupported_targets = [
            target(offer=replace(base_offer, max_slots=1)),
            target(offer=replace(base_offer, supported_modes=("self_use",))),
            target(offer=replace(base_offer, slot_fields=())),
            target(offer=replace(base_offer, execution_modes=("CONTROLLER_STEPPED",))),
            target(offer=replace(base_offer, max_slots=9)),
        ]
        for unsafe_target in unsupported_targets:
            with self.subTest(offer=unsafe_target.offer):
                harness = Harness()
                decision = self.dispatch(harness, target=unsafe_target)
                self.assertEqual(decision.state, RESULT_NOT_APPLIED)
                self.assertEqual(harness.sent, [])

        gapped = compile_absolute_schedule(
            plan([window(30, 60, "force_charge", target_soc=90)], horizon=120),
            ORIGIN - timedelta(minutes=5),
        )
        reject_gaps = target(offer=replace(base_offer, gap_policy="REJECT"))
        harness = Harness()
        decision = asyncio.run(harness.authority.dispatch(gapped, reject_gaps, lease(), NOW_MS, "cmd-gap"))
        self.assertEqual(decision.state, RESULT_NOT_APPLIED)
        self.assertEqual(harness.sent, [])

    def test_offer_sets_are_canonical_and_duplicates_are_rejected(self):
        """Equivalent tuple order has one fingerprint and duplicates are invalid."""
        base_offer = target().offer
        reordered = ResolvedScheduleOffer(
            max_slots=base_offer.max_slots,
            supported_modes=tuple(reversed(base_offer.supported_modes)),
            slot_fields=tuple(reversed(base_offer.slot_fields)),
            execution_modes=tuple(reversed(base_offer.execution_modes)),
            gap_policy=base_offer.gap_policy,
        )
        self.assertEqual(reordered, base_offer)
        self.assertEqual(reordered.fingerprint, base_offer.fingerprint)
        with self.assertRaisesRegex(ScheduleValidationError, "duplicates"):
            ResolvedScheduleOffer(8, ("self_use", "self_use"), (), ("NATIVE",), "DEFAULT_MODE")
        with self.assertRaisesRegex(ScheduleValidationError, "duplicates"):
            target(owned_node_ids=("GW-1", "GW-1"))

    def test_fencing_token_uses_full_uint64_domain(self):
        """Fence bounds follow protobuf uint64 rather than epoch-safe integers."""
        max_fence = (1 << 64) - 1
        accepted = Harness(send_results=[applied])
        decision = self.dispatch(accepted, lease=lease(fence=max_fence))
        self.assertEqual(decision.state, RESULT_APPLIED)
        self.assertEqual(accepted.storage.snapshot["high_water_token"], max_fence)

        too_large = Harness()
        rejected_decision = self.dispatch(too_large, lease=lease(fence=1 << 64))
        self.assertEqual(rejected_decision.state, RESULT_NOT_APPLIED)
        self.assertEqual(too_large.sent, [])

    def test_manually_forged_schedule_is_revalidated_before_send(self):
        """Public dataclasses cannot bypass the compiler's schedule invariants."""
        valid_slot = self.schedule.slots[0]
        forged = [
            replace(self.schedule, valid_from_ts_ms=0),
            replace(self.schedule, valid_until_ts_ms=(1 << 53)),
            replace(self.schedule, execution="MAGIC"),
            replace(self.schedule, diagnostic_timezone="../UTC"),
            replace(self.schedule, altitude_id=""),
            replace(self.schedule, slots=list(self.schedule.slots)),
            replace(
                self.schedule,
                slots=(
                    valid_slot,
                    replace(valid_slot, start_ts_ms=valid_slot.start_ts_ms + 1),
                ),
            ),
            replace(self.schedule, slots=(replace(valid_slot, mode="x" * 25),)),
            replace(self.schedule, slots=(replace(valid_slot, target_soc=float("nan")),)),
            AbsoluteScheduleIntent(
                altitude_id=ALTITUDE,
                slots=(),
                valid_from_ts_ms=self.schedule.valid_from_ts_ms,
                valid_until_ts_ms=self.schedule.valid_until_ts_ms,
                diagnostic_timezone="UTC",
                execution=EXECUTION_NATIVE,
                default_mode=None,
            ),
            replace(
                self.schedule,
                slots=tuple(
                    AbsoluteScheduleSlot(
                        self.schedule.valid_from_ts_ms + index * 2,
                        self.schedule.valid_from_ts_ms + index * 2 + 1,
                        "self_use",
                    )
                    for index in range(9)
                ),
            ),
        ]
        for schedule in forged:
            with self.subTest(schedule=schedule):
                harness = Harness()
                decision = asyncio.run(harness.authority.dispatch(schedule, target(), lease(), NOW_MS, "cmd-forged"))
                self.assertEqual(decision.state, RESULT_NOT_APPLIED)
                self.assertFalse(decision.fallback_allowed)
                self.assertEqual(harness.sent, [])

    def test_storage_and_command_id_exceptions_fail_closed(self):
        """Infrastructure exceptions return UNKNOWN and cannot reach transport."""

        def broken_command_id():
            """Raise from the injected identifier source."""
            raise RuntimeError("entropy unavailable")

        harnesses = [
            Harness(storage=FakeStorage(raise_load=True)),
            Harness(storage=FakeStorage(raise_saves={1})),
            Harness(command_id_factory=broken_command_id),
        ]
        for harness in harnesses:
            with self.subTest(harness=harness):
                decision = self.dispatch(harness)
                self.assertEqual(decision.state, RESULT_UNKNOWN)
                self.assertTrue(decision.quarantined)
                self.assertFalse(decision.fallback_allowed)
                self.assertEqual(harness.sent, [])

    def test_corrupt_restart_state_fails_closed(self):
        """Malformed persistence cannot activate either control path."""
        storage = FakeStorage({"schema_version": 1, "scope_id": SCOPE, "high_water_token": 10, "command": {"state": RESULT_APPLIED}})
        harness = Harness(storage=storage)
        decision = asyncio.run(harness.authority.legacy_decision(NOW_MS))
        self.assertEqual(decision.state, RESULT_UNKNOWN)
        self.assertTrue(decision.quarantined)
        self.assertTrue(decision.legacy_suppressed)
        self.assertFalse(decision.fallback_allowed)


if __name__ == "__main__":
    unittest.main()
