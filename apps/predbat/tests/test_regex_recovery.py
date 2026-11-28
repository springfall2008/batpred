"""Offline regressions for entity regexes missing during startup."""

import copy
import asyncio
import unittest
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch

from predbat import PredBat
from userinterface import UserInterface
from utils import SECRET_MASK, is_debug_excluded_key
from web import WebInterface


class DiscoveryProbe(UserInterface):
    """Use real configuration methods without starting components or connections."""

    def __init__(self, args):
        """Attach synthetic configuration and an initially empty HA state cache."""
        self.args = copy.deepcopy(args)
        self.unmatched_args = {}
        self._auto_config_pending = {}
        self._auto_config_next_retry = 0
        self._auto_config_retry_delay = 60
        self.states = {}
        self.log = Mock()
        self._invalidate_log_secret_pattern = Mock()

    def prepare_timer(self):
        """Stub unrelated timer work while exercising discovery and replanning unchanged."""
        self.ha_interface = SimpleNamespace(websocket_active=True, db_primary=False)
        self.prediction_started = False
        self.update_pending = False
        self.check_entity_refresh = Mock()
        self.validate_config_check_retry = Mock()
        self.dynamic_load_car_poll = Mock()
        self.car_charging_now_poll = Mock()
        self.load_user_config = Mock()
        self.validate_config = Mock(return_value=0)
        self.validate_config_schedule_retry = Mock()
        self.update_pred = Mock()
        self.create_entity_list = Mock()
        self.expose_config = Mock()
        self.get_arg = Mock(return_value=[])
        self.inverter_data_last_fetch = None
        self.quick_inverter_data_update = Mock()

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
        probe._auto_config_next_retry = 0
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
        probe._auto_config_next_retry = 0
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

    def test_steady_state_timer_backoff(self):
        """Permanently optional patterns scan only pending args and never warn or replan again."""
        for source in ("re:sensor.missing", ["re:sensor.missing"], {"late": "re:sensor.missing"}):
            with self.subTest(source=source), patch("userinterface.time.monotonic", return_value=0) as clock:
                probe = DiscoveryProbe({"optional": source, "fixed": "sensor.fixed"})
                probe.prepare_timer()
                probe.auto_config()
                probe.auto_config(final=True)
                self.assertEqual(len([call for call in probe.log.call_args_list if call.args[0].startswith("Warn:")]), 1)
                probe.log.reset_mock()
                probe._invalidate_log_secret_pattern.reset_mock()
                probe.get_state_wrapper = Mock(return_value={})
                probe._resolve_arg_re_pending = Mock(wraps=probe._resolve_arg_re_pending)
                scan_times = []
                for tick in range(15, 1801, 15):
                    clock.return_value = tick
                    before = probe.get_state_wrapper.call_count
                    PredBat.update_time_loop(probe, None)
                    if probe.get_state_wrapper.call_count != before:
                        scan_times.append(tick)
                self.assertEqual(scan_times, [60, 180, 420, 720, 1020, 1320, 1620])
                self.assertTrue(all(call.args[0] == "optional" for call in probe._resolve_arg_re_pending.call_args_list))
                probe.log.assert_not_called()
                probe.update_pred.assert_not_called()
                probe.validate_config.assert_not_called()
                probe._invalidate_log_secret_pattern.assert_not_called()

    def test_recovery_after_maximum_backoff(self):
        """Scalar, list and dict settings recover indefinitely, with exactly one new plan."""
        for source, expected in (("re:sensor.late_.*", "sensor.late_ready"), (["re:sensor.late_.*"], ["sensor.late_ready"]), ({"late": "re:sensor.late_.*"}, {"late": "sensor.late_ready"})):
            with self.subTest(source=source), patch("userinterface.time.monotonic", return_value=0) as clock:
                probe = DiscoveryProbe({"setting": source})
                probe.prepare_timer()
                probe.auto_config(final=True)
                for tick in range(15, 3601, 15):
                    clock.return_value = tick
                    PredBat.update_time_loop(probe, None)
                self.assertEqual(probe._auto_config_retry_delay, 300)
                due = probe._auto_config_next_retry
                probe.states = {"sensor.late_ready": {}}
                clock.return_value = due - 15
                PredBat.update_time_loop(probe, None)
                probe.update_pred.assert_not_called()
                clock.return_value = due
                PredBat.update_time_loop(probe, None)
                self.assertEqual(probe.args["setting"], expected)
                self.assertEqual(probe._auto_config_pending, {})
                self.assertEqual(probe.unmatched_args, {})
                self.assertEqual(probe._auto_config_retry_delay, 60)
                for tick in range(int(due) + 15, int(due) + 601, 15):
                    clock.return_value = tick
                    PredBat.update_time_loop(probe, None)
                probe.update_pred.assert_called_once_with(scheduled=False)
                probe.validate_config.assert_called_once()

    def test_timer_partial_recovery_resets_backoff(self):
        """Progress shortens the next retry while preserving already resolved siblings."""
        with patch("userinterface.time.monotonic", return_value=0) as clock:
            probe = DiscoveryProbe({"setting": ["re:sensor.first_.*", {"late": "re:sensor.second_.*"}]})
            probe.prepare_timer()
            probe.auto_config(final=True)
            for tick in (60, 180, 420):
                clock.return_value = tick
                PredBat.update_time_loop(probe, None)
            self.assertEqual(probe._auto_config_retry_delay, 300)
            probe.states = {"sensor.first_ready": {}}
            clock.return_value = 720
            PredBat.update_time_loop(probe, None)
            self.assertEqual(probe.args["setting"], ["sensor.first_ready", {"late": None}])
            self.assertEqual(probe._auto_config_next_retry, 780)
            probe.states = {"sensor.first_other": {}, "sensor.second_ready": {}}
            clock.return_value = 780
            PredBat.update_time_loop(probe, None)
            self.assertEqual(probe.args["setting"], ["sensor.first_ready", {"late": "sensor.second_ready"}])
            self.assertEqual(probe.update_pred.call_count, 2)

    def test_timer_drops_superseded_pending_values(self):
        """Explicit writes and direct overrides cancel pending work without scanning HA."""
        with patch("userinterface.time.monotonic", return_value=0) as clock:
            probe = DiscoveryProbe({"direct": ["re:sensor.late_.*"], "explicit": "re:sensor.late_.*", "deleted": "re:sensor.late_.*"})
            probe.prepare_timer()
            probe.auto_config(final=True)
            probe.args["direct"] = ["sensor.replacement"]
            probe.set_arg("explicit", "sensor.fixed")
            probe.set_arg("deleted", None)
            probe.get_state_wrapper = Mock(return_value={"sensor.late_ready": {}})
            clock.return_value = 60
            PredBat.update_time_loop(probe, None)
            self.assertEqual(probe.args, {"direct": ["sensor.replacement"], "explicit": "sensor.fixed"})
            self.assertEqual(probe._auto_config_pending, {})
            self.assertEqual(probe.unmatched_args, {})
            probe.get_state_wrapper.assert_not_called()
            probe.update_pred.assert_not_called()

    def test_due_retry_waits_for_prediction(self):
        """An overdue retry leaves args and its deadline untouched until prediction ends."""
        with patch("userinterface.time.monotonic", return_value=0) as clock:
            probe = DiscoveryProbe({"setting": ["re:sensor.late_.*"]})
            probe.prepare_timer()
            probe.auto_config(final=True)
            probe.states = {"sensor.late_ready": {}}
            probe.get_state_wrapper = Mock(return_value=probe.states)
            clock.return_value = 600
            probe.prediction_started = True
            PredBat.update_time_loop(probe, None)
            self.assertEqual(probe.args["setting"], [None])
            self.assertEqual(probe._auto_config_next_retry, 60)
            probe.get_state_wrapper.assert_not_called()
            probe.prediction_started = False
            PredBat.update_time_loop(probe, None)
            self.assertEqual(probe.args["setting"], ["sensor.late_ready"])
            probe.update_pred.assert_called_once_with(scheduled=False)

    def test_apps_pending_rows_are_unique(self):
        """Render one highlighted row per pending scalar or container without leaking secrets."""
        probe = DiscoveryProbe({"scalar": "re:sensor.missing", "list": ["re:sensor.missing"], "dict": {"late": "re:sensor.missing"}, "example_token": ["synthetic-private-value", "re:sensor.missing"]})
        probe.auto_config(final=True)
        probe.arg_errors = {}
        probe.arg_warnings = {}
        interface = WebInterface.__new__(WebInterface)
        interface.base = probe
        interface.args = probe.args
        interface.get_header = Mock(return_value="<html>")
        interface.get_state_wrapper = Mock(return_value={})
        interface.resolve_value_raw = Mock(side_effect=lambda arg, value: value)
        interface.render_type = Mock(side_effect=lambda arg, value, *args: str(value))
        interface.is_editable_value = Mock(return_value=False)
        interface.secret_row_attr = Mock(return_value="")
        text = asyncio.run(interface.html_apps(None)).text
        for arg in ("scalar", "list", "dict", "example_token"):
            self.assertEqual(text.count('data-arg-name="{}"'.format(arg)), 1)
        self.assertEqual(text.count('title="Missing items pending entity discovery"'), 3)
        self.assertNotIn("synthetic-private-value", text)
        probe.states = {"sensor.missing": {}}
        probe.auto_config(final=True)
        text = asyncio.run(interface.html_apps(None)).text
        for arg in ("scalar", "list", "dict", "example_token"):
            self.assertEqual(text.count('data-arg-name="{}"'.format(arg)), 1)
        self.assertNotIn("Missing items pending entity discovery", text)


def run_regex_recovery_tests(my_predbat):
    """Register isolated regressions with the canonical Predbat runner."""
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(RegexRecoveryTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return not result.wasSuccessful()
