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
    FragmentAdapterState,
    FragmentAdapterRegistry,
    FragmentAdapterRemoved,
    InMemoryFragmentAdapterStateStore,
    _compiler_fragment_snapshot,
)
from tests.test_lattice_autoconfig import snapshot  # noqa: E402


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

        self.assertIs(_compiler_fragment_snapshot(adapter), initial)
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
