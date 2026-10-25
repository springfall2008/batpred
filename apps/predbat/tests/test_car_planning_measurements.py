"""Meter scaling uses native conversion without changing observation freshness."""

from copy import deepcopy
from datetime import datetime, timezone
from datetime import timedelta
import unittest

from car_charging_measurements import normalise_car_meter


class CarPlanningMeasurementTests(unittest.TestCase):
    """Synthetic cached telemetry only; no physical reads or writes."""

    def setUp(self):
        """Use the real Predbat converter on the repository's mocked HA fixture."""
        from unit_test import create_predbat

        self.base = create_predbat()
        self.stamp = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def observation(self, value, units):
        """Build one exact cached publication whose timestamps must survive normalisation."""
        return {"state": value, "attributes": {"unit_of_measurement": units}, "last_changed": self.stamp, "last_updated": self.stamp}

    def test_native_energy_and_power_scaling(self):
        """Compatible SI units represent the same physical energy and power."""
        for state, units, required, expected in (("1500", "Wh", "kWh", 1.5), ("1.5", "kWh", "kWh", 1.5), ("0.0015", "MWh", "kWh", 1.5), ("3500", "W", "kW", 3.5), ("0.0035", "MW", "kW", 3.5)):
            original = self.observation(state, units)
            before = deepcopy(original)
            result = normalise_car_meter(self.base, original, required)
            self.assertAlmostEqual(result["state"], expected)
            self.assertEqual(result["attributes"]["unit_of_measurement"], required)
            self.assertEqual(result["last_changed"], self.stamp)
            self.assertEqual(result["last_updated"], self.stamp)
            self.assertEqual(original, before)

    def test_wrong_quantity_and_invalid_values_fail(self):
        """A current/percentage/missing measurement cannot masquerade as an energy meter."""
        for state, units in (("1", "A"), ("1", "%"), ("1", ""), (True, "Wh"), ("unavailable", "Wh"), (float("nan"), "Wh")):
            with self.assertRaises(ValueError):
                normalise_car_meter(self.base, self.observation(state, units), "kWh")

    def test_normalised_meter_keeps_one_physical_baseline(self):
        """Wh/W telemetry reaches the real session path without rebasing or fabricated energy."""
        from car_charging_recurring import resolve_recurring_session
        from tests.test_car_planning_recurring import recurring_inputs
        from tests.test_car_planning_actual import _set_entity

        recurring_inputs(self.base)
        start = self.base.now_utc
        _set_entity(self.base, "sensor.test_ev_energy", "100000", attributes={"unit_of_measurement": "Wh"})
        _set_entity(self.base, "sensor.test_ev_power", "0", attributes={"unit_of_measurement": "W"})
        first = resolve_recurring_session(self.base, now=start)
        self.assertEqual(first["status"], "ready", first)
        # A different unit representation of unchanged energy is not progress.
        stamp = start + timedelta(minutes=1)
        _set_entity(self.base, "sensor.test_ev_energy", "100.0", last_changed=stamp, last_updated=stamp, attributes={"unit_of_measurement": "kWh"})
        same = resolve_recurring_session(self.base, now=stamp)
        self.assertEqual(same["identity"], first["identity"])
        self.assertEqual(same["observation"]["ev_metered_ac_kwh"], 0)
        stamp = start + timedelta(minutes=5)
        _set_entity(self.base, "sensor.test_ev_energy", "100500", last_changed=stamp, last_updated=stamp, attributes={"unit_of_measurement": "Wh"})
        delivered = resolve_recurring_session(self.base, now=stamp)
        self.assertEqual(delivered["status"], "ready", delivered)
        self.assertEqual(delivered["identity"], first["identity"])
        self.assertAlmostEqual(delivered["observation"]["ev_metered_ac_kwh"], 0.5)

    def test_meter_publication_cadence_is_an_explicit_device_setting(self):
        """A slower configured meter does not silently inherit another device's timeout."""
        from car_charging_recurring import resolve_recurring_session
        from tests.test_car_planning_recurring import recurring_inputs
        from tests.test_car_planning_actual import _set_entity
        from car_charging_config import read_car_charging_config
        from unit_test import create_predbat

        for configured, expected in ((600, "blocked"), (900, "ready")):
            base = create_predbat()
            recurring_inputs(base)
            base.args["car_charging_meter_stall_seconds"] = configured
            first = resolve_recurring_session(base, now=base.now_utc)
            self.assertEqual(first["status"], "ready")
            settings = read_car_charging_config(base)
            _set_entity(base, settings["relay_entity"], "on", last_changed=base.now_utc, last_updated=base.now_utc)
            result = resolve_recurring_session(base, now=base.now_utc + timedelta(seconds=700))
            self.assertEqual(result["status"], expected, result)
            if expected == "blocked":
                self.assertEqual(result["reason"], "recurring_meter_stopped")

    def test_migrated_real_session_retains_identity_delivery_and_faults(self):
        """The runtime consumes copied physical evidence without silently starting over."""
        import asyncio
        from types import SimpleNamespace
        from car_charging_recurring import resolve_recurring_session
        from car_charging_storage import migrate_car_storage
        from tests.test_car_planning_storage import MemoryCarStorage
        from tests.test_car_planning_recurring import recurring_inputs
        from tests.test_car_planning_actual import _set_entity

        _, original = recurring_inputs(self.base)
        start = self.base.now_utc
        first = resolve_recurring_session(self.base, now=start)
        self.assertEqual(first["status"], "ready", first)
        later = start + timedelta(minutes=5)
        _set_entity(self.base, "sensor.test_ev_energy", "100.5", last_changed=later, last_updated=later, attributes={"unit_of_measurement": "kWh"})
        delivered = resolve_recurring_session(self.base, now=later)
        self.assertEqual(delivered["identity"], first["identity"])
        storage = MemoryCarStorage()
        source = deepcopy(original.records[("car_charging", "recurring_session")])
        storage.records = {("previous_planner", "recurring_session"): source}
        asyncio.run(migrate_car_storage(storage, "previous_planner"))
        self.base.components = SimpleNamespace(get_component=lambda name: storage if name == "storage" else None)
        restored = resolve_recurring_session(self.base, now=later)
        self.assertEqual(restored["identity"], first["identity"])
        self.assertEqual(restored["observation"]["ev_metered_ac_kwh"], 0.5)
        self.assertEqual(storage.records[("car_charging", "recurring_session")]["session"]["meter_baseline"], 100)
        self.assertEqual(storage.records[("previous_planner", "recurring_session")], source)
        # Faults are copied as evidence, never cleared merely by changing module.
        source["invalid"] = "recurring_meter_contradiction"
        storage.records = {("previous_planner", "recurring_session"): source}
        asyncio.run(migrate_car_storage(storage, "previous_planner"))
        blocked = resolve_recurring_session(self.base, now=later)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "recurring_meter_contradiction")
        self.assertEqual(storage.records[("car_charging", "recurring_session")]["session"]["identity"], first["identity"])

    def test_measurement_policy_changes_invalidate_execution_fingerprint(self):
        """A staged command cannot retain authority under a changed device tolerance."""
        from unittest.mock import patch
        from car_charging_execute import execution_fingerprint
        from tests.test_car_planning_recurring import recurring_inputs

        recurring_inputs(self.base)
        with patch("car_charging_plan._observation_clock", return_value=self.base.now_utc):
            before = execution_fingerprint(self.base)
            self.base.args["car_charging_meter_energy_tolerance_kwh"] = 0.2
            self.assertNotEqual(execution_fingerprint(self.base), before)


def run_car_planning_measurements_tests(base):
    """Expose native conversion coverage through the ordinary registry."""
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CarPlanningMeasurementTests))
    return not result.wasSuccessful()
