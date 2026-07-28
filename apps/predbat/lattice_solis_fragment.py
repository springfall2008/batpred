# -----------------------------------------------------------------------------
# Predbat Home Battery System - Solis Lattice fragment adapter
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Pure, default-off Solis fragment publisher.

The live ``SolisAPI`` component owns cloud authentication, discovery, CID
reads/writes, automatic configuration, and controls.  Local Solis inverter
profiles are a separate configuration path.  This module imports, registers,
and mutates neither path.  A future gated seam may pass explicit immutable
snapshots here after it has proved which local/cloud paths address one device.

Only a provider-owned device id and an explicitly verified hardware serial are
identity.  Station ids, configured serials, endpoints, authentication methods,
protocol versions, and CID/TOU profiles remain provider metadata.  Published
aliases are REFERENCE-only, with no roles, configuration projections,
capabilities, or control writes.
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
_CHANNELS = frozenset(("cloud", "local"))
_CLOUD_AUTH_METHODS = frozenset(("hmac", "oauth"))


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
    """Normalize one provider model code/name without accepting booleans."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return _required_text(value, "model")


def _input_version(value, name):
    """Validate one provider-owned monotonic input version."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("{} must be a non-negative integer".format(name))
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
    """Normalize Solis boolean/zero/one online status without guessing others."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return value == 1
    raise ValueError("online must be True, False, 1, 0, or None")


@dataclass(frozen=True)
class SolisAccessPathSnapshot:
    """One explicit access path; its coordinates never establish identity."""

    path_id: str
    channel: str
    protocol_version: str
    cid_profile: str
    auth_method: Optional[str] = None
    endpoint_id: Optional[str] = None

    def __post_init__(self):
        """Normalize access metadata and reject local/cloud ambiguity."""
        path_id = _required_text(self.path_id, "path_id")
        channel = _required_text(self.channel, "channel").lower()
        if channel not in _CHANNELS:
            raise ValueError("channel must be cloud or local")
        protocol_version = _required_text(
            self.protocol_version,
            "protocol_version",
        )
        cid_profile = _required_text(self.cid_profile, "cid_profile")
        auth_method = _optional_text(self.auth_method, "auth_method")
        if auth_method is not None:
            auth_method = auth_method.lower()
        endpoint_id = _optional_text(self.endpoint_id, "endpoint_id")

        if channel == "cloud":
            if auth_method not in _CLOUD_AUTH_METHODS:
                raise ValueError(
                    "a cloud access path requires hmac or oauth auth_method",
                )
        elif auth_method is not None:
            raise ValueError(
                "a local access path must not carry a cloud auth_method",
            )

        object.__setattr__(self, "path_id", path_id)
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "protocol_version", protocol_version)
        object.__setattr__(self, "cid_profile", cid_profile)
        object.__setattr__(self, "auth_method", auth_method)
        object.__setattr__(self, "endpoint_id", endpoint_id)


@dataclass(frozen=True)
class SolisDeviceSnapshot:
    """One explicitly grouped provider-local Solis inverter."""

    device_id: str
    access_paths: tuple
    serial: Optional[str] = None
    serial_verified: bool = False
    online: Optional[object] = None
    model: Optional[object] = None
    has_battery: Optional[bool] = None
    station_id: Optional[str] = None

    def __post_init__(self):
        """Normalize state while keeping unproved serials provider-local."""
        device_id = _required_text(self.device_id, "device_id")
        try:
            access_paths = tuple(self.access_paths)
        except TypeError as exc:
            raise ValueError(
                "access_paths must be an iterable of " "SolisAccessPathSnapshot",
            ) from exc
        if not access_paths:
            raise ValueError(
                "access_paths must contain at least one " "SolisAccessPathSnapshot",
            )
        if any(not isinstance(path, SolisAccessPathSnapshot) for path in access_paths):
            raise ValueError(
                "access_paths must contain only SolisAccessPathSnapshot " "values",
            )
        path_ids = set()
        for path in access_paths:
            if path.path_id in path_ids:
                raise FragmentAdapterConflict(
                    "Solis device {} contains duplicate access path {}".format(
                        device_id,
                        path.path_id,
                    )
                )
            path_ids.add(path.path_id)

        serial = _optional_text(self.serial, "serial")
        if serial is not None:
            serial = serial.upper()
        if not isinstance(self.serial_verified, bool):
            raise ValueError("serial_verified must be a boolean")
        if self.serial_verified and serial is None:
            raise ValueError("serial_verified requires serial")
        if self.has_battery is not None and not isinstance(
            self.has_battery,
            bool,
        ):
            raise ValueError("has_battery must be True, False, or None")

        object.__setattr__(self, "device_id", device_id)
        object.__setattr__(
            self,
            "access_paths",
            tuple(sorted(access_paths, key=lambda item: item.path_id)),
        )
        object.__setattr__(self, "serial", serial)
        object.__setattr__(self, "online", _online_status(self.online))
        object.__setattr__(self, "model", _optional_model(self.model))
        object.__setattr__(
            self,
            "station_id",
            _optional_text(self.station_id, "station_id"),
        )

    @property
    def node_id(self):
        """Return the deterministic provider-local device node identity."""
        return "solis:device:{}".format(self.device_id)


def _normalize_devices(devices):
    """Freeze, sort, and collision-check explicit device snapshots."""
    try:
        devices = tuple(devices)
    except TypeError as exc:
        raise ValueError(
            "devices must be an iterable of SolisDeviceSnapshot",
        ) from exc
    if any(not isinstance(device, SolisDeviceSnapshot) for device in devices):
        raise ValueError(
            "devices must contain only SolisDeviceSnapshot values",
        )
    if not devices:
        raise ValueError(
            "devices must contain at least one SolisDeviceSnapshot",
        )

    device_ids = set()
    verified_serials = {}
    for device in devices:
        if device.device_id in device_ids:
            raise FragmentAdapterConflict(
                "Solis discovery contains duplicate device {}".format(
                    device.device_id,
                )
            )
        device_ids.add(device.device_id)
        if device.serial_verified:
            owner = verified_serials.get(device.serial)
            if owner is not None:
                raise FragmentAdapterConflict(
                    "verified Solis serial {} belongs to both {} and {}".format(
                        device.serial,
                        owner,
                        device.device_id,
                    )
                )
            verified_serials[device.serial] = device.device_id
    return tuple(sorted(devices, key=lambda item: item.device_id))


def _access_path(path, provider_id):
    """Project provider coordinates without asserting a capability."""
    projected = {
        "id": path.path_id,
        "provider": provider_id,
        "preference": 0,
        "channel": path.channel,
        "protocolVersion": path.protocol_version,
        "cidProfile": path.cid_profile,
    }
    if path.auth_method is not None:
        projected["authMethod"] = path.auth_method
    if path.endpoint_id is not None:
        projected["endpointId"] = path.endpoint_id
    return projected


def _device_node(device, provider_id):
    """Project one inverter without claiming a read or control capability."""
    attributes = {
        "solisDeviceId": device.device_id,
        "serialVerified": device.serial_verified,
    }
    if device.serial is not None:
        attributes["serial"] = device.serial
    if device.online is not None:
        attributes["online"] = device.online
    if device.model is not None:
        attributes["model"] = device.model
    if device.has_battery is not None:
        attributes["hasBattery"] = device.has_battery
    if device.station_id is not None:
        attributes["stationId"] = device.station_id
    return {
        "id": device.node_id,
        "kind": "inverter",
        "attributes": attributes,
        "accessPaths": [_access_path(path, provider_id) for path in device.access_paths],
        "capabilities": [],
    }


def _topology_document(
    provider_id,
    generation,
    discovery_version,
    config_version,
    devices,
):
    """Build one deterministic provider-owned v0.3 topology fragment."""
    return {
        "topologyVersion": "0.3.0",
        "scope": "fragment",
        "docVersion": generation,
        "producer": {
            "name": "Solis",
            "provider": provider_id,
            "authority": 0,
            "inputVersions": {
                "discovery": discovery_version,
                "config": config_version,
            },
        },
        "nodes": [_device_node(device, provider_id) for device in devices],
    }


def _semantic_document(document):
    """Return topology semantics without the derived durable generation."""
    document = _plain(document)
    document.pop("docVersion", None)
    return document


def _input_versions(document):
    """Read and validate the durable two-input cursor."""
    try:
        versions = document["producer"]["inputVersions"]
        return (
            _input_version(versions["discovery"], "discovery_version"),
            _input_version(versions["config"], "config_version"),
        )
    except (KeyError, TypeError) as exc:
        raise FragmentAdapterReadError(
            "durable Solis fragment has no valid inputVersions cursor",
        ) from exc


def _reference_aliases(devices):
    """Publish provider-local names that never grant materialization roles."""
    aliases = []
    for device in devices:
        aliases.append(
            ProviderAlias(
                name="device:{}".format(device.device_id),
                node_id=device.node_id,
                roles=frozenset((AliasRole.REFERENCE,)),
            )
        )
        if device.serial is not None:
            prefix = "serial" if device.serial_verified else "configured-serial"
            aliases.append(
                ProviderAlias(
                    name="{}:{}".format(prefix, device.serial),
                    node_id=device.node_id,
                    roles=frozenset((AliasRole.REFERENCE,)),
                )
            )
    return tuple(
        sorted(
            aliases,
            key=lambda item: (item.name, item.node_id),
        )
    )


def _identity_aliases(provider_id, devices):
    """Publish provider-qualified ids and only explicitly verified serials."""
    aliases = []
    for device in devices:
        aliases.append(
            ProviderIdentityAlias(
                kind="solis-provider-device-id",
                value="{}:{}".format(provider_id, device.device_id),
                node_id=device.node_id,
            )
        )
        if device.serial_verified:
            aliases.append(
                ProviderIdentityAlias(
                    kind="serial",
                    value=device.serial,
                    node_id=device.node_id,
                )
            )
    return tuple(
        sorted(
            aliases,
            key=lambda item: (item.kind, item.value, item.node_id),
        )
    )


class SolisFragmentPublisher:
    """Adapt explicit Solis snapshots into one durable compiler fragment."""

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
    def input_versions(self):
        """Fresh-read the current discovery/config input cursor."""
        return _input_versions(
            _plain(self.read_state().snapshot.topology_fragment),
        )

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
        """Subscribe to discovery, config, liveness, and removal changes."""
        return self._adapter.subscribe_invalidation(listener)

    def _current_state(self):
        """Return the current state, treating an unseeded store as empty."""
        if not self._seeded:
            return None
        return self._adapter.read_state()

    def ingest_snapshot(
        self,
        discovery_version,
        config_version,
        devices,
        health=_HEALTH_UNCHANGED,
    ):
        """Publish one accepted complete discovery/config snapshot."""
        if not self._enabled:
            return False
        discovery_version = _input_version(
            discovery_version,
            "discovery_version",
        )
        config_version = _input_version(config_version, "config_version")
        devices = _normalize_devices(devices)

        with self._lock:
            current = self._current_state()
            if current is not None and current.removed:
                raise FragmentAdapterRemoved(
                    "provider {} was removed at generation {}; Solis input "
                    "cannot re-enrol it".format(
                        self.provider_id,
                        current.generation,
                    )
                )
            next_generation = 1 if current is None else current.generation + 1
            document = _topology_document(
                self.provider_id,
                next_generation,
                discovery_version,
                config_version,
                devices,
            )
            if current is None:
                next_health = ProviderHealth.DEGRADED if health is _HEALTH_UNCHANGED else _provider_health(health)
            else:
                previous = _plain(current.snapshot.topology_fragment)
                previous_discovery, previous_config = _input_versions(previous)
                discovery_regressed = discovery_version < previous_discovery
                config_regressed = config_version < previous_config
                discovery_advanced = discovery_version > previous_discovery
                config_advanced = config_version > previous_config
                if discovery_regressed or config_regressed:
                    if not discovery_advanced and not config_advanced:
                        return False
                    raise FragmentAdapterConflict(
                        "provider {} mixed Solis input regression from "
                        "discovery/config {}/{} to {}/{}".format(
                            self.provider_id,
                            previous_discovery,
                            previous_config,
                            discovery_version,
                            config_version,
                        )
                    )
                same_versions = not discovery_advanced and not config_advanced
                if same_versions and _semantic_document(document) != _semantic_document(previous):
                    raise FragmentAdapterConflict(
                        "provider {} reused Solis discovery/config versions "
                        "{}/{} for different content".format(
                            self.provider_id,
                            discovery_version,
                            config_version,
                        )
                    )
                next_health = current.snapshot.health if health is _HEALTH_UNCHANGED else _provider_health(health)
                if same_versions and next_health is current.snapshot.health:
                    return False

            snapshot = ProviderSnapshot(
                provider_id=self.provider_id,
                generation=next_generation,
                health=next_health,
                topology_fragment=document,
                aliases=_reference_aliases(devices),
                identity_aliases=_identity_aliases(
                    self.provider_id,
                    devices,
                ),
                role_assignments=(),
                config_projections=(),
            )
            published = self._adapter.publish(
                snapshot,
                "Solis inputs changed to discovery/config {}/{}".format(
                    discovery_version,
                    config_version,
                ),
            )
            if published:
                self._seeded = True
            return published

    def set_liveness(self, health):
        """Publish provider liveness without changing structural inputs."""
        if not self._enabled:
            return False
        health = _provider_health(health)
        with self._lock:
            current = self._current_state()
            if current is None:
                raise FragmentAdapterReadError(
                    "Solis inputs must be seeded before liveness",
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
                "Solis liveness changed to {}".format(health.value),
            )

    def remove(self):
        """Publish an irreversible provider-removal tombstone."""
        if not self._enabled:
            return False
        with self._lock:
            current = self._current_state()
            if current is None:
                raise FragmentAdapterReadError(
                    "Solis inputs must be seeded before removal",
                )
            if current.removed:
                return False
            return self._adapter.remove(
                current.generation + 1,
                "Solis integration removed",
            )
