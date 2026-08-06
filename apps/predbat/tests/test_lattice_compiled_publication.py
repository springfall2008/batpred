"""Tests for immutable durable publication of compiled Lattice state."""

# cspell:ignore autoconfig materializable

import os
import sys
import threading
import unittest
from dataclasses import FrozenInstanceError, replace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_autoconfig import (  # noqa: E402
    AliasRole,
    CompileStatus,
    ProjectionCardinality,
    ProjectionValueKind,
    ProviderHealth,
    ProviderSnapshot,
    UserConfigOverride,
)
from lattice_compiled_publication import (  # noqa: E402
    CompilationInvalidation,
    CompiledLatticeCompiler,
    InMemoryCompiledLatticeStateStore,
    UserOverrideSnapshot,
)
from tests.test_lattice_autoconfig import (  # noqa: E402
    MutableReader,
    config_projection,
    fragment,
    indexed_roles,
    projection_snapshot,
    projection_value,
    snapshot,
)


class MutableOverrideReader:
    """Mutable complete override snapshot reader with a call counter."""

    def __init__(self, value):
        """Store the first immutable override generation."""
        self.value = value
        self.calls = 0

    def __call__(self):
        """Return the current override generation and count the fresh read."""
        self.calls += 1
        return self.value


class RejectOnceStore(InMemoryCompiledLatticeStateStore):
    """Reference store that rejects its first atomic publication."""

    def __init__(self):
        """Create one rejecting reference store."""
        super().__init__()
        self.reject = True

    def compare_and_publish(self, expected_version, publication):
        """Reject once, then delegate to the atomic reference store."""
        if self.reject:
            self.reject = False
            return False
        return super().compare_and_publish(expected_version, publication)


class AmbiguousDifferentCursorStore(InMemoryCompiledLatticeStateStore):
    """Store that commits a different same-version publication, then raises."""

    def compare_and_publish(self, expected_version, publication):
        """Expose ambiguous-write recovery to a same-digest cursor collision."""
        conflicting = replace(
            publication,
            provider_generations=publication.provider_generations + (("offline", 7),),
            provider_fingerprints=publication.provider_fingerprints + (("offline", 7, "f" * 64),),
            provider_requested_generations=publication.provider_requested_generations + (("offline", 7),),
            invalidation_causes=publication.invalidation_causes
            + (
                CompilationInvalidation(
                    "provider",
                    "offline",
                    7,
                    "different committed cursor",
                ),
            ),
        )
        with self._lock:
            self._publication = conflicting
            self._writes += 1
        raise RuntimeError("connection lost after another cursor committed")


class AmbiguousExactStore(InMemoryCompiledLatticeStateStore):
    """Store that commits the exact candidate and then loses acknowledgement."""

    def compare_and_publish(self, expected_version, publication):
        """Commit the exact publication before raising an ambiguous error."""
        with self._lock:
            self._publication = publication
            self._writes += 1
        raise RuntimeError("connection lost after exact commit")


def override_projection_snapshot(generation=1):
    """Build one provider projection suitable for explicit override tests."""
    return projection_snapshot(
        "gateway",
        ("GW1",),
        indexed_roles(("GW1",)),
        (
            config_projection(
                "battery_rate_max",
                (
                    projection_value(
                        "GW1",
                        ProjectionValueKind.CONSTANT,
                        5,
                    ),
                ),
                role=AliasRole.PRIMARY,
                cardinality=ProjectionCardinality.PER_INDEX,
            ),
        ),
        generation=generation,
    )


def overrides(generation, value):
    """Build one complete immutable user override generation."""
    return UserOverrideSnapshot(
        generation,
        (
            UserConfigOverride(
                "battery_rate_max",
                [value],
                "user.battery_rate_max",
            ),
        ),
    )


class TestCompiledLatticePublication(unittest.TestCase):
    """Publication versions each changed durable successful input cursor."""

    def test_generation_cursor_and_changed_digest_each_publish_once(
        self,
    ):
        """Generation-only and semantic changes each advance lattice_version."""
        reader = MutableReader(snapshot("gateway", generation=1))
        store = InMemoryCompiledLatticeStateStore()
        compiler = CompiledLatticeCompiler(
            {"gateway": reader},
            state_store=store,
        )

        first = compiler.drain()

        self.assertTrue(first.published)
        self.assertEqual(first.publication.lattice_version, 1)
        self.assertEqual(store.writes, 1)

        reader.value = snapshot("gateway", generation=2)
        self.assertTrue(compiler.invalidate("gateway", 2, "generation bookkeeping"))
        unchanged = compiler.drain()

        self.assertTrue(unchanged.published)
        self.assertEqual(unchanged.publication.lattice_version, 2)
        self.assertEqual(unchanged.publication.digest, first.publication.digest)
        self.assertEqual(store.writes, 2)
        self.assertEqual(
            dict(unchanged.plan.provider_generations),
            {"gateway": 2},
        )
        self.assertIs(compiler.active_plan, unchanged.publication.plan)

        exact_repeat = compiler.drain()

        self.assertEqual(exact_repeat.attempts, 0)
        self.assertFalse(exact_repeat.published)
        self.assertIs(exact_repeat.publication, unchanged.publication)
        self.assertIs(exact_repeat.plan, unchanged.publication.plan)
        self.assertIs(compiler.active_plan, unchanged.publication.plan)
        self.assertEqual(store.writes, 2)

        reader.value = snapshot(
            "gateway",
            generation=3,
            node_id="INV2",
        )
        self.assertTrue(compiler.invalidate("gateway", 3, "new inverter identity"))
        changed = compiler.drain()

        self.assertTrue(changed.published)
        self.assertEqual(changed.publication.lattice_version, 3)
        self.assertEqual(store.writes, 3)
        self.assertNotEqual(
            changed.publication.digest,
            first.publication.digest,
        )
        self.assertEqual(
            changed.publication.invalidation_causes,
            (
                CompilationInvalidation(
                    "provider",
                    "gateway",
                    3,
                    "new inverter identity",
                ),
            ),
        )

    def test_generation_only_publication_restores_reuse_protection(self):
        """Restart restores the newest cursor even when its digest was unchanged."""
        reader = MutableReader(snapshot("gateway", generation=1))
        store = InMemoryCompiledLatticeStateStore()
        compiler = CompiledLatticeCompiler(
            {"gateway": reader},
            state_store=store,
        )
        compiler.drain()

        reader.value = snapshot("gateway", generation=2)
        self.assertTrue(compiler.invalidate("gateway", 2, "heartbeat cursor"))
        second = compiler.drain()

        self.assertTrue(second.published)
        self.assertEqual(second.publication.lattice_version, 2)
        self.assertEqual(
            dict(second.publication.provider_generations),
            {"gateway": 2},
        )

        restarted = CompiledLatticeCompiler(
            {"gateway": reader},
            state_store=store,
        )
        reader.value = snapshot(
            "gateway",
            generation=2,
            node_id="MUTATED",
        )
        failed = restarted.drain()

        self.assertEqual(failed.status, CompileStatus.STALE)
        self.assertIs(failed.publication, second.publication)
        self.assertIs(restarted.active_plan, second.publication.plan)
        self.assertIn(
            "provider_invalid",
            {issue.code for issue in failed.issues},
        )

    def test_offline_provider_cursor_is_durable_but_not_a_plan_contributor(self):
        """Offline inputs advance the cursor and retain restart reuse safety."""
        gateway = MutableReader(snapshot("gateway", generation=1))
        cloud = MutableReader(
            snapshot(
                "cloud",
                generation=1,
                node_id="CLOUD1",
                health=ProviderHealth.OFFLINE,
            )
        )
        store = InMemoryCompiledLatticeStateStore()
        compiler = CompiledLatticeCompiler(
            {"gateway": gateway, "cloud": cloud},
            state_store=store,
        )
        first = compiler.drain()

        self.assertEqual(
            dict(first.publication.provider_generations),
            {"cloud": 1, "gateway": 1},
        )
        self.assertEqual(
            dict(first.publication.plan.provider_generations),
            {"gateway": 1},
        )
        self.assertLessEqual(
            set(first.publication.plan.provider_generations),
            set(first.publication.provider_generations),
        )

        cloud.value = snapshot(
            "cloud",
            generation=2,
            node_id="CLOUD1",
            health=ProviderHealth.OFFLINE,
        )
        self.assertTrue(compiler.invalidate("cloud", 2, "offline heartbeat"))
        second = compiler.drain()

        self.assertTrue(second.published)
        self.assertEqual(second.publication.lattice_version, 2)
        self.assertEqual(second.publication.digest, first.publication.digest)
        self.assertEqual(
            dict(second.publication.provider_generations),
            {"cloud": 2, "gateway": 1},
        )
        durable_fingerprints = second.publication.provider_fingerprints

        exact_repeat = compiler.drain()
        self.assertEqual(exact_repeat.attempts, 0)
        self.assertFalse(exact_repeat.published)
        self.assertEqual(store.writes, 2)

        restarted = CompiledLatticeCompiler(
            {"gateway": gateway, "cloud": cloud},
            state_store=store,
        )
        cloud.value = snapshot(
            "cloud",
            generation=2,
            node_id="MUTATED",
            health=ProviderHealth.OFFLINE,
        )
        rejected = restarted.drain()

        self.assertEqual(rejected.status, CompileStatus.DEGRADED)
        self.assertTrue(rejected.published)
        self.assertEqual(rejected.publication.lattice_version, 3)
        self.assertEqual(
            rejected.publication.provider_fingerprints,
            durable_fingerprints,
        )
        self.assertIn(
            "provider_invalid",
            {issue.code for issue in rejected.issues},
        )
        self.assertIs(restarted.active_plan, rejected.publication.plan)

    def test_reader_failure_preserves_prior_cursor_and_requested_generation(self):
        """A reader failure cannot erase its cursor or acknowledged invalidation."""
        gateway = MutableReader(snapshot("gateway", generation=1))
        state = {
            "fail": False,
            "value": snapshot(
                "cloud",
                generation=1,
                health=ProviderHealth.OFFLINE,
            ),
        }

        def cloud_reader():
            """Return the cloud snapshot unless the simulated API is down."""
            if state["fail"]:
                raise RuntimeError("cloud API unavailable")
            return state["value"]

        store = InMemoryCompiledLatticeStateStore()
        compiler = CompiledLatticeCompiler(
            {"gateway": gateway, "cloud": cloud_reader},
            state_store=store,
        )
        first = compiler.drain()
        first_cloud_fingerprint = first.publication.provider_fingerprints[0]

        state["fail"] = True
        self.assertTrue(compiler.invalidate("cloud", 2, "cloud generation announced"))
        degraded = compiler.drain()

        self.assertTrue(degraded.published)
        self.assertEqual(
            dict(degraded.publication.provider_generations),
            {"cloud": 1, "gateway": 1},
        )
        self.assertEqual(
            degraded.publication.provider_fingerprints[0],
            first_cloud_fingerprint,
        )
        self.assertIn(
            "provider_read_failed",
            {issue.code for issue in degraded.issues},
        )
        self.assertEqual(
            dict(degraded.publication.provider_requested_generations),
            {"cloud": 2, "gateway": 1},
        )

        gateway.value = snapshot("gateway", generation=2)
        self.assertTrue(compiler.invalidate("gateway", 2, "unrelated gateway cursor"))
        later = compiler.drain()

        self.assertTrue(later.published)
        self.assertEqual(later.publication.lattice_version, 3)
        self.assertEqual(
            dict(later.publication.provider_requested_generations),
            {"cloud": 2, "gateway": 2},
        )
        self.assertNotIn(
            CompilationInvalidation(
                "provider",
                "cloud",
                2,
                "cloud generation announced",
            ),
            later.publication.invalidation_causes,
        )

        state["fail"] = False
        restarted = CompiledLatticeCompiler(
            {"gateway": gateway, "cloud": cloud_reader},
            state_store=store,
        )
        behind = restarted.drain()

        self.assertIn(
            "provider_invalid",
            {issue.code for issue in behind.issues},
        )
        self.assertIn(
            "behind invalidation 2",
            " ".join(issue.detail for issue in behind.issues),
        )

    def test_restart_filters_publication_only_unregistered_provider_cursor(self):
        """Removed readers are not misreported as freshly observed inputs."""
        gateway = MutableReader(snapshot("gateway", generation=1))
        cloud = MutableReader(
            snapshot(
                "cloud",
                generation=1,
                health=ProviderHealth.OFFLINE,
            )
        )
        store = InMemoryCompiledLatticeStateStore()
        CompiledLatticeCompiler(
            {"gateway": gateway, "cloud": cloud},
            state_store=store,
        ).drain()

        restarted = CompiledLatticeCompiler(
            {"gateway": gateway},
            state_store=store,
        )
        filtered = restarted.drain()

        self.assertTrue(filtered.published)
        self.assertEqual(filtered.publication.lattice_version, 2)
        self.assertEqual(
            dict(filtered.publication.provider_generations),
            {"gateway": 1},
        )
        self.assertEqual(
            filtered.publication.provider_generations,
            filtered.publication.plan.provider_generations,
        )

    def test_any_provider_or_override_invalidation_fresh_reads_every_input(
        self,
    ):
        """Every accepted cause reads all fragments and the complete overrides."""
        gateway = MutableReader(snapshot("gateway", generation=1))
        cloud = MutableReader(snapshot("cloud", generation=1))
        override_reader = MutableOverrideReader(UserOverrideSnapshot(1, ()))
        compiler = CompiledLatticeCompiler(
            {"gateway": gateway, "cloud": cloud},
            state_store=InMemoryCompiledLatticeStateStore(),
            override_reader=override_reader,
        )
        compiler.drain()

        gateway.value = snapshot("gateway", generation=2)
        self.assertTrue(compiler.invalidate("gateway", 2, "gateway discovery"))
        compiler.drain()

        override_reader.value = UserOverrideSnapshot(2, ())
        self.assertTrue(compiler.invalidate_user_overrides(2, "user config saved"))
        override_run = compiler.drain()

        self.assertEqual((gateway.calls, cloud.calls), (3, 3))
        self.assertEqual(override_reader.calls, 3)
        self.assertIn(
            CompilationInvalidation(
                "user_override",
                "user-overrides",
                2,
                "user config saved",
            ),
            override_run.causes,
        )

    def test_override_is_public_durable_input_and_reuse_fails_closed(self):
        """Override cursors publish even before an effective value changes."""
        provider = MutableReader(override_projection_snapshot())
        override_reader = MutableOverrideReader(overrides(1, 7))
        store = InMemoryCompiledLatticeStateStore()
        compiler = CompiledLatticeCompiler(
            {"gateway": provider},
            state_store=store,
            override_reader=override_reader,
        )

        first = compiler.drain()
        self.assertEqual(
            first.publication.plan.projected_config["battery_rate_max"],
            (7,),
        )
        self.assertEqual(first.publication.user_override_generation, 1)

        override_reader.value = overrides(2, 7)
        self.assertTrue(compiler.invalidate_user_overrides(2, "override cursor changed"))
        second = compiler.drain()

        self.assertTrue(second.published)
        self.assertEqual(second.publication.lattice_version, 2)
        self.assertEqual(second.publication.digest, first.publication.digest)
        self.assertEqual(second.publication.user_override_generation, 2)
        self.assertEqual(
            second.publication.plan.projected_config["battery_rate_max"],
            (7,),
        )

        override_reader.value = overrides(3, 8)
        self.assertTrue(compiler.invalidate_user_overrides(3, "override value changed"))
        third = compiler.drain()

        self.assertTrue(third.published)
        self.assertEqual(third.publication.lattice_version, 3)
        self.assertNotEqual(third.publication.digest, second.publication.digest)
        self.assertEqual(
            third.publication.plan.projected_config["battery_rate_max"],
            (8,),
        )

        override_reader.value = overrides(3, 9)
        provider.value = override_projection_snapshot(generation=2)
        self.assertTrue(compiler.invalidate("gateway", 2, "provider refresh"))
        failed = compiler.drain()

        self.assertEqual(failed.status, CompileStatus.STALE)
        self.assertIs(failed.publication, third.publication)
        self.assertIn(
            "user_overrides_invalid",
            {issue.code for issue in failed.issues},
        )

    def test_failed_compile_retains_lkg_and_causes_until_recovery(self):
        """A failed candidate never publishes and its causes survive retry."""
        reader = MutableReader(snapshot("gateway", generation=1))
        compiler = CompiledLatticeCompiler(
            {"gateway": reader},
            state_store=InMemoryCompiledLatticeStateStore(),
        )
        first = compiler.drain()

        bad = fragment("gateway", 2)
        bad["nodes"].append(dict(bad["nodes"][0]))
        reader.value = ProviderSnapshot(
            "gateway",
            2,
            ProviderHealth.HEALTHY,
            bad,
        )
        self.assertTrue(compiler.invalidate("gateway", 2, "conflicting rediscovery"))
        failed = compiler.drain()

        self.assertEqual(failed.status, CompileStatus.STALE)
        self.assertFalse(failed.published)
        self.assertIs(failed.publication, first.publication)
        self.assertEqual(first.publication.lattice_version, 1)

        reader.value = snapshot(
            "gateway",
            generation=3,
            node_id="INV2",
        )
        self.assertTrue(compiler.invalidate("gateway", 3, "corrected rediscovery"))
        recovered = compiler.drain()

        self.assertTrue(recovered.published)
        self.assertEqual(recovered.publication.lattice_version, 2)
        self.assertEqual(
            set(recovered.publication.invalidation_causes),
            {
                CompilationInvalidation(
                    "provider",
                    "gateway",
                    2,
                    "conflicting rediscovery",
                ),
                CompilationInvalidation(
                    "provider",
                    "gateway",
                    3,
                    "corrected rediscovery",
                ),
            },
        )

    def test_second_attempt_supersede_retains_all_causes(self):
        """A bounded follow-up superseded again publishes nothing until retry."""
        entered_first = threading.Event()
        release_first = threading.Event()
        entered_second = threading.Event()
        release_second = threading.Event()
        state = {
            "value": snapshot("gateway", generation=1),
            "calls": 0,
        }

        def reader():
            """Block the first two post-baseline reads after capturing state."""
            state["calls"] += 1
            value = state["value"]
            if state["calls"] == 2:
                entered_first.set()
                release_first.wait(5)
            elif state["calls"] == 3:
                entered_second.set()
                release_second.wait(5)
            return value

        compiler = CompiledLatticeCompiler(
            {"gateway": reader},
            state_store=InMemoryCompiledLatticeStateStore(),
        )
        baseline = compiler.drain().publication

        state["value"] = snapshot(
            "gateway",
            generation=2,
            node_id="INV2",
        )
        self.assertTrue(compiler.invalidate("gateway", 2, "generation two"))
        result = {}
        worker = threading.Thread(target=lambda: result.setdefault("run", compiler.drain()))
        worker.start()
        self.assertTrue(entered_first.wait(5))
        state["value"] = snapshot(
            "gateway",
            generation=3,
            node_id="INV3",
        )
        self.assertTrue(compiler.invalidate("gateway", 3, "generation three"))
        release_first.set()
        self.assertTrue(entered_second.wait(5))
        state["value"] = snapshot(
            "gateway",
            generation=4,
            node_id="INV4",
        )
        self.assertTrue(compiler.invalidate("gateway", 4, "generation four"))
        release_second.set()
        worker.join(5)

        superseded = result["run"]
        self.assertEqual(superseded.status, CompileStatus.STALE)
        self.assertFalse(superseded.published)
        self.assertIs(superseded.publication, baseline)
        self.assertTrue(superseded.pending)

        recovered = compiler.drain()
        self.assertTrue(recovered.published)
        self.assertEqual(
            {(cause.generation, cause.reason) for cause in recovered.publication.invalidation_causes},
            {
                (2, "generation two"),
                (3, "generation three"),
                (4, "generation four"),
            },
        )

    def test_store_conflict_is_fail_closed_and_retryable(self):
        """A rejected CAS keeps the LKG and retries the same next version."""
        store = RejectOnceStore()
        compiler = CompiledLatticeCompiler(
            {"gateway": MutableReader(snapshot("gateway"))},
            state_store=store,
        )

        rejected = compiler.drain()

        self.assertEqual(rejected.status, CompileStatus.STALE)
        self.assertIsNone(rejected.publication)
        self.assertIn(
            "publication_conflict",
            {issue.code for issue in rejected.issues},
        )
        self.assertTrue(rejected.pending)

        accepted = compiler.drain()

        self.assertTrue(accepted.published)
        self.assertEqual(accepted.publication.lattice_version, 1)
        self.assertEqual(store.writes, 1)

    def test_concurrent_same_input_cas_installs_exact_durable_plan(self):
        """A CAS loser adopts the winner's publication and exact plan object."""
        store = InMemoryCompiledLatticeStateStore()
        first_compiler = CompiledLatticeCompiler(
            {"gateway": MutableReader(snapshot("gateway"))},
            state_store=store,
        )
        second_compiler = CompiledLatticeCompiler(
            {"gateway": MutableReader(snapshot("gateway"))},
            state_store=store,
        )

        winner = first_compiler.drain()
        concurrent = second_compiler.drain()

        self.assertTrue(winner.published)
        self.assertFalse(concurrent.published)
        self.assertEqual(concurrent.status, CompileStatus.FRESH)
        self.assertIs(concurrent.publication, winner.publication)
        self.assertIs(concurrent.plan, winner.publication.plan)
        self.assertIs(second_compiler.active_plan, winner.publication.plan)
        self.assertEqual(store.writes, 1)

    def test_concurrent_same_input_retains_each_compiler_cause_once(self):
        """A CAS loser publishes a missing local cause in one follow-up version."""
        store = InMemoryCompiledLatticeStateStore()
        seed_reader = MutableReader(snapshot("gateway", generation=1))
        CompiledLatticeCompiler(
            {"gateway": seed_reader},
            state_store=store,
        ).drain()

        first_reader = MutableReader(snapshot("gateway", generation=2))
        second_reader = MutableReader(snapshot("gateway", generation=2))
        first_compiler = CompiledLatticeCompiler(
            {"gateway": first_reader},
            state_store=store,
        )
        second_compiler = CompiledLatticeCompiler(
            {"gateway": second_reader},
            state_store=store,
        )
        self.assertTrue(first_compiler.invalidate("gateway", 2, "compiler A discovery"))
        self.assertTrue(second_compiler.invalidate("gateway", 2, "compiler B discovery"))

        winner = first_compiler.drain()
        adopted = second_compiler.drain()

        self.assertTrue(winner.published)
        self.assertEqual(winner.publication.lattice_version, 2)
        self.assertIn(
            CompilationInvalidation(
                "provider",
                "gateway",
                2,
                "compiler A discovery",
            ),
            winner.publication.invalidation_causes,
        )
        self.assertFalse(adopted.published)
        self.assertTrue(adopted.pending)
        self.assertIs(adopted.publication, winner.publication)

        retained = second_compiler.drain()

        self.assertTrue(retained.published)
        self.assertFalse(retained.pending)
        self.assertEqual(retained.publication.lattice_version, 3)
        self.assertEqual(retained.publication.digest, winner.publication.digest)
        self.assertEqual(
            retained.publication.provider_generations,
            winner.publication.provider_generations,
        )
        self.assertEqual(
            retained.publication.invalidation_causes,
            (
                CompilationInvalidation(
                    "provider",
                    "gateway",
                    2,
                    "compiler B discovery",
                ),
            ),
        )

        exact_repeat = second_compiler.drain()

        self.assertEqual(exact_repeat.attempts, 0)
        self.assertFalse(exact_repeat.published)
        self.assertFalse(exact_repeat.pending)
        self.assertIs(exact_repeat.publication, retained.publication)
        self.assertEqual(store.writes, 3)

    def test_ambiguous_write_recovery_requires_exact_publication(self):
        """Same version, digest, and token cannot alias a different cursor."""
        store = AmbiguousDifferentCursorStore()
        compiler = CompiledLatticeCompiler(
            {"gateway": MutableReader(snapshot("gateway"))},
            state_store=store,
        )

        rejected = compiler.drain()

        self.assertEqual(rejected.status, CompileStatus.STALE)
        self.assertFalse(rejected.published)
        self.assertTrue(rejected.pending)
        self.assertEqual(
            dict(rejected.publication.provider_generations),
            {"gateway": 1, "offline": 7},
        )
        self.assertIn(
            "publication_failed",
            {issue.code for issue in rejected.issues},
        )
        self.assertIs(compiler.active_plan, rejected.publication.plan)

    def test_ambiguous_write_recovers_only_the_exact_publication(self):
        """Exact durable equality safely resolves a lost acknowledgement."""
        store = AmbiguousExactStore()
        compiler = CompiledLatticeCompiler(
            {"gateway": MutableReader(snapshot("gateway"))},
            state_store=store,
        )

        recovered = compiler.drain()

        self.assertEqual(recovered.status, CompileStatus.FRESH)
        self.assertTrue(recovered.published)
        self.assertFalse(recovered.pending)
        self.assertIs(recovered.publication, store.load())
        self.assertIs(compiler.active_plan, recovered.publication.plan)

    def test_restart_restores_generation_fingerprint_and_feedback_safety(self):
        """Durable state rejects replay, content reuse, and write feedback."""
        provider = MutableReader(override_projection_snapshot())
        override_reader = MutableOverrideReader(overrides(1, 7))
        store = InMemoryCompiledLatticeStateStore()
        first_compiler = CompiledLatticeCompiler(
            {"gateway": provider},
            state_store=store,
            override_reader=override_reader,
        )
        publication = first_compiler.drain().publication

        restarted = CompiledLatticeCompiler(
            {"gateway": provider},
            state_store=store,
            override_reader=override_reader,
        )

        self.assertFalse(restarted.invalidate("gateway", 1, "replayed generation"))
        self.assertFalse(
            restarted.invalidate(
                "gateway",
                2,
                "publication feedback",
                publication.feedback_token,
            )
        )
        self.assertFalse(
            restarted.invalidate_user_overrides(
                2,
                "publication feedback",
                publication.feedback_token,
            )
        )

        provider.value = snapshot(
            "gateway",
            generation=1,
            node_id="DIFFERENT",
        )
        failed = restarted.drain()

        self.assertEqual(failed.status, CompileStatus.STALE)
        self.assertIs(failed.publication, publication)
        self.assertIn(
            "provider_invalid",
            {issue.code for issue in failed.issues},
        )

    def test_restart_cannot_silently_drop_durable_override_input(self):
        """Observed or requested override state requires its reader on restart."""
        provider = MutableReader(override_projection_snapshot())
        override_reader = MutableOverrideReader(overrides(1, 7))
        store = InMemoryCompiledLatticeStateStore()
        compiler = CompiledLatticeCompiler(
            {"gateway": provider},
            state_store=store,
            override_reader=override_reader,
        )
        compiler.drain()

        with self.assertRaisesRegex(ValueError, "requires override_reader"):
            CompiledLatticeCompiler(
                {"gateway": provider},
                state_store=store,
            )

        plain_store = InMemoryCompiledLatticeStateStore()
        plain_publication = (
            CompiledLatticeCompiler(
                {"gateway": MutableReader(snapshot("gateway"))},
                state_store=plain_store,
            )
            .drain()
            .publication
        )
        requested_only = replace(
            plain_publication,
            user_override_requested_generation=2,
            invalidation_causes=(
                CompilationInvalidation(
                    "user_override",
                    "user-overrides",
                    2,
                    "override generation announced",
                ),
            ),
        )

        with self.assertRaisesRegex(ValueError, "requires override_reader"):
            CompiledLatticeCompiler(
                {"gateway": MutableReader(snapshot("gateway"))},
                state_store=InMemoryCompiledLatticeStateStore(requested_only),
            )

    def test_publication_is_deeply_immutable(self):
        """The public snapshot and its compiled plan cannot be mutated."""
        compiler = CompiledLatticeCompiler(
            {"gateway": MutableReader(snapshot("gateway"))},
            state_store=InMemoryCompiledLatticeStateStore(),
        )
        publication = compiler.drain().publication

        with self.assertRaises(FrozenInstanceError):
            publication.lattice_version = 9
        with self.assertRaises(TypeError):
            publication.plan.topology["scope"] = "site"

    def test_live_stale_diagnostics_do_not_mutate_durable_degraded_snapshot(
        self,
    ):
        """A later failure reports STALE beside the unchanged LKG snapshot."""
        gateway = MutableReader(snapshot("gateway", generation=1))
        offline = MutableReader(
            snapshot(
                "cloud",
                generation=1,
                health=ProviderHealth.OFFLINE,
            )
        )
        compiler = CompiledLatticeCompiler(
            {"gateway": gateway, "cloud": offline},
            state_store=InMemoryCompiledLatticeStateStore(),
        )
        first = compiler.drain()

        self.assertEqual(first.status, CompileStatus.DEGRADED)
        self.assertTrue(first.publication.diagnostics.degraded)
        self.assertFalse(first.publication.diagnostics.stale)

        gateway.value = snapshot(
            "gateway",
            generation=2,
            health=ProviderHealth.OFFLINE,
        )
        self.assertTrue(compiler.invalidate("gateway", 2, "gateway unavailable"))
        failed = compiler.drain()

        self.assertEqual(failed.status, CompileStatus.STALE)
        self.assertTrue(failed.diagnostics.stale)
        self.assertFalse(failed.diagnostics.degraded)
        self.assertIs(failed.publication, first.publication)
        self.assertEqual(
            failed.publication.diagnostics.status,
            CompileStatus.DEGRADED,
        )

    def test_publication_feedback_never_loops_for_any_input(self):
        """Published feedback tokens suppress provider and override causes."""
        provider = MutableReader(override_projection_snapshot())
        override_reader = MutableOverrideReader(overrides(1, 7))
        holder = {}

        class FeedbackStore(InMemoryCompiledLatticeStateStore):
            """Store that echoes publication feedback synchronously."""

            def compare_and_publish(self, expected_version, publication):
                """Commit, then echo the token through both invalidation APIs."""
                committed = super().compare_and_publish(
                    expected_version,
                    publication,
                )
                compiler = holder["compiler"]
                holder["feedback"] = (
                    compiler.invalidate(
                        "gateway",
                        2,
                        "published config observed",
                        publication.feedback_token,
                    ),
                    compiler.invalidate_user_overrides(
                        2,
                        "published config observed",
                        publication.feedback_token,
                    ),
                )
                return committed

        store = FeedbackStore()
        compiler = CompiledLatticeCompiler(
            {"gateway": provider},
            state_store=store,
            override_reader=override_reader,
        )
        holder["compiler"] = compiler

        run = compiler.drain()

        self.assertTrue(run.published)
        self.assertEqual(holder["feedback"], (False, False))
        self.assertFalse(run.pending)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(override_reader.calls, 1)

    def test_runtime_capabilities_are_preserved_in_durable_publication(self):
        """The publishing compiler records a runtime-materializable plan."""
        provider = MutableReader(override_projection_snapshot())
        compiler = CompiledLatticeCompiler(
            {"gateway": provider},
            state_store=InMemoryCompiledLatticeStateStore(),
            atomic_materializer=True,
            allow_provider_failover=True,
        )

        run = compiler.drain()

        self.assertTrue(run.published)
        self.assertTrue(run.publication.plan.materialization_readiness.ready)
        self.assertEqual(
            run.publication.plan.materialization_readiness.blockers,
            (),
        )


if __name__ == "__main__":
    unittest.main()
