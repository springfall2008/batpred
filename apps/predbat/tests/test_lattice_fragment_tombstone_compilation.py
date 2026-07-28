"""Focused coverage for compiler-only fragment tombstone translation."""

# cspell:ignore autoconfig

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_autoconfig import CompileStatus, ProviderHealth  # noqa: E402
from lattice_compiled_publication import (  # noqa: E402
    InMemoryCompiledLatticeStateStore,
)
from lattice_fragment_adapters import (  # noqa: E402
    DurableFragmentAdapter,
    FragmentAdapterReadError,
    FragmentAdapterState,
    FragmentAdapterRegistry,
    FragmentAdapterRemoved,
    InMemoryFragmentAdapterStateStore,
    _compiler_fragment_snapshot,
)
from tests.test_lattice_autoconfig import snapshot  # noqa: E402


def assert_value_free_read_error(test_case, action, raw_value):
    """Assert compiler structural reads retain no source-controlled value."""
    try:
        action()
    except FragmentAdapterReadError as error:
        test_case.assertIs(type(error), FragmentAdapterReadError)
        test_case.assertNotIn(raw_value, str(error))
        test_case.assertNotIn(raw_value, repr(error))
        test_case.assertNotIn(raw_value, repr(error.args))
        test_case.assertNotIn(raw_value, error.args)
        test_case.assertIsNone(error.__cause__)
        test_case.assertIsNone(error.__context__)
    else:
        test_case.fail("expected a value-free fragment read error")


class HostileText(str):
    """String subclass whose scalar hooks expose a fake provider secret."""

    secret = "provider-secret-value"

    def _fail(self, *_args, **_kwargs):
        """Fail if validation invokes any subclass-controlled hook."""
        raise RuntimeError(self.secret)

    strip = _fail
    __format__ = _fail
    __iter__ = _fail
    __len__ = _fail
    __hash__ = _fail
    __eq__ = _fail
    __lt__ = _fail


class HostileMapping(dict):
    """Mapping whose traversal exposes a fake provider secret."""

    def items(self):
        """Fail if fingerprint validation traverses corrupt state."""
        raise RuntimeError(HostileText.secret)


class HostileFragmentAdapterState(FragmentAdapterState):
    """State subclass whose validator must never be dispatched."""

    def __post_init__(self):
        """Expose a fake secret if subclass dispatch occurs."""
        raise RuntimeError(HostileText.secret)


class StructuralAdapter:
    """Minimal compiler-facing adapter for hostile-read tests."""

    def __init__(self, provider_id, reader):
        """Store one provider scalar and structural reader."""
        self.provider_id = provider_id
        self._reader = reader

    def read_state(self):
        """Delegate to the hostile structural reader."""
        return self._reader()


def publisher(provider_id, node_id):
    """Create one seeded integration-owned fragment publisher."""
    store = InMemoryFragmentAdapterStateStore()
    adapter = DurableFragmentAdapter(provider_id, store)
    initial = snapshot(
        provider_id,
        generation=1,
        node_id=node_id,
    )
    adapter.publish(initial, "initial fragment")
    return adapter, store, initial


def compiler_for(store, *adapters):
    """Create one explicitly enabled compiler over fixed membership."""
    registry = FragmentAdapterRegistry(enabled=True)
    for adapter in adapters:
        registry.register(adapter)
    return registry, registry.create_compiler(store)


def structurally_corrupt_state(
    provider_id,
    generation,
    semantic_fingerprint,
    provider_snapshot,
    removed=False,
):
    """Bypass construction validation to model a corrupt structural reader."""
    state = object.__new__(FragmentAdapterState)
    object.__setattr__(state, "provider_id", provider_id)
    object.__setattr__(state, "generation", generation)
    object.__setattr__(
        state,
        "semantic_fingerprint",
        semantic_fingerprint,
    )
    object.__setattr__(state, "snapshot", provider_snapshot)
    object.__setattr__(state, "removed", removed)
    return state


class TestCompilerFragmentTombstones(unittest.TestCase):
    """Removal is empty compiler input without weakening adapter reads."""

    def test_live_snapshot_is_exact_and_removed_snapshot_is_empty(self):
        """Translation changes only a durable removal state."""
        adapter, _store, initial = publisher("gateway", "GW-INV")

        live = _compiler_fragment_snapshot(adapter)
        self.assertEqual(live, initial)
        self.assertIsNot(live, initial)
        self.assertTrue(adapter.remove(2, "integration removed"))

        with self.assertRaises(FragmentAdapterRemoved):
            adapter.read_snapshot()
        tombstone = _compiler_fragment_snapshot(adapter)

        self.assertEqual(tombstone.provider_id, "gateway")
        self.assertEqual(tombstone.generation, 2)
        self.assertEqual(tombstone.health, ProviderHealth.HEALTHY)
        self.assertEqual(tombstone.topology_fragment["nodes"], ())
        self.assertEqual(tombstone.topology_fragment["relationships"], ())
        self.assertEqual(tombstone.aliases, ())
        self.assertEqual(tombstone.identity_aliases, ())
        self.assertEqual(tombstone.role_assignments, ())
        self.assertEqual(tombstone.config_projections, ())

    def test_hostile_structural_reads_fail_value_free_without_dispatch(self):
        """Compiler reads sanitize faults, subclasses, and corrupt snapshots."""
        adapter, state_store, _initial = publisher(
            "gateway",
            "GW-INV",
        )
        state = state_store.load()
        hostile_state = object.__new__(
            HostileFragmentAdapterState,
        )
        for field in (
            "provider_id",
            "generation",
            "semantic_fingerprint",
            "snapshot",
            "removed",
        ):
            object.__setattr__(
                hostile_state,
                field,
                getattr(state, field),
            )

        cases = (
            StructuralAdapter(
                "gateway",
                lambda: (_ for _ in ()).throw(
                    RuntimeError(HostileText.secret),
                ),
            ),
            StructuralAdapter(
                HostileText("gateway"),
                lambda: state,
            ),
            StructuralAdapter(
                "gateway",
                lambda: hostile_state,
            ),
        )
        for structural_adapter in cases:
            with self.subTest(adapter=structural_adapter):
                writes = state_store.writes
                assert_value_free_read_error(
                    self,
                    lambda: _compiler_fragment_snapshot(
                        structural_adapter,
                    ),
                    HostileText.secret,
                )
                self.assertEqual(state_store.writes, writes)

        object.__setattr__(
            state.snapshot,
            "topology_fragment",
            HostileMapping(),
        )
        writes = state_store.writes
        assert_value_free_read_error(
            self,
            lambda: _compiler_fragment_snapshot(adapter),
            HostileText.secret,
        )
        self.assertEqual(state_store.writes, writes)

    def test_all_removed_publishes_deterministic_empty_plan_and_restarts(self):
        """All tombstones settle, persist, and restore as one empty plan."""
        alpha, alpha_store, _alpha = publisher("alpha", "ALPHA-INV")
        beta, beta_store, _beta = publisher("beta", "BETA-INV")
        compiled_store = InMemoryCompiledLatticeStateStore()
        _registry, compiler = compiler_for(compiled_store, alpha, beta)
        baseline = compiler.drain()

        self.assertTrue(beta.remove(2, "beta removed"))
        self.assertTrue(alpha.remove(2, "alpha removed"))
        removed = compiler.drain()

        self.assertEqual(removed.status, CompileStatus.FRESH)
        self.assertTrue(removed.published)
        self.assertFalse(removed.pending)
        self.assertEqual(removed.plan.topology["nodes"], ())
        self.assertEqual(removed.plan.aliases, ())
        self.assertEqual(
            dict(removed.publication.provider_generations),
            {"alpha": 2, "beta": 2},
        )
        self.assertEqual(
            dict(removed.publication.provider_requested_generations),
            {"alpha": 2, "beta": 2},
        )
        self.assertNotEqual(
            removed.publication.digest,
            baseline.publication.digest,
        )

        restarted_alpha = DurableFragmentAdapter("alpha", alpha_store)
        restarted_beta = DurableFragmentAdapter("beta", beta_store)
        _registry, restarted = compiler_for(
            compiled_store,
            restarted_beta,
            restarted_alpha,
        )
        settled = restarted.drain()

        self.assertFalse(settled.published)
        self.assertFalse(settled.pending)
        self.assertEqual(settled.publication, removed.publication)
        self.assertEqual(settled.plan.digest, removed.plan.digest)

    def test_removed_payload_cannot_reappear_at_the_same_generation(self):
        """A tombstone never leaks its payload and cursor reuse remains unsafe."""
        adapter, _store, initial = publisher("cloud", "CLOUD-INV")
        self.assertTrue(adapter.remove(2, "cloud removed"))

        empty = _compiler_fragment_snapshot(adapter)
        self.assertEqual(empty.topology_fragment["nodes"], ())
        with self.assertRaisesRegex(ValueError, "reused"):
            adapter.publish(
                snapshot(
                    "cloud",
                    generation=2,
                    node_id=initial.topology_fragment["nodes"][0]["id"],
                ),
                "attempted resurrection",
            )
        self.assertEqual(
            _compiler_fragment_snapshot(adapter).topology_fragment["nodes"],
            (),
        )

    def test_fresh_read_revalidates_semantic_fingerprint_and_keeps_lkg(self):
        """Post-registration state corruption cannot reach publication."""
        adapter, _store, _initial = publisher("gateway", "GW-INV")
        compiled_store = InMemoryCompiledLatticeStateStore()
        _registry, compiler = compiler_for(compiled_store, adapter)
        baseline = compiler.drain()
        corrupt = structurally_corrupt_state(
            "gateway",
            2,
            "0" * 64,
            snapshot(
                "gateway",
                generation=2,
                node_id="GW-INV",
            ),
        )
        adapter.read_state = lambda: corrupt

        self.assertTrue(
            compiler.invalidate(
                "gateway",
                2,
                "corrupt structural read",
            )
        )
        failed = compiler.drain()

        self.assertEqual(failed.status, CompileStatus.STALE)
        self.assertTrue(failed.pending)
        self.assertFalse(failed.published)
        self.assertIs(failed.publication, baseline.publication)
        self.assertEqual(compiled_store.writes, 1)
        self.assertIn(
            "provider_read_failed",
            {issue.code for issue in failed.issues},
        )

    def test_fresh_read_revalidates_snapshot_cursor_bindings(self):
        """Provider and generation binding corruption both fail closed."""
        cases = (
            (
                "provider",
                snapshot(
                    "other-provider",
                    generation=2,
                    node_id="GW-INV",
                ),
            ),
            (
                "generation",
                snapshot(
                    "gateway",
                    generation=3,
                    node_id="GW-INV",
                ),
            ),
        )
        for label, corrupt_snapshot in cases:
            with self.subTest(binding=label):
                adapter, _store, _initial = publisher(
                    "gateway",
                    "GW-INV",
                )
                compiled_store = InMemoryCompiledLatticeStateStore()
                _registry, compiler = compiler_for(
                    compiled_store,
                    adapter,
                )
                baseline = compiler.drain()
                corrupt = structurally_corrupt_state(
                    "gateway",
                    2,
                    adapter.read_state().semantic_fingerprint,
                    corrupt_snapshot,
                )
                adapter.read_state = lambda value=corrupt: value

                self.assertTrue(
                    compiler.invalidate(
                        "gateway",
                        2,
                        "{} binding corruption".format(label),
                    )
                )
                failed = compiler.drain()

                self.assertEqual(failed.status, CompileStatus.STALE)
                self.assertTrue(failed.pending)
                self.assertFalse(failed.published)
                self.assertIs(
                    failed.publication,
                    baseline.publication,
                )
                self.assertEqual(compiled_store.writes, 1)
                self.assertIn(
                    "provider_read_failed",
                    {issue.code for issue in failed.issues},
                )


if __name__ == "__main__":
    unittest.main()
