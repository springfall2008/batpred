# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Real sensor-fetch cycles pin the accuracy guard's planning-source integration."""

import copy
import tempfile
import unittest
from contextlib import ExitStack
from datetime import timedelta
from unittest.mock import Mock, patch

from utils import is_debug_excluded_key


class GuardFetchTests(unittest.TestCase):
    """Run real config and sensor-fetch methods with only measured input channels replaced."""

    base_fixture = None

    def setUp(self):
        """Keep the shared PredBat and fake HA fixture isolated from every test cycle."""
        self.base = self.base_fixture
        self.saved_state = dict(self.base.__dict__)
        self.saved_ha = copy.deepcopy(self.base.ha_interface.dummy_items)
        self.saved_primary = self.base.ha_interface.db_primary
        self.saved_history = self.base.ha_interface.history_enable
        self.directory = tempfile.TemporaryDirectory(prefix="predbat_guard_fetch_")
        self.addCleanup(self.restore_fixture)
        self.base.CONFIG_ITEMS, self.base.config_index = copy.deepcopy((self.base.CONFIG_ITEMS, self.base.config_index))
        self.base.config_root = self.base.config_root_p = self.directory.name
        self.base.components = None
        self.base.ha_interface.db_primary = False
        self.base.ha_interface.history_enable = False
        self.base.load_forecast_guard = None
        self.base.load_forecast_guard_selected = None
        self.base.args = {
            "prefix": self.base.prefix,
            "load_today": "sensor.guard_test_load_today",
            "load_ml_enable": True,
            "load_ml_source": True,
            "load_ml_guard_mode": "auto",
            "days_previous": [1, 2, 3],
            "days_previous_auto": True,
            "rates_import": [{"rate": 10}],
            "rates_export": [{"rate": 5}],
            "num_cars": 0,
            "num_inverters": 0,
            "forecast_hours": 48,
            "load_power_fill_enable": False,
        }
        self.data = {m: 100 - m * 0.01 for m in range(7201)}
        self.ml = {m: m * 0.02 for m in range(5000)}
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.log = self.stack.enter_context(patch.object(self.base, "log", Mock()))
        self.stack.enter_context(patch.object(self.base, "minute_data_load", side_effect=lambda *a, **k: (self.data.copy(), 5)))
        self.stack.enter_context(patch.object(self.base, "fetch_ml_load_forecast", return_value=self.ml))
        self.history = self.stack.enter_context(patch.object(self.base, "apply_load_ml_forecast_history", Mock()))

    def restore_fixture(self):
        """Restore native config, HA values, history flags and all transient cycle state."""
        self.base.ha_interface.dummy_items = self.saved_ha
        self.base.ha_interface.db_primary = self.saved_primary
        self.base.ha_interface.history_enable = self.saved_history
        self.base.__dict__.clear()
        self.base.__dict__.update(self.saved_state)
        self.directory.cleanup()

    def configure(self, mode="auto", weighted=True):
        """Use the real config reader, including the saved native select that overrides YAML."""
        self.base.args["days_previous_auto"] = weighted
        self.base.expose_config("load_ml_guard_mode", mode)
        self.base.fetch_config_options()

    def future_energy(self, duration=60):
        """Energy that the planning series actually supplies over the next interval."""
        minute = self.base.minutes_now
        return self.base.load_forecast[minute + duration] - self.base.load_forecast[minute]

    def test_auto_saved_cycle_uses_historical_and_forces_replan(self):
        """The complete save path vetoes unqualified ML and leaves a non-empty historical plan."""
        self.configure()
        with patch.object(self.base, "compute_load_forecast_history", wraps=self.base.compute_load_forecast_history) as classic:
            changed = self.base.fetch_sensor_data(save=True)
        self.assertTrue(changed)
        self.assertEqual(self.base.load_forecast_guard_selected, "classic")
        self.assertEqual(classic.call_count, 1)
        self.assertEqual(len(self.base.load_forecast_array), 1)
        self.assertAlmostEqual(self.future_energy(), 0.6)
        self.assertEqual(self.history.call_args.args[1], {})

    def test_non_save_cycle_keeps_the_saved_veto_and_non_empty_historical(self):
        """The production config-then-fetch ordering keeps a comparison run off vetoed ML."""
        self.configure()
        self.base.fetch_sensor_data(save=True)
        guard = self.base.load_forecast_guard
        self.configure()
        self.base.fetch_sensor_data(save=False)
        self.assertIs(self.base.load_forecast_guard, guard)
        self.assertTrue(self.base.load_forecast_only)
        self.assertEqual(len(self.base.load_forecast_array), 1)
        self.assertAlmostEqual(self.future_energy(), 0.6)
        self.assertEqual(self.history.call_args.args[1], {})

    def test_manual_history_filter_runs_once_and_is_used_by_the_plan(self):
        """The counterfactual's filtered data and selected days become the manual fallback inputs."""
        self.configure(weighted=False)
        with patch.object(self.base, "previous_days_modal_filter", wraps=self.base.previous_days_modal_filter) as modal:
            self.assertTrue(self.base.fetch_sensor_data(save=True))
        self.assertEqual(modal.call_count, 1)
        self.assertEqual(self.base.load_forecast_guard_selected, "classic")
        self.assertFalse(self.base.load_forecast_only)
        filtered, days, weights = self.base.load_forecast_guard_classic[1]
        self.assertIs(self.base.load_minutes, filtered)
        self.assertEqual(self.base.days_previous, days)
        self.assertEqual(self.base.days_previous_weight, weights)
        energy, _ = self.base.get_filtered_load_minute(self.base.load_minutes, 0, historical=True, step=5)
        self.assertGreater(energy, 0)
        self.assertTrue(is_debug_excluded_key("load_forecast_guard_classic"))

    def test_historical_cache_is_rebuilt_for_the_next_cycle(self):
        """A new input snapshot cannot reuse the previous cycle's counterfactual or double its load."""
        self.configure()
        self.base.fetch_sensor_data(save=True)
        first = self.base.load_forecast_guard_classic
        guard = self.base.load_forecast_guard
        self.data = {m: 200 - m * 0.02 for m in range(7201)}
        self.configure()
        with patch.object(self.base, "compute_load_forecast_history", wraps=self.base.compute_load_forecast_history) as classic:
            self.base.fetch_sensor_data(save=True)
        self.assertEqual(classic.call_count, 1)
        self.assertIsNot(first, self.base.load_forecast_guard_classic)
        self.assertIs(guard, self.base.load_forecast_guard)
        self.assertAlmostEqual(self.future_energy(), 1.2)

    def test_monitor_keeps_the_configured_neural_forecast(self):
        """Monitoring observes without replacing the plan's configured source."""
        self.configure(mode="monitor")
        self.base.fetch_sensor_data(save=True)
        self.assertEqual(self.base.load_forecast_guard_selected, "ml")
        self.assertAlmostEqual(self.future_energy(), 1.2)
        self.assertIs(self.history.call_args.args[1], self.ml)

    def test_off_does_not_evaluate_the_guard(self):
        """Disabled recording keeps ML and does not build an unused counterfactual."""
        self.configure(mode="off")
        with patch.object(self.base, "apply_load_forecast_guard", wraps=self.base.apply_load_forecast_guard) as guard:
            self.base.fetch_sensor_data(save=True)
        guard.assert_not_called()
        self.assertIsNone(self.base.load_forecast_guard_classic)
        self.assertAlmostEqual(self.future_energy(), 1.2)

    def test_external_forecast_survives_the_complete_veto_path(self):
        """A separate configured source stays in the real planning aggregate exactly once."""
        entity = "sensor.guard_test_external_load"
        self.base.args["load_forecast"] = entity + "$results"
        data = {(self.base.midnight_utc + timedelta(minutes=m)).isoformat(): m * 0.003 for m in range(0, 5000, 5)}
        self.base.set_state_wrapper(entity, state="active", attributes={"results": data})
        self.configure()
        self.base.fetch_sensor_data(save=True)
        self.assertEqual(self.base.load_forecast_guard_selected, "classic")
        self.assertEqual(len(self.base.load_forecast_array), 2)
        self.assertAlmostEqual(self.future_energy(), 0.78, places=3)

    def test_monitoring_counterfactual_is_hourly_and_never_reused_as_a_new_snapshot(self):
        """ML planning does no extra historical calculation between actual hourly captures."""
        self.configure(mode="monitor")
        with patch.object(self.base, "compute_load_forecast_history", wraps=self.base.compute_load_forecast_history) as classic:
            self.base.fetch_sensor_data(save=True)
            first_count = len(self.base.load_forecast_guard.records)
            self.assertEqual(classic.call_count, 1)
            self.base.now_utc += timedelta(minutes=5)
            self.base.minutes_now += 5
            self.configure(mode="monitor")
            self.base.fetch_sensor_data(save=True)
            self.assertEqual(classic.call_count, 1)
            self.assertIsNone(self.base.load_forecast_guard_classic)
            self.assertEqual(len(self.base.load_forecast_guard.records), first_count)
            self.base.now_utc += timedelta(hours=1)
            self.base.minutes_now += 60
            self.data = {m: 200 - m * 0.02 for m in range(7201)}
            self.configure(mode="monitor")
            self.base.fetch_sensor_data(save=True)
            self.assertEqual(classic.call_count, 2)
            newest = max(self.base.load_forecast_guard.records, key=lambda row: row["start"])
            self.assertEqual(newest["start"], int(self.base.now_utc.timestamp() // 60))
            self.assertAlmostEqual(newest["classic"], newest["duration"] * 0.02, places=3)

    def test_historical_fallback_uses_normal_inday_adjustment(self):
        """The real fetch path adjusts Historical after a veto and disables adjustment for ML."""
        original_history = self.base.get_history_wrapper

        def history(*args, **kwargs):
            """Supply a known yesterday factor without changing any other input history."""
            if kwargs.get("entity_id") == self.base.prefix + ".load_inday_adjustment":
                return [[{"state": "150", "last_updated": (self.base.now_utc - timedelta(days=2)).isoformat()}, {"state": "150", "last_updated": self.base.now_utc.isoformat()}]]
            return original_history(*args, **kwargs)

        self.base.expose_config("calculate_inday_adjustment", True)
        with patch.object(self.base, "get_history_wrapper", side_effect=history), patch.object(self.base, "inday_yesterday_weight", return_value=1):
            self.configure()
            self.base.fetch_sensor_data(save=True)
            self.assertEqual(self.base.load_forecast_guard_selected, "classic")
            self.assertAlmostEqual(self.base.load_inday_adjustment, 1.5)
            self.configure(mode="monitor")
            self.base.fetch_sensor_data(save=True)
            self.assertEqual(self.base.load_forecast_guard_selected, "ml")
            self.assertEqual(self.base.load_inday_adjustment, 1.0)


def run_load_forecast_guard_fetch_tests(my_predbat):
    """Run full fetch integration checks through the normal shared-fixture registry."""
    GuardFetchTests.base_fixture = my_predbat
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(GuardFetchTests))
    return not result.wasSuccessful()
