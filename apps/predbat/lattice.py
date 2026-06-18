# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice projection core
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Pure, dependency-free Lattice model + merge + resolve.

Mirrors the gateway C++ descriptor engine on the cloud side: typed fragments
are merged by identity into one site graph, and reads/controls resolve over
ranked access paths (prefer local gateway, fall back to vendor cloud). No
PredBat or Home Assistant dependencies so it can be unit-tested standalone.
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
class Capability:
    """A read/control affordance on a node, served on a named access path."""

    capability: str
    unit: str = ""
    read: bool = False
    control: bool = False
    access_path: str = ""
    constraints: dict = field(default_factory=dict)

    @staticmethod
    def from_dict(d):
        """Build a Capability from its wire dict."""
        return Capability(
            capability=d["capability"],
            unit=d.get("unit", ""),
            read=bool(d.get("read", False)),
            control=bool(d.get("control", False)),
            access_path=d.get("accessPath", ""),
            constraints=dict(d.get("constraints", {})),
        )


@dataclass
class Node:
    """A device in the graph, identified by id (serial), with access paths + capabilities."""

    id: str
    kind: str
    device_type: str
    access_paths: list = field(default_factory=list)
    capabilities: list = field(default_factory=list)

    def capability(self, name) -> Optional[Capability]:
        """Return the first Capability matching name, or None."""
        for c in self.capabilities:
            if c.capability == name:
                return c
        return None

    @staticmethod
    def from_dict(d):
        """Build a Node from its wire dict."""
        return Node(
            id=d["id"],
            kind=d.get("kind", ""),
            device_type=d.get("deviceType", ""),
            access_paths=[AccessPath.from_dict(a) for a in d.get("accessPaths", [])],
            capabilities=[Capability.from_dict(c) for c in d.get("capabilities", [])],
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

    Same id from multiple producers becomes one node carrying every producer's
    access paths (ranked by preference desc) and the union of its capabilities.
    Distinct ids become sibling nodes. Relationships are combined.
    """
    by_id = {}
    order = []
    relationships = []
    for frag in fragments:
        for n in frag.nodes:
            if n.id not in by_id:
                by_id[n.id] = Node(id=n.id, kind=n.kind, device_type=n.device_type, access_paths=list(n.access_paths), capabilities=list(n.capabilities))
                order.append(n.id)
            else:
                existing = by_id[n.id]
                seen_ap = {ap.id for ap in existing.access_paths}
                existing.access_paths.extend(ap for ap in n.access_paths if ap.id not in seen_ap)
                seen_cap = {(c.capability, c.access_path) for c in existing.capabilities}
                existing.capabilities.extend(c for c in n.capabilities if (c.capability, c.access_path) not in seen_cap)
        relationships.extend(frag.relationships)
    for n in by_id.values():
        n.access_paths.sort(key=lambda ap: ap.preference, reverse=True)
    return SiteGraph(nodes=[by_id[i] for i in order], relationships=relationships)


@dataclass
class ResolveResult:
    """Outcome of a resolve: which provider/access path, the (clamped) value, and ok flag."""

    ok: bool = False
    provider: str = ""
    access_path: str = ""
    value: Optional[int] = None
    reason: str = ""


def _pick_path(node, cap, available, need_control):
    """Pick the highest-preference access path that is available and serves cap (read/control)."""
    for ap in node.access_paths:  # already preference-desc from merge
        served = next((x for x in node.capabilities if x.capability == cap.capability and x.access_path == ap.id and (x.control if need_control else x.read)), None)
        if served is not None and ap.provider in available:
            return ap, served
    return None, None


def _clamp(value, constraints):
    """Clamp value to constraints' min/max when present; tolerate malformed (non-comparable) bounds."""
    if value is None:
        return None
    low = constraints.get("min")
    high = constraints.get("max")
    try:
        if low is not None and value < low:
            value = low
        if high is not None and value > high:
            value = high
    except TypeError:
        return value  # malformed constraints (e.g. string bounds) -> leave unclamped rather than raise
    return value


def resolve_control(site, capability, node_id, value, available):
    """Resolve a control intent: pick the best available access path and clamp the value.

    `available` is the set of provider ids currently reachable (liveness). Returns a
    ResolveResult; ok is False when no available provider can control the capability.
    """
    node = site.node(node_id)
    if node is None:
        return ResolveResult(reason="no such node")
    cap = node.capability(capability)
    if cap is None:
        return ResolveResult(reason="capability not offered")
    ap, served = _pick_path(node, cap, available, need_control=True)
    if ap is None:
        return ResolveResult(reason="no available control path")
    return ResolveResult(ok=True, provider=ap.provider, access_path=ap.id, value=_clamp(value, served.constraints))


def resolve_read(site, capability, node_id, available):
    """Resolve a read: pick the best available access path that can read the capability."""
    node = site.node(node_id)
    if node is None:
        return ResolveResult(reason="no such node")
    cap = node.capability(capability)
    if cap is None:
        return ResolveResult(reason="capability not offered")
    ap, served = _pick_path(node, cap, available, need_control=False)
    if ap is None:
        return ResolveResult(reason="no available read path")
    return ResolveResult(ok=True, provider=ap.provider, access_path=ap.id)


def control_candidates(site, capability, node_id, value, available):
    """Return ranked (provider, access_path_id, clamped_value) candidates for a control intent.

    Highest-preference first, one entry per available access path that can control the
    capability, with `value` clamped to that path's own constraints. Used by callers that
    want to try providers in order and fall back on execution failure (not just availability).
    """
    node = site.node(node_id)
    if node is None:
        return []
    cap = node.capability(capability)
    if cap is None:
        return []
    candidates = []
    for ap in node.access_paths:  # preference-desc from merge
        served = next((x for x in node.capabilities if x.capability == capability and x.access_path == ap.id and x.control), None)
        if served is not None and ap.provider in available:
            candidates.append((ap.provider, ap.id, _clamp(value, served.constraints)))
    return candidates


# The capabilities a battery inverter offers, as data. Each spec: name, read/control, unit, and
# how to bound it ("rated" => 0..rated_w for power; a fixed (min,max) for percentages).
INVERTER_CAPS = (
    {"name": "charge_rate", "read": True, "control": True, "unit": "W", "max": "rated"},
    {"name": "discharge_rate", "read": True, "control": True, "unit": "W", "max": "rated"},
    {"name": "target_soc", "read": True, "control": True, "unit": "%", "min": 0, "max": 100},
    {"name": "reserve_soc", "read": True, "control": True, "unit": "%", "min": 0, "max": 100},
    {"name": "soc", "read": True, "control": False, "unit": "%"},
)


def _cap_constraints(spec, rated):
    """Resolve a capability spec's constraints against an inverter's rated power."""
    constraints = {}
    low = spec.get("min")
    if low is not None:
        constraints["min"] = low
    high = spec.get("max")
    if high == "rated":
        constraints["min"] = constraints.get("min", 0)
        if rated > 0:
            constraints["max"] = rated
    elif isinstance(high, int):
        constraints["max"] = high
    return constraints


def inverter_fragment(inverters, provider, name, transport, preference, locality, cap_specs=INVERTER_CAPS, controllable=None):
    """Build a producer fragment from plain inverter data.

    Each inverter dict needs a `serial` (skipped if missing) and may carry `device_type`
    and `rated_w` (used for "rated" power ceilings). Every inverter becomes a node offering
    the given capability specs on one access path. Pure — no PredBat deps.

    `controllable` is the set of capability names this provider can ACTUALLY execute via its
    `lattice_control`. A capability is marked `control` only if its spec allows control AND it
    is in `controllable` — so the fragment never over-promises (a provider with no executor must
    pass `()` to stay read-only). `None` (default) keeps every control-capable spec controllable.
    """
    nodes = []
    for inv in inverters:
        serial = inv.get("serial")
        if not serial:
            continue
        rated = int(inv.get("rated_w", 0) or 0)
        caps = []
        for spec in cap_specs:
            can_control = bool(spec.get("control", False))
            if controllable is not None:
                can_control = can_control and spec["name"] in controllable
            caps.append(
                {
                    "capability": spec["name"],
                    "unit": spec.get("unit", ""),
                    "read": bool(spec.get("read", False)),
                    "control": can_control,
                    "accessPath": provider,
                    "constraints": _cap_constraints(spec, rated),
                }
            )
        nodes.append(
            {
                "id": serial,
                "kind": "inverter",
                "deviceType": str(inv.get("device_type", "")).lower(),
                "accessPaths": [{"id": provider, "provider": provider, "locality": locality, "transport": transport, "preference": preference}],
                "capabilities": caps,
            }
        )
    return {"topologyVersion": "0.1.0", "scope": "fragment", "producer": {"name": name, "provider": provider}, "nodes": nodes, "relationships": []}
