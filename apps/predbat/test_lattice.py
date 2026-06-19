# -----------------------------------------------------------------------------
# Predbat Home Battery System - Lattice device-mapping unit tests (read-only)
# -----------------------------------------------------------------------------
"""Unit tests for the pure Lattice device-mapping core (no PredBat / Home Assistant)."""
import unittest

from lattice import Fragment, Sensor, AccessPath, merge_fragments, resolve_sensor, device_fragment


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


if __name__ == "__main__":
    unittest.main()
