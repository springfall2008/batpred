"""Tests for the pure Carbon reference-feed normalizer and publisher."""

# cspell:ignore autoconfig

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_autoconfig import AliasRole, ProviderHealth  # noqa: E402
from lattice_carbon_fragment import (  # noqa: E402
    CarbonReferenceFeedPublisher,
    CarbonReferenceFeedSnapshot,
)
from lattice_fragment_adapters import (  # noqa: E402
    FragmentAdapterConflict,
    FragmentAdapterReadError,
    InMemoryFragmentAdapterStateStore,
)


PRIVACY_KEY_A = b"carbon-installation-privacy-key-a"
PRIVACY_KEY_B = b"carbon-installation-privacy-key-b"


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


class TestCarbonReferenceFeedSnapshot(unittest.TestCase):
    """Raw location input never enters durable fragment metadata."""

    def test_postcode_is_replaced_by_provider_local_opaque_digest(self):
        """Equivalent postcodes normalize while raw location is absent."""
        first = CarbonReferenceFeedSnapshot.from_postcode(
            "carbon-home",
            "SW1A 1AA",
            "sensor.predbat_carbon_intensity",
            PRIVACY_KEY_A,
        )
        equivalent = CarbonReferenceFeedSnapshot.from_postcode(
            "carbon-home",
            "sw1a1aa",
            "sensor.predbat_carbon_intensity",
            PRIVACY_KEY_A,
        )
        other_provider = CarbonReferenceFeedSnapshot.from_postcode(
            "carbon-other",
            "SW1A 1AA",
            "sensor.predbat_carbon_intensity",
            PRIVACY_KEY_A,
        )
        other_key = CarbonReferenceFeedSnapshot.from_postcode(
            "carbon-home",
            "SW1A 1AA",
            "sensor.predbat_carbon_intensity",
            PRIVACY_KEY_B,
        )

        self.assertEqual(first, equivalent)
        self.assertNotEqual(
            first.location_digest,
            other_provider.location_digest,
        )
        self.assertNotEqual(first.location_digest, other_key.location_digest)
        self.assertEqual(first.provider_id, "carbon-home")
        self.assertEqual(len(first.location_digest), 32)
        self.assertNotIn("SW1A", repr(first))
        self.assertNotIn("sw1a", repr(first))
        self.assertNotIn(PRIVACY_KEY_A.decode("ascii"), repr(first))

    def test_snapshot_rejects_non_digest_identity(self):
        """A caller cannot persist a raw postcode in the digest field."""
        with self.assertRaisesRegex(ValueError, "hexadecimal digest"):
            CarbonReferenceFeedSnapshot(
                provider_id="carbon-home",
                location_digest="SW1A 1AA",
                entity_id="sensor.predbat_carbon_intensity",
            )

    def test_postcode_hash_inputs_are_control_free_and_bounded(self):
        """Raw locations and provider IDs fail closed without disclosure."""
        for postcode in ("SW1A\x001AA", "SW1A\n1AA", "x" * 33):
            with self.subTest(postcode=postcode):
                with self.assertRaises(ValueError) as raised:
                    CarbonReferenceFeedSnapshot.from_postcode(
                        "carbon-home",
                        postcode,
                        "sensor.predbat_carbon_intensity",
                        PRIVACY_KEY_A,
                    )
                error = str(raised.exception)
                self.assertNotIn(postcode, error)
                self.assertNotIn(PRIVACY_KEY_A.decode("ascii"), error)

        for provider_id in ("Carbon-home", "carbon-home\x00other", "x" * 129):
            with self.subTest(provider_id=provider_id):
                with self.assertRaisesRegex(ValueError, "provider_id"):
                    CarbonReferenceFeedSnapshot.from_postcode(
                        provider_id,
                        "SW1A 1AA",
                        "sensor.predbat_carbon_intensity",
                        PRIVACY_KEY_A,
                    )

    def test_postcode_rejects_unpaired_surrogate_without_retaining_source(self):
        """Non-encodable postcode input fails with a value-free error."""
        raw_value = "private-postcode\ud800"
        assert_value_free_validation_error(
            self,
            lambda: CarbonReferenceFeedSnapshot.from_postcode(
                "carbon-home",
                raw_value,
                "sensor.predbat_carbon_intensity",
                PRIVACY_KEY_A,
            ),
            raw_value,
        )


class TestCarbonReferenceFeedPublisher(unittest.TestCase):
    """Carbon adapter remains pure, default-off, and REFERENCE-only."""

    def test_default_off_and_explicit_ingestion(self):
        """Nothing is written until an enabled caller supplies a snapshot."""
        state_store = InMemoryFragmentAdapterStateStore()
        adapter = CarbonReferenceFeedPublisher(
            "carbon-home",
            state_store,
        )
        snapshot = CarbonReferenceFeedSnapshot.from_postcode(
            "carbon-home",
            "SW1A 1AA",
            "sensor.predbat_carbon_intensity",
            PRIVACY_KEY_A,
        )

        self.assertFalse(adapter.ingest_snapshot(1, snapshot, health=True))
        self.assertEqual(state_store.writes, 0)
        with self.assertRaises(FragmentAdapterReadError):
            adapter.read_state()

        adapter = CarbonReferenceFeedPublisher(
            "carbon-home",
            state_store,
            enabled=True,
        )
        self.assertTrue(adapter.ingest_snapshot(1, snapshot, health=True))
        published = adapter.read_snapshot()
        serialized = repr(published)

        self.assertEqual(published.health, ProviderHealth.HEALTHY)
        self.assertNotIn("SW1A", serialized)
        self.assertNotIn(PRIVACY_KEY_A.decode("ascii"), serialized)
        self.assertEqual(
            published.aliases[0].roles,
            frozenset((AliasRole.REFERENCE,)),
        )
        self.assertEqual(published.identity_aliases, ())
        self.assertEqual(published.role_assignments, ())
        self.assertEqual(published.config_projections, ())
        self.assertEqual(
            published.topology_fragment["nodes"][0]["capabilities"],
            (),
        )
        self.assertEqual(
            published.topology_fragment["producer"]["authority"],
            0,
        )

    def test_location_change_requires_a_new_source_generation(self):
        """A changed opaque location cannot reuse a source generation."""
        adapter = CarbonReferenceFeedPublisher(
            "carbon-home",
            InMemoryFragmentAdapterStateStore(),
            enabled=True,
        )
        first = CarbonReferenceFeedSnapshot.from_postcode(
            "carbon-home",
            "SW1A 1AA",
            "sensor.predbat_carbon_intensity",
            PRIVACY_KEY_A,
        )
        changed = CarbonReferenceFeedSnapshot.from_postcode(
            "carbon-home",
            "EH1 1YZ",
            "sensor.predbat_carbon_intensity",
            PRIVACY_KEY_A,
        )
        adapter.ingest_snapshot(4, first, health=True)

        with self.assertRaises(FragmentAdapterConflict):
            adapter.ingest_snapshot(4, changed)
        self.assertTrue(adapter.ingest_snapshot(5, changed))

    def test_provider_mismatch_is_rejected_after_restart(self):
        """A valid opaque digest cannot cross a provider boundary."""
        state_store = InMemoryFragmentAdapterStateStore()
        adapter = CarbonReferenceFeedPublisher(
            "carbon-home",
            state_store,
            enabled=True,
        )
        matching = CarbonReferenceFeedSnapshot.from_postcode(
            "carbon-home",
            "SW1A 1AA",
            "sensor.predbat_carbon_intensity",
            PRIVACY_KEY_A,
        )
        self.assertTrue(adapter.ingest_snapshot(1, matching, health=True))

        restarted = CarbonReferenceFeedPublisher(
            "carbon-home",
            state_store,
            enabled=True,
        )
        mismatched = CarbonReferenceFeedSnapshot(
            provider_id="carbon-other",
            location_digest="0" * 32,
            entity_id="sensor.predbat_carbon_intensity",
        )
        before = restarted.read_state()
        with self.assertRaisesRegex(ValueError, "does not match") as raised:
            restarted.ingest_snapshot(2, mismatched)
        self.assertEqual(restarted.read_state(), before)
        self.assertNotIn("0" * 32, str(raised.exception))
        self.assertNotIn(PRIVACY_KEY_A.decode("ascii"), str(raised.exception))


if __name__ == "__main__":
    unittest.main()
