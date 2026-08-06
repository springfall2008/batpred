# cspell:ignore autoconfig
"""Cross-provider tests for Gateway and GE Cloud auto-config fragments."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gateway_autoconfig import compile_gateway_auto_config  # noqa: E402
from gecloud_autoconfig import GECloudAutoConfigInput  # noqa: E402
from lattice_autoconfig import (  # noqa: E402
    LatticeAutoConfigCompiler,
    compile_auto_config,
)
from lattice_fragment_adapters import (  # noqa: E402
    InMemoryFragmentAdapterStateStore,
)
from lattice_gateway_fragment import (  # noqa: E402
    GatewayRetainedTopologyFragmentPublisher,
)
from lattice_ge_cloud_fragment import (  # noqa: E402
    GECloudDeviceSnapshot,
    GECloudFragmentPublisher,
)


def gateway_topology(serial):
    """Build one provider-owned retained topology fragment."""
    return {
        "topologyVersion": "0.3.0",
        "scope": "fragment",
        "docVersion": 1,
        "producer": {
            "name": "PredBat Gateway",
            "provider": "predbat-gateway",
            "authority": 10,
        },
        "nodes": [
            {
                "id": "gateway-inverter",
                "kind": "inverter",
                "attributes": {"serial": serial.lower()},
                "accessPaths": [
                    {
                        "id": "gateway-mqtt",
                        "provider": "predbat-gateway",
                        "preference": 10,
                    }
                ],
                "capabilities": [],
            }
        ],
    }


def ge_config(serial):
    """Build one complete single-inverter cloud mapping input."""
    return GECloudAutoConfigInput(
        batteries=(serial.lower(),),
        ems=None,
        gateway=None,
        pv=(),
        battery_meters=((serial.lower(), ("meter1",)),),
        register_names=(
            (
                serial.lower(),
                (
                    "battery_charge_power",
                    "battery_discharge_power",
                    "battery_reserve_percent_limit",
                ),
            ),
        ),
        models=((serial.lower(), "Hybrid"),),
        prefix="predbat",
    )


class TestGatewayGEComposition(unittest.TestCase):
    """Local Gateway candidates outrank correlated cloud candidates."""

    def test_gateway_wins_and_cloud_takes_over_when_gateway_offline(self):
        serial = "SER123"
        gateway = GatewayRetainedTopologyFragmentPublisher(
            "predbat-gateway",
            InMemoryFragmentAdapterStateStore(),
            enabled=True,
        )
        gateway.ingest_retained_topology(
            gateway_topology(serial),
            online=True,
        )
        gateway.ingest_auto_config(
            compile_gateway_auto_config(
                (
                    {
                        "serial": serial,
                        "kind": "inverter",
                        "battery_present": True,
                        "battery_capacity_wh": 9600,
                        "model": "Hybrid",
                    },
                )
            )
        )

        cloud = GECloudFragmentPublisher(
            "ge-cloud",
            InMemoryFragmentAdapterStateStore(),
            enabled=True,
        )
        cloud.ingest_discovery(
            1,
            (
                GECloudDeviceSnapshot(
                    serial=serial.lower(),
                    kind="battery-inverter",
                    model="Hybrid",
                    online=True,
                ),
            ),
            health=True,
        )
        cloud.ingest_auto_config(ge_config(serial))

        compiled = compile_auto_config((gateway.read_snapshot(), cloud.read_snapshot()))
        battery_power = next(item for item in compiled.config_arguments if item.name == "battery_power")

        self.assertEqual(
            compiled.projected_config["battery_power"],
            ("sensor.predbat_gateway_ser123_battery_power",),
        )
        self.assertEqual(
            {candidate.provider_id for candidate in battery_power.candidates},
            {"predbat-gateway", "ge-cloud"},
        )
        self.assertEqual(
            len(tuple(target for target in compiled.primary_targets if target.group == "inverters")),
            1,
        )
        self.assertEqual(
            len(tuple(target for target in compiled.control_targets if target.group == "inverters")),
            1,
        )

        gateway.set_liveness(False)
        fallback = (
            LatticeAutoConfigCompiler(
                {
                    "predbat-gateway": gateway.read_snapshot,
                    "ge-cloud": cloud.read_snapshot,
                }
            )
            .drain()
            .plan
        )
        self.assertEqual(
            fallback.projected_config["battery_power"],
            ("sensor.predbat_gecloud_ser123_battery_power",),
        )

    def test_running_compiler_fails_over_to_cloud_when_gateway_goes_offline(self):
        """The opt-in runtime policy replaces an unavailable local provider."""
        serial = "SER123"
        gateway = GatewayRetainedTopologyFragmentPublisher(
            "predbat-gateway",
            InMemoryFragmentAdapterStateStore(),
            enabled=True,
        )
        gateway.ingest_retained_topology(
            gateway_topology(serial),
            online=True,
        )
        gateway.ingest_auto_config(
            compile_gateway_auto_config(
                (
                    {
                        "serial": serial,
                        "kind": "inverter",
                        "battery_present": True,
                        "battery_capacity_wh": 9600,
                        "model": "Hybrid",
                    },
                )
            )
        )
        cloud = GECloudFragmentPublisher(
            "ge-cloud",
            InMemoryFragmentAdapterStateStore(),
            enabled=True,
        )
        cloud.ingest_discovery(
            1,
            (
                GECloudDeviceSnapshot(
                    serial=serial.lower(),
                    kind="battery-inverter",
                    model="Hybrid",
                    online=True,
                ),
            ),
            health=True,
        )
        cloud.ingest_auto_config(ge_config(serial))
        compiler = LatticeAutoConfigCompiler(
            {
                "predbat-gateway": gateway.read_snapshot,
                "ge-cloud": cloud.read_snapshot,
            },
            atomic_materializer=True,
            allow_provider_failover=True,
        )

        first = compiler.drain()
        self.assertEqual(
            first.plan.projected_config["battery_power"],
            ("sensor.predbat_gateway_ser123_battery_power",),
        )

        self.assertTrue(gateway.set_liveness(False))
        self.assertTrue(
            compiler.invalidate(
                "predbat-gateway",
                gateway.generation,
                "gateway offline",
            )
        )
        fallback = compiler.drain()

        self.assertEqual(
            fallback.plan.projected_config["battery_power"],
            ("sensor.predbat_gecloud_ser123_battery_power",),
        )
        self.assertNotIn(
            "active_provider_unavailable",
            {issue.code for issue in fallback.issues},
        )


if __name__ == "__main__":
    unittest.main()
