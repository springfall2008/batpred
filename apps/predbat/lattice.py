# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice device-mapping core (read-only)
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Pure, dependency-free Lattice device-mapping model.

Each integration publishes a *fragment* describing the devices it can see — their identity,
type, access paths, and the sensors they expose (referencing existing entities). Fragments merge
by identity into one site graph: a device seen via two integrations becomes ONE node carrying
both providers' access paths (ranked) and the union of its sensors.

READ-ONLY by design: this maps the network and inventories sensors. Control is a separate model
(a common intent/shape/binding API) and deliberately not part of this — see the lattice-spec repo.
No PredBat/Home Assistant dependencies, so it can be unit-tested standalone.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AccessPath:
    """A way to reach a node (a provider/transport with a ranked preference)."""

    id: str
    provider: str
    locality: str = "local"
    transport: str = ""
    preference: int = 0

    @staticmethod
    def from_dict(d):
        """Build an AccessPath from its wire dict."""
        return AccessPath(
            id=d["id"],
            provider=d.get("provider", ""),
            locality=d.get("locality", "local"),
            transport=d.get("transport", ""),
            preference=int(d.get("preference", 0)),
        )


@dataclass
class Sensor:
    """A telemetry reading a node exposes, referencing the existing entity that carries it."""

    capability: str
    unit: str = ""
    entity: str = ""
    access_path: str = ""

    @staticmethod
    def from_dict(d):
        """Build a Sensor from its wire dict."""
        return Sensor(capability=d["capability"], unit=d.get("unit", ""), entity=d.get("entity", ""), access_path=d.get("accessPath", ""))


@dataclass
class Node:
    """A device in the graph, identified by id (serial), with access paths + sensors."""

    id: str
    kind: str
    device_type: str
    access_paths: list = field(default_factory=list)
    sensors: list = field(default_factory=list)

    def sensor(self, name) -> Optional[Sensor]:
        """Return the first Sensor matching name, or None."""
        for s in self.sensors:
            if s.capability == name:
                return s
        return None

    @staticmethod
    def from_dict(d):
        """Build a Node from its wire dict."""
        return Node(
            id=d["id"],
            kind=d.get("kind", ""),
            device_type=d.get("deviceType", ""),
            access_paths=[AccessPath.from_dict(a) for a in d.get("accessPaths", [])],
            sensors=[Sensor.from_dict(s) for s in d.get("sensors", [])],
        )


@dataclass
class Fragment:
    """A producer's slice of the topology: nodes + relationships + its provider id."""

    provider: str
    nodes: list = field(default_factory=list)
    relationships: list = field(default_factory=list)
    name: str = ""
    version: str = "0.1.0"

    @staticmethod
    def from_dict(d):
        """Build a Fragment from a producer's wire dict."""
        prod = d.get("producer", {})
        return Fragment(
            provider=prod.get("provider", ""),
            name=prod.get("name", ""),
            version=d.get("topologyVersion", "0.1.0"),
            nodes=[Node.from_dict(n) for n in d.get("nodes", [])],
            relationships=list(d.get("relationships", [])),
        )


@dataclass
class SiteGraph:
    """The merged site: one node per physical device, carrying all producers' access paths."""

    nodes: list = field(default_factory=list)
    relationships: list = field(default_factory=list)

    def node(self, node_id) -> Optional[Node]:
        """Return the node with this id, or None."""
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None


def merge_fragments(fragments) -> SiteGraph:
    """Merge producer fragments into one site graph, keyed by node id (serial).

    Same id from multiple producers becomes one node carrying every producer's access paths
    (ranked by preference desc) and the union of its sensors. Distinct ids become sibling nodes.
    Relationships are combined.
    """
    by_id = {}
    order = []
    relationships = []
    for frag in fragments:
        for n in frag.nodes:
            if n.id not in by_id:
                by_id[n.id] = Node(id=n.id, kind=n.kind, device_type=n.device_type, access_paths=list(n.access_paths), sensors=list(n.sensors))
                order.append(n.id)
            else:
                existing = by_id[n.id]
                seen_ap = {ap.id for ap in existing.access_paths}
                existing.access_paths.extend(ap for ap in n.access_paths if ap.id not in seen_ap)
                seen_sensor = {(s.capability, s.access_path) for s in existing.sensors}
                existing.sensors.extend(s for s in n.sensors if (s.capability, s.access_path) not in seen_sensor)
        relationships.extend(frag.relationships)
    for n in by_id.values():
        n.access_paths.sort(key=lambda ap: ap.preference, reverse=True)
    return SiteGraph(nodes=[by_id[i] for i in order], relationships=relationships)


def resolve_sensor(site, capability, node_id):
    """Return the preferred entity for a node's sensor (highest-preference access path), or None.

    When a device is seen via several providers, this picks the sensor on the most-preferred
    available access path. Pure read resolution — no control.
    """
    node = site.node(node_id)
    if node is None:
        return None
    best = None
    best_pref = None
    pref = {ap.id: ap.preference for ap in node.access_paths}
    for s in node.sensors:
        if s.capability != capability or not s.entity:
            continue
        p = pref.get(s.access_path, 0)
        if best is None or p > best_pref:
            best, best_pref = s.entity, p
    return best


def device_fragment(devices, provider, name, transport, preference, locality):
    """Build a read-only producer fragment from plain device data.

    Each device dict needs a `serial` (skipped if missing) and may carry `device_type` and a
    `sensors` list of {capability, unit, entity}. Every device becomes a node on one access path
    advertising those sensors. Pure — no PredBat deps, no control.
    """
    nodes = []
    for dev in devices:
        serial = dev.get("serial")
        if not serial:
            continue
        sensors = [{"capability": s["capability"], "unit": s.get("unit", ""), "entity": s.get("entity", ""), "accessPath": provider} for s in dev.get("sensors", [])]
        nodes.append(
            {
                "id": serial,
                "kind": dev.get("kind", "inverter"),
                "deviceType": str(dev.get("device_type", "")).lower(),
                "accessPaths": [{"id": provider, "provider": provider, "locality": locality, "transport": transport, "preference": preference}],
                "sensors": sensors,
            }
        )
    return {"topologyVersion": "0.1.0", "scope": "fragment", "producer": {"name": name, "provider": provider}, "nodes": nodes, "relationships": []}
