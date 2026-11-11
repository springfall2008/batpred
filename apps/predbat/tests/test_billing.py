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


def test_parse_edf_tou_bucket():
    """EDF import half-hours carry one TOU_BUCKET_COST row with a label but no kWh (shape seen live on an EDF GoElectric account, 6 Nov 2026)."""

    def tou(label, cost):
        """One EDF time-of-use bucket row: a label and a cost, no kWh."""
        return {"type": "TOU_BUCKET_COST", "label": label, "value": None, "costInclTax": {"estimatedAmount": str(cost), "costCurrency": "GBP"}}

    standing = {"type": "STANDING_CHARGE_COST", "label": None, "value": None, "costInclTax": {"estimatedAmount": "1.13", "costCurrency": "GBP"}}
    nodes = [
        _node("2026-11-02T02:00:00+00:00", 3.25, [standing, tou("OFF_PEAK", 21.65)]),
        _node("2026-11-02T17:00:00+00:00", 0.5, [standing, tou("STANDARD_RATE", 13.63)]),
        _node("2026-11-02T18:00:00+00:00", 0.4, [standing, tou("OFF_PEAK", 2.66), tou("STANDARD_RATE", 5.45)]),
    ]
    slots = parse_measurements(_response(nodes)) or {}
    night = slots.get("2026-11-02T02:00:00Z")
    peak = slots.get("2026-11-02T17:00:00Z")
    if not night or night["buckets"] != {"OFF_PEAK": {"kwh": 3.25, "cost": 21.65}} or not _close(night["standing"], 1.13):
        print("ERROR: an EDF off-peak half-hour should be one OFF_PEAK bucket holding the node's 3.25 kWh, got {}".format(night))
        return True
    if not peak or peak["buckets"] != {"STANDARD_RATE": {"kwh": 0.5, "cost": 13.63}}:
        print("ERROR: an EDF peak half-hour should be one STANDARD_RATE bucket holding the node's 0.5 kWh, got {}".format(peak))
        return True
    if "2026-11-02T18:00:00Z" in slots:
        print("ERROR: two kWh-less buckets in one half-hour can't be split, so the half-hour should be dropped")
        return True
    return False


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
    if (
        not day
        or day.get("complete") is not True
        or not _close(day.get("import_cost"), 480.0)
        or not _close(day.get("export_cost"), 360.0)
        or not _close(day.get("standing"), 48.0)
        or not _close(day.get("import_kwh"), 48.0)
        or not _close(day.get("export_kwh"), 24.0)
    ):
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
    if billing.billing_entity_id("predbat") != "sensor.predbat_billing" or billing.billing_entity_id("predbat2") != "sensor.predbat2_billing":
        print("ERROR: the billing sensor should be sensor.<prefix>_billing, got {}".format(billing.billing_entity_id("predbat")))
        return True
    return False


class _FakeStorage:
    """In-memory stand-in for the Storage component."""

    def __init__(self):
        self.data = {}

    async def load(self, module, filename):
        """Return the stored blob, or None."""
        return copy.deepcopy(self.data.get((module, filename)))

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Store a blob."""
        self.data[(module, filename)] = copy.deepcopy(data)
        return True


class _Owner:
    """The bits of a component BillingReader uses: log, storage, local_tz and dashboard_item."""

    def __init__(self, storage=None):
        self.storage = storage
        self.local_tz = "Europe/London"
        self.prefix = "predbat"
        self.logged = []
        self.published = {}

    def log(self, message):
        """Capture a log line."""
        self.logged.append(str(message))

    def dashboard_item(self, entity_id, state=None, attributes=None, app=None):
        """Capture a published entity."""
        self.published[entity_id] = (state, attributes, app)


def _day_response(day, kwh=1.0, cost=10.0, label="Home Low Rate", stat_type="CONSUMPTION_COST"):
    """A measurements response holding every half-hour of local date day."""
    nodes = []
    for start in day_slot_starts(day, LONDON):
        if stat_type == "GENERATION_VALUE":
            stats = [_stat("GENERATION_VALUE", "GENERATION", None, cost), _stat("STANDING_CHARGE_COST", None, None, 0)]
        else:
            stats = [_stat(stat_type, label, kwh, cost), _stat("STANDING_CHARGE_COST", "SC", None, 1.0)]
        nodes.append(_node(start.isoformat(), kwh, stats))
    return _response(nodes)


def _query_day(query):
    """The local date a billing query asks for, from its startAt."""
    start = query.split('startAt: "')[1].split('"')[0]
    return datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone(LONDON).date()


def test_reader_fetches_only_incomplete_days():
    """A refresh asks only for days that are not complete yet, one query per day."""
    import asyncio
    from billing import BillingReader

    now_utc = datetime(2026, 11, 2, 10, 0, tzinfo=LONDON).astimezone(timezone.utc)
    owner = _Owner(_FakeStorage())
    reader = BillingReader(owner, "octopus_user", "billing")
    reader.cache = {"import": _slots_for_day(date(2026, 10, 31))}
    queries = []

    async def query_fn(query, context):
        """Answer every day with a full day of billing."""
        queries.append(query)
        return _day_response(_query_day(query))

    changed = asyncio.run(reader.refresh(query_fn, {"import": ("A-1", "MPAN-IMP"), "export": None}, now_utc))
    asked = sorted(_query_day(query) for query in queries)
    if not changed or date(2026, 10, 31) in asked or date(2026, 11, 1) not in asked or date(2026, 11, 2) not in asked or len(asked) != len(set(asked)):
        print("ERROR: refresh should skip complete 31 Oct and ask once for each other day, asked {}".format(asked))
        return True
    if any("readingDirection: CONSUMPTION" not in query or 'marketSupplyPointId: "MPAN-IMP"' not in query or 'accountNumber: "A-1"' not in query for query in queries):
        print("ERROR: import queries should name the import MPAN, the account and the CONSUMPTION direction")
        return True
    if ("octopus_user", "billing") not in owner.storage.data:
        print("ERROR: refresh should save the cache to storage")
        return True
    return False


def test_reader_failed_read_marks_leg():
    """A failed export read marks the leg False and keeps what was already cached."""
    import asyncio
    from billing import BillingReader

    now_utc = datetime(2026, 11, 2, 10, 0, tzinfo=LONDON).astimezone(timezone.utc)
    reader = BillingReader(_Owner(_FakeStorage()), "octopus_user", "billing")
    cached_export = _slots_for_day(date(2026, 11, 1), label="GENERATION")
    reader.cache = {"export": dict(cached_export)}

    async def query_fn(query, context):
        """Import reads fine, export fails."""
        if "GENERATION" in query:
            return None
        return _day_response(_query_day(query))

    asyncio.run(reader.refresh(query_fn, {"import": ("A-1", "IMP"), "export": ("A-1", "EXP")}, now_utc))
    state, attributes = reader.sensor("octopus", now_utc)
    if attributes["legs"] != {"import": True, "export": False}:
        print("ERROR: legs should be import True, export False, got {}".format(attributes["legs"]))
        return True
    if any(key not in reader.cache.get("export", {}) for key in cached_export):
        print("ERROR: a failed export read should not wipe the cached export")
        return True
    return False


def test_reader_restart_restores_cache():
    """A restart restores the cache and doesn't refetch until it is due."""
    import asyncio
    from billing import BillingReader

    now_utc = datetime(2026, 11, 2, 10, 0, tzinfo=LONDON).astimezone(timezone.utc)
    storage = _FakeStorage()
    first = BillingReader(_Owner(storage), "octopus_user", "billing")

    async def query_fn(query, context):
        """Answer every day with a full day of billing."""
        return _day_response(_query_day(query))

    asyncio.run(first.refresh(query_fn, {"import": ("A-1", "IMP"), "export": None}, now_utc - timedelta(minutes=10)))
    second = BillingReader(_Owner(storage), "octopus_user", "billing")
    asyncio.run(second.load())
    if second.due(now_utc):
        print("ERROR: a reader restored 10 minutes after a refresh should not be due")
        return True
    if not second.due(now_utc + timedelta(minutes=171)):
        print("ERROR: the reader should be due 180 minutes after the last refresh")
        return True
    if second.cache.get("import") != first.cache.get("import") or not second.cache.get("import"):
        print("ERROR: the restored cache should match what was saved")
        return True
    if not BillingReader(_Owner(_FakeStorage()), "octopus_user", "billing").due(now_utc):
        print("ERROR: a reader that has never fetched should be due")
        return True
    return False


def test_reader_random_first_read_delay():
    """With random_delay on, the first read after a start waits for a random moment up to 30 minutes after the first check."""
    from unittest.mock import patch
    from billing import BillingReader

    now_utc = datetime(2026, 11, 2, 10, 0, tzinfo=LONDON).astimezone(timezone.utc)
    reader = BillingReader(_Owner(_FakeStorage()), "octopus_user", "billing")
    reader.random_delay = True
    with patch.object(billing.random, "uniform", return_value=600.0) as uniform:
        if reader.due(now_utc):
            print("ERROR: with random_delay the first read should be held back")
            return True
        if uniform.call_args.args != (0, billing.BILLING_FIRST_READ_DELAY_SECONDS) or billing.BILLING_FIRST_READ_DELAY_SECONDS != 30 * 60:
            print("ERROR: the first read delay should be drawn from 0 to 30 minutes, got {}".format(uniform.call_args.args))
            return True
        if reader.due(now_utc + timedelta(minutes=9)):
            print("ERROR: the first read should still be held back 9 minutes in, before the 10 minute delay")
            return True
        if not reader.due(now_utc + timedelta(minutes=10)):
            print("ERROR: the first read should be due once the random delay has passed")
            return True
        if uniform.call_count != 1:
            print("ERROR: the delay should be picked once per start, picked {} times".format(uniform.call_count))
            return True
    # A reader restored from a recent read waits out the refresh period as before, as well as the delay
    reader.fetched_at = now_utc
    if reader.due(now_utc + timedelta(minutes=10)):
        print("ERROR: a reader read 10 minutes ago should not be due")
        return True
    if not BillingReader(_Owner(_FakeStorage()), "octopus_user", "billing").due(now_utc):
        print("ERROR: without random_delay a reader that has never fetched should be due at once")
        return True
    return False


def test_reader_paces_day_queries():
    """With pace_seconds, each day's query after the first waits a random 0 to pace_seconds; without it none waits."""
    import asyncio
    from unittest.mock import patch
    from billing import BillingReader

    now_utc = datetime(2026, 11, 2, 10, 0, tzinfo=LONDON).astimezone(timezone.utc)
    mpans = {"import": ("A-1", "IMP"), "export": ("A-1", "EXP")}
    events = []

    async def query_fn(query, context):
        """Answer every day with a full day of billing, noting the query."""
        events.append("query")
        return _day_response(_query_day(query))

    async def fake_sleep(seconds):
        """Note the sleep instead of waiting."""
        events.append(seconds)

    with patch.object(billing.asyncio, "sleep", fake_sleep), patch.object(billing.random, "uniform", side_effect=lambda low, high: high / 2):
        asyncio.run(BillingReader(_Owner(_FakeStorage()), "octopus_user", "billing").refresh(query_fn, mpans, now_utc, pace_seconds=billing.BILLING_DAY_PACE_SECONDS))
        queries = events.count("query")
        sleeps = [event for event in events if event != "query"]
        if queries != 2 * (billing.BILLING_DAYS + 1) or len(sleeps) != queries - 1 or events[0] != "query":
            print("ERROR: expected {} queries with a sleep before each but the first, got {}".format(2 * (billing.BILLING_DAYS + 1), events))
            return True
        if any(sleep != billing.BILLING_DAY_PACE_SECONDS / 2 for sleep in sleeps) or billing.BILLING_DAY_PACE_SECONDS != 60:
            print("ERROR: each sleep should be drawn from 0 to 60 seconds, got {}".format(sleeps))
            return True
        events.clear()
        asyncio.run(BillingReader(_Owner(_FakeStorage()), "octopus_user", "billing").refresh(query_fn, mpans, now_utc))
        if any(event != "query" for event in events):
            print("ERROR: a refresh without pacing should not sleep, got {}".format(events))
            return True
    return False


def test_reader_background_read():
    """With random_delay on, read() runs the paced refresh in the background, isn't due again meanwhile, and publishes when done."""
    import asyncio
    from unittest.mock import patch
    from billing import BillingReader

    now_utc = datetime(2026, 11, 2, 10, 0, tzinfo=LONDON).astimezone(timezone.utc)
    owner = _Owner(_FakeStorage())
    reader = BillingReader(owner, "octopus_user", "billing")
    reader.random_delay = True
    reader.first_read_after = now_utc
    release = None

    async def query_fn(query, context):
        """Hold the first query until released, then answer every day with a full day of billing."""
        await release.wait()
        return _day_response(_query_day(query))

    async def scenario():
        """Start a background read, check it is running, release it and wait for it."""
        nonlocal release
        release = asyncio.Event()
        await reader.read(query_fn, {"import": ("A-1", "IMP"), "export": None}, now_utc, "octopus", "octopus")
        await asyncio.sleep(0)
        if not reader.running() or reader.due(now_utc + timedelta(hours=4)):
            return "the read should be running in the background and not due again while it runs"
        if owner.published:
            return "nothing should be published before the background read finishes"
        release.set()
        await reader.task
        if reader.running() or billing.billing_entity_id("predbat") not in owner.published:
            return "the background read should publish the billing when it finishes"
        return None

    async def cancelled():
        """A stop() while the background read waits cancels it."""
        nonlocal release
        release = asyncio.Event()
        stopping = BillingReader(_Owner(_FakeStorage()), "octopus_user", "billing")
        stopping.random_delay = True
        stopping.first_read_after = now_utc
        await stopping.read(query_fn, {"import": ("A-1", "IMP"), "export": None}, now_utc, "octopus", "octopus")
        await asyncio.sleep(0)
        stopping.stop()
        try:
            await stopping.task
        except asyncio.CancelledError:
            pass
        return None if stopping.task.cancelled() else "stop() should cancel the background read"

    with patch.object(billing.random, "uniform", return_value=0.0):
        for check in (scenario, cancelled):
            error = asyncio.run(check())
            if error:
                print("ERROR: " + error)
                return True
    return False


def test_kraken_run_random_delay_reads_in_background():
    """With plan_random_delay set, KrakenAPI.run() starts the billing read in the background and it publishes when done."""
    import asyncio
    from unittest.mock import MagicMock, patch

    api = _kraken_api_for_billing(provider="eon")
    api.import_mpan = "3333333333333"
    api.base.get_arg = MagicMock(side_effect=lambda arg, default=None, **kwargs: 30 if arg == "plan_random_delay" else default)

    async def scenario():
        """Run one cycle, then wait for the background read it started."""
        await api.run(60, False)
        if not api.billing.task:
            return "run() should start a background billing read with plan_random_delay set"
        await api.billing.task
        return None

    # A zero draw makes both the first read delay and the pacing sleeps nothing, so the read runs straight through
    with patch.object(billing.random, "uniform", return_value=0.0):
        error = asyncio.run(scenario())
    if error:
        print("ERROR: " + error)
        return True
    if not api.billing.random_delay or not _billing_publishes(api):
        print("ERROR: the background billing read should publish sensor.predbat_billing")
        return True
    return False


def test_octopus_partial_errors_is_failure(my_predbat):
    """A response with data and errors (e.g. KT-CT-4718 on one MPAN) is a failed read, not an empty day."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock
    from octopus import OctopusAPI, integration_context_header

    api = OctopusAPI(my_predbat, key="test-api-key", account_id="test-account", automatic=False)
    body = json.dumps({"data": {"account": {"properties": [{"measurements": {"edges": []}}]}}, "errors": [{"message": "Not authorised", "extensions": {"errorCode": "KT-CT-4718"}}]})
    response = MagicMock()
    response.status = 200
    response.text = AsyncMock(return_value=body)
    response.headers = {}
    response.request_info = MagicMock()
    response.request_info.headers = {integration_context_header: "test-context"}
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=None)
    session = MagicMock()
    session.post = MagicMock(return_value=response)
    api.api.async_create_client_session = AsyncMock(return_value=session)
    api.graphql_token = "token"
    api.async_refresh_token = AsyncMock(return_value="token")
    api.log = lambda message: None
    data = asyncio.run(api.async_graphql_query("query { a }", "billing-import"))
    if parse_measurements(data) is not None:
        print("ERROR: a partial-error response should be a failed read (None), got {}".format(data))
        return True
    return False


def test_octopus_run_publishes_billing(my_predbat):
    """OctopusAPI.run() reads billing for the modelled import MPAN only and publishes sensor.predbat_billing."""
    import asyncio
    from unittest.mock import AsyncMock

    from octopus import OctopusAPI

    api = OctopusAPI(my_predbat, key="test-key", account_id="A-TEST", automatic=False)
    api.async_get_account = AsyncMock(return_value=True)
    api.async_find_tariffs = AsyncMock(return_value={})
    # A Weekend Happy Hour joined yesterday 10:00-11:00 UTC, whose two billed half-hours (10p each) Octopus credits today
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    happy_hour = {"eventId": 1, "eventType": "WEEKEND_HAPPY_HOUR", "startAt": yesterday.isoformat(), "endAt": (yesterday + timedelta(hours=1)).isoformat()}
    api.async_get_flexibility_events = AsyncMock(return_value={"events": [], "account": {"joinedEvents": [happy_hour]}})
    api.get_saving_session_data = lambda: None
    api.fetch_tariffs = AsyncMock(return_value=None)
    api.async_update_intelligent_devices = AsyncMock(return_value=None)
    api.async_intelligent_update_sensor = AsyncMock(return_value=None)
    api.save_octopus_cache = AsyncMock(return_value=None)
    api.refresh_discovery = lambda: None
    api.process_commands = AsyncMock(return_value=False)
    api.tariffs = {"import": {"tariffCode": "E-1R-IOG-SMB-VAR-24-10-29-L", "mpan": "1111111111111"}, "export": {"tariffCode": "E-1R-AGILE-OUTGOING-19-05-13-L", "mpan": "2222222222222"}}
    queries = []

    async def fake_query(query, context, **kwargs):
        """Answer billing queries with a full day; nothing else is expected to reach the API."""
        queries.append(query)
        if "transactions" in query:
            return _credits_response([_credit_node("1", datetime.now(timezone.utc).date().isoformat(), 19)])
        if "GENERATION" in query:
            return _day_response(_query_day(query), kwh=0.5, cost=7.5, stat_type="GENERATION_VALUE")
        return _day_response(_query_day(query))

    api.async_graphql_query = fake_query
    # coverage/apps.yaml sets plan_random_delay, which would hold the read back and run it in the background
    had_delay = "plan_random_delay" in my_predbat.args
    saved_delay = my_predbat.args.get("plan_random_delay")
    my_predbat.args["plan_random_delay"] = 0
    try:
        asyncio.run(api.run(60, False))
    finally:
        if had_delay:
            my_predbat.args["plan_random_delay"] = saved_delay
        else:
            my_predbat.args.pop("plan_random_delay", None)
    billing_queries = [query for query in queries if "measurements" in query]
    mpans = {query.split('marketSupplyPointId: "')[1].split('"')[0] for query in billing_queries}
    if mpans != {"1111111111111", "2222222222222"}:
        print("ERROR: billing should query exactly the modelled import and export MPANs, got {}".format(mpans))
        return True
    state = my_predbat.get_state_wrapper("sensor.predbat_billing")
    legs = my_predbat.get_state_wrapper("sensor.predbat_billing", attribute="legs")
    if state is None or legs != {"import": True, "export": True}:
        print("ERROR: sensor.predbat_billing should be published with both legs, got state {} legs {}".format(state, legs))
        return True
    if not any('accountNumber: "A-TEST"' in query and "transactions" in query for query in queries):
        print("ERROR: billing should read the account's credits")
        return True
    events = my_predbat.get_state_wrapper("sensor.predbat_billing", attribute="credit_events") or []
    if len(events) != 1 or events[0].get("credit") != 19 or events[0].get("billed") != 20.0:
        print("ERROR: the joined Happy Hour should be published with its 19p credit against 20p billed, got {}".format(events))
        return True
    return False


def _kraken_api_for_billing(**kwargs):
    """A KrakenAPI with a discovered tariff, fresh rates, fake storage and recorded queries."""
    from unittest.mock import AsyncMock, MagicMock
    from tests.test_kraken import make_kraken_api, attach_fake_storage

    api = make_kraken_api(**kwargs)
    attach_fake_storage(api)
    api.current_tariff = {"tariff_code": "E-1R-NEXT-FLEX-01", "product_code": "NEXT-FLEX-01"}
    api.tariff_fetched_at = datetime.now()
    api.rates_fetched_at = datetime.now()
    api.async_discover_smart_devices = AsyncMock()
    api.dashboard_item = MagicMock()
    api.set_arg = MagicMock()
    api.update_success_timestamp = MagicMock()
    api.queries = []

    async def fake_query(query, context, **kw):
        """Answer billing queries with a full single-bucket day."""
        api.queries.append(query)
        if "GENERATION" in query:
            return _day_response(_query_day(query), kwh=0.5, cost=7.5, stat_type="GENERATION_VALUE")
        return _day_response(_query_day(query), kwh=1.0, cost=6.571, label="CONSUMPTION_CHARGE_STANDARD_B")

    api.async_graphql_query = fake_query
    return api


def _billing_publishes(api):
    """The sensor.predbat_billing calls a mocked dashboard_item received."""
    return [call for call in api.dashboard_item.call_args_list if call.args and call.args[0] == "sensor.predbat_billing"]


def test_kraken_run_publishes_billing():
    """KrakenAPI.run() reads the import MPAN's billing and publishes sensor.predbat_billing for the supplier."""
    import asyncio

    api = _kraken_api_for_billing(provider="eon")
    api.import_mpan = "3333333333333"
    asyncio.run(api.run(60, False))
    calls = _billing_publishes(api)
    if not calls:
        print("ERROR: KrakenAPI.run() should publish sensor.predbat_billing")
        return True
    attributes = calls[-1].kwargs.get("attributes") or {}
    if attributes.get("supplier") != "eon" or calls[-1].kwargs.get("app") != "kraken":
        print("ERROR: billing should be published for supplier eon by app kraken, got {} / {}".format(attributes.get("supplier"), calls[-1].kwargs.get("app")))
        return True
    slots = attributes.get("slots", {}).get("import", {})
    if not slots or any(list(slot["buckets"]) != ["CONSUMPTION_CHARGE_STANDARD_B"] for slot in slots.values()):
        print("ERROR: every E.ON slot should carry its one bucket")
        return True
    return False


def test_kraken_export_uses_export_account():
    """Export billing is read on the export account and MPAN, in the GENERATION direction."""
    import asyncio

    api = _kraken_api_for_billing(provider="edf", export_account_id="A-EXP", export_mpan="X")
    api.import_mpan = "3333333333333"
    asyncio.run(api.run(60, False))
    exports = [query for query in api.queries if "readingDirection: GENERATION" in query]
    if not exports or any('accountNumber: "A-EXP"' not in query or 'marketSupplyPointId: "X"' not in query for query in exports):
        print("ERROR: export billing should be read on account A-EXP, MPAN X")
        return True
    imports = [query for query in api.queries if "readingDirection: CONSUMPTION" in query]
    if not imports or any('accountNumber: "A-TEST123"' not in query for query in imports):
        print("ERROR: import billing should be read on the import account")
        return True
    return False


def test_kraken_no_mpan_skips():
    """With no import MPAN known yet, no billing is read or published."""
    import asyncio

    api = _kraken_api_for_billing(provider="eon")
    api.import_mpan = None
    asyncio.run(api.run(60, False))
    if any("measurements" in query for query in api.queries) or _billing_publishes(api):
        print("ERROR: no billing should be read or published without an import MPAN")
        return True
    return False


def _billing_attrs(import_slots, export_slots=None, legs=None, supplier="octopus"):
    """A sensor.predbat_billing attributes dict holding the given slots."""
    slots = {"import": import_slots}
    if export_slots is not None:
        slots["export"] = export_slots
    complete = sorted(import_slots)[-1] if import_slots else None
    return {"supplier": supplier, "legs": legs or {"import": True, "export": None}, "complete_until": complete, "slots": slots, "days": {}}


def _slot(kwh, cost, label="Home Low Rate", standing=1.15674):
    """One parsed billing slot."""
    return {"kwh": kwh, "cost": cost, "standing": standing, "buckets": {label: {"kwh": kwh, "cost": cost}}}


def _plan(slot_minutes):
    """A plan JSON with one row per slot minute."""
    return {"rows": [{"time": "x", "slot_minute": minute, "cost_change": 0.0} for minute in slot_minutes]}


VIEW_MIDNIGHT = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)


def _key(minute, midnight=VIEW_MIDNIGHT):
    """The billing slot key for a minute after the view's midnight."""
    return (midnight + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_attach_basic():
    """Billing that matches Predbat's cost gives a zero Diff on every row and in the summary."""
    from billing import attach_billing

    billing = _billing_attrs({_key(m): _slot(1.0, 10.0) for m in (600, 630)})
    plan = attach_billing(_plan([600, 630]), billing, VIEW_MIDNIGHT, 30, {600: 10.0, 630: 10.0}, 55.52, {}, False, {600: 1.0, 630: 1.0, 660: 5.0}, {600: 0.0})
    rows = plan["rows"]
    if any(row.get("billed") != 0.1 or row.get("billing_diff") != 0.0 for row in rows):
        print("ERROR: matching billing should give billed 0.1 and diff 0.0, got {}".format(rows))
        return True
    summary = plan.get("billing_summary") or {}
    if summary.get("diff") != 0.0 or summary.get("billed_cost") != 0.2 or summary.get("predbat_cost") != 0.2 or summary.get("supplier") != "octopus" or summary.get("import_only") is not False:
        print("ERROR: unexpected summary {}".format(summary))
        return True
    if summary.get("import_kwh") != [2.0, 2.0] or rows[0].get("billed_buckets", {}).get("Home Low Rate", {}).get("cost") != 0.1:
        print("ERROR: summary kWh / row buckets wrong: {} {}".format(summary.get("import_kwh"), rows[0].get("billed_buckets")))
        return True
    return False


def test_attach_unpublished_row():
    """A row Octopus hasn't published yet shows no billing and is left out of the summary."""
    from billing import attach_billing

    billing = _billing_attrs({_key(600): _slot(1.0, 10.0)})
    plan = attach_billing(_plan([600, 630]), billing, VIEW_MIDNIGHT, 30, {600: 10.0, 630: 99.0}, 55.52, {}, False, None, None)
    late = plan["rows"][1]
    if late.get("billed") is not None or late.get("billing_diff") is not None or late.get("billed_buckets") is not None:
        print("ERROR: the unpublished row should have billed/diff/buckets None, got {}".format(late))
        return True
    if plan["billing_summary"]["predbat_cost"] != 0.1:
        print("ERROR: the summary's Predbat cost should leave the unpublished row out, got {}".format(plan["billing_summary"]["predbat_cost"]))
        return True
    return False


def test_attach_hour_rows():
    """An hour-long row sums its two half-hours, and shows nothing unless both are published."""
    from billing import attach_billing

    billing = _billing_attrs({_key(600): _slot(1.0, 10.0), _key(630): _slot(1.0, 5.0), _key(660): _slot(1.0, 7.0)})
    plan = attach_billing(_plan([600, 660]), billing, VIEW_MIDNIGHT, 60, {600: 15.0, 660: 7.0}, 55.52, {}, False, None, None)
    if plan["rows"][0].get("billed") != 0.15 or plan["rows"][0].get("billing_diff") != 0.0:
        print("ERROR: the 10:00 hour should bill 0.15 with diff 0, got {}".format(plan["rows"][0]))
        return True
    if plan["rows"][1].get("billed") is not None:
        print("ERROR: the 11:00 hour has only one half-hour published and should show no billing, got {}".format(plan["rows"][1]))
        return True
    return False


def test_attach_standing_stripped():
    """Row costs come in without the standing charge; both standing charges are reported once per billed day."""
    from billing import attach_billing

    billing = _billing_attrs({_key(0): _slot(1.0, 10.0)})
    plan = attach_billing(_plan([0]), billing, VIEW_MIDNIGHT, 30, {0: 10.0}, 55.52, {}, False, None, None)
    summary = plan["billing_summary"]
    if plan["rows"][0].get("billing_diff") != 0.0 or summary.get("standing_predbat") != 0.01 or summary.get("standing_billed") != 0.01:
        print("ERROR: midnight standing should be stripped from Diff and shown separately, got row {} summary {}".format(plan["rows"][0], summary))
        return True
    return False


def test_attach_clock_change():
    """Rows map to billing by UTC instant, so the clock-change day lines up."""
    from billing import attach_billing

    midnight = datetime(2026, 10, 25, 0, 0, tzinfo=LONDON).astimezone(timezone.utc)
    billing = _billing_attrs({"2026-10-25T01:00:00Z": _slot(1.0, 6.6)})
    plan = attach_billing(_plan([120]), billing, midnight, 30, {120: 6.6}, 55.52, {}, False, None, None)
    if plan["rows"][0].get("billed") != 0.07:
        print("ERROR: slot_minute 120 on 25 Oct should pick up 2026-10-25T01:00:00Z, got {}".format(plan["rows"][0]))
        return True
    return False


def test_attach_event_label():
    """Rows priced with an event the bill won't carry are labelled with it."""
    from billing import attach_billing

    billing = _billing_attrs({_key(m): _slot(1.0, 10.0) for m in (600, 630, 660, 690)})
    events = {605: "saving", 630: "manual", 665: "increment", 690: "future"}
    plan = attach_billing(_plan([600, 630, 660, 690]), billing, VIEW_MIDNIGHT, 30, {m: 10.0 for m in (600, 630, 660, 690)}, 55.52, events, False, None, None)
    labels = [row.get("billing_event") for row in plan["rows"]]
    if labels != ["Saving event", "Manual rate", "Rate adjustment", None]:
        print("ERROR: unexpected event labels {}".format(labels))
        return True
    return False


def test_attach_import_only():
    """When export billing can't be read, Billed and Diff are import-only and say so."""
    from billing import attach_billing

    billing = _billing_attrs({_key(600): _slot(1.0, 10.0)}, export_slots={_key(600): _slot(0.5, 7.5, label="GENERATION", standing=0.0)}, legs={"import": True, "export": False})
    plan = attach_billing(_plan([600]), billing, VIEW_MIDNIGHT, 30, {600: 10.0}, 55.52, {}, True, None, None)
    if plan["rows"][0].get("billed") != 0.1 or plan["billing_summary"].get("import_only") is not True:
        print("ERROR: import-only billing should ignore export and be flagged, got {} {}".format(plan["rows"][0], plan["billing_summary"]))
        return True
    midnight = attach_billing(_plan([0]), _billing_attrs({_key(0): _slot(1.0, 10.0)}, legs={"import": True, "export": False}), VIEW_MIDNIGHT, 30, {0: 10.0}, 55.52, {}, True, None, None)
    if midnight["rows"][0].get("billing_diff") != 0.0 or midnight["billing_summary"].get("standing_predbat") != 0.01:
        print("ERROR: import-only cost has no standing charge in it, so nothing should be taken off the midnight row, got {} {}".format(midnight["rows"][0], midnight["billing_summary"]))
        return True
    net = attach_billing(_plan([600]), dict(billing, legs={"import": True, "export": True}), VIEW_MIDNIGHT, 30, {600: 2.5}, 55.52, {}, False, None, None)
    if net["rows"][0].get("billed") != 0.03 or net["rows"][0].get("billing_diff") != 0.0:
        print("ERROR: net billing should be import less export (2.5p), got {}".format(net["rows"][0]))
        return True
    return False


def test_attach_no_billing():
    """No billing leaves the plan exactly as it was."""
    from billing import attach_billing

    plan = _plan([600])
    before = copy.deepcopy(plan)
    attach_billing(plan, {}, VIEW_MIDNIGHT, 30, {600: 10.0}, 55.52, {}, False, None, None)
    attach_billing(plan, None, VIEW_MIDNIGHT, 30, {600: 10.0}, 55.52, {}, False, None, None)
    if plan != before:
        print("ERROR: empty billing should leave the plan untouched, got {}".format(plan))
        return True
    return False


def test_parse_missing_cost_rejects_slot():
    """A bucket with kWh but no cost isn't priced yet; the half-hour is left out so the day is read again."""
    nodes = [_node("2026-11-01T10:00:00+00:00", 1.0, [{"type": "CONSUMPTION_COST", "label": "A", "value": "1.0", "costInclTax": {"estimatedAmount": None}}]), _node("2026-11-01T10:30:00+00:00", 1.0, [_stat("CONSUMPTION_COST", "A", 1.0, 5.0)])]
    slots = parse_measurements(_response(nodes))
    if slots is None or "2026-11-01T10:00:00Z" in slots or "2026-11-01T10:30:00Z" not in slots:
        print("ERROR: a bucket with kWh and no cost should drop its half-hour, got {}".format(None if slots is None else sorted(slots)))
        return True
    return False


def test_attach_sub_half_hour_interval_skipped():
    """15-minute plan rows can't be matched to half-hourly billing, so none is attached."""
    from billing import attach_billing

    plan = _plan([600, 615])
    before = copy.deepcopy(plan)
    attach_billing(plan, _billing_attrs({_key(600): _slot(1.0, 10.0)}), VIEW_MIDNIGHT, 15, {600: 5.0, 615: 5.0}, 55.52, {}, False, None, None)
    if plan != before:
        print("ERROR: 15-minute rows should get no billing, got {}".format(plan))
        return True
    return False


def test_attach_standing_pro_rata():
    """On a part-billed day Predbat's standing charge is counted for the billed half-hours only, so the tile doesn't look like a mismatch."""
    from billing import attach_billing

    billing = _billing_attrs({_key(m): _slot(1.0, 10.0, standing=1.15674) for m in range(0, 720, 30)})
    plan = attach_billing(_plan(list(range(0, 1440, 30))), billing, VIEW_MIDNIGHT, 30, {m: 10.0 for m in range(0, 1440, 30)}, 55.52, {}, False, None, None)
    summary = plan["billing_summary"]
    if summary["standing_predbat"] != summary["standing_billed"] or summary["standing_billed"] != 0.28:
        print("ERROR: half a day billed should show £0.28 standing on both sides, got {} vs {}".format(summary["standing_predbat"], summary["standing_billed"]))
        return True
    # 25 Oct has 50 half-hours and Octopus spreads one day's charge over them (1.11047p each in the live fixture), so a 1/48 share would show £0.58 against £0.56
    midnight = datetime(2026, 10, 25, 0, 0, tzinfo=LONDON).astimezone(timezone.utc)
    billing = _billing_attrs({_key(m, midnight): _slot(1.0, 10.0, standing=1.11047) for m in range(0, 1500, 30)})
    plan = attach_billing(_plan(list(range(0, 1500, 30))), billing, midnight, 30, {m: 10.0 for m in range(0, 1500, 30)}, 55.52, {}, False, None, None)
    summary = plan["billing_summary"]
    if summary["standing_predbat"] != summary["standing_billed"] or summary["standing_billed"] != 0.56:
        print("ERROR: a whole 25-hour day billed should show £0.56 standing on both sides, got {} vs {}".format(summary["standing_predbat"], summary["standing_billed"]))
        return True
    return False


def test_attach_hides_diff_when_predbat_saw_nothing():
    """A row where Predbat recorded no cost and no import but the meter shows energy (Predbat down, recorder gap) gets no Diff."""
    from billing import attach_billing

    billing = _billing_attrs({_key(600): _slot(1.0, 10.0), _key(630): _slot(1.0, 10.0)})
    plan = attach_billing(_plan([600, 630]), billing, VIEW_MIDNIGHT, 30, {600: 0.0, 630: 10.0}, 55.52, {}, False, {600: 0.0, 630: 1.0}, None)
    if plan["rows"][0].get("billed") != 0.1 or plan["rows"][0].get("billing_diff") is not None or plan["rows"][1].get("billing_diff") != 0.0:
        print("ERROR: the row Predbat recorded nothing for should show Billed but no Diff, got {}".format(plan["rows"]))
        return True
    if plan["billing_summary"]["predbat_cost"] != 0.1 or plan["billing_summary"]["billed_cost"] != 0.1:
        print("ERROR: the summary should leave the unrecorded row out of both sides, got {}".format(plan["billing_summary"]))
        return True
    return False


def test_attach_implausible_rate_hidden():
    """Billing whose average rate is nowhere near Predbat's (a units mix-up, say) is not shown: the columns stay, empty, with no summary."""
    from billing import attach_billing

    plan = {"rows": [{"slot_minute": m, "cost_change": 0.0, "import_rate": 25.0} for m in (600, 630)]}
    billing = _billing_attrs({_key(600): _slot(1.0, 2500.0), _key(630): _slot(1.0, 2500.0)})
    attach_billing(plan, billing, VIEW_MIDNIGHT, 30, {600: 25.0, 630: 25.0}, 55.52, {}, False, None, None)
    if "billing_summary" in plan or any(row.get("billed") is not None or row.get("billing_diff") is not None or row.get("billed_buckets") is not None or "billed" not in row for row in plan["rows"]):
        print("ERROR: billing at 100x Predbat's rate should leave the billing columns empty with no summary, got {}".format(plan))
        return True
    plan = {"rows": [{"slot_minute": m, "cost_change": 0.0, "import_rate": 36.28} for m in (600, 630)]}
    attach_billing(plan, _billing_attrs({_key(600): _slot(1.0, 6.6), _key(630): _slot(1.0, 36.28)}), VIEW_MIDNIGHT, 30, {600: 36.28, 630: 36.28}, 55.52, {}, False, None, None)
    if plan["rows"][0].get("billed") != 0.07:
        print("ERROR: one slot billed cheap against a peak rate is a real difference and must still show")
        return True
    return False


def test_reader_cache_tied_to_meter():
    """The saved cache records which meters it came from, and is dropped when the meter changes."""
    import asyncio
    from billing import BillingReader

    now_utc = datetime(2026, 11, 2, 10, 0, tzinfo=LONDON).astimezone(timezone.utc)
    storage = _FakeStorage()
    first = BillingReader(_Owner(storage), "octopus_user", "billing")

    async def old_meter(query, context):
        """A full day of 10p half-hours."""
        return _day_response(_query_day(query), cost=10.0)

    asyncio.run(first.refresh(old_meter, {"import": ("A-1", "IMP"), "export": None}, now_utc))
    saved = storage.data.get(("octopus_user", "billing")) or {}
    if saved.get("mpans") != {"import": "IMP", "export": None}:
        print("ERROR: the saved billing cache should record its meters, got {}".format(saved.get("mpans")))
        return True
    second = BillingReader(_Owner(storage), "octopus_user", "billing")
    asyncio.run(second.load())

    async def new_meter(query, context):
        """Nothing published yet on the new meter."""
        return _response([])

    asyncio.run(second.refresh(new_meter, {"import": ("A-1", "IMP2"), "export": None}, now_utc + timedelta(hours=4)))
    if second.cache.get("import") or (storage.data.get(("octopus_user", "billing")) or {}).get("mpans") != {"import": "IMP2", "export": None}:
        print("ERROR: a new meter should drop the old meter's billing and save the new meters")
        return True
    return False


def test_reader_settles_old_days():
    """A day that never completes (here an export meter that publishes nothing) is read once it is BILLING_SETTLE_DAYS old and then left alone, across a restart too."""
    import asyncio
    from billing import BillingReader, BILLING_SETTLE_DAYS

    now_utc = datetime(2026, 11, 2, 10, 0, tzinfo=LONDON).astimezone(timezone.utc)
    storage = _FakeStorage()
    owner = _Owner(storage)
    reader = BillingReader(owner, "octopus_user", "billing")
    mpans = {"import": ("A-1", "IMP"), "export": ("A-1", "EXP1234")}
    asked = []

    async def query_fn(query, context):
        """Import publishes every half-hour; export publishes nothing at all."""
        if "GENERATION" in query:
            asked.append(_query_day(query))
            return _response([])
        return _day_response(_query_day(query))

    def warnings():
        """The export days logged as given up on."""
        return [line for line in owner.logged if "won't be read again" in line and "export" in line]

    asyncio.run(reader.refresh(query_fn, mpans, now_utc))
    settle_by = date(2026, 11, 2) - timedelta(days=BILLING_SETTLE_DAYS)
    if BILLING_SETTLE_DAYS != 5:
        print("ERROR: export billing can lag days behind import, so a day should be read until it is 5 days old, got {}".format(BILLING_SETTLE_DAYS))
        return True
    if sorted(asked) != [date(2026, 10, 26) + timedelta(days=offset) for offset in range(8)] or len(warnings()) != 3:
        print("ERROR: the first refresh should ask for every export day and give up on the 3 up to {}, asked {} warned {}".format(settle_by, sorted(asked), warnings()))
        return True
    if reader.settled["export"] != ["2026-10-26", "2026-10-27", "2026-10-28"] or reader.legs["export"] is not True:
        print("ERROR: 26-28 Oct should be settled with the export leg still read OK, got {} {}".format(reader.settled, reader.legs))
        return True

    asked.clear()
    asyncio.run(reader.refresh(query_fn, mpans, now_utc + timedelta(hours=3)))
    if sorted(asked) != [date(2026, 10, 29), date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 2)] or len(warnings()) != 3:
        print("ERROR: a later refresh should only re-read the export days not yet settled, asked {}".format(sorted(asked)))
        return True

    restarted_owner = _Owner(storage)
    restarted = BillingReader(restarted_owner, "octopus_user", "billing")
    asyncio.run(restarted.load())
    asked.clear()
    asyncio.run(restarted.refresh(query_fn, mpans, datetime(2026, 11, 3, 10, 0, tzinfo=LONDON).astimezone(timezone.utc)))
    if sorted(asked) != [date(2026, 10, 29), date(2026, 10, 30), date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)]:
        print("ERROR: after a restart the settled days should stay settled, and 29 Oct is read once more on 3 Nov, asked {}".format(sorted(asked)))
        return True
    if restarted.settled["export"] != ["2026-10-27", "2026-10-28", "2026-10-29"]:
        print("ERROR: 26 Oct should leave the settled list with the window and 29 Oct join it, got {}".format(restarted.settled["export"]))
        return True
    if any("import" in line and "won't be read again" in line for line in owner.logged + restarted_owner.logged):
        print("ERROR: complete import days are settled quietly")
        return True
    return False


def test_sensor_needs_export_when_configured():
    """With an export meter whose read failed, a day isn't complete on import alone, so the state never quietly becomes import-only."""
    now_utc = datetime(2026, 11, 2, 10, 0, tzinfo=LONDON).astimezone(timezone.utc)
    cache = {"import": _slots_for_day(date(2026, 11, 1))}
    state, attributes = build_billing_sensor(cache, {"import": True, "export": False}, "octopus", now_utc, LONDON)
    if state is not None or attributes["days"]["2026-11-01"]["complete"] is not False:
        print("ERROR: a failed export read should leave the day incomplete and no state, got {} {}".format(state, attributes["days"]["2026-11-01"]["complete"]))
        return True
    return False


def test_attach_export_pending():
    """With an export meter, a row whose import is billed but whose export isn't yet shows the import charge, flagged, with no Diff and outside the summary."""
    from billing import attach_billing

    legs = {"import": True, "export": True}
    imports = {_key(m): _slot(1.0, 10.0) for m in (600, 630)}
    exports = {_key(600): _slot(0.5, 7.5, label="GENERATION", standing=0.0)}
    plan = attach_billing(_plan([600, 630]), _billing_attrs(imports, export_slots=exports, legs=legs), VIEW_MIDNIGHT, 30, {600: 2.5, 630: 10.0}, 55.52, {}, False, None, None)
    full, pending = plan["rows"]
    if full.get("billed") != 0.03 or full.get("billing_diff") != 0.0 or full.get("billing_pending_export") is not False:
        print("ERROR: the fully billed row should show the net 2.5p with a zero Diff, got {}".format(full))
        return True
    if pending.get("billed") != 0.1 or pending.get("billing_diff") is not None or pending.get("billing_pending_export") is not True or list(pending.get("billed_buckets") or {}) != ["Home Low Rate"]:
        print("ERROR: the row waiting for its export should show its 10p import, flagged, with no Diff, got {}".format(pending))
        return True
    summary = plan["billing_summary"]
    if summary["billed_cost"] != 0.03 or summary["predbat_cost"] != 0.03 or summary["export_pending_slots"] != 1 or summary["import_kwh"][1] != 1.0 or summary["standing_billed"] != 0.01:
        print("ERROR: the summary should count only the fully billed row and say one is waiting for export, got {}".format(summary))
        return True
    # A row waiting for its export before a fully billed one shows its import, so it is counted as pending, not missing
    plan = attach_billing(_plan([600, 630]), _billing_attrs(imports, export_slots={_key(630): _slot(0.5, 7.5, label="GENERATION", standing=0.0)}, legs=legs), VIEW_MIDNIGHT, 30, {600: 10.0, 630: 2.5}, 55.52, {}, False, None, None)
    summary = plan.get("billing_summary") or {}
    if summary.get("missing_slots") != 0 or summary.get("export_pending_slots") != 1 or summary.get("billed_until") != _key(660):
        print("ERROR: a pending row before the last billed one is pending, not missing, got {}".format(summary))
        return True
    # No export at all yet: every row shows its import, flagged, and there is no summary
    plan = attach_billing(_plan([600, 630]), _billing_attrs(imports, export_slots={}, legs=legs), VIEW_MIDNIGHT, 30, {600: 2.5, 630: 10.0}, 55.52, {}, False, None, None)
    if "billing_summary" in plan or [row.get("billing_pending_export") for row in plan["rows"]] != [True, True] or [row.get("billed") for row in plan["rows"]] != [0.1, 0.1]:
        print("ERROR: with no export billed yet every row should show its import flagged and no summary, got {}".format(plan))
        return True
    return False


def test_attach_columns_when_nothing_billed():
    """A view with billing configured but none of it inside the view (today, say) still gets the billing fields, empty, so the columns show."""
    from billing import attach_billing

    billing = _billing_attrs({_key(600, VIEW_MIDNIGHT - timedelta(days=3)): _slot(1.0, 10.0)})
    plan = attach_billing(_plan([600, 630]), billing, VIEW_MIDNIGHT, 30, {600: 10.0, 630: 10.0}, 55.52, {610: "saving"}, False, None, None)
    if "billing_summary" in plan or any("billed" not in row or row["billed"] is not None or row["billing_diff"] is not None for row in plan["rows"]):
        print("ERROR: a view with nothing billed should carry empty billing fields and no summary, got {}".format(plan))
        return True
    if plan["rows"][0].get("billing_event") != "Saving event":
        print("ERROR: a row with nothing billed should still carry its event label, got {}".format(plan["rows"][0]))
        return True
    return False


def test_attach_shows_gaps():
    """The summary says how far billing reaches and how many half-hours before that are missing."""
    from billing import attach_billing

    billing = _billing_attrs({_key(m): _slot(1.0, 10.0) for m in (0, 30, 90, 120)})
    plan = attach_billing(_plan([0, 30, 60, 90, 120, 150]), billing, VIEW_MIDNIGHT, 30, {m: 10.0 for m in range(0, 180, 30)}, 55.52, {}, False, None, None)
    summary = plan["billing_summary"]
    if summary.get("billed_until") != _key(150) or summary.get("missing_slots") != 1:
        print("ERROR: billing reaches 02:30 with the 01:00 half-hour missing, got {} / {}".format(summary.get("billed_until"), summary.get("missing_slots")))
        return True
    return False


def _credit_node(transaction_id, posted, amount, title="Weekend Happy Hour", typename="Credit"):
    """One account transaction node the way the transactions query returns it."""
    return {"__typename": typename, "id": transaction_id, "postedDate": posted, "createdAt": posted + "T16:00:00+00:00", "title": title, "amounts": {"gross": amount}}


def _credits_response(nodes):
    """Wrap transaction nodes in the transactions response envelope."""
    return {"account": {"transactions": {"edges": [{"node": node} for node in nodes]}}}


# Sunday 8 Nov 2026 on a live Octopus account: two Weekend Happy Hours billed at the full rate (08:00-09:00 and
# 11:00-12:00 UTC: 88.7p + 92.2p and 92.7p + 75.9p), refunded by two credits posted on Tuesday 10 Nov
HAPPY_SUNDAY = {"2026-11-08T08:00:00Z": 88.7, "2026-11-08T08:30:00Z": 92.2, "2026-11-08T11:00:00Z": 92.7, "2026-11-08T11:30:00Z": 75.9}
HAPPY_EVENTS = [{"start": "2026-11-08T08:00:00Z", "end": "2026-11-08T09:00:00Z"}, {"start": "2026-11-08T11:00:00Z", "end": "2026-11-08T12:00:00Z"}]


def test_parse_credits():
    """Only Happy Hour credits posted within BILLING_CREDIT_DAYS are kept, oldest first; a failed read is None."""
    from billing import parse_credits

    now_utc = datetime(2026, 11, 10, 22, 0, tzinfo=timezone.utc)
    nodes = [
        _credit_node("3", "2026-11-10", 175),
        _credit_node("2", "2026-11-10", 176),
        _credit_node("1", "2026-11-01", 179),
        _credit_node("9", "2026-11-09", 9340, title="Electricity", typename="Charge"),
        _credit_node("8", "2026-11-09", 500, title="Referral reward"),
        _credit_node("7", "2026-10-01", 120),
    ]
    credits = parse_credits(_credits_response(nodes), now_utc)
    if [credit["id"] for credit in credits] != ["1", "2", "3"] or credits[0]["amount"] != 179.0 or credits[0]["posted"] != "2026-11-01":
        print("ERROR: expected the three recent Happy Hour credits oldest first, got {}".format(credits))
        return True
    if parse_credits(None, now_utc) is not None or parse_credits({"account": {}}, now_utc) is not None:
        print("ERROR: a failed transactions read should give None")
        return True
    if parse_credits(_credits_response([]), now_utc) != []:
        print("ERROR: a read with no transactions should give an empty list")
        return True
    return False


def test_match_credits():
    """Each credit goes to the closest-billed unpaid event that started by the day it was posted."""
    from billing import match_credits

    imports = {key: _slot(2.9, cost) for key, cost in HAPPY_SUNDAY.items()}
    credits = [{"id": "2", "posted": "2026-11-10", "created": "a", "title": "Weekend Happy Hour", "amount": 176.0}, {"id": "3", "posted": "2026-11-10", "created": "b", "title": "Weekend Happy Hour", "amount": 175.0}]
    events = match_credits(credits, HAPPY_EVENTS, imports, LONDON)
    if [event["credit"] for event in events] != [176.0, 175.0] or [event["billed"] for event in events] != [180.9, 168.6] or any(event["posted"] != "2026-11-10" for event in events):
        print("ERROR: both Sunday Happy Hours should take a credit, got {}".format(events))
        return True
    early = match_credits([dict(credits[0], posted="2026-11-07")], HAPPY_EVENTS, imports, LONDON)
    if any(event["credit"] is not None for event in early):
        print("ERROR: a credit posted before an event can't pay for it, got {}".format(early))
        return True
    far_off = match_credits([dict(credits[0], amount=1000.0)], HAPPY_EVENTS, imports, LONDON)
    if any(event["credit"] is not None for event in far_off):
        print("ERROR: a credit far from any event's billed import should not be matched, got {}".format(far_off))
        return True
    part_billed = match_credits(credits, HAPPY_EVENTS, {key: slot for key, slot in imports.items() if not key.startswith("2026-11-08T11")}, LONDON)
    if part_billed[0]["credit"] is None or part_billed[1]["billed"] is not None or part_billed[1]["credit"] is not None:
        print("ERROR: an event not fully billed takes no credit, got {}".format(part_billed))
        return True
    late = match_credits([dict(credits[0], posted="2026-11-13")], HAPPY_EVENTS, imports, LONDON)
    if any(event["credit"] is not None for event in late):
        print("ERROR: a credit posted more than BILLING_CREDIT_LAG_DAYS after an event should not pay for it, got {}".format(late))
        return True
    pending = match_credits(credits[:1], HAPPY_EVENTS, imports, LONDON)
    if pending[0]["credit"] != 176.0 or pending[1]["credit"] is not None:
        print("ERROR: one credit should pay the closer event and leave the other pending, got {}".format(pending))
        return True
    return False


def test_sensor_credits():
    """The sensor totals each day's matched credits and publishes the events; without events nothing changes."""
    now_utc = datetime(2026, 11, 10, 22, 0, tzinfo=timezone.utc)
    cache = {"import": _slots_for_day(date(2026, 11, 8))}
    for key, cost in HAPPY_SUNDAY.items():
        cache["import"][key] = _slot(2.9, cost)
    credits = [{"id": "2", "posted": "2026-11-10", "created": "a", "title": "Weekend Happy Hour", "amount": 176.0}, {"id": "3", "posted": "2026-11-10", "created": "b", "title": "Weekend Happy Hour", "amount": 175.0}]
    state, attributes = build_billing_sensor(cache, {"import": True, "export": None}, "octopus", now_utc, LONDON, credits, HAPPY_EVENTS)
    day = attributes["days"]["2026-11-08"]
    if day.get("credits") != 351.0 or day.get("credits_pending") != 0 or len(attributes.get("credit_events") or []) != 2:
        print("ERROR: Sunday should total 351p of credits with both events published, got {} {}".format(day, attributes.get("credit_events")))
        return True
    if "day" in attributes["credit_events"][0]:
        print("ERROR: the published events should not carry the internal day key")
        return True
    plain_state, plain = build_billing_sensor(cache, {"import": True, "export": None}, "octopus", now_utc, LONDON, credits, [])
    if "credit_events" in plain or "credits" in plain["days"]["2026-11-08"] or plain_state != state:
        print("ERROR: without credit events the sensor should be unchanged, got {}".format(plain["days"]["2026-11-08"]))
        return True
    return False


def test_reader_reads_credits():
    """With credits_account set a refresh reads the credits once and saves them; a failed read keeps them; without it none are read."""
    import asyncio
    from billing import BillingReader

    now_utc = datetime(2026, 11, 10, 22, 0, tzinfo=timezone.utc)
    storage = _FakeStorage()
    reader = BillingReader(_Owner(storage), "octopus_user", "billing")
    reader.credits_account = "A-1"
    answer = {"credits": _credits_response([_credit_node("2", "2026-11-10", 176)])}
    queries = []

    async def query_fn(query, context):
        """Billing days are full; credits answer from answer."""
        queries.append(query)
        if "transactions" in query:
            return answer["credits"]
        return _day_response(_query_day(query))

    asyncio.run(reader.refresh(query_fn, {"import": ("A-1", "MPAN-IMP"), "export": None}, now_utc))
    if sum(1 for query in queries if "transactions" in query) != 1 or [credit["id"] for credit in reader.credits] != ["2"]:
        print("ERROR: the refresh should read the credits once, got {}".format(reader.credits))
        return True
    if storage.data[("octopus_user", "billing")].get("credits") != reader.credits:
        print("ERROR: the credits should be saved with the cache")
        return True
    restored = BillingReader(_Owner(storage), "octopus_user", "billing")
    asyncio.run(restored.load())
    if restored.credits != reader.credits:
        print("ERROR: the credits should be restored after a restart, got {}".format(restored.credits))
        return True
    answer["credits"] = None
    asyncio.run(reader.refresh(query_fn, {"import": ("A-1", "MPAN-IMP"), "export": None}, now_utc))
    if [credit["id"] for credit in reader.credits] != ["2"]:
        print("ERROR: a failed credits read should keep the credits already read, got {}".format(reader.credits))
        return True
    queries.clear()
    plain = BillingReader(_Owner(_FakeStorage()), "kraken", "billing")
    asyncio.run(plain.refresh(query_fn, {"import": ("A-1", "MPAN-IMP"), "export": None}, now_utc))
    if any("transactions" in query for query in queries):
        print("ERROR: without credits_account no credits should be read")
        return True
    return False


def test_attach_credits():
    """Rows inside a credited event are flagged, and the summary's diff is after the event's credit."""
    from billing import attach_billing

    imports = {_key(m): _slot(1.0, 30.0) for m in (480, 510, 540)}
    event = {"start": _key(480), "end": _key(540), "billed": 60.0, "credit": 58.0, "posted": "2026-11-03"}
    billing = dict(_billing_attrs(imports), credit_events=[event])
    plan = attach_billing(_plan([480, 510, 540]), billing, VIEW_MIDNIGHT, 30, {480: 0.0, 510: 0.0, 540: 30.0}, 55.52, {}, False, None, None)
    flags = [row.get("billing_credit") for row in plan["rows"]]
    summary = plan["billing_summary"]
    if flags != ["credited", "credited", None]:
        print("ERROR: only the event's rows should be flagged credited, got {}".format(flags))
        return True
    if summary.get("credits") != 0.58 or summary.get("credits_pending") != 0 or summary.get("billed_cost") != 0.9 or summary.get("diff") != 0.02:
        print("ERROR: the summary should take the 58p credit off a 90p bill against 30p, got {}".format(summary))
        return True
    pending = attach_billing(_plan([480, 510, 540]), dict(billing, credit_events=[dict(event, credit=None, posted=None)]), VIEW_MIDNIGHT, 30, {480: 0.0, 510: 0.0, 540: 30.0}, 55.52, {}, False, None, None)
    if [row.get("billing_credit") for row in pending["rows"]][:2] != ["pending", "pending"] or pending["billing_summary"].get("credits_pending") != 1 or pending["billing_summary"].get("diff") != 0.6:
        print("ERROR: an event with no credit yet should be flagged pending and not change the diff, got {}".format(pending["billing_summary"]))
        return True
    # Predbat recorded nothing for the 08:30 half-hour, so its bill is left out of the totals and so is its share of the credit
    part = attach_billing(_plan([480, 510, 540]), billing, VIEW_MIDNIGHT, 30, {480: 0.0, 540: 30.0}, 55.52, {}, False, None, None)
    if part["billing_summary"].get("credits") != 0.29 or part["billing_summary"].get("billed_cost") != 0.6 or part["billing_summary"].get("diff") != 0.01:
        print("ERROR: only half the event's bill was counted, so only half its credit should be, got {}".format(part["billing_summary"]))
        return True
    # A half-hour of the event not billed yet shows no * on its dash
    waiting = attach_billing(_plan([480, 510, 540]), dict(billing, slots={"import": {key: slot for key, slot in imports.items() if key != _key(510)}}), VIEW_MIDNIGHT, 30, {480: 0.0, 510: 0.0, 540: 30.0}, 55.52, {}, False, None, None)
    if waiting["rows"][1].get("billing_credit") is not None or waiting["rows"][0].get("billing_credit") != "credited":
        print("ERROR: a row not yet billed in the event should not be flagged, got {}".format([row.get("billing_credit") for row in waiting["rows"]]))
        return True
    # Predbat priced the event free; its full-rate bill must not make the day's rates look implausible and hide the billing
    rated = _plan([480, 510, 540])
    for row, rate in zip(rated["rows"], (0.0, 0.0, 6.0)):
        row["import_rate"] = rate
    priced = attach_billing(rated, dict(billing, slots={"import": dict(imports, **{_key(540): _slot(1.0, 6.0)})}), VIEW_MIDNIGHT, 30, {480: 0.0, 510: 0.0, 540: 6.0}, 55.52, {}, False, None, None)
    if priced["rows"][0].get("billed") != 0.3 or "billing_summary" not in priced:
        print("ERROR: a credited event's full-rate bill should be left out of the plausibility check, got {}".format(priced["rows"][0]))
        return True
    plain = attach_billing(_plan([480]), _billing_attrs(imports), VIEW_MIDNIGHT, 30, {480: 0.0}, 55.52, {}, False, None, None)
    if plain["rows"][0].get("billing_credit") is not None or plain["billing_summary"].get("credits") != 0.0:
        print("ERROR: billing without credit events should flag nothing, got {}".format(plain["billing_summary"]))
        return True
    return False


def test_parse_axle_events():
    """Axle events are read in the sensor's time format, and ones without a time, direction or price are dropped."""
    from billing import parse_axle_events

    events = parse_axle_events(
        [
            {"start_time": "2026-11-02T17:00:00+0000", "end_time": "2026-11-02T18:00:00+0000", "import_export": "export", "pence_per_kwh": 100},
            {"start_time": "2026-11-03T02:00:00Z", "end_time": "2026-11-03T03:00:00Z", "import_export": "import", "pence_per_kwh": "20"},
            {"start_time": "2026-11-04T17:00:00+0000", "end_time": "2026-11-04T16:00:00+0000", "import_export": "export", "pence_per_kwh": 100},
            {"start_time": "2026-11-04T17:00:00", "end_time": "2026-11-04T18:00:00", "import_export": "export", "pence_per_kwh": 100},
            {"start_time": "2026-11-05T17:00:00+0000", "end_time": "2026-11-05T18:00:00+0000", "import_export": None, "pence_per_kwh": 100},
            {"start_time": "2026-11-06T17:00:00+0000", "end_time": "2026-11-06T18:00:00+0000", "import_export": "export", "pence_per_kwh": None},
            {"start_time": "2026-11-07T12:00:00+0000", "end_time": "2026-11-07T12:30:00+0000", "import_export": "export", "pence_per_kwh": 0},
            {"start_time": "bad", "end_time": "2026-11-06T18:00:00+0000", "import_export": "export", "pence_per_kwh": 100},
            "not an event",
        ]
    )
    expected = [
        (datetime(2026, 11, 2, 17, 0, tzinfo=timezone.utc), datetime(2026, 11, 2, 18, 0, tzinfo=timezone.utc), "export", 100.0),
        (datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc), datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc), "import", 20.0),
        (datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc), datetime(2026, 11, 7, 12, 30, tzinfo=timezone.utc), "export", 0.0),
    ]
    if events != expected:
        print("ERROR: expected the export, import and zero-priced events only, got {}".format(events))
        return True
    if parse_axle_events(None) != []:
        print("ERROR: no Axle events should parse to none")
        return True
    return False


def _axle_event(start_minute, end_minute, direction="export", pence=100):
    """An Axle event as the axle_session sensor holds it, by minutes after the view's midnight."""
    return {
        "start_time": (VIEW_MIDNIGHT + timedelta(minutes=start_minute)).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "end_time": (VIEW_MIDNIGHT + timedelta(minutes=end_minute)).strftime("%Y-%m-%dT%H:%M:%S%z"),
        "import_export": direction,
        "pence_per_kwh": pence,
    }


def test_attach_axle():
    """Rows inside an Axle event carry an estimate of the Axle payment Predbat priced in, and the summary's diff is after it."""
    from billing import attach_billing

    # A real export event: 2.56 kWh exported in each half-hour at Agile Outgoing 32.92p and 33.30p plus Axle's 100p, then a plain row
    minutes = [1020, 1050, 1080]
    imports = {_key(1020): _slot(0.0, 0.0), _key(1050): _slot(0.0, 0.0), _key(1080): _slot(1.0, 30.0)}
    exports = {_key(1020): _slot(2.56, 2.56 * 32.92), _key(1050): _slot(2.56, 2.56 * 33.30), _key(1080): _slot(0.0, 0.0)}
    billing = dict(_billing_attrs(imports, exports, legs={"import": True, "export": True}))
    predbat_cost = {1020: -2.56 * 132.92, 1050: -2.56 * 133.30, 1080: 30.0}
    import_kwh = {1020: 0.0, 1050: 0.0, 1080: 1.0}
    export_kwh = {1020: 2.56, 1050: 2.56, 1080: 0.0}
    plan = attach_billing(_plan(minutes), billing, VIEW_MIDNIGHT, 30, predbat_cost, 55.52, {}, False, import_kwh, export_kwh, [_axle_event(1020, 1080)])
    summary = plan["billing_summary"]
    if [row.get("billing_axle") for row in plan["rows"]] != [2.56, 2.56, None]:
        print("ERROR: each event row should carry its 2.56 kWh at 100p, got {}".format([row.get("billing_axle") for row in plan["rows"]]))
        return True
    if summary.get("axle") != 5.12 or summary.get("diff") != 0.0 or summary.get("predbat_cost") != -6.52 or summary.get("axle_slots") != 2:
        print("ERROR: the Axle payment should close the gap between the bill and Predbat's cost, got {}".format(summary))
        return True
    if [row.get("billing_diff") for row in plan["rows"]] != [0.0, 0.0, 0.0]:
        print("ERROR: each Axle row's Diff should be after its estimate, got {}".format([row.get("billing_diff") for row in plan["rows"]]))
        return True
    # A row waiting for its export bill isn't counted, so it isn't flagged either
    waiting = attach_billing(
        _plan(minutes), dict(billing, slots={"import": imports, "export": {key: slot for key, slot in exports.items() if key != _key(1050)}}), VIEW_MIDNIGHT, 30, predbat_cost, 55.52, {}, False, import_kwh, export_kwh, [_axle_event(1020, 1080)]
    )
    if [row.get("billing_axle") for row in waiting["rows"]] != [2.56, None, None] or waiting["billing_summary"].get("axle") != 2.56:
        print("ERROR: a row waiting for its export should carry no estimate, got {}".format([row.get("billing_axle") for row in waiting["rows"]]))
        return True
    # Managed mode makes every row an Axle row: billing at the wrong rates must still be caught by the plausibility check
    managed = _plan(minutes)
    for row in managed["rows"]:
        row["import_rate"] = 110.0
    managed_billing = dict(billing, slots={"import": {key: _slot(1.0, 90.0) for key in imports}, "export": exports})
    hidden = attach_billing(managed, managed_billing, VIEW_MIDNIGHT, 30, predbat_cost, 55.52, {}, False, import_kwh, export_kwh, [_axle_event(1020, 1110)])
    if "billing_summary" in hidden or hidden["rows"][0].get("billed") is not None:
        print("ERROR: billing at three times Predbat's rate less the Axle premium should be hidden, got {}".format(hidden.get("billing_summary")))
        return True
    # Without the event the same day shows the Axle payment as an overspend
    plain = attach_billing(_plan(minutes), billing, VIEW_MIDNIGHT, 30, predbat_cost, 55.52, {}, False, import_kwh, export_kwh)
    if plain["billing_summary"].get("axle") != 0.0 or plain["billing_summary"].get("diff") != 5.12 or plain["rows"][0].get("billing_axle") is not None:
        print("ERROR: without Axle events nothing should be estimated, got {}".format(plain["billing_summary"]))
        return True
    # Import during an export event: Predbat priced it 100p/kWh higher for planning, which the bill doesn't charge and Axle doesn't pay
    phantom_imports = dict(imports, **{_key(1020): _slot(0.5, 15.0)})
    phantom_cost = {**predbat_cost, 1020: 0.5 * 130.0 - 2.56 * 132.92}
    phantom = attach_billing(_plan(minutes), dict(billing, slots={"import": phantom_imports, "export": exports}), VIEW_MIDNIGHT, 30, phantom_cost, 55.52, {}, False, {**import_kwh, 1020: 0.5}, export_kwh, [_axle_event(1020, 1080)])
    if phantom["rows"][0].get("billing_axle") != 2.06 or phantom["billing_summary"].get("diff") != 0.0:
        print("ERROR: the export event's import premium should be given back, got {} {}".format(phantom["rows"][0].get("billing_axle"), phantom["billing_summary"]))
        return True
    # An event covering half a row counts half its price; an import event pays its price on the import
    half = attach_billing(_plan([1020]), billing, VIEW_MIDNIGHT, 30, {1020: -2.56 * 82.92}, 55.52, {}, False, import_kwh, export_kwh, [_axle_event(1035, 1050)])
    if half["rows"][0].get("billing_axle") != 1.28 or half["billing_summary"].get("diff") != 0.0:
        print("ERROR: an event over half the row should count half its price, got {}".format(half["billing_summary"]))
        return True
    night = {_key(120): _slot(4.0, 4.0 * 20.0)}
    imported = attach_billing(_plan([120]), _billing_attrs(night), VIEW_MIDNIGHT, 30, {120: 4.0 * 5.0}, 55.52, {}, True, {120: 4.0}, None, [_axle_event(120, 180, "import", 15)])
    if imported["rows"][0].get("billing_axle") != 0.6 or imported["billing_summary"].get("diff") != 0.0:
        print("ERROR: an import event should be paid on the import, got {}".format(imported["billing_summary"]))
        return True
    # An Axle row's import rate carries the premium, so it mustn't make the day's rates look implausible and hide the billing
    rated = _plan(minutes)
    for row, rate in zip(rated["rows"], (130.0, 130.0, 30.0)):
        row["import_rate"] = rate
    rated_billing = dict(billing, slots={"import": dict(imports, **{_key(1020): _slot(1.0, 30.0), _key(1050): _slot(1.0, 30.0)}), "export": exports})
    priced = attach_billing(rated, rated_billing, VIEW_MIDNIGHT, 30, predbat_cost, 55.52, {}, False, import_kwh, export_kwh, [_axle_event(1020, 1080)])
    if "billing_summary" not in priced:
        print("ERROR: an Axle row's import rate should be left out of the plausibility check")
        return True
    # A managed price curve can go negative: Predbat took 5p/kWh off both rates, so the estimate is negative too
    negative_cost = {1020: 1.0 * 25.0 - 2.56 * 27.92}
    negative_billing = dict(billing, slots={"import": {_key(1020): _slot(1.0, 30.0)}, "export": exports})
    negative = attach_billing(_plan([1020]), negative_billing, VIEW_MIDNIGHT, 30, negative_cost, 55.52, {}, False, {1020: 1.0}, export_kwh, [_axle_event(1020, 1050, pence=-5)])
    if negative["rows"][0].get("billing_axle") != -0.08 or negative["billing_summary"].get("diff") != 0.0:
        print("ERROR: a negative Axle price should give a negative estimate, got {} {}".format(negative["rows"][0].get("billing_axle"), negative["billing_summary"]))
        return True
    # An event starting mid-row: the row's import_rate (read at its first minute) has no premium, so none is taken off for the check
    late = _plan([1020])
    late["rows"][0]["import_rate"] = 30.0
    late_billing = dict(billing, slots={"import": {_key(1020): _slot(1.0, 30.0)}, "export": exports})
    late_plan = attach_billing(late, late_billing, VIEW_MIDNIGHT, 30, {1020: 30.0 - 2.56 * 60.92}, 55.52, {}, False, {1020: 1.0}, export_kwh, [_axle_event(1035, 1050, pence=56)])
    if "billing_summary" not in late_plan:
        print("ERROR: an event starting mid-row shouldn't take its premium off the row's start rate for the plausibility check")
        return True
    # The estimate explains the difference the "(Axle)" event label warns of, so the label is dropped from estimated rows
    labelled = attach_billing(_plan(minutes), billing, VIEW_MIDNIGHT, 30, predbat_cost, 55.52, {minute: "axle" for minute in range(1020, 1110)}, False, import_kwh, export_kwh, [_axle_event(1020, 1080)])
    if labelled["rows"][0].get("billing_event") is not None:
        print("ERROR: an estimated Axle row shouldn't keep its event label, got {}".format(labelled["rows"][0].get("billing_event")))
        return True
    # Without Predbat's recorded kWh there is nothing to estimate from, so the row isn't flagged
    unmetered = attach_billing(_plan(minutes), billing, VIEW_MIDNIGHT, 30, predbat_cost, 55.52, {}, False, None, None, [_axle_event(1020, 1080)])
    if unmetered["rows"][0].get("billing_axle") is not None or unmetered["billing_summary"].get("axle") != 0.0:
        print("ERROR: without recorded kWh no Axle estimate should be made, got {}".format(unmetered["billing_summary"]))
        return True
    return False


def test_plan_renderer_js_parses():
    """The plan renderer's JavaScript, billing columns and summary included, parses: one bad escape stops every plan view rendering."""
    import re
    import shutil
    import subprocess
    import tempfile
    from web_helper import get_plan_renderer_js

    node = shutil.which("node")
    if not node:
        print("Skipping the renderer syntax check: node is not installed")
        return False
    html = get_plan_renderer_js()
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write("\n".join(scripts) if scripts else html)
        path = handle.name
    try:
        result = subprocess.run([node, "--check", path], capture_output=True, text=True, timeout=60)
    finally:
        os.unlink(path)
    if result.returncode != 0:
        print("ERROR: the plan renderer's JavaScript doesn't parse: {}".format(result.stderr.strip()))
        return True
    return False


def test_history_billing_mark_axle():
    """The History views rebuild when an Axle event appears or changes."""
    from output import Output

    billing = _billing_attrs({_key(480): _slot(1.0, 30.0), _key(1050): _slot(1.0, 30.0)})
    base = Output.history_billing_mark(billing)
    with_event = Output.history_billing_mark(dict(billing, axle_events=[_axle_event(1020, 1080)]))
    repriced = Output.history_billing_mark(dict(billing, axle_events=[_axle_event(1020, 1080, pence=80)]))
    if base == with_event or with_event == repriced:
        print("ERROR: the billing mark should change with the Axle events, got {} {} {}".format(base, with_event, repriced))
        return True
    # Events outside the billed half-hours (a managed curve's future prices, or ones older than the bill) don't rebuild anything
    outside = Output.history_billing_mark(dict(billing, axle_events=[_axle_event(1020, 1080), _axle_event(1200, 1230), _axle_event(-60, -30)]))
    if outside != with_event:
        print("ERROR: Axle events outside the billed half-hours shouldn't change the mark, got {} {}".format(outside, with_event))
        return True
    # A slot key that can't be read must not stop the per-cycle mark
    odd = dict(billing, slots={"import": dict(billing["slots"]["import"], **{"not a key": _slot(1.0, 30.0)})}, axle_events=[_axle_event(1020, 1080)])
    if Output.history_billing_mark(odd) is None:
        print("ERROR: an unreadable slot key should still give a mark")
        return True
    return False


def test_octopus_billing_credit_events():
    """Only joined Weekend Happy Hours become credit events, the type falling back to the events list."""
    from types import SimpleNamespace
    from octopus import OctopusAPI

    sessions = {
        "events": [{"id": 2, "eventType": "WEEKEND_HAPPY_HOUR"}],
        "account": {
            "joinedEvents": [
                {"eventId": 1, "eventType": "WEEKEND_HAPPY_HOUR", "startAt": "2026-11-08T08:00:00+00:00", "endAt": "2026-11-08T09:00:00+00:00"},
                {"eventId": 2, "eventType": None, "startAt": "2026-11-08T11:00:00+00:00", "endAt": "2026-11-08T12:00:00+00:00"},
                {"eventId": 3, "eventType": "TURN_DOWN", "startAt": "2026-11-03T17:30:00+00:00", "endAt": "2026-11-03T18:30:00+00:00"},
                {"eventId": 4, "eventType": "WEEKEND_HAPPY_HOUR", "startAt": None, "endAt": None},
            ]
        },
    }
    spans = OctopusAPI.billing_credit_events(SimpleNamespace(saving_sessions=sessions))
    if spans != HAPPY_EVENTS:
        print("ERROR: expected the two Sunday Happy Hours, got {}".format(spans))
        return True
    if OctopusAPI.billing_credit_events(SimpleNamespace(saving_sessions={})) != []:
        print("ERROR: no saving sessions should give no credit events")
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
        test_parse_edf_tou_bucket,
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
    for test in (
        test_reader_fetches_only_incomplete_days,
        test_reader_failed_read_marks_leg,
        test_reader_restart_restores_cache,
        test_reader_random_first_read_delay,
        test_reader_paces_day_queries,
        test_reader_background_read,
        test_kraken_run_random_delay_reads_in_background,
        test_kraken_run_publishes_billing,
        test_kraken_export_uses_export_account,
        test_kraken_no_mpan_skips,
        test_attach_basic,
        test_attach_unpublished_row,
        test_attach_hour_rows,
        test_attach_standing_stripped,
        test_attach_clock_change,
        test_attach_event_label,
        test_attach_import_only,
        test_attach_no_billing,
        test_parse_missing_cost_rejects_slot,
        test_attach_sub_half_hour_interval_skipped,
        test_attach_standing_pro_rata,
        test_attach_hides_diff_when_predbat_saw_nothing,
        test_attach_implausible_rate_hidden,
        test_reader_cache_tied_to_meter,
        test_reader_settles_old_days,
        test_sensor_needs_export_when_configured,
        test_attach_shows_gaps,
        test_attach_export_pending,
        test_attach_columns_when_nothing_billed,
        test_parse_credits,
        test_match_credits,
        test_sensor_credits,
        test_reader_reads_credits,
        test_attach_credits,
        test_parse_axle_events,
        test_attach_axle,
        test_history_billing_mark_axle,
        test_plan_renderer_js_parses,
        test_octopus_billing_credit_events,
    ):
        print("billing: {}".format(test.__name__))
        if test():
            print("ERROR: {} failed".format(test.__name__))
            failed = True
    for test in (test_octopus_partial_errors_is_failure, test_octopus_run_publishes_billing):
        print("billing: {}".format(test.__name__))
        if test(my_predbat):
            print("ERROR: {} failed".format(test.__name__))
            failed = True
    return failed
