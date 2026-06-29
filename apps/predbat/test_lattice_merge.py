# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice authority-ranked merge unit tests
# -----------------------------------------------------------------------------
"""Unit tests for the authority-ranked Lattice merge (pure; mirrors the lattice-spec reference)."""
import unittest

from lattice import merge


def _frag(**over):
    """Build a minimal fragment dict with sensible defaults, overridable per test."""
    base = {"topologyVersion": "0.1.0", "scope": "fragment", "producer": {"name": "p", "provider": "x", "authority": 0}, "nodes": []}
    base.update(over)
    return base


class TestMergeRules(unittest.TestCase):
    """The authority-ranked merge obeys precedence, override modes, tombstones and provenance."""

    def test_union_ranks_access_paths_and_unions_caps(self):
        """Same node via two peer providers becomes one node with ranked paths and unioned caps."""
        a = _frag(
            producer={"name": "gw", "provider": "gw", "authority": 0},
            docVersion=1,
            nodes=[{"id": "INV", "kind": "inverter", "accessPaths": [{"id": "gw-local", "provider": "gw", "preference": 10}], "capabilities": [{"capability": "battery.soc", "accessPath": "gw-local", "read": {}}]}],
        )
        b = _frag(
            producer={"name": "cloud", "provider": "cloud", "authority": 0},
            docVersion=1,
            nodes=[
                {
                    "id": "INV",
                    "kind": "inverter",
                    "accessPaths": [{"id": "vendor-cloud", "provider": "cloud", "preference": 1}],
                    "capabilities": [{"capability": "battery.soc", "accessPath": "vendor-cloud", "read": {}}, {"capability": "battery.power", "accessPath": "vendor-cloud", "read": {}}],
                }
            ],
        )
        site = merge([a, b])["site"]
        node = site["nodes"][0]
        self.assertEqual([ap["id"] for ap in node["accessPaths"]], ["gw-local", "vendor-cloud"])
        self.assertEqual([o["capability"] for o in node["capabilities"]], ["battery.soc", "battery.soc", "battery.power"])
        self.assertEqual(node["capabilities"][0]["ref"], node["capabilities"][1]["ref"])
        self.assertNotEqual(node["capabilities"][0]["ref"], node["capabilities"][2]["ref"])

    def test_override_is_per_field(self):
        """Higher authority wins a conflicting scalar; discovered fields it omits survive."""
        disc = _frag(nodes=[{"id": "N", "kind": "inverter", "attributes": {"ratedW": 5000}}])
        over = _frag(producer={"name": "i", "provider": "installer", "authority": 50}, nodes=[{"id": "N", "kind": "gateway"}])
        site = merge([disc, over])["site"]
        self.assertEqual(site["nodes"][0]["kind"], "gateway")
        self.assertEqual(site["nodes"][0]["attributes"]["ratedW"], 5000)

    def test_attributes_merge_per_key(self):
        """Bag fields merge per key by authority."""
        disc = _frag(nodes=[{"id": "N", "kind": "inverter", "attributes": {"ratedW": 5000, "phase": 1}}])
        over = _frag(producer={"name": "i", "provider": "installer", "authority": 50}, nodes=[{"id": "N", "kind": "inverter", "attributes": {"phase": 3}}])
        self.assertEqual(merge([disc, over])["site"]["nodes"][0]["attributes"], {"ratedW": 5000, "phase": 3})

    def test_aggregate_overridden_wholesale(self):
        """Aggregate is a cohesive object overridden wholesale by the highest authority."""
        disc = _frag(nodes=[{"id": "G", "kind": "gateway", "aggregate": {"serves": True, "minChildren": 2}}])
        over = _frag(producer={"name": "i", "provider": "installer", "authority": 50}, nodes=[{"id": "G", "kind": "gateway", "aggregate": {"serves": False}}])
        self.assertEqual(merge([disc, over])["site"]["nodes"][0]["aggregate"], {"serves": False})

    def test_node_tombstone_drops_node_and_relationships(self):
        """A node tombstone removes the node and any relationship referencing it (with a warning)."""
        disc = _frag(nodes=[{"id": "A", "kind": "gateway"}, {"id": "B", "kind": "inverter"}], relationships=[{"from": "A", "to": "B", "type": "contains"}])
        over = _frag(producer={"name": "i", "provider": "installer", "authority": 50}, nodes=[{"id": "B", "removed": True}])
        out = merge([disc, over])
        self.assertEqual([n["id"] for n in out["site"]["nodes"]], ["A"])
        self.assertNotIn("relationships", out["site"])
        self.assertTrue(any("dropped" in w for w in out["warnings"]))

    def test_offer_tombstone_removes_one_offer(self):
        """An access-path offer tombstone removes only that offer."""
        disc = _frag(nodes=[{"id": "N", "kind": "inverter", "accessPaths": [{"id": "ap", "provider": "x"}], "capabilities": [{"capability": "battery.soc", "accessPath": "ap", "read": {}}, {"capability": "battery.power", "accessPath": "ap", "read": {}}]}])
        over = _frag(producer={"name": "i", "provider": "installer", "authority": 50}, nodes=[{"id": "N", "kind": "inverter", "capabilities": [{"capability": "battery.power", "accessPath": "ap", "removed": True}]}])
        self.assertEqual([o["capability"] for o in merge([disc, over])["site"]["nodes"][0]["capabilities"]], ["battery.soc"])

    def test_bare_capability_tombstone_targets_derived_offer(self):
        """A bare-capability tombstone removes only the access-path-less (derived) offer."""
        disc = _frag(
            nodes=[
                {
                    "id": "N",
                    "kind": "inverter",
                    "accessPaths": [{"id": "ap", "provider": "x"}],
                    "capabilities": [{"capability": "meter.load_power", "accessPath": "ap", "read": {}}, {"capability": "meter.load_power", "derived": {"op": "sum", "inputs": []}}],
                }
            ]
        )
        over = _frag(producer={"name": "i", "provider": "installer", "authority": 50}, nodes=[{"id": "N", "kind": "inverter", "capabilities": [{"capability": "meter.load_power", "removed": True}]}])
        offers = merge([disc, over])["site"]["nodes"][0]["capabilities"]
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["accessPath"], "ap")

    def test_recency_breaks_authority_tie(self):
        """Equal authority, higher docVersion wins."""
        a = _frag(producer={"name": "a", "provider": "a", "authority": 10}, docVersion=1, nodes=[{"id": "N", "kind": "inverter"}])
        b = _frag(producer={"name": "b", "provider": "b", "authority": 10}, docVersion=5, nodes=[{"id": "N", "kind": "gateway"}])
        self.assertEqual(merge([a, b])["site"]["nodes"][0]["kind"], "gateway")

    def test_full_tie_warns_and_keeps_first(self):
        """Equal authority and docVersion with conflicting values: first wins, warning emitted."""
        a = _frag(nodes=[{"id": "N", "kind": "inverter"}])
        b = _frag(nodes=[{"id": "N", "kind": "gateway"}])
        out = merge([a, b])
        self.assertEqual(out["site"]["nodes"][0]["kind"], "inverter")
        self.assertTrue(any("conflicting values" in w for w in out["warnings"]))

    def test_survival_after_rediscovery(self):
        """A fresh higher-docVersion discovery does not clobber a higher-authority overlay."""
        over = _frag(producer={"name": "i", "provider": "installer", "authority": 50}, docVersion=1, nodes=[{"id": "N", "kind": "gateway"}])
        disc2 = _frag(docVersion=9, nodes=[{"id": "N", "kind": "inverter", "attributes": {"ratedW": 6000}}])
        site = merge([disc2, over])["site"]
        self.assertEqual(site["nodes"][0]["kind"], "gateway")
        self.assertEqual(site["nodes"][0]["attributes"]["ratedW"], 6000)

    def test_tombstone_noop_when_absent(self):
        """Tombstoning an absent element changes nothing and emits no warning."""
        disc = _frag(nodes=[{"id": "N", "kind": "inverter"}])
        over = _frag(producer={"name": "i", "provider": "installer", "authority": 50}, nodes=[{"id": "GHOST", "removed": True}])
        out = merge([disc, over])
        self.assertEqual([n["id"] for n in out["site"]["nodes"]], ["N"])
        self.assertEqual(out["warnings"], [])

    def test_device_types_carried(self):
        """Top-level deviceTypes are unioned by key and carried into the merged site."""
        frag = _frag(producer={"name": "gw", "provider": "gw", "authority": 0}, docVersion=1, deviceTypes=[{"key": "ge-aio", "capabilities": [{"capability": "battery.soc", "read": {}}]}], nodes=[{"id": "INV", "kind": "inverter", "deviceType": "ge-aio"}])
        self.assertEqual([d["key"] for d in merge([frag])["site"]["deviceTypes"]], ["ge-aio"])

    def test_site_id_from_highest_authority_setter(self):
        """site.id comes from the highest-authority input that sets id, not merely the top input."""
        disc = _frag(id="site:home", nodes=[{"id": "INV", "kind": "inverter"}])
        over = _frag(producer={"name": "i", "provider": "installer", "authority": 50}, nodes=[{"id": "INV", "kind": "gateway"}])
        self.assertEqual(merge([disc, over])["site"]["id"], "site:home")

    def test_synthetic_producer_and_docversion(self):
        """Merged producer is synthetic with input provenance; docVersion is a positive int digest."""
        a = _frag(producer={"name": "gw", "provider": "gw", "authority": 0}, docVersion=3, nodes=[{"id": "N", "kind": "inverter"}])
        b = _frag(producer={"name": "cloud", "provider": "cloud", "authority": 0}, docVersion=7, nodes=[{"id": "M", "kind": "meter"}])
        site = merge([a, b])["site"]
        self.assertEqual(site["producer"]["provider"], "lattice-merge")
        self.assertEqual([i["provider"] for i in site["producer"]["inputs"]], ["gw", "cloud"])
        self.assertIsInstance(site["docVersion"], int)
        self.assertGreater(site["docVersion"], 0)
        c = _frag(producer={"name": "cloud", "provider": "cloud", "authority": 0}, docVersion=7, nodes=[{"id": "M", "kind": "gateway"}])
        self.assertNotEqual(site["docVersion"], merge([a, c])["site"]["docVersion"])

    def test_empty_input_raises(self):
        """Merging zero documents raises (no valid zero-node site)."""
        with self.assertRaises(ValueError):
            merge([])

    def test_incompatible_major_raises(self):
        """Inputs with different topologyVersion majors cannot be merged."""
        a = _frag(topologyVersion="0.1.0", nodes=[{"id": "N", "kind": "inverter"}])
        b = _frag(topologyVersion="1.0.0", nodes=[{"id": "N", "kind": "inverter"}])
        with self.assertRaises(ValueError):
            merge([a, b])


if __name__ == "__main__":
    unittest.main()
