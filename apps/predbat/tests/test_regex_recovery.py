"""Offline regressions for entity regexes missing during startup."""

import copy
import unittest
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

from predbat import PredBat
from userinterface import UserInterface
from utils import SECRET_MASK, is_debug_excluded_key


class DiscoveryProbe(UserInterface):
    """Use real configuration methods without starting components or connections."""

    def __init__(self, args):
        """Attach synthetic configuration and an initially empty HA state cache."""
        self.args = copy.deepcopy(args)
        self.unmatched_args = {}
        self.states = {}
        self.log = Mock()
        self._invalidate_log_secret_pattern = Mock()

    def get_state_wrapper(self):
        """Return synthetic entities only."""
        return self.states


class RegexRecoveryTests(unittest.TestCase):
    """Check safe consumer values, late discovery and lifecycle behaviour."""

    def test_scalar_after_final(self):
        """An optional scalar recovers even after final startup cleanup."""
        probe = DiscoveryProbe({"octopus_free_session": "re:(event.test_free_.*)"})
        probe.auto_config(final=True)
        self.assertNotIn("octopus_free_session", probe.args)
        probe.auto_config(final=True)
        self.assertIn("octopus_free_session", probe.unmatched_args)
        probe.states = {"event.test_free_session": {}}
        probe.auto_config(final=True)
        self.assertEqual(probe.args["octopus_free_session"], "event.test_free_session")
        self.assertEqual(probe.unmatched_args, {})

    def test_partial_containers(self):
        """Nested lists and dicts recover without rematching resolved siblings."""
        probe = DiscoveryProbe({"sensors": ["re:sensor.early_.*", {"late": "re:sensor.late_.*", "fixed": "sensor.fixed"}, None]})
        probe.states = {"sensor.early_first": {}}
        probe.auto_config()
        self.assertEqual(probe.args["sensors"], ["sensor.early_first", {"late": None, "fixed": "sensor.fixed"}, None])
        probe.states = {"sensor.early_second": {}, "sensor.late_ready": {}}
        probe.auto_config(final=True)
        self.assertEqual(probe.args["sensors"], ["sensor.early_first", {"late": "sensor.late_ready", "fixed": "sensor.fixed"}, None])
        self.assertEqual(probe.unmatched_args, {})

    def test_no_regex_for_consumers(self):
        """Both startup passes hide unresolved scalar, list and dict patterns."""
        for final in (False, True):
            with self.subTest(final=final):
                probe = DiscoveryProbe({"scalar": "re:sensor.missing", "list": ["sensor.fixed", "re:sensor.missing"], "dict": {"late": "re:sensor.missing"}})
                probe.auto_config(final=final)
                self.assertEqual(probe.args, {"list": ["sensor.fixed", None], "dict": {"late": None}})
                self.assertEqual(set(probe.unmatched_args), {"scalar", "list", "dict"})

    def test_staggered_recovery(self):
        """A second missing list entry must not discard the first recovery."""
        probe = DiscoveryProbe({"sensors": ["re:sensor.first_.*", "re:sensor.second_.*"]})
        probe.auto_config()
        for entity, expected in (("sensor.first_ready", ["sensor.first_ready", None]), ("sensor.second_ready", ["sensor.first_ready", "sensor.second_ready"])):
            probe.states = {entity: {}}
            probe.auto_config(final=True)
            self.assertEqual(probe.args["sensors"], expected)
        self.assertEqual(probe.unmatched_args, {})

    def test_explicit_set_and_delete(self):
        """An explicit replacement or deletion cancels that argument's old retry."""
        for value in (None, "sensor.explicit", ["sensor.explicit"], {"fixed": "sensor.explicit"}):
            with self.subTest(value=value):
                probe = DiscoveryProbe({"setting": "re:sensor.late_.*"})
                probe.auto_config(final=True)
                probe.set_arg("setting", value)
                probe.states = {"sensor.late_ready": {}}
                probe.auto_config(final=True)
                self.assertEqual(probe.args, {} if value is None else {"setting": value})
                self.assertEqual(probe.unmatched_args, {})

    def test_direct_container_override(self):
        """Changed arguments must not be overwritten by a saved pending tree."""
        probe = DiscoveryProbe({"sensors": ["re:sensor.late_.*"]})
        probe.auto_config(final=True)
        probe.args["sensors"] = ["sensor.explicit"]
        probe.states = {"sensor.late_ready": {}}
        probe.auto_config(final=True)
        self.assertEqual(probe.args["sensors"], ["sensor.explicit"])
        self.assertEqual(probe.unmatched_args, {})

    def test_timer_recovers_without_validation_error(self):
        """The real timer retries optional discovery with no validation retry armed."""
        probe = DiscoveryProbe({"octopus_free_session": "re:event.late_.*"})
        probe.auto_config(final=True)
        probe.states = {"event.late_ready": {}}
        probe.ha_interface = SimpleNamespace(websocket_active=True, db_primary=False)
        probe.prediction_started = False
        probe.update_pending = False
        probe.check_entity_refresh = Mock()
        probe.validate_config_check_retry = Mock()
        probe.dynamic_load_car_poll = Mock()
        probe.validate_config_retries_remaining = 0
        probe.validate_config_next_retry_time = None
        probe.validate_config_check_retry = MethodType(PredBat.validate_config_check_retry, probe)
        probe.car_charging_now_poll = Mock()
        probe.load_user_config = Mock()
        probe.validate_config = Mock(return_value=0)
        probe.validate_config_schedule_retry = Mock()
        probe.update_pred = Mock()
        probe.create_entity_list = Mock()
        probe.expose_config = Mock()
        probe.get_arg = Mock(return_value=["event.late_ready"])
        probe.inverter_data_last_fetch = None
        probe.quick_inverter_data_update = Mock()
        probe.watch_list = []
        PredBat.update_time_loop(probe, None)
        self.assertEqual(probe.args["octopus_free_session"], "event.late_ready")
        probe.update_pred.assert_called_once_with(scheduled=False)
        probe.validate_config.assert_called_once()
        self.assertEqual(probe.watch_list, ["event.late_ready"])

    def test_timer_defers_during_prediction(self):
        """Do not change configuration while prediction is using it."""
        probe = DiscoveryProbe({"setting": "re:sensor.late_.*"})
        probe.auto_config(final=True)
        probe.states = {"sensor.late_ready": {}}
        probe.ha_interface = SimpleNamespace(websocket_active=True, db_primary=False)
        probe.prediction_started = True
        probe.update_pending = False
        probe.check_entity_refresh = Mock()
        probe.validate_config_check_retry = Mock()
        PredBat.update_time_loop(probe, None)
        self.assertNotIn("setting", probe.args)

    def test_success_reports_change_once(self):
        """A missing pattern does not cause repeated replanning."""
        probe = DiscoveryProbe({"setting": "re:sensor.late_.*"})
        probe.auto_config(final=True)
        probe._invalidate_log_secret_pattern.reset_mock()
        self.assertFalse(probe.auto_config(final=True))
        probe._invalidate_log_secret_pattern.assert_not_called()
        probe.states = {"sensor.late_ready": {}}
        self.assertTrue(probe.auto_config(final=True))
        probe._invalidate_log_secret_pattern.assert_called_once()
        self.assertFalse(probe.auto_config(final=True))
        probe._invalidate_log_secret_pattern.assert_called_once()

    def test_reload_discards_pending_discovery(self):
        """The real reset discards retry state before a new config is initialised."""
        probe = DiscoveryProbe({"setting": "re:sensor.old_.*"})
        probe.auto_config(final=True)
        probe.args = {"setting": "sensor.replacement"}
        PredBat.reset(probe)
        self.assertEqual(probe.unmatched_args, {})
        self.assertEqual(probe._auto_config_pending, {})
        probe.states = {"sensor.old_ready": {}}
        probe.auto_config(final=True)
        self.assertEqual(probe.args["setting"], "sensor.replacement")

    def test_finalisation_warns_once_after_initial_pass(self):
        """The normal non-final/final startup sequence must retain a visible warning."""
        probe = DiscoveryProbe({"setting": "re:sensor.late_.*"})
        probe.auto_config()
        probe.log.reset_mock()
        probe.auto_config(final=True)
        probe.auto_config(final=True)
        warnings = [call for call in probe.log.call_args_list if "not fully matched" in call.args[0]]
        self.assertEqual(len(warnings), 1)

    def test_pending_credentials_are_private(self):
        """Partial retry trees must not add credentials to diagnostic output."""
        probe = DiscoveryProbe({"example_token": ["synthetic-private-value", "re:sensor.late_.*"]})
        probe.auto_config(final=True)
        self.assertEqual(probe.unmatched_args["example_token"], SECRET_MASK)
        self.assertTrue(is_debug_excluded_key("_auto_config_pending"))
        probe.states = {"sensor.late_ready": {}}
        probe.auto_config(final=True)
        self.assertEqual(probe.args["example_token"], ["synthetic-private-value", "sensor.late_ready"])


def run_regex_recovery_tests(my_predbat):
    """Register isolated regressions with the canonical Predbat runner."""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RegexRecoveryTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return not result.wasSuccessful()
