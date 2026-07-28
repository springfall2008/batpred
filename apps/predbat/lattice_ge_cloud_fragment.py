# -----------------------------------------------------------------------------
# Predbat Home Battery System - GE Cloud Lattice fragment adapter
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Pure, default-off GivEnergy Cloud fragment publisher.

The live ``GECloudDirect`` component owns API discovery and polling.  This
module deliberately does not import, register, or mutate that component.  A
future gated seam may pass explicit discovery/device snapshots here after a
successful cloud read.

Every accepted discovery version, device change, liveness change, or removal
publishes a new durable generation through the common fragment-adapter
surface.  Published aliases are REFERENCE-only: this tranche exposes identity
and provider-local topology for composition, but grants no materialization-ready
PRIMARY/CONTROL roles and publishes no PredBat configuration or control write.
"""

# cspell:ignore autoconfig

import threading
from dataclasses import dataclass
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


_HEALTH_UNCHANGED = object()
_DEVICE_KINDS = frozenset(
    (
        "battery-inverter",
        "ems",
        "ev-charger",
        "gateway",
        "inverter",
        "pv-inverter",
    )
)
_TOPOLOGY_KINDS = {
    "battery-inverter": "inverter",
    "ems": "energy-management-system",
    "ev-charger": "ev-charger",
    "gateway": "gateway",
    "inverter": "inverter",
    "pv-inverter": "inverter",
}


def _required_text(value, name):
    """Return one normalized non-empty text field."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{} must be a non-empty string".format(name))
    return value.strip()


def _optional_text(value, name):
    """Return one normalized optional text field."""
    if value is None:
        return None
    return _required_text(value, name)


def _optional_identifier(value, name):
    """Normalize an optional cloud identifier without accepting booleans."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return _required_text(value, name)


def _discovery_version(value):
    """Validate one provider-owned monotonic discovery version."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("discovery_version must be a non-negative integer")
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
    raise ValueError(
        "health must be ProviderHealth, True, False, or None",
    )


@dataclass(frozen=True)
class GECloudDeviceSnapshot:
    """One explicit provider-local device discovered by GivEnergy Cloud."""

    serial: str
    kind: str = "inverter"
    online: Optional[bool] = None
    model: Optional[str] = None
    site_id: Optional[object] = None
    uuid: Optional[str] = None

    def __post_init__(self):
        """Normalize stable identities and reject ambiguous device state."""
        serial = _required_text(self.serial, "serial").upper()
        kind = _required_text(self.kind, "kind").lower().replace("_", "-")
        if kind not in _DEVICE_KINDS:
            raise ValueError(
                "kind must be one of {}".format(
                    ", ".join(sorted(_DEVICE_KINDS)),
                )
            )
        if self.online is not None and not isinstance(self.online, bool):
            raise ValueError("online must be True, False, or None")
        object.__setattr__(self, "serial", serial)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "model", _optional_text(self.model, "model"))
        object.__setattr__(
            self,
            "site_id",
            _optional_identifier(self.site_id, "site_id"),
        )
        object.__setattr__(self, "uuid", _optional_text(self.uuid, "uuid"))

    @property
    def node_id(self):
        """Return the deterministic provider-local node identity."""
        return "ge-cloud:{}".format(self.serial)


def _normalize_devices(devices):
    """Freeze, sort, and collision-check explicit device snapshots."""
    try:
        devices = tuple(devices)
    except TypeError as exc:
        raise ValueError("devices must be an iterable of GECloudDeviceSnapshot") from exc
    if any(not isinstance(device, GECloudDeviceSnapshot) for device in devices):
        raise ValueError("devices must contain only GECloudDeviceSnapshot values")
    if not devices:
        raise ValueError("devices must contain at least one GECloudDeviceSnapshot")

    by_serial = {}
    uuid_owners = {}
    for device in devices:
        if device.serial in by_serial:
            raise FragmentAdapterConflict(
                "GE Cloud discovery contains duplicate serial {}".format(
                    device.serial,
                )
            )
        by_serial[device.serial] = device
        if device.uuid is not None:
            owner = uuid_owners.get(device.uuid)
            if owner is not None and owner != device.serial:
                raise FragmentAdapterConflict(
                    "GE Cloud UUID {} belongs to both {} and {}".format(
                        device.uuid,
                        owner,
                        device.serial,
                    )
                )
            uuid_owners[device.uuid] = device.serial
    return tuple(sorted(devices, key=lambda item: (item.serial, item.kind)))


def _device_node(device, provider_id):
    """Project one device without claiming read or control capabilities."""
    attributes = {
        "serial": device.serial,
        "geCloudKind": device.kind,
    }
    if device.online is not None:
        attributes["online"] = device.online
    if device.model is not None:
        attributes["model"] = device.model
    if device.site_id is not None:
        attributes["siteId"] = device.site_id
    if device.uuid is not None:
        attributes["geCloudUuid"] = device.uuid
    return {
        "id": device.node_id,
        "kind": _TOPOLOGY_KINDS[device.kind],
        "attributes": attributes,
        "accessPaths": [
            {
                "id": "ge-cloud-api",
                "provider": provider_id,
                "preference": 0,
            }
        ],
        "capabilities": [],
    }


def _topology_document(provider_id, discovery_version, devices):
    """Build one deterministic provider-owned v0.3 topology fragment."""
    return {
        "topologyVersion": "0.3.0",
        "scope": "fragment",
        "docVersion": discovery_version,
        "producer": {
            "name": "GivEnergy Cloud",
            "provider": provider_id,
            "authority": 0,
        },
        "nodes": [_device_node(device, provider_id) for device in devices],
    }


def _reference_aliases(devices):
    """Publish provider-local names that never grant materialization roles."""
    return tuple(
        ProviderAlias(
            name="serial:{}".format(device.serial),
            node_id=device.node_id,
            roles=frozenset((AliasRole.REFERENCE,)),
        )
        for device in devices
    )


def _identity_aliases(devices):
    """Publish strong serial and optional provider UUID identities.

    The deterministic node id remains provider-local metadata.  It is not
    asserted as a cross-provider ``lattice-node-id`` identity because another
    integration may legitimately name the same serial differently.
    """
    aliases = []
    for device in devices:
        aliases.append(
            ProviderIdentityAlias(
                kind="serial",
                value=device.serial,
                node_id=device.node_id,
            )
        )
        if device.uuid is not None:
            aliases.append(
                ProviderIdentityAlias(
                    kind="ge-cloud-uuid",
                    value=device.uuid,
                    node_id=device.node_id,
                )
            )
    return tuple(
        sorted(
            aliases,
            key=lambda item: (item.kind, item.value, item.node_id),
        )
    )


class GECloudFragmentPublisher:
    """Adapt explicit cloud snapshots into one durable compiler fragment."""

    def __init__(self, provider_id, state_store, enabled=False):
        """Create an unwired publisher; disabled is the safe default."""
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        self._enabled = enabled
        self._adapter = DurableFragmentAdapter(provider_id, state_store)
        self.provider_id = self._adapter.provider_id
        self._lock = threading.RLock()
        try:
            self._adapter.read_state()
        except FragmentAdapterReadError as exc:
            expected = "provider {} has no durable fragment".format(self.provider_id)
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
    def discovery_version(self):
        """Fresh-read the current provider-owned discovery version."""
        state = self.read_state()
        return _plain(state.snapshot.topology_fragment)["docVersion"]

    def lattice_fragment_adapter(self):
        """Expose structural discovery only when enabled and durably seeded."""
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
        """Subscribe to discovery, device, liveness, and removal changes."""
        return self._adapter.subscribe_invalidation(listener)

    def _current_state(self):
        """Return the current state, treating an unseeded store as empty."""
        if not self._seeded:
            return None
        return self._adapter.read_state()

    def ingest_discovery(
        self,
        discovery_version,
        devices,
        health=_HEALTH_UNCHANGED,
    ):
        """Publish one accepted complete discovery/device snapshot."""
        if not self._enabled:
            return False
        discovery_version = _discovery_version(discovery_version)
        devices = _normalize_devices(devices)
        document = _topology_document(
            self.provider_id,
            discovery_version,
            devices,
        )

        with self._lock:
            current = self._current_state()
            if current is not None and current.removed:
                raise FragmentAdapterRemoved(
                    "provider {} was removed at generation {}; GE Cloud "
                    "discovery cannot re-enrol it".format(
                        self.provider_id,
                        current.generation,
                    )
                )
            if current is None:
                next_health = ProviderHealth.DEGRADED if health is _HEALTH_UNCHANGED else _provider_health(health)
            else:
                previous = _plain(current.snapshot.topology_fragment)
                previous_version = previous["docVersion"]
                if discovery_version < previous_version:
                    return False
                if discovery_version == previous_version and document != previous:
                    raise FragmentAdapterConflict(
                        "provider {} reused GE Cloud discovery version {} "
                        "for different content".format(
                            self.provider_id,
                            discovery_version,
                        )
                    )
                next_health = current.snapshot.health if health is _HEALTH_UNCHANGED else _provider_health(health)
                if discovery_version == previous_version and next_health is current.snapshot.health:
                    return False

            generation = 1 if current is None else current.generation + 1
            snapshot = ProviderSnapshot(
                provider_id=self.provider_id,
                generation=generation,
                health=next_health,
                topology_fragment=document,
                aliases=_reference_aliases(devices),
                identity_aliases=_identity_aliases(devices),
                role_assignments=(),
                config_projections=(),
            )
            published = self._adapter.publish(
                snapshot,
                "GE Cloud discovery changed to version {}".format(
                    discovery_version,
                ),
            )
            if published:
                self._seeded = True
            return published

    def set_liveness(self, health):
        """Publish provider liveness without changing the discovery document."""
        if not self._enabled:
            return False
        health = _provider_health(health)
        with self._lock:
            current = self._current_state()
            if current is None:
                raise FragmentAdapterReadError(
                    "GE Cloud discovery must be seeded before liveness",
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
                "GE Cloud liveness changed to {}".format(health.value),
            )

    def remove(self):
        """Publish an irreversible provider-removal tombstone."""
        if not self._enabled:
            return False
        with self._lock:
            current = self._current_state()
            if current is None:
                raise FragmentAdapterReadError(
                    "GE Cloud discovery must be seeded before removal",
                )
            if current.removed:
                return False
            return self._adapter.remove(
                current.generation + 1,
                "GE Cloud integration removed",
            )
