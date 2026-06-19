# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice device-mapping unit tests (read-only)
# -----------------------------------------------------------------------------
"""Unit tests for the pure Lattice device-mapping core (no PredBat / Home Assistant)."""
import unittest

from lattice import Fragment, Sensor, AccessPath, merge_fragments, resolve_sensor, device_fragment
from lattice_projection import LatticeProjection


class TestModel(unittest.TestCase):
    """Fragment parses from the wire dict shape (devices + access paths + sensors)."""

    def test_fragment_from_dict(self):
        """A producer dict becomes a typed Fragment with nodes/sensors/access paths."""
        f = Fragment.from_dict(
            {
                "topologyVersion": "0.1.0",
                "scope": "fragment",
                "producer": {"name": "Local gateway", "provider": "local-gateway"},
                "nodes": [
                    {
                        "id": "INV-1",
                        "kind": "inverter",
                        "deviceType": "ge-aio",
                        "accessPaths": [{"id": "gw-local", "provider": "local-gateway", "locality": "local", "transport": "modbus", "preference": 10}],
                        "sensors": [{"capability": "soc", "unit": "%", "entity": "sensor.predbat_gateway_inv1_soc", "accessPath": "gw-local"}],
                    }
                ],
            }
        )
        self.assertEqual(f.provider, "local-gateway")
        n = f.nodes[0]
        self.assertEqual(n.id, "INV-1")
        self.assertEqual(n.access_paths[0].preference, 10)
        self.assertEqual(n.sensor("soc").entity, "sensor.predbat_gateway_inv1_soc")
        self.assertIsNone(n.sensor("nope"))

    def test_defaults(self):
        """AccessPath/Sensor from_dict tolerate minimal dicts."""
        self.assertEqual(AccessPath.from_dict({"id": "x"}).preference, 0)
        self.assertEqual(Sensor.from_dict({"capability": "soc"}).entity, "")


class TestMerge(unittest.TestCase):
    """Same serial from two producers becomes one node with both access paths + combined sensors."""

    def _frag(self, provider, pref, entity):
        return Fragment.from_dict(
            {
                "producer": {"provider": provider},
                "nodes": [
                    {
                        "id": "INV-1",
                        "kind": "inverter",
                        "deviceType": "ge-aio",
                        "accessPaths": [{"id": provider, "provider": provider, "preference": pref}],
                        "sensors": [{"capability": "soc", "unit": "%", "entity": entity, "accessPath": provider}],
                    }
                ],
            }
        )

    def test_merge_by_serial(self):
        """One device via gateway + cloud => one node, 2 access paths (gateway first), 2 soc sensors."""
        merged = merge_fragments([self._frag("local-gateway", 10, "sensor.gw_soc"), self._frag("ge-cloud", 1, "sensor.cloud_soc")])
        self.assertEqual(len(merged.nodes), 1)
        node = merged.nodes[0]
        self.assertEqual([ap.provider for ap in node.access_paths], ["local-gateway", "ge-cloud"])
        self.assertEqual(len(node.sensors), 2)

    def test_distinct_serials_are_siblings(self):
        """Different serials remain separate nodes."""
        a = self._frag("local-gateway", 10, "sensor.a")
        b = self._frag("ge-cloud", 1, "sensor.b")
        b.nodes[0].id = "INV-2"
        self.assertEqual(len(merge_fragments([a, b]).nodes), 2)


class TestResolveSensor(unittest.TestCase):
    """resolve_sensor returns the entity on the highest-preference access path."""

    def test_prefers_highest_preference(self):
        """A device seen via gateway(10) + cloud(1) resolves soc to the gateway's entity."""
        merged = merge_fragments(
            [
                Fragment.from_dict(device_fragment([{"serial": "INV-1", "sensors": [{"capability": "soc", "entity": "sensor.gw_soc"}]}], provider="local-gateway", name="GW", transport="modbus", preference=10, locality="local")),
                Fragment.from_dict(device_fragment([{"serial": "INV-1", "sensors": [{"capability": "soc", "entity": "sensor.cloud_soc"}]}], provider="ge-cloud", name="Cloud", transport="https", preference=1, locality="cloud")),
            ]
        )
        self.assertEqual(resolve_sensor(merged, "soc", "INV-1"), "sensor.gw_soc")
        self.assertIsNone(resolve_sensor(merged, "soc", "NOPE"))
        self.assertIsNone(resolve_sensor(merged, "power", "INV-1"))


class TestDeviceFragment(unittest.TestCase):
    """device_fragment builds a read-only fragment from plain device data."""

    def test_build(self):
        """Each device becomes a node with its access path + declared sensors; no control anywhere."""
        f = Fragment.from_dict(
            device_fragment(
                [{"serial": "CH-1", "device_type": "GIVENERGY_AIO", "sensors": [{"capability": "soc", "unit": "%", "entity": "sensor.ch1_soc"}, {"capability": "battery_power", "unit": "W", "entity": "sensor.ch1_power"}]}],
                provider="local-gateway",
                name="GW",
                transport="modbus",
                preference=10,
                locality="local",
            )
        )
        n = f.nodes[0]
        self.assertEqual(n.id, "CH-1")
        self.assertEqual(n.device_type, "givenergy_aio")
        self.assertEqual({s.capability for s in n.sensors}, {"soc", "battery_power"})
        self.assertFalse(hasattr(n, "capabilities"))  # no control surface

    def test_skips_without_serial(self):
        """Devices with no serial are skipped (cannot be identity-keyed)."""
        self.assertEqual(device_fragment([{"device_type": "x"}], provider="p", name="n", transport="t", preference=1, locality="local")["nodes"], [])


class _FakeComp:
    """A stand-in producer component exposing lattice_fragment() and is_alive()."""

    def __init__(self, fragment, alive=True):
        self._fragment = fragment
        self._alive = alive

    def lattice_fragment(self):
        return self._fragment

    def is_alive(self):
        return self._alive


class _FakeComponents:
    """A stand-in component registry."""

    def __init__(self, mapping):
        self._mapping = mapping

    def get_component(self, name):
        return self._mapping.get(name)

    def get_all(self):
        return list(self._mapping.keys())


class _FakeBase:
    """A minimal PredBat base for dependency-injected tests."""

    def __init__(self, components, args=None):
        self.components = components
        self._args = args or {}
        self.args = self._args
        self.logs = []
        self.local_tz = None
        self.prefix = "predbat"

    def get_arg(self, name, default=None):
        return self._args.get(name, default)

    def log(self, message):
        self.logs.append(message)


class TestLatticeProjection(unittest.TestCase):
    """The projection discovers producers, merges them, and resolves sensors — read-only."""

    def _proj(self, **args):
        gw = device_fragment([{"serial": "INV-1", "sensors": [{"capability": "soc", "entity": "sensor.gw_soc"}]}], provider="local-gateway", name="GW", transport="modbus", preference=10, locality="local")
        cloud = device_fragment([{"serial": "INV-1", "sensors": [{"capability": "soc", "entity": "sensor.cloud_soc"}]}], provider="ge-cloud", name="Cloud", transport="https", preference=1, locality="cloud")
        fox = device_fragment([{"serial": "FOX-1", "sensors": []}], provider="fox-cloud", name="Fox", transport="https", preference=1, locality="cloud")
        comps = _FakeComponents({"gateway": _FakeComp(gw), "gecloud": _FakeComp(cloud), "fox": _FakeComp(fox)})
        proj = LatticeProjection(_FakeBase(comps, args or None))
        proj.refresh()
        return proj

    def test_merges_and_discovers_any_brand(self):
        """All producers (incl. a third brand) auto-merge into the device map."""
        proj = self._proj()
        self.assertEqual({n.id for n in proj.site.nodes}, {"INV-1", "FOX-1"})

    def test_sensor_entity_prefers_gateway(self):
        """A device seen via gateway + cloud resolves soc to the gateway entity."""
        self.assertEqual(self._proj().sensor_entity("soc", "INV-1"), "sensor.gw_soc")

    def test_enabled_default_off(self):
        """Mapping is off unless lattice_projection_enable is set."""
        self.assertFalse(self._proj().enabled())
        self.assertTrue(self._proj(lattice_projection_enable=True).enabled())


class TestLatticeComponent(unittest.IsolatedAsyncioTestCase):
    """The component builds + logs the device map when enabled; no-op when off."""

    def _base(self, enabled):
        gw = device_fragment([{"serial": "INV-1", "sensors": [{"capability": "soc", "entity": "sensor.gw_soc"}]}], provider="local-gateway", name="GW", transport="modbus", preference=10, locality="local")
        comps = _FakeComponents({"gateway": _FakeComp(gw)})
        return _FakeBase(comps, {"lattice_projection_enable": True} if enabled else None)

    async def test_noop_when_disabled(self):
        from lattice_component import LatticeComponent

        comp = LatticeComponent(self._base(enabled=False))
        self.assertTrue(await comp.run(0, True))
        self.assertIsNone(comp.projection.site)
        self.assertEqual(comp.base.logs, [])

    async def test_logs_map_when_enabled(self):
        from lattice_component import LatticeComponent

        comp = LatticeComponent(self._base(enabled=True))
        self.assertTrue(await comp.run(0, True))
        self.assertEqual(len(comp.projection.site.nodes), 1)
        self.assertTrue(any("device map" in m for m in comp.base.logs))


if __name__ == "__main__":
    unittest.main()
