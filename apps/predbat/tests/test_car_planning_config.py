"""Native argument contracts and migration without any physical device access."""

from types import SimpleNamespace
import unittest

from car_charging_config import measurement_policy, native_car_arguments, read_car_charging_config


class CarPlanningConfigTests(unittest.TestCase):
    """Exercise only public native arguments and explicit migration inputs."""

    def test_default_and_explicit_off_are_inert(self):
        """Ordinary native car fields cannot opt a household into extra execution."""
        for args in ({}, {"car_charging_soc": ["sensor.example_soc"]}, {"car_charging_plan_mode": "off"}):
            self.assertIsNone(read_car_charging_config(SimpleNamespace(args=args)))

    def test_flat_arguments_use_native_car_order(self):
        """Read car zero mappings without declaring multi-car execution supported."""
        args = {"car_charging_plan_mode": "shadow", "car_charging_plugged": ["sensor.first_plug", "sensor.second_plug"], "import_limit": 5000, "load_limit": 12000}
        settings = read_car_charging_config(SimpleNamespace(args=args))
        self.assertEqual(settings["connection_entity"], "sensor.first_plug")
        self.assertEqual(settings["import_limit_w"], 5000)
        self.assertEqual(settings["load_port_limit_w"], 12000)
        self.assertEqual(settings["soc_entity_unit"], "%")
        self.assertNotIn("settings", args)

    def test_migration_preserves_values_and_saved_window(self):
        """A supplied saved choice must not be silently replaced with a new-install default."""
        previous = {"mode": "apply", "connection_entity": "sensor.example_plug", "recurring_session": {"enabled": True}}
        migrated = native_car_arguments(previous, battery_window_minutes=5)
        self.assertEqual(migrated["battery_min_window_minutes"], 5)
        self.assertEqual(migrated["car_charging_plugged"], previous["connection_entity"])
        self.assertEqual(previous["recurring_session"], migrated["car_charging_recurring_session"])
        migrated["car_charging_recurring_session"]["enabled"] = False
        self.assertTrue(previous["recurring_session"]["enabled"])

    def test_unknown_and_conflicting_migration_input_fails(self):
        """Bad input must not become a partially understood active configuration."""
        for previous, window in (({"unknown": True}, None), ({"battery_min_window_minutes": 30}, 5)):
            with self.assertRaises(ValueError):
                native_car_arguments(previous, window)

    def test_device_policy_is_configurable_and_bounded(self):
        """Device cadence/accuracy is configurable but malformed or excessive values fail closed."""
        base = SimpleNamespace(args={"car_charging_meter_stall_seconds": 900, "car_charging_soc_recent_seconds": 240, "car_charging_meter_energy_tolerance_kwh": 0.2})
        policy = measurement_policy(base)
        self.assertEqual(policy["meter_stall_seconds"], 900)
        self.assertEqual(policy["soc_recent_seconds"], 240)
        self.assertEqual(policy["meter_energy_tolerance_kwh"], 0.2)
        for value in (True, -1, float("nan"), 100000):
            with self.assertRaises(ValueError):
                measurement_policy(SimpleNamespace(args={"car_charging_meter_stall_seconds": value}))

    def test_native_schema_covers_every_flat_argument(self):
        """Register the adapter contract without replacing established native mappings."""
        import predbat  # Initialise the native config/version import order.
        from config import APPS_SCHEMA
        from car_charging_config import CAR_SETTING_KEYS, MEASUREMENT_DEFAULTS

        self.assertIsNotNone(predbat.THIS_VERSION)
        self.assertFalse(set(CAR_SETTING_KEYS.values()) - set(APPS_SCHEMA))
        self.assertFalse({"car_charging_" + key for key in MEASUREMENT_DEFAULTS} - set(APPS_SCHEMA))
        self.assertEqual(APPS_SCHEMA["car_charging_soc"], {"type": "sensor", "sensor_type": "float", "entries": "num_cars"})
        self.assertEqual(APPS_SCHEMA["import_limit"], {"type": "float", "zero": True})

    def test_native_soc_percentage_default_and_explicit_energy(self):
        """Exercise the native fetch conversion, not a second SOC implementation."""
        from fetch import Fetch

        for units, value, expected in ((None, 25, 10), ("%", 25, 10), ("kWh", 7, 7)):
            args = {"car_charging_soc": value}
            if units is not None:
                args["car_charging_soc_unit"] = units
            base = SimpleNamespace(car_charging_battery_size=[40], get_arg=lambda name, default, **kwargs: args.get(name, default))
            self.assertEqual(Fetch.car_sensor_soc_kwh(base, 0), expected)


def run_car_planning_config_tests(base):
    """Expose unittest cases through the repository's normal test registry."""
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CarPlanningConfigTests))
    return not result.wasSuccessful()
