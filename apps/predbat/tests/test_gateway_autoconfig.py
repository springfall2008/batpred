# cspell:ignore autoconfig
"""Tests for the pure Gateway automatic-configuration mapper."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gateway_autoconfig import (  # noqa: E402
    GatewayAutoConfigError,
    compile_gateway_auto_config,
)


def inverter(serial="CE123456789", **overrides):
    """Build one detached Gateway inverter snapshot."""
    value = {
        "serial": serial,
        "kind": "inverter",
        "primary": False,
        "battery_present": True,
        "battery_capacity_wh": 9600,
        "model": "",
        "ems_num_inverters": 0,
    }
    value.update(overrides)
    return value


class TestGatewayAutoConfig(unittest.TestCase):
    """The pure plan preserves existing Gateway selection and entity shapes."""

    def test_single_inverter_exact_core_values_and_immutability(self):
        plan = compile_gateway_auto_config((inverter(),))

        self.assertEqual(plan.selected_serials, ("CE123456789",))
        self.assertEqual(plan.arguments["inverter_type"], ("GWMQTT",))
        self.assertEqual(plan.arguments["num_inverters"], 1)
        self.assertEqual(
            plan.arguments["battery_power"],
            ("sensor.predbat_gateway_456789_battery_power",),
        )
        self.assertEqual(
            plan.arguments["charge_start_time"],
            ("select.predbat_gateway_456789_charge_slot1_start",),
        )
        self.assertIsNone(plan.arguments["pause_mode"])
        self.assertFalse(plan.arguments["ge_cloud_data"])
        with self.assertRaises(TypeError):
            plan.arguments["num_inverters"] = 2
        legacy = plan.legacy_arguments()
        self.assertIsInstance(legacy["battery_power"], list)

    def test_multi_direct_inverters_are_serial_stable(self):
        plan = compile_gateway_auto_config(
            (inverter("SER-B"), inverter("SER-A")),
        )

        self.assertEqual(plan.selected_serials, ("SER-A", "SER-B"))
        self.assertEqual(plan.arguments["num_inverters"], 2)
        self.assertEqual(
            plan.arguments["battery_power"],
            (
                "sensor.predbat_gateway_ser-a_battery_power",
                "sensor.predbat_gateway_ser-b_battery_power",
            ),
        )

    def test_gateway_coordinator_requires_multiple_aios(self):
        gateway = inverter(
            "GW-1",
            kind="gateway",
            battery_present=False,
            battery_capacity_wh=0,
        )
        one = compile_gateway_auto_config((gateway, inverter("AIO-1")))
        two = compile_gateway_auto_config(
            (gateway, inverter("AIO-1"), inverter("AIO-2")),
        )

        self.assertEqual(one.selected_serials, ("AIO-1",))
        self.assertEqual(two.selected_serials, ("GW-1",))
        self.assertEqual(two.arguments["soc_max"], (None,))

    def test_ems_adds_aggregate_values(self):
        ems = inverter(
            "EMS-1",
            kind="ems",
            battery_present=False,
            battery_capacity_wh=0,
            ems_num_inverters=3,
        )
        plan = compile_gateway_auto_config((inverter("AIO-1"), ems))

        self.assertEqual(plan.selected_serials, ("EMS-1",))
        self.assertEqual(
            plan.arguments["ems_total_soc"],
            "sensor.predbat_gateway_ems_total_soc",
        )
        self.assertEqual(
            plan.arguments["idle_start_time"],
            ("select.predbat_gateway_ems-1_discharge_slot1_start",),
        )

    def test_filter_primary_fallback_and_hybrid_decision(self):
        empty_primary = inverter(
            "EMPTY",
            primary=True,
            battery_present=False,
            battery_capacity_wh=0,
        )
        fallback = inverter("FALLBACK", model="All-in-One")
        plan = compile_gateway_auto_config(
            (empty_primary, fallback),
            serial_filter=("fallback",),
        )

        self.assertEqual(plan.selected_serials, ("FALLBACK",))
        self.assertFalse(plan.hybrid)
        with self.assertRaises(GatewayAutoConfigError):
            compile_gateway_auto_config((fallback,), serial_filter=("missing",))


if __name__ == "__main__":
    unittest.main()
