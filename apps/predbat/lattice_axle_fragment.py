# -----------------------------------------------------------------------------
# Predbat Home Battery System - Axle reference-feed Lattice adapter
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Pure Axle flexibility-session normalization for Lattice fragments.

Managed site IDs and BYOK installation IDs are reduced to provider-local
digests.  Live VPP/session state is deliberately absent: observing a session
can affect planning, but it does not grant battery or account control
authority.  The live Axle component is not imported or registered.
"""

from dataclasses import dataclass

from lattice_reference_feed_fragment import (
    HEALTH_UNCHANGED,
    ReferenceFeedFragmentPublisher,
    ReferenceFeedSource,
    _bounded_text,
    canonical_provider_id,
    provider_local_digest,
)


_CONNECTION_KINDS = frozenset(("byok-local", "managed-site"))


@dataclass(frozen=True)
class AxleReferenceFeedSnapshot:
    """One provider-local flexibility-session observation source."""

    provider_id: str
    connection_kind: str
    source_digest: str
    entity_id: str

    def __post_init__(self):
        """Validate bounded non-correlating source metadata."""
        provider_id = canonical_provider_id(self.provider_id)
        connection_kind = _bounded_text(
            self.connection_kind,
            "connection_kind",
            32,
        ).lower()
        if connection_kind not in _CONNECTION_KINDS:
            raise ValueError(
                "connection_kind must be byok-local or managed-site",
            )
        digest = _bounded_text(
            self.source_digest,
            "source_digest",
            32,
        ).lower()
        if len(digest) != 32 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError(
                "source_digest must be a 32-character hexadecimal digest",
            )
        source = ReferenceFeedSource(
            source_id="{}-{}".format(connection_kind, digest),
            feed_type="flex-session",
            entity_id=self.entity_id,
        )
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "connection_kind", connection_kind)
        object.__setattr__(self, "source_digest", digest)
        object.__setattr__(self, "entity_id", source.entity_id)

    @classmethod
    def managed(
        cls,
        provider_id,
        site_id,
        entity_id,
        installation_privacy_key,
    ):
        """Normalize one provider-managed site without a global assertion."""
        provider_id = canonical_provider_id(provider_id)
        site_id = _bounded_text(site_id, "site_id", 256)
        return cls(
            provider_id=provider_id,
            connection_kind="managed-site",
            source_digest=provider_local_digest(
                provider_id,
                "axle-managed-site",
                site_id,
                installation_privacy_key,
            ),
            entity_id=entity_id,
        )

    @classmethod
    def byok(
        cls,
        provider_id,
        local_instance_id,
        entity_id,
        installation_privacy_key,
    ):
        """Normalize one installation-local BYOK identity.

        ``local_instance_id`` must be a stable installation-local opaque value,
        not an account token or credential.  Only its provider-scoped digest is
        retained.
        """
        provider_id = canonical_provider_id(provider_id)
        local_instance_id = _bounded_text(
            local_instance_id,
            "local_instance_id",
            256,
        )
        return cls(
            provider_id=provider_id,
            connection_kind="byok-local",
            source_digest=provider_local_digest(
                provider_id,
                "axle-byok-instance",
                local_instance_id,
                installation_privacy_key,
            ),
            entity_id=entity_id,
        )

    def reference_source(self):
        """Return the generic immutable source projection."""
        return ReferenceFeedSource(
            source_id="{}-{}".format(
                self.connection_kind,
                self.source_digest,
            ),
            feed_type="flex-session",
            entity_id=self.entity_id,
        )


class AxleReferenceFeedPublisher(ReferenceFeedFragmentPublisher):
    """Default-off publisher for explicit Axle feed snapshots."""

    def __init__(self, provider_id, state_store, enabled=False):
        """Create an unwired Axle reference publisher."""
        super().__init__(
            provider_id,
            "Axle Flex",
            state_store,
            enabled=enabled,
        )

    def ingest_snapshot(
        self,
        source_generation,
        snapshot,
        health=HEALTH_UNCHANGED,
        feedback_token=None,
    ):
        """Publish one explicit Axle source generation."""
        if not isinstance(snapshot, AxleReferenceFeedSnapshot):
            raise ValueError(
                "snapshot must be AxleReferenceFeedSnapshot",
            )
        if snapshot.provider_id != self.provider_id:
            raise ValueError("snapshot provider_id does not match publisher")
        return self.ingest_sources(
            source_generation,
            (snapshot.reference_source(),),
            health=health,
            feedback_token=feedback_token,
        )
