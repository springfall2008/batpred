# cspell:ignore autoconfig xiaozhi XIAO BATONE PVONE
"""Focused tests for pure live provider input adapters."""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gateway_autoconfig import compile_gateway_auto_config  # noqa: E402
from gecloud_autoconfig import compile_gecloud_auto_config  # noqa: E402
from lattice_provider_inputs import (  # noqa: E402
    LiveProviderInputError,
    gateway_status_to_lattice_input,
    gecloud_state_to_lattice_input,
)
from lattice_topology import decode_topology  # noqa: E402


class ProtoMessage:
    """Small protobuf-like object with explicit message presence."""

    def __init__(self, present=True, **values):
        self._present = present
        for name, value in values.items():
            setattr(self, name, value)

    def ByteSize(self):
        """Return protobuf-like presence, independent of field values."""
        return 1 if self._present else 0


def inverter(
    serial="CE123456789",
    inverter_type=6,
    primary=True,
    capacity_wh=9500,
    model="Hybrid 5.0",
):
    """Build one XIAO Gateway inverter telemetry entry."""
    return SimpleNamespace(
        serial=serial,
        type=inverter_type,
        primary=primary,
        battery=ProtoMessage(capacity_wh=capacity_wh),
        ems=ProtoMessage(present=False, num_inverters=0),
        model=model,
    )


def status(*inverters, ev_chargers=(), timestamp=0):
    """Build one proto-like Gateway status frame."""
    return SimpleNamespace(
        inverters=inverters,
        ev_chargers=ev_chargers,
        timestamp=timestamp,
    )


class TestGatewayLiveInput(unittest.TestCase):
    """Gateway telemetry is detached without live component side effects."""

    def test_xiaozhi_single_aio_compiles_and_seeds_valid_topology(self):
        live = gateway_status_to_lattice_input(status(inverter()), document_version=4)
        plan = compile_gateway_auto_config(live.inverters)
        document = decode_topology(live.topology_fragment)

        self.assertEqual(plan.selected_serials, ("CE123456789",))
        self.assertEqual(plan.arguments["inverter_type"], ("GWMQTT",))
        self.assertEqual(plan.arguments["soc_max"], ("sensor.predbat_gateway_456789_battery_capacity",))
        self.assertEqual(document["docVersion"], 4)
        self.assertEqual(document["nodes"][0]["id"], "gateway:CE123456789")
        self.assertEqual(document["nodes"][0]["attributes"]["serial"], "CE123456789")

    def test_topology_is_order_stable_and_version_is_external_metadata(self):
        first = gateway_status_to_lattice_input(
            status(inverter("B"), inverter("A"), timestamp=100),
            document_version=1,
        )
        second = gateway_status_to_lattice_input(
            status(inverter("A"), inverter("B"), timestamp=999),
            document_version=9,
        )
        first_document = dict(first.topology_fragment)
        second_document = dict(second.topology_fragment)
        first_document.pop("docVersion")
        second_document.pop("docVersion")

        self.assertEqual(first_document, second_document)
        self.assertEqual(
            [node["id"] for node in first_document["nodes"]],
            ["gateway:A", "gateway:B"],
        )

    def test_ems_ev_and_duplicate_serials_fail_closed(self):
        with self.assertRaisesRegex(LiveProviderInputError, "EMS"):
            gateway_status_to_lattice_input(status(inverter(inverter_type=7)))
        with self.assertRaisesRegex(LiveProviderInputError, "EV"):
            gateway_status_to_lattice_input(
                status(inverter(), ev_chargers=(SimpleNamespace(),)),
            )
        with self.assertRaisesRegex(LiveProviderInputError, "duplicate"):
            gateway_status_to_lattice_input(status(inverter("same"), inverter("SAME")))


class TestGECloudLiveInput(unittest.TestCase):
    """GE discovery, register state, and flags map to existing contracts."""

    def test_registers_models_meters_and_flags_are_detached(self):
        live = gecloud_state_to_lattice_input(
            devices={
                "battery": ["BatOne"],
                "pv": ["PvOne"],
                "gateway": None,
                "ems": None,
                "battery_meters": {"BatOne": [12345, 98765]},
            },
            settings={
                "BatOne": {
                    1: {"name": "Battery Charge Power"},
                    2: {"name": "Battery Reserve % Limit"},
                    3: {"name": "Enable-AC-Charge"},
                },
                "PvOne": {},
            },
            info={
                "BatOne": {
                    "info": {"model": "All-In-One 6.0"},
                    "site_id": 321,
                    "uuid": "battery-uuid",
                },
                "PvOne": {"info": {"model": "GIV-PV"}, "site_id": 321},
            },
            prefix="site",
            load_today_ignore=True,
            split_pv=True,
            split_ct=True,
            shared_ct=False,
        )
        config = live.auto_config
        plan = compile_gecloud_auto_config(config)

        self.assertEqual(config.batteries, ("BatOne",))
        self.assertEqual(config.battery_meters, (("BatOne", (12345, 98765)),))
        self.assertEqual(config.models, (("BatOne", "All-In-One 6.0"),))
        self.assertEqual(
            dict(config.register_names)["BatOne"],
            (
                "battery_charge_power",
                "battery_reserve_percent_limit",
                "enable_ac_charge",
            ),
        )
        self.assertTrue(config.load_today_ignore)
        self.assertTrue(config.split_pv)
        self.assertTrue(config.split_ct)
        self.assertFalse(config.shared_ct)
        self.assertNotIn("load_today", plan.legacy_arguments())
        self.assertTrue(plan.ac_coupled)
        self.assertEqual(plan.pv_sources, ("BatOne", "PvOne"))
        self.assertEqual(
            [(item.serial, item.kind, item.model, item.site_id) for item in live.devices],
            [
                ("BATONE", "battery-inverter", "All-In-One 6.0", "321"),
                ("PVONE", "pv-inverter", "GIV-PV", "321"),
            ],
        )

    def test_gateway_registers_and_shared_meter_inputs_are_included(self):
        live = gecloud_state_to_lattice_input(
            devices={
                "battery": ["bat2", "bat1"],
                "pv": [],
                "gateway": "gw1",
                "ems": None,
                "battery_meters": {
                    "bat1": [44],
                    "bat2": [44],
                },
            },
            settings={
                "bat1": {},
                "bat2": {},
                "gw1": {1: {"name": "Charge Power Rate"}},
            },
            info={
                "bat1": {"info": {"model": "Hybrid"}},
                "bat2": {"info": {"model": "Hybrid"}},
                "gw1": {"info": {"model": "Gateway"}},
            },
            shared_ct=True,
        )
        config = live.auto_config
        plan = compile_gecloud_auto_config(config)

        self.assertEqual(dict(config.register_names)["gw1"], ("charge_power_rate",))
        self.assertEqual(plan.primary_targets, ("gw1",))
        self.assertEqual(plan.arguments[0], ("inverter_type", ("GEC",)))
        self.assertTrue(config.shared_ct)

    def test_missing_batteries_and_non_boolean_flags_fail_closed(self):
        with self.assertRaisesRegex(LiveProviderInputError, "no batteries"):
            gecloud_state_to_lattice_input({}, {}, {})
        with self.assertRaisesRegex(LiveProviderInputError, "split_pv"):
            gecloud_state_to_lattice_input(
                {"battery": ["bat"], "battery_meters": {}},
                {"bat": {}},
                {},
                split_pv="true",
            )


if __name__ == "__main__":
    unittest.main()
