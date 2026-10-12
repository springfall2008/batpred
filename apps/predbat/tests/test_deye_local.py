# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# pylint: disable=too-many-lines
# -----------------------------------------------------------------------------
# Test DEYE local inverter integration (deye_local.py)
#
# The new runtime module apps/predbat/deye_local.py is wired into
# inverter.py's refresh_config()/adjust_charge_immediate()/adjust_export_immediate().
# It is opt-in via apps.yaml's `deye_local` setting, and replaces the legacy
# service-template and time-entity mappings for DEYE hardware by owning six
# ToU program entities (soc, power, charging, time) plus three select entities
# (time_of_use, work_mode, energy_pattern) and a discharge-power cap.
#
# Scope of these tests:
#   - configuration (prepare, validate_limits)
#   - actuator scopes (only the six SOC + power + source fields write)
#   - charging-source interlock, with bounded retries
#   - execute_plan integration through real Inverter.adjust_charge_immediate()
#   - rejection paths for malformed inputs
#   - read_only short-circuit
#   - native freeze semantics (power=0, source=Disabled)
#
# Out of scope:
#   - SUNSYNK/DEYE cloud SK behaviour
#   - hardware calls (network connections blocked by the test runner)
#   - real HA settings.yaml plumbing (we touch only entities in dummy_items)
# -----------------------------------------------------------------------------

"""Regression tests for the new DEYE local inverter runtime (``deye_local.py``).

These tests build a real ``Inverter`` object with ``inverter_type='DEYE'`` and
exercise the new ``DeyeLocal`` adapter end-to-end through both the direct API
(``DeyeLocal.apply`` / ``snapshot`` / ``validate_limits``) and the public
``Inverter.adjust_charge_immediate`` / ``Inverter.adjust_export_immediate``
hooks that ``execute_plan`` drives. Network access is blocked - the
``TestHAInterface`` mock answers every call - and the only entities the
adapter is allowed to write are the six SOC, power and charging-source fields.
Times and operating-mode selects are read-only. Unchanged controls are asserted, never
assumed.
"""

import copy
import math
import traceback
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


from inverter import Inverter

# The legacy keys the DEYE local adapter rejects in apps.yaml. These must be
# absent from both `args` (live state) and `args_from_apps_yaml` (the
# pre-defaulting snapshot Inverter.user_configured_entity reads), so the
# helper strips them from both before building the Inverter. Mirrors the
# `outputs` tuple in deye_local.prepare() so any new rejection key gets
# added here as well.
_LEGACY_DEYE_KEYS = (
    "charge_start_service",
    "charge_stop_service",
    "charge_freeze_service",
    "discharge_start_service",
    "discharge_stop_service",
    "discharge_freeze_service",
    "charge_start_time",
    "charge_end_time",
    "discharge_start_time",
    "discharge_end_time",
    "charge_limit",
    "reserve",
    "charge_rate",
    "discharge_rate",
    "timed_charge_current",
    "timed_discharge_current",
    "charge_rate_percent",
    "discharge_rate_percent",
    "charge_limit_enable",
    "pause_mode",
    "charge_start_hour",
    "charge_end_hour",
    "charge_start_minute",
    "charge_end_minute",
    "discharge_start_hour",
    "discharge_end_hour",
    "discharge_start_minute",
    "discharge_end_minute",
)


# Default "now" in UTC used for the telemetry_time sensor. Kept within the
# snapshot's [-30 s, +180 s] freshness window so control=True configs are
# not rejected as stale. The seed helper re-evaluates this on every call
# so a slow test suite does not drift outside the freshness window mid-run.
def _fresh_now_iso():
    """Return a fresh synthetic telemetry timestamp for this isolated test."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _program_rows(prefix):
    """Return six ToU program rows with distinct entity ids and increasing times.

    Times start at 00:00 (or the offset given by ``prefix``) and step every four
    hours so all six rows remain strictly ascending and cyclically partition
    the day. Every entity id is anonymised - no user-facing prefix leaks.
    """
    rows = []
    for index in range(6):
        hour = (index * 4) % 24
        rows.append(
            {
                "time": "time.dt_{}_{:02d}00".format(prefix, hour),
                "soc": "number.ds_{}_{}".format(prefix, index),
                "power": "number.dp_{}_{}".format(prefix, index),
                "charging": "select.dc_{}_{}".format(prefix, index),
            }
        )
    return rows


def _seed_entities(my_predbat, rows, *, tou="Week", work="Zero Export To CT", energy="Load First", soc=50, power=0, source="Disabled", max_power_w=5000, prefix="p", control=False, telemetry_iso=None):
    """Seed the HA observation cache with valid deye_local entities.

    Each entity carries the full ``{"state": ..., "attributes": {...}}`` shape
    the production ``ha.py.get_state(raw=True)`` returns; the ``TestHAInterface``
    flat shape would not expose ``attributes`` to the snapshot validator.

    ``telemetry_iso`` defaults to the wall-clock "now" so a slow test suite
    does not let the telemetry_time sensor drift outside the snapshot's
    [-30, +180] second freshness window.
    """
    if telemetry_iso is None:
        telemetry_iso = _fresh_now_iso()
    my_predbat.ha_interface.dummy_items["select.dt_{}_tou".format(prefix)] = {
        "state": tou,
        "attributes": {"options": ["Week", "Enabled", "Disabled"]},
    }
    my_predbat.ha_interface.dummy_items["select.dt_{}_wm".format(prefix)] = {
        "state": work,
        "attributes": {"options": ["Zero Export To CT", "Zero Export To Load", "Selling First"]},
    }
    my_predbat.ha_interface.dummy_items["select.dt_{}_ep".format(prefix)] = {
        "state": energy,
        "attributes": {"options": ["Load First", "Selling First"]},
    }
    my_predbat.ha_interface.dummy_items["sensor.dt_{}_tt".format(prefix)] = {
        "state": telemetry_iso,
        "attributes": {},
    }
    for index, row in enumerate(rows):
        my_predbat.ha_interface.dummy_items[row["time"]] = {
            "state": "{:02d}:00".format((index * 4) % 24),
            "attributes": {},
        }
        my_predbat.ha_interface.dummy_items[row["soc"]] = {
            "state": int(soc),
            "attributes": {"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"},
        }
        my_predbat.ha_interface.dummy_items[row["power"]] = {
            "state": int(power),
            "attributes": {"min": 0, "max": int(max_power_w), "step": 1, "unit_of_measurement": "W"},
        }
        my_predbat.ha_interface.dummy_items[row["charging"]] = {
            "state": source,
            "attributes": {"options": ["Disabled", "Grid", "Generator", "Both"]},
        }
    return control


def _make_inverter(my_predbat, *, max_power_w=5000, num_inverters=1, prefix="p", soc=50, power=0, source="Disabled", control=False, hold_verified=False):
    """Build a real Inverter wired to a fresh DeyeLocal, returning it.

    Strips every legacy apps.yaml key the adapter rejects, sets a single
    inverter of type DEYE, seeds the HA observation cache with synthetic
    entities, and replaces the inverter's sleep with a no-op so the test
    suite never actually waits.
    """
    saved_args = dict(my_predbat.args)
    had_raw = hasattr(my_predbat, "args_from_apps_yaml")
    saved_raw = copy.deepcopy(getattr(my_predbat, "args_from_apps_yaml", None)) if had_raw else None
    saved_items = dict(getattr(my_predbat.ha_interface, "dummy_items", {}))

    try:
        for key in _LEGACY_DEYE_KEYS:
            my_predbat.args.pop(key, None)
            if had_raw and saved_raw is not None and key in saved_raw:
                del saved_raw[key]
        if not had_raw:
            my_predbat.args_from_apps_yaml = dict(my_predbat.args)
        else:
            my_predbat.args_from_apps_yaml = saved_raw

        my_predbat.args["inverter_type"] = ["DEYE"]
        my_predbat.args["num_inverters"] = num_inverters
        my_predbat.args["inverter"] = None

        rows = _program_rows(prefix)
        settings = {
            "programs": [dict(row) for row in rows],
            "time_of_use": "select.dt_{}_tou".format(prefix),
            "work_mode": "select.dt_{}_wm".format(prefix),
            "energy_pattern": "select.dt_{}_ep".format(prefix),
            "max_discharge_power_w": int(max_power_w),
            "control": bool(control),
            "hold_verified": bool(hold_verified),
        }
        if control:
            settings["telemetry_time"] = "sensor.dt_{}_tt".format(prefix)
        my_predbat.args["deye_local"] = settings
        # Make sure the snapshot used by user_configured_entity contains the
        # legacy keys as None - the prepare() guard walks every key explicitly.
        my_predbat.args_from_apps_yaml = dict(my_predbat.args)

        _seed_entities(
            my_predbat,
            rows,
            max_power_w=max_power_w,
            soc=soc,
            power=power,
            source=source,
            prefix=prefix,
            control=control,
        )

        inverter = Inverter(my_predbat, 0, quiet=True)
        inverter.sleep = _dummy_sleep
        return inverter, rows, saved_args, had_raw, saved_raw, saved_items
    except Exception:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)
        if had_raw:
            my_predbat.args_from_apps_yaml = saved_raw
        raise


def _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items):
    """Restore the per-test args/args_from_apps_yaml/HA cache state."""
    my_predbat.args.clear()
    my_predbat.args.update(saved_args)
    if had_raw:
        if saved_raw is not None:
            my_predbat.args_from_apps_yaml = saved_raw
        elif hasattr(my_predbat, "args_from_apps_yaml"):
            del my_predbat.args_from_apps_yaml
    items = getattr(my_predbat.ha_interface, "dummy_items", None)
    if items is not None:
        items.clear()
        items.update(saved_items)


def _dummy_sleep(seconds):
    """No-op replacement for Inverter.sleep - keeps tests fast and side-effect-free."""
    return None


def _monotonic_factory():
    """Return (clock, advance) so a test can drive the bounded-retry deadline."""
    state = {"value": 0.0}

    def clock():
        """Read the synthetic monotonic clock."""
        return state["value"]

    def advance(seconds):
        """Move the synthetic monotonic clock forward."""
        state["value"] += seconds

    return clock, advance


def _scrub_base(adapter, prefix="p"):
    """Strip the entities a fresh adapter is allowed to write so we can detect changes."""
    rows = adapter.settings["programs"]
    entities = []
    for row in rows:
        entities.extend([row["soc"], row["power"], row["charging"]])
    return set(entities)


def _reset_service_log(my_predbat):
    """Clear the captured services so the next assertion starts from a clean slate."""
    my_predbat.ha_interface.service_store = []
    my_predbat.ha_interface.service_store_fail = set()


def _install_attribute_preserving_service_wrapper(my_predbat):
    """Replace call_service_wrapper so writes preserve the ``attributes`` dict.

    The TestHAInterface's default ``call_service`` for ``number/set_value``
    overwrites ``dummy_items[entity]`` with the bare value, dropping the
    ``attributes`` field that ``snapshot()`` reads. This wrapper rewrites
    the entity's nested dict with the new state and the original
    attributes, so the next snapshot() still sees min/max/step/unit.
    """
    original_call = my_predbat.call_service_wrapper
    cache = my_predbat.ha_interface.dummy_items

    def preserving_call(service, **kwargs):
        """Apply synthetic writes without discarding the control metadata."""
        if service in ("number/set_value", "select/select_option"):
            entity_id = kwargs.get("entity_id")
            existing = cache.get(entity_id)
            if isinstance(existing, dict):
                if service == "number/set_value":
                    new_state = kwargs.get("value", 0)
                else:
                    new_state = kwargs.get("option")
                cache[entity_id] = dict(existing)
                cache[entity_id]["state"] = new_state
        return original_call(service, **kwargs)

    my_predbat.call_service_wrapper = preserving_call
    return original_call


def test_prepare_rejects_more_than_one_inverter(my_predbat):
    """prepare() refuses to wire up DEYE local on a multi-inverter install.

    The adapter only knows how to drive six physical ToU rows and assumes they
    belong to inverter 0, so a multi-inverter setup would silently conflict.
    """
    failed = False
    saved_args = dict(my_predbat.args)
    had_raw = hasattr(my_predbat, "args_from_apps_yaml")
    saved_raw = copy.deepcopy(getattr(my_predbat, "args_from_apps_yaml", None)) if had_raw else None
    saved_items = dict(getattr(my_predbat.ha_interface, "dummy_items", {}))
    try:
        for key in _LEGACY_DEYE_KEYS:
            my_predbat.args.pop(key, None)
            if had_raw and saved_raw is not None and key in saved_raw:
                del saved_raw[key]
        if not had_raw:
            my_predbat.args_from_apps_yaml = dict(my_predbat.args)
        else:
            my_predbat.args_from_apps_yaml = saved_raw

        my_predbat.args["inverter_type"] = ["DEYE", "DEYE"]
        my_predbat.args["num_inverters"] = 2
        my_predbat.args["inverter"] = None
        rows = _program_rows("reject")
        my_predbat.args["deye_local"] = {
            "programs": [dict(row) for row in rows],
            "time_of_use": "select.dt_reject_tou",
            "work_mode": "select.dt_reject_wm",
            "energy_pattern": "select.dt_reject_ep",
            "max_discharge_power_w": 5000,
            "control": False,
            "hold_verified": False,
        }
        my_predbat.args_from_apps_yaml = dict(my_predbat.args)
        _seed_entities(my_predbat, rows)

        try:
            Inverter(my_predbat, 0, quiet=True)
            print("ERROR: multi-inverter should raise, did not")
            failed = True
        except ValueError as exc:
            if "one inverter" not in str(exc):
                print("ERROR: wrong rejection reason: {}".format(exc))
                failed = True
    finally:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)
        if had_raw:
            my_predbat.args_from_apps_yaml = saved_raw
        items = getattr(my_predbat.ha_interface, "dummy_items", None)
        if items is not None:
            items.clear()
            items.update(saved_items)
    assert not failed, "test_prepare_rejects_more_than_one_inverter"


def test_prepare_rejects_legacy_service_keys(my_predbat):
    """prepare() rejects apps.yaml charge/discharge service templates.

    DEYE local owns the actuator space entirely; coexistence with the legacy
    templates would mean two writers for the same intent.
    """
    failed = False
    saved_args = dict(my_predbat.args)
    had_raw = hasattr(my_predbat, "args_from_apps_yaml")
    saved_raw = copy.deepcopy(getattr(my_predbat, "args_from_apps_yaml", None)) if had_raw else None
    saved_items = dict(getattr(my_predbat.ha_interface, "dummy_items", {}))
    try:
        for key in _LEGACY_DEYE_KEYS:
            my_predbat.args.pop(key, None)
            if had_raw and saved_raw is not None and key in saved_raw:
                del saved_raw[key]
        # Plant a single legacy service key; prepare must surface it.
        my_predbat.args["charge_start_service"] = "switch.svc"
        if had_raw:
            saved_raw["charge_start_service"] = "switch.svc"
        else:
            my_predbat.args_from_apps_yaml = dict(my_predbat.args)

        my_predbat.args["inverter_type"] = ["DEYE"]
        my_predbat.args["num_inverters"] = 1
        my_predbat.args["inverter"] = None
        rows = _program_rows("svc")
        my_predbat.args["deye_local"] = {
            "programs": [dict(row) for row in rows],
            "time_of_use": "select.dt_svc_tou",
            "work_mode": "select.dt_svc_wm",
            "energy_pattern": "select.dt_svc_ep",
            "max_discharge_power_w": 5000,
            "control": False,
            "hold_verified": False,
        }
        if had_raw:
            saved_raw["deye_local"] = my_predbat.args["deye_local"]
        _seed_entities(my_predbat, rows)

        try:
            Inverter(my_predbat, 0, quiet=True)
            print("ERROR: legacy service key should be rejected")
            failed = True
        except ValueError as exc:
            if "legacy service" not in str(exc):
                print("ERROR: wrong rejection reason: {}".format(exc))
                failed = True
    finally:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)
        if had_raw:
            my_predbat.args_from_apps_yaml = saved_raw
        items = getattr(my_predbat.ha_interface, "dummy_items", None)
        if items is not None:
            items.clear()
            items.update(saved_items)
    assert not failed, "test_prepare_rejects_legacy_service_keys"


def test_prepare_rejects_legacy_generic_mappings(my_predbat):
    """prepare() rejects apps.yaml charge_limit/reserve/charge_rate/etc entities."""
    failed = False
    saved_args = dict(my_predbat.args)
    had_raw = hasattr(my_predbat, "args_from_apps_yaml")
    saved_raw = copy.deepcopy(getattr(my_predbat, "args_from_apps_yaml", None)) if had_raw else None
    saved_items = dict(getattr(my_predbat.ha_interface, "dummy_items", {}))
    try:
        for key in _LEGACY_DEYE_KEYS:
            my_predbat.args.pop(key, None)
            if had_raw and saved_raw is not None and key in saved_raw:
                del saved_raw[key]
        if not had_raw:
            my_predbat.args_from_apps_yaml = dict(my_predbat.args)
            had_raw = True
            saved_raw = dict(my_predbat.args_from_apps_yaml)
        my_predbat.args["charge_limit"] = ["number.dt_legacy_charge_limit"]
        saved_raw["charge_limit"] = ["number.dt_legacy_charge_limit"]

        my_predbat.args["inverter_type"] = ["DEYE"]
        my_predbat.args["num_inverters"] = 1
        my_predbat.args["inverter"] = None
        rows = _program_rows("gen")
        my_predbat.args["deye_local"] = {
            "programs": [dict(row) for row in rows],
            "time_of_use": "select.dt_gen_tou",
            "work_mode": "select.dt_gen_wm",
            "energy_pattern": "select.dt_gen_ep",
            "max_discharge_power_w": 5000,
            "control": False,
            "hold_verified": False,
        }
        saved_raw["deye_local"] = my_predbat.args["deye_local"]
        my_predbat.args_from_apps_yaml = saved_raw
        _seed_entities(my_predbat, rows)

        try:
            Inverter(my_predbat, 0, quiet=True)
            print("ERROR: legacy generic mapping should be rejected")
            failed = True
        except ValueError as exc:
            if "charge_limit" not in str(exc):
                print("ERROR: wrong rejection reason: {}".format(exc))
                failed = True
    finally:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)
        if had_raw:
            my_predbat.args_from_apps_yaml = saved_raw
        items = getattr(my_predbat.ha_interface, "dummy_items", None)
        if items is not None:
            items.clear()
            items.update(saved_items)
    assert not failed, "test_prepare_rejects_legacy_generic_mappings"


def test_prepare_rejects_capability_overrides(my_predbat):
    """prepare() refuses apps.yaml's `inverter:` capability overrides."""
    failed = False
    saved_args = dict(my_predbat.args)
    had_raw = hasattr(my_predbat, "args_from_apps_yaml")
    saved_raw = copy.deepcopy(getattr(my_predbat, "args_from_apps_yaml", None)) if had_raw else None
    saved_items = dict(getattr(my_predbat.ha_interface, "dummy_items", {}))
    try:
        for key in _LEGACY_DEYE_KEYS:
            my_predbat.args.pop(key, None)
            if had_raw and saved_raw is not None and key in saved_raw:
                del saved_raw[key]
        if not had_raw:
            my_predbat.args_from_apps_yaml = dict(my_predbat.args)
            had_raw = True
            saved_raw = dict(my_predbat.args_from_apps_yaml)

        my_predbat.args["inverter_type"] = ["DEYE"]
        my_predbat.args["num_inverters"] = 1
        my_predbat.args["inverter"] = {"charge_time_format": "HH:MM:SS"}
        saved_raw["inverter"] = {"charge_time_format": "HH:MM:SS"}
        rows = _program_rows("cap")
        my_predbat.args["deye_local"] = {
            "programs": [dict(row) for row in rows],
            "time_of_use": "select.dt_cap_tou",
            "work_mode": "select.dt_cap_wm",
            "energy_pattern": "select.dt_cap_ep",
            "max_discharge_power_w": 5000,
            "control": False,
            "hold_verified": False,
        }
        saved_raw["deye_local"] = my_predbat.args["deye_local"]
        my_predbat.args_from_apps_yaml = saved_raw
        _seed_entities(my_predbat, rows)

        try:
            Inverter(my_predbat, 0, quiet=True)
            print("ERROR: inverter override should be rejected")
            failed = True
        except ValueError as exc:
            if "capability" not in str(exc):
                print("ERROR: wrong rejection reason: {}".format(exc))
                failed = True
    finally:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)
        if had_raw:
            my_predbat.args_from_apps_yaml = saved_raw
        items = getattr(my_predbat.ha_interface, "dummy_items", None)
        if items is not None:
            items.clear()
            items.update(saved_items)
    assert not failed, "test_prepare_rejects_capability_overrides"


def test_prepare_rejects_wrong_number_of_programs(my_predbat):
    """prepare() requires exactly six physical ToU programs."""
    failed = False
    saved_args = dict(my_predbat.args)
    had_raw = hasattr(my_predbat, "args_from_apps_yaml")
    saved_raw = copy.deepcopy(getattr(my_predbat, "args_from_apps_yaml", None)) if had_raw else None
    saved_items = dict(getattr(my_predbat.ha_interface, "dummy_items", {}))
    try:
        for key in _LEGACY_DEYE_KEYS:
            my_predbat.args.pop(key, None)
            if had_raw and saved_raw is not None and key in saved_raw:
                del saved_raw[key]
        if not had_raw:
            my_predbat.args_from_apps_yaml = dict(my_predbat.args)
            had_raw = True
            saved_raw = dict(my_predbat.args_from_apps_yaml)
        my_predbat.args["inverter_type"] = ["DEYE"]
        my_predbat.args["num_inverters"] = 1
        my_predbat.args["inverter"] = None
        rows = _program_rows("n5")[:5]
        my_predbat.args["deye_local"] = {
            "programs": [dict(row) for row in rows],
            "time_of_use": "select.dt_n5_tou",
            "work_mode": "select.dt_n5_wm",
            "energy_pattern": "select.dt_n5_ep",
            "max_discharge_power_w": 5000,
            "control": False,
            "hold_verified": False,
        }
        saved_raw["deye_local"] = my_predbat.args["deye_local"]
        my_predbat.args_from_apps_yaml = saved_raw
        _seed_entities(my_predbat, rows)

        try:
            Inverter(my_predbat, 0, quiet=True)
            print("ERROR: 5-row programs should be rejected")
            failed = True
        except ValueError as exc:
            if "six physical" not in str(exc):
                print("ERROR: wrong rejection reason: {}".format(exc))
                failed = True
    finally:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)
        if had_raw:
            my_predbat.args_from_apps_yaml = saved_raw
        items = getattr(my_predbat.ha_interface, "dummy_items", None)
        if items is not None:
            items.clear()
            items.update(saved_items)
    assert not failed, "test_prepare_rejects_wrong_number_of_programs"


def test_prepare_rejects_duplicate_entities(my_predbat):
    """prepare() rejects an entity id appearing in more than one slot."""
    failed = False
    saved_args = dict(my_predbat.args)
    had_raw = hasattr(my_predbat, "args_from_apps_yaml")
    saved_raw = copy.deepcopy(getattr(my_predbat, "args_from_apps_yaml", None)) if had_raw else None
    saved_items = dict(getattr(my_predbat.ha_interface, "dummy_items", {}))
    try:
        for key in _LEGACY_DEYE_KEYS:
            my_predbat.args.pop(key, None)
            if had_raw and saved_raw is not None and key in saved_raw:
                del saved_raw[key]
        if not had_raw:
            my_predbat.args_from_apps_yaml = dict(my_predbat.args)
            had_raw = True
            saved_raw = dict(my_predbat.args_from_apps_yaml)
        my_predbat.args["inverter_type"] = ["DEYE"]
        my_predbat.args["num_inverters"] = 1
        my_predbat.args["inverter"] = None
        rows = _program_rows("dup")
        # Row 5's charging entity equals row 0's charging - same domain, same entity.
        rows[5]["charging"] = rows[0]["charging"]
        my_predbat.args["deye_local"] = {
            "programs": [dict(row) for row in rows],
            "time_of_use": "select.dt_dup_tou",
            "work_mode": "select.dt_dup_wm",
            "energy_pattern": "select.dt_dup_ep",
            "max_discharge_power_w": 5000,
            "control": False,
            "hold_verified": False,
        }
        saved_raw["deye_local"] = my_predbat.args["deye_local"]
        my_predbat.args_from_apps_yaml = saved_raw
        _seed_entities(my_predbat, rows)

        try:
            Inverter(my_predbat, 0, quiet=True)
            print("ERROR: duplicate entities should be rejected")
            failed = True
        except ValueError as exc:
            if "distinct" not in str(exc):
                print("ERROR: wrong rejection reason: {}".format(exc))
                failed = True
    finally:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)
        if had_raw:
            my_predbat.args_from_apps_yaml = saved_raw
        items = getattr(my_predbat.ha_interface, "dummy_items", None)
        if items is not None:
            items.clear()
            items.update(saved_items)
    assert not failed, "test_prepare_rejects_duplicate_entities"


def test_prepare_rejects_non_integer_or_out_of_range_discharge_cap(my_predbat):
    """The discharge-power cap must be a positive whole wattage inside [1, 100000]."""
    failed = False
    saved_args = dict(my_predbat.args)
    had_raw = hasattr(my_predbat, "args_from_apps_yaml")
    saved_raw = copy.deepcopy(getattr(my_predbat, "args_from_apps_yaml", None)) if had_raw else None
    saved_items = dict(getattr(my_predbat.ha_interface, "dummy_items", {}))
    cases = [
        ("zero", 0),
        ("negative", -100),
        ("non_finite_string", "abc"),
        ("float", 1500.5),
        ("boolean", True),
        ("over_max", 100001),
        ("none", None),
    ]
    for name, value in cases:
        try:
            for key in _LEGACY_DEYE_KEYS:
                my_predbat.args.pop(key, None)
                if had_raw and saved_raw is not None and key in saved_raw:
                    del saved_raw[key]
            if not had_raw:
                my_predbat.args_from_apps_yaml = dict(my_predbat.args)
                had_raw = True
                saved_raw = dict(my_predbat.args_from_apps_yaml)
            my_predbat.args["inverter_type"] = ["DEYE"]
            my_predbat.args["num_inverters"] = 1
            my_predbat.args["inverter"] = None
            rows = _program_rows("cap_{}".format(name))
            my_predbat.args["deye_local"] = {
                "programs": [dict(row) for row in rows],
                "time_of_use": "select.dt_cap_{}_tou".format(name),
                "work_mode": "select.dt_cap_{}_wm".format(name),
                "energy_pattern": "select.dt_cap_{}_ep".format(name),
                "max_discharge_power_w": value,
                "control": False,
                "hold_verified": False,
            }
            saved_raw["deye_local"] = my_predbat.args["deye_local"]
            my_predbat.args_from_apps_yaml = saved_raw
            _seed_entities(my_predbat, rows, max_power_w=5000)
            try:
                Inverter(my_predbat, 0, quiet=True)
                print("ERROR: discharge cap {} should be rejected".format(name))
                failed = True
            except ValueError as exc:
                if "Invalid" not in str(exc):
                    print("ERROR: bad cap {} rejected for the wrong reason: {}".format(name, exc))
                    failed = True
        finally:
            my_predbat.args.clear()
            my_predbat.args.update(saved_args)
            if had_raw:
                my_predbat.args_from_apps_yaml = saved_raw
            items = getattr(my_predbat.ha_interface, "dummy_items", None)
            if items is not None:
                items.clear()
                items.update(saved_items)
    assert not failed, "test_prepare_rejects_non_integer_or_out_of_range_discharge_cap"


def test_prepare_rejects_malformed_program_rows(my_predbat):
    """Each row must carry exactly the four keys: time, soc, power, charging."""
    failed = False
    saved_args = dict(my_predbat.args)
    had_raw = hasattr(my_predbat, "args_from_apps_yaml")
    saved_raw = copy.deepcopy(getattr(my_predbat, "args_from_apps_yaml", None)) if had_raw else None
    saved_items = dict(getattr(my_predbat.ha_interface, "dummy_items", {}))
    cases = [
        ("missing_field", {"time": "time.t", "soc": "number.s", "power": "number.p"}),
        ("extra_field", {"time": "time.t", "soc": "number.s", "power": "number.p", "charging": "select.c", "extra": "x"}),
        ("not_a_dict", ["time.t", "number.s", "number.p", "select.c"]),
        ("domain_wrong_time", {"time": "number.t", "soc": "number.s", "power": "number.p", "charging": "select.c"}),
        ("domain_wrong_soc", {"time": "time.t", "soc": "select.s", "power": "number.p", "charging": "select.c"}),
        ("domain_wrong_power", {"time": "time.t", "soc": "number.s", "power": "select.p", "charging": "select.c"}),
        ("domain_wrong_charging", {"time": "time.t", "soc": "number.s", "power": "number.p", "charging": "number.c"}),
    ]
    for name, programs in cases:
        try:
            for key in _LEGACY_DEYE_KEYS:
                my_predbat.args.pop(key, None)
                if had_raw and saved_raw is not None and key in saved_raw:
                    del saved_raw[key]
            if not had_raw:
                my_predbat.args_from_apps_yaml = dict(my_predbat.args)
                had_raw = True
                saved_raw = dict(my_predbat.args_from_apps_yaml)
            my_predbat.args["inverter_type"] = ["DEYE"]
            my_predbat.args["num_inverters"] = 1
            my_predbat.args["inverter"] = None
            rows = [programs] * 6
            my_predbat.args["deye_local"] = {
                "programs": rows,
                "time_of_use": "select.dt_mal_tou",
                "work_mode": "select.dt_mal_wm",
                "energy_pattern": "select.dt_mal_ep",
                "max_discharge_power_w": 5000,
                "control": False,
                "hold_verified": False,
            }
            saved_raw["deye_local"] = my_predbat.args["deye_local"]
            my_predbat.args_from_apps_yaml = saved_raw
            try:
                Inverter(my_predbat, 0, quiet=True)
                print("ERROR: {} program row should be rejected".format(name))
                failed = True
            except ValueError as exc:
                msg = str(exc)
                if "Invalid" not in msg and "needs" not in msg and "concrete" not in msg:
                    print("ERROR: malformed row {} rejected for the wrong reason: {}".format(name, exc))
                    failed = True
        finally:
            my_predbat.args.clear()
            my_predbat.args.update(saved_args)
            if had_raw:
                my_predbat.args_from_apps_yaml = saved_raw
            items = getattr(my_predbat.ha_interface, "dummy_items", None)
            if items is not None:
                items.clear()
                items.update(saved_items)
    assert not failed, "test_prepare_rejects_malformed_program_rows"


def test_prepare_rejects_settings_with_wrong_keys(my_predbat):
    """The deye_local config dict must carry exactly the five core keys."""
    failed = False
    saved_args = dict(my_predbat.args)
    had_raw = hasattr(my_predbat, "args_from_apps_yaml")
    saved_raw = copy.deepcopy(getattr(my_predbat, "args_from_apps_yaml", None)) if had_raw else None
    saved_items = dict(getattr(my_predbat.ha_interface, "dummy_items", {}))
    cases = [
        ("missing_cap", {"programs": [], "time_of_use": "select.t", "work_mode": "select.w", "energy_pattern": "select.e"}),
        ("not_a_dict", ["bad"]),
        ("absent_entirely", None),
        ("unknown_extra_key", {"programs": [], "time_of_use": "select.t", "work_mode": "select.w", "energy_pattern": "select.e", "max_discharge_power_w": 5000, "control": False, "hold_verified": False, "mystery": "x"}),
    ]
    for name, settings in cases:
        try:
            for key in _LEGACY_DEYE_KEYS:
                my_predbat.args.pop(key, None)
                if had_raw and saved_raw is not None and key in saved_raw:
                    del saved_raw[key]
            if not had_raw:
                my_predbat.args_from_apps_yaml = dict(my_predbat.args)
                had_raw = True
                saved_raw = dict(my_predbat.args_from_apps_yaml)
            my_predbat.args["inverter_type"] = ["DEYE"]
            my_predbat.args["num_inverters"] = 1
            my_predbat.args["inverter"] = None
            my_predbat.args["deye_local"] = settings
            saved_raw["deye_local"] = settings
            my_predbat.args_from_apps_yaml = saved_raw
            try:
                Inverter(my_predbat, 0, quiet=True)
                print("ERROR: {} settings should be rejected".format(name))
                failed = True
            except ValueError as exc:
                if "Invalid" not in str(exc) and "explicit" not in str(exc) and "needs" not in str(exc):
                    print("ERROR: {} rejected for the wrong reason: {}".format(name, exc))
                    failed = True
        finally:
            my_predbat.args.clear()
            my_predbat.args.update(saved_args)
            if had_raw:
                my_predbat.args_from_apps_yaml = saved_raw
            items = getattr(my_predbat.ha_interface, "dummy_items", None)
            if items is not None:
                items.clear()
                items.update(saved_items)
    assert not failed, "test_prepare_rejects_settings_with_wrong_keys"


def test_prepare_rejects_non_boolean_control_or_hold_verified(my_predbat):
    """control and hold_verified must be booleans, never strings or None."""
    failed = False
    saved_args = dict(my_predbat.args)
    had_raw = hasattr(my_predbat, "args_from_apps_yaml")
    saved_raw = copy.deepcopy(getattr(my_predbat, "args_from_apps_yaml", None)) if had_raw else None
    saved_items = dict(getattr(my_predbat.ha_interface, "dummy_items", {}))
    cases = [
        ("control_string", {"control": "yes"}),
        ("control_int", {"control": 1}),
        ("hold_verified_string", {"hold_verified": "verified"}),
        ("hold_verified_int", {"hold_verified": 1}),
    ]
    for name, extra in cases:
        try:
            for key in _LEGACY_DEYE_KEYS:
                my_predbat.args.pop(key, None)
                if had_raw and saved_raw is not None and key in saved_raw:
                    del saved_raw[key]
            if not had_raw:
                my_predbat.args_from_apps_yaml = dict(my_predbat.args)
                had_raw = True
                saved_raw = dict(my_predbat.args_from_apps_yaml)
            my_predbat.args["inverter_type"] = ["DEYE"]
            my_predbat.args["num_inverters"] = 1
            my_predbat.args["inverter"] = None
            rows = _program_rows("bool_{}".format(name))
            settings = {
                "programs": [dict(row) for row in rows],
                "time_of_use": "select.dt_bool_{}_tou".format(name),
                "work_mode": "select.dt_bool_{}_wm".format(name),
                "energy_pattern": "select.dt_bool_{}_ep".format(name),
                "max_discharge_power_w": 5000,
                "control": False,
                "hold_verified": False,
            }
            settings.update(extra)
            my_predbat.args["deye_local"] = settings
            saved_raw["deye_local"] = settings
            my_predbat.args_from_apps_yaml = saved_raw
            _seed_entities(my_predbat, rows)
            try:
                Inverter(my_predbat, 0, quiet=True)
                print("ERROR: {} should be rejected as non-boolean".format(name))
                failed = True
            except ValueError as exc:
                if "bool" not in str(exc):
                    print("ERROR: {} rejected for the wrong reason: {}".format(name, exc))
                    failed = True
        finally:
            my_predbat.args.clear()
            my_predbat.args.update(saved_args)
            if had_raw:
                my_predbat.args_from_apps_yaml = saved_raw
            items = getattr(my_predbat.ha_interface, "dummy_items", None)
            if items is not None:
                items.clear()
                items.update(saved_items)
    assert not failed, "test_prepare_rejects_non_boolean_control_or_hold_verified"


def test_prepare_accepts_control_true_only_with_telemetry_time(my_predbat):
    """control=True requires a concrete sensor.telemetry_time entity id."""
    failed = False
    saved_args = dict(my_predbat.args)
    had_raw = hasattr(my_predbat, "args_from_apps_yaml")
    saved_raw = copy.deepcopy(getattr(my_predbat, "args_from_apps_yaml", None)) if had_raw else None
    saved_items = dict(getattr(my_predbat.ha_interface, "dummy_items", {}))
    try:
        for key in _LEGACY_DEYE_KEYS:
            my_predbat.args.pop(key, None)
            if had_raw and saved_raw is not None and key in saved_raw:
                del saved_raw[key]
        if not had_raw:
            my_predbat.args_from_apps_yaml = dict(my_predbat.args)
            had_raw = True
            saved_raw = dict(my_predbat.args_from_apps_yaml)
        my_predbat.args["inverter_type"] = ["DEYE"]
        my_predbat.args["num_inverters"] = 1
        my_predbat.args["inverter"] = None
        rows = _program_rows("telemetry")
        my_predbat.args["deye_local"] = {
            "programs": [dict(row) for row in rows],
            "time_of_use": "select.dt_telemetry_tou",
            "work_mode": "select.dt_telemetry_wm",
            "energy_pattern": "select.dt_telemetry_ep",
            "max_discharge_power_w": 5000,
            "control": True,
            "hold_verified": False,
        }
        saved_raw["deye_local"] = my_predbat.args["deye_local"]
        my_predbat.args_from_apps_yaml = saved_raw
        _seed_entities(my_predbat, rows)
        # The default settings dict omits telemetry_time entirely; the
        # explicit-key validator must surface it.
        my_predbat.args["deye_local"].pop("telemetry_time", None)
        saved_raw["deye_local"].pop("telemetry_time", None)
        my_predbat.args_from_apps_yaml = saved_raw
        try:
            Inverter(my_predbat, 0, quiet=True)
            print("ERROR: control=True without telemetry_time should be rejected")
            failed = True
        except ValueError as exc:
            if "concrete" not in str(exc):
                print("ERROR: missing telemetry_time rejected for the wrong reason: {}".format(exc))
                failed = True
    finally:
        my_predbat.args.clear()
        my_predbat.args.update(saved_args)
        if had_raw:
            my_predbat.args_from_apps_yaml = saved_raw
        items = getattr(my_predbat.ha_interface, "dummy_items", None)
        if items is not None:
            items.clear()
            items.update(saved_items)
    assert not failed, "test_prepare_accepts_control_true_only_with_telemetry_time"


def test_validate_limits_caps_discharge_and_zeros_export(my_predbat):
    """validate_limits() narrows the per-inverter discharge ceiling to deye_local's cap and zeroes export."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000)
    try:
        inverter.battery_rate_max_discharge = (10000) / 60.0 / 1000.0
        inverter.battery_rate_max_export = (8000) / 60.0 / 1000.0
        inverter.deye_local.validate_limits()
        if inverter.battery_rate_max_discharge > 5000 / 60.0 / 1000.0 + 1e-9:
            print("ERROR: validate_limits did not cap discharge to deye_local's max_power_w: {}".format(inverter.battery_rate_max_discharge))
            failed = True
        if inverter.battery_rate_max_export != 0:
            print("ERROR: validate_limits did not zero export, got {}".format(inverter.battery_rate_max_export))
            failed = True
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_validate_limits_caps_discharge_and_zeros_export"


def test_apply_returns_disabled_when_control_flag_false(my_predbat):
    """apply() refuses to issue service calls when control=False.

    The runtime gates physical writes behind the ``control`` opt-in (the
    experimental flag the user must set in apps.yaml after commissioning).
    """
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=20, source="Disabled", power=0, prefix="dis", control=False)
    try:
        inverter.reserve_percent = 10
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []

        ok = inverter.adjust_charge_immediate(80, freeze=False)
        if ok is not False:
            print("ERROR: control=False should return False, got {}".format(ok))
            failed = True
        if my_predbat.ha_interface.service_store:
            print("ERROR: control=False should not write anything, got {}".format(my_predbat.ha_interface.service_store))
            failed = True
        control_entity = "{}.deye_local_control".format(my_predbat.prefix)
        observed = my_predbat.ha_interface.dummy_items.get(control_entity)
        if not observed or observed.get("state") != "disabled":
            print("ERROR: status not 'disabled': {}".format(observed))
            failed = True
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_returns_disabled_when_control_flag_false"


def test_apply_charge_writes_grid_and_raises_soc(my_predbat):
    """adjust_charge_immediate(target_soc>0) writes Grid source and target SOC >= the requested value."""
    failed = False
    # Start from a clean Disabled source. Phase 1 (write Disabled first) is a
    # no-op so the only observable writes are the SOC, power and Grid.
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=20, source="Disabled", power=0, prefix="charge", control=True)
    try:
        inverter.reserve_percent = 10
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        ok = inverter.adjust_charge_immediate(80, freeze=False)
        if not ok:
            print("ERROR: charge apply should have succeeded, services={}".format(my_predbat.ha_interface.service_store))
            failed = True

        number_writes = [(s, k["entity_id"], k["value"]) for s, k in my_predbat.ha_interface.service_store if s == "number/set_value"]
        soc_entities = {r["soc"] for r in rows}
        power_entities = {r["power"] for r in rows}
        soc_values = [v for e, v in [(e, v) for s, e, v in number_writes if e in soc_entities]]
        power_values = [v for e, v in [(e, v) for s, e, v in number_writes if e in power_entities]]
        if not soc_values or any(v < 80 for v in soc_values):
            print("ERROR: SOC writes below target (need >=80): {}".format(soc_values))
            failed = True
        if not power_values or any(v != 5000 for v in power_values):
            print("ERROR: power writes not all at cap 5000 W: {}".format(power_values))
            failed = True

        grid_calls = [(s, k) for s, k in my_predbat.ha_interface.service_store if s == "select/select_option" and k.get("option") == "Grid"]
        grid_entities = {k["entity_id"] for s, k in grid_calls}
        charging_entities = {r["charging"] for r in rows}
        if grid_entities != charging_entities:
            print("ERROR: Grid not written on every charging entity, missing: {}".format(charging_entities - grid_entities))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_charge_writes_grid_and_raises_soc"


def test_apply_charge_uses_reserve_as_floor(my_predbat):
    """The SOC write never goes below the inverter's reserve_percent."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=60, source="Disabled", power=0, prefix="floor", control=True)
    try:
        inverter.reserve_percent = 50
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        inverter.adjust_charge_immediate(10, freeze=False)
        number_writes = [(e, v) for s, k in my_predbat.ha_interface.service_store if s == "number/set_value" for e, v in [(k["entity_id"], k["value"])]]
        soc_values = [v for e, v in number_writes if e in {r["soc"] for r in rows}]
        if not soc_values or any(v < 50 for v in soc_values):
            print("ERROR: SOC dropped below reserve (need >=50): {}".format(soc_values))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_charge_uses_reserve_as_floor"


def test_apply_demand_writes_disabled_and_low_soc(my_predbat):
    """adjust_charge_immediate(0) is a Demand intent: source Disabled, SOC at the reserve floor."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=40, source="Disabled", power=0, prefix="demand", control=True)
    try:
        inverter.reserve_percent = 10
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        inverter.adjust_charge_immediate(0, freeze=False)

        grid_calls = [(s, k) for s, k in my_predbat.ha_interface.service_store if s == "select/select_option" and k.get("option") == "Grid"]
        if grid_calls:
            print("ERROR: demand should not arm Grid: {}".format(grid_calls))
            failed = True

        number_writes = [(e, v) for s, k in my_predbat.ha_interface.service_store if s == "number/set_value" for e, v in [(k["entity_id"], k["value"])]]
        soc_values = [v for e, v in number_writes if e in {r["soc"] for r in rows}]
        if not soc_values or any(v < 10 for v in soc_values):
            print("ERROR: SOC below reserve floor on demand: {}".format(soc_values))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_demand_writes_disabled_and_low_soc"


def test_apply_hold_requires_hold_verified(my_predbat):
    """A freeze (freeze=True) requires hold_verified=True; otherwise apply() raises."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(
        my_predbat,
        max_power_w=5000,
        soc=80,
        source="Disabled",
        power=0,
        prefix="holdgate",
        control=True,
        hold_verified=False,
    )
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []

        ok = inverter.adjust_charge_immediate(50, freeze=True)
        if ok is not False:
            print("ERROR: hold without hold_verified should fail, got {}".format(ok))
            failed = True
        if my_predbat.ha_interface.service_store:
            print("ERROR: hold without hold_verified should write nothing, got {}".format(my_predbat.ha_interface.service_store))
            failed = True
        control_entity = "{}.deye_local_control".format(my_predbat.prefix)
        observed = my_predbat.ha_interface.dummy_items.get(control_entity)
        if not observed or observed.get("state") != "failed":
            print("ERROR: status not 'failed' for hold without hold_verified: {}".format(observed))
            failed = True
        if not (observed and "commissioning" in str(observed.get("reason", ""))):
            print("ERROR: hold failure reason should mention commissioning: {}".format(observed))
            failed = True
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_hold_requires_hold_verified"


def test_apply_hold_freeze_writes_zero_power_and_disabled_source(my_predbat):
    """freeze=True (hold) lands SOC at 100, power at 0, source at Disabled on every row."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(
        my_predbat,
        max_power_w=5000,
        soc=80,
        source="Disabled",
        power=0,
        prefix="hold",
        control=True,
        hold_verified=True,
    )
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        inverter.adjust_charge_immediate(50, freeze=True)

        number_writes = [(e, v) for s, k in my_predbat.ha_interface.service_store if s == "number/set_value" for e, v in [(k["entity_id"], k["value"])]]
        soc_values = [v for e, v in number_writes if e in {r["soc"] for r in rows}]
        # Power was already 0; nothing should change. We assert it stays 0.
        for r in rows:
            if my_predbat.ha_interface.dummy_items[r["power"]]["state"] != 0:
                print("ERROR: hold power must remain 0, got {}".format(my_predbat.ha_interface.dummy_items[r["power"]]["state"]))
                failed = True
        if not soc_values or any(v != 100 for v in soc_values):
            print("ERROR: hold SOC must be exactly 100, got {}".format(soc_values))
            failed = True
        grid_calls = [(s, k) for s, k in my_predbat.ha_interface.service_store if s == "select/select_option" and k.get("option") == "Grid"]
        if grid_calls:
            print("ERROR: hold must never arm Grid, got {}".format(grid_calls))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_hold_freeze_writes_soc_to_100_and_never_grid"


def test_apply_returns_false_on_read_only(my_predbat):
    """set_read_only short-circuits apply() to "read_only" with no writes."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=20, source="Disabled", power=0, prefix="ro", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = True
        my_predbat.set_read_only_axle = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []

        ok = inverter.adjust_charge_immediate(80, freeze=False)
        if ok is not False:
            print("ERROR: read_only should return False, got {}".format(ok))
            failed = True
        if my_predbat.ha_interface.service_store:
            print("ERROR: read_only should not write anything, got {}".format(my_predbat.ha_interface.service_store))
            failed = True
        control_entity = "{}.deye_local_control".format(my_predbat.prefix)
        observed = my_predbat.ha_interface.dummy_items.get(control_entity)
        if not observed or observed.get("state") != "read_only":
            print("ERROR: status not 'read_only': {}".format(observed))
            failed = True
    finally:
        my_predbat.set_read_only = False
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_returns_false_on_read_only"


def test_export_immediate_is_no_op_on_deye_local(my_predbat):
    """adjust_export_immediate is a no-op for deye_local except to surface an error."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=50, source="Disabled", power=0, prefix="export", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []

        ret = inverter.adjust_export_immediate(20, freeze=False)
        if ret is not None:
            print("ERROR: adjust_export_immediate should be a no-op (return None), got {}".format(ret))
            failed = True
        if my_predbat.ha_interface.service_store:
            print("ERROR: adjust_export_immediate wrote services: {}".format(my_predbat.ha_interface.service_store))
            failed = True

        ret = inverter.adjust_export_immediate(80, freeze=False)
        if ret is not None:
            print("ERROR: forced export should still return None, got {}".format(ret))
            failed = True
        ret = inverter.adjust_export_immediate(50, freeze=True)
        if ret is not None:
            print("ERROR: export freeze should still return None, got {}".format(ret))
            failed = True
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_export_immediate_is_no_op_on_deye_local"


def test_apply_uses_only_configured_tou_fields(my_predbat):
    """Only the configured ToU row fields and the three selects ever get written."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=20, source="Disabled", power=0, prefix="scope", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        inverter.adjust_charge_immediate(80, freeze=False)

        allowed = _scrub_base(inverter.deye_local)
        seen = {k["entity_id"] for s, k in my_predbat.ha_interface.service_store if "entity_id" in k}
        # Time entities must never appear as writes.
        time_writes = [e for e in seen if e in {r["time"] for r in rows}]
        if time_writes:
            print("ERROR: time entities written: {}".format(time_writes))
            failed = True
        outside = seen - allowed
        if outside:
            print("ERROR: writes outside the configured ToU scope: {}".format(outside))
            failed = True
        for r in rows:
            if r["soc"] not in seen:
                print("ERROR: row SOC entity {} not written".format(r["soc"]))
                failed = True
            if r["power"] not in seen:
                print("ERROR: row power entity {} not written".format(r["power"]))
                failed = True
            if r["charging"] not in seen:
                print("ERROR: row charging entity {} not written".format(r["charging"]))
                failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_uses_only_configured_tou_fields"


def test_apply_only_writes_what_changed_dedup(my_predbat):
    """A second apply with no state change performs zero writes (deduplicated)."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=20, source="Disabled", power=0, prefix="dedup", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        inverter.adjust_charge_immediate(80, freeze=False)
        first_count = len(my_predbat.ha_interface.service_store)
        if not first_count:
            print("ERROR: first apply wrote nothing")
            failed = True
        # Mirror the desired state into the HA cache so the snapshot sees no drift.
        for r in rows:
            my_predbat.ha_interface.dummy_items[r["soc"]]["state"] = 80
            my_predbat.ha_interface.dummy_items[r["power"]]["state"] = 5000
            my_predbat.ha_interface.dummy_items[r["charging"]]["state"] = "Grid"

        my_predbat.ha_interface.service_store = []
        ok = inverter.adjust_charge_immediate(80, freeze=False)
        if not ok:
            print("ERROR: redundant apply should still confirm, got False")
            failed = True
        if my_predbat.ha_interface.service_store:
            print("ERROR: redundant apply wrote: {}".format(my_predbat.ha_interface.service_store))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_only_writes_what_changed_dedup"


def test_apply_all_six_transitions_each_get_one_write(my_predbat):
    """All six rows transition together; the snapshot reports them all."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=15, source="Grid", power=2000, prefix="all6", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        inverter.adjust_charge_immediate(90, freeze=False)

        soc_writes = [k["entity_id"] for s, k in my_predbat.ha_interface.service_store if s == "number/set_value" and k["entity_id"] in {r["soc"] for r in rows}]
        if sorted(soc_writes) != sorted(r["soc"] for r in rows):
            print("ERROR: not all six SOC entities written: {}".format(soc_writes))
            failed = True

        power_writes = [k["entity_id"] for s, k in my_predbat.ha_interface.service_store if s == "number/set_value" and k["entity_id"] in {r["power"] for r in rows}]
        if sorted(power_writes) != sorted(r["power"] for r in rows):
            print("ERROR: not all six power entities written: {}".format(power_writes))
            failed = True

        grid_writes = [k["entity_id"] for s, k in my_predbat.ha_interface.service_store if s == "select/select_option" and k.get("option") == "Grid" and k["entity_id"] in {r["charging"] for r in rows}]
        if sorted(grid_writes) != sorted(r["charging"] for r in rows):
            print("ERROR: not all six charging entities set to Grid: {}".format(grid_writes))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_all_six_transitions_each_get_one_write"


def test_apply_mismatch_repaired_observed_before_next_phase(my_predbat):
    """If the snapshot reports a mismatch vs. desired, the adapter writes before advancing."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=15, source="Grid", power=2000, prefix="repair", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        inverter.adjust_charge_immediate(90, freeze=False)

        service_log = my_predbat.ha_interface.service_store
        last_disabled = max(
            (i for i, (s, k) in enumerate(service_log) if s == "select/select_option" and k.get("option") == "Disabled"),
            default=-1,
        )
        first_numeric = next(
            (i for i, (s, k) in enumerate(service_log) if s == "number/set_value"),
            len(service_log),
        )
        if not (last_disabled < first_numeric):
            print("ERROR: SOC/power write fired before all sources were disabled ({}, {})".format(last_disabled, first_numeric))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_mismatch_repaired_observed_before_next_phase"


def test_apply_rejected_call_prevents_later_phase(my_predbat):
    """A service call returning False halts the rest of the phase and the cycle."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=15, source="Grid", power=2000, prefix="reject", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        fail_targets = {rows[2]["charging"]}
        my_predbat.ha_interface.service_store_fail = set()
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        def selective(service, **kwargs):
            """Reject a selected synthetic service before applying its state."""
            if service == "select/select_option" and kwargs.get("entity_id") in fail_targets and kwargs.get("option") == "Disabled":
                return False
            return original_call(service, **kwargs)

        my_predbat.call_service_wrapper = selective
        try:
            ok = inverter.adjust_charge_immediate(90, freeze=False)
            if ok:
                print("ERROR: a rejected call should make apply return False, got True")
                failed = True
            for s, k in my_predbat.ha_interface.service_store:
                if s == "number/set_value":
                    print("ERROR: numeric write fired after a rejected Disabled: {}".format((s, k)))
                    failed = True
                if s == "select/select_option" and k.get("option") == "Grid":
                    print("ERROR: Grid write fired after a rejected Disabled: {}".format((s, k)))
                    failed = True
        finally:
            my_predbat.call_service_wrapper = original_call
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_rejected_call_prevents_later_phase"


def test_apply_accepted_without_observation_waits_for_next_native_cycle(my_predbat):
    """A write that the HA cache never reflects (still Grid) returns 'pending' and waits."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=15, source="Grid", power=2000, prefix="pending", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []

        def revert_state(service, **kwargs):
            """Simulate an accepted write whose observation has not caught up."""
            if service == "select/select_option":
                entity = kwargs.get("entity_id")
                if entity and entity in my_predbat.ha_interface.dummy_items:
                    # Force the snapshot to keep reporting Grid regardless of
                    # what the underlying call_service did.
                    my_predbat.ha_interface.dummy_items[entity]["state"] = "Grid"
            return original(service, **kwargs)

        original = _install_attribute_preserving_service_wrapper(my_predbat)
        my_predbat.call_service_wrapper = revert_state
        try:
            ok = inverter.adjust_charge_immediate(90, freeze=False)
            if ok:
                print("ERROR: unobserved write should NOT confirm, got True")
                failed = True
            control_entity = "{}.deye_local_control".format(my_predbat.prefix)
            status = my_predbat.ha_interface.dummy_items.get(control_entity)
            if not status or status.get("state") != "pending":
                print("ERROR: status not 'pending' after unobserved writes: {}".format(status))
                failed = True
        finally:
            my_predbat.call_service_wrapper = original
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_accepted_without_observation_waits_for_next_native_cycle"


def test_apply_bounded_retry_after_three_attempts_raises(my_predbat):
    """A value stuck unobserved for 300 s is retried at most three times before failing closed."""
    failed = False
    clock, advance = _monotonic_factory()
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=15, source="Grid", power=2000, prefix="retry", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []

        # Patch time.monotonic() inside deye_local so the 300 s deadline
        # actually advances between retries. Each phase call bumps it by the
        # native settle time the real impl would have waited.
        import deye_local as deye_local_module

        original_monotonic = deye_local_module.time.monotonic

        def fake_monotonic():
            """Read the test's retry clock without waiting in real time."""
            return clock()

        deye_local_module.time.monotonic = fake_monotonic

        # Patch inverter.sleep so we do not actually wait inv_write_and_poll_sleep.
        original_sleep = inverter.sleep
        inverter.sleep = lambda seconds: advance(seconds)

        original_call = my_predbat.call_service_wrapper

        def sticky(service, **kwargs):
            """Accept a synthetic write while keeping its old observation."""
            if service == "select/select_option" and kwargs.get("option") == "Disabled":
                entity = kwargs.get("entity_id")
                if entity and entity in my_predbat.ha_interface.dummy_items:
                    my_predbat.ha_interface.dummy_items[entity]["state"] = "Grid"
            return original_call(service, **kwargs)

        my_predbat.call_service_wrapper = sticky

        try:
            # The first apply call: snapshot is fresh so attempts is 0; all six rows
            # attempt once and the snapshot still reports Grid, so the phase
            # returns False; apply() reports pending.
            ok = inverter.adjust_charge_immediate(90, freeze=False)
            if ok:
                print("ERROR: first attempt should NOT confirm")
                failed = True
            # Advance past the 300 s deadline.
            advance(305.0)
            # Each subsequent call should bump attempts by one.
            for cycle in range(2):
                inverter.adjust_charge_immediate(90, freeze=False)
                advance(305.0)
            # The next call should hit the attempts >= 3 cap and raise.
            inverter.adjust_charge_immediate(90, freeze=False)
            control_entity = "{}.deye_local_control".format(my_predbat.prefix)
            status = my_predbat.ha_interface.dummy_items.get(control_entity)
            if not status or status.get("state") != "failed":
                print("ERROR: status not 'failed' after bound exceeded: {}".format(status))
                failed = True
        finally:
            my_predbat.call_service_wrapper = original_call
            inverter.sleep = original_sleep
            deye_local_module.time.monotonic = original_monotonic
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_bounded_retry_after_three_attempts_raises"


def test_snapshot_rejects_invalid_state_observations(my_predbat):
    """snapshot() refuses to operate on unknown / unavailable / missing entity observations."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=50, source="Disabled", power=0, prefix="snap", control=True)
    try:
        my_predbat.ha_interface.dummy_items.pop(rows[0]["soc"])
        try:
            inverter.deye_local.snapshot()
            print("ERROR: missing entity snapshot should fail")
            failed = True
        except ValueError:
            pass

        my_predbat.ha_interface.dummy_items[rows[0]["soc"]] = {
            "state": "unknown",
            "attributes": {"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"},
        }
        try:
            inverter.deye_local.snapshot()
            print("ERROR: unknown state snapshot should fail")
            failed = True
        except ValueError:
            pass

        my_predbat.ha_interface.dummy_items[rows[0]["soc"]] = {
            "state": "unavailable",
            "attributes": {"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"},
        }
        try:
            inverter.deye_local.snapshot()
            print("ERROR: unavailable snapshot should fail")
            failed = True
        except ValueError:
            pass

        my_predbat.ha_interface.dummy_items[rows[0]["soc"]] = {
            "state": "inf",
            "attributes": {"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"},
        }
        try:
            inverter.deye_local.snapshot()
            print("ERROR: non-finite numeric state should fail")
            failed = True
        except ValueError:
            pass
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_snapshot_rejects_invalid_state_observations"


def test_snapshot_rejects_non_integer_or_out_of_range_numeric(my_predbat):
    """SOC/power observations must be integers inside the configured unit/range."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=50, source="Disabled", power=0, prefix="range", control=True)
    try:
        my_predbat.ha_interface.dummy_items[rows[0]["soc"]] = {
            "state": "200",
            "attributes": {"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"},
        }
        try:
            inverter.deye_local.snapshot()
            print("ERROR: SOC=200 should be rejected")
            failed = True
        except ValueError:
            pass

        my_predbat.ha_interface.dummy_items[rows[0]["soc"]] = {
            "state": "-1",
            "attributes": {"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"},
        }
        try:
            inverter.deye_local.snapshot()
            print("ERROR: SOC=-1 should be rejected")
            failed = True
        except ValueError:
            pass

        my_predbat.ha_interface.dummy_items[rows[0]["soc"]] = {
            "state": "50.5",
            "attributes": {"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"},
        }
        try:
            inverter.deye_local.snapshot()
            print("ERROR: SOC=50.5 should be rejected")
            failed = True
        except ValueError:
            pass

        my_predbat.ha_interface.dummy_items[rows[0]["soc"]] = {
            "state": "50",
            "attributes": {"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"},
        }
        # A power attribute range below max_discharge_power_w=5000 is rejected
        # by the snapshot's range validator even if the observed value itself
        # would fit in [0, 100000].
        my_predbat.ha_interface.dummy_items[rows[0]["power"]] = {
            "state": "0",
            "attributes": {"min": 0, "max": 4000, "step": 1, "unit_of_measurement": "W"},
        }
        try:
            inverter.deye_local.snapshot()
            print("ERROR: power attr-max below cap should be rejected")
            failed = True
        except ValueError:
            pass
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_snapshot_rejects_non_integer_or_out_of_range_numeric"


def test_snapshot_rejects_bad_time_formats(my_predbat):
    """Time observations must match HH:MM or HH:MM:SS with :00 seconds."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=50, source="Disabled", power=0, prefix="time", control=True)
    try:
        bad_times = ["24:00", "12:60", "1:00", "12", "noon", "12:00:30", ""]
        for bad in bad_times:
            my_predbat.ha_interface.dummy_items[rows[0]["time"]] = {"state": bad, "attributes": {}}
            try:
                inverter.deye_local.snapshot()
                print("ERROR: bad time {} should be rejected".format(bad))
                failed = True
            except ValueError:
                pass
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_snapshot_rejects_bad_time_formats"


def test_snapshot_rejects_unsorted_or_duplicate_times(my_predbat):
    """Six distinct ascending times are required; cycles must partition the day."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=50, source="Disabled", power=0, prefix="order", control=True)
    try:
        for index in range(6):
            my_predbat.ha_interface.dummy_items[rows[index]["time"]] = {
                "state": "12:00",
                "attributes": {},
            }
        try:
            inverter.deye_local.snapshot()
            print("ERROR: duplicate times should be rejected")
            failed = True
        except ValueError:
            pass

        new_order = ["08:00", "00:00", "12:00", "04:00", "20:00", "16:00"]
        for index, value in enumerate(new_order):
            my_predbat.ha_interface.dummy_items[rows[index]["time"]] = {
                "state": value,
                "attributes": {},
            }
        try:
            inverter.deye_local.snapshot()
            print("ERROR: out-of-order times should be rejected")
            failed = True
        except ValueError:
            pass
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_snapshot_rejects_unsorted_or_duplicate_times"


def test_snapshot_rejects_wrong_unit_or_step_or_range(my_predbat):
    """The number entities must declare %, W with step=1 and a range that covers the request."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=50, source="Disabled", power=0, prefix="unit", control=True)
    try:
        my_predbat.ha_interface.dummy_items[rows[0]["soc"]] = {
            "state": "50",
            "attributes": {"min": 0, "max": 100, "step": 1, "unit_of_measurement": "W"},
        }
        try:
            inverter.deye_local.snapshot()
            print("ERROR: wrong unit should be rejected")
            failed = True
        except ValueError:
            pass

        my_predbat.ha_interface.dummy_items[rows[0]["soc"]] = {
            "state": "50",
            "attributes": {"min": 0, "max": 100, "step": 2, "unit_of_measurement": "%"},
        }
        try:
            inverter.deye_local.snapshot()
            print("ERROR: step!=1 should be rejected")
            failed = True
        except ValueError:
            pass

        my_predbat.ha_interface.dummy_items[rows[0]["soc"]] = {
            "state": "50",
            "attributes": {"min": 5, "max": 100, "step": 1, "unit_of_measurement": "%"},
        }
        try:
            inverter.deye_local.snapshot()
            print("ERROR: min>0 should be rejected")
            failed = True
        except ValueError:
            pass

        my_predbat.ha_interface.dummy_items[rows[0]["soc"]] = {
            "state": "50",
            "attributes": {"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%"},
        }
        my_predbat.ha_interface.dummy_items[rows[0]["power"]] = {
            "state": "0",
            "attributes": {"min": 0, "max": 2000, "step": 1, "unit_of_measurement": "W"},
        }
        try:
            inverter.deye_local.snapshot()
            print("ERROR: power range below cap should be rejected")
            failed = True
        except ValueError:
            pass
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_snapshot_rejects_wrong_unit_or_step_or_range"


def test_snapshot_rejects_non_allowed_operating_modes(my_predbat):
    """time_of_use, work_mode, energy_pattern have strict enums the adapter allows."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=50, source="Disabled", power=0, prefix="modes", control=True)
    try:
        bad_states = [
            ("select.dt_modes_tou", "Tuesday"),
            ("select.dt_modes_wm", "Feed-in priority"),
            ("select.dt_modes_ep", "Solar First"),
        ]
        for ent, value in bad_states:
            current = my_predbat.ha_interface.dummy_items[ent]
            current["state"] = value
            try:
                inverter.deye_local.snapshot()
                print("ERROR: bad {}={} should be rejected".format(ent, value))
                failed = True
            except ValueError:
                current["state"] = {
                    "select.dt_modes_tou": "Week",
                    "select.dt_modes_wm": "Zero Export To CT",
                    "select.dt_modes_ep": "Load First",
                }[ent]
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_snapshot_rejects_non_allowed_operating_modes"


def test_snapshot_rejects_charging_source_options_missing_disabled_or_grid(my_predbat):
    """Each row's source entity must offer Disabled and Grid as options."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=50, source="Disabled", power=0, prefix="opts", control=True)
    try:
        item = my_predbat.ha_interface.dummy_items[rows[0]["charging"]]
        item["attributes"]["options"] = ["Disabled", "Generator"]
        try:
            inverter.deye_local.snapshot()
            print("ERROR: missing Grid option should be rejected")
            failed = True
        except ValueError:
            pass
        item["attributes"]["options"] = ["Disabled", "Grid", "Generator", "Both"]
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_snapshot_rejects_charging_source_options_missing_disabled_or_grid"


def test_snapshot_accepts_first_row_at_00_00_and_subsequent_at_00_00(my_predbat):
    """Midnight (00:00) is a valid boundary; a midnight-first day with one row at 00:00 is allowed."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=50, source="Disabled", power=0, prefix="midnight", control=True)
    try:
        snap = inverter.deye_local.snapshot()
        if not snap:
            print("ERROR: ascending midnight-anchored times should snapshot cleanly")
            failed = True
        my_predbat.ha_interface.dummy_items[rows[0]["time"]] = {"state": "00:30", "attributes": {}}
        snap = inverter.deye_local.snapshot()
        if not snap:
            print("ERROR: first-row at 00:30 should still snapshot")
            failed = True
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_snapshot_accepts_first_row_at_00_00_and_subsequent_at_00_00"


def test_snapshot_rejects_telemetry_clock_outside_window(my_predbat):
    """snapshot() refuses to operate if the telemetry_time clock is outside [-30, +180] seconds."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(
        my_predbat,
        max_power_w=5000,
        soc=50,
        source="Disabled",
        power=0,
        prefix="stale",
        control=True,
    )
    try:
        # 10 minutes in the past: outside the [-30, +180] window.
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(microsecond=0).isoformat()
        my_predbat.ha_interface.dummy_items["sensor.dt_stale_tt"]["state"] = stale
        try:
            inverter.deye_local.snapshot()
            print("ERROR: stale telemetry_time should be rejected")
            failed = True
        except ValueError as exc:
            if "telemetry" not in str(exc).lower():
                print("ERROR: stale telemetry rejected for the wrong reason: {}".format(exc))
                failed = True

        # 1 hour in the future: also outside.
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0).isoformat()
        my_predbat.ha_interface.dummy_items["sensor.dt_stale_tt"]["state"] = future
        try:
            inverter.deye_local.snapshot()
            print("ERROR: future telemetry_time should be rejected")
            failed = True
        except ValueError as exc:
            if "telemetry" not in str(exc).lower():
                print("ERROR: future telemetry rejected for the wrong reason: {}".format(exc))
                failed = True

        # A timestamp with no tzinfo must localise via the apps.yaml timezone.
        naive = datetime.now().replace(microsecond=0).isoformat()
        my_predbat.ha_interface.dummy_items["sensor.dt_stale_tt"]["state"] = naive
        try:
            inverter.deye_local.snapshot()
            # If the localiser is configured (it is, via my_predbat.args["timezone"]), this
            # should pass. If not configured it should fail with "invalid or ambiguous".
        except ValueError as exc:
            if "telemetry" not in str(exc).lower():
                print("ERROR: naive telemetry rejected for the wrong reason: {}".format(exc))
                failed = True
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_snapshot_rejects_telemetry_clock_outside_window"


def test_apply_respects_manual_native_charge_intent(my_predbat):
    """A manual charge target set just before apply() is the value apply() writes."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=20, source="Disabled", power=0, prefix="manual", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        inverter.adjust_charge_immediate(70, freeze=False)
        soc_values = sorted({k["value"] for s, k in my_predbat.ha_interface.service_store if s == "number/set_value" and k["entity_id"] in {r["soc"] for r in rows}})
        if soc_values != [70]:
            print("ERROR: SOC writes not all 70: {}".format(soc_values))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_respects_manual_native_charge_intent"


def test_apply_demand_uses_reserve_floor_and_cap_power(my_predbat):
    """Demand (target_soc=0) keeps the SOC at the reserve floor and the power at the cap (not zero)."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=60, source="Disabled", power=0, prefix="demand_floor", control=True)
    try:
        inverter.reserve_percent = 20
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        inverter.adjust_charge_immediate(0, freeze=False)
        soc_values = [k["value"] for s, k in my_predbat.ha_interface.service_store if s == "number/set_value" and k["entity_id"] in {r["soc"] for r in rows}]
        power_values = [k["value"] for s, k in my_predbat.ha_interface.service_store if s == "number/set_value" and k["entity_id"] in {r["power"] for r in rows}]
        if not soc_values or any(v < 20 for v in soc_values):
            print("ERROR: SOC below reserve floor on demand: {}".format(soc_values))
            failed = True
        if not power_values or any(v != 5000 for v in power_values):
            print("ERROR: power not at discharge cap on demand: {}".format(power_values))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_demand_uses_reserve_floor_and_cap_power"


def test_native_freeze_uses_power_zero_and_disabled_grid_and_keeps_pv_cap(my_predbat):
    """A native freeze (freeze=True) is power=0, source=Disabled on every row.

    The discharge cap is unchanged by the freeze: a future unfreeze must
    immediately resume exporting at the same ceiling, otherwise export plans
    would silently reset to a lower cap after every hold.
    """
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(
        my_predbat,
        max_power_w=5000,
        soc=80,
        source="Disabled",
        power=0,
        prefix="pv_cap",
        control=True,
        hold_verified=True,
    )
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        cap_before = inverter.battery_rate_max_discharge
        inverter.adjust_charge_immediate(40, freeze=True)
        cap_after = inverter.battery_rate_max_discharge
        if cap_before != cap_after:
            print("ERROR: discharge cap changed by freeze (before={}, after={})".format(cap_before, cap_after))
            failed = True

        power_values = [k["value"] for s, k in my_predbat.ha_interface.service_store if s == "number/set_value" and k["entity_id"] in {r["power"] for r in rows}]
        if any(v != 0 for v in power_values):
            print("ERROR: freeze must write power=0, got {}".format(power_values))
            failed = True

        grid_writes = [(s, k) for s, k in my_predbat.ha_interface.service_store if s == "select/select_option" and k.get("option") == "Grid"]
        if grid_writes:
            print("ERROR: freeze must not arm Grid: {}".format(grid_writes))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_native_freeze_uses_power_zero_and_disabled_grid_and_keeps_pv_cap"


def test_reset_safe_no_force_export_fallback(my_predbat):
    """There is no export-side fallback for the just-applied intent."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=50, source="Disabled", power=0, prefix="reset", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        inverter.adjust_charge_immediate(80, freeze=False)
        grid_first = sum(1 for s, k in my_predbat.ha_interface.service_store if s == "select/select_option" and k.get("option") == "Grid")
        if grid_first != 6:
            print("ERROR: charge apply should arm Grid 6 times, got {}".format(grid_first))
            failed = True

        for r in rows:
            my_predbat.ha_interface.dummy_items[r["soc"]]["state"] = 80
            my_predbat.ha_interface.dummy_items[r["power"]]["state"] = 5000
            my_predbat.ha_interface.dummy_items[r["charging"]]["state"] = "Grid"
        my_predbat.ha_interface.service_store = []

        inverter.adjust_export_immediate(20, freeze=False)
        if my_predbat.ha_interface.service_store:
            print("ERROR: export immediate wrote services: {}".format(my_predbat.ha_interface.service_store))
            failed = True
        inverter.adjust_charge_immediate(80, freeze=False)
        if my_predbat.ha_interface.service_store:
            print("ERROR: redundant charge wrote services: {}".format(my_predbat.ha_interface.service_store))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_reset_safe_no_force_export_fallback"


def test_per_inverter_discharge_cap_caps_battery_rate(my_predbat):
    """The discharge cap set in deye_local caps the inverter's battery_rate_max_discharge."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=20, source="Disabled", power=0, prefix="cap", control=True)
    try:
        inverter.battery_rate_max_discharge = 8000 / 60.0 / 1000.0
        inverter.deye_local.validate_limits()
        if inverter.battery_rate_max_discharge > 5000 / 60.0 / 1000.0 + 1e-9:
            print("ERROR: validate_limits did not narrow discharge cap to 5 kW: {}".format(inverter.battery_rate_max_discharge))
            failed = True

        inverter.battery_rate_max_discharge = 1000 / 60.0 / 1000.0
        inverter.deye_local.validate_limits()
        if abs(inverter.battery_rate_max_discharge - 1000 / 60.0 / 1000.0) > 1e-9:
            print("ERROR: validate_limits changed a below-cap ceiling: {}".format(inverter.battery_rate_max_discharge))
            failed = True
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_per_inverter_discharge_cap_caps_battery_rate"


def test_settings_change_clears_pending(my_predbat):
    """Changing the settings dict (e.g. swapping programs) clears the pending retry state."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=20, source="Disabled", power=0, prefix="clear", control=True)
    try:
        inverter.deye_local.pending["number.dt_clear_0"] = {"value": 10, "at": 0.0, "attempts": 1}
        inverter.deye_local.prepare()
        if not inverter.deye_local.pending:
            print("ERROR: prepare() with identical settings cleared pending unexpectedly")
            failed = True
        new_rows = _program_rows("clear2")
        my_predbat.args["deye_local"] = {
            "programs": [dict(row) for row in new_rows],
            "time_of_use": "select.dt_clear2_tou",
            "work_mode": "select.dt_clear2_wm",
            "energy_pattern": "select.dt_clear2_ep",
            "max_discharge_power_w": 5000,
            "control": True,
            "hold_verified": False,
            "telemetry_time": "sensor.dt_clear2_tt",
        }
        my_predbat.args_from_apps_yaml = dict(my_predbat.args)
        _seed_entities(my_predbat, new_rows)
        inverter.deye_local.prepare()
        if inverter.deye_local.pending:
            print("ERROR: prepare() did not clear pending after settings change: {}".format(inverter.deye_local.pending))
            failed = True
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_settings_change_clears_pending"


def test_apply_target_soc_out_of_range_fails_cleanly(my_predbat):
    """A target SoC outside [0, 100] fails closed via the _number validator, surfaced as a 'failed' status."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=50, source="Disabled", power=0, prefix="range_apply", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []

        for bad in (-1, 101, math.inf):
            my_predbat.ha_interface.service_store = []
            ok = inverter.adjust_charge_immediate(bad, freeze=False)
            if ok is not False:
                print("ERROR: target_soc {} should fail closed, got {}".format(bad, ok))
                failed = True
            if my_predbat.ha_interface.service_store:
                print("ERROR: target_soc {} wrote services: {}".format(bad, my_predbat.ha_interface.service_store))
                failed = True
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_target_soc_out_of_range_fails_cleanly"


def test_apply_rejects_reserve_percent_out_of_range(my_predbat):
    """inverter.reserve_percent outside [0, 100] makes apply() fail closed via the floor validator."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=50, source="Disabled", power=0, prefix="reserve", control=True)
    try:
        inverter.reserve_percent = 150  # out of range
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []

        ok = inverter.adjust_charge_immediate(80, freeze=False)
        if ok is not False:
            print("ERROR: bad reserve should fail closed, got {}".format(ok))
            failed = True
        if my_predbat.ha_interface.service_store:
            print("ERROR: bad reserve wrote services: {}".format(my_predbat.ha_interface.service_store))
            failed = True
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_rejects_reserve_percent_out_of_range"


def test_deye_local_does_not_impose_set_export_window_or_freeze(my_predbat):
    """The DEYE local adapter overrides set_export_* / set_charge_low_power / set_freeze_export_during_demand flags to False."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=20, source="Disabled", power=0, prefix="flags", control=True)
    try:
        for flag in ("set_export_window", "set_export_freeze", "set_export_freeze_only", "set_charge_low_power", "set_freeze_export_during_demand"):
            if getattr(my_predbat, flag):
                print("ERROR: prepare() did not clear {} on DEYE local".format(flag))
                failed = True
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_deye_local_does_not_impose_set_export_window_or_freeze"


def test_apply_failure_status_records_dashboard(my_predbat):
    """A failed apply publishes a dashboard status carrying the reason."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=50, source="Disabled", power=0, prefix="dash", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        inverter.adjust_charge_immediate(-5, freeze=False)
        control_entity = "{}.deye_local_control".format(my_predbat.prefix)
        observed = my_predbat.ha_interface.dummy_items.get(control_entity)
        if not observed or observed.get("state") != "failed":
            print("ERROR: status not 'failed': {}".format(observed))
            failed = True
        attrs = observed or {}
        if not attrs.get("reason"):
            print("ERROR: failed status missing reason: {}".format(attrs))
            failed = True
        if not attrs.get("hardware_commissioning_required"):
            print("ERROR: status should advertise hardware_commissioning_required: {}".format(attrs))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_failure_status_records_dashboard"


def test_execute_plan_integration_calls_apply(my_predbat):
    """execute_plan() routes to inverter.adjust_charge_immediate, which the DEYE local adapter owns."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=20, source="Disabled", power=0, prefix="exec", control=True, hold_verified=True)
    try:
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False

        my_predbat.inverters = [inverter]
        my_predbat.soc_kw = 0.0
        my_predbat.soc_max = 10.0
        my_predbat.battery_loss = 1.0
        my_predbat.inverter_loss = 1.0
        my_predbat.battery_loss_discharge = 1.0
        my_predbat.inverter_hybrid = True
        my_predbat.num_inverters = 1
        my_predbat.inverter_limit = 1.0 / 60.0
        my_predbat.export_limit = 10.0 / 60.0
        my_predbat.pv_ac_limit = 0.0
        my_predbat.charge_window = []
        my_predbat.export_window = []
        my_predbat.export_limits = []
        my_predbat.current_charge_limit = 0
        my_predbat.battery_rate_max_charge = 1.0 / 60.0
        my_predbat.battery_rate_max_charge_dc = 1.0 / 60.0
        my_predbat.battery_rate_max_discharge = 1.0 / 60.0
        my_predbat.battery_rate_min = 0.0
        my_predbat.reserve = 0.0
        my_predbat.reserve_percent = 0.0
        my_predbat.reserve_current = 0.0
        my_predbat.reserve_percent_current = 0.0
        my_predbat.inverter_soc_reset = False
        my_predbat.inverter_needs_reset = False
        my_predbat.metric_battery_cycle = 0
        my_predbat.set_charge_window = True
        my_predbat.set_export_window = False
        my_predbat.set_charge_freeze = True
        my_predbat.set_export_freeze = False
        my_predbat.set_export_freeze_only = False
        my_predbat.set_charge_freeze_only = False
        my_predbat.set_freeze_export_during_demand = False
        my_predbat.inverter_hybrid = True
        my_predbat.minutes_now = 12 * 60
        my_predbat.best_soc_keep = 0.0
        my_predbat.carbon_enable = 0
        my_predbat.alert_active_keep = {}
        my_predbat.manual_soc_keep = {}
        my_predbat.manual_soc_max_keep = {}
        my_predbat.charge_limit_best = []
        my_predbat.charge_window_best = []
        my_predbat.export_limits_best = []
        my_predbat.export_window_best = []
        my_predbat.manual_charge_times = []
        my_predbat.manual_demand_times = []
        my_predbat.manual_export_times = []
        my_predbat.manual_freeze_charge_times = []
        my_predbat.manual_freeze_export_times = []
        my_predbat.num_cars = 0
        my_predbat.car_charging_slots = [None] * 5
        my_predbat.car_charging_now = [False] * 5
        my_predbat.car_charging_from_battery = [False] * 5
        my_predbat.car_charging_limit = [100.0] * 5
        my_predbat.car_charging_soc = [0] * 5
        my_predbat.iboost_enable = False
        my_predbat.iboost_solar = False
        my_predbat.iboost_gas = False
        my_predbat.iboost_gas_export = False
        my_predbat.iboost_charging = False
        my_predbat.iboost_smart = False
        my_predbat.iboost_on_export = False
        my_predbat.iboost_prevent_discharge = False
        my_predbat.charge_rate_now = 1.0 / 60.0
        my_predbat.discharge_rate_now = 1.0 / 60.0
        my_predbat.pv_power = 0
        my_predbat.load_power = 0
        my_predbat.grid_power = 0
        my_predbat.battery_temperature_charge_curve = {20: 1.0, 10: 0.5, 9: 0.5, 8: 0.5, 7: 0.5, 6: 0.3, 5: 0.3, 4: 0.3, 3: 0.262, 2: 0.1, 1: 0.1, 0: 0}
        my_predbat.pv_forecast_minute = {minute: 0.0 for minute in range(my_predbat.minutes_now, my_predbat.minutes_now + my_predbat.forecast_minutes)}
        my_predbat.count_inverter_writes = {}

        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []

        inverter.update_status(my_predbat.minutes_now)
        inverter.soc_percent = 50
        inverter.soc_kw = inverter.soc_max * 0.5
        inverter.reserve_percent = 10
        inverter.reserve = inverter.soc_max * 0.1
        my_predbat.soc_kw = inverter.soc_kw
        my_predbat.soc_max = inverter.soc_max
        my_predbat.reserve = inverter.reserve
        my_predbat.reserve_percent = inverter.reserve_percent
        my_predbat.battery_rate_max_charge = inverter.battery_rate_max_charge
        my_predbat.battery_rate_max_discharge = inverter.battery_rate_max_discharge
        my_predbat.set_reserve_enable = False
        my_predbat.set_reserve_hold = False
        my_predbat.set_discharge_during_charge = True
        my_predbat.set_soc_enable = True
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)
        original_times = [my_predbat.ha_interface.dummy_items[row["time"]]["state"] for row in rows]
        try:
            for minute, target, expected_soc, expected_source in (
                (720, None, 10, "Disabled"),
                (725, 80, 80, "Grid"),
                (730, "freeze", 100, "Disabled"),
                (735, 30, 30, "Grid"),
                (1435, 80, 80, "Grid"),
                (1500, None, 10, "Disabled"),
            ):
                my_predbat.minutes_now = minute
                my_predbat.now_utc = my_predbat.midnight_utc + timedelta(minutes=minute)
                my_predbat.manual_soc_keep = {minute: 30} if target == 30 else {}
                manual_before = copy.deepcopy(my_predbat.manual_soc_keep)
                my_predbat.charge_window_best = [] if target is None else [{"start": minute, "end": minute + 30, "average": 1}]
                my_predbat.charge_limit_best = [] if target is None else [my_predbat.reserve if target == "freeze" else inverter.soc_max * target / 100]
                my_predbat.execute_plan()
                assert inverter.deye_local.status == "observed", (minute, target, inverter.deye_local.status)
                assert my_predbat.manual_soc_keep == manual_before
                for row in rows:
                    assert my_predbat.ha_interface.dummy_items[row["soc"]]["state"] == expected_soc, (minute, target, my_predbat.ha_interface.dummy_items[row["soc"]]["state"], expected_soc)
                    assert my_predbat.ha_interface.dummy_items[row["charging"]]["state"] == expected_source
                    if target == "freeze":
                        assert my_predbat.ha_interface.dummy_items[row["power"]]["state"] == 0
            assert my_predbat.ha_interface.service_store, "Real execute_plan issued no DEYE commands"
            allowed = _scrub_base(inverter.deye_local)
            seen = {k["entity_id"] for s, k in my_predbat.ha_interface.service_store if "entity_id" in k}
            assert seen <= allowed, seen - allowed
            assert original_times == [my_predbat.ha_interface.dummy_items[row["time"]]["state"] for row in rows]
        finally:
            my_predbat.call_service_wrapper = original_call
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_execute_plan_integration_calls_apply"


def test_inverter_adjusted_directly_via_adjust_charge_immediate(my_predbat):
    """A direct call to inverter.adjust_charge_immediate() drives the adapter end-to-end."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(
        my_predbat,
        max_power_w=5000,
        soc=20,
        source="Disabled",
        power=0,
        prefix="adj",
        control=True,
        hold_verified=True,
    )
    try:
        inverter.reserve_percent = 10
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        # Freeze path
        inverter.adjust_charge_immediate(40, freeze=True)
        for r in rows:
            if my_predbat.ha_interface.dummy_items[r["charging"]]["state"] != "Disabled":
                print("ERROR: freeze left row {} source at {}".format(r["charging"], my_predbat.ha_interface.dummy_items[r["charging"]]["state"]))
                failed = True

        # Now a real charge
        for r in rows:
            my_predbat.ha_interface.dummy_items[r["soc"]]["state"] = 100
            my_predbat.ha_interface.dummy_items[r["power"]]["state"] = 0
            my_predbat.ha_interface.dummy_items[r["charging"]]["state"] = "Disabled"
        my_predbat.ha_interface.service_store = []

        inverter.adjust_charge_immediate(90, freeze=False)
        grid = [s for s, k in my_predbat.ha_interface.service_store if s == "select/select_option" and k.get("option") == "Grid"]
        if len(grid) != 6:
            print("ERROR: charge should arm Grid 6 times, got {}".format(len(grid)))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_inverter_adjusted_directly_via_adjust_charge_immediate"


def test_prepare_accepts_minute_seconds_and_second_minute(my_predbat):
    """Time strings of HH:MM or HH:MM:SS are both valid HA observations."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=50, source="Disabled", power=0, prefix="secs", control=True)
    try:
        for index, row in enumerate(rows):
            hour = (index * 4) % 24
            my_predbat.ha_interface.dummy_items[row["time"]] = {
                "state": "{:02d}:00:00".format(hour),
                "attributes": {},
            }
        snap = inverter.deye_local.snapshot()
        if not snap:
            print("ERROR: HH:MM:SS with :00 seconds should snapshot")
            failed = True

        for index, row in enumerate(rows):
            hour = (index * 4) % 24
            my_predbat.ha_interface.dummy_items[row["time"]] = {
                "state": "{:02d}:00".format(hour),
                "attributes": {},
            }
        snap = inverter.deye_local.snapshot()
        if not snap:
            print("ERROR: HH:MM should snapshot")
            failed = True
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_prepare_accepts_minute_seconds_and_second_minute"


def test_apply_skipped_when_target_equals_observation(my_predbat):
    """If the observed state already matches the desired, apply() writes nothing."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=80, source="Grid", power=5000, prefix="noop", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        ok = inverter.adjust_charge_immediate(80, freeze=False)
        if not ok:
            print("ERROR: matching observed state should confirm, got False")
            failed = True
        if my_predbat.ha_interface.service_store:
            print("ERROR: matching state wrote services: {}".format(my_predbat.ha_interface.service_store))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_skipped_when_target_equals_observation"


def test_apply_uses_smaller_of_configured_cap_and_inverter_cap(my_predbat):
    """The power write is min(configured cap, native battery_rate_max_discharge * MINUTE_WATT)."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=3000, soc=20, source="Disabled", power=0, prefix="min_cap", control=True)
    try:
        inverter.battery_rate_max_discharge = 8000 / 60.0 / 1000.0
        inverter.reserve_percent = 4
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        inverter.adjust_charge_immediate(80, freeze=False)
        power_values = [k["value"] for s, k in my_predbat.ha_interface.service_store if s == "number/set_value" and k["entity_id"] in {r["power"] for r in rows}]
        if any(v != 3000 for v in power_values):
            print("ERROR: power not capped to configured 3000 W: {}".format(power_values))
            failed = True

        inverter.battery_rate_max_discharge = 1000 / 60.0 / 1000.0
        my_predbat.ha_interface.service_store = []
        inverter.adjust_charge_immediate(80, freeze=False)
        power_values = [k["value"] for s, k in my_predbat.ha_interface.service_store if s == "number/set_value" and k["entity_id"] in {r["power"] for r in rows}]
        if any(v != 1000 for v in power_values):
            print("ERROR: power not capped to native 1000 W: {}".format(power_values))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_uses_smaller_of_configured_cap_and_inverter_cap"


def test_apply_writes_grid_after_soc_and_power_phase(my_predbat):
    """The order Disabled -> (SOC + power) -> Grid is exactly preserved across multiple cycles."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=20, source="Grid", power=2000, prefix="phase_order", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        for cycle in range(2):
            my_predbat.ha_interface.service_store = []
            inverter.adjust_charge_immediate(80 + cycle, freeze=False)
            service_log = my_predbat.ha_interface.service_store
            last_disabled = max(
                (i for i, (s, k) in enumerate(service_log) if s == "select/select_option" and k.get("option") == "Disabled"),
                default=-1,
            )
            first_numeric = next((i for i, (s, k) in enumerate(service_log) if s == "number/set_value"), len(service_log))
            first_grid = next((i for i, (s, k) in enumerate(service_log) if s == "select/select_option" and k.get("option") == "Grid"), len(service_log))
            if not (last_disabled < first_numeric < first_grid):
                print("ERROR: cycle {} order broken (last_disabled={}, first_numeric={}, first_grid={})".format(cycle, last_disabled, first_numeric, first_grid))
                failed = True

            for r in rows:
                my_predbat.ha_interface.dummy_items[r["soc"]]["state"] = 80 + cycle
                my_predbat.ha_interface.dummy_items[r["power"]]["state"] = 5000
                my_predbat.ha_interface.dummy_items[r["charging"]]["state"] = "Grid"
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_writes_grid_after_soc_and_power_phase"


def test_apply_does_not_touch_other_inverter_entities(my_predbat):
    """No entity outside the configured ToU rows is ever written."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=20, source="Disabled", power=0, prefix="strict", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False

        decoys = {
            "number.dt_strict_charge_limit": 100,
            "number.dt_strict_reserve": 10,
            "number.dt_strict_current": 50,
            "select.dt_strict_bms": "off",
            "select.dt_strict_work_mode": "Selling First",
            "select.dt_strict_energy_pattern": "Selling First",
            "number.dt_strict_peak": 5000,
            "number.dt_strict_ev": 0,
        }
        for entity, state in decoys.items():
            my_predbat.ha_interface.dummy_items[entity] = {"state": state}

        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        inverter.adjust_charge_immediate(80, freeze=False)

        allowed = _scrub_base(inverter.deye_local)
        seen = {k["entity_id"] for s, k in my_predbat.ha_interface.service_store if "entity_id" in k}
        outside = seen - allowed
        decoy_writes = outside & set(decoys)
        if decoy_writes:
            print("ERROR: decoy entities were written: {}".format(decoy_writes))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_does_not_touch_other_inverter_entities"


def test_apply_uses_only_setter_calls_for_charge_immediate(my_predbat):
    """The physical writes are exclusively number/set_value and select/select_option."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=20, source="Disabled", power=0, prefix="services", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        inverter.adjust_charge_immediate(80, freeze=False)
        services = {s for s, _ in my_predbat.ha_interface.service_store}
        expected = {"number/set_value", "select/select_option"}
        unexpected = services - expected
        if unexpected:
            print("ERROR: unexpected services used: {}".format(unexpected))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_uses_only_setter_calls_for_charge_immediate"


def test_apply_writes_each_row_with_matching_field(my_predbat):
    """Each row receives its own time/soc/power/charging entities - no cross-talk."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=20, source="Disabled", power=0, prefix="rows", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        inverter.adjust_charge_immediate(80, freeze=False)
        soc_writes = {k["entity_id"]: k["value"] for s, k in my_predbat.ha_interface.service_store if s == "number/set_value" and k["entity_id"] in {r["soc"] for r in rows}}
        if set(soc_writes) != {r["soc"] for r in rows}:
            print("ERROR: SOC entities written do not match rows: {}".format(soc_writes))
            failed = True
        if set(soc_writes.values()) != {80}:
            print("ERROR: SOC writes not all 80: {}".format(set(soc_writes.values())))
            failed = True
        power_writes = {k["entity_id"]: k["value"] for s, k in my_predbat.ha_interface.service_store if s == "number/set_value" and k["entity_id"] in {r["power"] for r in rows}}
        if set(power_writes) != {r["power"] for r in rows}:
            print("ERROR: power entities written do not match rows: {}".format(power_writes))
            failed = True
        if set(power_writes.values()) != {5000}:
            print("ERROR: power writes not all 5000: {}".format(set(power_writes.values())))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_writes_each_row_with_matching_field"


def test_apply_records_one_register_write_per_service(my_predbat):
    """Each successful service call increments inverter.count_register_writes by one."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=20, source="Grid", power=2000, prefix="counter", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)
        baseline = inverter.count_register_writes

        inverter.adjust_charge_immediate(80, freeze=False)
        service_count = sum(1 for s, _ in my_predbat.ha_interface.service_store if s in ("number/set_value", "select/select_option"))
        if inverter.count_register_writes - baseline != service_count:
            print("ERROR: count_register_writes grew by {} but service count is {}".format(inverter.count_register_writes - baseline, service_count))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_records_one_register_write_per_service"


def test_apply_charge_freeze_keeps_grid_disabled(my_predbat):
    """A freeze keeps the source at Disabled; subsequent charge arms Grid again."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(
        my_predbat,
        max_power_w=5000,
        soc=80,
        source="Grid",
        power=5000,
        prefix="cycle",
        control=True,
        hold_verified=True,
    )
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        inverter.adjust_charge_immediate(40, freeze=True)
        for r in rows:
            if my_predbat.ha_interface.dummy_items[r["charging"]]["state"] != "Disabled":
                print("ERROR: freeze left {} at {}".format(r["charging"], my_predbat.ha_interface.dummy_items[r["charging"]]["state"]))
                failed = True
        for r in rows:
            my_predbat.ha_interface.dummy_items[r["soc"]]["state"] = 100
            my_predbat.ha_interface.dummy_items[r["power"]]["state"] = 0
            my_predbat.ha_interface.dummy_items[r["charging"]]["state"] = "Disabled"
        my_predbat.ha_interface.service_store = []
        inverter.adjust_charge_immediate(80, freeze=False)
        grid_writes = [k["entity_id"] for s, k in my_predbat.ha_interface.service_store if s == "select/select_option" and k.get("option") == "Grid"]
        if sorted(grid_writes) != sorted(r["charging"] for r in rows):
            print("ERROR: charge after freeze did not rearm all six Grid sources: {}".format(grid_writes))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_charge_freeze_keeps_grid_disabled"


def test_snapshot_rejects_missing_entity_attributes(my_predbat):
    """snapshot() refuses to operate if any number entity is missing unit/min/max/step."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=50, source="Disabled", power=0, prefix="attrs", control=True)
    try:
        item = my_predbat.ha_interface.dummy_items[rows[0]["soc"]]
        item["attributes"] = {"min": 0, "max": 100, "step": 1}
        try:
            inverter.deye_local.snapshot()
            print("ERROR: missing unit_of_measurement should be rejected")
            failed = True
        except ValueError:
            pass

        item["attributes"] = {"max": 100, "step": 1, "unit_of_measurement": "%"}
        try:
            inverter.deye_local.snapshot()
            print("ERROR: missing min should be rejected")
            failed = True
        except ValueError:
            pass

        item["attributes"] = {"min": 0, "step": 1, "unit_of_measurement": "%"}
        try:
            inverter.deye_local.snapshot()
            print("ERROR: missing max should be rejected")
            failed = True
        except ValueError:
            pass
    finally:
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_snapshot_rejects_missing_entity_attributes"


def test_apply_phase_keeps_settle_sleep_after_writes(my_predbat):
    """After a write, _phase sleeps the inv_write_and_poll_sleep before re-snapshotting."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=20, source="Disabled", power=0, prefix="settle", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0

        sleeps = []

        my_predbat.set_read_only = False
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []
        original_call = _install_attribute_preserving_service_wrapper(my_predbat)

        with patch("deye_local.time.sleep", side_effect=sleeps.append):
            inverter.adjust_charge_immediate(80, freeze=False)
        original_sleep_used = sum(1 for s in sleeps if s == inverter.inv_write_and_poll_sleep)
        if original_sleep_used == 0:
            print("ERROR: _phase did not use the configured settle interval, sleeps={}".format(sleeps))
            failed = True
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_phase_keeps_settle_sleep_after_writes"


def test_apply_blocked_by_manual_soc_max(my_predbat):
    """manual_soc_max_keep is a Predbat-level solar-charge ceiling; the DEYE local adapter cannot enforce it and fails closed."""
    failed = False
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, max_power_w=5000, soc=20, source="Disabled", power=0, prefix="manual_soc", control=True)
    try:
        inverter.reserve_percent = 4
        inverter.battery_rate_max_discharge = 5000 / 60.0 / 1000.0
        inverter.sleep = _dummy_sleep
        my_predbat.set_read_only = False
        my_predbat.manual_soc_max_keep = {my_predbat.minutes_now: 95}
        my_predbat.ha_interface.service_store_enable = True
        my_predbat.ha_interface.service_store = []

        ok = inverter.adjust_charge_immediate(80, freeze=False)
        if ok is not False:
            print("ERROR: manual_soc_max_keep should make apply fail closed, got {}".format(ok))
            failed = True
        if my_predbat.ha_interface.service_store:
            print("ERROR: manual_soc_max_keep should not write, got {}".format(my_predbat.ha_interface.service_store))
            failed = True
    finally:
        my_predbat.manual_soc_max_keep = {}
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)
    assert not failed, "test_apply_blocked_by_manual_soc_max"


def test_deye_local_module_has_no_global_state(my_predbat):
    """The DeyeLocal class itself owns no module-level mutable state shared between instances."""
    # Module-level imports (math, re, time, datetime, pytz) are not Predbat's.
    import deye_local

    bad = []
    for attr in dir(deye_local):
        if attr.startswith("__") and attr.endswith("__"):
            continue
        if attr in ("math", "re", "time", "datetime", "timezone", "pytz", "MINUTE_WATT", "annotations"):
            # Imports and constants are expected; skip them.
            continue
        value = getattr(deye_local, attr)
        if callable(value) or isinstance(value, (int, float, str, bytes, bool, type(None))):
            continue
        bad.append(attr)
    assert not bad, "DeyeLocal module-level mutable state: {}".format(bad)


def test_superseded_pending_intent_and_rejected_write_counter(my_predbat):
    """New targets get a fresh retry budget; rejected calls are not counted as writes."""
    inverter, rows, saved_args, had_raw, saved_raw, saved_items = _make_inverter(my_predbat, control=True, soc=20)
    original_call = my_predbat.call_service_wrapper
    try:
        adapter = inverter.deye_local
        entity = rows[0]["soc"]
        my_predbat.call_service_wrapper = lambda *args, **kwargs: True
        with patch("deye_local.time.monotonic", return_value=1):
            for target in (80, 60, 40, 70):
                ready, _ = adapter._phase({entity: target}, adapter.snapshot())
                assert not ready and adapter.pending[entity]["attempts"] == 1
        assert inverter.count_register_writes == 4
        my_predbat.call_service_wrapper = lambda *args, **kwargs: False
        with patch("deye_local.time.monotonic", return_value=2):
            try:
                adapter._phase({entity: 90}, adapter.snapshot())
            except ValueError:
                pass
            else:
                raise AssertionError("Rejected service was treated as accepted")
        assert inverter.count_register_writes == 4
    finally:
        my_predbat.call_service_wrapper = original_call
        _restore(my_predbat, saved_args, had_raw, saved_raw, saved_items)


def run_deye_local_tests(my_predbat):
    """Entry point registered in TEST_REGISTRY by the parent harness.

    Runs each case with a fresh PredBat fixture; returns True on any failure.
    """
    from unit_test import create_predbat

    failed = False
    tests = [
        ("prepare_rejects_more_than_one_inverter", test_prepare_rejects_more_than_one_inverter),
        ("prepare_rejects_legacy_service_keys", test_prepare_rejects_legacy_service_keys),
        ("prepare_rejects_legacy_generic_mappings", test_prepare_rejects_legacy_generic_mappings),
        ("prepare_rejects_capability_overrides", test_prepare_rejects_capability_overrides),
        ("prepare_rejects_wrong_number_of_programs", test_prepare_rejects_wrong_number_of_programs),
        ("prepare_rejects_duplicate_entities", test_prepare_rejects_duplicate_entities),
        ("prepare_rejects_non_integer_or_out_of_range_discharge_cap", test_prepare_rejects_non_integer_or_out_of_range_discharge_cap),
        ("prepare_rejects_malformed_program_rows", test_prepare_rejects_malformed_program_rows),
        ("prepare_rejects_settings_with_wrong_keys", test_prepare_rejects_settings_with_wrong_keys),
        ("prepare_rejects_non_boolean_control_or_hold_verified", test_prepare_rejects_non_boolean_control_or_hold_verified),
        ("prepare_accepts_control_true_only_with_telemetry_time", test_prepare_accepts_control_true_only_with_telemetry_time),
        ("validate_limits_caps_discharge_and_zeros_export", test_validate_limits_caps_discharge_and_zeros_export),
        ("apply_returns_disabled_when_control_flag_false", test_apply_returns_disabled_when_control_flag_false),
        ("apply_charge_writes_grid_and_raises_soc", test_apply_charge_writes_grid_and_raises_soc),
        ("apply_charge_uses_reserve_as_floor", test_apply_charge_uses_reserve_as_floor),
        ("apply_demand_writes_disabled_and_low_soc", test_apply_demand_writes_disabled_and_low_soc),
        ("apply_hold_requires_hold_verified", test_apply_hold_requires_hold_verified),
        ("apply_hold_freeze_writes_zero_power_and_disabled_source", test_apply_hold_freeze_writes_zero_power_and_disabled_source),
        ("apply_returns_false_on_read_only", test_apply_returns_false_on_read_only),
        ("export_immediate_is_no_op_on_deye_local", test_export_immediate_is_no_op_on_deye_local),
        ("apply_uses_only_configured_tou_fields", test_apply_uses_only_configured_tou_fields),
        ("apply_only_writes_what_changed_dedup", test_apply_only_writes_what_changed_dedup),
        ("apply_all_six_transitions_each_get_one_write", test_apply_all_six_transitions_each_get_one_write),
        ("apply_mismatch_repaired_observed_before_next_phase", test_apply_mismatch_repaired_observed_before_next_phase),
        ("apply_rejected_call_prevents_later_phase", test_apply_rejected_call_prevents_later_phase),
        ("apply_accepted_without_observation_waits_for_next_native_cycle", test_apply_accepted_without_observation_waits_for_next_native_cycle),
        ("apply_bounded_retry_after_three_attempts_raises", test_apply_bounded_retry_after_three_attempts_raises),
        ("snapshot_rejects_invalid_state_observations", test_snapshot_rejects_invalid_state_observations),
        ("snapshot_rejects_non_integer_or_out_of_range_numeric", test_snapshot_rejects_non_integer_or_out_of_range_numeric),
        ("snapshot_rejects_bad_time_formats", test_snapshot_rejects_bad_time_formats),
        ("snapshot_rejects_unsorted_or_duplicate_times", test_snapshot_rejects_unsorted_or_duplicate_times),
        ("snapshot_rejects_wrong_unit_or_step_or_range", test_snapshot_rejects_wrong_unit_or_step_or_range),
        ("snapshot_rejects_non_allowed_operating_modes", test_snapshot_rejects_non_allowed_operating_modes),
        ("snapshot_rejects_charging_source_options_missing_disabled_or_grid", test_snapshot_rejects_charging_source_options_missing_disabled_or_grid),
        ("snapshot_accepts_first_row_at_00_00_and_subsequent_at_00_00", test_snapshot_accepts_first_row_at_00_00_and_subsequent_at_00_00),
        ("snapshot_rejects_telemetry_clock_outside_window", test_snapshot_rejects_telemetry_clock_outside_window),
        ("apply_respects_manual_native_charge_intent", test_apply_respects_manual_native_charge_intent),
        ("apply_demand_uses_reserve_floor_and_cap_power", test_apply_demand_uses_reserve_floor_and_cap_power),
        ("native_freeze_uses_power_zero_and_disabled_grid_and_keeps_pv_cap", test_native_freeze_uses_power_zero_and_disabled_grid_and_keeps_pv_cap),
        ("reset_safe_no_force_export_fallback", test_reset_safe_no_force_export_fallback),
        ("per_inverter_discharge_cap_caps_battery_rate", test_per_inverter_discharge_cap_caps_battery_rate),
        ("settings_change_clears_pending", test_settings_change_clears_pending),
        ("apply_target_soc_out_of_range_fails_cleanly", test_apply_target_soc_out_of_range_fails_cleanly),
        ("apply_rejects_reserve_percent_out_of_range", test_apply_rejects_reserve_percent_out_of_range),
        ("deye_local_does_not_impose_set_export_window_or_freeze", test_deye_local_does_not_impose_set_export_window_or_freeze),
        ("apply_failure_status_records_dashboard", test_apply_failure_status_records_dashboard),
        ("execute_plan_integration_calls_apply", test_execute_plan_integration_calls_apply),
        ("inverter_adjusted_directly_via_adjust_charge_immediate", test_inverter_adjusted_directly_via_adjust_charge_immediate),
        ("prepare_accepts_minute_seconds_and_second_minute", test_prepare_accepts_minute_seconds_and_second_minute),
        ("apply_skipped_when_target_equals_observation", test_apply_skipped_when_target_equals_observation),
        ("apply_uses_smaller_of_configured_cap_and_inverter_cap", test_apply_uses_smaller_of_configured_cap_and_inverter_cap),
        ("apply_writes_grid_after_soc_and_power_phase", test_apply_writes_grid_after_soc_and_power_phase),
        ("apply_does_not_touch_other_inverter_entities", test_apply_does_not_touch_other_inverter_entities),
        ("apply_uses_only_setter_calls_for_charge_immediate", test_apply_uses_only_setter_calls_for_charge_immediate),
        ("apply_writes_each_row_with_matching_field", test_apply_writes_each_row_with_matching_field),
        ("apply_records_one_register_write_per_service", test_apply_records_one_register_write_per_service),
        ("apply_charge_freeze_keeps_grid_disabled", test_apply_charge_freeze_keeps_grid_disabled),
        ("snapshot_rejects_missing_entity_attributes", test_snapshot_rejects_missing_entity_attributes),
        ("apply_phase_keeps_settle_sleep_after_writes", test_apply_phase_keeps_settle_sleep_after_writes),
        ("apply_blocked_by_manual_soc_max", test_apply_blocked_by_manual_soc_max),
        ("deye_local_module_has_no_global_state", test_deye_local_module_has_no_global_state),
        ("superseded_pending_intent_and_rejected_write_counter", test_superseded_pending_intent_and_rejected_write_counter),
    ]
    for name, func in tests:
        try:
            with patch("deye_local.time.sleep"), patch("socket.socket.connect", side_effect=AssertionError("Network is forbidden in DEYE tests")):
                func(create_predbat())
            print("PASS:", name)
        except Exception as exc:
            failed = True
            print("ERROR: {} raised {}".format(name, exc))
            traceback.print_exc()
    return failed
