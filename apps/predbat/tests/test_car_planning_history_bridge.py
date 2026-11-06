"""Fractional live leases must not crash historical replay or fabricate energy."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
import unittest


def _history(slots, load, loss=1, reported=True):
    """Exercise the real historical consumer with no HA service or metered-energy access."""
    from output import Output

    base = SimpleNamespace(
        get_arg=lambda *args, **kwargs: None,
        num_cars=len(slots),
        octopus_slots=[],
        car_charging_slots=deepcopy(slots),
        car_charging_energy={},
        car_energy_reported_load=reported,
        car_charging_loss=loss,
    )
    result = deepcopy(load)
    Output.yesterday_reconstruct_car_slots(base, 1440, result, 0)
    return base.car_charging_slots, result


class CarPlanningHistoryBridgeTests(unittest.TestCase):
    """Pin both native producer integration and independently calculated overlap energy."""

    def test_actual_live_lease_survives_historical_reconstruction(self):
        """A seconds-precision lease must remain valid when the live loop recalculates history."""
        from car_charging_execute import _publish_ev
        from output import Output
        from unit_test import create_predbat
        from tests.test_car_planning_native import _setup

        base = create_predbat()
        _setup(base)
        now = base.now_utc + timedelta(seconds=12, microseconds=345678)
        expires = now + timedelta(seconds=300)
        _publish_ev(base, {"ev_on": True}, now, expires)
        entity = "binary_sensor." + base.prefix + "_car_charging_slot"
        authority = deepcopy(base.dashboard_values[entity])
        slot = deepcopy(base.car_charging_slots[0][0])
        self.assertIsInstance(slot["start"], float)
        self.assertIsInstance(slot["end"], float)
        start = (int(slot["start"]) // 5) * 5
        load = {minute: 2 for minute in range(start, start + 15, 5)}
        base.car_charging_energy = {}
        base.car_energy_reported_load = True
        base.car_charging_loss = 1
        base.args.pop("octopus_intelligent_slot", None)
        result = deepcopy(load)
        Output.yesterday_reconstruct_car_slots(base, 1440 + base.minutes_now, result, base.minutes_now)
        self.assertAlmostEqual(base.car_charging_slots[0][0]["kwh"], slot["kwh"])
        self.assertAlmostEqual(sum(load.values()) - sum(result.values()), slot["kwh"])
        self.assertEqual(base.dashboard_values[entity], authority)
        self.assertEqual(authority["attributes"]["valid_from"], now.isoformat())
        self.assertEqual(authority["attributes"]["valid_until"], expires.isoformat())

    def test_hand_calculated_partial_bucket_energy(self):
        """Overlap energy, not integer truncation or rounded slot length, controls subtraction."""
        examples = (
            (1.5, 6.5, 0.4, {0: 0.22, 5: 0.38, 10: 0.5}),
            (0.0, 5.0, 0.4, {0: 0.1, 5: 0.5, 10: 0.5}),
            (4.5, 5.5, 0.08, {0: 0.46, 5: 0.46, 10: 0.5}),
            (1.1, 1.2, 0.005, {0: 0.495, 5: 0.5, 10: 0.5}),
            (5.0, 10.0, 0.4, {0: 0.5, 5: 0.1, 10: 0.5}),
        )
        for start, end, energy, expected in examples:
            with self.subTest(start=start, end=end):
                slots, result = _history([[{"start": start, "end": end, "kwh": energy}]], {0: 0.5, 5: 0.5, 10: 0.5})
                self.assertEqual(slots[0][0]["kwh"], energy)
                self.assertEqual(set(result), set(expected))
                for minute, value in expected.items():
                    self.assertAlmostEqual(result[minute], value)
                self.assertAlmostEqual(1.5 - sum(result.values()), energy)

    def test_excess_energy_is_capped_only_by_overlapping_load(self):
        """Energy outside the interval cannot justify a larger historical car allocation."""
        slot = {"start": 1.5, "end": 6.5, "kwh": 2}
        slots, result = _history([[slot]], {0: 0.5, 5: 0.5, 10: 100}, loss=0.9)
        self.assertAlmostEqual(slots[0][0]["kwh"], 0.45)
        self.assertAlmostEqual(result[0], 0.185)
        self.assertAlmostEqual(result[5], 0.365)
        self.assertEqual(result[10], 100)
        self.assertTrue(all(value >= 0 for value in result.values()))

    def test_missing_load_and_large_contradictions_do_not_invent_delivery(self):
        """An absent/near-zero historical band rejects claimed energy without crashing."""
        for load in ({}, {0: 0, 5: 0}, {0: 0.001, 5: 0.001}):
            with self.subTest(load=load):
                slots, result = _history([[{"start": 1.5, "end": 6.5, "kwh": 10}]], load)
                self.assertEqual(slots[0][0]["kwh"], 0)
                self.assertEqual(result, load)

    def test_disabled_subtraction_and_empty_slots_are_noops(self):
        """A disabled energy mapping and empty/reversed intervals leave the load unchanged."""
        load = {0: 0.5, 5: 0.5}
        for start, end, reported in ((1.5, 6.5, False), (1.5, 1.5, True), (6.5, 1.5, True)):
            with self.subTest(start=start, end=end, reported=reported):
                slot = {"start": start, "end": end, "kwh": 0.4}
                slots, result = _history([[slot]], load, reported=reported)
                self.assertEqual(result, load)
                self.assertEqual(slots, [[slot]])

    def test_multiple_cars_keep_independent_fractional_intervals(self):
        """Disjoint partial sessions retain both allocations without creating off-grid buckets."""
        slots, result = _history([[{"start": 1.5, "end": 6.5, "kwh": 0.4}], [{"start": 6.5, "end": 11.5, "kwh": 0.2}]], {0: 1, 5: 1, 10: 1})
        self.assertAlmostEqual(sum(slot["kwh"] for car in slots for slot in car), 0.6)
        self.assertAlmostEqual(sum(result.values()), 2.4)
        self.assertEqual(set(result), {0, 5, 10})

    def test_uneven_or_missing_buckets_cannot_add_back_unremoved_energy(self):
        """Per-bucket clamps must reconcile the slot to energy actually removed from history."""
        for load in ({0: 0, 5: 1}, {5: 1}):
            with self.subTest(load=load):
                slots, result = _history([[{"start": 1.5, "end": 6.5, "kwh": 0.4}]], load)
                removed = sum(load.values()) - sum(result.values())
                self.assertAlmostEqual(slots[0][0]["kwh"], removed)
                self.assertAlmostEqual(slots[0][0]["kwh"], 0.09)
                self.assertAlmostEqual(result[5], 0.91)
                self.assertEqual(set(result), set(load))

    def test_partial_bucket_cannot_borrow_energy_outside_the_interval(self):
        """Clamping uses only the covered fraction of a bucket, not all its available energy."""
        slots, result = _history([[{"start": 4.5, "end": 5.5, "kwh": 0.05}]], {0: 0.01, 5: 0.9})
        self.assertAlmostEqual(result[0], 0.009)
        self.assertAlmostEqual(result[5], 0.875)
        self.assertAlmostEqual(slots[0][0]["kwh"], 0.026)
        self.assertAlmostEqual(sum(result.values()) + slots[0][0]["kwh"], 0.91)


def run_car_planning_history_bridge_tests(base):
    """Run the live-publisher/history integration regression through the native registry."""
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CarPlanningHistoryBridgeTests))
    return not result.wasSuccessful()
