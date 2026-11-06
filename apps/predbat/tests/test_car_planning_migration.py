"""Offline native YAML conversion preserves references, unrelated settings and originals."""

import importlib.util
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest


def migration_module():
    """Import the repository utility without starting its CLI or touching a real configuration."""
    path = Path(__file__).resolve().parents[3] / "tools" / "migrate_car_planning.py"
    spec = importlib.util.spec_from_file_location("car_migration_test_module", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CarPlanningMigrationTests(unittest.TestCase):
    """Use only synthetic input and temporary artifacts."""

    def test_preserves_secrets_and_unrelated_configuration(self):
        """Only supplied feature settings are flattened, with the saved battery choice retained."""
        module = migration_module()
        text = "pred_bat:\n  api_key: !secret example_key\n  unrelated: retained\n  previous_settings:\n    mode: shadow\n    connection_entity: sensor.example_plug\n"
        document = module.yaml.load(text, Loader=module.ConfigurationLoader)
        converted = module.convert_configuration(document, "pred_bat", "previous_settings", 5)
        self.assertIn("previous_settings", document["pred_bat"])
        self.assertNotIn("previous_settings", converted["pred_bat"])
        self.assertEqual(converted["pred_bat"]["car_charging_plugged"], "sensor.example_plug")
        self.assertEqual(converted["pred_bat"]["battery_min_window_minutes"], 5)
        self.assertEqual(converted["pred_bat"]["unrelated"], "retained")
        output = module.yaml.dump(converted, Dumper=module.ConfigurationDumper)
        self.assertIn("!secret", output)
        self.assertIn("example_key", output)

    def test_refuses_conflicting_native_values(self):
        """Migration does not silently overwrite an existing native physical mapping."""
        module = migration_module()
        document = {"pred_bat": {"car_charging_plugged": "sensor.other_plug", "previous_settings": {"mode": "shadow", "connection_entity": "sensor.example_plug"}}}
        with self.assertRaises(ValueError):
            module.convert_configuration(document, "pred_bat", "previous_settings", 5)

    def test_private_output_is_idempotent_but_never_overwritten(self):
        """Output is private and a second different migration cannot replace it."""
        module = migration_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output.yaml"
            module.atomic_private_write(path, b"synthetic\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            module.atomic_private_write(path, b"synthetic\n")
            with self.assertRaises(ValueError):
                module.atomic_private_write(path, b"different\n")
            self.assertEqual(path.read_bytes(), b"synthetic\n")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                module.atomic_private_write(path, b"synthetic\n")

    def test_cli_preserves_source_and_requires_evidence_for_retained_sessions(self):
        """Exercise the real offline command with synthetic files and no device transport."""
        script = Path(__file__).resolve().parents[3] / "tools" / "migrate_car_planning.py"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.yaml"
            target = Path(directory) / "output.yaml"
            before = b"pred_bat:\n  api_key: !secret example_key\n  previous_settings:\n    mode: shadow\n"
            source.write_bytes(before)
            command = [sys.executable, str(script), "--input", str(source), "--output", str(target), "--profile", "pred_bat", "--settings-key", "previous_settings", "--battery-window-minutes", "5", "--offline"]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(source.read_bytes(), before)
            self.assertIn("car_charging_plan_mode: shadow", target.read_text())
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            source.write_text("pred_bat:\n  previous_settings:\n    mode: apply\n    recurring_session:\n      enabled: true\n")
            target.unlink()
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(target.exists())

    def test_cli_copies_real_storage_and_requires_the_matching_record(self):
        """The wrong kind of retained record cannot stand in for a recurring energy baseline."""
        import asyncio
        from storage import StorageLocalFiles

        script = Path(__file__).resolve().parents[3] / "tools" / "migrate_car_planning.py"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input.yaml"
            target = root / "output.yaml"
            original = b"pred_bat:\n  previous_settings:\n    mode: apply\n    recurring_session:\n      enabled: true\n"
            source.write_bytes(original)
            storage = StorageLocalFiles(str(root), lambda message: None)
            guard = {"identity": "synthetic", "invalid": True}
            self.assertTrue(asyncio.run(storage.save("previous_planner", "confirmed_session_guard", guard, format="json", expiry=None)))
            command = [
                sys.executable,
                str(script),
                "--input",
                str(source),
                "--output",
                str(target),
                "--profile",
                "pred_bat",
                "--settings-key",
                "previous_settings",
                "--battery-window-minutes",
                "5",
                "--offline",
                "--config-root",
                str(root),
                "--source-module",
                "previous_planner",
            ]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(target.exists())
            retained = {"identity": "retained", "invalid": "recurring_meter_contradiction", "session": {"meter_baseline": 100, "last_energy": 100.5}}
            self.assertTrue(asyncio.run(storage.save("previous_planner", "recurring_session", retained, format="json", expiry=None)))
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(asyncio.run(storage.load_checked("previous_planner", "recurring_session")), retained)
            self.assertEqual(asyncio.run(storage.load_checked("car_charging", "recurring_session")), retained)
            self.assertTrue(Path(str(target) + ".migration.json").exists())


def run_car_planning_migration_tests(base):
    """Expose the offline utility contract through the normal test registry."""
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CarPlanningMigrationTests))
    return not result.wasSuccessful()
