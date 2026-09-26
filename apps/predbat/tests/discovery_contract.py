# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Contract checks every inverter reporter's tests run against its discovery record.

Completeness: the record alone rebuilds the reporter's INVERTER_DEF row (no row as a base - with one, a
field the record left out would silently inherit the right answer and prove nothing). Agreement: every
setting the component's automatic_config() binds is in the record, so the coordinator can later take
automatic configuration over from it. See docs/superpowers/specs/2026-09-24-discovery-inverter-record-vocabulary-design.md.
"""

import asyncio
import inspect
import re

from config import INVERTER_DEF
from coordinator import PRESENCE_FLAGS, inverter_definition, validate_report

# inverter.py's own defaults for the fields some rows leave out (inverter.py:400-432)
ROW_DEFAULTS = {
    "current_dp": 1,
    "support_feedin_first": False,
    "has_ge_eco_toggle": False,
    "charge_discharge_with_rate": False,
    "target_soc_used_for_discharge": True,
}

# Row fields the record is not expected to rebuild: the display name, and two fields nothing reads
NOT_COMPARED = ("name", "has_time_window", "has_rest_api")

# Settings automatic_config() sets that are not facts about this inverter (spec section 1.5)
SETTINGS_OUTSIDE_THE_RECORD = frozenset(
    {
        "inverter_type",
        "num_inverters",
        "givtcp_rest",
        "ge_cloud_data",
        "ge_cloud_direct",
        "ge_cloud_serial",
        "num_cars",
        "car_charging_energy",
        "car_charging_planned",
        "car_charging_power",
    }
)

# A *_invert setting describes the sign of the entity it names
INVERT_SETTINGS = {"grid_power_invert": "grid_power", "battery_power_invert": "battery_power", "load_power_invert": "load_power"}

# Bound once for the whole site, from the first driven inverter (spec D15) - expected on that record only
SITE_SETTINGS = frozenset({"battery_temperature_history"})

# GE Cloud builds entity ids from the raw, upper-case serial, so the object id may hold capitals
ENTITY_ID_RE = re.compile(r"^[a-z_]+\.[A-Za-z0-9_]+$")


def row_value(inverter_type, field):
    """The value inverter.py would use for one field of an INVERTER_DEF row."""
    row = INVERTER_DEF[inverter_type]
    return row[field] if field in row else ROW_DEFAULTS[field]


def dummied_settings(inverter_type):
    """The settings inverter.py replaces with a dummy entity for this inverter type, so a record leaves them out."""
    row = INVERTER_DEF[inverter_type]
    settings = {setting for flag, names in PRESENCE_FLAGS.items() if not row.get(flag, False) for setting in names}
    if not row.get("has_ge_inverter_mode", False) and not row.get("has_ge_eco_toggle", False):
        settings.add("inverter_mode")
    return settings


def validated_inverters(report):
    """Run a report through validate_report() and return its inverter records, failing if any record was dropped."""
    logs = []
    cleaned = validate_report(report, "contract", logs.append)
    raw = report.get("inverters") or []
    records = cleaned.get("inverters") or []
    assert len(records) == len(raw), "validate_report() dropped {} of {} inverter records: {}".format(len(raw) - len(records), len(raw), logs)
    return records


def assert_definition_complete(record, write_and_poll_sleep, except_fields=()):
    """Fail unless the record alone rebuilds its INVERTER_DEF row: no gaps, and every applicable field equal.

    except_fields names row fields the record deliberately describes differently (spec D13: the GE family's
    clock_time_format, which the record gives as the ISO format its sensor really publishes). Each must still
    differ - an exception that no longer applies fails, so it is removed once the row is fixed.
    """
    inverter_type = record["inverter_type"]
    definition, gaps, not_applicable = inverter_definition(record, write_and_poll_sleep)
    assert not gaps, "{} record cannot rebuild these INVERTER_DEF fields: {}".format(inverter_type, gaps)
    stale = [field for field in except_fields if definition.get(field) == row_value(inverter_type, field)]
    assert not stale, "{} exceptions no longer needed - the record now matches the row: {}".format(inverter_type, stale)
    fields = (set(INVERTER_DEF[inverter_type]) | set(ROW_DEFAULTS)) - set(NOT_COMPARED) - set(not_applicable) - set(except_fields)
    mismatches = {field: {"record": definition.get(field), "row": row_value(inverter_type, field)} for field in sorted(fields) if definition.get(field) != row_value(inverter_type, field)}
    assert not mismatches, "{} record disagrees with its INVERTER_DEF row: {}".format(inverter_type, mismatches)


def capture_automatic_config(component):
    """Run component.automatic_config() with set_arg/set_arg_auto recorded instead of applied; return {setting: value}.

    The two setters are replaced on the instance only and removed afterwards, so the class methods are
    back in place whatever automatic_config() raised.
    """
    captured = {}

    def record(arg, value, *args, **kwargs):
        """Remember the last value bound to each setting."""
        captured[arg] = value

    component.set_arg = record
    component.set_arg_auto = record
    try:
        result = component.automatic_config()
        if inspect.isawaitable(result):
            asyncio.run(result)
    finally:
        del component.set_arg
        del component.set_arg_auto
    return captured


def _is_true(value):
    """Whether an invert setting's value means True - components pass True or the string "True"."""
    return value is True or str(value).lower() == "true"


def assert_record_agrees(record, captured, index=0):
    """Fail unless every setting automatic_config() bound for device `index` appears in the record.

    Skips the settings that are not facts about the inverter (SETTINGS_OUTSIDE_THE_RECORD), resets to None,
    and settings inverter.py replaces with a dummy for this type. An entity-id value must match the
    descriptor's entity_id; a *_invert value must match the named entity's invert; any other literal must
    match the descriptor's value stand-in or the rating of the same name.
    """
    inverter_type = record["inverter_type"]
    entities = record.get("entities") or {}
    ratings = record.get("ratings") or {}
    skipped = SETTINGS_OUTSIDE_THE_RECORD | dummied_settings(inverter_type)
    if index != 0:
        skipped = skipped | SITE_SETTINGS
    problems = []
    for setting, value in sorted(captured.items()):
        if setting in skipped or value is None:
            continue
        if isinstance(value, list):
            if index >= len(value):
                problems.append("{}: automatic_config() bound no value for device {}".format(setting, index))
                continue
            value = value[index]
        if setting in INVERT_SETTINGS:
            actual = bool((entities.get(INVERT_SETTINGS[setting]) or {}).get("invert", False))
            if actual != _is_true(value):
                problems.append("{}: automatic_config() sets {}, record's {} invert is {}".format(setting, value, INVERT_SETTINGS[setting], actual))
            continue
        descriptor = entities.get(setting) or {}
        if isinstance(value, str) and ENTITY_ID_RE.match(value):
            if descriptor.get("entity_id") != value:
                problems.append("{}: automatic_config() binds {}, record has {}".format(setting, value, descriptor.get("entity_id")))
        elif "value" in descriptor:
            if descriptor["value"] != value:
                problems.append("{}: automatic_config() sets {}, record's value is {}".format(setting, value, descriptor["value"]))
        elif ratings.get(setting) != value:
            problems.append("{}: automatic_config() sets {}, record has neither a value stand-in nor a matching rating ({})".format(setting, value, ratings.get(setting)))
    assert not problems, "{} record disagrees with automatic_config(): {}".format(inverter_type, problems)


def assert_record_binds_nothing_extra(record, captured, index=0, allowed_extra=()):
    """Fail if the record's entities bind a setting automatic_config() did not bind for device `index`.

    The reverse of assert_record_agrees(): together they pin the record to exactly what automatic_config()
    binds. allowed_extra names settings the record carries on purpose although automatic_config() skipped
    them - a device fact the user told Predbat to ignore (spec D11), or one a fleet-wide gate withheld (D10).
    """
    bound = set()
    for setting, value in captured.items():
        if isinstance(value, list):
            value = value[index] if index < len(value) else None
        if value is not None:
            bound.add(setting)
    extra = sorted(set(record.get("entities") or {}) - bound - set(allowed_extra))
    assert not extra, "{} record binds settings automatic_config() did not bind for device {}: {}".format(record["inverter_type"], index, extra)
