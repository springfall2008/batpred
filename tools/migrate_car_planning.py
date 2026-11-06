"""Offline migration into native car arguments and Storage, without device access.

The previous configuration key and Storage module are mandatory caller inputs;
the utility embeds no prototype namespace, site profile or installation paths.
"""

import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "predbat"))
from car_charging_config import CAR_SETTING_KEYS, INDEXED_CAR_SETTINGS, native_car_arguments
from car_charging_storage import migrate_car_storage


class SecretReference(str):
    """Retain a YAML secret reference by name without resolving its value."""


class ConfigurationLoader(yaml.SafeLoader):
    """Safe YAML loader preserving only the existing secret-reference tag."""


class ConfigurationDumper(yaml.SafeDumper):
    """Safe YAML writer retaining secret references, not injecting credentials."""


def load_secret_reference(loader, node):
    """Read the reference name; never access an environment variable or a secret store."""
    return SecretReference(loader.construct_scalar(node))


def dump_secret_reference(dumper, value):
    """Emit the same reference tag and name on output."""
    return dumper.represent_scalar("!secret", str(value))


ConfigurationLoader.add_constructor("!secret", load_secret_reference)
ConfigurationDumper.add_representer(SecretReference, dump_secret_reference)


def convert_configuration(document, profile, settings_key, battery_window_minutes):
    """Migrate one explicit profile while preserving unrelated data and refusing conflicts."""
    result = deepcopy(document)
    if not isinstance(result, dict) or not isinstance(result.get(profile), dict):
        raise ValueError("An explicit application profile is required")
    args = result[profile]
    if settings_key not in args:
        raise ValueError("Source settings are missing")
    previous = args.pop(settings_key)
    converted = native_car_arguments(previous, battery_window_minutes)
    indexed = {CAR_SETTING_KEYS[name] for name in INDEXED_CAR_SETTINGS}
    for key, value in converted.items():
        if key in args:
            existing = args[key]
            comparable = existing[0] if key in indexed and isinstance(existing, list) and len(existing) == 1 else existing
            if comparable != value:
                raise ValueError("Conflicting native car argument")
        args[key] = value
    return result


def atomic_private_write(destination, data):
    """Create a private output without replacing an existing, different operator artifact."""
    destination = Path(destination)
    if destination.exists():
        if destination.is_symlink() or not destination.is_file() or destination.stat().st_mode & 0o077:
            raise ValueError("Existing destination is not a private regular file")
        if destination.read_bytes() != data:
            raise ValueError("Destination already exists with different contents")
        return
    descriptor, temporary = tempfile.mkstemp(prefix=".car-migration-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.link(temporary, destination)
    finally:
        os.unlink(temporary)


def main():
    """Prepare an offline, verified migration; this never starts or controls an application."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--settings-key", required=True)
    parser.add_argument("--battery-window-minutes", type=int, choices=(5, 15, 30, 60), required=True)
    parser.add_argument("--offline", action="store_true", required=True, help="Operator attestation that the application is stopped; the utility does not verify or change service state")
    parser.add_argument("--config-root", type=Path)
    parser.add_argument("--source-module")
    options = parser.parse_args()
    if options.input.resolve() == options.output.resolve():
        raise ValueError("Source configuration must be preserved separately")
    if bool(options.config_root) != bool(options.source_module):
        raise ValueError("Storage root and source module must be supplied together")
    before = options.input.read_bytes()
    document = yaml.load(before, Loader=ConfigurationLoader)
    converted = convert_configuration(document, options.profile, options.settings_key, options.battery_window_minutes)
    configuration = converted[options.profile]
    retained_session = configuration.get("car_charging_recurring_session") or configuration.get("car_charging_confirmed_session")
    if retained_session and not options.config_root:
        raise ValueError("Retained sessions require Storage migration before configuration output")
    receipt = {"source_configuration_sha256": hashlib.sha256(before).hexdigest(), "source_preserved": True, "physical_verification": False, "execution_authorised_by_migration": False}
    if options.config_root:
        from storage import StorageLocalFiles

        storage = StorageLocalFiles(str(options.config_root), lambda message: None)
        receipt["storage"] = asyncio.run(migrate_car_storage(storage, options.source_module))
        expected = []
        if configuration.get("car_charging_recurring_session"):
            expected.append("recurring_session")
        if configuration.get("car_charging_confirmed_session"):
            expected.append("confirmed_session_guard")
        if any(name not in receipt["storage"]["records"] for name in expected):
            raise ValueError("Expected retained session evidence is missing")
    output = yaml.dump(converted, Dumper=ConfigurationDumper, sort_keys=False, allow_unicode=True).encode()
    receipt["output_configuration_sha256"] = hashlib.sha256(output).hexdigest()
    assert options.input.read_bytes() == before, "Source configuration changed during migration"
    atomic_private_write(options.output, output)
    atomic_private_write(Path(str(options.output) + ".migration.json"), (json.dumps(receipt, indent=2) + "\n").encode())
    print("Offline configuration and evidence migration verified; no application or device was controlled.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # YAML parser/transport exceptions can contain private source text.
        print("Migration failed ({}); source artifacts were not deleted.".format(type(error).__name__), file=sys.stderr)
        raise SystemExit(2) from None
