"""Tests for the pure Axle reference-feed normalizer and publisher."""

# cspell:ignore autoconfig

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_autoconfig import AliasRole, ProviderHealth  # noqa: E402
from lattice_axle_fragment import (  # noqa: E402
    AxleReferenceFeedPublisher,
    AxleReferenceFeedSnapshot,
)
from lattice_fragment_adapters import (  # noqa: E402
    FragmentAdapterConflict,
    InMemoryFragmentAdapterStateStore,
)


PRIVACY_KEY_A = b"axle-installation-privacy-key-a"
PRIVACY_KEY_B = b"axle-installation-privacy-key-b"


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


class TestAxleReferenceFeedSnapshot(unittest.TestCase):
    """Managed and BYOK sources remain provider-local references."""

    def test_managed_site_is_opaque_and_provider_scoped(self):
        """A managed provider site never becomes a global identity alias."""
        first = AxleReferenceFeedSnapshot.managed(
            "axle-home",
            "site-live-123",
            "binary_sensor.predbat_axle_event",
            PRIVACY_KEY_A,
        )
        equivalent = AxleReferenceFeedSnapshot.managed(
            "axle-home",
            "site-live-123",
            "binary_sensor.predbat_axle_event",
            PRIVACY_KEY_A,
        )
        other_provider = AxleReferenceFeedSnapshot.managed(
            "axle-other",
            "site-live-123",
            "binary_sensor.predbat_axle_event",
            PRIVACY_KEY_A,
        )
        other_key = AxleReferenceFeedSnapshot.managed(
            "axle-home",
            "site-live-123",
            "binary_sensor.predbat_axle_event",
            PRIVACY_KEY_B,
        )

        self.assertEqual(first, equivalent)
        self.assertNotEqual(first.source_digest, other_provider.source_digest)
        self.assertNotEqual(first.source_digest, other_key.source_digest)
        self.assertEqual(first.provider_id, "axle-home")
        self.assertNotIn("site-live-123", repr(first))
        self.assertNotIn(PRIVACY_KEY_A.decode("ascii"), repr(first))
        self.assertEqual(first.connection_kind, "managed-site")

    def test_byok_uses_only_installation_local_opaque_input(self):
        """BYOK source material is hashed and cannot correlate globally."""
        first = AxleReferenceFeedSnapshot.byok(
            "axle-home",
            "installation-random-id",
            "binary_sensor.predbat_axle_event",
            PRIVACY_KEY_A,
        )
        other_provider = AxleReferenceFeedSnapshot.byok(
            "axle-other",
            "installation-random-id",
            "binary_sensor.predbat_axle_event",
            PRIVACY_KEY_A,
        )

        self.assertEqual(first.connection_kind, "byok-local")
        self.assertNotEqual(first.source_digest, other_provider.source_digest)
        self.assertNotIn("installation-random-id", repr(first))

    def test_connection_kind_and_digest_are_closed_fields(self):
        """Arbitrary account modes and raw tokens cannot enter metadata."""
        with self.assertRaisesRegex(ValueError, "connection_kind"):
            AxleReferenceFeedSnapshot(
                provider_id="axle-home",
                connection_kind="oauth-account",
                source_digest="0" * 32,
                entity_id="binary_sensor.predbat_axle_event",
            )
        with self.assertRaisesRegex(ValueError, "hexadecimal digest"):
            AxleReferenceFeedSnapshot(
                provider_id="axle-home",
                connection_kind="byok-local",
                source_digest="raw-account-token",
                entity_id="binary_sensor.predbat_axle_event",
            )

    def test_hash_inputs_are_control_free_bounded_and_private(self):
        """Site and BYOK inputs fail closed without retaining secrets."""
        for raw_value in ("site\x00secret", "site\nsecret", "x" * 257):
            with self.subTest(raw_value=raw_value):
                with self.assertRaises(ValueError) as raised:
                    AxleReferenceFeedSnapshot.managed(
                        "axle-home",
                        raw_value,
                        "binary_sensor.predbat_axle_event",
                        PRIVACY_KEY_A,
                    )
                error = str(raised.exception)
                self.assertNotIn(raw_value, error)
                self.assertNotIn(PRIVACY_KEY_A.decode("ascii"), error)

        with self.assertRaisesRegex(ValueError, "control characters"):
            AxleReferenceFeedSnapshot.byok(
                "axle-home",
                "local\x00secret",
                "binary_sensor.predbat_axle_event",
                PRIVACY_KEY_A,
            )

    def test_managed_site_surrogate_error_does_not_retain_source(self):
        """Non-encodable managed identifiers fail before UTF-8 encoding."""
        raw_value = "private-managed-site\ud800"
        assert_value_free_validation_error(
            self,
            lambda: AxleReferenceFeedSnapshot.managed(
                "axle-home",
                raw_value,
                "binary_sensor.predbat_axle_event",
                PRIVACY_KEY_A,
            ),
            raw_value,
        )

    def test_byok_surrogate_error_does_not_retain_source(self):
        """Non-encodable BYOK identifiers fail before UTF-8 encoding."""
        raw_value = "private-byok-identity\ud800"
        assert_value_free_validation_error(
            self,
            lambda: AxleReferenceFeedSnapshot.byok(
                "axle-home",
                raw_value,
                "binary_sensor.predbat_axle_event",
                PRIVACY_KEY_A,
            ),
            raw_value,
        )


class TestAxleReferenceFeedPublisher(unittest.TestCase):
    """Axle session observation never grants VPP or battery authority."""

    def test_managed_and_byok_are_reference_only(self):
        """No connection mode creates roles, projections, or capabilities."""
        for index, snapshot in enumerate(
            (
                AxleReferenceFeedSnapshot.managed(
                    "axle-{}".format(index),
                    "site-{}".format(index),
                    "binary_sensor.predbat_axle_event",
                    PRIVACY_KEY_A,
                )
                for index in range(2)
            )
        ):
            adapter = AxleReferenceFeedPublisher(
                "axle-{}".format(index),
                InMemoryFragmentAdapterStateStore(),
                enabled=True,
            )
            self.assertTrue(adapter.ingest_snapshot(1, snapshot, health=True))
            published = adapter.read_snapshot()

            self.assertEqual(published.health, ProviderHealth.HEALTHY)
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

    def test_session_entity_change_requires_new_source_generation(self):
        """A local observation seam cannot mutate one generation in place."""
        adapter = AxleReferenceFeedPublisher(
            "axle-home",
            InMemoryFragmentAdapterStateStore(),
            enabled=True,
        )
        first = AxleReferenceFeedSnapshot.byok(
            "axle-home",
            "installation-random-id",
            "binary_sensor.predbat_axle_event",
            PRIVACY_KEY_A,
        )
        changed = AxleReferenceFeedSnapshot.byok(
            "axle-home",
            "installation-random-id",
            "binary_sensor.predbat_axle_event_v2",
            PRIVACY_KEY_A,
        )
        adapter.ingest_snapshot(8, first, health=True)

        with self.assertRaises(FragmentAdapterConflict):
            adapter.ingest_snapshot(8, changed)
        self.assertTrue(adapter.ingest_snapshot(9, changed))

    def test_provider_mismatch_is_rejected_after_restart(self):
        """A directly constructed valid digest remains provider-bound."""
        state_store = InMemoryFragmentAdapterStateStore()
        adapter = AxleReferenceFeedPublisher(
            "axle-home",
            state_store,
            enabled=True,
        )
        matching = AxleReferenceFeedSnapshot.byok(
            "axle-home",
            "installation-random-id",
            "binary_sensor.predbat_axle_event",
            PRIVACY_KEY_A,
        )
        self.assertTrue(adapter.ingest_snapshot(1, matching, health=True))

        restarted = AxleReferenceFeedPublisher(
            "axle-home",
            state_store,
            enabled=True,
        )
        mismatched = AxleReferenceFeedSnapshot(
            provider_id="axle-other",
            connection_kind="byok-local",
            source_digest="f" * 32,
            entity_id="binary_sensor.predbat_axle_event",
        )
        before = restarted.read_state()
        with self.assertRaisesRegex(ValueError, "does not match") as raised:
            restarted.ingest_snapshot(2, mismatched)
        self.assertEqual(restarted.read_state(), before)
        self.assertNotIn("f" * 32, str(raised.exception))
        self.assertNotIn(PRIVACY_KEY_A.decode("ascii"), str(raised.exception))


if __name__ == "__main__":
    unittest.main()
