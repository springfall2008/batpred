# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice projection core unit tests
# -----------------------------------------------------------------------------
"""Unit tests for the pure Lattice model/merge/resolve core.

These run standalone (no PredBat / Home Assistant), like test_oo_utils.py.
"""
import unittest

from lattice import Fragment, Node, Capability, AccessPath, merge_fragments, resolve_control, resolve_read, inverter_fragment, control_candidates


class TestModel(unittest.TestCase):
    """Fragment parses from and round-trips to the wire dict shape."""

    def test_fragment_from_dict(self):
        """A producer dict becomes a typed Fragment with nodes/capabilities/access paths."""
        d = {
            "topologyVersion": "0.1.0",
            "scope": "fragment",
            "producer": {"name": "Local gateway", "provider": "local-gateway"},
            "nodes": [
                {
                    "id": "INV-1",
                    "kind": "inverter",
                    "deviceType": "ge-aio",
                    "accessPaths": [{"id": "gw-local", "provider": "local-gateway", "locality": "local", "transport": "modbus", "preference": 10}],
                    "capabilities": [{"capability": "charge_rate", "unit": "W", "read": True, "control": True, "accessPath": "gw-local", "constraints": {"min": 0, "max": 6000}}],
                }
            ],
            "relationships": [],
        }
        f = Fragment.from_dict(d)
        self.assertEqual(f.provider, "local-gateway")
        self.assertEqual(len(f.nodes), 1)
        n = f.nodes[0]
        self.assertEqual(n.id, "INV-1")
        self.assertEqual(n.access_paths[0].preference, 10)
        cap = n.capability("charge_rate")
        self.assertIsNotNone(cap)
        self.assertTrue(cap.control)
        self.assertEqual(cap.constraints["max"], 6000)

    def test_node_capability_missing(self):
        """capability() returns None for an unknown capability."""
        n = Node(id="X", kind="meter", device_type="m", access_paths=[], capabilities=[])
        self.assertIsNone(n.capability("charge_rate"))

    def test_access_path_defaults(self):
        """AccessPath.from_dict tolerates a minimal dict."""
        ap = AccessPath.from_dict({"id": "x"})
        self.assertEqual(ap.id, "x")
        self.assertEqual(ap.preference, 0)

    def test_capability_defaults(self):
        """Capability.from_dict defaults read/control to False and constraints to {}."""
        c = Capability.from_dict({"capability": "soc"})
        self.assertFalse(c.read)
        self.assertFalse(c.control)
        self.assertEqual(c.constraints, {})


class TestMerge(unittest.TestCase):
    """Same serial from two producers becomes one node with both access paths, ranked."""

    def _frag(self, provider, pref, transport):
        """Build a one-node fragment for INV-1 with a single access path at the given preference."""
        return Fragment.from_dict(
            {
                "producer": {"name": provider, "provider": provider},
                "nodes": [
                    {
                        "id": "INV-1",
                        "kind": "inverter",
                        "deviceType": "ge-aio",
                        "accessPaths": [{"id": provider, "provider": provider, "locality": "local" if pref >= 10 else "cloud", "transport": transport, "preference": pref}],
                        "capabilities": [{"capability": "charge_rate", "control": True, "accessPath": provider}],
                    }
                ],
                "relationships": [],
            }
        )

    def test_merge_by_serial(self):
        """One physical device seen via gateway + cloud => one node, 2 access paths (gateway first)."""
        merged = merge_fragments([self._frag("local-gateway", 10, "modbus"), self._frag("ge-cloud", 1, "https")])
        self.assertEqual(len(merged.nodes), 1)
        node = merged.nodes[0]
        self.assertEqual(node.id, "INV-1")
        self.assertEqual([ap.provider for ap in node.access_paths], ["local-gateway", "ge-cloud"])  # preference desc

    def test_distinct_serials_are_siblings(self):
        """Different serials remain separate nodes."""
        a = self._frag("local-gateway", 10, "modbus")
        b = self._frag("ge-cloud", 1, "https")
        b.nodes[0].id = "INV-2"
        merged = merge_fragments([a, b])
        self.assertEqual(len(merged.nodes), 2)


class TestResolve(unittest.TestCase):
    """Resolution picks the highest-preference AVAILABLE access path, clamps, and falls back."""

    def _site(self):
        """A site where INV-1 is reachable via gateway (preferred) and GE-Cloud (fallback)."""
        return merge_fragments(
            [
                Fragment.from_dict(
                    {
                        "producer": {"provider": "local-gateway"},
                        "nodes": [
                            {
                                "id": "INV-1",
                                "kind": "inverter",
                                "deviceType": "ge-aio",
                                "accessPaths": [{"id": "gw-local", "provider": "local-gateway", "preference": 10}],
                                "capabilities": [{"capability": "charge_rate", "read": True, "control": True, "accessPath": "gw-local", "constraints": {"min": 0, "max": 6000}}],
                            }
                        ],
                    }
                ),
                Fragment.from_dict(
                    {
                        "producer": {"provider": "ge-cloud"},
                        "nodes": [
                            {
                                "id": "INV-1",
                                "kind": "inverter",
                                "deviceType": "ge-aio",
                                "accessPaths": [{"id": "ge-cloud", "provider": "ge-cloud", "preference": 1}],
                                "capabilities": [{"capability": "charge_rate", "read": True, "control": True, "accessPath": "ge-cloud", "constraints": {"min": 0, "max": 6000}}],
                            }
                        ],
                    }
                ),
            ]
        )

    def test_control_prefers_gateway(self):
        """With both available, control routes to the local gateway."""
        r = resolve_control(self._site(), "charge_rate", "INV-1", 3000, available={"local-gateway", "ge-cloud"})
        self.assertTrue(r.ok)
        self.assertEqual(r.provider, "local-gateway")
        self.assertEqual(r.value, 3000)

    def test_control_falls_back_to_cloud(self):
        """Gateway unavailable (weak link) => control falls back to GE-Cloud — the Phil scenario."""
        r = resolve_control(self._site(), "charge_rate", "INV-1", 3000, available={"ge-cloud"})
        self.assertTrue(r.ok)
        self.assertEqual(r.provider, "ge-cloud")

    def test_control_clamps_to_constraints(self):
        """A value above max clamps to the capability ceiling."""
        r = resolve_control(self._site(), "charge_rate", "INV-1", 9999, available={"local-gateway"})
        self.assertEqual(r.value, 6000)

    def test_control_none_available(self):
        """No available provider => not ok (caller leaves the entity as-is)."""
        r = resolve_control(self._site(), "charge_rate", "INV-1", 3000, available=set())
        self.assertFalse(r.ok)

    def test_read_prefers_gateway(self):
        """Read resolves to the highest-preference available provider."""
        r = resolve_read(self._site(), "charge_rate", "INV-1", available={"local-gateway", "ge-cloud"})
        self.assertTrue(r.ok)
        self.assertEqual(r.provider, "local-gateway")


class TestProducerFragment(unittest.TestCase):
    """inverter_fragment builds a producer fragment from plain inverter data."""

    def test_gateway_local_fragment(self):
        """A local producer emits a high-preference modbus access path with a rated ceiling."""
        d = inverter_fragment([{"serial": "CH-1", "device_type": "GIVENERGY_AIO", "rated_w": 6000}], provider="local-gateway", name="Local gateway", transport="modbus", preference=10, locality="local")
        f = Fragment.from_dict(d)
        self.assertEqual(f.provider, "local-gateway")
        n = f.nodes[0]
        self.assertEqual(n.id, "CH-1")
        self.assertEqual(n.access_paths[0].preference, 10)
        self.assertTrue(n.capability("charge_rate").control)
        self.assertEqual(n.capability("charge_rate").constraints["max"], 6000)

    def test_cloud_fragment_low_pref_open_max(self):
        """A cloud producer emits a low-preference https path; with no rating the max is open."""
        d = inverter_fragment([{"serial": "CH-1", "device_type": "ge-aio"}], provider="ge-cloud", name="GivEnergy Cloud", transport="https", preference=1, locality="cloud")
        f = Fragment.from_dict(d)
        self.assertEqual(f.nodes[0].access_paths[0].preference, 1)
        self.assertEqual(f.nodes[0].capability("charge_rate").constraints, {"min": 0})

    def test_skips_inverters_without_serial(self):
        """Inverters with no serial are skipped (cannot be identity-keyed)."""
        d = inverter_fragment([{"device_type": "x"}], provider="p", name="n", transport="t", preference=1, locality="local")
        self.assertEqual(d["nodes"], [])

    def test_default_caps_include_soc_and_targets(self):
        """The default inverter fragment exposes soc(read-only) + target_soc/reserve_soc(percent control)."""
        f = Fragment.from_dict(inverter_fragment([{"serial": "CH-1", "rated_w": 6000}], provider="local-gateway", name="GW", transport="modbus", preference=10, locality="local"))
        n = f.nodes[0]
        self.assertTrue(n.capability("soc").read)
        self.assertFalse(n.capability("soc").control)
        self.assertEqual(n.capability("target_soc").constraints, {"min": 0, "max": 100})
        self.assertTrue(n.capability("reserve_soc").control)
        self.assertEqual(n.capability("charge_rate").constraints, {"min": 0, "max": 6000})  # rate still W/rated

    def test_controllable_restricts_control_flags(self):
        """A provider declares control=true ONLY for capabilities it can actually execute."""
        # read-only producer (no lattice_control): nothing controllable
        ro = Fragment.from_dict(inverter_fragment([{"serial": "X1", "rated_w": 6000}], provider="fox-cloud", name="Fox", transport="https", preference=1, locality="cloud", controllable=()))
        n = ro.nodes[0]
        self.assertFalse(n.capability("charge_rate").control)
        self.assertTrue(n.capability("charge_rate").read)  # still readable
        # partial: only charge_rate executable
        partial = Fragment.from_dict(inverter_fragment([{"serial": "X1", "rated_w": 6000}], provider="local-gateway", name="GW", transport="modbus", preference=10, locality="local", controllable=("charge_rate", "discharge_rate")))
        p = partial.nodes[0]
        self.assertTrue(p.capability("charge_rate").control)
        self.assertFalse(p.capability("target_soc").control)  # not executable here

    def test_gateway_and_cloud_merge_to_one_node(self):
        """The two producers' fragments merge by serial into one node with both paths (gateway first)."""
        gw = Fragment.from_dict(inverter_fragment([{"serial": "CH-1", "rated_w": 6000}], provider="local-gateway", name="GW", transport="modbus", preference=10, locality="local"))
        cloud = Fragment.from_dict(inverter_fragment([{"serial": "CH-1"}], provider="ge-cloud", name="Cloud", transport="https", preference=1, locality="cloud"))
        merged = merge_fragments([gw, cloud])
        self.assertEqual(len(merged.nodes), 1)
        self.assertEqual([ap.provider for ap in merged.nodes[0].access_paths], ["local-gateway", "ge-cloud"])


class TestControlCandidates(unittest.TestCase):
    """control_candidates returns ranked, per-path-clamped (provider, access_path, value) tuples."""

    def _site(self):
        """INV-1 via gateway (pref 10, max 6000) and GE-Cloud (pref 1, open max)."""
        return merge_fragments(
            [
                Fragment.from_dict(inverter_fragment([{"serial": "INV-1", "rated_w": 6000}], provider="local-gateway", name="GW", transport="modbus", preference=10, locality="local")),
                Fragment.from_dict(inverter_fragment([{"serial": "INV-1"}], provider="ge-cloud", name="Cloud", transport="https", preference=1, locality="cloud")),
            ]
        )

    def test_ranked_and_clamped(self):
        """Both providers returned gateway-first; the gateway candidate clamps to its 6000 max."""
        candidates = control_candidates(self._site(), "charge_rate", "INV-1", 9999, {"local-gateway", "ge-cloud"})
        self.assertEqual([c[0] for c in candidates], ["local-gateway", "ge-cloud"])
        self.assertEqual(candidates[0][2], 6000)

    def test_only_available(self):
        """Unavailable providers are excluded."""
        candidates = control_candidates(self._site(), "charge_rate", "INV-1", 3000, {"ge-cloud"})
        self.assertEqual([c[0] for c in candidates], ["ge-cloud"])

    def test_unknown_node_or_capability(self):
        """A missing node or capability yields no candidates."""
        self.assertEqual(control_candidates(self._site(), "charge_rate", "NOPE", 1, {"local-gateway"}), [])
        self.assertEqual(control_candidates(self._site(), "nope", "INV-1", 1, {"local-gateway"}), [])

    def test_malformed_constraints_do_not_crash(self):
        """A non-numeric constraint bound (malformed fragment) must not raise — value left unclamped."""
        site = merge_fragments(
            [
                Fragment.from_dict(
                    {
                        "producer": {"provider": "p"},
                        "nodes": [
                            {
                                "id": "N",
                                "kind": "inverter",
                                "deviceType": "x",
                                "accessPaths": [{"id": "p", "provider": "p", "preference": 5}],
                                "capabilities": [{"capability": "charge_rate", "control": True, "accessPath": "p", "constraints": {"min": 0, "max": "6000"}}],
                            }
                        ],
                    }
                )
            ]
        )
        candidates = control_candidates(site, "charge_rate", "N", 9999, {"p"})
        self.assertEqual([c[0] for c in candidates], ["p"])  # resolved without raising


if __name__ == "__main__":
    unittest.main()
