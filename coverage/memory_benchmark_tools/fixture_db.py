# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Export the memory benchmark's reference database from a live Predbat predbat.db.

The live database is read through a snapshot. The export keeps the last N days up to T0 of every entity, Predbat's own
included (it reads some of their history back, and a db_primary install reloads them all; strip_outputs leaves them
out), with the real inverter's history re-keyed onto the dummy inverter's sensors. Identifiers and personal attributes
are scrubbed from every row.
"""

import json
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import types
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "apps" / "predbat"))

from db_engine import DatabaseEngine, TIME_FORMAT_DB  # noqa: E402

import scrub  # noqa: E402

# Live apps.yaml inverter setting -> (dummy inverter sensor name, kind)
INVERTER_REMAP = {
    "soc_kw": ("battery_soc", "gauge_kwh"),
    "soc_max": ("battery_capacity", "gauge_kwh"),
    "battery_power": ("battery_power", "power_w"),
    "pv_power": ("pv_power", "power_w"),
    "grid_power": ("grid_power", "power_w"),
    "load_power": ("load_power", "power_w"),
    "pv_today": ("pv_lifetime", "lifetime"),
    "load_today": ("load_lifetime", "lifetime"),
    "import_today": ("grid_import_lifetime", "lifetime"),
    "export_today": ("grid_export_lifetime", "lifetime"),
}

# Lifetime sensor -> the key of the dummy inverter's totals block it seeds
LIFETIME_TOTALS = {"pv_lifetime": "pv", "load_lifetime": "load", "grid_import_lifetime": "import", "grid_export_lifetime": "export"}

# A placeholder in a templated live setting, such as {geserial}
PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}")

# A counter falling below this fraction of its last value has reset (midnight); a smaller dip is jitter
RESET_FRACTION = 0.5


@dataclass(frozen=True)
class Remap:
    """One real inverter entity whose history is copied to a dummy inverter sensor.

    kind is "lifetime" (an energy counter, converted to a rising kWh total), "power_w" (converted to W) or
    "gauge_kwh" (converted to kWh). invert flips the sign, from the live config's <setting>_invert.
    """

    real_entity: str
    dummy_entity: str
    kind: str
    invert: bool = False


def _first_entity(value):
    """Return the entity id a live apps.yaml setting names (a string or the first item of a list), or None."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, str) and "." in value and " " not in value:
        return value
    return None


def _flag(value):
    """True when a live apps.yaml flag (a bool or a list of them) is set."""
    if isinstance(value, list):
        value = value[0] if value else False
    return value is True or str(value).lower() == "true"


def _template_regex(template):
    """Return a regex matching the entity names a templated setting ({geserial} and the like) can stand for."""
    parts = PLACEHOLDER_RE.split(template)
    return re.compile("^" + "([0-9a-z]+)".join(re.escape(part) for part in parts) + "$")


def _resolve(entity, entity_names):
    """Return the entity name a possibly templated setting resolves to among entity_names, or None."""
    if not PLACEHOLDER_RE.search(entity):
        return entity if entity in entity_names else None
    pattern = _template_regex(entity)
    matches = sorted(name for name in entity_names if pattern.match(name))
    return matches[0] if matches else None


def _strings(value):
    """Yield every string inside a live setting's value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def template_values(live_args, entity_names):
    """Return what the placeholders in the live settings ({geserial} and the like) stand for in entity_names.

    These are real device serials, read off the entity names they appear in, so the export can scrub them.
    """
    found = set()
    for value in live_args.values():
        for text in _strings(value):
            if PLACEHOLDER_RE.search(text) and "." in text:
                pattern = _template_regex(text)
                for name in entity_names:
                    match = pattern.match(name)
                    if match:
                        found.update(group for group in match.groups() if group)
    return found


def _backup_once(source, dest):
    """Copy the database at source to dest with SQLite's backup API, reading source through a read-only connection."""
    live = sqlite3.connect("file:{}?mode=ro".format(Path(source).resolve()), uri=True)
    out = sqlite3.connect(str(dest))
    try:
        live.backup(out)
    finally:
        out.close()
        live.close()


def snapshot_db(source, dest, attempts=5, delay=3):
    """Take a consistent copy of a live predbat.db that another process keeps writing.

    A long read of the live database can collide with its writer (seen as "attempt to write a readonly database" when
    the read-only connection meets a hot journal); the backup API copies a consistent snapshot in one pass, and a
    collision is retried. Raises RuntimeError after the last attempt.
    """
    for attempt in range(1, attempts + 1):
        try:
            if Path(dest).exists():
                Path(dest).unlink()
            _backup_once(source, dest)
            return
        except sqlite3.OperationalError as error:
            if attempt == attempts:
                raise RuntimeError("could not snapshot {} after {} attempts: {}".format(source, attempts, error)) from error
            time.sleep(delay)


def octopus_account_ids(names, prefix):
    """Return the Octopus account numbers (lower case, without "a_") that the Octopus component's entity names carry.

    The component names its entities <domain>.<prefix>_octopus_a_<account digits>_..., so the account number is in
    the database even when the live apps.yaml holds it as a !secret.
    """
    pattern = re.compile(r"\.{}_octopus_a_([0-9a-f]{{8}})_".format(re.escape(prefix)))
    return {match.group(1) for name in names for match in [pattern.search(name)] if match}


def entity_names(db_path):
    """Return the set of entity names in a predbat.db, opened read-only."""
    conn = sqlite3.connect("file:{}?mode=ro".format(Path(db_path).resolve()), uri=True)
    try:
        return {row[0] for row in conn.execute("SELECT entity_name FROM entities")}
    finally:
        conn.close()


def build_remap(live_args, prefix, names=None):
    """Map each live inverter setting to the dummy inverter's matching sensor.

    With names (the live database's entity names), Predbat's own record of the inverter it talks to,
    sensor.<prefix>_givtcp_0_<setting>, is preferred, even for a setting the live config leaves unset; otherwise the
    live setting is used with its placeholders resolved, and a setting that resolves to nothing is skipped. Without
    names the live settings are taken as written.
    """
    remap = []
    for setting, (name, kind) in INVERTER_REMAP.items():
        live = _first_entity(live_args.get(setting))
        if names is None:
            entity = live
        else:
            own = "sensor.{}_givtcp_0_{}".format(prefix, setting)
            entity = own if own in names else (_resolve(live, names) if live else None)
        if entity:
            invert = kind == "power_w" and _flag(live_args.get(setting + "_invert", False))
            remap.append(Remap(entity, "sensor.{}_dummy_{}".format(prefix, name), kind, invert))
    return remap


def daily_to_lifetime(rows):
    """Turn a counter that resets near zero at midnight (or already only rises) into a total that only rises.

    rows are (time, value) pairs, oldest first. Returns the rebased series, starting at 0, and its final total. A
    fall below RESET_FRACTION of the last value is a reset and the new value counts from zero; a smaller dip is
    jitter and is held at the last value.
    """
    series = []
    total = 0.0
    previous = None
    for when, value in rows:
        if previous is not None:
            if value < previous * RESET_FRACTION:
                total += value
                previous = value
            elif value >= previous:
                total += value - previous
                previous = value
        else:
            previous = value
        series.append((when, total))
    return series, total


def settings_entities(prefix):
    """Return the entity id of every Predbat setting in config.CONFIG_ITEMS, named <type>.<prefix>_<name>.

    config.py takes THIS_VERSION from predbat.py, which imports the whole app; when predbat is not already loaded a
    stand-in module supplies just that name while config is imported, so this tool does not need the app's dependencies.
    """
    stand_in = None
    if "predbat" not in sys.modules:
        stand_in = types.ModuleType("predbat")
        stand_in.THIS_VERSION = "memory-benchmark"
        sys.modules["predbat"] = stand_in
    try:
        from config import CONFIG_ITEMS
    finally:
        if stand_in is not None and sys.modules.get("predbat") is stand_in:
            del sys.modules["predbat"]

    return {"{}.{}_{}".format(item["type"], prefix, item["name"]) for item in CONFIG_ITEMS if item.get("type") != "update"}


def is_predbat_output(entity_id, prefix, settings):
    """True for predbat.* and any other <domain>.<prefix>_* entity that is not one of Predbat's settings."""
    if entity_id.startswith("predbat."):
        return True
    domain, _, name = entity_id.partition(".")
    return name.startswith(prefix + "_") and entity_id not in settings


def _number(state):
    """Return state as a float, or None when it is not a number (unavailable, unknown)."""
    try:
        return float(state)
    except (TypeError, ValueError):
        return None


def _scale(unit, kind):
    """Return the factor that turns a value in unit into the dummy's unit for kind (kWh, or W for power)."""
    unit = (unit or "").strip()
    if kind == "power_w":
        return 1000.0 if unit == "kW" else 1.0
    return 0.001 if unit in ("Wh", "W") else 1.0


def _converted_rows(remap, rows):
    """Convert one real entity's (datetime text, state, attributes json, system, keep) rows for its dummy sensor.

    Returns (rows, final value), the rows in the same shape with the dummy's unit in the attributes.
    """
    unit_out = "W" if remap.kind == "power_w" else "kWh"
    values = []
    for when, state, attributes, system, keep in rows:
        number = _number(state)
        if number is None:
            continue
        try:
            unit = json.loads(attributes or "{}").get("unit_of_measurement")
        except json.JSONDecodeError:
            unit = None
        number *= _scale(unit, remap.kind)
        if remap.invert:
            number = -number
        values.append((when, number, system, keep))
    if remap.kind == "lifetime":
        series, final = daily_to_lifetime([(when, number) for when, number, _, _ in values])
        values = [(when, total, system, keep) for (when, total), (_, _, system, keep) in zip(series, values)]
    else:
        final = values[-1][1] if values else 0.0
    attributes = {"unit_of_measurement": unit_out}
    if remap.kind == "lifetime":
        attributes["state_class"] = "total_increasing"
    attributes_json = json.dumps(attributes)
    return [(when, str(round(number, 6)), attributes_json, system, keep) for when, number, system, keep in values], final


def _scrub_text(text, replacements, tokens):
    """Apply the known replacements and the device-id tokens to one piece of text."""
    text = scrub.replace_everywhere(text, replacements)
    return scrub.replace_tokens(text, tokens) if tokens and isinstance(text, str) else text


def _scrub_row(row, replacements, tokens):
    """Scrub one (datetime, state, attributes json, system, keep) row: identifiers, then personal attributes by category."""
    when, state, attributes, system, keep = row
    attributes = _scrub_text(attributes, replacements, tokens)
    try:
        parsed = json.loads(attributes) if attributes else None
    except (TypeError, json.JSONDecodeError):
        parsed = None
    if isinstance(parsed, dict):
        attributes = json.dumps(scrub.scrub_personal_attributes(parsed))
    return (when, _scrub_text(state, replacements, tokens), attributes, system, keep)


class _EngineBase:
    """Just enough of PredBat for DatabaseEngine to create an empty database with its own schema."""

    def __init__(self, root):
        self.config_root = str(root)
        self.now_utc_real = datetime.utcnow()
        self.log = lambda *args, **kwargs: None


def _create_empty_db(out_db):
    """Create out_db with exactly the schema DatabaseEngine creates."""
    with tempfile.TemporaryDirectory() as tmp:
        DatabaseEngine(_EngineBase(tmp), 30)._close()
        shutil.copyfile(Path(tmp) / "predbat.db", out_db)


def export_db(live_db, out_db, t0, days, remap, prefix, dropped, replacements, strip_outputs=False, tokens=None):
    """Write out_db from live_db: the last days days up to t0 of every exported entity, and their latest states.

    t0 is naive UTC, as the database stores times. Exported are the entities that dropped() does not reject and that
    are not remapped real inverter entities, including Predbat's own (it reads some of their history back, and a
    db_primary install reloads them all); strip_outputs leaves out Predbat's outputs, keeping its settings; the remapped entities' history is
    written under the dummy's sensor names instead. replacements (real value: fake) and tokens (device id read off entity
    names: fake, matched as whole tokens) are applied to every name, state and attribute, and personal attributes
    (coordinates, postcodes, contact details) are replaced by category.
    Returns {"entities", "rows", "totals" (the dummy's starting lifetime totals), "dropped", "merged"}, merged counting the
    entities left out because another had already scrubbed to the same name.
    """
    out_db = Path(out_db)
    if out_db.exists():
        out_db.unlink()
    _create_empty_db(out_db)
    settings = settings_entities(prefix)
    remap_by_entity = {item.real_entity: item for item in remap}
    start = (t0 - timedelta(days=days)).strftime(TIME_FORMAT_DB)
    end = t0.strftime(TIME_FORMAT_DB)

    live = sqlite3.connect("file:{}?mode=ro".format(Path(live_db).resolve()), uri=True)
    out = sqlite3.connect(str(out_db))
    stats = {"entities": 0, "rows": 0, "totals": {"pv": 0.0, "load": 0.0, "import": 0.0, "export": 0.0}, "dropped": 0, "merged": 0}
    exported = set()
    try:
        entities = live.execute("SELECT entity_index, entity_name FROM entities ORDER BY entity_index").fetchall()
        for entity_index, name in entities:
            item = remap_by_entity.get(name)
            if item is None and (dropped(name) or (strip_outputs and is_predbat_output(name, prefix, settings))):
                stats["dropped"] += 1
                continue
            rows = live.execute("SELECT datetime, state, attributes, system, keep FROM states WHERE entity_index = ? AND datetime >= ? AND datetime <= ? ORDER BY datetime", (entity_index, start, end)).fetchall()
            latest = live.execute("SELECT datetime, state, attributes, system, keep FROM latest WHERE entity_index = ?", (entity_index,)).fetchone()
            if item is not None:
                if not rows and latest is not None:
                    # Recorded only on change and unchanged for the whole window (battery capacity): its latest value
                    # still holds at T0
                    rows = [latest]
                rows, final = _converted_rows(item, rows)
                latest = rows[-1] if rows else None
                total_key = LIFETIME_TOTALS.get(item.dummy_entity.rsplit("_dummy_", 1)[-1])
                if total_key:
                    stats["totals"][total_key] = round(final, 6)
                name = item.dummy_entity
            else:
                if not rows or rows[0][0] > start:
                    # The state at the window's start: the newest change before it, stamped at the start, as a history
                    # query of a recorder returns it (the database itself only records changes)
                    before = live.execute("SELECT datetime, state, attributes, system, keep FROM states WHERE entity_index = ? AND datetime < ? ORDER BY datetime DESC LIMIT 1", (entity_index, start)).fetchone()
                    if before is not None:
                        rows = [(start,) + tuple(before[1:])] + list(rows)
                name = _scrub_text(name, replacements, tokens)
                rows = [_scrub_row(row, replacements, tokens) for row in rows]
                latest = _scrub_row(latest, replacements, tokens) if latest else None
            if not rows and latest is None:
                continue
            if name in exported:
                # Another entity scrubbed to the same name (two spellings of one serial): keep the first
                stats["merged"] += 1
                continue
            exported.add(name)
            cursor = out.execute("INSERT INTO entities (entity_name) VALUES (?)", (name,))
            new_index = cursor.lastrowid
            out.executemany("INSERT INTO states (datetime, entity_index, state, attributes, system, keep) VALUES (?, ?, ?, ?, ?, ?)", [(when, new_index, state, attributes, system, keep) for when, state, attributes, system, keep in rows])
            if latest is not None:
                out.execute("INSERT INTO latest (entity_index, datetime, state, attributes, system, keep) VALUES (?, ?, ?, ?, ?, ?)", (new_index,) + tuple(latest))
            stats["entities"] += 1
            stats["rows"] += len(rows)
        out.commit()
    finally:
        live.close()
        out.close()
    return stats
