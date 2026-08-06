# cspell:ignore autoconfig
"""Tests for the pure GivEnergy Cloud auto-configuration mapper."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gecloud_autoconfig import (  # noqa: E402
    GECloudAutoConfigError,
    GECloudAutoConfigInput,
    compile_gecloud_auto_config,
)


def config(**overrides):
    """Build one complete single-battery GE discovery input."""
    values = {
        "batteries": ("bat1",),
        "ems": None,
        "gateway": None,
        "pv": (),
        "battery_meters": (("bat1", ("meter1",)),),
        "register_names": (
            (
                "bat1",
                (
                    "battery_charge_power",
                    "battery_discharge_power",
                    "battery_reserve_percent_limit",
                    "ac_charge_upper_percent_limit",
                    "enable_ac_charge_upper_percent_limit",
                    "ac_charge_enable",
                    "enable_dc_discharge",
                ),
            ),
        ),
        "models": (("bat1", "Hybrid"),),
        "prefix": "predbat",
    }
    values.update(overrides)
    return GECloudAutoConfigInput(**values)


class TestGECloudAutoConfig(unittest.TestCase):
    """The pure plan preserves current GE policy and argument shapes."""

    def test_single_battery_core_values(self):
        plan = compile_gecloud_auto_config(config())
        arguments = plan.legacy_arguments()

        self.assertEqual(arguments["inverter_type"], ["GEC"])
        self.assertEqual(arguments["num_inverters"], 1)
        self.assertEqual(
            arguments["battery_power"],
            ["sensor.predbat_gecloud_bat1_battery_power"],
        )
        self.assertEqual(
            arguments["charge_rate"],
            ["number.predbat_gecloud_bat1_battery_charge_power"],
        )
        self.assertIsNone(arguments["charge_rate_percent"])
        self.assertEqual(arguments["ge_cloud_serial"], "bat1")
        self.assertFalse(plan.ac_coupled)

    def test_percentage_fallback_and_missing_controls(self):
        plan = compile_gecloud_auto_config(
            config(
                register_names=(("bat1", ("inverter_charge_power_percentage",)),),
            )
        )
        arguments = plan.legacy_arguments()

        self.assertIsNone(arguments["charge_rate"])
        self.assertEqual(
            arguments["charge_rate_percent"],
            ["number.predbat_gecloud_bat1_" "inverter_charge_power_percentage"],
        )
        self.assertIsNone(arguments["pause_mode"])
        self.assertIsNone(arguments["discharge_target_soc"])

    def test_gateway_coordinator_and_split_pv(self):
        plan = compile_gecloud_auto_config(
            config(
                batteries=("bat2", "bat1"),
                gateway="gateway1",
                pv=("pv1",),
                split_pv=True,
                register_names=(("gateway1", ()),),
            )
        )

        self.assertEqual(plan.inverter_targets, ("gateway1",))
        self.assertEqual(plan.pv_sources, ("gateway1", "pv1"))
        self.assertEqual(plan.legacy_arguments()["num_inverters"], 1)
        self.assertEqual(
            len(plan.legacy_arguments()["pv_power"]),
            2,
        )

    def test_shared_ct_uses_one_daily_source_and_zero_power_slots(self):
        plan = compile_gecloud_auto_config(
            config(
                batteries=("bat1", "bat2"),
                battery_meters=(
                    ("bat1", ("shared",)),
                    ("bat2", ("shared",)),
                ),
                register_names=(("bat1", ()), ("bat2", ())),
            )
        )
        arguments = plan.legacy_arguments()

        self.assertTrue(plan.shared_ct)
        self.assertEqual(plan.grid_energy_sources, ("bat1",))
        self.assertEqual(arguments["grid_power"][1], 0)
        self.assertEqual(len(arguments["import_today"]), 1)

    def test_ems_fans_out_controls_and_aggregate_power(self):
        plan = compile_gecloud_auto_config(
            config(
                batteries=("bat1", "bat2"),
                ems="ems1",
                register_names=(("bat1", ()), ("bat2", ())),
            )
        )
        arguments = plan.legacy_arguments()

        self.assertEqual(plan.inverter_targets, ("ems1", "ems1"))
        self.assertEqual(arguments["inverter_type"], ["GEE", "GEE"])
        self.assertEqual(
            arguments["battery_power"],
            ["sensor.predbat_gecloud_ems1_battery_power", 0],
        )
        self.assertEqual(len(arguments["charge_start_time"]), 2)
        self.assertFalse(arguments["ge_cloud_data"])

    def test_load_today_omission_model_and_empty_input(self):
        plan = compile_gecloud_auto_config(config(load_today_ignore=True, models=(("bat1", "All-in-One"),)))

        self.assertNotIn("load_today", plan.legacy_arguments())
        self.assertTrue(plan.ac_coupled)
        with self.assertRaises(GECloudAutoConfigError):
            compile_gecloud_auto_config(config(batteries=()))


if __name__ == "__main__":
    unittest.main()
