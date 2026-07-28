# -----------------------------------------------------------------------------
# Predbat Home Battery System - Complete inventory Lattice fragment adapter
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Pure, default-off publisher for complete provider inventory snapshots.

The caller owns discovery and supplies a complete immutable site/device view
at a monotonically increasing discovery generation.  A newer accepted view
replaces the provider's previous view, so an omitted site or device is no
longer present in that provider-local fragment.

Inventory observations are REFERENCE-only.  Provider site/device identifiers
remain local aliases and never correlate nodes globally.  Only an explicitly
API-verified hardware serial can produce a strong ``ProviderIdentityAlias``.
No capability, configuration projection, role assignment, endpoint, or
control authority is inferred.

This module composes the existing durable fragment contracts.  It imports no
live integration and performs no discovery or registration by itself.
"""

# cspell:ignore autoconfig

import re
import threading
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import islice
from typing import Optional

from lattice_autoconfig import (
    AliasRole,
    ProviderAlias,
    ProviderHealth,
    ProviderIdentityAlias,
    ProviderSnapshot,
    _plain,
)
from lattice_fragment_adapters import (
    DurableFragmentAdapter,
    FragmentAdapterConflict,
    FragmentAdapterReadError,
    FragmentAdapterRemoved,
)


INVENTORY_HEALTH_UNCHANGED = object()
MAX_COMPLETE_INVENTORY_SITES = 1024
MAX_COMPLETE_INVENTORY_DEVICES = 16384
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_DEVICE_KINDS = frozenset(
    (
        "battery",
        "energy-management-system",
        "ev-charger",
        "gateway",
        "inverter",
        "meter",
        "sensor",
    )
)


def _bounded_text(value, name, maximum):
    """Return normalized non-empty text with an explicit size bound."""
    if type(value) is not str or not value.strip():
        raise ValueError("{} must be a non-empty string".format(name))
    if any(unicodedata.category(character) in ("Cc", "Cf", "Cs") for character in value):
        raise ValueError(
            "{} must not contain control characters or malformed Unicode".format(
                name,
            ),
        )
    value = value.strip()
    if len(value) > maximum:
        raise ValueError("{} must be at most {} characters".format(name, maximum))
    return value


def _provider_id(value):
    """Normalize one bounded canonical integration provider ID."""
    value = _bounded_text(value, "provider_id", 128)
    if _PROVIDER_ID_RE.fullmatch(value) is None:
        raise ValueError(
            "provider_id must use lowercase identifier characters",
        )
    return value


def _provider_identifier(value, name):
    """Normalize one bounded provider-local string or integer identifier."""
    if type(value) is int:
        value = str(value)
    value = _bounded_text(value, name, 128)
    if _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError("{} must be a provider identifier".format(name))
    return value


def _optional_text(value, name, maximum=128):
    """Return one normalized optional bounded text field."""
    if value is None:
        return None
    return _bounded_text(value, name, maximum)


def _optional_bool(value, name):
    """Accept only explicit boolean or unknown state."""
    if value is None or isinstance(value, bool):
        return value
    raise ValueError("{} must be True, False, or None".format(name))


def _discovery_generation(value):
    """Validate one caller-owned monotonic discovery generation."""
    if type(value) is not int or value < 0:
        raise ValueError(
            "discovery_generation must be a non-negative integer",
        )
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


def _bounded_observations(values, name, observation_type, maximum):
    """Collect at most max+1 typed observations without retaining source faults."""
    expected = observation_type.__name__
    if isinstance(values, (str, bytes, bytearray, Mapping)):
        raise ValueError(
            "{} must be an iterable of {}".format(name, expected),
        )
    invalid_iterable = False
    try:
        observations = tuple(
            islice(
                iter(values),
                maximum + 1,
            )
        )
    except Exception:
        invalid_iterable = True
    if invalid_iterable:
        raise ValueError(
            "{} must be an iterable of {}".format(name, expected),
        )
    if len(observations) > maximum:
        raise ValueError(
            "{} must contain at most {} observations".format(
                name,
                maximum,
            ),
        )
    if any(type(observation) is not observation_type for observation in observations):
        raise ValueError(
            "{} must contain only {} values".format(
                name,
                expected,
            ),
        )
    return observations


@dataclass(frozen=True)
class InventorySiteObservation:
    """One provider-local site/station observation."""

    site_id: object
    online: Optional[bool] = None

    def __post_init__(self):
        """Normalize bounded site state without creating strong identity."""
        object.__setattr__(
            self,
            "site_id",
            _provider_identifier(self.site_id, "site_id"),
        )
        object.__setattr__(
            self,
            "online",
            _optional_bool(self.online, "online"),
        )

    def node_id(self, provider_id):
        """Return one stable provider-qualified site node."""
        return "inventory:{}:site:{}".format(provider_id, self.site_id)


@dataclass(frozen=True)
class InventoryDeviceObservation:
    """One provider-local device with optional verified hardware identity."""

    device_id: object
    site_id: object
    kind: str
    model: Optional[str] = None
    online: Optional[bool] = None
    verified_hardware_serial: Optional[str] = None

    def __post_init__(self):
        """Normalize metadata and retain only an explicitly verified serial."""
        device_id = _provider_identifier(self.device_id, "device_id")
        site_id = _provider_identifier(self.site_id, "site_id")
        kind = _bounded_text(self.kind, "kind", 64).lower().replace("_", "-")
        if kind not in _DEVICE_KINDS:
            raise ValueError(
                "kind must be one of {}".format(
                    ", ".join(sorted(_DEVICE_KINDS)),
                )
            )
        serial = self.verified_hardware_serial
        if serial is not None:
            serial = _provider_identifier(
                serial,
                "verified_hardware_serial",
            ).upper()
        object.__setattr__(self, "device_id", device_id)
        object.__setattr__(self, "site_id", site_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self,
            "model",
            _optional_text(self.model, "model"),
        )
        object.__setattr__(
            self,
            "online",
            _optional_bool(self.online, "online"),
        )
        object.__setattr__(
            self,
            "verified_hardware_serial",
            serial,
        )

    def node_id(self, provider_id):
        """Return one stable provider-qualified device node."""
        return "inventory:{}:device:{}".format(
            provider_id,
            self.device_id,
        )


def _normalize_sites(sites):
    """Freeze, sort, and collision-check a complete site set."""
    sites = _bounded_observations(
        sites,
        "sites",
        InventorySiteObservation,
        MAX_COMPLETE_INVENTORY_SITES,
    )
    canonical_sites = []
    for site in sites:
        raw_site_id = site.site_id
        raw_online = site.online
        clean = InventorySiteObservation(
            site_id=raw_site_id,
            online=raw_online,
        )
        if (raw_site_id, raw_online) != (
            clean.site_id,
            clean.online,
        ):
            raise ValueError(
                "complete inventory site observation is not canonical",
            )
        canonical_sites.append(clean)
    sites = tuple(canonical_sites)
    seen = set()
    for site in sites:
        if site.site_id in seen:
            raise FragmentAdapterConflict(
                "complete inventory contains a duplicate site_id",
            )
        seen.add(site.site_id)
    return tuple(sorted(sites, key=lambda item: item.site_id))


def _normalize_devices(devices, site_ids):
    """Freeze, sort, and reject ambiguous device or serial ownership."""
    devices = _bounded_observations(
        devices,
        "devices",
        InventoryDeviceObservation,
        MAX_COMPLETE_INVENTORY_DEVICES,
    )
    canonical_devices = []
    for device in devices:
        raw_device_id = device.device_id
        raw_site_id = device.site_id
        raw_kind = device.kind
        raw_model = device.model
        raw_online = device.online
        raw_serial = device.verified_hardware_serial
        clean = InventoryDeviceObservation(
            device_id=raw_device_id,
            site_id=raw_site_id,
            kind=raw_kind,
            model=raw_model,
            online=raw_online,
            verified_hardware_serial=raw_serial,
        )
        if (
            raw_device_id,
            raw_site_id,
            raw_kind,
            raw_model,
            raw_online,
            raw_serial,
        ) != (
            clean.device_id,
            clean.site_id,
            clean.kind,
            clean.model,
            clean.online,
            clean.verified_hardware_serial,
        ):
            raise ValueError(
                "complete inventory device observation is not canonical",
            )
        canonical_devices.append(clean)
    devices = tuple(canonical_devices)

    device_owners = {}
    serial_owners = {}
    for device in devices:
        if device.site_id not in site_ids:
            raise ValueError(
                "complete inventory device references an unknown site_id",
            )
        owner = device_owners.get(device.device_id)
        if owner is not None:
            raise FragmentAdapterConflict(
                "complete inventory contains a duplicate device_id",
            )
        device_owners[device.device_id] = device.site_id
        serial = device.verified_hardware_serial
        if serial is not None:
            owner = serial_owners.get(serial)
            if owner is not None and owner != device.device_id:
                raise FragmentAdapterConflict(
                    "complete inventory contains duplicate " "verified_hardware_serial ownership",
                )
            serial_owners[serial] = device.device_id
    return tuple(
        sorted(
            devices,
            key=lambda item: (
                item.site_id,
                item.device_id,
                item.kind,
            ),
        )
    )


def _site_node(site, provider_id):
    """Project one provider-local site without authority."""
    attributes = {
        "inventorySiteId": site.site_id,
        "identityScope": "provider-local",
    }
    if site.online is not None:
        attributes["online"] = site.online
    return {
        "id": site.node_id(provider_id),
        "kind": "site",
        "attributes": attributes,
        "accessPaths": [
            {
                "id": "inventory-read",
                "provider": provider_id,
                "preference": 0,
            }
        ],
        "capabilities": [],
    }


def _device_node(device, provider_id):
    """Project bounded device observations without deriving capabilities."""
    attributes = {
        "inventoryDeviceId": device.device_id,
        "inventorySiteId": device.site_id,
        "identityScope": "provider-local",
    }
    if device.model is not None:
        attributes["model"] = device.model
    if device.online is not None:
        attributes["online"] = device.online
    if device.verified_hardware_serial is not None:
        attributes["serial"] = device.verified_hardware_serial
        attributes["serialEvidence"] = "api-verified"
    return {
        "id": device.node_id(provider_id),
        "kind": device.kind,
        "attributes": attributes,
        "accessPaths": [
            {
                "id": "inventory-read",
                "provider": provider_id,
                "preference": 0,
            }
        ],
        "capabilities": [],
    }


def _topology_document(
    provider_id,
    producer_name,
    discovery_generation,
    sites,
    devices,
):
    """Build one deterministic complete provider inventory fragment."""
    nodes = [_site_node(site, provider_id) for site in sites]
    nodes.extend(_device_node(device, provider_id) for device in devices)
    relationships = [
        {
            "from": "inventory:{}:site:{}".format(
                provider_id,
                device.site_id,
            ),
            "to": device.node_id(provider_id),
            "type": "contains",
        }
        for device in devices
    ]
    document = {
        "topologyVersion": "0.3.0",
        "scope": "fragment",
        "docVersion": discovery_generation,
        "producer": {
            "name": producer_name,
            "provider": provider_id,
            "authority": 0,
        },
        "nodes": nodes,
    }
    if relationships:
        document["relationships"] = relationships
    return document


def _reference_aliases(provider_id, sites, devices):
    """Publish provider-local names with REFERENCE role only."""
    aliases = [
        ProviderAlias(
            name="site:{}".format(site.site_id),
            node_id=site.node_id(provider_id),
            roles=frozenset((AliasRole.REFERENCE,)),
        )
        for site in sites
    ]
    aliases.extend(
        ProviderAlias(
            name="device:{}".format(device.device_id),
            node_id=device.node_id(provider_id),
            roles=frozenset((AliasRole.REFERENCE,)),
        )
        for device in devices
    )
    return tuple(
        sorted(
            aliases,
            key=lambda item: (item.name, item.node_id),
        )
    )


def _identity_aliases(provider_id, devices):
    """Publish only explicitly API-verified hardware serial assertions."""
    return tuple(
        sorted(
            (
                ProviderIdentityAlias(
                    kind="serial",
                    value=device.verified_hardware_serial,
                    node_id=device.node_id(provider_id),
                )
                for device in devices
                if device.verified_hardware_serial is not None
            ),
            key=lambda item: (item.kind, item.value, item.node_id),
        )
    )


class CompleteInventoryFragmentPublisher:
    """Publish complete immutable site/device inventory generations."""

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
        provider_id = _provider_id(provider_id)
        self._producer_name = _bounded_text(
            producer_name,
            "producer_name",
            128,
        )
        self._enabled = enabled
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
    def discovery_generation(self):
        """Fresh-read the current caller-owned discovery generation."""
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
        """Subscribe to inventory, liveness, and removal changes."""
        return self._adapter.subscribe_invalidation(listener)

    def _current_state(self):
        """Return current state, treating an unseeded store as empty."""
        if not self._seeded:
            return None
        return self._adapter.read_state()

    def ingest_inventory(
        self,
        discovery_generation,
        sites,
        devices,
        health=INVENTORY_HEALTH_UNCHANGED,
        feedback_token=None,
    ):
        """Publish one complete site/device inventory replacement."""
        if not self._enabled:
            return False
        discovery_generation = _discovery_generation(
            discovery_generation,
        )
        sites = _normalize_sites(sites)
        site_ids = frozenset(site.site_id for site in sites)
        devices = _normalize_devices(devices, site_ids)
        document = _topology_document(
            self.provider_id,
            self._producer_name,
            discovery_generation,
            sites,
            devices,
        )

        with self._lock:
            current = self._current_state()
            if current is not None and current.removed:
                raise FragmentAdapterRemoved(
                    "provider {} was removed at generation {}; complete "
                    "inventory cannot re-enrol it".format(
                        self.provider_id,
                        current.generation,
                    )
                )
            if current is None:
                next_health = ProviderHealth.DEGRADED if health is INVENTORY_HEALTH_UNCHANGED else _provider_health(health)
            else:
                previous = _plain(current.snapshot.topology_fragment)
                previous_generation = previous["docVersion"]
                if discovery_generation < previous_generation:
                    return False
                if discovery_generation == previous_generation and document != previous:
                    raise FragmentAdapterConflict(
                        "provider {} reused complete-inventory discovery "
                        "generation {} for different content".format(
                            self.provider_id,
                            discovery_generation,
                        )
                    )
                next_health = current.snapshot.health if health is INVENTORY_HEALTH_UNCHANGED else _provider_health(health)
                if discovery_generation == previous_generation and next_health is current.snapshot.health:
                    return False

            generation = 1 if current is None else current.generation + 1
            snapshot = ProviderSnapshot(
                provider_id=self.provider_id,
                generation=generation,
                health=next_health,
                topology_fragment=document,
                aliases=_reference_aliases(
                    self.provider_id,
                    sites,
                    devices,
                ),
                identity_aliases=_identity_aliases(
                    self.provider_id,
                    devices,
                ),
                role_assignments=(),
                config_projections=(),
            )
            published = self._adapter.publish(
                snapshot,
                "complete inventory changed to discovery generation " "{}".format(discovery_generation),
                feedback_token=feedback_token,
            )
            if published:
                self._seeded = True
            return published

    def set_liveness(self, health, feedback_token=None):
        """Publish provider liveness without changing inventory contents."""
        if not self._enabled:
            return False
        health = _provider_health(health)
        with self._lock:
            current = self._current_state()
            if current is None:
                raise FragmentAdapterReadError(
                    "complete inventory must be seeded before liveness",
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
                identity_aliases=previous.identity_aliases,
                role_assignments=(),
                config_projections=(),
            )
            return self._adapter.publish(
                snapshot,
                "complete inventory liveness changed to {}".format(
                    health.value,
                ),
                feedback_token=feedback_token,
            )

    def remove(self, feedback_token=None):
        """Publish an irreversible whole-provider removal tombstone."""
        if not self._enabled:
            return False
        with self._lock:
            current = self._current_state()
            if current is None:
                raise FragmentAdapterReadError(
                    "complete inventory must be seeded before removal",
                )
            if current.removed:
                return False
            return self._adapter.remove(
                current.generation + 1,
                "complete inventory integration removed",
                feedback_token=feedback_token,
            )
