"""Test the portable electrical acceptance matrix without a live HA installation."""

from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
import unittest


def _runner():
    """Load only the maintained test generator, never a controller or a live fixture."""
    name = "car_executor_acceptance_tests"
    if name not in sys.modules:
        path = Path(__file__).resolve().parents[3] / "tools" / "test_car_charging_executor.py"
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class ExecutorContractTests(unittest.TestCase):
    """Protect matrix breadth, boundary oracles, explicit configuration and purity."""

    def setUp(self):
        """Use synthetic limits that are not a copy of a household configuration."""
        self.runner = _runner()
        self.limits = dict(grid_cap_w=5000, load_cap_w=14000, battery_discharge_w=7000, ev_rate_w=6500, stop_percent=12, restart_percent=22, cooldown_seconds=600, measurement_seconds=180)

    def test_matrix_has_real_acceptances_rejections_and_unique_cases(self):
        """Many cases must exercise both ON/OFF paths, not repeat the same outcome."""
        cases = self.runner.executor_cases(self.limits)
        self.assertGreater(len(cases), 2000)
        self.assertEqual(len(cases), len({case.name for case in cases}))
        decisions = [self.runner.expected_power_decision(case, self.limits) for case in cases]
        self.assertGreater(sum(decisions), 100)
        self.assertGreater(len(decisions) - sum(decisions), 100)
        self.assertGreater(len({(case.grid_w, case.load_w, case.pv_w) for case in cases}), 100)

    def test_exact_headroom_threshold_and_pv_support(self):
        """One watt either side of a cold-start limit changes the independent decision."""
        case = self.runner.ExecutorCase("boundary", load_w=5500)
        self.assertTrue(self.runner.expected_power_decision(case, self.limits))
        self.assertFalse(self.runner.expected_power_decision(replace(case, load_w=5501), self.limits))
        self.assertTrue(self.runner.expected_power_decision(replace(case, load_w=5501, pv_w=1), self.limits))
        self.assertFalse(self.runner.expected_power_decision(replace(case, load_w=7501, pv_w=10000), self.limits))

    def test_running_is_not_the_same_as_starting(self):
        """Continuation avoids start hysteresis/cooldown but retains the physical stop floor."""
        case = self.runner.ExecutorCase("continuation", powered=True, load_w=13000, battery_percent=20, off_seconds=0)
        self.assertTrue(self.runner.expected_power_decision(case, self.limits))
        self.assertFalse(self.runner.expected_power_decision(replace(case, powered=False), self.limits))
        self.assertFalse(self.runner.expected_power_decision(replace(case, battery_percent=12), self.limits))
        self.assertFalse(self.runner.expected_power_decision(replace(case, load_w=14001), self.limits))

    def test_signed_grid_export_and_emergency_ceiling(self):
        """Characterise the existing emergency guard, not physical peak-shaving enforcement."""
        for grid in (-5000, 0, 5000, 14000):
            self.assertTrue(self.runner.expected_power_decision(self.runner.ExecutorCase("grid", grid_w=grid), self.limits))
        self.assertFalse(self.runner.expected_power_decision(self.runner.ExecutorCase("overload", grid_w=14001), self.limits))

    def test_permission_and_cooldown_boundaries(self):
        """Only exact ON consent can start, and cooldown is inclusive at its boundary."""
        for permission in ("off", "unknown", "unavailable", "true", "ON"):
            self.assertFalse(self.runner.expected_power_decision(self.runner.ExecutorCase("permission", permission=permission), self.limits))
        self.assertFalse(self.runner.expected_power_decision(self.runner.ExecutorCase("early", off_seconds=599.99), self.limits))
        self.assertTrue(self.runner.expected_power_decision(self.runner.ExecutorCase("ready", off_seconds=600), self.limits))

    def test_matrix_retains_expiry_freshness_and_latched_pause_cases(self):
        """Rare evidence failures remain explicit rather than hidden in a large power count."""
        cases = {case.name: case for case in self.runner.executor_cases(self.limits)}
        for name in ("lease_expired", "lease_future", "lease_overlong", "lease_boolean_version", "lease_missing_end", "health_error", "health_stale", "health_future", "reserve_latched_below_restart"):
            self.assertFalse(cases[name].expected, name)
        self.assertTrue(cases["lease_start_inclusive"].expected)
        self.assertFalse(cases["lease_end_exclusive"].expected)
        self.assertTrue(cases["reserve_latched_at_restart"].expected)

    def test_no_default_or_invalid_electrical_contract(self):
        """Missing, non-finite, boolean and inconsistent limits never become test defaults."""
        invalid = [{}, {**self.limits, "unknown": 1}, {**self.limits, "stop_percent": 23}, {**self.limits, "grid_cap_w": 15000}]
        invalid.extend({**self.limits, "ev_rate_w": value} for value in (True, False, None, "6500", 0, -1, float("nan"), float("inf")))
        for limits in invalid:
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                self.runner.executor_cases(limits)

    def test_saved_dictionary_equality_does_not_certify_active_order(self):
        """An incremental HA reload can keep stale order despite an equal saved mapping."""
        bad = {"want_on": "{{ lease_ok }}", "lease_ok": "{{ true }}"}
        fixed = {"lease_ok": "{{ true }}", "want_on": "{{ lease_ok }}"}
        self.assertEqual(bad, fixed)
        self.assertNotEqual(list(bad), list(fixed))
        with self.assertRaises(ValueError):
            self.runner.validate_active_variable_order(fixed, list(bad))
        with self.assertRaises(ValueError):
            self.runner.validate_active_variable_order(fixed, None)
        self.runner.validate_active_variable_order(fixed, list(fixed))


def run_car_executor_contract_tests(base):
    """Expose test-generator contracts through the ordinary Predbat registry."""
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ExecutorContractTests))
    return not result.wasSuccessful()
