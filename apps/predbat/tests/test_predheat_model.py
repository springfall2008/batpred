"""Standalone unit tests for the pure shared PredHeat thermal model."""

from dataclasses import FrozenInstanceError, replace
import unittest

from predheat_model import HeatingConfig, HeatingInput, HeatingState, simulate_heating


class TestHeatingModel(unittest.TestCase):
    """Verify thermal accounting, schedule isolation and invalid-input boundaries."""

    def model_input(self, **changes):
        """Return a one-step model with analytically checkable heat and losses."""
        config = HeatingConfig(heat_max_power=6000, heat_min_power=1000, heat_cop=3, heat_output=0, heat_loss_watts=120, watt_per_degree=4000, heat_volume=100)
        inputs = HeatingInput(config, HeatingState(20, 20, True), (21,), (5,))
        return replace(inputs, **changes)

    def test_energy_and_room_water_balance(self):
        """Six thermal kW at COP three uses one sixth electrical kWh in five minutes."""
        result = simulate_heating(self.model_input())
        interval = result.intervals[0]
        self.assertAlmostEqual(interval.electrical_kwh, 1 / 6)
        self.assertAlmostEqual(interval.heat_generated_kwh, 0.5)
        self.assertEqual(interval.heat_delivered_kwh, 0)
        self.assertAlmostEqual(interval.room_temperature_c, 20 - 120 * 15 / 12 / 4000)
        self.assertAlmostEqual(interval.water_temperature_c, 20 + 6000 / 1.16 / 100 / 12)
        self.assertEqual(result.final_state.room_temperature_c, interval.room_temperature_c)

    def test_fraction_caps_and_stored_heat(self):
        """A zero cap stops generation while radiator heat still transfers from water."""
        original = self.model_input()
        full = simulate_heating(original).intervals[0]
        self.assertEqual(simulate_heating(replace(original, heat_output_fraction=(1,))).intervals[0], full)
        capped = simulate_heating(replace(original, heat_output_fraction=(0.25,))).intervals[0]
        self.assertAlmostEqual(capped.electrical_kwh, full.electrical_kwh / 4)
        config = replace(original.config, heat_output=1200, delta_correction=((0, 0),))
        inhibited = simulate_heating(replace(original, config=config, initial_state=HeatingState(20, 30, True), heat_output_fraction=(0,))).intervals[0]
        self.assertEqual(inhibited.electrical_kwh, 0)
        self.assertEqual(inhibited.heat_generated_kwh, 0)
        self.assertFalse(inhibited.heating_active)
        self.assertTrue(inhibited.heating_on)
        self.assertAlmostEqual(inhibited.heat_delivered_kwh, 0.1)
        self.assertLess(inhibited.water_temperature_c, 30)

    def test_gas_never_becomes_battery_load(self):
        """Gas fuel input remains separate from the electrical trajectory."""
        original = self.model_input()
        interval = simulate_heating(replace(original, config=replace(original.config, mode="gas", heat_cop=1, gas_efficiency=((20, 0.8),)))).intervals[0]
        self.assertEqual(interval.electrical_kwh, 0)
        self.assertAlmostEqual(interval.input_energy_kwh, 0.625)

    def test_hysteresis_and_first_interval(self):
        """The current heating state wins the first step, with hysteresis on later steps."""
        original = self.model_input(initial_state=HeatingState(20, 20, False), target_temperature_c=(22, 22, 10), outdoor_temperature_c=(5, 5, 5))
        result = simulate_heating(original)
        self.assertEqual(tuple(interval.heating_on for interval in result.intervals), (False, True, False))
        self.assertEqual(tuple(interval.minute for interval in result.intervals), (0, 5, 10))
        self.assertEqual(result.intervals[0].electrical_kwh, 0)

    def test_inputs_are_deep_snapshots_and_results_deterministic(self):
        """Caller mutation cannot change a later candidate's physics or schedule."""
        targets = [21, 22]
        curve = [[5, 1]]
        config = HeatingConfig(heat_pump_efficiency=curve)
        inputs = HeatingInput(config, HeatingState(20, 25, True), targets, [5, 5])
        before = simulate_heating(inputs)
        targets[0] = 30
        curve[0][1] = 0
        self.assertEqual(simulate_heating(inputs), before)
        with self.assertRaises(FrozenInstanceError):
            config.heat_cop = 0

    def test_invalid_config(self):
        """Missing, non-finite and physically invalid divisors fail before simulation."""
        for changes in ({"heat_cop": 0}, {"heat_volume": 0}, {"heat_output": -1}, {"flow_temp": None}, {"heat_loss_watts": float("nan")}, {"heat_cop": float("inf")}, {"heat_min_power": 8000}, {"heat_pump_efficiency": ((0, 0),)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                HeatingConfig(**changes)

    def test_invalid_schedules(self):
        """Every interval needs finite temperatures and an optional bounded output cap."""
        for changes in (
            {"target_temperature_c": ()},
            {"outdoor_temperature_c": ()},
            {"outdoor_temperature_c": (None,)},
            {"target_temperature_c": (float("inf"),)},
            {"heat_output_fraction": (float("nan"),)},
            {"heat_output_fraction": (-0.1,)},
            {"heat_output_fraction": (1.1,)},
            {"heat_output_fraction": ()},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.model_input(**changes)
        with self.assertRaises(ValueError):
            HeatingState(None, 20, False)
        with self.assertRaises(ValueError):
            simulate_heating(None)

    def test_weather_curve_requires_selected_outdoor_key(self):
        """An incomplete enabled weather curve cannot silently invent a flow temperature."""
        original = self.model_input()
        inputs = replace(original, config=replace(original.config, weather_compensation_enabled=True, weather_compensation=((0, 40),)))
        with self.assertRaises(ValueError):
            simulate_heating(inputs)


def run_predheat_model_tests(_my_predbat):
    """Run the pure model suite through Predbat's boolean failure test registry."""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestHeatingModel)
    return not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == "__main__":
    unittest.main()
