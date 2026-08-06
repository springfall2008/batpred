# cspell:ignore autoconfig
"""Focused tests for the default-off live Lattice auto-config coordinator."""

import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lattice_autoconfig import ProviderHealth  # noqa: E402
from lattice_autoconfig_runtime import LatticeAutoConfigRuntime  # noqa: E402
from lattice_fragment_adapters import FragmentAdapterConflict  # noqa: E402
from tests.test_lattice_provider_inputs import inverter, status  # noqa: E402


class MemoryAsyncStorage:
    """Small thread-safe async storage double for durable journal tests."""

    def __init__(self):
        self.values = {}
        self._lock = threading.RLock()
        self.fail_save = False

    async def load(self, module, filename):
        """Return a detached-enough stored JSON value."""
        with self._lock:
            return self.values.get((module, filename))

    async def save(self, module, filename, value, **_kwargs):
        """Store one complete journal envelope."""
        if self.fail_save:
            raise OSError("injected durable write failure")
        with self._lock:
            self.values[(module, filename)] = value
        return True

    async def age(self, module, filename):
        """Distinguish absent keys from unreadable values."""
        with self._lock:
            return 0 if (module, filename) in self.values else None


class RuntimeBase:
    """Minimal PredBat surface mutated by the runtime."""

    def __init__(self):
        self.args = {}
        self.prefix = "predbat"
        self.update_pending = False
        self.plan_valid = True
        self.inverters = [object()]
        self.messages = []

    def log(self, message):
        """Capture diagnostics without printing test output."""
        self.messages.append(message)


def ge_devices(serial="ser123"):
    """Return one complete GE discovery input."""
    return {
        "battery": [serial],
        "pv": [],
        "gateway": None,
        "ems": None,
        "battery_meters": {serial: ["meter1"]},
    }


def ge_settings(serial="ser123"):
    """Return minimum direct-control register discovery."""
    return {
        serial: {
            1: {"name": "Battery Charge Power"},
            2: {"name": "Battery Discharge Power"},
            3: {"name": "Battery Reserve Percent Limit"},
        }
    }


class TestLatticeAutoConfigRuntime(unittest.TestCase):
    """Durable publication is separate from atomic config activation."""

    def test_disabled_runtime_has_no_storage_requirement_or_generated_values(self):
        base = RuntimeBase()

        runtime = LatticeAutoConfigRuntime(base, None, enabled=False)

        self.assertFalse(runtime.enabled)
        self.assertFalse(base.lattice_generated_overlay.snapshot.enabled)
        self.assertEqual(runtime.bind(gateway_enabled=True), ())

    def test_gateway_publication_is_queued_then_applied_as_one_overlay(self):
        base = RuntimeBase()
        runtime = LatticeAutoConfigRuntime(
            base,
            MemoryAsyncStorage(),
            enabled=True,
        )
        self.assertEqual(
            runtime.bind(gateway_enabled=True),
            ("predbat-gateway",),
        )

        plan, result = runtime.ingest_gateway_status(
            status(inverter("SER123")),
        )

        self.assertTrue(result.accepted)
        self.assertTrue(result.staged)
        self.assertFalse(base.lattice_generated_overlay.snapshot.enabled)
        self.assertTrue(base.update_pending)
        self.assertTrue(runtime.apply_pending())
        self.assertEqual(base.inverters, [])
        self.assertEqual(
            base.lattice_generated_overlay.read("battery_power"),
            (True, ["sensor.predbat_gateway_ser123_battery_power"]),
        )
        self.assertEqual(plan.selected_serials, ("SER123",))

    def test_running_gateway_to_cloud_failover_replaces_the_overlay(self):
        base = RuntimeBase()
        runtime = LatticeAutoConfigRuntime(
            base,
            MemoryAsyncStorage(),
            enabled=True,
        )
        runtime.bind(gateway_enabled=True, gecloud_enabled=True)
        runtime.ingest_gecloud_state(
            ge_devices(),
            ge_settings(),
            {"ser123": {"info": {"model": "AC 3ph"}}},
        )
        runtime.ingest_gateway_status(status(inverter("SER123")))
        self.assertTrue(runtime.apply_pending())
        self.assertEqual(
            base.lattice_generated_overlay.read("battery_power")[1],
            ["sensor.predbat_gateway_ser123_battery_power"],
        )

        result = runtime.set_gateway_liveness(False)

        self.assertTrue(result.accepted)
        self.assertTrue(result.staged)
        self.assertTrue(runtime.apply_pending())
        self.assertEqual(
            base.lattice_generated_overlay.read("battery_power")[1],
            ["sensor.predbat_gecloud_ser123_battery_power"],
        )

    def test_pending_gateway_config_is_cancelled_if_provider_fails_before_apply(self):
        base = RuntimeBase()
        runtime = LatticeAutoConfigRuntime(base, MemoryAsyncStorage(), enabled=True)
        runtime.bind(gateway_enabled=True)
        runtime.ingest_gateway_status(status(inverter("SER123")))

        invalidated = runtime.set_gateway_liveness(False)

        self.assertFalse(invalidated.accepted)
        self.assertFalse(runtime.apply_pending())
        self.assertFalse(base.lattice_generated_overlay.snapshot.enabled)

    def test_rejection_cancels_pending_even_if_offline_persistence_fails(self):
        base = RuntimeBase()
        storage = MemoryAsyncStorage()
        runtime = LatticeAutoConfigRuntime(base, storage, enabled=True)
        runtime.bind(gateway_enabled=True)
        runtime.ingest_gateway_status(status(inverter("SER123")))
        storage.fail_save = True

        with self.assertRaises(FragmentAdapterConflict):
            runtime.set_gateway_liveness(False)

        self.assertFalse(runtime.apply_pending())
        self.assertFalse(base.lattice_generated_overlay.snapshot.enabled)

    def test_applied_overlay_is_removed_when_sole_provider_goes_offline(self):
        base = RuntimeBase()
        runtime = LatticeAutoConfigRuntime(base, MemoryAsyncStorage(), enabled=True)
        runtime.bind(gateway_enabled=True)
        runtime.ingest_gateway_status(status(inverter("SER123")))
        self.assertTrue(runtime.apply_pending())

        invalidated = runtime.set_gateway_liveness(False)

        self.assertFalse(invalidated.accepted)
        self.assertTrue(runtime.apply_pending())
        self.assertFalse(base.lattice_generated_overlay.snapshot.enabled)

    def test_return_to_applied_provider_cancels_unapplied_failover(self):
        base = RuntimeBase()
        runtime = LatticeAutoConfigRuntime(base, MemoryAsyncStorage(), enabled=True)
        runtime.bind(gateway_enabled=True, gecloud_enabled=True)
        runtime.ingest_gecloud_state(
            ge_devices(),
            ge_settings(),
            {"ser123": {"info": {"model": "Hybrid"}}},
        )
        runtime.ingest_gateway_status(status(inverter("SER123")))
        self.assertTrue(runtime.apply_pending())

        runtime.set_gateway_liveness(False)
        runtime.ingest_gateway_status(status(inverter("SER123")))

        self.assertFalse(runtime.apply_pending())
        self.assertEqual(
            base.lattice_generated_overlay.read("battery_power")[1],
            ["sensor.predbat_gateway_ser123_battery_power"],
        )

    def test_restart_does_not_activate_cached_plan_before_fresh_provider_read(self):
        storage = MemoryAsyncStorage()
        first_base = RuntimeBase()
        first = LatticeAutoConfigRuntime(first_base, storage, enabled=True)
        first.bind(gateway_enabled=True)
        first.ingest_gateway_status(status(inverter("SER123")))
        self.assertTrue(first.apply_pending())

        restarted_base = RuntimeBase()
        restarted = LatticeAutoConfigRuntime(
            restarted_base,
            storage,
            enabled=True,
        )
        self.assertEqual(
            restarted.gateway_publisher.read_snapshot().health,
            ProviderHealth.OFFLINE,
        )
        restarted.bind(gateway_enabled=True)

        retained_online = restarted.set_gateway_liveness(True)
        self.assertFalse(retained_online.accepted)
        self.assertFalse(restarted.apply_pending())

        stale = restarted.reconcile()

        self.assertFalse(stale.accepted)
        self.assertFalse(restarted.apply_pending())
        self.assertFalse(restarted_base.lattice_generated_overlay.snapshot.enabled)
        _plan, fresh = restarted.ingest_gateway_status(status(inverter("SER123")))
        self.assertTrue(fresh.accepted)
        self.assertTrue(restarted.apply_pending())

    def test_restart_cloud_liveness_cannot_promote_cached_config(self):
        storage = MemoryAsyncStorage()
        first = LatticeAutoConfigRuntime(RuntimeBase(), storage, enabled=True)
        first.bind(gecloud_enabled=True)
        first.ingest_gecloud_state(
            ge_devices(),
            ge_settings(),
            {"ser123": {"info": {"model": "Hybrid"}}},
        )

        base = RuntimeBase()
        restarted = LatticeAutoConfigRuntime(base, storage, enabled=True)
        restarted.bind(gecloud_enabled=True)

        degraded = restarted.set_gecloud_liveness(None)

        self.assertFalse(degraded.accepted)
        self.assertFalse(restarted.apply_pending())
        self.assertFalse(base.lattice_generated_overlay.snapshot.enabled)

    def test_component_stop_requires_new_gateway_telemetry_after_retained_online(self):
        base = RuntimeBase()
        runtime = LatticeAutoConfigRuntime(base, MemoryAsyncStorage(), enabled=True)
        runtime.bind(gateway_enabled=True)
        runtime.ingest_gateway_status(status(inverter("SER123")))
        self.assertTrue(runtime.apply_pending())

        runtime.set_gateway_liveness(False)
        retained = runtime.set_gateway_liveness(True)

        self.assertFalse(retained.accepted)
        self.assertTrue(runtime.apply_pending())
        self.assertFalse(base.lattice_generated_overlay.snapshot.enabled)

    def test_component_stop_requires_new_ge_poll_after_degraded_health(self):
        base = RuntimeBase()
        runtime = LatticeAutoConfigRuntime(base, MemoryAsyncStorage(), enabled=True)
        runtime.bind(gecloud_enabled=True)
        runtime.ingest_gecloud_state(
            ge_devices(),
            ge_settings(),
            {"ser123": {"info": {"model": "Hybrid"}}},
        )
        self.assertTrue(runtime.apply_pending())

        runtime.set_gecloud_liveness(False)
        degraded = runtime.set_gecloud_liveness(None)

        self.assertFalse(degraded.accepted)
        self.assertTrue(runtime.apply_pending())
        self.assertFalse(base.lattice_generated_overlay.snapshot.enabled)

    def test_ac_coupled_hardware_fact_is_materialized_authoritatively(self):
        base = RuntimeBase()
        runtime = LatticeAutoConfigRuntime(base, MemoryAsyncStorage(), enabled=True)
        runtime.bind(gecloud_enabled=True)

        runtime.ingest_gecloud_state(
            ge_devices(),
            ge_settings(),
            {"ser123": {"info": {"model": "All-in-One"}}},
        )

        self.assertTrue(runtime.apply_pending())
        self.assertEqual(base.lattice_generated_overlay.read("inverter_hybrid"), (True, False))
        self.assertTrue(base.lattice_generated_overlay.authoritative("inverter_hybrid"))


if __name__ == "__main__":
    unittest.main()
