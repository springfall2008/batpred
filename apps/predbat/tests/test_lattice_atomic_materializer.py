"""Tests for the two-phase atomic Lattice auto-config materializer."""

# cspell:ignore autoconfig dedupe preimage readback

import copy
import itertools
import math
import os
import sys
import threading
import unittest
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_atomic_materializer import (  # noqa: E402
    MAX_SEQUENCE,
    AtomicConfigMaterializer,
    AtomicMaterializationState,
    MaterializationPhase,
    MaterializationStatus,
    MaterializationStorageError,
    MaterializationValidationError,
    PublishedMaterializationRequest,
    TargetOperationKey,
    TargetOperationPhase,
    TargetOperationSnapshot,
    TargetTerminalPhase,
    _canonical_config,
    _config_digest,
    _configs_equal,
    _validate_envelope,
)
from lattice_autoconfig import (  # noqa: E402
    MaterializationReadiness,
    MaterializationRequest,
    ProjectionCardinality,
    ProjectionRouting,
    ProjectionValueKind,
    ProviderConfigProjection,
    compile_auto_config,
)
from tests.test_lattice_autoconfig import (  # noqa: E402
    indexed_roles,
    projection_snapshot,
    projection_value,
)


class InMemoryAtomicStateStore:
    """Thread-safe durable fence/CAS store with deterministic cuts."""

    def __init__(self, state=None):
        """Create a store with optional durable state."""
        self._lock = threading.RLock()
        self.state = state
        self.fence = 0 if state is None else state.reservation_key.fence
        self.writes = 0
        self.calls = 0
        self.history = []
        self.fail_on_calls = {}
        self.corrupt_load = None
        self.raise_load = False
        self.raise_fence = False
        self.fence_override = None
        self.block_cas_call = None
        self.cas_entered = None
        self.cas_release = None

    def allocate_fence(self):
        """Allocate a globally increasing fence."""
        with self._lock:
            if self.raise_fence:
                raise OSError("secret-store-fence-error")
            if self.fence_override is not None:
                return self.fence_override
            self.fence += 1
            return self.fence

    def load(self):
        """Return current state or one injected failure."""
        with self._lock:
            if self.raise_load:
                raise OSError("secret-store-load-error")
            if self.corrupt_load is not None:
                return self.corrupt_load
            return self.state

    def compare_and_store(self, expected, replacement):
        """CAS with deterministic conflict and acknowledgement faults."""
        with self._lock:
            self.calls += 1
            call = self.calls
        if call == self.block_cas_call:
            self.cas_entered.set()
            self.cas_release.wait(5)
        with self._lock:
            mode = self.fail_on_calls.get(call)
            if mode == "reject":
                return False
            if mode == "raise_before":
                raise OSError("secret-store-cas-error")
            if self.state != expected:
                return False
            self.state = replacement
            self.writes += 1
            self.history.append(replacement)
            if mode == "raise_after":
                raise OSError("secret-store-cas-error")
            return True


class InMemoryWholeConfigTarget:
    """Durable target journal that alone retains raw configurations."""

    def __init__(self, config=None):
        """Create an exact fenced target with lifecycle fault injection."""
        self._lock = threading.RLock()
        self.config = copy.deepcopy(config or {})
        self.records = {}
        self.current_fence = 0
        self.prepare_calls = 0
        self.lookup_calls = 0
        self.apply_calls = 0
        self.rollback_calls = 0
        self.seal_calls = 0
        self.abort_calls = 0
        self.invalidate_calls = 0
        self.writes = 0
        self.history = []
        self.raise_prepare = False
        self.raise_lookup = False
        self.raise_apply_before = False
        self.raise_apply_after = False
        self.raise_rollback_before = False
        self.raise_rollback_after = False
        self.raise_seal = False
        self.raise_abort = False
        self.raise_invalidate = False
        self.lookup_override = None
        self.partial_apply = None
        self.block_prepare = False
        self.prepare_entered = None
        self.prepare_release = None
        self.block_apply = False
        self.apply_entered = None
        self.apply_release = None
        self.block_rollback = False
        self.rollback_entered = None
        self.rollback_release = None
        self.reservation_probe = None

    @staticmethod
    def _tuple_key(key):
        """Return a dictionary key for one exact operation key."""
        return (key.fence, key.projection_digest)

    def _key(self, fence, projection_digest):
        """Build one exact operation key."""
        return TargetOperationKey(fence, projection_digest)

    def _snapshot(self, record):
        """Return secret-free durable journal metadata."""
        if record["prepared"]:
            return TargetOperationSnapshot(
                key=record["key"],
                phase=record["phase"],
                handle=record["handle"],
                preimage_digest=_config_digest(record["preimage"]),
                candidate_digest=_config_digest(record["candidate"]),
                changed=not _configs_equal(
                    record["preimage"],
                    record["candidate"],
                ),
                terminal_phase=record["terminal"],
            )
        return TargetOperationSnapshot(
            key=record["key"],
            phase=TargetOperationPhase.SEALED,
            terminal_phase=TargetTerminalPhase.ROLLED_BACK,
        )

    def _record_for_snapshot(self, snapshot):
        """Resolve an exact snapshot without trusting only its handle."""
        record = self.records.get(self._tuple_key(snapshot.key))
        if record is None or not record["prepared"]:
            return None
        current = self._snapshot(record)
        if snapshot.key != current.key or snapshot.handle != current.handle or snapshot.preimage_digest != current.preimage_digest or snapshot.candidate_digest != current.candidate_digest:
            return None
        return record

    def prepare(self, fence, projection_digest, projection):
        """Create PREPARED only after observing a winning reservation."""
        if self.block_prepare:
            self.prepare_entered.set()
            self.prepare_release.wait(5)
        with self._lock:
            self.prepare_calls += 1
            if self.reservation_probe is not None:
                self.reservation_probe(fence, projection_digest)
            if self.raise_prepare:
                raise OSError("secret-target-prepare-error")
            key = self._key(fence, projection_digest)
            tuple_key = self._tuple_key(key)
            if fence < self.current_fence or tuple_key in self.records:
                raise ValueError("stale or replayed prepare")
            preimage = _canonical_config(
                copy.deepcopy(self.config),
                "target preimage",
            )
            projection = _canonical_config(
                copy.deepcopy(projection),
                "target projection",
            )
            if _config_digest(projection) != projection_digest:
                raise ValueError("projection digest mismatch")
            candidate = dict(preimage)
            candidate.update(dict(projection))
            candidate = _canonical_config(candidate, "target candidate")
            record = {
                "key": key,
                "prepared": True,
                "handle": "txn-{}".format(fence),
                "preimage": preimage,
                "candidate": candidate,
                "phase": TargetOperationPhase.PREPARED,
                "terminal": None,
            }
            self.current_fence = fence
            self.records[tuple_key] = record
            return self._snapshot(record)

    def lookup(self, fence, projection_digest):
        """Recover a journal record by reservation key."""
        with self._lock:
            self.lookup_calls += 1
            if self.raise_lookup:
                raise OSError("secret-target-lookup-error")
            if self.lookup_override is not None:
                return self.lookup_override
            record = self.records.get((fence, projection_digest))
            return None if record is None else self._snapshot(record)

    def apply(self, operation):
        """Commit only after rechecking current fence and operation phase."""
        with self._lock:
            self.apply_calls += 1
            record = self._record_for_snapshot(operation)
            if record is None or record["key"].fence != self.current_fence or record["phase"] is not TargetOperationPhase.PREPARED:
                if record is None:
                    return operation
                return self._snapshot(record)
            record["phase"] = TargetOperationPhase.APPLYING
            if self.raise_apply_before:
                raise OSError("secret-target-apply-error")
        if self.block_apply:
            self.apply_entered.set()
            self.apply_release.wait(5)
        with self._lock:
            if record["key"].fence != self.current_fence or record["phase"] is not TargetOperationPhase.APPLYING:
                return self._snapshot(record)
            if not _configs_equal(self.config, record["preimage"]):
                record["phase"] = TargetOperationPhase.UNKNOWN
                return self._snapshot(record)
            if self.partial_apply is None:
                self.config = copy.deepcopy(dict(record["candidate"]))
            else:
                self.config = copy.deepcopy(self.partial_apply)
            self.writes += 1
            self.history.append(copy.deepcopy(self.config))
            if _configs_equal(self.config, record["candidate"]):
                record["phase"] = TargetOperationPhase.APPLIED
            else:
                record["phase"] = TargetOperationPhase.UNKNOWN
            if self.raise_apply_after:
                raise OSError("secret-target-apply-error")
            return self._snapshot(record)

    def rollback(self, operation):
        """Rollback only the current sealed APPLIED handle."""
        with self._lock:
            self.rollback_calls += 1
            record = self._record_for_snapshot(operation)
            if record is None or record["key"].fence != self.current_fence or record["phase"] is not TargetOperationPhase.SEALED or record["terminal"] is not TargetTerminalPhase.APPLIED:
                if record is None:
                    return operation
                return self._snapshot(record)
            record["phase"] = TargetOperationPhase.ROLLING_BACK
            record["terminal"] = None
            if self.raise_rollback_before:
                raise OSError("secret-target-rollback-error")
        if self.block_rollback:
            self.rollback_entered.set()
            self.rollback_release.wait(5)
        with self._lock:
            if record["key"].fence != self.current_fence or record["phase"] is not TargetOperationPhase.ROLLING_BACK:
                return self._snapshot(record)
            if not _configs_equal(self.config, record["candidate"]):
                record["phase"] = TargetOperationPhase.UNKNOWN
                return self._snapshot(record)
            self.config = copy.deepcopy(dict(record["preimage"]))
            self.writes += 1
            self.history.append(copy.deepcopy(self.config))
            record["phase"] = TargetOperationPhase.ROLLED_BACK
            if self.raise_rollback_after:
                raise OSError("secret-target-rollback-error")
            return self._snapshot(record)

    def seal(self, operation):
        """Seal a stable result and make apply replay impossible."""
        with self._lock:
            self.seal_calls += 1
            if self.raise_seal:
                raise OSError("secret-target-seal-error")
            record = self._record_for_snapshot(operation)
            if record is None:
                raise ValueError("unknown operation")
            if record["phase"] is TargetOperationPhase.SEALED:
                return self._snapshot(record)
            if record["phase"] is TargetOperationPhase.APPLIED:
                terminal = TargetTerminalPhase.APPLIED
            elif record["phase"] in (
                TargetOperationPhase.ROLLED_BACK,
                TargetOperationPhase.PREPARED,
            ):
                terminal = TargetTerminalPhase.ROLLED_BACK
            else:
                raise ValueError("unstable operation")
            record["phase"] = TargetOperationPhase.SEALED
            record["terminal"] = terminal
            return self._snapshot(record)

    def _invalidate_record(self, record):
        """Atomically fence and seal one record from its exact visibility."""
        if record["phase"] is TargetOperationPhase.SEALED:
            return self._snapshot(record)
        if not record["prepared"]:
            return self._snapshot(record)
        if _configs_equal(self.config, record["candidate"]):
            terminal = TargetTerminalPhase.APPLIED
        elif _configs_equal(self.config, record["preimage"]):
            terminal = TargetTerminalPhase.ROLLED_BACK
        else:
            terminal = TargetTerminalPhase.UNKNOWN
        record["phase"] = TargetOperationPhase.SEALED
        record["terminal"] = terminal
        return self._snapshot(record)

    def abort(self, fence, projection_digest):
        """Tombstone even an unprepared reservation."""
        with self._lock:
            self.abort_calls += 1
            if self.raise_abort:
                raise OSError("secret-target-abort-error")
            key = self._key(fence, projection_digest)
            tuple_key = self._tuple_key(key)
            record = self.records.get(tuple_key)
            if record is None:
                record = {
                    "key": key,
                    "prepared": False,
                    "phase": TargetOperationPhase.SEALED,
                    "terminal": TargetTerminalPhase.ROLLED_BACK,
                }
                self.records[tuple_key] = record
            self.current_fence = max(self.current_fence, fence)
            return self._invalidate_record(record)

    def invalidate(self, operation_key):
        """Fence a key so any late commit recheck fails."""
        with self._lock:
            self.invalidate_calls += 1
            if self.raise_invalidate:
                raise OSError("secret-target-invalidate-error")
            tuple_key = self._tuple_key(operation_key)
            record = self.records.get(tuple_key)
            if record is None:
                record = {
                    "key": operation_key,
                    "prepared": False,
                    "phase": TargetOperationPhase.SEALED,
                    "terminal": TargetTerminalPhase.ROLLED_BACK,
                }
                self.records[tuple_key] = record
            self.current_fence = max(
                self.current_fence,
                operation_key.fence,
            )
            return self._invalidate_record(record)


class ExplosiveSeam:
    """Disabled-mode sentinel that must never be touched."""

    def __getattr__(self, _name):
        """Fail if any seam operation is inspected or called."""
        raise AssertionError("disabled materializer touched seam")


class InfiniteMapping(Mapping):
    """Mapping whose items iterator never terminates."""

    def __getitem__(self, key):
        raise KeyError(key)

    def __iter__(self):
        return itertools.count()

    def __len__(self):
        return 0

    def items(self):
        return (("key_{}".format(index), index) for index in itertools.count())


class FaultingMapping(Mapping):
    """Mapping whose item traversal raises a secret-bearing error."""

    def __getitem__(self, key):
        raise KeyError(key)

    def __iter__(self):
        raise OSError("secret-topology-error")

    def __len__(self):
        raise OSError("secret-topology-error")

    def items(self):
        raise OSError("secret-topology-error")


def config_plan(value=7):
    """Compile one projection-complete scalar config plan."""
    node = "INV1"
    projection = ProviderConfigProjection(
        argument="battery_rate_max",
        role=indexed_roles((node,))[0].role,
        group="inverters",
        routing=ProjectionRouting.LEAF,
        cardinality=ProjectionCardinality.SCALAR,
        values=(
            projection_value(
                node,
                ProjectionValueKind.CONSTANT,
                value,
            ),
        ),
    )
    provider = projection_snapshot(
        "gateway",
        (node,),
        indexed_roles((node,)),
        (projection,),
    )
    return compile_auto_config((provider,))


def values_plan():
    """Compile explicit None, list, scalar, boolean, and string values."""
    nodes = ("INV1", "INV2")
    roles = indexed_roles(nodes)
    projections = (
        ProviderConfigProjection(
            "battery_power",
            roles[0].role,
            "inverters",
            ProjectionRouting.LEAF,
            ProjectionCardinality.PER_INDEX,
            (
                projection_value(
                    nodes[0],
                    ProjectionValueKind.CONSTANT,
                    5,
                ),
                projection_value(
                    nodes[1],
                    ProjectionValueKind.NONE,
                ),
            ),
            required=False,
        ),
        ProviderConfigProjection(
            "givtcp_rest",
            roles[0].role,
            "inverters",
            ProjectionRouting.LEAF,
            ProjectionCardinality.SCALAR,
            (
                projection_value(
                    nodes[0],
                    ProjectionValueKind.NONE,
                ),
            ),
            required=False,
        ),
        ProviderConfigProjection(
            "inverter_type",
            roles[0].role,
            "inverters",
            ProjectionRouting.LEAF,
            ProjectionCardinality.SCALAR,
            (
                projection_value(
                    nodes[0],
                    ProjectionValueKind.CONSTANT,
                    "GEC",
                ),
            ),
        ),
        ProviderConfigProjection(
            "set_read_only",
            roles[0].role,
            "inverters",
            ProjectionRouting.LEAF,
            ProjectionCardinality.SCALAR,
            (
                projection_value(
                    nodes[0],
                    ProjectionValueKind.CONSTANT,
                    True,
                ),
            ),
        ),
    )
    provider = projection_snapshot(
        "gateway",
        nodes,
        roles,
        projections,
    )
    return compile_auto_config((provider,))


def envelope(plan=None, version=1, token="lattice-publication-1-test"):
    """Bind one plan to its publication cursor."""
    plan = plan or config_plan()
    return PublishedMaterializationRequest(
        MaterializationRequest(plan, token),
        plan.digest,
        version,
    )


def enabled_materializer(config=None, store=None, target=None):
    """Create one enabled coordinator and its reference seams."""
    store = store or InMemoryAtomicStateStore()
    target = target or InMemoryWholeConfigTarget(config)
    materializer = AtomicConfigMaterializer(
        store,
        target,
        enabled=True,
    )
    return materializer, store, target


def reserve_only(materializer, request):
    """Create one winning RESERVED state without touching the target."""
    (
        _projection,
        projection_digest,
        feedback_digest,
        provenance_digest,
        provenance_count,
    ) = _validate_envelope(request)
    reserved, terminal = materializer._reserve(
        request,
        projection_digest,
        feedback_digest,
        provenance_digest,
        provenance_count,
    )
    if terminal is not None:
        raise AssertionError("reservation unexpectedly returned terminal result")
    return reserved


class TestAtomicConfigMaterializer(unittest.TestCase):
    """Reservations and target operations remain atomic across every cut."""

    def test_default_off_touches_nothing(self):
        """The unregistered default cannot discover or mutate live config."""
        materializer = AtomicConfigMaterializer(
            ExplosiveSeam(),
            ExplosiveSeam(),
        )

        result = materializer.apply(object())

        self.assertEqual(result.status, MaterializationStatus.DISABLED)
        self.assertIsNone(result.state)

    def test_reservation_precedes_prepare_and_state_contains_no_secrets(self):
        """The target sees durable RESERVED before any secret-bearing prepare."""
        secret = "do-not-persist-secret-value"
        store = InMemoryAtomicStateStore()
        target = InMemoryWholeConfigTarget({"manual_setting": "keep", "api_secret": secret})

        def assert_reserved(fence, projection_digest):
            state = store.state
            self.assertEqual(state.phase, MaterializationPhase.RESERVED)
            self.assertEqual(state.reservation_key.fence, fence)
            self.assertEqual(
                state.projection_digest,
                projection_digest,
            )

        target.reservation_probe = assert_reserved
        materializer, _store, _target = enabled_materializer(
            store=store,
            target=target,
        )
        request = envelope()

        result = materializer.apply(request)

        self.assertEqual(result.status, MaterializationStatus.APPLIED)
        self.assertEqual(
            [state.phase for state in store.history],
            [
                MaterializationPhase.RESERVED,
                MaterializationPhase.PREPARED,
                MaterializationPhase.APPLYING,
                MaterializationPhase.APPLIED,
            ],
        )
        self.assertEqual(result.state.operation.phase, TargetOperationPhase.SEALED)
        self.assertEqual(
            result.state.operation.terminal_phase,
            TargetTerminalPhase.APPLIED,
        )
        durable_text = repr(result.state) + repr(result) + repr(request)
        self.assertNotIn(secret, durable_text)
        self.assertNotIn(request.request.feedback_token, durable_text)
        for name in (
            "projection",
            "preimage",
            "candidate",
            "feedback_token",
            "provenance",
        ):
            self.assertFalse(hasattr(result.state, name))

    def test_semantic_forgery_and_cursor_regression_fail_closed(self):
        """Initial digest forgery and later publication regression cannot apply."""
        plan = config_plan(1)
        forged_argument = replace(plan.config_arguments[0], value=99)
        forged = replace(
            plan,
            config_arguments=(forged_argument,),
            projected_config={"battery_rate_max": 99},
        )
        materializer, store, target = enabled_materializer()

        with self.assertRaises(MaterializationValidationError):
            materializer.apply(envelope(forged, 1, "forged"))
        self.assertEqual(store.writes, 0)
        self.assertEqual(target.prepare_calls, 0)

        materializer.apply(envelope(plan, 1, "one"))
        replay = materializer.apply(envelope(plan, 100, "hundred"))
        self.assertEqual(replay.status, MaterializationStatus.UNCHANGED)
        self.assertEqual(replay.state.publication_version, 100)
        regressed = materializer.apply(envelope(config_plan(2), 2, "two"))
        self.assertEqual(regressed.status, MaterializationStatus.SUPERSEDED)
        self.assertEqual(target.config["battery_rate_max"], 1)

    def test_dual_reserve_cas_loser_never_prepares_target(self):
        """Only the durable CAS winner may create a target transaction."""
        store = InMemoryAtomicStateStore()
        store.block_cas_call = 1
        store.cas_entered = threading.Event()
        store.cas_release = threading.Event()
        target = InMemoryWholeConfigTarget()
        target.block_prepare = True
        target.prepare_entered = threading.Event()
        target.prepare_release = threading.Event()
        first, _store, _target = enabled_materializer(
            store=store,
            target=target,
        )
        second, _store, _target = enabled_materializer(
            store=store,
            target=target,
        )
        request = envelope()
        results = {}

        first_thread = threading.Thread(target=lambda: results.setdefault("first", first.apply(request)))
        second_thread = threading.Thread(target=lambda: results.setdefault("second", second.apply(request)))
        first_thread.start()
        self.assertTrue(store.cas_entered.wait(5))
        second_thread.start()
        self.assertTrue(target.prepare_entered.wait(5))
        store.cas_release.set()
        first_thread.join(5)

        self.assertEqual(
            results["first"].status,
            MaterializationStatus.UNKNOWN,
        )
        self.assertEqual(target.prepare_calls, 0)
        self.assertEqual(len(target.records), 0)
        target.prepare_release.set()
        second_thread.join(5)
        self.assertEqual(
            results["second"].status,
            MaterializationStatus.APPLIED,
        )
        self.assertEqual(target.prepare_calls, 1)
        self.assertEqual(len(target.records), 1)

    def test_restart_overlap_invalidates_late_apply(self):
        """Recovery fences APPLYING before a blocked old commit can resume."""
        store = InMemoryAtomicStateStore()
        target = InMemoryWholeConfigTarget()
        target.block_apply = True
        target.apply_entered = threading.Event()
        target.apply_release = threading.Event()
        first, _store, _target = enabled_materializer(
            store=store,
            target=target,
        )
        restarted, _store, _target = enabled_materializer(
            store=store,
            target=target,
        )
        results = {}
        thread = threading.Thread(
            target=lambda: results.setdefault(
                "apply",
                first.apply(envelope()),
            )
        )
        thread.start()
        self.assertTrue(target.apply_entered.wait(5))

        recovered = restarted.recover()
        target.apply_release.set()
        thread.join(5)

        self.assertEqual(recovered.status, MaterializationStatus.NOT_APPLIED)
        self.assertEqual(results["apply"].status, MaterializationStatus.NOT_APPLIED)
        self.assertEqual(target.writes, 0)
        self.assertEqual(store.state.phase, MaterializationPhase.ROLLED_BACK)

    def test_restart_overlap_invalidates_late_rollback(self):
        """Recovery seals visibility before a blocked rollback can resume."""
        store = InMemoryAtomicStateStore()
        target = InMemoryWholeConfigTarget()
        materializer, _store, _target = enabled_materializer(
            store=store,
            target=target,
        )
        materializer.apply(envelope())
        target.block_rollback = True
        target.rollback_entered = threading.Event()
        target.rollback_release = threading.Event()
        results = {}
        thread = threading.Thread(
            target=lambda: results.setdefault(
                "rollback",
                materializer.rollback(),
            )
        )
        thread.start()
        self.assertTrue(target.rollback_entered.wait(5))
        restarted, _store, _target = enabled_materializer(
            store=store,
            target=target,
        )

        recovered = restarted.recover()
        target.rollback_release.set()
        thread.join(5)

        self.assertEqual(recovered.status, MaterializationStatus.APPLIED)
        self.assertEqual(results["rollback"].status, MaterializationStatus.APPLIED)
        self.assertEqual(target.config["battery_rate_max"], 7)
        self.assertEqual(store.state.phase, MaterializationPhase.APPLIED)

    def test_sealed_and_rolled_back_handles_reject_apply_replay(self):
        """A captured old PREPARED handle can never write after sealing."""
        materializer, _store, target = enabled_materializer()
        applied = materializer.apply(envelope())
        record = target.records[target._tuple_key(applied.state.reservation_key)]
        captured = TargetOperationSnapshot(
            key=record["key"],
            phase=TargetOperationPhase.PREPARED,
            handle=record["handle"],
            preimage_digest=_config_digest(record["preimage"]),
            candidate_digest=_config_digest(record["candidate"]),
            changed=True,
        )
        writes = target.writes

        replay_after_apply = target.apply(captured)
        materializer.rollback()
        replay_after_rollback = target.apply(captured)

        self.assertEqual(
            replay_after_apply.phase,
            TargetOperationPhase.SEALED,
        )
        self.assertEqual(
            replay_after_rollback.terminal_phase,
            TargetTerminalPhase.ROLLED_BACK,
        )
        self.assertEqual(target.writes, writes + 1)
        self.assertNotIn("battery_rate_max", target.config)

    def test_higher_reserved_publication_cannot_be_cancelled_by_lower(self):
        """A blocked v2 reservation makes a later v1 immediately stale."""
        store = InMemoryAtomicStateStore()
        target = InMemoryWholeConfigTarget()
        target.block_prepare = True
        target.prepare_entered = threading.Event()
        target.prepare_release = threading.Event()
        higher, _store, _target = enabled_materializer(
            store=store,
            target=target,
        )
        lower, _store, _target = enabled_materializer(
            store=store,
            target=target,
        )
        results = {}
        thread = threading.Thread(
            target=lambda: results.setdefault(
                "higher",
                higher.apply(envelope(config_plan(2), 2, "two")),
            )
        )
        thread.start()
        self.assertTrue(target.prepare_entered.wait(5))

        stale = lower.apply(envelope(config_plan(1), 1, "one"))

        self.assertEqual(stale.status, MaterializationStatus.SUPERSEDED)
        self.assertEqual(store.state.publication_version, 2)
        self.assertEqual(target.invalidate_calls, 0)
        target.prepare_release.set()
        thread.join(5)
        self.assertEqual(results["higher"].status, MaterializationStatus.APPLIED)
        self.assertEqual(target.config["battery_rate_max"], 2)

    def test_topology_is_recursively_bounded_before_any_seam(self):
        """Infinite/faulting mapping proxies fail without len or full iteration."""
        plan = config_plan()
        cases = (
            MappingProxyType(InfiniteMapping()),
            MappingProxyType(FaultingMapping()),
            {
                "nested": MappingProxyType(FaultingMapping()),
            },
        )
        for topology in cases:
            with self.subTest(topology=type(topology).__name__):
                materializer, store, target = enabled_materializer()
                malformed = replace(plan, topology=topology)
                with self.assertRaises(MaterializationValidationError) as caught:
                    materializer.apply(envelope(malformed))
                self.assertNotIn("secret-topology", str(caught.exception))
                self.assertIsNone(caught.exception.__context__)
                self.assertEqual(store.writes, 0)
                self.assertEqual(target.prepare_calls, 0)

    def test_all_numeric_sequences_are_bounded_before_external_calls(self):
        """Huge config/generation/revision/version values fail before seams."""
        huge = MAX_SEQUENCE + 1
        plan = config_plan()
        argument = replace(plan.config_arguments[0], value=huge)
        huge_config = replace(
            plan,
            config_arguments=(argument,),
            projected_config={"battery_rate_max": huge},
        )
        materializer, store, target = enabled_materializer()
        with self.assertRaises(MaterializationValidationError):
            materializer.apply(envelope(huge_config))
        self.assertEqual(store.writes, 0)
        self.assertEqual(target.prepare_calls, 0)

        huge_generation = replace(
            plan,
            provider_generations=(("gateway", huge),),
        )
        with self.assertRaises(MaterializationValidationError):
            materializer.apply(envelope(huge_generation))
        self.assertEqual(store.writes, 0)

        provenance = replace(plan.provenance[0], generation=huge)
        huge_provenance = replace(plan, provenance=(provenance,))
        with self.assertRaises(MaterializationValidationError):
            materializer.apply(envelope(huge_provenance))
        self.assertEqual(store.writes, 0)

        with self.assertRaises(ValueError):
            envelope(plan, huge, "huge-version")

        fence_store = InMemoryAtomicStateStore()
        fence_store.fence_override = huge
        fenced, _store, fence_target = enabled_materializer(store=fence_store)
        with self.assertRaises(MaterializationStorageError):
            fenced.apply(envelope())
        self.assertEqual(fence_target.prepare_calls, 0)

        reserved = reserve_only(materializer, envelope(plan, 1, "one"))
        object.__setattr__(reserved, "revision", huge)
        store.state = reserved
        with self.assertRaises(MaterializationStorageError):
            materializer.apply(envelope(config_plan(2), 2, "two"))
        self.assertEqual(target.prepare_calls, 0)

    def test_crash_cuts_reservation_prepare_attach_and_applying(self):
        """Every pre-commit crash cut is recoverable without raw config."""
        request = envelope()

        materializer, store, target = enabled_materializer()
        reserved = reserve_only(materializer, request)
        recovered = materializer.recover()
        self.assertEqual(recovered.status, MaterializationStatus.NOT_APPLIED)
        self.assertEqual(target.abort_calls, 1)
        projection = _validate_envelope(request)[0]
        with self.assertRaises(ValueError):
            target.prepare(
                reserved.reservation_key.fence,
                reserved.projection_digest,
                dict(projection),
            )

        materializer, store, target = enabled_materializer()
        reserved = reserve_only(materializer, request)
        target.prepare(
            reserved.reservation_key.fence,
            reserved.projection_digest,
            dict(projection),
        )
        recovered = materializer.recover()
        self.assertEqual(recovered.status, MaterializationStatus.NOT_APPLIED)
        self.assertEqual(target.invalidate_calls, 1)

        materializer, store, target = enabled_materializer()
        reserved = reserve_only(materializer, request)
        operation = target.prepare(
            reserved.reservation_key.fence,
            reserved.projection_digest,
            dict(projection),
        )
        prepared, terminal = materializer._attach_operation(
            reserved,
            operation,
        )
        self.assertIsNone(terminal)
        recovered = materializer.recover()
        self.assertEqual(recovered.status, MaterializationStatus.NOT_APPLIED)

        materializer, store, target = enabled_materializer()
        reserved = reserve_only(materializer, request)
        operation = target.prepare(
            reserved.reservation_key.fence,
            reserved.projection_digest,
            dict(projection),
        )
        prepared, _terminal = materializer._attach_operation(
            reserved,
            operation,
        )
        applying, _terminal = materializer._mark_applying(prepared)
        self.assertEqual(applying.phase, MaterializationPhase.APPLYING)
        recovered = materializer.recover()
        self.assertEqual(recovered.status, MaterializationStatus.NOT_APPLIED)

    def test_sealed_target_recovers_across_final_checkpoint_cut(self):
        """A durable target seal survives final CAS failure or ack loss."""
        store = InMemoryAtomicStateStore()
        store.fail_on_calls[4] = "raise_before"
        materializer, _store, target = enabled_materializer(store=store)

        with self.assertRaises(MaterializationStorageError):
            materializer.apply(envelope())

        self.assertEqual(store.state.phase, MaterializationPhase.APPLYING)
        snapshot = target.lookup(
            store.state.reservation_key.fence,
            store.state.projection_digest,
        )
        self.assertEqual(snapshot.phase, TargetOperationPhase.SEALED)
        self.assertEqual(
            snapshot.terminal_phase,
            TargetTerminalPhase.APPLIED,
        )
        self.assertEqual(
            materializer.recover().status,
            MaterializationStatus.APPLIED,
        )

        store = InMemoryAtomicStateStore()
        store.fail_on_calls[4] = "raise_after"
        materializer, _store, target = enabled_materializer(store=store)
        applied = materializer.apply(envelope())
        self.assertEqual(applied.status, MaterializationStatus.APPLIED)
        self.assertEqual(store.state.phase, MaterializationPhase.APPLIED)
        self.assertEqual(target.writes, 1)

    def test_partial_target_apply_is_sealed_unknown(self):
        """A partial target mutation is never guessed applied or rolled back."""
        target = InMemoryWholeConfigTarget()
        target.partial_apply = {"partially": "written"}
        materializer, store, _target = enabled_materializer(target=target)

        result = materializer.apply(envelope())

        self.assertEqual(result.status, MaterializationStatus.UNKNOWN)
        self.assertEqual(store.state.phase, MaterializationPhase.UNKNOWN)
        self.assertEqual(
            store.state.operation.phase,
            TargetOperationPhase.SEALED,
        )
        self.assertEqual(
            store.state.operation.terminal_phase,
            TargetTerminalPhase.UNKNOWN,
        )
        self.assertEqual(target.config, {"partially": "written"})

    def test_seal_abort_invalidate_failures_remain_recoverable(self):
        """Failed lifecycle acknowledgements never become terminal guesses."""
        materializer, store, target = enabled_materializer()
        target.raise_seal = True
        unknown = materializer.apply(envelope())
        self.assertEqual(unknown.status, MaterializationStatus.UNKNOWN)
        self.assertEqual(store.state.phase, MaterializationPhase.APPLYING)
        target.raise_seal = False
        recovered = materializer.recover()
        self.assertEqual(recovered.status, MaterializationStatus.APPLIED)

        materializer, store, target = enabled_materializer()
        reserve_only(materializer, envelope())
        target.raise_abort = True
        unknown = materializer.recover()
        self.assertEqual(unknown.status, MaterializationStatus.UNKNOWN)
        self.assertEqual(store.state.phase, MaterializationPhase.RESERVED)
        target.raise_abort = False
        self.assertEqual(
            materializer.recover().status,
            MaterializationStatus.NOT_APPLIED,
        )

        materializer, store, target = enabled_materializer()
        reserved = reserve_only(materializer, envelope())
        projection = _validate_envelope(envelope())[0]
        operation = target.prepare(
            reserved.reservation_key.fence,
            reserved.projection_digest,
            dict(projection),
        )
        materializer._attach_operation(reserved, operation)
        target.raise_invalidate = True
        unknown = materializer.recover()
        self.assertEqual(unknown.status, MaterializationStatus.UNKNOWN)
        target.raise_invalidate = False
        self.assertEqual(
            materializer.recover().status,
            MaterializationStatus.NOT_APPLIED,
        )

    def test_lookup_corruption_never_finalizes_checkpoint(self):
        """Corrupt lookup metadata leaves the durable phase non-terminal."""
        materializer, store, target = enabled_materializer()
        reserved = reserve_only(materializer, envelope())
        projection = _validate_envelope(envelope())[0]
        operation = target.prepare(
            reserved.reservation_key.fence,
            reserved.projection_digest,
            dict(projection),
        )
        prepared, _terminal = materializer._attach_operation(
            reserved,
            operation,
        )
        cases = (
            object(),
            replace(operation, handle="different-handle"),
        )
        for corrupt in cases:
            with self.subTest(corrupt=type(corrupt).__name__):
                target.lookup_override = corrupt
                recovered = materializer.recover()
                self.assertEqual(
                    recovered.status,
                    MaterializationStatus.UNKNOWN,
                )
                self.assertEqual(store.state, prepared)

    def test_recovery_covers_every_target_operation_phase(self):
        """No target phase is finalized before an acknowledged SEALED result."""
        phases = (
            TargetOperationPhase.PREPARED,
            TargetOperationPhase.APPLYING,
            TargetOperationPhase.APPLIED,
            TargetOperationPhase.ROLLING_BACK,
            TargetOperationPhase.ROLLED_BACK,
            TargetOperationPhase.SEALED,
            TargetOperationPhase.UNKNOWN,
        )
        for phase in phases:
            with self.subTest(phase=phase.value):
                materializer, store, target = enabled_materializer()
                reserved = reserve_only(materializer, envelope())
                projection = _validate_envelope(envelope())[0]
                operation = target.prepare(
                    reserved.reservation_key.fence,
                    reserved.projection_digest,
                    dict(projection),
                )
                record = target.records[target._tuple_key(reserved.reservation_key)]
                record["phase"] = phase
                if phase is TargetOperationPhase.APPLIED:
                    target.config = copy.deepcopy(dict(record["candidate"]))
                elif phase is TargetOperationPhase.ROLLED_BACK:
                    target.config = copy.deepcopy(dict(record["preimage"]))
                elif phase is TargetOperationPhase.SEALED:
                    target.config = copy.deepcopy(dict(record["candidate"]))
                    record["terminal"] = TargetTerminalPhase.APPLIED
                unknown_state = reserved.with_operation(
                    MaterializationPhase.UNKNOWN,
                    operation,
                )
                store.state = unknown_state

                recovered = materializer.recover()

                self.assertIn(
                    recovered.status,
                    (
                        MaterializationStatus.APPLIED,
                        MaterializationStatus.NOT_APPLIED,
                        MaterializationStatus.UNKNOWN,
                    ),
                )
                self.assertEqual(
                    target.lookup(
                        reserved.reservation_key.fence,
                        reserved.projection_digest,
                    ).phase,
                    TargetOperationPhase.SEALED,
                )

    def test_target_and_store_errors_are_value_free(self):
        """No secret-bearing seam exception survives in message or context."""
        store = InMemoryAtomicStateStore()
        store.raise_load = True
        materializer, _store, target = enabled_materializer(store=store)
        with self.assertRaises(MaterializationStorageError) as caught:
            materializer.apply(envelope())
        self.assertNotIn("secret-store", str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(target.prepare_calls, 0)

        store = InMemoryAtomicStateStore()
        store.raise_fence = True
        materializer, _store, target = enabled_materializer(store=store)
        with self.assertRaises(MaterializationStorageError):
            materializer.apply(envelope())
        self.assertEqual(target.prepare_calls, 0)

        target = InMemoryWholeConfigTarget()
        target.raise_prepare = True
        materializer, store, _target = enabled_materializer(target=target)
        result = materializer.apply(envelope())
        self.assertEqual(result.status, MaterializationStatus.UNKNOWN)
        self.assertEqual(store.state.phase, MaterializationPhase.RESERVED)

        for mode in ("reject", "raise_before", "raise_after"):
            with self.subTest(mode=mode):
                store = InMemoryAtomicStateStore()
                store.fail_on_calls[1] = mode
                materializer, _store, target = enabled_materializer(store=store)
                if mode == "raise_after":
                    result = materializer.apply(envelope())
                    self.assertEqual(
                        result.status,
                        MaterializationStatus.APPLIED,
                    )
                else:
                    with self.assertRaises(MaterializationStorageError):
                        materializer.apply(envelope())
                if mode != "raise_after":
                    self.assertEqual(target.prepare_calls, 0)

    def test_apply_and_rollback_exceptions_recover_by_lookup_and_seal(self):
        """Before/after target exceptions resolve only through sealed lookup."""
        for attribute, expected in (
            ("raise_apply_before", MaterializationStatus.NOT_APPLIED),
            ("raise_apply_after", MaterializationStatus.APPLIED),
        ):
            with self.subTest(attribute=attribute):
                target = InMemoryWholeConfigTarget()
                setattr(target, attribute, True)
                materializer, _store, _target = enabled_materializer(target=target)
                result = materializer.apply(envelope())
                self.assertEqual(result.status, expected)

        for attribute, expected in (
            ("raise_rollback_before", MaterializationStatus.APPLIED),
            ("raise_rollback_after", MaterializationStatus.ROLLED_BACK),
        ):
            with self.subTest(attribute=attribute):
                target = InMemoryWholeConfigTarget()
                materializer, _store, _target = enabled_materializer(target=target)
                materializer.apply(envelope())
                setattr(target, attribute, True)
                result = materializer.rollback()
                self.assertEqual(result.status, expected)

    def test_canonical_values_are_type_exact_and_non_finite_rejected(self):
        """None/list/scalars survive while numeric ambiguities are rejected."""

        class HostileInt(int):
            def __lt__(self, _other):
                raise RuntimeError("secret-hostile-integer")

            def __gt__(self, _other):
                raise RuntimeError("secret-hostile-integer")

        class HostileFloat(float):
            def __float__(self):
                raise RuntimeError("secret-hostile-float")

        class HostileStr(str):
            def strip(self, _chars=None):
                raise RuntimeError("secret-hostile-string")

            def encode(self, _encoding="utf-8", _errors="strict"):
                raise RuntimeError("secret-hostile-string")

        materializer, _store, target = enabled_materializer()
        result = materializer.apply(envelope(values_plan()))
        self.assertEqual(result.status, MaterializationStatus.APPLIED)
        self.assertEqual(
            target.config,
            {
                "battery_power": (5, None),
                "givtcp_rest": None,
                "inverter_type": "GEC",
                "set_read_only": True,
            },
        )
        self.assertFalse(_configs_equal({"value": True}, {"value": 1}))
        self.assertFalse(_configs_equal({"value": 1}, {"value": 1.0}))
        for value in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ValueError):
                _canonical_config({"value": value}, "test")
        for value in (HostileInt(1), HostileFloat(1.0), HostileStr("x")):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(ValueError) as caught:
                    _canonical_config({"value": value}, "test")
                self.assertNotIn("secret-hostile", str(caught.exception))
                self.assertIsNone(caught.exception.__context__)
        with self.assertRaises(ValueError):
            _canonical_config({HostileStr("value"): 1}, "test")

        store = InMemoryAtomicStateStore()
        store.fence_override = HostileInt(1)
        materializer, _store, target = enabled_materializer(store=store)
        with self.assertRaises(MaterializationStorageError) as caught:
            materializer.apply(envelope())
        self.assertNotIn("secret-hostile", str(caught.exception))
        self.assertEqual(target.prepare_calls, 0)

    def test_corrupt_state_subtype_and_fingerprint_fail_closed(self):
        """Exact base type and integrity reject corrupt durable checkpoints."""
        materializer, store, _target = enabled_materializer()
        valid = materializer.apply(envelope()).state
        subtype = type(
            "BypassState",
            (AtomicMaterializationState,),
            {"__post_init__": lambda self: None},
        )
        corrupt = object.__new__(subtype)
        for name, value in valid.__dict__.items():
            object.__setattr__(corrupt, name, value)
        store.corrupt_load = corrupt
        with self.assertRaises(MaterializationStorageError) as caught:
            materializer.recover()
        self.assertIsNone(caught.exception.__context__)

        store.corrupt_load = None
        torn = object.__new__(AtomicMaterializationState)
        for name, value in valid.__dict__.items():
            object.__setattr__(torn, name, value)
        object.__setattr__(torn, "integrity_digest", "0" * 64)
        store.corrupt_load = torn
        with self.assertRaises(MaterializationStorageError) as caught:
            materializer.recover()
        self.assertIsNone(caught.exception.__context__)

    def test_infinite_argument_and_provenance_iterables_are_not_consumed(self):
        """Non-tuple compiler fields fail immediately without unbounded reads."""
        plan = config_plan()
        malformed = (
            replace(
                plan,
                config_arguments=itertools.repeat(plan.config_arguments[0]),
            ),
            replace(
                plan,
                provenance=itertools.repeat(plan.provenance[0]),
            ),
        )
        for candidate in malformed:
            materializer, store, target = enabled_materializer()
            with self.assertRaises(MaterializationValidationError):
                materializer.apply(envelope(candidate))
            self.assertEqual(store.writes, 0)
            self.assertEqual(target.prepare_calls, 0)

    def test_readiness_and_publication_identity_are_exact(self):
        """Only the sole blocker and exact same-version audit may proceed."""
        plan = config_plan()
        ready = replace(
            plan,
            materialization_readiness=MaterializationReadiness(True, ()),
        )
        materializer, _store, target = enabled_materializer()
        with self.assertRaises(MaterializationValidationError):
            materializer.apply(envelope(ready))

        materializer.apply(envelope(plan, 2, "two"))
        with self.assertRaises(MaterializationValidationError):
            materializer.apply(envelope(plan, 2, "different-token"))
        with self.assertRaises(MaterializationValidationError):
            materializer.apply(envelope(config_plan(2), 2, "conflict"))
        self.assertEqual(target.config["battery_rate_max"], 7)


if __name__ == "__main__":
    unittest.main()
