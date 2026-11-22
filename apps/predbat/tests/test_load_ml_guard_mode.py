"""Native guard-mode controls, migration, immediate persistence and safe chart return."""

import asyncio
import copy
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web as aiohttp_web
from web import WebInterface


class GuardModeControlTests(unittest.TestCase):
    """Run real configuration callbacks against the standard fake HA and a temporary config root."""

    base_fixture = None

    def setUp(self):
        """Isolate one real Config item without changing the fixture's other settings."""
        self.base = self.base_fixture
        self.saved_state = dict(self.base.__dict__)
        self.saved_ha = copy.deepcopy(self.base.ha_interface.dummy_items)
        self.saved_primary = self.base.ha_interface.db_primary
        self.saved_history_enabled = self.base.ha_interface.history_enable
        self.directory = tempfile.TemporaryDirectory(prefix="predbat_guard_mode_")
        self.addCleanup(self.restore_fixture)
        item = copy.deepcopy(self.base.config_index["load_ml_guard_mode"])
        item.pop("value", None)
        item.pop("entity", None)
        self.base.CONFIG_ITEMS = [item]
        self.base.args = {"load_ml_enable": True, "load_ml_guard_mode": "monitor", "load_ml_source": False}
        self.base.components = SimpleNamespace(select_event=AsyncMock(), get_component=lambda name: None)
        self.base.config_root = self.directory.name
        self.base.config_root_p = self.directory.name
        self.base.ha_interface.db_primary = False
        self.base.ha_interface.history_enable = False
        self.base.ha_interface.dummy_items.pop("select." + self.base.prefix + "_load_ml_guard_mode", None)
        self.base.load_user_config()
        self.web = WebInterface.__new__(WebInterface)
        self.web.base = self.base
        self.web.prefix = self.base.prefix
        self.web.log = self.base.log
        self.web.get_header = lambda *args, **kwargs: "<html><head></head>"
        self.field = self.base.config_index["load_ml_guard_mode"]["entity"].replace(".", "__")

    def restore_fixture(self):
        """Put back all fixture state and remove only this test's temporary files."""
        self.base.ha_interface.dummy_items = self.saved_ha
        self.base.ha_interface.db_primary = self.saved_primary
        self.base.ha_interface.history_enable = self.saved_history_enabled
        self.base.__dict__.clear()
        self.base.__dict__.update(self.saved_state)
        self.directory.cleanup()

    def submit(self, data):
        """Submit a normal form through the real /config handler and return its redirect."""

        async def post():
            """The posted fields aiohttp normally supplies."""
            return data

        with self.assertRaises(aiohttp_web.HTTPFound) as result:
            asyncio.run(self.web.html_config_post(SimpleNamespace(post=post)))
        return result.exception.location

    def test_fresh_install_defaults_to_monitor_without_yaml(self):
        """The chart defaults to Monitoring only when apps.yaml has no mode setting."""
        self.base.args.pop("load_ml_guard_mode")
        item = self.base.config_index["load_ml_guard_mode"]
        self.base.load_user_config()
        self.assertEqual(item["default"], "monitor")
        self.assertEqual(self.base.get_arg("load_ml_guard_mode"), "monitor")
        self.assertIn('<option value="monitor" selected>Monitoring only</option>', self.web.get_load_ml_accuracy_html())

    def test_native_item_and_legacy_initial_defaults(self):
        """Old apps.yaml modes seed the new native select without enabling ML planning."""
        item = self.base.config_index["load_ml_guard_mode"]
        self.assertEqual(item["type"], "select")
        self.assertEqual(item["options"], ["off", "monitor", "auto"])
        for mode in ("off", "auto", "monitor"):
            item["value"] = None
            self.base.args["load_ml_guard_mode"] = mode
            self.base.ha_interface.dummy_items.pop(item["entity"], None)
            self.base.load_user_config()
            self.assertEqual(self.base.get_arg("load_ml_guard_mode"), mode)
            self.assertFalse(self.base.get_arg("load_ml_source", False))

    def test_selector_on_both_charts_and_config(self):
        """Both chart controls and the existing Config page use the same entity and friendly choices."""
        for chart in ("LoadML", "LoadMLPower"):
            html = self.web.get_load_ml_accuracy_html(chart)
            self.assertIn('name="{}"'.format(self.field), html)
            self.assertIn('name="_load_ml_guard_return_chart" value="{}"'.format(chart), html)
            self.assertIn('<option value="monitor" selected>Monitoring only</option>', html)
            self.assertIn("Apply mode", html)
            self.assertIn("does not enable ML for planning", html)
        response = asyncio.run(self.web.html_config(SimpleNamespace()))
        self.assertIn('name="{}"'.format(self.field), response.text)
        self.assertIn('label="Automatic fallback"', response.text)
        self.assertIn("LoadML forecast guard mode", response.text)

    def test_chart_save_replans_and_survives_immediate_restart(self):
        """Real select callbacks update the live value and persist it ahead of a restart."""
        self.base.plan_valid, self.base.update_pending = True, False
        previous_reset = self.base.inverter_needs_reset
        location = self.submit({self.field: "auto", "_load_ml_guard_return_chart": "LoadMLPower"})
        self.assertEqual(location, "./charts?chart=LoadMLPower")
        self.assertEqual(self.base.get_arg("load_ml_guard_mode"), "auto")
        self.assertEqual(self.base.args["load_ml_guard_mode"], "monitor")
        self.assertTrue(self.base.update_pending)
        self.assertFalse(self.base.plan_valid)
        self.assertEqual(self.base.inverter_needs_reset, previous_reset)
        self.assertFalse(self.base.get_arg("load_ml_source", False))
        saved = json.loads(pathlib.Path(self.directory.name, "predbat_config.json").read_text())
        self.assertEqual(saved["load_ml_guard_mode"], "auto")
        item = self.base.config_index["load_ml_guard_mode"]
        item["value"] = None
        self.base.ha_interface.set_state(item["entity"], "unavailable")
        self.base.load_user_config(load_config=True)
        self.assertEqual(self.base.get_arg("load_ml_guard_mode"), "auto")
        self.base.args.pop("load_ml_guard_mode")
        self.base.load_user_config()
        self.assertEqual(self.base.get_arg("load_ml_guard_mode"), "auto")

    def test_config_page_save_and_off_remains_editable(self):
        """The Config page saves the same value; Off still provides a route back to monitoring."""
        self.assertEqual(self.submit({self.field: "off"}), "./config")
        self.assertEqual(self.base.get_arg("load_ml_guard_mode"), "off")
        html = self.web.get_load_ml_accuracy_html()
        self.assertIn('<option value="off" selected>Off</option>', html)
        self.assertIn("Apply mode", html)
        self.submit({self.field: "monitor", "_load_ml_guard_return_chart": "LoadML"})
        self.assertEqual(self.base.get_arg("load_ml_guard_mode"), "monitor")

    def test_invalid_mode_is_rejected_before_any_write(self):
        """Malformed selections cannot change other fields or persist a bad guard mode."""
        self.web.set_state_external = AsyncMock()
        saved_path = pathlib.Path(self.directory.name, "predbat_config.json")
        saved_before = saved_path.read_text()

        async def post():
            """Include another field first to check validation precedes every write."""
            return {"input_number__predbat_load_scaling": "2", self.field: "not-a-mode"}

        with self.assertRaises(aiohttp_web.HTTPBadRequest):
            asyncio.run(self.web.html_config_post(SimpleNamespace(post=post)))
        self.web.set_state_external.assert_not_awaited()
        self.assertEqual(saved_path.read_text(), saved_before)
        self.assertEqual(self.base.get_arg("load_ml_guard_mode"), "monitor")

    def test_return_destination_is_allowlisted(self):
        """The optional chart return cannot become an external redirect."""
        self.assertEqual(self.submit({self.field: "auto", "_load_ml_guard_return_chart": "https://example.invalid"}), "./config")

    def test_unrelated_config_posts_keep_existing_behaviour(self):
        """Ordinary Config saves still convert values and return to Config without guard persistence."""
        self.web.set_state_external = AsyncMock()
        self.base.save_current_config = lambda: self.fail("Unrelated form attempted a guard save")
        self.assertEqual(self.submit({"input_number__predbat_load_scaling": "1.5"}), "./config")
        self.web.set_state_external.assert_awaited_once_with("input_number.predbat_load_scaling", 1.5)

    def test_custom_prefix_and_unencoded_native_entity(self):
        """Instances with another prefix control their own saved setting only."""
        self.base.prefix = self.web.prefix = "solar_test"
        self.base.load_user_config()
        entity = self.base.config_index["load_ml_guard_mode"]["entity"]
        self.assertEqual(entity, "select.solar_test_load_ml_guard_mode")
        self.assertEqual(self.submit({entity: "auto", "_load_ml_guard_return_chart": "LoadML"}), "./charts?chart=LoadML")
        self.assertEqual(self.base.get_arg("load_ml_guard_mode"), "auto")
        self.assertIn('name="select__solar_test_load_ml_guard_mode"', self.web.get_load_ml_accuracy_html())

    def test_guard_control_is_enabled_only_for_loadml_installs(self):
        """Ordinary LoadML users have the control; unrelated installations do not expose it."""
        item = self.base.config_index["load_ml_guard_mode"]
        self.assertTrue(self.base.user_config_item_enabled(item))
        self.base.args["load_ml_enable"] = False
        self.assertFalse(self.base.user_config_item_enabled(item))
        self.base.args["load_ml_enable"] = True
        self.assertTrue(self.base.user_config_item_enabled(item))


def run_load_ml_guard_mode_tests(my_predbat):
    """Exercise the saved-mode workflow using Predbat's standard test fixture."""
    GuardModeControlTests.base_fixture = my_predbat
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(GuardModeControlTests))
    GuardModeControlTests.base_fixture = None
    return not result.wasSuccessful()
