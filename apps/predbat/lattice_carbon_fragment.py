# -----------------------------------------------------------------------------
# Predbat Home Battery System - Carbon reference-feed Lattice adapter
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Pure Carbon reference-feed normalization over the generic fragment surface.

The live Carbon API component is intentionally not imported.  Raw postcode
data is accepted only by the normalizer and replaced immediately with a
provider-local opaque digest.  The resulting fragment is REFERENCE-only and
cannot grant configuration or control authority.
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


@dataclass(frozen=True)
class CarbonReferenceFeedSnapshot:
    """One carbon-intensity source with no retained raw location."""

    provider_id: str
    location_digest: str
    entity_id: str

    def __post_init__(self):
        """Validate only the opaque digest and local read reference."""
        provider_id = canonical_provider_id(self.provider_id)
        digest = _bounded_text(
            self.location_digest,
            "location_digest",
            32,
        ).lower()
        if len(digest) != 32 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError(
                "location_digest must be a 32-character hexadecimal digest",
            )
        source = ReferenceFeedSource(
            source_id="location-{}".format(digest),
            feed_type="carbon-intensity",
            entity_id=self.entity_id,
        )
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "location_digest", digest)
        object.__setattr__(self, "entity_id", source.entity_id)

    @classmethod
    def from_postcode(
        cls,
        provider_id,
        postcode,
        entity_id,
        installation_privacy_key,
    ):
        """Replace one raw postcode with a provider-local opaque digest."""
        provider_id = canonical_provider_id(provider_id)
        postcode = _bounded_text(postcode, "postcode", 32)
        normalized = "".join(postcode.upper().split())
        if not normalized:
            raise ValueError("postcode must contain non-whitespace characters")
        return cls(
            provider_id=provider_id,
            location_digest=provider_local_digest(
                provider_id,
                "carbon-location",
                normalized,
                installation_privacy_key,
            ),
            entity_id=entity_id,
        )

    def reference_source(self):
        """Return the generic immutable source projection."""
        return ReferenceFeedSource(
            source_id="location-{}".format(self.location_digest),
            feed_type="carbon-intensity",
            entity_id=self.entity_id,
        )


class CarbonReferenceFeedPublisher(ReferenceFeedFragmentPublisher):
    """Default-off publisher for explicit Carbon feed snapshots."""

    def __init__(self, provider_id, state_store, enabled=False):
        """Create an unwired Carbon reference publisher."""
        super().__init__(
            provider_id,
            "Carbon Intensity",
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
        """Publish one explicit Carbon source generation."""
        if not isinstance(snapshot, CarbonReferenceFeedSnapshot):
            raise ValueError(
                "snapshot must be CarbonReferenceFeedSnapshot",
            )
        if snapshot.provider_id != self.provider_id:
            raise ValueError("snapshot provider_id does not match publisher")
        return self.ingest_sources(
            source_generation,
            (snapshot.reference_source(),),
            health=health,
            feedback_token=feedback_token,
        )
