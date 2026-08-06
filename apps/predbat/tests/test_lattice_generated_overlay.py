# -----------------------------------------------------------------------------
# Predbat Home Battery System - generated Lattice configuration overlay tests
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------

import importlib.util
from pathlib import Path
import unittest
import sys
import types

from lattice_generated_overlay import GeneratedConfigOverlay


def _load_user_interface():
    """Load the mixin directly without changing the process's real modules."""
    saved_config = sys.modules.get("config")
    saved_predbat = sys.modules.get("predbat")
    config_stub = types.ModuleType("config")
    config_stub.CONFIG_API_OVERRIDE = {"inverter_limit": True}
    predbat_stub = types.ModuleType("predbat")
    predbat_stub.THIS_VERSION = "test"
    try:
        sys.modules["config"] = config_stub
        sys.modules["predbat"] = predbat_stub
        module_path = Path(__file__).resolve().parents[1] / "userinterface.py"
        spec = importlib.util.spec_from_file_location("_lattice_overlay_userinterface", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.UserInterface
    finally:
        if saved_config is None:
            sys.modules.pop("config", None)
        else:
            sys.modules["config"] = saved_config
        if saved_predbat is None:
            sys.modules.pop("predbat", None)
        else:
            sys.modules["predbat"] = saved_predbat


UserInterface = _load_user_interface()


class OverlayUserInterface(UserInterface):
    """Minimal argument-reader harness with controllable API and HA sources."""

    def __init__(self, args=None, overlay=None, ha_values=None, api_values=None):
        self.args = dict(args or {})
        self.lattice_generated_overlay = overlay
        self.ha_values = dict(ha_values or {})
        self.api_values = dict(api_values or {})

    def get_ha_config(self, name, default):
        if name in self.ha_values:
            return self.ha_values[name], default
        return None, default

    def get_manual_api(self, name):
        return list(self.api_values.get(name, ()))

    def get_state_wrapper(self, entity_id=None, default=None, **kwargs):
        return default

    def log(self, message):
        pass

    def record_status(self, message, **kwargs):
        pass


class TestGeneratedConfigOverlay(unittest.TestCase):
    """Verify immutable publication and effective argument precedence."""

    def test_overlay_is_default_off_and_absent_overlay_keeps_legacy_default(self):
        overlay = GeneratedConfigOverlay()
        overlay.replace({"example": "generated"})
        overlay.disable()

        disabled = OverlayUserInterface(overlay=overlay)
        absent = OverlayUserInterface(overlay=None)

        self.assertEqual(disabled.get_arg("example", "default", indirect=False), "default")
        self.assertEqual(absent.get_arg("example", "default", indirect=False), "default")

    def test_precedence_is_ha_then_explicit_then_generated_then_default(self):
        overlay = GeneratedConfigOverlay()
        overlay.replace(
            {
                "ha_wins": "generated",
                "explicit_wins": "generated",
                "generated_wins": "generated",
            }
        )
        ui = OverlayUserInterface(
            args={"ha_wins": "apps", "explicit_wins": "apps"},
            overlay=overlay,
            ha_values={"ha_wins": "ha"},
        )

        self.assertEqual(ui.get_arg("ha_wins", "default", indirect=False), "ha")
        self.assertEqual(ui.get_arg("explicit_wins", "default", indirect=False), "apps")
        self.assertEqual(ui.get_arg("generated_wins", "default", indirect=False), "generated")
        self.assertEqual(ui.get_arg("missing", "default", indirect=False), "default")

    def test_topology_owned_hardware_fact_precedes_ha_but_allows_explicit_escape_hatch(self):
        overlay = GeneratedConfigOverlay(authoritative_arguments=("inverter_hybrid",))
        overlay.replace({"inverter_hybrid": False})

        generated = OverlayUserInterface(
            overlay=overlay,
            ha_values={"inverter_hybrid": True},
        )
        explicit = OverlayUserInterface(
            args={"inverter_hybrid": True},
            overlay=overlay,
            ha_values={"inverter_hybrid": False},
        )

        self.assertFalse(generated.get_arg("inverter_hybrid", True, indirect=False))
        self.assertTrue(explicit.get_arg("inverter_hybrid", False, indirect=False))

    def test_explicit_none_suppresses_generated_value(self):
        overlay = GeneratedConfigOverlay()
        overlay.replace({"example": "generated"})
        ui = OverlayUserInterface(args={"example": None}, overlay=overlay)

        self.assertIsNone(ui.get_arg("example", "default", indirect=False))

    def test_generated_per_index_value_uses_requested_index(self):
        overlay = GeneratedConfigOverlay()
        overlay.replace({"battery_rate_max": [3000, 4000]})
        ui = OverlayUserInterface(overlay=overlay)

        self.assertEqual(ui.get_arg("battery_rate_max", 0.0, index=1, indirect=False), 4000.0)

    def test_api_list_override_does_not_mutate_generated_snapshot(self):
        overlay = GeneratedConfigOverlay()
        overlay.replace({"inverter_limit": [3000, 4000]})
        ui = OverlayUserInterface(
            overlay=overlay,
            api_values={"inverter_limit": ({"index": 1, "value": "4500"},)},
        )

        self.assertEqual(ui.get_arg("inverter_limit", [0, 0], indirect=False), [3000, 4500])

        ui.api_values = {}
        self.assertEqual(ui.get_arg("inverter_limit", [0, 0], indirect=False), [3000, 4000])
        self.assertEqual(overlay.read("inverter_limit"), (True, [3000, 4000]))

    def test_domain_arguments_never_fall_back_to_generated_overlay(self):
        overlay = GeneratedConfigOverlay()
        overlay.replace({"example": "generated"})
        ui = OverlayUserInterface(args={"provider": {"example": "domain"}}, overlay=overlay)

        self.assertEqual(ui.get_arg("example", "default", domain="provider", indirect=False), "domain")
        self.assertEqual(ui.get_arg("missing", "default", domain="provider", indirect=False), "default")

    def test_replace_is_full_replacement_and_reads_are_detached(self):
        overlay = GeneratedConfigOverlay()
        original = {"removed": [1], "retained": {"nested": [2]}}
        first = overlay.replace(original)
        original["retained"]["nested"].append(99)

        present, value = overlay.read("retained")
        self.assertTrue(present)
        self.assertEqual(value, {"nested": [2]})
        value["nested"].append(3)
        self.assertEqual(overlay.read("retained"), (True, {"nested": [2]}))

        second = overlay.replace({"retained": [4]})
        self.assertEqual(second.version, first.version + 1)
        self.assertEqual(overlay.read("removed"), (False, None))
        self.assertEqual(overlay.read("retained"), (True, [4]))


if __name__ == "__main__":
    unittest.main()
