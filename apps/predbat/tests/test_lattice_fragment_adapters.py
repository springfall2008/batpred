"""Tests for generic durable Lattice fragment adapter discovery."""

# cspell:ignore autoconfig

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_autoconfig import (  # noqa: E402
    CompileStatus,
    ProviderHealth,
)
from lattice_compiled_publication import (  # noqa: E402
    InMemoryCompiledLatticeStateStore,
)
from lattice_fragment_adapters import (  # noqa: E402
    DurableFragmentAdapter,
    FragmentAdapterConflict,
    FragmentAdapterReadError,
    FragmentAdapterRegistry,
    FragmentAdapterRemoved,
    FragmentAdapterState,
    InMemoryFragmentAdapterStateStore,
)
from tests.test_lattice_autoconfig import snapshot  # noqa: E402


class FragmentComponent:
    """Brand-neutral component exposing only the common discovery method."""

    def __init__(self, adapter):
        """Store the adapter returned during discovery."""
        self.adapter = adapter
        self.calls = 0

    def lattice_fragment_adapter(self):
        """Return the component-owned fragment publisher."""
        self.calls += 1
        return self.adapter


class UnrelatedComponent:
    """Component without any Lattice fragment publisher surface."""


class RaisingLoadStore(InMemoryFragmentAdapterStateStore):
    """State store whose durable read is unavailable."""

    def load(self):
        """Raise one representative durable-store fault."""
        raise OSError("disk unavailable")


class RejectingStore(InMemoryFragmentAdapterStateStore):
    """State store rejecting every atomic fragment write."""

    def compare_and_store(self, expected, replacement):
        """Reject the candidate without changing durable state."""
        return False


class ToggleLoadStore(InMemoryFragmentAdapterStateStore):
    """State store that can fail after an initial successful compilation."""

    def __init__(self):
        """Create an initially available durable store."""
        super().__init__()
        self.fail_reads = False

    def load(self):
        """Return state until the test makes durable reads unavailable."""
        if self.fail_reads:
            raise OSError("durable fragment unavailable")
        return super().load()


def publisher(provider_id, generation=1, health=ProviderHealth.HEALTHY):
    """Build one seeded durable generic publisher."""
    state_store = InMemoryFragmentAdapterStateStore()
    adapter = DurableFragmentAdapter(provider_id, state_store)
    initial = snapshot(
        provider_id,
        generation=generation,
        node_id="{}-INV".format(provider_id.upper()),
        health=health,
    )
    adapter.publish(initial, "initial discovery")
    return adapter, state_store, initial


def advance(
    current,
    generation,
    health=None,
    node_id=None,
):
    """Build the next immutable test snapshot without copying frozen mappings."""
    if health is None:
        health = current.health
    if node_id is None:
        node_id = current.topology_fragment["nodes"][0]["id"]
    return snapshot(
        current.provider_id,
        generation=generation,
        node_id=node_id,
        health=health,
    )


def compiled_registry(*adapters):
    """Discover generic components and create a durable compiler."""
    registry = FragmentAdapterRegistry(enabled=True)
    components = [
        UnrelatedComponent(),
    ] + [FragmentComponent(adapter) for adapter in adapters]
    discovered = registry.discover(components)
    compiled_store = InMemoryCompiledLatticeStateStore()
    compiler = registry.create_compiler(compiled_store)
    return registry, compiler, compiled_store, discovered


class TestDurableFragmentAdapter(unittest.TestCase):
    """Each integration owns a monotonic durable immutable fragment cursor."""

    def test_seed_and_fresh_reads_are_immutable_and_durable(self):
        """A published snapshot is detached and restored from its store."""
        adapter, state_store, initial = publisher("gateway")

        state = adapter.read_state()

        self.assertIsInstance(state, FragmentAdapterState)
        self.assertEqual(state.generation, 1)
        self.assertEqual(len(state.semantic_fingerprint), 64)
        self.assertIs(state.snapshot, initial)
        self.assertIs(adapter.read_snapshot(), initial)
        self.assertEqual(state_store.writes, 1)

        restarted = DurableFragmentAdapter("gateway", state_store)
        self.assertEqual(restarted.read_state(), state)
        self.assertIs(restarted.read_snapshot(), initial)

    def test_restart_rejects_regression_and_generation_reuse(self):
        """Durable cursor restoration rejects regressions and mutations."""
        _adapter, state_store, initial = publisher("cloud", generation=7)
        restarted = DurableFragmentAdapter("cloud", state_store)

        with self.assertRaisesRegex(ValueError, "regressed"):
            restarted.publish(
                advance(initial, 6),
                "stale cache",
            )
        with self.assertRaisesRegex(ValueError, "reused"):
            restarted.publish(
                advance(initial, 7, node_id="MUTATED"),
                "same generation mutation",
            )
        self.assertFalse(
            restarted.publish(initial, "exact replay"),
        )
        self.assertEqual(state_store.writes, 1)

    def test_reader_store_failure_is_wrapped_and_fails_closed(self):
        """No synthetic or empty fragment is returned on durable read failure."""
        store = RaisingLoadStore()

        with self.assertRaisesRegex(
            FragmentAdapterReadError,
            "disk unavailable",
        ):
            DurableFragmentAdapter("gateway", store)

    def test_conflicting_atomic_write_leaves_requested_generation_pending(self):
        """A rejected CAS never presents an uncommitted fragment as current."""
        adapter, seeded_store, initial = publisher("gateway")
        rejecting = RejectingStore(adapter.read_state())
        adapter = DurableFragmentAdapter("gateway", rejecting)
        _registry, compiler, _compiled_store, _discovered = compiled_registry(adapter)
        first = compiler.drain()
        self.assertEqual(first.status, CompileStatus.FRESH)

        updated = advance(initial, 2)
        with self.assertRaises(FragmentAdapterConflict):
            adapter.publish(updated, "new telemetry")

        failed = compiler.drain()
        self.assertEqual(failed.status, CompileStatus.STALE)
        self.assertTrue(failed.pending)
        self.assertEqual(adapter.read_state(), seeded_store.load())
        self.assertEqual(
            first.publication,
            compiler.publication,
        )

    def test_removal_is_durable_and_reader_fails_closed_after_restart(self):
        """Runtime removal is an auditable tombstone, never silent absence."""
        adapter, state_store, _initial = publisher("gateway")

        self.assertTrue(adapter.remove(2, "integration removed"))
        removed = adapter.read_state()

        self.assertTrue(removed.removed)
        self.assertEqual(removed.generation, 2)
        self.assertEqual(removed.snapshot.health, ProviderHealth.OFFLINE)
        with self.assertRaises(FragmentAdapterRemoved):
            adapter.read_snapshot()

        restarted = DurableFragmentAdapter("gateway", state_store)
        self.assertEqual(restarted.read_state(), removed)
        with self.assertRaises(FragmentAdapterRemoved):
            restarted.read_snapshot()


class TestFragmentAdapterRegistry(unittest.TestCase):
    """Any common-surface component can drive the compiled coordinator."""

    def test_registry_is_default_off_and_does_not_touch_components(self):
        """Disabled discovery performs no integration calls or registration."""
        adapter, _store, _initial = publisher("gateway")
        component = FragmentComponent(adapter)
        registry = FragmentAdapterRegistry()

        self.assertEqual(registry.discover([component]), ())
        self.assertEqual(component.calls, 0)
        self.assertEqual(registry.provider_ids, ())
        self.assertFalse(registry.register(adapter))
        self.assertFalse(registry.unregister("gateway"))
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            registry.create_compiler(InMemoryCompiledLatticeStateStore())

    def test_discovery_has_no_brand_allow_list(self):
        """Arbitrary provider IDs are discovered through the common surface."""
        alpha, _alpha_store, _alpha = publisher("future-cloud-alpha")
        beta, _beta_store, _beta = publisher("local-modbus-beta")

        registry, compiler, _store, discovered = compiled_registry(
            alpha,
            beta,
        )
        run = compiler.drain()

        self.assertEqual(
            discovered,
            ("future-cloud-alpha", "local-modbus-beta"),
        )
        self.assertEqual(
            registry.provider_ids,
            ("future-cloud-alpha", "local-modbus-beta"),
        )
        self.assertEqual(
            dict(run.publication.provider_generations),
            {
                "future-cloud-alpha": 1,
                "local-modbus-beta": 1,
            },
        )

    def test_invalid_discovery_batch_is_transactional(self):
        """Duplicate discovery does not partially mutate registry membership."""
        alpha, _alpha_store, _initial = publisher("same-provider")
        duplicate = DurableFragmentAdapter(
            "same-provider",
            _alpha_store,
        )
        registry = FragmentAdapterRegistry(enabled=True)

        with self.assertRaisesRegex(ValueError, "more than once"):
            registry.discover(
                [
                    FragmentComponent(alpha),
                    FragmentComponent(duplicate),
                ]
            )

        self.assertEqual(registry.provider_ids, ())

    def test_register_unregister_only_before_compiler_is_sealed(self):
        """Runtime membership changes cannot silently alter compiler inputs."""
        adapter, _store, _initial = publisher("gateway")
        registry = FragmentAdapterRegistry(enabled=True)

        self.assertTrue(registry.register(adapter))
        self.assertTrue(registry.unregister("gateway"))
        self.assertTrue(registry.register(adapter))
        compiler = registry.create_compiler(InMemoryCompiledLatticeStateStore())

        with self.assertRaisesRegex(RuntimeError, "tombstone"):
            registry.unregister("gateway")
        with self.assertRaisesRegex(RuntimeError, "sealed"):
            registry.register(adapter)
        self.assertEqual(compiler.drain().status, CompileStatus.FRESH)

    def test_any_registered_integration_invalidates_and_republishes(self):
        """Every discovered publisher can replace its prior fragment."""
        alpha, _alpha_store, alpha_one = publisher("alpha")
        beta, _beta_store, beta_one = publisher("beta")
        _registry, compiler, compiled_store, _ids = compiled_registry(
            alpha,
            beta,
        )
        first = compiler.drain()

        alpha_two = advance(alpha_one, 2)
        beta_two = advance(beta_one, 2)
        self.assertTrue(alpha.publish(alpha_two, "alpha refresh"))
        self.assertTrue(beta.publish(beta_two, "beta refresh"))
        second = compiler.drain()

        self.assertEqual(second.attempts, 1)
        self.assertTrue(second.published)
        self.assertEqual(second.publication.lattice_version, 2)
        self.assertEqual(
            dict(second.publication.provider_generations),
            {"alpha": 2, "beta": 2},
        )
        self.assertEqual(compiled_store.writes, 2)
        self.assertEqual(
            {(cause.source_id, cause.generation) for cause in second.publication.invalidation_causes},
            {("alpha", 2), ("beta", 2)},
        )
        self.assertEqual(first.publication.lattice_version, 1)

    def test_degraded_fragment_invalidates_and_publishes_degraded_cursor(self):
        """Health-only changes are durable inputs and trigger recompilation."""
        adapter, _store, initial = publisher("gateway")
        _registry, compiler, _compiled_store, _ids = compiled_registry(adapter)
        compiler.drain()

        degraded = advance(
            initial,
            2,
            health=ProviderHealth.DEGRADED,
        )
        self.assertTrue(adapter.publish(degraded, "provider health degraded"))
        run = compiler.drain()

        self.assertTrue(run.published)
        self.assertEqual(run.status, CompileStatus.DEGRADED)
        self.assertEqual(
            dict(run.publication.provider_generations),
            {"gateway": 2},
        )
        self.assertIn(
            "provider_degraded",
            {issue.code for issue in run.issues},
        )

    def test_offline_fragment_invalidates_but_preserves_last_known_good(self):
        """An active provider going offline fails closed after one attempt."""
        adapter, _store, initial = publisher("gateway")
        _registry, compiler, _compiled_store, _ids = compiled_registry(adapter)
        first = compiler.drain()

        offline = advance(
            initial,
            2,
            health=ProviderHealth.OFFLINE,
        )
        self.assertTrue(adapter.publish(offline, "provider disconnected"))
        run = compiler.drain()

        self.assertEqual(run.attempts, 1)
        self.assertEqual(run.status, CompileStatus.STALE)
        self.assertTrue(run.pending)
        self.assertIs(run.publication, first.publication)
        self.assertIs(run.plan, first.publication.plan)
        self.assertIn(
            "active_provider_unavailable",
            {issue.code for issue in run.issues},
        )

    def test_other_provider_invalidation_exposes_reader_failure_fail_closed(self):
        """A fresh-read failure blocks publication instead of dropping input."""
        gateway_store = ToggleLoadStore()
        gateway = DurableFragmentAdapter("gateway", gateway_store)
        gateway_one = snapshot("gateway", generation=1, node_id="GW-INV")
        gateway.publish(gateway_one, "initial gateway discovery")
        cloud, _cloud_store, cloud_one = publisher("cloud")
        _registry, compiler, _compiled_store, _ids = compiled_registry(
            gateway,
            cloud,
        )
        first = compiler.drain()

        gateway_store.fail_reads = True
        self.assertTrue(
            cloud.publish(
                advance(cloud_one, 2),
                "cloud refresh requires fresh read of every provider",
            )
        )
        run = compiler.drain()

        self.assertEqual(run.attempts, 1)
        self.assertEqual(run.status, CompileStatus.STALE)
        self.assertTrue(run.pending)
        self.assertIs(run.publication, first.publication)
        self.assertIn(
            "provider_read_failed",
            {issue.code for issue in run.issues},
        )

    def test_removal_invalidates_and_preserves_last_known_good(self):
        """A durable removal triggers a bounded fail-closed recompile."""
        adapter, _store, _initial = publisher("gateway")
        _registry, compiler, _compiled_store, _ids = compiled_registry(adapter)
        first = compiler.drain()

        self.assertTrue(adapter.remove(2, "integration disabled by user"))
        run = compiler.drain()

        self.assertEqual(run.attempts, 1)
        self.assertEqual(run.status, CompileStatus.STALE)
        self.assertTrue(run.pending)
        self.assertIs(run.publication, first.publication)
        self.assertIn(
            "provider_read_failed",
            {issue.code for issue in run.issues},
        )

    def test_restart_restores_adapter_and_compiler_cursor_protection(self):
        """Both durable layers reject reuse after a complete process restart."""
        adapter, adapter_store, initial = publisher("gateway")
        registry, compiler, compiled_store, _ids = compiled_registry(adapter)
        compiler.drain()
        second_snapshot = advance(initial, 2)
        self.assertTrue(adapter.publish(second_snapshot, "new discovery"))
        second = compiler.drain()
        self.assertEqual(second.publication.lattice_version, 2)

        restarted_adapter = DurableFragmentAdapter(
            "gateway",
            adapter_store,
        )
        restarted_registry, restarted_compiler, _same_store, _ids = compiled_registry_with_store(
            compiled_store,
            restarted_adapter,
        )
        exact = restarted_compiler.drain()

        self.assertEqual(
            restarted_registry.provider_ids,
            registry.provider_ids,
        )
        self.assertFalse(exact.published)
        self.assertEqual(exact.publication, second.publication)
        with self.assertRaisesRegex(ValueError, "reused"):
            restarted_adapter.publish(
                advance(second_snapshot, 2, node_id="REUSED"),
                "same cursor mutation after restart",
            )
        self.assertEqual(
            restarted_compiler.publication,
            second.publication,
        )

    def test_publication_feedback_token_does_not_persist_or_recompile(self):
        """Compiler-origin feedback is suppressed before adapter persistence."""
        adapter, state_store, initial = publisher("gateway")
        _registry, compiler, compiled_store, _ids = compiled_registry(adapter)
        first = compiler.drain()

        feedback_snapshot = advance(initial, 2)
        self.assertFalse(
            adapter.publish(
                feedback_snapshot,
                "published config observed",
                feedback_token=first.publication.feedback_token,
            )
        )
        idle = compiler.drain()

        self.assertEqual(adapter.read_state().generation, 1)
        self.assertEqual(state_store.writes, 1)
        self.assertEqual(compiled_store.writes, 1)
        self.assertEqual(idle.attempts, 0)
        self.assertIs(idle.publication, first.publication)

    def test_invalidation_during_read_gets_one_bounded_follow_up(self):
        """Concurrent adapter invalidation triggers exactly one fresh follow-up."""
        adapter, _store, initial = publisher("gateway")
        registry = FragmentAdapterRegistry(enabled=True)
        registry.register(adapter)
        original_reader = adapter.read_snapshot
        fired = [False]

        def invalidating_reader():
            """Publish one newer generation during the first compile read."""
            value = original_reader()
            if not fired[0]:
                fired[0] = True
                adapter.publish(
                    advance(initial, 2),
                    "concurrent rediscovery",
                )
            return value

        adapter.read_snapshot = invalidating_reader
        compiler = registry.create_compiler(InMemoryCompiledLatticeStateStore())
        run = compiler.drain()

        self.assertEqual(run.attempts, 2)
        self.assertTrue(run.published)
        self.assertEqual(
            dict(run.publication.provider_generations),
            {"gateway": 2},
        )


def compiled_registry_with_store(compiled_store, *adapters):
    """Create a restarted registry against an existing compiled store."""
    registry = FragmentAdapterRegistry(enabled=True)
    discovered = registry.discover([FragmentComponent(adapter) for adapter in adapters])
    compiler = registry.create_compiler(compiled_store)
    return registry, compiler, compiled_store, discovered


if __name__ == "__main__":
    unittest.main()
