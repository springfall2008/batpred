# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Build the memory benchmark's local fixture from a live Predbat add-on config directory.

    python3 coverage/memory_benchmark_tools/export_fixture.py --live <add-on config dir> --out coverage/memory_benchmark_data

Reads the live apps.yaml, predbat.db and cache/ read-only (secrets.yaml is never read) and writes predbat.db, cache/,
apps.yaml and manifest.json to the output directory. T0, the time the benchmark's clock is frozen at, is the time of
the export. The output is checked for every real identifier the export knows of; if any survived, the output is
deleted and the export fails.
"""

import argparse
import copy
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture_cache  # noqa: E402
import fixture_db  # noqa: E402
import scrub  # noqa: E402

TOOL_VERSION = 1
TEMPLATE_PATH = Path(__file__).resolve().parent / "apps_template.yaml"
FAKE_SERIAL = "BENCH_SERIAL"
MIN_SERIAL_LENGTH = 6

# Live settings for the real inverter, which the dummy inverter replaces (its automatic_config sets its own)
INVERTER_KEYS = {
    "num_inverters",
    "balance_inverters_seconds",
    "charge_rate",
    "discharge_rate",
    "battery_power",
    "grid_power",
    "pv_power",
    "load_power",
    "soc_kw",
    "soc_max",
    "soc_percent",
    "reserve",
    "pause_mode",
    "charge_start_time",
    "charge_end_time",
    "charge_limit",
    "scheduled_charge_enable",
    "scheduled_discharge_enable",
    "discharge_start_time",
    "discharge_end_time",
    "discharge_target_soc",
    "pv_today",
    "load_today",
    "import_today",
    "export_today",
    "battery_rate_max",
    "battery_charge_power_curve",
    "battery_discharge_power_curve",
    "export_limit",
    # Names the real inverter's {geserial} entities
    "watch_list",
}
INVERTER_PREFIXES = ("inverter_", "givtcp_", "ge_cloud_", "battery_temperature", "geserial")

# Live settings that connect to Home Assistant, turn on a networked component, or carry an identifier
NETWORK_KEYS = {
    "ha_url",
    "ha_key",
    "chat",
    "alerts",
    "temperature_enable",
    "predheat",
    "compare_list",
    "device_id",
    "pv_forecast_today",
    "pv_forecast_tomorrow",
    "pv_forecast_d3",
    "pv_forecast_d4",
    "open_meteo_forecast",
}
NETWORK_PREFIXES = (
    "axle_",
    "carbon_",
    "wallbox_",
    "ohme_",
    "hypervolt_",
    "myenergi_",
    "solis_",
    "fox_",
    "solax_",
    "sigenergy_",
    "deye_",
    "sunsynk_",
    "alphaess_",
    "enphase_",
    "growatt_",
    "teslemetry_",
    "kraken_",
    "gateway_",
    "mcp_",
    "load_ml_",
    "solcast_",
)

# Dropped live settings whose values identify the home or a device; the output is checked for them
# (geserial is not here: it may just say "auto"; the serials themselves are read off entity names, see export())
IDENTIFYING_KEYS = ("ha_url", "carbon_postcode", "axle_api_key", "wallbox_password", "mcp_secret", "device_id")

# The LoadML training benchmark's fixture (--load-ml): the live LoadML history is copied, never a trained model, so every
# run trains from scratch, and LoadML is switched on with its forecast as the load source
ML_HISTORY = "predbat_ml_history.npz"
LOAD_ML_SETTINGS = {"load_ml_enable": True, "load_ml_source": True}


class Secret:
    """A !secret reference in the live apps.yaml. Its value is never read; any setting holding one is dropped."""

    def __init__(self, name):
        self.name = name

    def __eq__(self, other):
        """Two references are equal when they name the same secret, as the resolved values would be."""
        return isinstance(other, Secret) and other.name == self.name

    def __hash__(self):
        """Hash by the secret's name, matching __eq__."""
        return hash(("Secret", self.name))


class LiveLoader(yaml.SafeLoader):
    """YAML loader for the live apps.yaml that keeps !secret references unresolved."""


LiveLoader.add_constructor("!secret", lambda loader, node: Secret(loader.construct_scalar(node)))


def load_live_apps_text(text):
    """Return the Predbat block's settings from the text of a live apps.yaml."""
    document = yaml.load(text, Loader=LiveLoader)  # noqa: S506 - LiveLoader is a SafeLoader
    for value in (document or {}).values():
        if isinstance(value, dict) and value.get("module") == "predbat":
            return value
    raise ValueError("no Predbat block (module: predbat) in the live apps.yaml")


def load_template():
    """Return the committed apps.yaml template."""
    return yaml.safe_load(TEMPLATE_PATH.read_text())


def _holds_secret(value):
    """True when value is, or contains, a !secret reference."""
    if isinstance(value, Secret):
        return True
    if isinstance(value, dict):
        return any(_holds_secret(item) for item in value.values())
    if isinstance(value, list):
        return any(_holds_secret(item) for item in value)
    return False


def is_dropped_setting(key):
    """True for a live setting the benchmark leaves out: the real inverter's, and networked or identifying ones."""
    return key in INVERTER_KEYS or key in NETWORK_KEYS or key.startswith(INVERTER_PREFIXES) or key.startswith(NETWORK_PREFIXES) or key.endswith("_invert")


def build_apps_yaml(template, live_args, forecast_config, totals, sizing, load_ml=False):
    """Return the benchmark's apps.yaml: the live settings that are kept, under the template's.

    forecast_config is the neutral forecast_solar list, totals the dummy inverter's starting lifetime totals and sizing
    its battery_size and soc_initial. Octopus is pointed at the fake account when the live config uses it. load_ml switches
    LoadML on for the LoadML training benchmark's fixture (LOAD_ML_SETTINGS, over the template's off).
    """
    args = {key: copy.deepcopy(value) for key, value in live_args.items() if not is_dropped_setting(key) and not _holds_secret(value)}
    args.pop("forecast_solar", None)
    args.setdefault("timezone", "Europe/London")
    template_args = copy.deepcopy(template["pred_bat"])
    dummy = template_args.pop("dummy_inverter")
    args.update(template_args)
    if load_ml:
        args.update(LOAD_ML_SETTINGS)
    if "octopus_api_key" in live_args or "octopus_api_account" in live_args:
        args["octopus_api_key"] = scrub.FAKE_OCTOPUS_KEY
        args["octopus_api_account"] = scrub.FAKE_ACCOUNT
    if forecast_config:
        args["forecast_solar"] = forecast_config
    dummy.update({key: value for key, value in sizing.items() if value is not None})
    dummy["totals"] = dict(totals)
    args["dummy_inverter"] = dummy
    return {"pred_bat": args}


def battery_sizing(db_path, prefix):
    """Return {battery_size, soc_initial} from the exported database's dummy capacity and SoC, or None for each missing."""
    conn = sqlite3.connect(str(db_path))
    try:

        def latest(name):
            """Return the latest numeric state of a dummy sensor, or None."""
            row = conn.execute("SELECT l.state FROM latest l JOIN entities e USING(entity_index) WHERE e.entity_name = ?", ("sensor.{}_dummy_{}".format(prefix, name),)).fetchone()
            try:
                return float(row[0]) if row else None
            except (TypeError, ValueError):
                return None

        size = latest("battery_capacity")
        soc = latest("battery_soc")
    finally:
        conn.close()
    soc_initial = round(soc / size * 100.0, 1) if size and soc is not None else None
    return {"battery_size": size, "soc_initial": soc_initial}


def copy_ml_history(live, out, t0):
    """Copy the live LoadML history npz into out; return {npz_sha256, npz_saved_utc} for the manifest.

    LoadML shifts the saved history forward by the time since it was saved, so a history saved after T0 would be shifted
    backwards: that raises ValueError. A missing history raises FileNotFoundError.
    """
    import numpy as np

    source = Path(live) / ML_HISTORY
    if not source.exists():
        raise FileNotFoundError("no LoadML history at {}".format(source))
    target = Path(out) / ML_HISTORY
    shutil.copyfile(source, target)
    with np.load(target, allow_pickle=False) as data:
        saved_utc = datetime.fromisoformat(json.loads(str(data["metadata_json"]))["saved_utc"])
    if saved_utc > t0:
        raise ValueError("the LoadML history was saved at {}, after T0 {}".format(saved_utc.isoformat(), t0.isoformat()))
    return {"npz_sha256": _sha256(target), "npz_saved_utc": saved_utc.isoformat()}


def ml_history_metadata(out):
    """Return the LoadML history's metadata text from a fixture directory, or None when it holds no history."""
    path = Path(out) / ML_HISTORY
    if not path.exists():
        return None
    import numpy as np

    with np.load(path, allow_pickle=False) as data:
        return str(data["metadata_json"])


def _sha256(path):
    """Return the hex sha256 of a file."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def export(live, out, days, t0, strip_outputs=False, load_ml=False):
    """Write the fixture for live into out (which must not exist) and return (stats, secrets, tokens).

    secrets are the real values known to the export and tokens the device ids read off entity names, for the leak check.
    With load_ml the live LoadML history is copied first, just before the database snapshot, so the two are from the
    same moment, and LoadML is switched on.
    """
    live_args = load_live_apps_text((live / "apps.yaml").read_text())
    prefix = live_args.get("prefix", "predbat")
    (out / "cache").mkdir(parents=True)
    ml_history = copy_ml_history(live, out, t0) if load_ml else None
    with tempfile.TemporaryDirectory() as snapshot_dir:
        # A consistent local copy of the live database, which the add-on keeps writing; kept outside the fixture
        snapshot = Path(snapshot_dir) / "predbat.db"
        fixture_db.snapshot_db(live / "predbat.db", snapshot)
        return _export_from(live, out, days, t0, live_args, prefix, snapshot, strip_outputs, ml_history)


def _export_from(live, out, days, t0, live_args, prefix, snapshot, strip_outputs, ml_history=None):
    """Finish export() from a snapshot of the live database; returns (stats, secrets, tokens)."""
    live_names = fixture_db.entity_names(snapshot)
    # Device ids inside entity names (Solax sites and serials, chargers, Octopus devices), replaced wherever they stand as
    # a whole token; the Octopus cache needs them first, so a device id's fake keeps the suffix its entities were renamed to
    tokens = scrub.name_id_tokens(live_names)
    replacements = fixture_cache.export_octopus_cache(live / "cache", out / "cache", t0, tokens=tokens)
    # Device serials, read off the entity names the live config's {geserial}-style templates match; the geserial setting
    # itself may just say "auto". Too short a value would be replaced inside unrelated text, so it is not trusted.
    serials = sorted(value for value in fixture_db.template_values(live_args, live_names) if len(value) >= MIN_SERIAL_LENGTH)
    # The Octopus account number, read off the component's entity names (the live config may hold it as a !secret), in
    # the two spellings it appears in: the account's "A-XXXXXXXX" and the entity names' "a_xxxxxxxx"
    fake_digits = scrub.FAKE_ACCOUNT[2:]
    for digits in fixture_db.octopus_account_ids(live_names, prefix):
        for real, fake in (("a_" + digits, "a_" + fake_digits.lower()), ("A-" + digits.upper(), scrub.FAKE_ACCOUNT), ("a-" + digits, "a-" + fake_digits.lower()), ("A_" + digits.upper(), "A_" + fake_digits)):
            replacements[real] = fake
    for number, serial in enumerate(serials, start=1):
        fake = "{}{}".format(FAKE_SERIAL, number)
        replacements[serial.lower()] = fake.lower()
        replacements[serial.upper()] = fake
    replacements = scrub.with_case_variants(replacements)
    forecast_config, forecast_secrets = [], []
    if live_args.get("forecast_solar"):
        forecast_config, forecast_secrets = fixture_cache.export_forecast_cache(live / "cache", out / "cache", t0, live_args["forecast_solar"])

    forecast_entities = {fixture_db._first_entity(live_args.get(key)) for key in ("pv_forecast_today", "pv_forecast_tomorrow", "pv_forecast_d3", "pv_forecast_d4")} - {None}
    remap = fixture_db.build_remap(live_args, prefix, live_names)
    stats = fixture_db.export_db(snapshot, out / "predbat.db", t0.replace(tzinfo=None), days, remap, prefix, dropped=lambda name: name in forecast_entities, replacements=replacements, strip_outputs=strip_outputs, tokens=tokens)

    apps = build_apps_yaml(load_template(), live_args, forecast_config, stats["totals"], battery_sizing(out / "predbat.db", prefix), load_ml=ml_history is not None)
    apps_text = scrub.replace_tokens(scrub.replace_everywhere(yaml.safe_dump(apps, sort_keys=False), replacements), tokens)
    (out / "apps.yaml").write_text(apps_text)
    manifest = {"t0": t0.isoformat(), "days": days, "db_sha256": _sha256(out / "predbat.db"), "tool_version": TOOL_VERSION, "entities": stats["entities"], "rows": stats["rows"]}
    if ml_history:
        manifest.update(ml_history)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))

    secrets = [real for real in replacements if real] + forecast_secrets
    secrets += [str(live_args[key]) for key in IDENTIFYING_KEYS if isinstance(live_args.get(key), str) and live_args[key]]
    return stats, sorted(set(secrets)), tokens


def main(argv=None):
    """Run the export; return 0 on success, 1 when the output leaked a real value, 2 on a usage error."""
    parser = argparse.ArgumentParser(description="Build the memory benchmark fixture from a live Predbat add-on config directory")
    parser.add_argument("--live", required=True, help="the live add-on config directory (holding apps.yaml, predbat.db and cache/)")
    parser.add_argument("--out", required=True, help="the fixture directory to write, normally coverage/memory_benchmark_data")
    parser.add_argument("--days", type=int, default=7, help="days of history to keep (default 7)")
    parser.add_argument("--strip-outputs", action="store_true", help="leave Predbat's own output entities out (its settings stay)")
    parser.add_argument("--t0", help="the extraction time as an ISO datetime, for tests and re-exports (default now)")
    parser.add_argument("--load-ml", action="store_true", help="also copy the live LoadML history and switch LoadML on, for the LoadML training benchmark (use --days 28)")
    args = parser.parse_args(argv)

    live, out = Path(args.live), Path(args.out)
    if out.exists() and not (out / "manifest.json").exists():
        print("Error: {} exists and was not written by this tool (no manifest.json); not replacing it".format(out))
        return 2
    t0 = datetime.fromisoformat(args.t0) if args.t0 else datetime.now(timezone.utc).replace(microsecond=0)
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=timezone.utc)
    t0 = t0.astimezone(timezone.utc)

    work = out.with_name(out.name + ".tmp")
    if work.exists():
        shutil.rmtree(work)
    try:
        stats, secrets, tokens = export(live, work, args.days, t0, args.strip_outputs, args.load_ml)
        leaks = scrub.find_leaks(work, secrets, tokens)
        personal = scrub.find_personal_data(work)
        # The byte scans skip the compressed LoadML history; its metadata, its only text, is scanned here
        metadata = ml_history_metadata(work)
        found = scrub.scan_text(metadata, secrets, tokens) if metadata is not None else []
        if "real identifier" in found:
            leaks.append(ML_HISTORY)
        personal += [(ML_HISTORY, kind) for kind in found if kind != "real identifier"]
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    if leaks:
        shutil.rmtree(work, ignore_errors=True)
        print("Error: real identifiers survived in {} file(s): {}; no fixture written".format(len(leaks), ", ".join(leaks)))
        return 1
    if personal:
        # What looks like personal data the scrub does not know of: name the file and the kind, never the value
        shutil.rmtree(work, ignore_errors=True)
        print("Error: possible personal data survived ({}); no fixture written".format(", ".join("{} in {}".format(kind, path) for path, kind in personal)))
        return 1
    if out.exists():
        shutil.rmtree(out)
    work.rename(out)
    print("Fixture written to {}: T0 {}, {} entities, {} history rows, {} dropped".format(out, t0.isoformat(), stats["entities"], stats["rows"], stats["dropped"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
