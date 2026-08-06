# -----------------------------------------------------------------------------
# Predbat Home Battery System - Gateway retained-topology Lattice adapter
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Pure, default-off Gateway retained-topology fragment publisher.

The live Gateway component already receives a provider-owned retained topology
document and a separate liveness signal.  This module adapts those two inputs
to the common ``lattice_fragment_adapter()`` surface without registering
itself, mutating PredBat configuration, or publishing control.

Gateway retained topology remains on the existing v0.2/v0.3 document surface
in this tranche.  The v0.4 schedule transport is independent; retained v0.4
topology is rejected until the shared topology compiler is promoted explicitly.

Gateway aliases are deliberately REFERENCE-only.  The adapter exposes
provider-local capability projection metadata for inspection and future
composition, but publishes no PRIMARY/CONTROL role assignment and no
configuration projection ready for materialization.
"""

# cspell:ignore autoconfig materializable

import threading
from dataclasses import dataclass
from typing import Optional

from lattice_autoconfig import (
    AliasRole,
    ProjectionCardinality,
    ProjectionRouting,
    ProjectionValueKind,
    ProviderAlias,
    ProviderConfigProjection,
    ProviderHealth,
    ProviderIdentityAlias,
    ProviderProjectionValue,
    ProviderRoleAssignment,
    ProviderSnapshot,
    _fingerprint_snapshot,
    _plain,
)
from gateway_autoconfig import GatewayAutoConfigPlan
from lattice_fragment_adapters import (
    DurableFragmentAdapter,
    FragmentAdapterReadError,
    FragmentAdapterRemoved,
)
from lattice_topology import TopologyValidationError, decode_topology


_LIVENESS_UNCHANGED = object()
_CONFIG_ACCESS_PATH = "predbat-config"

_CONTROL_ARGUMENTS = frozenset(
    (
        "charge_rate",
        "discharge_rate",
        "reserve",
        "charge_limit",
        "charge_start_time",
        "charge_end_time",
        "discharge_start_time",
        "discharge_end_time",
        "scheduled_charge_enable",
        "scheduled_discharge_enable",
        "export_limit",
        "charge_rate_percent",
        "discharge_rate_percent",
        "pause_mode",
        "pause_start_time",
        "pause_end_time",
        "discharge_target_soc",
        "idle_start_time",
        "idle_end_time",
    )
)
_FIRST_SOURCE_ARGUMENTS = frozenset(
    (
        "pv_today",
        "import_today",
        "export_today",
        "load_today",
        "battery_scaling",
        "battery_rate_max",
        "inverter_time",
    )
)
_SCALAR_ARGUMENTS = frozenset(
    (
        "num_inverters",
        "battery_temperature_history",
        "givtcp_rest",
        "ge_cloud_serial",
        "ge_cloud_data",
        "ge_cloud_direct",
        "inverter_hybrid",
        "ems_total_soc",
        "ems_total_charge",
        "ems_total_discharge",
        "ems_total_grid",
        "ems_total_pv",
        "ems_total_load",
    )
)


def _serial_nodes(document):
    """Index retained nodes by normalized physical serial identity."""
    result = {}
    for node in _live_nodes(document):
        attributes = node.get("attributes") or {}
        serial = attributes.get("serial")
        if isinstance(serial, str) and serial.strip():
            result[serial.strip().upper()] = str(node["id"])
    return result


def _projection_sources(plan, argument, value):
    """Return the selected serial owning each raw legacy value."""
    serials = plan.selected_serials
    if not serials:
        raise TopologyValidationError("Gateway auto-config plan has no targets")
    if isinstance(value, tuple):
        if argument in _FIRST_SOURCE_ARGUMENTS:
            return (serials[0],) * len(value)
        if len(value) == len(serials):
            return serials
        raise TopologyValidationError(
            "Gateway projection {} has {} values for {} targets".format(
                argument,
                len(value),
                len(serials),
            )
        )
    return (serials[0],)


def _projection_group(argument):
    """Keep variable-width source arrays independent from inverter slots."""
    if argument == "pv_power":
        return "pv_power_sources"
    if argument == "pv_today":
        return "pv_energy_sources"
    if argument in ("import_today", "export_today", "load_today"):
        return "grid_energy_sources"
    if argument in ("battery_scaling", "battery_rate_max", "inverter_time"):
        return "battery_metadata_sources"
    if argument == "num_inverters" or not isinstance(argument, str):
        return "site"
    return "inverters"


def _value_kind(value):
    """Map a raw immutable mapper value to the compiler value kind."""
    if value is None:
        return ProjectionValueKind.NONE
    if isinstance(value, str) and "." in value:
        return ProjectionValueKind.ENTITY
    return ProjectionValueKind.CONSTANT


def _materializable_snapshot(document, plan, provider_id, generation, health):
    """Bind one pure Gateway plan to retained topology identities."""
    if not isinstance(plan, GatewayAutoConfigPlan):
        raise TypeError("plan must be GatewayAutoConfigPlan")
    document = _plain(document)
    nodes_by_serial = _serial_nodes(document)
    missing = sorted(serial for serial in plan.selected_serials if serial.upper() not in nodes_by_serial)
    if missing:
        raise TopologyValidationError("Gateway auto-config serials are absent from retained topology: {}".format(", ".join(missing)))
    node_lookup = {str(node["id"]): node for node in document.get("nodes", ())}
    selected_nodes = tuple(nodes_by_serial[serial.upper()] for serial in plan.selected_serials)
    if any(node_lookup[node_id].get("kind") == "energy-management-system" for node_id in selected_nodes):
        raise TopologyValidationError("Gateway EMS plans require the multi-battery coordinator model")

    assignments = set()
    for index, node_id in enumerate(selected_nodes):
        assignments.add((AliasRole.PRIMARY, "inverters", index, node_id))
        assignments.add((AliasRole.CONTROL, "inverters", index, node_id))

    projections = []
    arguments = dict(plan.arguments)
    if plan.hybrid is not None:
        arguments["inverter_hybrid"] = plan.hybrid
    for argument, raw_value in sorted(arguments.items()):
        if not isinstance(raw_value, tuple) and argument not in _SCALAR_ARGUMENTS:
            raw_value = tuple(raw_value for _serial in plan.selected_serials)
        values = raw_value if isinstance(raw_value, tuple) else (raw_value,)
        serials = _projection_sources(plan, argument, raw_value)
        node_ids = tuple(nodes_by_serial[serial.upper()] for serial in serials)
        cardinality = ProjectionCardinality.PER_INDEX if isinstance(raw_value, tuple) else ProjectionCardinality.SCALAR
        group = _projection_group(argument) if cardinality is ProjectionCardinality.PER_INDEX else "site"
        role = AliasRole.CONTROL if argument in _CONTROL_ARGUMENTS else AliasRole.PRIMARY
        for index, node_id in enumerate(node_ids):
            assignments.add((role, group, index, node_id))

        projection_values = []
        for node_id, serial, value in zip(node_ids, serials, values):
            kind = _value_kind(value)
            capability = None
            access_path_id = None
            identity_kind = None
            identity_value = None
            if kind is not ProjectionValueKind.NONE:
                identity_kind = "serial"
                identity_value = serial.upper()
                access_path_id = _CONFIG_ACCESS_PATH
            if kind is ProjectionValueKind.ENTITY:
                capability = "predbat.config.{}".format(argument)
                node = node_lookup[node_id]
                access_paths = node.setdefault("accessPaths", [])
                if not any(str(path.get("id")) == _CONFIG_ACCESS_PATH for path in access_paths):
                    access_paths.append(
                        {
                            "id": _CONFIG_ACCESS_PATH,
                            "provider": provider_id,
                            "preference": 100,
                        }
                    )
                capabilities = node.setdefault("capabilities", [])
                if not any(offer.get("capability") == capability and offer.get("accessPath") == _CONFIG_ACCESS_PATH for offer in capabilities):
                    capabilities.append(
                        {
                            "capability": capability,
                            "accessPath": _CONFIG_ACCESS_PATH,
                            "read": {
                                "protocol": "home-assistant",
                                "address": value,
                            },
                        }
                    )
            projection_values.append(
                ProviderProjectionValue(
                    node_id=node_id,
                    kind=kind,
                    value=value,
                    capability=capability,
                    identity_kind=identity_kind,
                    identity_value=identity_value,
                    access_path_id=access_path_id,
                )
            )
        projections.append(
            ProviderConfigProjection(
                argument=argument,
                role=role,
                group=group,
                routing=ProjectionRouting.LEAF,
                cardinality=cardinality,
                values=tuple(projection_values),
                required=False,
            )
        )

    for node in document.get("nodes", ()):
        attributes = node.get("attributes")
        if isinstance(attributes, dict):
            serial = attributes.get("serial")
            if isinstance(serial, str) and serial.strip():
                attributes["serial"] = serial.strip().upper()
    roles = tuple(
        ProviderRoleAssignment(role, group, index, node_id)
        for role, group, index, node_id in sorted(
            assignments,
            key=lambda item: (item[1], item[0].value, item[2], item[3]),
        )
    )
    return ProviderSnapshot(
        provider_id=provider_id,
        generation=generation,
        health=health,
        topology_fragment=document,
        aliases=_reference_aliases(document),
        identity_aliases=_identity_aliases(document),
        role_assignments=roles,
        config_projections=tuple(projections),
    )


@dataclass(frozen=True)
class GatewayCapabilityProjection:
    """One provider-local capability coordinate from the retained document."""

    node_id: str
    capability: str
    access_path_id: str
    cap_ref: Optional[int]
    readable: bool
    controllable: bool


@dataclass(frozen=True)
class GatewayTopologyProjection:
    """Immutable inspection metadata for one durable Gateway fragment."""

    provider_id: str
    generation: int
    semantic_fingerprint: str
    health: ProviderHealth
    topology_version: str
    document_version: int
    node_ids: tuple
    capabilities: tuple
    removed: bool


def _document_version(document):
    """Return an explicit version or the legacy v0.2 retained-doc default."""
    value = document.get("docVersion")
    if value is None:
        return 0
    return value


def _health_from_liveness(online):
    """Map the independent Gateway liveness signal to provider health."""
    if online is True:
        return ProviderHealth.HEALTHY
    if online is False:
        return ProviderHealth.OFFLINE
    if online is None:
        return ProviderHealth.DEGRADED
    raise ValueError("online must be True, False, or None")


def _live_nodes(document):
    """Return provider nodes that are not topology tombstones."""
    return tuple(node for node in document.get("nodes", ()) if node.get("removed") is not True)


def _reference_aliases(document):
    """Build deterministic REFERENCE-only aliases for every provider node."""
    return tuple(
        ProviderAlias(
            name="node:{}".format(node_id),
            node_id=node_id,
            roles=frozenset((AliasRole.REFERENCE,)),
        )
        for node_id in sorted(str(node["id"]) for node in _live_nodes(document))
    )


def _identity_aliases(document):
    """Publish strong identities asserted by the provider-owned fragment."""
    aliases = []
    for node in _live_nodes(document):
        node_id = str(node["id"])
        aliases.append(
            ProviderIdentityAlias(
                kind="lattice-node-id",
                value=node_id,
                node_id=node_id,
            )
        )
        attributes = node.get("attributes")
        serial = attributes.get("serial") if isinstance(attributes, dict) else None
        if isinstance(serial, str) and serial.strip():
            aliases.append(
                ProviderIdentityAlias(
                    kind="serial",
                    value=serial.strip(),
                    node_id=node_id,
                )
            )
    return tuple(
        sorted(
            aliases,
            key=lambda item: (item.kind, item.value, item.node_id),
        )
    )


def _valid_binding(binding):
    """Return whether a read/control binding has usable protocol coordinates."""
    if not isinstance(binding, dict):
        return False
    protocol = binding.get("protocol")
    if not isinstance(protocol, str) or not protocol.strip():
        return False
    if "address" not in binding:
        return False
    address = binding["address"]
    if isinstance(address, bool):
        return False
    if isinstance(address, str):
        return bool(address.strip())
    return isinstance(address, (int, dict))


def _capability_projections(document):
    """Extract immutable provider-local refs without inferring brand behavior."""
    projections = []
    for node in _live_nodes(document):
        node_id = str(node["id"])
        for offer in node.get("capabilities", ()):
            if not isinstance(offer, dict) or offer.get("removed") is True:
                continue
            capability = offer.get("capability")
            if capability is None:
                continue
            cap_ref = offer.get("ref")
            if not isinstance(cap_ref, int) or isinstance(cap_ref, bool) or cap_ref <= 0:
                cap_ref = None
            projections.append(
                GatewayCapabilityProjection(
                    node_id=node_id,
                    capability=str(capability),
                    access_path_id=str(offer.get("accessPath", "")),
                    cap_ref=cap_ref,
                    readable=_valid_binding(offer.get("read")) or isinstance(offer.get("derived"), dict),
                    controllable=_valid_binding(offer.get("control")),
                )
            )
    return tuple(
        sorted(
            projections,
            key=lambda item: (
                item.node_id,
                item.capability,
                item.access_path_id,
                item.cap_ref or 0,
            ),
        )
    )


def _validate_fragment(document, provider_id):
    """Require one provider-local fragment owned by this exact adapter."""
    if document.get("scope") != "fragment":
        raise TopologyValidationError("Gateway retained topology scope must be fragment")
    document_provider = document["producer"]["provider"]
    if document_provider != provider_id:
        raise TopologyValidationError(
            "Gateway adapter {} cannot publish fragment owned by {}".format(
                provider_id,
                document_provider,
            )
        )


class GatewayRetainedTopologyFragmentPublisher:
    """Adapt retained topology and liveness into a durable fragment publisher."""

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
        """Return whether this explicitly constructed reference publisher runs."""
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
    def projection_metadata(self):
        """Return immutable provider-local projection metadata."""
        state = self.read_state()
        document = _plain(state.snapshot.topology_fragment)
        return GatewayTopologyProjection(
            provider_id=state.provider_id,
            generation=state.generation,
            semantic_fingerprint=state.semantic_fingerprint,
            health=state.snapshot.health,
            topology_version=document["topologyVersion"],
            document_version=_document_version(document),
            node_ids=tuple(sorted(str(node["id"]) for node in _live_nodes(document))),
            capabilities=_capability_projections(document),
            removed=state.removed,
        )

    def lattice_fragment_adapter(self):
        """Expose the structural registry surface only when enabled and seeded."""
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
        """Subscribe to topology, liveness, and removal invalidations."""
        return self._adapter.subscribe_invalidation(listener)

    def _current_state(self):
        """Return the current state, treating an unseeded store as empty."""
        if not self._seeded:
            return None
        return self._adapter.read_state()

    def ingest_retained_topology(self, payload, online=_LIVENESS_UNCHANGED):
        """Publish one accepted retained fragment and invalidate subscribers."""
        if not self._enabled:
            return False
        document = decode_topology(payload)
        _validate_fragment(document, self.provider_id)

        with self._lock:
            current = self._current_state()
            if online is _LIVENESS_UNCHANGED and current is not None:
                health = current.snapshot.health
            elif online is _LIVENESS_UNCHANGED:
                health = ProviderHealth.DEGRADED
            else:
                health = _health_from_liveness(online)
            if current is not None and current.removed:
                raise FragmentAdapterRemoved(
                    "provider {} was removed at generation {}; retained topology "
                    "cannot re-enrol it".format(
                        self.provider_id,
                        current.generation,
                    )
                )
            if current is not None:
                previous = _plain(current.snapshot.topology_fragment)
                previous_explicit = "docVersion" in previous
                incoming_explicit = "docVersion" in document
                previous_version = _document_version(previous)
                incoming_version = _document_version(document)
                if previous_explicit and not incoming_explicit:
                    return False
                if previous_explicit and incoming_explicit:
                    if incoming_version < previous_version:
                        return False
                    if incoming_version == previous_version:
                        if document != previous:
                            raise TopologyValidationError(
                                "provider {} reused docVersion {} for different "
                                "content".format(
                                    self.provider_id,
                                    incoming_version,
                                )
                            )
                        if health is current.snapshot.health:
                            return False
                elif document == previous and health is current.snapshot.health:
                    return False

            generation = 1 if current is None else current.generation + 1
            snapshot = ProviderSnapshot(
                provider_id=self.provider_id,
                generation=generation,
                health=health,
                topology_fragment=document,
                aliases=_reference_aliases(document),
                identity_aliases=_identity_aliases(document),
                role_assignments=(),
                config_projections=(),
            )
            published = self._adapter.publish(
                snapshot,
                "gateway retained topology changed",
            )
            if published:
                self._seeded = True
            return published

    def set_liveness(self, online):
        """Publish a liveness-only generation when provider health changes."""
        if not self._enabled:
            return False
        health = _health_from_liveness(online)
        with self._lock:
            current = self._current_state()
            if current is None:
                raise FragmentAdapterReadError("Gateway topology must be seeded before liveness")
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
                role_assignments=previous.role_assignments,
                config_projections=previous.config_projections,
            )
            return self._adapter.publish(
                snapshot,
                "gateway liveness changed to {}".format(health.value),
            )

    def ingest_auto_config(self, plan):
        """Attach one pure mapper plan to the current retained fragment."""
        if not self._enabled:
            return False
        with self._lock:
            current = self._current_state()
            if current is None:
                raise FragmentAdapterReadError("Gateway topology must be seeded before auto-config")
            if current.removed:
                raise FragmentAdapterRemoved(
                    "provider {} was removed at generation {}".format(
                        self.provider_id,
                        current.generation,
                    )
                )
            snapshot = _materializable_snapshot(
                current.snapshot.topology_fragment,
                plan,
                self.provider_id,
                current.generation,
                current.snapshot.health,
            )
            if _fingerprint_snapshot(snapshot) == _fingerprint_snapshot(current.snapshot):
                return False
            snapshot = _materializable_snapshot(
                current.snapshot.topology_fragment,
                plan,
                self.provider_id,
                current.generation + 1,
                current.snapshot.health,
            )
            return self._adapter.publish(
                snapshot,
                "gateway auto-config plan changed",
            )

    def ingest_complete(self, payload, plan, online=_LIVENESS_UNCHANGED):
        """Atomically publish retained topology and its complete mapper plan."""
        if not self._enabled:
            return False
        document = decode_topology(payload)
        _validate_fragment(document, self.provider_id)

        with self._lock:
            current = self._current_state()
            if online is _LIVENESS_UNCHANGED and current is not None:
                health = current.snapshot.health
            elif online is _LIVENESS_UNCHANGED:
                health = ProviderHealth.DEGRADED
            else:
                health = _health_from_liveness(online)
            if current is not None and current.removed:
                raise FragmentAdapterRemoved(
                    "provider {} was removed at generation {}".format(
                        self.provider_id,
                        current.generation,
                    )
                )
            if current is not None:
                previous = _plain(current.snapshot.topology_fragment)
                previous_explicit = "docVersion" in previous
                incoming_explicit = "docVersion" in document
                previous_version = _document_version(previous)
                incoming_version = _document_version(document)
                if previous_explicit and not incoming_explicit:
                    return False
                if previous_explicit and incoming_explicit:
                    if incoming_version < previous_version:
                        return False
                    if incoming_version == previous_version and document != previous:
                        raise TopologyValidationError(
                            "provider {} reused docVersion {} for different content".format(
                                self.provider_id,
                                incoming_version,
                            )
                        )
                candidate = _materializable_snapshot(
                    document,
                    plan,
                    self.provider_id,
                    current.generation,
                    health,
                )
                if _fingerprint_snapshot(candidate) == _fingerprint_snapshot(current.snapshot):
                    return False
                generation = current.generation + 1
            else:
                generation = 1
            snapshot = _materializable_snapshot(
                document,
                plan,
                self.provider_id,
                generation,
                health,
            )
            published = self._adapter.publish(
                snapshot,
                "gateway topology and auto-config changed",
            )
            if published:
                self._seeded = True
            return published

    def remove(self):
        """Publish a durable provider-removal tombstone and invalidate."""
        if not self._enabled:
            return False
        with self._lock:
            current = self._current_state()
            if current is None:
                raise FragmentAdapterReadError("Gateway topology must be seeded before removal")
            if current.removed:
                return False
            return self._adapter.remove(
                current.generation + 1,
                "gateway integration removed",
            )
