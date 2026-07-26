"""Tests for read-only Lattice topology ingestion and provenance."""

import ast
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_topology import LatticeTopologyStore, TopologyValidationError, merge_topologies


def topology(provider, doc_version, authority=0, topology_version="0.3.0", kind="inverter", capability="battery.target_soc", access_path=None, cap_ref=1):
    """Build a compact provider-local topology document."""
    access_path = access_path or "{}-local".format(provider)
    return {
        "topologyVersion": topology_version,
        "scope": "fragment",
        "docVersion": doc_version,
        "producer": {"name": provider, "provider": provider, "authority": authority},
        "nodes": [
            {
                "id": "INV1",
                "kind": kind,
                "accessPaths": [{"id": access_path, "provider": provider, "preference": authority}],
                "capabilities": [
                    {
                        "capability": capability,
                        "accessPath": access_path,
                        "ref": cap_ref,
                        "shape": "setpoint",
                        "control": {"protocol": "mqtt"},
                    }
                ],
            }
        ],
    }


class TestTopologyReplacement(unittest.TestCase):
    """Provider documents replace atomically by docVersion."""

    def test_newer_replaces_and_stale_is_ignored(self):
        """A newer retained provider document replaces its predecessor."""
        store = LatticeTopologyStore()
        self.assertTrue(store.ingest(topology("gateway", 1, cap_ref=11)))
        first_doc_version = store.snapshot.site["docVersion"]
        self.assertTrue(store.ingest(topology("gateway", 2, cap_ref=12)))
        self.assertEqual(store.source_ref("INV1", "battery.target_soc").cap_ref, 12)
        self.assertNotEqual(store.snapshot.site["docVersion"], first_doc_version)
        self.assertFalse(store.ingest(topology("gateway", 1, cap_ref=99)))
        self.assertEqual(store.source_ref("INV1", "battery.target_soc").cap_ref, 12)

    def test_duplicate_is_ignored_and_reused_version_is_rejected(self):
        """A docVersion cannot name two different documents from one provider."""
        store = LatticeTopologyStore()
        document = topology("gateway", 4, cap_ref=10)
        self.assertTrue(store.ingest(document))
        self.assertFalse(store.ingest(json.dumps(document).encode()))
        with self.assertRaises(TopologyValidationError):
            store.ingest(topology("gateway", 4, cap_ref=20))

    def test_malformed_document_preserves_previous_snapshot(self):
        """Malformed retained data cannot erase the last known-good topology."""
        store = LatticeTopologyStore()
        store.ingest(topology("gateway", 1, cap_ref=11))
        before = store.snapshot
        with self.assertRaises(TopologyValidationError):
            store.ingest(b"{broken")
        self.assertIs(store.snapshot, before)

    def test_gateway_02_without_doc_version_replaces_by_arrival(self):
        """Current gateway 0.2 fragments work before firmware adds docVersion."""
        store = LatticeTopologyStore()
        first = topology("predbat-gateway", 1, topology_version="0.2.0", cap_ref=11)
        second = topology("predbat-gateway", 1, topology_version="0.2.0", cap_ref=12)
        del first["docVersion"]
        del second["docVersion"]
        self.assertTrue(store.ingest(first))
        self.assertTrue(store.ingest(second))
        source = store.source_ref("INV1", "battery.target_soc")
        self.assertEqual(source.doc_version, 0)
        self.assertEqual(source.cap_ref, 12)


class TestAuthorityMerge(unittest.TestCase):
    """Authority, recency, and stable input order choose deterministic winners."""

    def test_authority_then_recency_select_winner(self):
        """Higher authority wins before docVersion; recency breaks authority ties."""
        low_new = topology("cloud", 99, authority=1, kind="battery", cap_ref=90)
        high_old = topology("gateway", 1, authority=10, kind="inverter", cap_ref=10)
        result = merge_topologies([low_new, high_old])
        self.assertEqual(result.site["nodes"][0]["kind"], "inverter")

        newer = topology("installer", 2, authority=10, kind="hybrid-inverter", access_path="installer", cap_ref=20)
        result = merge_topologies([high_old, newer])
        self.assertEqual(result.site["nodes"][0]["kind"], "hybrid-inverter")

    def test_exact_tie_keeps_first_and_warns(self):
        """Input order is the stable final tiebreak for scalar conflicts."""
        first = topology("a", 1, authority=5, kind="inverter", cap_ref=1)
        second = topology("b", 1, authority=5, kind="battery", cap_ref=2)
        result = merge_topologies([first, second])
        self.assertEqual(result.site["nodes"][0]["kind"], "inverter")
        self.assertTrue(any("tied" in warning for warning in result.warnings))

    def test_reminted_refs_map_to_provider_local_refs(self):
        """Merged refs are independent while the sidecar retains each local ref."""
        gateway = topology("gateway", 7, authority=10, access_path="gw", cap_ref=41)
        cloud = topology("cloud", 3, authority=1, access_path="cloud", cap_ref=900)
        result = merge_topologies([gateway, cloud])
        offers = result.site["nodes"][0]["capabilities"]
        self.assertEqual({offer["ref"] for offer in offers}, {1})
        self.assertEqual(result.source_ref("INV1", "battery.target_soc", "gw").cap_ref, 41)
        self.assertEqual(result.source_ref("INV1", "battery.target_soc", "cloud").cap_ref, 900)
        self.assertEqual(result.source_ref("INV1", "battery.target_soc").provider, "gateway")

    def test_mixed_02_and_03_providers_retain_source_versions(self):
        """Compatible 0.x providers keep their individual topology versions."""
        old = topology("cloud", 1, authority=1, topology_version="0.2.0", access_path="cloud", cap_ref=2)
        current = topology("gateway", 2, authority=10, topology_version="0.3.0", access_path="gw", cap_ref=3)
        result = merge_topologies([old, current])
        self.assertEqual(result.site["topologyVersion"], "0.3.0")
        self.assertEqual(result.source_ref("INV1", "battery.target_soc", "cloud").topology_version, "0.2.0")
        self.assertEqual(result.source_ref("INV1", "battery.target_soc", "gw").topology_version, "0.3.0")

    def test_capability_and_node_tombstones_remove_provenance(self):
        """Winning tombstones remove merged offers/nodes and their sidecar entries."""
        discovered = topology("gateway", 1, authority=0, access_path="gw", cap_ref=3)
        capability_tombstone = {
            "topologyVersion": "0.3.0",
            "scope": "overlay",
            "docVersion": 1,
            "producer": {"name": "installer", "provider": "installer", "authority": 50},
            "nodes": [{"id": "INV1", "capabilities": [{"capability": "battery.target_soc", "accessPath": "gw", "removed": True}]}],
        }
        result = merge_topologies([discovered, capability_tombstone])
        self.assertNotIn("capabilities", result.site["nodes"][0])
        self.assertIsNone(result.source_ref("INV1", "battery.target_soc", "gw"))

        node_tombstone = {
            "topologyVersion": "0.3.0",
            "scope": "overlay",
            "docVersion": 2,
            "producer": {"name": "installer", "provider": "installer", "authority": 50},
            "nodes": [{"id": "INV1", "removed": True}],
        }
        result = merge_topologies([discovered, node_tombstone])
        self.assertEqual(result.site["nodes"], [])
        self.assertEqual(result.provenance, {})

    def test_node_tombstone_exact_tie_respects_stable_input_order(self):
        """Equal-rank live/tombstone conflicts keep the first contribution."""
        live = topology("gateway", 1, authority=10, cap_ref=3)
        tombstone = {
            "topologyVersion": "0.3.0",
            "scope": "overlay",
            "docVersion": 1,
            "producer": {"name": "installer", "provider": "installer", "authority": 10},
            "nodes": [{"id": "INV1", "removed": True}],
        }

        live_first = merge_topologies([live, tombstone])
        self.assertEqual([node["id"] for node in live_first.site["nodes"]], ["INV1"])
        self.assertEqual(live_first.source_ref("INV1", "battery.target_soc").cap_ref, 3)

        tombstone_first = merge_topologies([tombstone, live])
        self.assertEqual(tombstone_first.site["nodes"], [])
        self.assertEqual(tombstone_first.provenance, {})


class TestGatewayFeatureFlag(unittest.TestCase):
    """Gateway topology behavior is absent unless explicitly enabled."""

    def test_feature_flag_defaults_off(self):
        """Default initialization does not expose a dispatcher binding."""
        gateway_path = os.path.join(os.path.dirname(__file__), "..", "gateway.py")
        with open(gateway_path, "r", encoding="utf-8") as gateway_file:
            module = ast.parse(gateway_file.read())
        gateway_class = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "GatewayMQTT")
        initialize = next(node for node in gateway_class.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "initialize")
        defaults = dict(zip([argument.arg for argument in initialize.args.args[-len(initialize.args.defaults) :]], initialize.args.defaults))
        self.assertIs(defaults["lattice_projection_enable"].value, False)


if __name__ == "__main__":
    unittest.main()
