"""Tests for the generic pure reference-feed Lattice publisher."""

# cspell:ignore autoconfig

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_autoconfig import (  # noqa: E402
    AliasRole,
    CompileStatus,
    ProviderHealth,
    compile_auto_config,
)
from lattice_compiled_publication import (  # noqa: E402
    InMemoryCompiledLatticeStateStore,
)
from lattice_fragment_adapters import (  # noqa: E402
    FragmentAdapterConflict,
    FragmentAdapterReadError,
    FragmentAdapterRegistry,
    FragmentAdapterRemoved,
    InMemoryFragmentAdapterStateStore,
)
from lattice_reference_feed_fragment import (  # noqa: E402
    ReferenceFeedFragmentPublisher,
    ReferenceFeedSource,
    _length_prefixed_payload,
    provider_local_digest,
)


PRIVACY_KEY_A = b"reference-feed-privacy-key-a"
PRIVACY_KEY_B = b"reference-feed-privacy-key-b"


def assert_value_free_validation_error(test_case, action, raw_value):
    """Assert validation errors retain no secret-bearing source object."""
    secret_marker = raw_value[:-1]
    try:
        action()
    except ValueError as error:
        test_case.assertIs(type(error), ValueError)
        test_case.assertNotIn(secret_marker, str(error))
        test_case.assertNotIn(secret_marker, repr(error))
        test_case.assertNotIn(secret_marker, repr(error.args))
        test_case.assertNotIn(raw_value, error.args)
        test_case.assertFalse(hasattr(error, "object"))
        test_case.assertIsNone(error.__cause__)
        test_case.assertIsNone(error.__context__)
    else:
        test_case.fail("expected a value-free ValueError")


def source(
    source_id="source-a",
    feed_type="reference-data",
    entity_id="sensor.predbat_reference_data",
):
    """Build one bounded provider-local source."""
    return ReferenceFeedSource(
        source_id=source_id,
        feed_type=feed_type,
        entity_id=entity_id,
    )


def publisher(enabled=True, state_store=None):
    """Build one generic publisher with its durable store."""
    state_store = state_store or InMemoryFragmentAdapterStateStore()
    return (
        ReferenceFeedFragmentPublisher(
            "reference-provider",
            "Reference Provider",
            state_store,
            enabled=enabled,
        ),
        state_store,
    )


class TestReferenceFeedSource(unittest.TestCase):
    """Reference source metadata is bounded and deterministic."""

    def test_source_normalizes_without_accepting_arbitrary_metadata(self):
        """Only opaque identity, feed kind, and local entity are retained."""
        item = ReferenceFeedSource(
            "Source-A",
            "CARBON-INTENSITY",
            "SENSOR.PREDBAT_CARBON",
        )

        self.assertEqual(item.source_id, "Source-A")
        self.assertEqual(item.feed_type, "carbon-intensity")
        self.assertEqual(item.entity_id, "sensor.predbat_carbon")
        self.assertEqual(
            item.node_id("carbon-local"),
            "reference-feed:carbon-local:Source-A",
        )
        self.assertFalse(hasattr(item, "metadata"))

    def test_source_rejects_unbounded_or_non_entity_values(self):
        """Free-form metadata, paths, and oversized IDs cannot enter a fragment."""
        with self.assertRaisesRegex(ValueError, "opaque identifier"):
            ReferenceFeedSource("contains a secret", "feed")
        with self.assertRaisesRegex(ValueError, "at most 128"):
            ReferenceFeedSource("x" * 129, "feed")
        with self.assertRaisesRegex(ValueError, "lowercase"):
            ReferenceFeedSource("source", "not a feed")
        with self.assertRaisesRegex(ValueError, "Home Assistant"):
            ReferenceFeedSource("source", "feed", "not-an-entity")
        with self.assertRaisesRegex(ValueError, "control characters"):
            ReferenceFeedSource("source\x00secret", "feed")
        with self.assertRaisesRegex(ValueError, "control characters"):
            ReferenceFeedSource("source", "feed", "sensor.feed\nsecret")

    def test_digest_is_keyed_length_framed_and_strictly_bounded(self):
        """Digest fields cannot exploit old delimiter ambiguity or leak inputs."""
        old_left = "\x00".join(("a", "b\x00c", "d")).encode("utf-8")
        old_right = "\x00".join(("a\x00b", "c", "d")).encode("utf-8")
        self.assertEqual(old_left, old_right)
        self.assertNotEqual(
            _length_prefixed_payload(("a", "b\x00c", "d")),
            _length_prefixed_payload(("a\x00b", "c", "d")),
        )

        digest_a = provider_local_digest(
            "reference-provider",
            "reference-source",
            "private-source-value",
            PRIVACY_KEY_A,
        )
        digest_b = provider_local_digest(
            "reference-provider",
            "reference-source",
            "private-source-value",
            PRIVACY_KEY_B,
        )
        self.assertNotEqual(digest_a, digest_b)
        self.assertNotIn("private-source-value", digest_a)
        self.assertNotIn(PRIVACY_KEY_A.decode("ascii"), digest_a)

        invalid_values = (
            ("Reference-provider", "reference-source", "value", PRIVACY_KEY_A),
            ("reference-provider\x00x", "reference-source", "value", PRIVACY_KEY_A),
            ("x" * 129, "reference-source", "value", PRIVACY_KEY_A),
            ("reference-provider", "reference\x00source", "value", PRIVACY_KEY_A),
            ("reference-provider", "x" * 65, "value", PRIVACY_KEY_A),
            ("reference-provider", "reference-source", "value\x00secret", PRIVACY_KEY_A),
            ("reference-provider", "reference-source", "x" * 257, PRIVACY_KEY_A),
            ("reference-provider", "reference-source", "value", b"short"),
            ("reference-provider", "reference-source", "value", b"x" * 129),
            (
                "reference-provider",
                "reference-source",
                "value",
                "secret-key-as-text",
            ),
        )
        for values in invalid_values:
            with self.subTest(values=values[:3]):
                with self.assertRaises(ValueError) as raised:
                    provider_local_digest(*values)
                error = str(raised.exception)
                self.assertNotIn("value\x00secret", error)
                self.assertNotIn(PRIVACY_KEY_A.decode("ascii"), error)
                self.assertNotIn("secret-key-as-text", error)

    def test_digest_rejects_unpaired_surrogate_without_retaining_source(self):
        """Non-encodable identity text fails before UTF-8 encoding."""
        raw_value = "private-digest-source\ud800"
        assert_value_free_validation_error(
            self,
            lambda: provider_local_digest(
                "reference-provider",
                "reference-source",
                raw_value,
                PRIVACY_KEY_A,
            ),
            raw_value,
        )

    def test_digest_accepts_valid_non_ascii_printable_identity(self):
        """Printable Unicode scalar input is valid deterministic source data."""
        raw_value = "café-家庭"
        first = provider_local_digest(
            "reference-provider",
            "reference-source",
            raw_value,
            PRIVACY_KEY_A,
        )
        second = provider_local_digest(
            "reference-provider",
            "reference-source",
            raw_value,
            PRIVACY_KEY_A,
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)
        self.assertNotIn(raw_value, first)

    def test_publisher_requires_a_canonical_provider_id(self):
        """Provider namespaces cannot be normalized from ambiguous input."""
        for provider_id in (
            "Reference-provider",
            " reference-provider",
            "reference/provider",
            "reference-provider\x00other",
            "x" * 129,
        ):
            with self.subTest(provider_id=provider_id):
                with self.assertRaisesRegex(ValueError, "provider_id"):
                    ReferenceFeedFragmentPublisher(
                        provider_id,
                        "Reference Provider",
                        InMemoryFragmentAdapterStateStore(),
                    )


class TestReferenceFeedFragmentPublisher(unittest.TestCase):
    """A reference feed owns a monotonic durable immutable fragment."""

    def test_default_off_is_unseeded_unregistered_and_write_free(self):
        """Construction and disabled ingestion cannot discover or persist."""
        adapter, state_store = publisher(enabled=False)

        self.assertIsNone(adapter.lattice_fragment_adapter())
        self.assertFalse(adapter.ingest_sources(1, (source(),), health=True))
        self.assertEqual(state_store.writes, 0)
        with self.assertRaises(FragmentAdapterReadError):
            adapter.read_state()

    def test_snapshot_is_deterministic_reference_only_and_authority_free(self):
        """Source order cannot affect a fragment or manufacture authority."""
        adapter, state_store = publisher()
        sources = (
            source(
                "source-b",
                "flex-session",
                "binary_sensor.predbat_session",
            ),
            source(),
        )

        self.assertTrue(
            adapter.ingest_sources(
                7,
                reversed(sources),
                health=True,
            )
        )
        snapshot = adapter.read_snapshot()
        document = snapshot.topology_fragment

        self.assertEqual(adapter.source_generation, 7)
        self.assertEqual(adapter.generation, 1)
        self.assertEqual(state_store.writes, 1)
        self.assertEqual(snapshot.health, ProviderHealth.HEALTHY)
        self.assertEqual(
            tuple(node["id"] for node in document["nodes"]),
            (
                "reference-feed:reference-provider:source-a",
                "reference-feed:reference-provider:source-b",
            ),
        )
        self.assertEqual(document["producer"]["authority"], 0)
        self.assertTrue(all(node["kind"] == "data-source" for node in document["nodes"]))
        self.assertTrue(all(node["capabilities"] == () for node in document["nodes"]))
        self.assertTrue(all(alias.roles == frozenset((AliasRole.REFERENCE,)) for alias in snapshot.aliases))
        self.assertEqual(snapshot.identity_aliases, ())
        self.assertEqual(snapshot.role_assignments, ())
        self.assertEqual(snapshot.config_projections, ())

        plan = compile_auto_config((snapshot,))
        self.assertEqual(plan.role_assignments, ())
        self.assertEqual(plan.config_arguments, ())
        self.assertFalse(plan.materialization_readiness.ready)

    def test_source_generation_replay_regression_and_conflict(self):
        """Exact replay is inert and same-generation mutation fails closed."""
        adapter, state_store = publisher()
        initial = source()
        adapter.ingest_sources(4, (initial,), health=True)

        self.assertFalse(adapter.ingest_sources(4, (initial,)))
        self.assertFalse(adapter.ingest_sources(3, (initial,)))
        with self.assertRaisesRegex(
            FragmentAdapterConflict,
            "reused reference-feed source generation 4",
        ):
            adapter.ingest_sources(
                4,
                (
                    source(
                        entity_id="sensor.predbat_changed_reference",
                    ),
                ),
            )
        self.assertEqual(adapter.generation, 1)
        self.assertEqual(state_store.writes, 1)

        self.assertTrue(adapter.ingest_sources(5, (initial,)))
        self.assertEqual(adapter.source_generation, 5)
        self.assertEqual(adapter.generation, 2)

    def test_liveness_advances_without_changing_source_generation(self):
        """Health changes independently and exact replay is inert."""
        adapter, state_store = publisher()
        adapter.ingest_sources(9, (source(),), health=True)
        invalidations = []
        adapter.subscribe_invalidation(
            lambda provider_id, generation, reason, feedback_token: (
                invalidations.append(
                    (
                        provider_id,
                        generation,
                        reason,
                        feedback_token,
                    )
                )
            )
        )

        self.assertTrue(adapter.set_liveness(False))
        self.assertFalse(adapter.set_liveness(False))
        self.assertEqual(adapter.generation, 2)
        self.assertEqual(adapter.source_generation, 9)
        self.assertEqual(
            adapter.read_snapshot().health,
            ProviderHealth.OFFLINE,
        )
        self.assertEqual(state_store.writes, 2)
        self.assertEqual(invalidations[-1][0:2], ("reference-provider", 2))

    def test_compiler_invalidation_and_feedback_suppression(self):
        """Accepted changes recompile; publication feedback persists nothing."""
        adapter, state_store = publisher()
        adapter.ingest_sources(1, (source(),), health=True)
        registry = FragmentAdapterRegistry(enabled=True)
        self.assertEqual(
            registry.discover((adapter,)),
            ("reference-provider",),
        )
        compiler = registry.create_compiler(
            InMemoryCompiledLatticeStateStore(),
        )
        first = compiler.drain()
        self.assertEqual(first.status, CompileStatus.FRESH)

        self.assertTrue(
            adapter.ingest_sources(
                2,
                (
                    source(
                        entity_id="sensor.predbat_reference_data_v2",
                    ),
                ),
            )
        )
        second = compiler.drain()
        self.assertEqual(second.status, CompileStatus.FRESH)
        self.assertEqual(
            dict(second.publication.provider_generations),
            {"reference-provider": 2},
        )

        before = adapter.read_state()
        writes = state_store.writes
        self.assertFalse(
            adapter.ingest_sources(
                3,
                (
                    source(
                        entity_id="sensor.predbat_reference_data_v3",
                    ),
                ),
                feedback_token=second.publication.feedback_token,
            )
        )
        self.assertEqual(adapter.read_state(), before)
        self.assertEqual(state_store.writes, writes)
        self.assertFalse(compiler.drain().pending)

    def test_removal_is_durable_invalidating_and_irreversible(self):
        """Ambient replay cannot resurrect a removed reference source."""
        adapter, state_store = publisher()
        adapter.ingest_sources(5, (source(),), health=True)
        invalidations = []
        adapter.subscribe_invalidation(
            lambda provider_id, generation, reason, feedback_token: (
                invalidations.append(
                    (provider_id, generation, reason),
                )
            )
        )

        self.assertTrue(adapter.remove())
        self.assertFalse(adapter.remove())
        removed = adapter.read_state()
        self.assertTrue(removed.removed)
        self.assertEqual(removed.generation, 2)
        self.assertEqual(
            removed.snapshot.health,
            ProviderHealth.OFFLINE,
        )
        with self.assertRaises(FragmentAdapterRemoved):
            adapter.read_snapshot()
        with self.assertRaisesRegex(
            FragmentAdapterRemoved,
            "cannot re-enrol",
        ):
            adapter.ingest_sources(99, (source(),), health=True)
        self.assertEqual(
            invalidations[-1],
            (
                "reference-provider",
                2,
                "reference feed integration removed",
            ),
        )

        restarted = ReferenceFeedFragmentPublisher(
            "reference-provider",
            "Reference Provider",
            state_store,
            enabled=True,
        )
        self.assertTrue(restarted.read_state().removed)
        with self.assertRaises(FragmentAdapterRemoved):
            restarted.read_snapshot()
        with self.assertRaises(FragmentAdapterRemoved):
            restarted.ingest_sources(100, (source(),))


if __name__ == "__main__":
    unittest.main()
