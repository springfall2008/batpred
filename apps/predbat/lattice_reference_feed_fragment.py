# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice reference-feed fragment adapter
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Pure, default-off publisher for provider-local reference data feeds.

Reference feeds are observations such as carbon intensity or a flexibility
session.  Their presence may influence planning, but it does not prove control
authority over a battery, inverter, charger, or provider account.  This module
therefore publishes only provider-qualified REFERENCE aliases.  It never emits
strong cross-provider identity assertions, role assignments, configuration
projections, or topology capabilities.

The caller owns discovery and supplies a monotonically increasing source
generation.  Accepted source, liveness, and removal changes are published
through the existing durable fragment contract and invalidate its compiler
subscribers.  Nothing in this module imports or registers a live integration.
"""

# cspell:ignore autoconfig

import hashlib
import hmac
import re
import threading
from dataclasses import dataclass
from typing import Optional

from lattice_autoconfig import (
    AliasRole,
    ProviderAlias,
    ProviderHealth,
    ProviderSnapshot,
    _plain,
)
from lattice_fragment_adapters import (
    DurableFragmentAdapter,
    FragmentAdapterConflict,
    FragmentAdapterReadError,
    FragmentAdapterRemoved,
)


HEALTH_UNCHANGED = object()
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_FEED_TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ENTITY_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_PRIVACY_KEY_MINIMUM = 16
_PRIVACY_KEY_MAXIMUM = 128
_DIGEST_PROTOCOL = b"predbat-reference-feed-v1"


def _bounded_text(value, name, maximum):
    """Return bounded text containing only UTF-8-encodable Unicode scalars."""
    if not isinstance(value, str):
        raise ValueError("{} must be a non-empty string".format(name))
    if len(value) > maximum:
        raise ValueError("{} must be at most {} characters".format(name, maximum))
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("{} must not contain control characters".format(name))
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError("{} must contain only UTF-8 encodable text".format(name))
    normalized = value.strip()
    if not normalized:
        raise ValueError("{} must be a non-empty string".format(name))
    return normalized


def canonical_provider_id(value):
    """Validate one bounded canonical provider identifier."""
    normalized = _bounded_text(value, "provider_id", 128)
    if normalized != value or _PROVIDER_ID_RE.fullmatch(normalized) is None:
        raise ValueError(
            "provider_id must use canonical lowercase identifier characters",
        )
    return normalized


def _installation_privacy_key(value):
    """Validate but never retain or render one caller-owned privacy key."""
    if not isinstance(value, bytes):
        raise ValueError("installation_privacy_key must be bytes")
    if not _PRIVACY_KEY_MINIMUM <= len(value) <= _PRIVACY_KEY_MAXIMUM:
        raise ValueError(
            "installation_privacy_key must be between 16 and 128 bytes",
        )
    return value


def _length_prefixed_payload(values):
    """Frame digest inputs unambiguously before keyed hashing."""
    payload = bytearray(_DIGEST_PROTOCOL)
    for value in values:
        encoded = value.encode("utf-8")
        payload.extend(len(encoded).to_bytes(4, byteorder="big"))
        payload.extend(encoded)
    return bytes(payload)


def provider_local_digest(
    provider_id,
    namespace,
    value,
    installation_privacy_key,
):
    """Return a keyed opaque digest scoped to one provider installation.

    The raw value and caller-owned key are used only for this HMAC operation
    and are never retained.  Length framing prevents tuple ambiguity.  This is
    a provider-local reference key, not a globally correlatable identity alias.
    """
    provider_id = canonical_provider_id(provider_id)
    namespace = _bounded_text(namespace, "namespace", 64)
    if _PROVIDER_ID_RE.fullmatch(namespace) is None:
        raise ValueError(
            "namespace must use canonical lowercase identifier characters",
        )
    value = _bounded_text(value, "identity value", 256)
    privacy_key = _installation_privacy_key(installation_privacy_key)
    payload = _length_prefixed_payload((provider_id, namespace, value))
    return hmac.new(privacy_key, payload, hashlib.sha256).hexdigest()[:32]


def _source_generation(value):
    """Validate one caller-owned monotonic source generation."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("source_generation must be a non-negative integer")
    return value


def _provider_health(value):
    """Normalize explicit integration liveness into compiler health."""
    if isinstance(value, ProviderHealth):
        return value
    if value is True:
        return ProviderHealth.HEALTHY
    if value is False:
        return ProviderHealth.OFFLINE
    if value is None:
        return ProviderHealth.DEGRADED
    raise ValueError("health must be ProviderHealth, True, False, or None")


@dataclass(frozen=True)
class ReferenceFeedSource:
    """One bounded provider-local observation source.

    ``source_id`` must already be a non-secret opaque identifier.  It is never
    promoted to ``ProviderIdentityAlias`` and therefore cannot correlate nodes
    across providers.  ``entity_id`` is an optional local read reference only;
    publishing it does not create a PredBat configuration projection.
    """

    source_id: str
    feed_type: str
    entity_id: Optional[str] = None

    def __post_init__(self):
        """Normalize the bounded deterministic source metadata."""
        source_id = _bounded_text(self.source_id, "source_id", 128)
        if _OPAQUE_ID_RE.fullmatch(source_id) is None:
            raise ValueError("source_id must be an opaque identifier")
        feed_type = _bounded_text(self.feed_type, "feed_type", 64).lower()
        if _FEED_TYPE_RE.fullmatch(feed_type) is None:
            raise ValueError("feed_type must use lowercase identifier characters")
        entity_id = self.entity_id
        if entity_id is not None:
            entity_id = _bounded_text(entity_id, "entity_id", 255).lower()
            if _ENTITY_ID_RE.fullmatch(entity_id) is None:
                raise ValueError("entity_id must be a Home Assistant entity identifier")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "feed_type", feed_type)
        object.__setattr__(self, "entity_id", entity_id)

    def node_id(self, provider_id):
        """Return one stable provider-qualified topology node identity."""
        return "reference-feed:{}:{}".format(provider_id, self.source_id)


def _normalize_sources(sources):
    """Freeze, sort, and collision-check a complete source snapshot."""
    try:
        sources = tuple(sources)
    except TypeError as exc:
        raise ValueError(
            "sources must be an iterable of ReferenceFeedSource",
        ) from exc
    if any(not isinstance(source, ReferenceFeedSource) for source in sources):
        raise ValueError("sources must contain only ReferenceFeedSource values")
    if not sources:
        raise ValueError("sources must contain at least one ReferenceFeedSource")

    seen = {}
    for source in sources:
        previous = seen.get(source.source_id)
        if previous is not None:
            raise FragmentAdapterConflict(
                "reference feed contains duplicate source {}".format(
                    source.source_id,
                )
            )
        seen[source.source_id] = source
    return tuple(
        sorted(
            sources,
            key=lambda item: (
                item.source_id,
                item.feed_type,
                item.entity_id or "",
            ),
        )
    )


def _source_node(source, provider_id):
    """Project one read reference without deriving any capability."""
    attributes = {
        "feedType": source.feed_type,
        "sourceScope": "provider-local",
    }
    if source.entity_id is not None:
        attributes["entityId"] = source.entity_id
    return {
        "id": source.node_id(provider_id),
        "kind": "data-source",
        "attributes": attributes,
        "accessPaths": [
            {
                "id": "reference-feed-read",
                "provider": provider_id,
                "preference": 0,
            }
        ],
        "capabilities": [],
    }


def _topology_document(
    provider_id,
    producer_name,
    source_generation,
    sources,
):
    """Build one deterministic provider-owned topology fragment."""
    return {
        "topologyVersion": "0.3.0",
        "scope": "fragment",
        "docVersion": source_generation,
        "producer": {
            "name": producer_name,
            "provider": provider_id,
            "authority": 0,
        },
        "nodes": [_source_node(source, provider_id) for source in sources],
    }


def _reference_aliases(provider_id, sources):
    """Publish only provider-qualified REFERENCE aliases."""
    return tuple(
        ProviderAlias(
            name="feed:{}:{}".format(
                source.feed_type,
                source.source_id,
            ),
            node_id=source.node_id(provider_id),
            roles=frozenset((AliasRole.REFERENCE,)),
        )
        for source in sources
    )


class ReferenceFeedFragmentPublisher:
    """Adapt explicit reference sources into one durable compiler fragment."""

    def __init__(
        self,
        provider_id,
        producer_name,
        state_store,
        enabled=False,
    ):
        """Create an unwired publisher; disabled is the safe default."""
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        self._enabled = enabled
        provider_id = canonical_provider_id(provider_id)
        self._producer_name = _bounded_text(
            producer_name,
            "producer_name",
            128,
        )
        self._adapter = DurableFragmentAdapter(provider_id, state_store)
        self.provider_id = self._adapter.provider_id
        self._lock = threading.RLock()
        try:
            self._adapter.read_state()
        except FragmentAdapterReadError as exc:
            expected = "provider {} has no durable fragment".format(
                self.provider_id,
            )
            if str(exc) != expected:
                raise
            self._seeded = False
        else:
            self._seeded = True

    @property
    def enabled(self):
        """Return whether this explicitly constructed publisher accepts input."""
        return self._enabled

    @property
    def generation(self):
        """Fresh-read the current durable adapter generation."""
        return self.read_state().generation

    @property
    def semantic_fingerprint(self):
        """Fresh-read the generation-bound semantic fingerprint."""
        return self.read_state().semantic_fingerprint

    @property
    def source_generation(self):
        """Fresh-read the current caller-owned source generation."""
        return _plain(
            self.read_state().snapshot.topology_fragment,
        )["docVersion"]

    def lattice_fragment_adapter(self):
        """Expose structural discovery only when enabled and seeded."""
        if not self._enabled or not self._seeded:
            return None
        self.read_state()
        return self

    def read_state(self):
        """Fresh-read the complete durable adapter state."""
        return self._adapter.read_state()

    def read_snapshot(self):
        """Fresh-read the current immutable provider snapshot."""
        return self._adapter.read_snapshot()

    def subscribe_invalidation(self, listener):
        """Subscribe to source, liveness, and removal changes."""
        return self._adapter.subscribe_invalidation(listener)

    def _current_state(self):
        """Return current state, treating an unseeded store as empty."""
        if not self._seeded:
            return None
        return self._adapter.read_state()

    def ingest_sources(
        self,
        source_generation,
        sources,
        health=HEALTH_UNCHANGED,
        feedback_token=None,
    ):
        """Publish one accepted complete reference-source snapshot."""
        if not self._enabled:
            return False
        source_generation = _source_generation(source_generation)
        sources = _normalize_sources(sources)
        document = _topology_document(
            self.provider_id,
            self._producer_name,
            source_generation,
            sources,
        )

        with self._lock:
            current = self._current_state()
            if current is not None and current.removed:
                raise FragmentAdapterRemoved(
                    "provider {} was removed at generation {}; reference "
                    "feed cannot re-enrol it".format(
                        self.provider_id,
                        current.generation,
                    )
                )
            if current is None:
                next_health = ProviderHealth.DEGRADED if health is HEALTH_UNCHANGED else _provider_health(health)
            else:
                previous = _plain(current.snapshot.topology_fragment)
                previous_generation = previous["docVersion"]
                if source_generation < previous_generation:
                    return False
                if source_generation == previous_generation and document != previous:
                    raise FragmentAdapterConflict(
                        "provider {} reused reference-feed source generation "
                        "{} for different content".format(
                            self.provider_id,
                            source_generation,
                        )
                    )
                next_health = current.snapshot.health if health is HEALTH_UNCHANGED else _provider_health(health)
                if source_generation == previous_generation and next_health is current.snapshot.health:
                    return False

            generation = 1 if current is None else current.generation + 1
            snapshot = ProviderSnapshot(
                provider_id=self.provider_id,
                generation=generation,
                health=next_health,
                topology_fragment=document,
                aliases=_reference_aliases(self.provider_id, sources),
                identity_aliases=(),
                role_assignments=(),
                config_projections=(),
            )
            published = self._adapter.publish(
                snapshot,
                "reference feed changed to source generation {}".format(
                    source_generation,
                ),
                feedback_token=feedback_token,
            )
            if published:
                self._seeded = True
            return published

    def set_liveness(self, health, feedback_token=None):
        """Publish provider liveness without changing source contents."""
        if not self._enabled:
            return False
        health = _provider_health(health)
        with self._lock:
            current = self._current_state()
            if current is None:
                raise FragmentAdapterReadError(
                    "reference feed must be seeded before liveness",
                )
            if current.removed:
                raise FragmentAdapterRemoved(
                    "provider {} was removed at generation {}".format(
                        self.provider_id,
                        current.generation,
                    )
                )
            if current.snapshot.health is health:
                return False
            previous = current.snapshot
            snapshot = ProviderSnapshot(
                provider_id=self.provider_id,
                generation=current.generation + 1,
                health=health,
                topology_fragment=_plain(previous.topology_fragment),
                aliases=previous.aliases,
                identity_aliases=(),
                role_assignments=(),
                config_projections=(),
            )
            return self._adapter.publish(
                snapshot,
                "reference feed liveness changed to {}".format(health.value),
                feedback_token=feedback_token,
            )

    def remove(self, feedback_token=None):
        """Publish an irreversible provider-removal tombstone."""
        if not self._enabled:
            return False
        with self._lock:
            current = self._current_state()
            if current is None:
                raise FragmentAdapterReadError(
                    "reference feed must be seeded before removal",
                )
            if current.removed:
                return False
            return self._adapter.remove(
                current.generation + 1,
                "reference feed integration removed",
                feedback_token=feedback_token,
            )
