# -----------------------------------------------------------------------------
# Predbat Home Battery System - Fox Cloud Lattice fragment adapter
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Pure, default-off Fox Cloud fragment publisher.

The live ``FoxAPI`` component owns OAuth/API-key authentication, discovery,
polling, scheduler state, automatic configuration, and control writes.  This
module deliberately does not import, register, or mutate that high-impact
component.  A later gated seam may pass explicit immutable discovery snapshots
here after a successful cloud read.

Accepted discovery, liveness, and removal changes advance the common durable
fragment generation and invalidate the fresh-read compiler.  This tranche is
REFERENCE-only: it publishes topology and identity, but no roles,
configuration projections, capabilities, or write authority.
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


def _optional_bool(value, name):
    """Accept only explicit boolean or unknown state."""
    if value is None or isinstance(value, bool):
        return value
    raise ValueError("{} must be True, False, or None".format(name))


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
    raise ValueError("health must be ProviderHealth, True, False, or None")


@dataclass(frozen=True)
class FoxStationSnapshot:
    """One explicit provider-local Fox station."""

    station_id: str
    name: Optional[str] = None

    def __post_init__(self):
        """Normalize the provider-owned station identity."""
        object.__setattr__(
            self,
            "station_id",
            _required_text(self.station_id, "station_id"),
        )
        object.__setattr__(
            self,
            "name",
            _optional_text(self.name, "name"),
        )

    @property
    def node_id(self):
        """Return the deterministic provider-local station node."""
        return "fox:station:{}".format(self.station_id)


@dataclass(frozen=True)
class FoxDeviceSnapshot:
    """One explicit provider-local Fox inverter/device."""

    serial: str
    station_id: str
    device_type: Optional[str] = None
    online: Optional[bool] = None
    has_battery: Optional[bool] = None
    has_pv: Optional[bool] = None
    third_party_generation: Optional[bool] = None
    scheduler_supported: Optional[bool] = None

    def __post_init__(self):
        """Normalize strong hardware identity and explicit feature evidence."""
        object.__setattr__(
            self,
            "serial",
            _required_text(self.serial, "serial").upper(),
        )
        object.__setattr__(
            self,
            "station_id",
            _required_text(self.station_id, "station_id"),
        )
        object.__setattr__(
            self,
            "device_type",
            _optional_text(self.device_type, "device_type"),
        )
        object.__setattr__(
            self,
            "online",
            _optional_bool(self.online, "online"),
        )
        object.__setattr__(
            self,
            "has_battery",
            _optional_bool(self.has_battery, "has_battery"),
        )
        object.__setattr__(
            self,
            "has_pv",
            _optional_bool(self.has_pv, "has_pv"),
        )
        object.__setattr__(
            self,
            "third_party_generation",
            _optional_bool(
                self.third_party_generation,
                "third_party_generation",
            ),
        )
        object.__setattr__(
            self,
            "scheduler_supported",
            _optional_bool(
                self.scheduler_supported,
                "scheduler_supported",
            ),
        )

    @property
    def node_id(self):
        """Return the deterministic provider-local hardware node."""
        return "fox:device:{}".format(self.serial)


def _normalize_stations(stations):
    """Freeze, sort, and collision-check station snapshots."""
    try:
        stations = tuple(stations)
    except TypeError as exc:
        raise ValueError(
            "stations must be an iterable of FoxStationSnapshot",
        ) from exc
    if any(not isinstance(item, FoxStationSnapshot) for item in stations):
        raise ValueError("stations must contain only FoxStationSnapshot values")
    if not stations:
        raise ValueError("stations must contain at least one FoxStationSnapshot")

    seen = set()
    for station in stations:
        if station.station_id in seen:
            raise FragmentAdapterConflict(
                "Fox discovery contains duplicate station {}".format(
                    station.station_id,
                )
            )
        seen.add(station.station_id)
    return tuple(sorted(stations, key=lambda item: item.station_id))


def _normalize_devices(devices, station_ids):
    """Freeze, sort, and reject ambiguous hardware ownership."""
    try:
        devices = tuple(devices)
    except TypeError as exc:
        raise ValueError(
            "devices must be an iterable of FoxDeviceSnapshot",
        ) from exc
    if any(not isinstance(item, FoxDeviceSnapshot) for item in devices):
        raise ValueError("devices must contain only FoxDeviceSnapshot values")
    if not devices:
        raise ValueError("devices must contain at least one FoxDeviceSnapshot")

    serial_owners = {}
    for device in devices:
        if device.station_id not in station_ids:
            raise ValueError(
                "device {} references unknown station {}".format(
                    device.serial,
                    device.station_id,
                )
            )
        owner = serial_owners.get(device.serial)
        if owner is not None:
            raise FragmentAdapterConflict(
                "Fox serial {} belongs to both stations {} and {}".format(
                    device.serial,
                    owner,
                    device.station_id,
                )
            )
        serial_owners[device.serial] = device.station_id
    return tuple(
        sorted(
            devices,
            key=lambda item: (item.station_id, item.serial),
        )
    )


def _station_node(station, provider_id):
    """Project one provider-local site without claiming authority."""
    attributes = {"stationId": station.station_id}
    if station.name is not None:
        attributes["stationName"] = station.name
    return {
        "id": station.node_id,
        "kind": "site",
        "attributes": attributes,
        "accessPaths": [
            {
                "id": "fox-cloud-api",
                "provider": provider_id,
                "preference": 0,
            }
        ],
        "capabilities": [],
    }


def _device_node(device, provider_id):
    """Project explicit Fox observations without deriving capabilities."""
    attributes = {
        "deviceSn": device.serial,
        "stationId": device.station_id,
    }
    optional = (
        ("deviceType", device.device_type),
        ("online", device.online),
        ("hasBattery", device.has_battery),
        ("hasPv", device.has_pv),
        ("thirdPartyGeneration", device.third_party_generation),
        ("schedulerSupported", device.scheduler_supported),
    )
    for key, value in optional:
        if value is not None:
            attributes[key] = value
    return {
        "id": device.node_id,
        "kind": "inverter",
        "attributes": attributes,
        "accessPaths": [
            {
                "id": "fox-cloud-api",
                "provider": provider_id,
                "preference": 0,
            }
        ],
        "capabilities": [],
    }


def _topology_document(provider_id, discovery_version, stations, devices):
    """Build one deterministic provider-owned v0.3 fragment."""
    nodes = [_station_node(station, provider_id) for station in stations]
    nodes.extend(_device_node(device, provider_id) for device in devices)
    relationships = [
        {
            "from": "fox:station:{}".format(device.station_id),
            "to": device.node_id,
            "type": "contains",
        }
        for device in devices
    ]
    return {
        "topologyVersion": "0.3.0",
        "scope": "fragment",
        "docVersion": discovery_version,
        "producer": {
            "name": "Fox Cloud",
            "provider": provider_id,
            "authority": 0,
        },
        "nodes": nodes,
        "relationships": relationships,
    }


def _reference_aliases(stations, devices):
    """Publish provider-local names with REFERENCE role only."""
    aliases = [
        ProviderAlias(
            name="station:{}".format(station.station_id),
            node_id=station.node_id,
            roles=frozenset((AliasRole.REFERENCE,)),
        )
        for station in stations
    ]
    aliases.extend(
        ProviderAlias(
            name="serial:{}".format(device.serial),
            node_id=device.node_id,
            roles=frozenset((AliasRole.REFERENCE,)),
        )
        for device in devices
    )
    return tuple(sorted(aliases, key=lambda item: (item.name, item.node_id)))


def _identity_aliases(stations, devices):
    """Publish provider station IDs and globally useful hardware serials."""
    aliases = [
        ProviderIdentityAlias(
            kind="fox-station-id",
            value=station.station_id,
            node_id=station.node_id,
        )
        for station in stations
    ]
    aliases.extend(
        ProviderIdentityAlias(
            kind="serial",
            value=device.serial,
            node_id=device.node_id,
        )
        for device in devices
    )
    return tuple(
        sorted(
            aliases,
            key=lambda item: (item.kind, item.value, item.node_id),
        )
    )


class FoxCloudFragmentPublisher:
    """Adapt explicit Fox snapshots into one durable compiler fragment."""

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
    def discovery_version(self):
        """Fresh-read the provider-owned discovery version."""
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
        """Subscribe to discovery, liveness, and removal changes."""
        return self._adapter.subscribe_invalidation(listener)

    def _current_state(self):
        """Return current state, treating an unseeded store as empty."""
        if not self._seeded:
            return None
        return self._adapter.read_state()

    def ingest_discovery(
        self,
        discovery_version,
        stations,
        devices,
        health=_HEALTH_UNCHANGED,
    ):
        """Publish one accepted complete station/device snapshot."""
        if not self._enabled:
            return False
        discovery_version = _discovery_version(discovery_version)
        stations = _normalize_stations(stations)
        devices = _normalize_devices(
            devices,
            frozenset(item.station_id for item in stations),
        )
        document = _topology_document(
            self.provider_id,
            discovery_version,
            stations,
            devices,
        )

        with self._lock:
            current = self._current_state()
            if current is not None and current.removed:
                raise FragmentAdapterRemoved(
                    "provider {} was removed at generation {}; Fox "
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
                        "provider {} reused Fox discovery version {} "
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
                aliases=_reference_aliases(stations, devices),
                identity_aliases=_identity_aliases(stations, devices),
                role_assignments=(),
                config_projections=(),
            )
            published = self._adapter.publish(
                snapshot,
                "Fox discovery changed to version {}".format(
                    discovery_version,
                ),
            )
            if published:
                self._seeded = True
            return published

    def set_liveness(self, health):
        """Publish liveness without changing discovery contents."""
        if not self._enabled:
            return False
        health = _provider_health(health)
        with self._lock:
            current = self._current_state()
            if current is None:
                raise FragmentAdapterReadError(
                    "Fox discovery must be seeded before liveness",
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
                "Fox liveness changed to {}".format(health.value),
            )

    def remove(self):
        """Publish an irreversible provider-removal tombstone."""
        if not self._enabled:
            return False
        with self._lock:
            current = self._current_state()
            if current is None:
                raise FragmentAdapterReadError(
                    "Fox discovery must be seeded before removal",
                )
            if current.removed:
                return False
            return self._adapter.remove(
                current.generation + 1,
                "Fox integration removed",
            )
