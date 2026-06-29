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
import json as _json
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


def _authority_of(doc):
    """Producer authority (default 0), the primary merge precedence; bools are not authorities."""
    a = doc.get("producer", {}).get("authority") if isinstance(doc, dict) else None
    return a if isinstance(a, int) and not isinstance(a, bool) else 0


def _recency_of(doc):
    """Document docVersion (default 0), the recency tiebreak."""
    v = doc.get("docVersion") if isinstance(doc, dict) else None
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _major_of(version):
    """Major component of a semver-ish topologyVersion string."""
    return str(version if version is not None else "").split(".")[0]


def _better(a, b):
    """True if rank a outranks rank b: higher authority, then higher recency, then earlier order."""
    if a[0] != b[0]:
        return a[0] > b[0]
    if a[1] != b[1]:
        return a[1] > b[1]
    return a[2] < b[2]


def _top_by(contribs):
    """Return the highest-ranked (item, rank) contributor."""
    best = contribs[0]
    for cur in contribs[1:]:
        if _better(cur[1], best[1]):
            best = cur
    return best


def _pick_field(contribs, field, node_id, warnings):
    """Highest-ranked setter of a scalar/object field; warn on equal-precedence conflicts."""
    best = None
    best_rank = None
    found = False
    for item, rank in contribs:
        if field not in item:
            continue
        if not found or _better(rank, best_rank):
            best = item[field]
            best_rank = rank
            found = True
    if not found:
        return None
    for item, rank in contribs:
        if field not in item:
            continue
        if rank[0] == best_rank[0] and rank[1] == best_rank[1] and item[field] != best:
            warnings.append('node "{}" field "{}": conflicting values at equal precedence; kept first'.format(node_id, field))
            break
    return best


def _merge_bag(contribs, field):
    """Per-key bag merge (attributes/parameters): each key from its highest-ranked setter."""
    by_key = {}
    for item, rank in contribs:
        bag = item.get(field)
        if not isinstance(bag, dict):
            continue
        for k, v in bag.items():
            if k not in by_key or _better(rank, by_key[k][1]):
                by_key[k] = (v, rank)
    if not by_key:
        return None
    return {k: v for k, (v, _r) in by_key.items()}


def _merge_collection(contribs, field, key_of):
    """Identity-keyed collection union; highest-ranked entry per key wins; tombstones omitted."""
    order = []
    by_key = {}
    for item, rank in contribs:
        entries = item.get(field)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            key = key_of(entry)
            if key is None:
                continue
            if key not in by_key:
                order.append(key)
            if key not in by_key or _better(rank, by_key[key][1]):
                by_key[key] = (entry, rank)
    out = []
    for key in order:
        entry = by_key[key][0]
        if entry.get("removed") is True:
            continue
        out.append({k: v for k, v in entry.items() if k != "removed"})
    return out


def _offer_key(offer):
    """Offer identity (capability, accessPath); a derived offer keys on capability alone."""
    if offer.get("capability") is None:
        return None
    ap = offer.get("accessPath")
    return "{}|{}".format(offer["capability"], "" if ap is None else ap)


def _digest(obj):
    """Deterministic positive-integer content digest (FNV-1a, 31-bit) over canonical JSON."""
    s = _json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    h = 0x811C9DC5
    for ch in s:
        h ^= ord(ch)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return (h % 2147483647) + 1


def _merge_node(contribs, warnings):
    """Build one merged node from its (non-tombstone) contributors."""
    top = _top_by(contribs)
    node_id = str(top[0]["id"])
    node = {"id": top[0]["id"], "kind": _pick_field(contribs, "kind", node_id, warnings)}
    device_type = _pick_field(contribs, "deviceType", node_id, warnings)
    if device_type is not None:
        node["deviceType"] = device_type
    aggregate = _pick_field(contribs, "aggregate", node_id, warnings)
    if aggregate is not None:
        node["aggregate"] = aggregate
    attributes = _merge_bag(contribs, "attributes")
    if attributes:
        node["attributes"] = attributes
    parameters = _merge_bag(contribs, "parameters")
    if parameters:
        node["parameters"] = parameters
    access_paths = _merge_collection(contribs, "accessPaths", lambda ap: str(ap["id"]) if ap.get("id") is not None else None)
    access_paths.sort(key=lambda ap: (-(ap.get("preference") or 0), str(ap.get("id"))))
    if access_paths:
        node["accessPaths"] = access_paths
    capabilities = _merge_collection(contribs, "capabilities", _offer_key)
    if capabilities:
        node["capabilities"] = capabilities
    return node


def merge(docs):
    """Compose producer fragments + upstream overlays into one authority-ranked site doc.

    Pure mirror of the lattice-spec reference (editor/src/merge-engine.ts), pinned by the
    conformance/merge/ corpus. Returns {"site": <site doc>, "warnings": [str]}. Raises ValueError
    on an empty input list or a mismatched topologyVersion major. docVersion is a content digest
    (implementation-defined; normalised out of the cross-language comparison).
    """
    warnings = []
    inputs = [d for d in (docs or []) if isinstance(d, dict)]
    if not inputs:
        raise ValueError("cannot merge an empty document list")
    majors = {_major_of(d.get("topologyVersion")) for d in inputs}
    if len(majors) > 1:
        raise ValueError("cannot merge incompatible topologyVersion majors: " + ", ".join(sorted(majors)))

    ranked = [(doc, (_authority_of(doc), _recency_of(doc), order)) for order, doc in enumerate(inputs)]
    top = _top_by(ranked)

    node_order = []
    node_contribs = {}
    for doc, rank in ranked:
        for n in doc.get("nodes") or []:
            if not isinstance(n, dict) or n.get("id") is None:
                continue
            nid = str(n["id"])
            if nid not in node_contribs:
                node_contribs[nid] = []
                node_order.append(nid)
            node_contribs[nid].append((n, rank))

    surviving = set()
    merged_nodes = []
    for nid in node_order:
        contribs = node_contribs[nid]
        if _top_by(contribs)[0].get("removed") is True:
            continue
        surviving.add(nid)
        merged_nodes.append(_merge_node([(it, rk) for it, rk in contribs if it.get("removed") is not True], warnings))

    rel_order = []
    rel_contribs = {}
    for doc, rank in ranked:
        for rel in doc.get("relationships") or []:
            if not isinstance(rel, dict) or rel.get("from") is None or rel.get("to") is None or rel.get("type") is None:
                continue
            key = "{}|{}|{}".format(rel["from"], rel["to"], rel["type"])
            if key not in rel_contribs:
                rel_contribs[key] = []
                rel_order.append(key)
            rel_contribs[key].append((rel, rank))
    merged_rels = []
    for key in rel_order:
        winner = _top_by(rel_contribs[key])[0]
        if winner.get("removed") is True:
            continue
        if str(winner["from"]) not in surviving or str(winner["to"]) not in surviving:
            warnings.append("relationship {} dropped: endpoint not in merged node set".format(key))
            continue
        merged_rels.append({k: v for k, v in winner.items() if k != "removed"})

    next_ref = 1
    for node in merged_nodes:
        caps = node.get("capabilities")
        if not isinstance(caps, list):
            continue
        ref_by_cap = {}
        for offer in caps:
            cap = str(offer.get("capability"))
            if cap not in ref_by_cap:
                ref_by_cap[cap] = next_ref
                next_ref += 1
            offer["ref"] = ref_by_cap[cap]

    device_types = _merge_collection(ranked, "deviceTypes", lambda dt: str(dt["key"]) if dt.get("key") is not None else None)

    top_tv = top[0].get("topologyVersion")
    site = {
        "topologyVersion": "0.1.0" if top_tv is None else top_tv,
        "scope": "site",
        "producer": {
            "name": "lattice-merge",
            "provider": "lattice-merge",
            "inputs": [{"name": d.get("producer", {}).get("name"), "provider": d.get("producer", {}).get("provider"), "authority": _authority_of(d), "docVersion": _recency_of(d)} for d, _r in ranked],
        },
        "nodes": merged_nodes,
    }
    if device_types:
        site["deviceTypes"] = device_types
    id_contribs = [(d, r) for d, r in ranked if d.get("id") is not None]
    if id_contribs:
        site["id"] = _top_by(id_contribs)[0]["id"]
    if merged_rels:
        site["relationships"] = merged_rels
    site["docVersion"] = _digest(site)
    return {"site": site, "warnings": warnings}
