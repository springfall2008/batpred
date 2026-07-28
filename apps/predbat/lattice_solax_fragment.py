# -----------------------------------------------------------------------------
# Predbat Home Battery System - SolaX Cloud Lattice fragment adapter
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Pure, default-off SolaX Cloud fragment publisher.

The live ``SolaxAPI`` component owns authentication, plant/device discovery,
telemetry polling, automatic configuration, and controls.  This module does
not import, register, or mutate that component.  A future gated seam may pass
explicit immutable discovery snapshots here after successful cloud reads.

Every accepted discovery version, plant/device change, liveness change, or
removal publishes a new durable generation through the common fragment
adapter.  Published aliases remain REFERENCE-only: there are no materialization
roles, configuration projections, capabilities, or control writes.
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
        "battery",
        "ev-charger",
        "inverter",
        "meter",
    )
)
_TOPOLOGY_KINDS = {
    "battery": "battery",
    "ev-charger": "ev-charger",
    "inverter": "inverter",
    "meter": "meter",
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


def _optional_model(value):
    """Normalize the cloud model code/name without accepting booleans."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return _required_text(value, "model")


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


def _online_status(value):
    """Normalize SolaX boolean/zero/one online status without guessing others."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return value == 1
    raise ValueError("online must be True, False, 1, 0, or None")


@dataclass(frozen=True)
class SolaxPlantSnapshot:
    """One explicit provider-local SolaX plant."""

    plant_id: str
    name: Optional[str] = None

    def __post_init__(self):
        """Normalize the cloud-owned stable plant identity."""
        object.__setattr__(
            self,
            "plant_id",
            _required_text(self.plant_id, "plant_id"),
        )
        object.__setattr__(self, "name", _optional_text(self.name, "name"))

    @property
    def node_id(self):
        """Return the deterministic provider-local plant node identity."""
        return "solax:plant:{}".format(self.plant_id)


@dataclass(frozen=True)
class SolaxDeviceSnapshot:
    """One explicit provider-local SolaX device."""

    serial: str
    plant_id: str
    kind: str
    online: Optional[object] = None
    model: Optional[object] = None
    synthetic: bool = False
    source_serial: Optional[str] = None

    def __post_init__(self):
        """Normalize identities and quarantine synthetic battery placeholders."""
        serial = _required_text(self.serial, "serial").upper()
        plant_id = _required_text(self.plant_id, "plant_id")
        kind = _required_text(self.kind, "kind").lower().replace("_", "-")
        if kind not in _DEVICE_KINDS:
            raise ValueError(
                "kind must be one of {}".format(
                    ", ".join(sorted(_DEVICE_KINDS)),
                )
            )
        if not isinstance(self.synthetic, bool):
            raise ValueError("synthetic must be a boolean")
        source_serial = _optional_text(self.source_serial, "source_serial")
        if source_serial is not None:
            source_serial = source_serial.upper()
        if self.synthetic:
            if kind != "battery":
                raise ValueError("only a battery snapshot may be synthetic")
            if source_serial is None:
                raise ValueError(
                    "a synthetic battery requires source_serial",
                )
            expected = "{}_BATTERY".format(source_serial)
            if serial != expected:
                raise ValueError(
                    "synthetic battery serial must be {}".format(expected),
                )
        elif source_serial is not None:
            raise ValueError(
                "source_serial is only valid for a synthetic battery",
            )

        object.__setattr__(self, "serial", serial)
        object.__setattr__(self, "plant_id", plant_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "online", _online_status(self.online))
        object.__setattr__(self, "model", _optional_model(self.model))
        object.__setattr__(self, "source_serial", source_serial)

    @property
    def node_id(self):
        """Return the deterministic provider-local device node identity."""
        if self.synthetic:
            return "solax:synthetic:{}:battery:{}".format(
                self.plant_id,
                self.source_serial,
            )
        return "solax:device:{}".format(self.serial)


def _normalize_plants(plants):
    """Freeze, sort, and collision-check plant snapshots."""
    try:
        plants = tuple(plants)
    except TypeError as exc:
        raise ValueError(
            "plants must be an iterable of SolaxPlantSnapshot",
        ) from exc
    if any(not isinstance(plant, SolaxPlantSnapshot) for plant in plants):
        raise ValueError("plants must contain only SolaxPlantSnapshot values")
    if not plants:
        raise ValueError("plants must contain at least one SolaxPlantSnapshot")

    seen = set()
    for plant in plants:
        if plant.plant_id in seen:
            raise FragmentAdapterConflict(
                "SolaX discovery contains duplicate plant {}".format(
                    plant.plant_id,
                )
            )
        seen.add(plant.plant_id)
    return tuple(sorted(plants, key=lambda item: item.plant_id))


def _normalize_devices(devices, plant_ids):
    """Freeze, sort, and collision-check device snapshots."""
    try:
        devices = tuple(devices)
    except TypeError as exc:
        raise ValueError(
            "devices must be an iterable of SolaxDeviceSnapshot",
        ) from exc
    if any(not isinstance(device, SolaxDeviceSnapshot) for device in devices):
        raise ValueError("devices must contain only SolaxDeviceSnapshot values")
    if not devices:
        raise ValueError("devices must contain at least one SolaxDeviceSnapshot")

    serial_owners = {}
    node_owners = {}
    for device in devices:
        if device.plant_id not in plant_ids:
            raise ValueError(
                "device {} references unknown plant {}".format(
                    device.serial,
                    device.plant_id,
                )
            )
        owner = serial_owners.get(device.serial)
        if owner is not None:
            raise FragmentAdapterConflict(
                "SolaX serial {} belongs to both plants {} and {}".format(
                    device.serial,
                    owner,
                    device.plant_id,
                )
            )
        serial_owners[device.serial] = device.plant_id
        node_owner = node_owners.get(device.node_id)
        if node_owner is not None:
            raise FragmentAdapterConflict(
                "SolaX node {} is claimed by both {} and {}".format(
                    device.node_id,
                    node_owner,
                    device.serial,
                )
            )
        node_owners[device.node_id] = device.serial
    return tuple(
        sorted(
            devices,
            key=lambda item: (
                item.plant_id,
                item.kind,
                item.serial,
            ),
        )
    )


def _plant_node(plant, provider_id):
    """Project one plant without claiming a control capability."""
    attributes = {"plantId": plant.plant_id}
    if plant.name is not None:
        attributes["name"] = plant.name
    return {
        "id": plant.node_id,
        "kind": "site",
        "attributes": attributes,
        "accessPaths": [
            {
                "id": "solax-cloud-api",
                "provider": provider_id,
                "preference": 0,
            }
        ],
        "capabilities": [],
    }


def _device_node(device, provider_id):
    """Project one device without claiming a read or control capability."""
    attributes = {
        "deviceSn": device.serial,
        "plantId": device.plant_id,
        "solaxKind": device.kind,
        "synthetic": device.synthetic,
    }
    if device.online is not None:
        attributes["online"] = device.online
    if device.model is not None:
        attributes["model"] = device.model
    if device.source_serial is not None:
        attributes["sourceSerial"] = device.source_serial
    return {
        "id": device.node_id,
        "kind": _TOPOLOGY_KINDS[device.kind],
        "attributes": attributes,
        "accessPaths": [
            {
                "id": "solax-cloud-api",
                "provider": provider_id,
                "preference": 0,
            }
        ],
        "capabilities": [],
    }


def _topology_document(
    provider_id,
    discovery_version,
    plants,
    devices,
):
    """Build one deterministic provider-owned v0.3 topology fragment."""
    nodes = [_plant_node(plant, provider_id) for plant in plants]
    nodes.extend(_device_node(device, provider_id) for device in devices)
    relationships = [
        {
            "from": "solax:plant:{}".format(device.plant_id),
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
            "name": "SolaX Cloud",
            "provider": provider_id,
            "authority": 0,
        },
        "nodes": nodes,
        "relationships": relationships,
    }


def _reference_aliases(plants, devices):
    """Publish provider-local names that never grant materialization roles."""
    aliases = [
        ProviderAlias(
            name="plant:{}".format(plant.plant_id),
            node_id=plant.node_id,
            roles=frozenset((AliasRole.REFERENCE,)),
        )
        for plant in plants
    ]
    aliases.extend(
        ProviderAlias(
            name=("synthetic:{}".format(device.serial) if device.synthetic else "serial:{}".format(device.serial)),
            node_id=device.node_id,
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


def _identity_aliases(plants, devices):
    """Publish only globally unambiguous plant and hardware identities."""
    aliases = [
        ProviderIdentityAlias(
            kind="solax-plant-id",
            value=plant.plant_id,
            node_id=plant.node_id,
        )
        for plant in plants
    ]
    aliases.extend(
        ProviderIdentityAlias(
            kind="serial",
            value=device.serial,
            node_id=device.node_id,
        )
        for device in devices
        if not device.synthetic
    )
    return tuple(
        sorted(
            aliases,
            key=lambda item: (item.kind, item.value, item.node_id),
        )
    )


class SolaxCloudFragmentPublisher:
    """Adapt explicit SolaX snapshots into one durable compiler fragment."""

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
        plants,
        devices,
        health=_HEALTH_UNCHANGED,
    ):
        """Publish one accepted complete plant/device discovery snapshot."""
        if not self._enabled:
            return False
        discovery_version = _discovery_version(discovery_version)
        plants = _normalize_plants(plants)
        devices = _normalize_devices(
            devices,
            frozenset(plant.plant_id for plant in plants),
        )
        document = _topology_document(
            self.provider_id,
            discovery_version,
            plants,
            devices,
        )

        with self._lock:
            current = self._current_state()
            if current is not None and current.removed:
                raise FragmentAdapterRemoved(
                    "provider {} was removed at generation {}; SolaX "
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
                        "provider {} reused SolaX discovery version {} "
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
                aliases=_reference_aliases(plants, devices),
                identity_aliases=_identity_aliases(plants, devices),
                role_assignments=(),
                config_projections=(),
            )
            published = self._adapter.publish(
                snapshot,
                "SolaX discovery changed to version {}".format(
                    discovery_version,
                ),
            )
            if published:
                self._seeded = True
            return published

    def set_liveness(self, health):
        """Publish provider liveness without changing discovery contents."""
        if not self._enabled:
            return False
        health = _provider_health(health)
        with self._lock:
            current = self._current_state()
            if current is None:
                raise FragmentAdapterReadError(
                    "SolaX discovery must be seeded before liveness",
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
                "SolaX liveness changed to {}".format(health.value),
            )

    def remove(self):
        """Publish an irreversible provider-removal tombstone."""
        if not self._enabled:
            return False
        with self._lock:
            current = self._current_state()
            if current is None:
                raise FragmentAdapterReadError(
                    "SolaX discovery must be seeded before removal",
                )
            if current.removed:
                return False
            return self._adapter.remove(
                current.generation + 1,
                "SolaX integration removed",
            )
