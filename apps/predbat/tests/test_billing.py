# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""
Unit tests for billing.py: reading the half-hourly bill from the Kraken measurements API.

The fixtures in billing_fixture.json are real (anonymised) responses from one Octopus account on
Intelligent Octopus Go with Agile Outgoing export, read on 2 Nov 2026 - see the "Verified data
shape" section of docs/superpowers/specs/2026-11-01-octopus-billing-history-design.md.
"""

import copy
import json
import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import billing
from billing import parse_measurements, day_slot_starts, merge_slots, days_to_fetch, build_billing_sensor

LONDON = ZoneInfo("Europe/London")
_FIXTURE_FILE = os.path.join(os.path.dirname(__file__), "billing_fixture.json")


def load_fixture(name):
    """Return a deep copy of one measurements payload from billing_fixture.json."""
    with open(_FIXTURE_FILE) as handle:
        return copy.deepcopy(json.load(handle)[name])


def _node(start, value, statistics):
    """Build one measurements node the way the API returns it."""
    end = (datetime.fromisoformat(start) + timedelta(minutes=30)).isoformat()
    return {"value": None if value is None else str(value), "unit": "kwh", "startAt": start, "endAt": end, "metaData": {"statistics": statistics}}


def _stat(stat_type, label, value, cost):
    """Build one measurements statistic row."""
    money = {"estimatedAmount": str(cost), "costCurrency": "GBP"}
    return {"type": stat_type, "label": label, "description": None, "value": None if value is None else str(value), "costInclTax": money, "costExclTax": money}


def _response(nodes):
    """Wrap nodes in the measurements response envelope."""
    return {"account": {"properties": [{"measurements": {"edges": [{"node": node} for node in nodes]}}]}}


def _slots_for_day(day, kwh=1.0, cost=10.0, label="Rate"):
    """A full day of parsed slots for one local date, every slot the same."""
    slots = {}
    for start in day_slot_starts(day, LONDON):
        slots[start.strftime("%Y-%m-%dT%H:%M:%SZ")] = {"kwh": kwh, "cost": cost, "standing": 1.0, "buckets": {label: {"kwh": kwh, "cost": cost}}}
    return slots


def _close(a, b, tolerance=0.01):
    """True when two floats agree within tolerance."""
    return a is not None and b is not None and abs(a - b) <= tolerance


def test_parse_iog_buckets():
    """The IOG import day parses into 48 trusted slots with the standing charge kept out of the buckets."""
    failed = False
    payload = load_fixture("octopus_iog_import_2026_10_31")
    slots = parse_measurements(payload)
    if not slots or len(slots) != 48:
        print("ERROR: expected 48 slots, got {}".format(len(slots) if slots else slots))
        return True
    fixture_labels = set()
    for edge in payload["account"]["properties"][0]["measurements"]["edges"]:
        for stat in edge["node"]["metaData"]["statistics"]:
            if stat["type"] != "STANDING_CHARGE_COST" and float(stat["value"] or 0) > 0:
                fixture_labels.add(stat["label"])
    labels = set()
    standing_total = 0.0
    for key, slot in slots.items():
        if not _close(sum(b["kwh"] for b in slot["buckets"].values()), slot["kwh"]):
            print("ERROR: slot {} bucket kWh do not sum to the slot's kWh".format(key))
            failed = True
        if not _close(sum(b["cost"] for b in slot["buckets"].values()), slot["cost"], 0.0001):
            print("ERROR: slot {} bucket costs do not sum to the slot's cost".format(key))
            failed = True
        if not _close(slot["standing"], 1.15674, 0.00001):
            print("ERROR: slot {} standing should be 1.15674, got {}".format(key, slot["standing"]))
            failed = True
        if any("STANDING" in label.upper() for label in slot["buckets"]):
            print("ERROR: slot {} has the standing charge in its buckets".format(key))
            failed = True
        standing_total += slot["standing"]
        labels |= set(slot["buckets"])
    if not _close(standing_total, 55.52):
        print("ERROR: the day's standing charge should be 55.52p, got {}".format(standing_total))
        failed = True
    if labels != fixture_labels:
        print("ERROR: bucket labels {} should be exactly the fixture's {}".format(labels, fixture_labels))
        failed = True
    first = slots.get("2026-10-31T00:00:00Z")
    if not first or not _close(first["cost"], 9.801, 0.0001) or set(first["buckets"]) != {"Home Low Rate"}:
        print("ERROR: 00:00Z should be 9.801p all in Home Low Rate, got {}".format(first))
        failed = True
    return failed


def test_parse_export_generation():
    """Export half-hours carry their kWh on the node and one GENERATION_VALUE row."""
    failed = False
    slots = parse_measurements(load_fixture("octopus_export_2026_10_31"))
    if not slots or len(slots) != 48:
        print("ERROR: expected 48 export slots, got {}".format(len(slots) if slots else slots))
        return True
    peak = slots.get("2026-10-31T18:00:00Z")
    if not peak or list(peak["buckets"]) != ["GENERATION"] or not _close(peak["buckets"]["GENERATION"]["kwh"], 1.417, 0.0001) or not _close(peak["buckets"]["GENERATION"]["cost"], 44.86, 0.0001):
        print("ERROR: 18:00Z export should be one GENERATION bucket of 1.417 kWh / 44.86p, got {}".format(peak))
        failed = True
    if not _close(sum(s["kwh"] for s in slots.values()), 5.363, 0.0001) or not _close(sum(s["cost"] for s in slots.values()), 168.92, 0.0001):
        print("ERROR: export day should total 5.363 kWh / 168.92p")
        failed = True
    return failed


def test_parse_unknown_labels():
    """Bucket labels are taken as they come, never matched by name."""
    nodes = [_node("2026-11-01T10:00:00+00:00", 1.5, [_stat("CONSUMPTION_COST", "Zebra", 1.0, 10.0), _stat("CONSUMPTION_COST", "Yak", 0.5, 2.5), _stat("STANDING_CHARGE_COST", "SC", None, 1.2)])]
    slots = parse_measurements(_response(nodes))
    slot = (slots or {}).get("2026-11-01T10:00:00Z")
    if not slot or set(slot["buckets"]) != {"Zebra", "Yak"} or not _close(slot["cost"], 12.5) or not _close(slot["standing"], 1.2):
        print("ERROR: expected buckets Zebra and Yak costing 12.5p with 1.2p standing, got {}".format(slot))
        return True
    return False


def test_parse_bucket_mismatch_drops_slot():
    """A half-hour whose buckets don't add up to its kWh can't be trusted and is left out."""
    nodes = [
        _node("2026-11-01T10:00:00+00:00", 1.0, [_stat("CONSUMPTION_COST", "A", 1.0, 5.0), _stat("CONSUMPTION_COST", "B", 1.0, 5.0)]),
        _node("2026-11-01T10:30:00+00:00", 1.0, [_stat("CONSUMPTION_COST", "A", 1.0, 5.0)]),
    ]
    slots = parse_measurements(_response(nodes))
    if slots is None or "2026-11-01T10:00:00Z" in slots or "2026-11-01T10:30:00Z" not in slots:
        print("ERROR: the mismatched 10:00Z slot should be dropped and 10:30Z kept, got {}".format(None if slots is None else sorted(slots)))
        return True
    return False


def test_parse_none_vs_empty():
    """A failed read is None; a read that succeeded with nothing published is an empty dict."""
    if parse_measurements(None) is not None:
        print("ERROR: parse_measurements(None) should be None")
        return True
    if parse_measurements({"account": {"properties": [{"measurements": {"edges": []}}]}}) != {}:
        print("ERROR: an empty read should parse to {}")
        return True
    return False


def test_day_slot_starts_clock_change():
    """A local day has 50 half-hours when the clocks go back and 46 when they go forward."""
    failed = False
    for day, expected in ((date(2026, 10, 25), 50), (date(2026, 3, 29), 46), (date(2026, 11, 2), 48)):
        starts = day_slot_starts(day, LONDON)
        if len(starts) != expected:
            print("ERROR: {} should have {} half-hours, got {}".format(day, expected, len(starts)))
            failed = True
        elif starts[0].tzinfo is None or starts[0].utcoffset() != timedelta(0):
            print("ERROR: slot starts should be UTC datetimes")
            failed = True
    clock_change = parse_measurements(load_fixture("octopus_iog_import_2026_10_25"))
    if not clock_change or len(clock_change) != 50:
        print("ERROR: the 25 Oct fixture should parse to 50 slots, got {}".format(len(clock_change) if clock_change else clock_change))
        failed = True
    return failed


def test_days_to_fetch_skips_complete():
    """Complete days are never asked for again; incomplete ones in the window are."""
    now_utc = datetime(2026, 11, 2, 10, 0, tzinfo=LONDON).astimezone(timezone.utc)
    cache = {"import": _slots_for_day(date(2026, 10, 31))}
    days = days_to_fetch(cache, "import", now_utc, LONDON)
    if date(2026, 10, 31) in days or date(2026, 11, 1) not in days or date(2026, 11, 2) not in days or min(days) < date(2026, 10, 26):
        print("ERROR: unexpected days to fetch {}".format(days))
        return True
    return False


def test_merge_prunes_old():
    """Slots older than the 7-day window are dropped on merge, and the input cache is not changed."""
    now_utc = datetime(2026, 11, 2, 10, 0, tzinfo=LONDON).astimezone(timezone.utc)
    cache = {"import": {"2026-10-20T10:00:00Z": {"kwh": 1.0, "cost": 1.0, "standing": 0.0, "buckets": {}}}}
    merged = merge_slots(cache, "import", _slots_for_day(date(2026, 11, 1)), now_utc, LONDON)
    if "2026-10-20T10:00:00Z" in merged["import"] or "2026-11-01T10:00:00Z" not in merged["import"]:
        print("ERROR: merge should drop 20 Oct and keep 1 Nov")
        return True
    if "2026-10-20T10:00:00Z" not in cache["import"]:
        print("ERROR: merge_slots should not modify the cache it was given")
        return True
    return False


def test_sensor_shape():
    """The sensor's state is the last complete day's net energy cost; attributes carry slots and day summaries."""
    failed = False
    now_utc = datetime(2026, 11, 2, 10, 0, tzinfo=LONDON).astimezone(timezone.utc)
    imports = _slots_for_day(date(2026, 11, 1), kwh=1.0, cost=10.0, label="Home Low Rate")
    partial = _slots_for_day(date(2026, 11, 2), kwh=1.0, cost=10.0, label="Home Low Rate")
    imports.update({key: value for key, value in sorted(partial.items())[:4]})
    exports = _slots_for_day(date(2026, 11, 1), kwh=0.5, cost=7.5, label="GENERATION")
    cache = {"import": imports, "export": exports}
    state, attributes = build_billing_sensor(cache, {"import": True, "export": True}, "octopus", now_utc, LONDON)
    if not _close(state, 48 * 10.0 - 48 * 7.5):
        print("ERROR: state should be 1 Nov net energy cost {}, got {}".format(48 * 10.0 - 48 * 7.5, state))
        failed = True
    for key in ("supplier", "legs", "complete_until", "slots", "days", "friendly_name", "unit_of_measurement", "icon"):
        if key not in attributes:
            print("ERROR: sensor attributes missing {}".format(key))
            failed = True
    if failed:
        return True
    if attributes["supplier"] != "octopus" or attributes["legs"] != {"import": True, "export": True} or attributes["unit_of_measurement"] != "p" or attributes["icon"] != "mdi:receipt-text":
        print("ERROR: unexpected supplier/legs/unit/icon {}".format({k: attributes[k] for k in ("supplier", "legs", "unit_of_measurement", "icon")}))
        failed = True
    if attributes["complete_until"] != "2026-11-02T02:00:00Z":
        print("ERROR: complete_until should be the end of the last contiguous import slot 2026-11-02T02:00:00Z, got {}".format(attributes["complete_until"]))
        failed = True
    if set(attributes["slots"]) != {"import", "export"} or len(attributes["slots"]["import"]) != 52:
        print("ERROR: slots should hold both legs with 52 import half-hours")
        failed = True
    day = attributes["days"].get("2026-11-01")
    if not day or day.get("complete") is not True or not _close(day.get("import_cost"), 480.0) or not _close(day.get("export_cost"), 360.0) or not _close(day.get("standing"), 48.0) or not _close(day.get("import_kwh"), 48.0) or not _close(day.get("export_kwh"), 24.0):
        print("ERROR: unexpected 1 Nov day summary {}".format(day))
        failed = True
    elif not _close(day["buckets"]["Home Low Rate"]["kwh"], 48.0) or not _close(day["buckets"]["Home Low Rate"]["cost"], 480.0):
        print("ERROR: 1 Nov buckets should total Home Low Rate 48 kWh / 480p, got {}".format(day["buckets"]))
        failed = True
    if attributes["days"].get("2026-11-02", {}).get("complete") is not False:
        print("ERROR: 2 Nov should be marked incomplete")
        failed = True
    return failed


def test_sensor_no_data():
    """No cached billing means no state."""
    state, attributes = build_billing_sensor({}, {"import": False, "export": None}, "octopus", datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc), LONDON)
    if state is not None:
        print("ERROR: an empty cache should give state None, got {}".format(state))
        return True
    return False


def test_billing_constants():
    """The window, cadence and entity name are fixed by the design."""
    if billing.BILLING_DAYS != 7 or billing.BILLING_REFRESH_MINUTES != 180 or billing.BILLING_ENTITY_SUFFIX != "billing":
        print("ERROR: unexpected billing constants")
        return True
    return False


def run_billing_tests(my_predbat):
    """Run the billing tests."""
    failed = False
    print("**** Running billing tests ****")
    for test in (
        test_billing_constants,
        test_parse_iog_buckets,
        test_parse_export_generation,
        test_parse_unknown_labels,
        test_parse_bucket_mismatch_drops_slot,
        test_parse_none_vs_empty,
        test_day_slot_starts_clock_change,
        test_days_to_fetch_skips_complete,
        test_merge_prunes_old,
        test_sensor_shape,
        test_sensor_no_data,
    ):
        print("billing: {}".format(test.__name__))
        if test():
            print("ERROR: {} failed".format(test.__name__))
            failed = True
    return failed
