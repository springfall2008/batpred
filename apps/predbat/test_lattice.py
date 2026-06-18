# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice projection core unit tests
# -----------------------------------------------------------------------------
"""Unit tests for the pure Lattice model/merge/resolve core.

These run standalone (no PredBat / Home Assistant), like test_oo_utils.py.
"""
import unittest

from lattice import Fragment, Node, Capability, AccessPath, merge_fragments, resolve_control, resolve_read, inverter_fragment, control_candidates
from lattice_projection import entity_for, projection_entries, LatticeProjection


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


class TestProjection(unittest.TestCase):
    """The curated table maps (capability, scope) to a predbat.* entity and direction."""

    def test_charge_rate_entry(self):
        """charge_rate (battery-system) maps to predbat.charge_rate, read+write."""
        ent = entity_for("charge_rate", "battery-system")
        self.assertEqual(ent.entity, "predbat.charge_rate")
        self.assertTrue(ent.write)

    def test_unknown_capability_not_projected(self):
        """A capability not in the table returns None (behaves as today)."""
        self.assertIsNone(entity_for("does_not_exist", "plant"))

    def test_entries_are_unique(self):
        """No duplicate (capability, scope) keys in the curated table."""
        keys = [(e.capability, e.scope) for e in projection_entries()]
        self.assertEqual(len(keys), len(set(keys)))

    def test_target_and_reserve_entries(self):
        """target_soc and reserve_soc are projected controls."""
        self.assertEqual(entity_for("target_soc", "battery-system").entity, "predbat.target_soc")
        self.assertTrue(entity_for("reserve_soc", "battery-system").write)


class _FakeComp:
    """A stand-in producer component exposing lattice_fragment() and is_alive()."""

    def __init__(self, fragment, alive=True):
        """Hold a pre-built fragment dict and a liveness flag."""
        self._fragment = fragment
        self._alive = alive

    def lattice_fragment(self):
        """Return the canned fragment dict."""
        return self._fragment

    def is_alive(self):
        """Return whether this producer is currently reachable."""
        return self._alive


class _FakeComponents:
    """A stand-in component registry mapping name to component."""

    def __init__(self, mapping):
        """Hold the name -> component mapping."""
        self._mapping = mapping

    def get_component(self, name):
        """Return the component for name, or None."""
        return self._mapping.get(name)

    def get_all(self):
        """Return all registered component names."""
        return list(self._mapping.keys())


class _FakeBase:
    """A minimal PredBat base for dependency-injected projection tests."""

    def __init__(self, components, args=None):
        """Hold a components registry and an args dict (+ ComponentBase init attrs)."""
        self.components = components
        self._args = args or {}
        self.args = self._args
        self.logs = []
        self.local_tz = None
        self.prefix = "predbat"

    def get_arg(self, name, default=None):
        """Return a config arg from the fake args dict."""
        return self._args.get(name, default)

    def log(self, message):
        """Capture a log line."""
        self.logs.append(message)


class TestLatticeProjection(unittest.TestCase):
    """End-to-end: collect producers, merge, and route charge_rate with gateway->cloud fallback."""

    def _proj(self, gw_alive=True, cloud_alive=True, args=None):
        """Build a LatticeProjection over fake gateway + gecloud producers and refresh it."""
        gw = inverter_fragment([{"serial": "CH-1", "rated_w": 6000}], provider="local-gateway", name="GW", transport="modbus", preference=10, locality="local")
        cloud = inverter_fragment([{"serial": "CH-1"}], provider="ge-cloud", name="Cloud", transport="https", preference=1, locality="cloud")
        comps = _FakeComponents({"gateway": _FakeComp(gw, gw_alive), "gecloud": _FakeComp(cloud, cloud_alive)})
        proj = LatticeProjection(_FakeBase(comps, args))
        proj.refresh()
        return proj

    def test_refresh_merges_producers(self):
        """refresh() collects both producers and merges them by serial into one node."""
        proj = self._proj()
        self.assertEqual(len(proj.site.nodes), 1)
        self.assertEqual(len(proj.site.nodes[0].access_paths), 2)

    def test_write_prefers_gateway(self):
        """With both producers alive, charge_rate write resolves to the gateway."""
        r = self._proj().write("charge_rate", "battery-system", "CH-1", 3000)
        self.assertTrue(r.ok)
        self.assertEqual(r.provider, "local-gateway")
        self.assertEqual(r.value, 3000)

    def test_write_falls_back_when_gateway_down(self):
        """Gateway component not alive => write falls back to GE-Cloud (the Phil scenario)."""
        r = self._proj(gw_alive=False).write("charge_rate", "battery-system", "CH-1", 3000)
        self.assertTrue(r.ok)
        self.assertEqual(r.provider, "ge-cloud")

    def test_write_capability_not_in_table(self):
        """A capability not in the projection table is not routed."""
        r = self._proj().write("does_not_exist", "plant", "CH-1", 1)
        self.assertFalse(r.ok)

    def test_enabled_default_off(self):
        """The projection is switched off unless lattice_projection_enable is set."""
        self.assertFalse(self._proj().enabled())
        self.assertTrue(self._proj(args={"lattice_projection_enable": True}).enabled())

    def test_would_handle(self):
        """would_handle gates the live write path: enabled + projected + an available provider."""
        off = self._proj()  # flag off by default
        self.assertFalse(off.would_handle("charge_rate", "battery-system", "CH-1"))
        on = self._proj(args={"lattice_projection_enable": True})
        self.assertTrue(on.would_handle("charge_rate", "battery-system", "CH-1"))
        self.assertFalse(on.would_handle("does_not_exist", "plant", "CH-1"))
        self.assertFalse(on.would_handle("charge_rate", "battery-system", "NOPE"))  # no such node

    def test_would_handle_false_when_no_provider_live(self):
        """No reachable provider => would_handle is False (caller keeps the normal write)."""
        on = self._proj(gw_alive=False, cloud_alive=False, args={"lattice_projection_enable": True})
        self.assertFalse(on.would_handle("charge_rate", "battery-system", "CH-1"))

    def test_discovers_any_brand_producer(self):
        """A third brand (Fox) that publishes a fragment is auto-discovered — no projection change."""
        gw = inverter_fragment([{"serial": "GE-1", "rated_w": 6000}], provider="local-gateway", name="GW", transport="modbus", preference=10, locality="local")
        fox = inverter_fragment([{"serial": "FOX-1", "rated_w": 3600}], provider="fox-cloud", name="Fox", transport="https", preference=1, locality="cloud")
        comps = _FakeComponents({"gateway": _FakeComp(gw), "fox": _FakeComp(fox)})
        proj = LatticeProjection(_FakeBase(comps))
        proj.refresh()
        self.assertEqual({n.id for n in proj.site.nodes}, {"GE-1", "FOX-1"})
        # the Fox-only node resolves to the fox-cloud provider with no central registration
        r = proj.write("charge_rate", "battery-system", "FOX-1", 2000)
        self.assertTrue(r.ok)
        self.assertEqual(r.provider, "fox-cloud")


class _FakeAsyncComp:
    """A producer whose lattice_control records calls and can simulate failure/liveness."""

    def __init__(self, fragment, alive=True, succeed=True):
        """Hold a fragment, a liveness flag, and whether writes succeed."""
        self._fragment = fragment
        self._alive = alive
        self._succeed = succeed
        self.calls = []

    def lattice_fragment(self):
        """Return the canned fragment dict."""
        return self._fragment

    def is_alive(self):
        """Return whether this producer is reachable."""
        return self._alive

    async def lattice_control(self, node_id, capability, value):
        """Record the control call and return the configured success flag."""
        self.calls.append((node_id, capability, value))
        return self._succeed


class TestLatticeApply(unittest.IsolatedAsyncioTestCase):
    """apply() resolves AND executes, trying providers in order and falling back on failure."""

    def _build(self, gw_alive=True, gw_ok=True, cloud_alive=True, cloud_ok=True):
        """Wire a projection over fake async gateway + gecloud producers for INV-1."""
        gw_frag = inverter_fragment([{"serial": "INV-1", "rated_w": 6000}], provider="local-gateway", name="GW", transport="modbus", preference=10, locality="local")
        cloud_frag = inverter_fragment([{"serial": "INV-1"}], provider="ge-cloud", name="Cloud", transport="https", preference=1, locality="cloud")
        gw = _FakeAsyncComp(gw_frag, gw_alive, gw_ok)
        cloud = _FakeAsyncComp(cloud_frag, cloud_alive, cloud_ok)
        proj = LatticeProjection(_FakeBase(_FakeComponents({"gateway": gw, "gecloud": cloud})))
        proj.refresh()
        return proj, gw, cloud

    async def test_executes_on_gateway(self):
        """Both up + gateway write succeeds => executed on the gateway with the clamped value."""
        proj, gw, cloud = self._build()
        r = await proj.apply("charge_rate", "battery-system", "INV-1", 3000)
        self.assertTrue(r.ok)
        self.assertEqual(r.provider, "local-gateway")
        self.assertEqual(gw.calls, [("INV-1", "charge_rate", 3000)])
        self.assertEqual(cloud.calls, [])

    async def test_falls_back_when_gateway_write_fails(self):
        """Gateway write returns False => fall back and execute on GE-Cloud."""
        proj, gw, cloud = self._build(gw_ok=False)
        r = await proj.apply("charge_rate", "battery-system", "INV-1", 3000)
        self.assertTrue(r.ok)
        self.assertEqual(r.provider, "ge-cloud")
        self.assertEqual(len(gw.calls), 1)  # tried first
        self.assertEqual(len(cloud.calls), 1)  # then fell back

    async def test_gateway_down_uses_cloud(self):
        """Gateway not alive => only GE-Cloud is a candidate."""
        proj, gw, cloud = self._build(gw_alive=False)
        r = await proj.apply("charge_rate", "battery-system", "INV-1", 3000)
        self.assertTrue(r.ok)
        self.assertEqual(r.provider, "ge-cloud")
        self.assertEqual(gw.calls, [])

    async def test_all_providers_fail(self):
        """Every provider's write fails => not ok."""
        proj, gw, cloud = self._build(gw_ok=False, cloud_ok=False)
        r = await proj.apply("charge_rate", "battery-system", "INV-1", 3000)
        self.assertFalse(r.ok)

    async def test_capability_not_in_table_not_applied(self):
        """A capability not in the table is not executed."""
        proj, gw, cloud = self._build()
        r = await proj.apply("does_not_exist", "plant", "INV-1", 1)
        self.assertFalse(r.ok)
        self.assertEqual(gw.calls, [])


class TestLatticeComponent(unittest.IsolatedAsyncioTestCase):
    """The live component refreshes + logs the merged graph when enabled, and is a no-op when off."""

    def _base(self, enabled):
        """A fake base with a gateway + gecloud producer; flag on/off."""
        gw = inverter_fragment([{"serial": "INV-1", "rated_w": 6000}], provider="local-gateway", name="GW", transport="modbus", preference=10, locality="local")
        cloud = inverter_fragment([{"serial": "INV-1"}], provider="ge-cloud", name="Cloud", transport="https", preference=1, locality="cloud")
        comps = _FakeComponents({"gateway": _FakeComp(gw), "gecloud": _FakeComp(cloud)})
        return _FakeBase(comps, args={"lattice_projection_enable": True} if enabled else None)

    async def test_run_noop_when_disabled(self):
        """Flag off: run() returns True, does not refresh, and logs nothing."""
        from lattice_component import LatticeComponent

        comp = LatticeComponent(self._base(enabled=False))
        ok = await comp.run(0, True)
        self.assertTrue(ok)
        self.assertIsNone(comp.projection.site)
        self.assertEqual(comp.base.logs, [])

    async def test_run_logs_graph_when_enabled(self):
        """Flag on: run() merges producers and logs the graph."""
        from lattice_component import LatticeComponent

        comp = LatticeComponent(self._base(enabled=True))
        ok = await comp.run(0, True)
        self.assertTrue(ok)
        self.assertEqual(len(comp.projection.site.nodes), 1)  # merged by serial
        self.assertTrue(any("merged site graph" in m for m in comp.base.logs))


if __name__ == "__main__":
    unittest.main()
