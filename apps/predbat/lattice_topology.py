# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice topology shadow store (read-only)
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Read-only Lattice topology ingestion, merge, and source-reference provenance.

The merged site document remints capability ``ref`` values.  Those refs are not
safe to send to a provider: a control dispatcher must use the ref from the
provider-local source document.  This module therefore keeps an out-of-band
sidecar keyed by ``(node id, capability, access path)``.

There are deliberately no MQTT or Predbat dependencies here.  Control
publishing, planner writes, and inverter writes are outside this module.
"""

import copy
import json
from dataclasses import dataclass
from typing import Optional


SUPPORTED_TOPOLOGY_VERSIONS = frozenset(("0.2", "0.3"))


class TopologyValidationError(ValueError):
    """Raised when an incoming topology document is unsafe to ingest."""


@dataclass(frozen=True)
class SourceCapabilityRef:
    """Provider-local coordinates for one winning capability offer."""

    provider: str
    topology_version: str
    doc_version: int
    cap_ref: int
    node_id: str
    capability: str
    access_path: str


@dataclass(frozen=True)
class TopologySnapshot:
    """An immutable-by-convention merged site and its provenance sidecar."""

    site: dict
    provenance: dict
    warnings: tuple

    def source_ref(self, node_id, capability, access_path=None) -> Optional[SourceCapabilityRef]:
        """Return the winning provider-local ref for a future dispatcher.

        When ``access_path`` is omitted, select the highest-preference merged
        access path that offers the capability.  Exact lookup remains available
        so a dispatcher can follow a resolver's chosen fallback path.
        """
        node_key = str(node_id)
        capability_key = str(capability)
        if access_path is not None:
            return self.provenance.get((node_key, capability_key, str(access_path)))

        node = next((item for item in self.site.get("nodes", []) if str(item.get("id")) == node_key), None)
        if node is None:
            return None
        preferences = {str(path.get("id")): path.get("preference", 0) or 0 for path in node.get("accessPaths", []) if path.get("id") is not None}
        candidates = []
        for key, source in self.provenance.items():
            if key[0] == node_key and key[1] == capability_key:
                candidates.append((-(preferences.get(key[2], 0)), key[2], source))
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2] if candidates else None


def _topology_family(version):
    """Return the supported major/minor family for a topology version."""
    parts = str(version).split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else str(version)


def _authority(document):
    """Return producer authority, excluding bools from Python's integer type."""
    value = (document.get("producer") or {}).get("authority")
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _doc_version(document):
    """Return a validated document version, defaulting legacy 0.2 fragments to zero."""
    value = document.get("docVersion")
    if value is None and _topology_family(document.get("topologyVersion")) == "0.2":
        return 0
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TopologyValidationError("docVersion must be a non-negative integer")
    return value


def _rank(document, order):
    """Build the authority, recency, input-order rank tuple."""
    return (_authority(document), _doc_version(document), -order)


def _better(left, right):
    """Return whether ``left`` outranks ``right``."""
    return left > right


def _winner(contributions):
    """Return the highest-ranked contribution, preserving first on an exact tie."""
    best = contributions[0]
    for contribution in contributions[1:]:
        if _better(contribution[1], best[1]):
            best = contribution
    return best


def _validate_document(document):
    """Validate the minimum shape needed for deterministic safe ingestion."""
    if not isinstance(document, dict):
        raise TopologyValidationError("topology payload must be a JSON object")
    version = document.get("topologyVersion")
    if _topology_family(version) not in SUPPORTED_TOPOLOGY_VERSIONS:
        raise TopologyValidationError("unsupported topologyVersion {!r}".format(version))
    if document.get("scope") not in ("fragment", "overlay", "site"):
        raise TopologyValidationError("scope must be fragment, overlay, or site")
    producer = document.get("producer")
    if not isinstance(producer, dict) or not isinstance(producer.get("provider"), str) or not producer["provider"]:
        raise TopologyValidationError("producer.provider must be a non-empty string")
    _doc_version(document)
    nodes = document.get("nodes", [])
    if not isinstance(nodes, list):
        raise TopologyValidationError("nodes must be an array")
    for node in nodes:
        if not isinstance(node, dict) or node.get("id") is None:
            raise TopologyValidationError("every node must be an object with an id")
        for field in ("accessPaths", "capabilities"):
            if field in node and not isinstance(node[field], list):
                raise TopologyValidationError("node {} {} must be an array".format(node["id"], field))
    return document


def decode_topology(payload):
    """Decode and validate a retained MQTT topology payload."""
    if isinstance(payload, dict):
        document = copy.deepcopy(payload)
    else:
        if isinstance(payload, bytes):
            try:
                payload = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise TopologyValidationError("topology payload is not UTF-8") from exc
        if not isinstance(payload, str) or not payload.strip():
            raise TopologyValidationError("topology payload is empty")
        try:
            document = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise TopologyValidationError("topology payload is malformed JSON") from exc
    return _validate_document(document)


def _collection_winners(contributions, field, key_function):
    """Merge an identity-keyed collection and retain each source document."""
    order = []
    values = {}
    for item, rank, document in contributions:
        entries = item.get(field, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            key = key_function(entry)
            if key is None:
                continue
            if key not in values:
                order.append(key)
            current = values.get(key)
            if current is None or _better(rank, current[1]):
                values[key] = (entry, rank, document)
    return [(key, values[key]) for key in order if values[key][0].get("removed") is not True]


def _offer_key(offer):
    """Return a capability-offer identity."""
    if offer.get("capability") is None:
        return None
    return (str(offer["capability"]), str(offer.get("accessPath", "")))


def _pick_field(contributions, field, node_id, warnings):
    """Choose a scalar field by authority, recency, then input order."""
    setters = [(item[field], rank) for item, rank, _document in contributions if field in item]
    if not setters:
        return None
    value, rank = _winner(setters)
    for other_value, other_rank in setters:
        if other_rank[:2] == rank[:2] and other_value != value:
            warnings.append('node "{}" field "{}" tied; kept first input'.format(node_id, field))
            break
    return copy.deepcopy(value)


def _merge_bag(contributions, field):
    """Merge an attributes/parameters bag per key."""
    values = {}
    for item, rank, _document in contributions:
        bag = item.get(field)
        if not isinstance(bag, dict):
            continue
        for key, value in bag.items():
            if key not in values or _better(rank, values[key][1]):
                values[key] = (copy.deepcopy(value), rank)
    return {key: value for key, (value, _rank_value) in values.items()}


def _source_ref(document, node_id, offer):
    """Build source coordinates for a winning capability offer."""
    source_ref = offer.get("ref")
    if not isinstance(source_ref, int) or isinstance(source_ref, bool) or source_ref <= 0:
        return None
    return SourceCapabilityRef(
        provider=document["producer"]["provider"],
        topology_version=document["topologyVersion"],
        doc_version=_doc_version(document),
        cap_ref=source_ref,
        node_id=str(node_id),
        capability=str(offer["capability"]),
        access_path=str(offer.get("accessPath", "")),
    )


def _merge_node(contributions, warnings):
    """Merge one surviving node and return its source-offer sidecar."""
    winner = _winner([(item, rank) for item, rank, _document in contributions])[0]
    node_id = str(winner["id"])
    node = {"id": copy.deepcopy(winner["id"])}
    for field in ("kind", "deviceType", "aggregate"):
        value = _pick_field(contributions, field, node_id, warnings)
        if value is not None:
            node[field] = value
    for field in ("attributes", "parameters"):
        bag = _merge_bag(contributions, field)
        if bag:
            node[field] = bag

    path_winners = _collection_winners(contributions, "accessPaths", lambda path: str(path["id"]) if path.get("id") is not None else None)
    paths = [copy.deepcopy(value[0]) for _key, value in path_winners]
    paths.sort(key=lambda path: (-(path.get("preference", 0) or 0), str(path.get("id"))))
    if paths:
        node["accessPaths"] = paths

    offer_winners = _collection_winners(contributions, "capabilities", _offer_key)
    offers = []
    provenance = {}
    for key, (offer, _rank_value, document) in offer_winners:
        merged_offer = {name: copy.deepcopy(value) for name, value in offer.items() if name not in ("removed", "ref")}
        offers.append(merged_offer)
        source = _source_ref(document, node_id, offer)
        if source is not None:
            provenance[(node_id, key[0], key[1])] = source
        else:
            warnings.append('node "{}" capability "{}" on "{}" has no provider-local ref'.format(node_id, key[0], key[1]))
    if offers:
        node["capabilities"] = offers
    return node, provenance


def _digest(document):
    """Return a deterministic positive 31-bit digest for merged docVersion."""
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    value = 0x811C9DC5
    for character in encoded:
        value ^= ord(character)
        value = (value * 0x01000193) & 0xFFFFFFFF
    return (value % 2147483647) + 1


def merge_topologies(documents):
    """Authority-merge topology documents while retaining local capability refs."""
    documents = [copy.deepcopy(_validate_document(document)) for document in documents]
    if not documents:
        raise TopologyValidationError("cannot merge an empty topology set")

    ranked_documents = [(document, _rank(document, order)) for order, document in enumerate(documents)]
    top_document = _winner(ranked_documents)[0]
    warnings = []

    node_order = []
    node_contributions = {}
    for document, rank in ranked_documents:
        for node in document.get("nodes", []):
            key = str(node["id"])
            if key not in node_contributions:
                node_order.append(key)
                node_contributions[key] = []
            node_contributions[key].append((node, rank, document))

    nodes = []
    provenance = {}
    surviving = set()
    for node_id in node_order:
        contributions = node_contributions[node_id]
        if _winner([(item, rank) for item, rank, _document in contributions])[0].get("removed") is True:
            continue
        tombstones = [rank for item, rank, _document in contributions if item.get("removed") is True]
        barrier = max(tombstones) if tombstones else None
        live = [(item, rank, document) for item, rank, document in contributions if item.get("removed") is not True and (barrier is None or _better(rank, barrier) or rank == barrier)]
        node, node_provenance = _merge_node(live, warnings)
        nodes.append(node)
        provenance.update(node_provenance)
        surviving.add(node_id)

    relationship_contributions = {}
    relationship_order = []
    for document, rank in ranked_documents:
        for relationship in document.get("relationships", []):
            if not isinstance(relationship, dict) or any(relationship.get(field) is None for field in ("from", "to", "type")):
                continue
            key = (str(relationship["from"]), str(relationship["to"]), str(relationship["type"]))
            if key not in relationship_contributions:
                relationship_order.append(key)
                relationship_contributions[key] = []
            relationship_contributions[key].append((relationship, rank))
    relationships = []
    for key in relationship_order:
        relationship = _winner(relationship_contributions[key])[0]
        if relationship.get("removed") is True:
            continue
        if key[0] not in surviving or key[1] not in surviving:
            warnings.append("relationship {} dropped because an endpoint is absent".format("|".join(key)))
            continue
        relationships.append({name: copy.deepcopy(value) for name, value in relationship.items() if name != "removed"})

    next_ref = 1
    for node in nodes:
        refs = {}
        for offer in node.get("capabilities", []):
            capability = str(offer.get("capability"))
            if capability not in refs:
                refs[capability] = next_ref
                next_ref += 1
            offer["ref"] = refs[capability]

    site = {
        "topologyVersion": top_document["topologyVersion"],
        "scope": "site",
        "producer": {
            "name": "predbat-lattice-shadow",
            "provider": "predbat",
            "inputs": [
                {
                    "name": document.get("producer", {}).get("name"),
                    "provider": document["producer"]["provider"],
                    "authority": _authority(document),
                    "docVersion": _doc_version(document),
                    "topologyVersion": document["topologyVersion"],
                }
                for document, _rank_value in ranked_documents
            ],
        },
        "nodes": nodes,
    }
    if relationships:
        site["relationships"] = relationships
    site["docVersion"] = _digest(site)
    return TopologySnapshot(site=site, provenance=provenance, warnings=tuple(warnings))


class LatticeTopologyStore:
    """Replace provider documents and expose the current merged shadow snapshot."""

    def __init__(self):
        """Create an empty provider document set."""
        self._documents = {}
        self.snapshot = None

    def ingest(self, payload):
        """Ingest a provider document, replacing only with a newer version.

        Returns ``True`` when the current snapshot changes.  Stale or duplicate
        documents are ignored.  Invalid documents raise
        :class:`TopologyValidationError` and leave the prior snapshot intact.
        """
        document = decode_topology(payload)
        provider = document["producer"]["provider"]
        previous = self._documents.get(provider)
        if previous is not None:
            incoming_version = _doc_version(document)
            previous_version = _doc_version(previous)
            if incoming_version < previous_version:
                return False
            if incoming_version == previous_version:
                if document == previous:
                    return False
                # Current gateway 0.2 retained fragments predate docVersion. Arrival
                # order is their only replacement signal; explicit versions remain
                # protected against reuse with different content.
                if "docVersion" in document or "docVersion" in previous:
                    raise TopologyValidationError("provider {} reused docVersion {} for different content".format(provider, incoming_version))
        candidate_documents = dict(self._documents)
        candidate_documents[provider] = document
        snapshot = merge_topologies(candidate_documents.values())
        self._documents = candidate_documents
        self.snapshot = snapshot
        return True

    def source_ref(self, node_id, capability, access_path=None) -> Optional[SourceCapabilityRef]:
        """Expose provider-local capability coordinates for a future dispatcher."""
        if self.snapshot is None:
            return None
        return self.snapshot.source_ref(node_id, capability, access_path)
