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
    data = asyncio.run(api.billing_query("query { a }", "billing-import"))
    if parse_measurements(data) is not None:
        print("ERROR: a partial-error response should be a failed read (None), got {}".format(data))
        return True
    return False


def test_octopus_run_publishes_billing(my_predbat):
    """OctopusAPI.run() reads billing for the modelled import MPAN only and publishes predbat.billing."""
    import asyncio
    from unittest.mock import AsyncMock

    from octopus import OctopusAPI

    api = OctopusAPI(my_predbat, key="test-key", account_id="A-TEST", automatic=False)
    api.async_get_account = AsyncMock(return_value=True)
    api.async_find_tariffs = AsyncMock(return_value={})
    api.async_get_flexibility_events = AsyncMock(return_value={})
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
        if "GENERATION" in query:
            return _day_response(_query_day(query), kwh=0.5, cost=7.5, stat_type="GENERATION_VALUE")
        return _day_response(_query_day(query))

    api.async_graphql_query = fake_query
    asyncio.run(api.run(60, False))
    billing_queries = [query for query in queries if "measurements" in query]
    mpans = {query.split('marketSupplyPointId: "')[1].split('"')[0] for query in billing_queries}
    if mpans != {"1111111111111", "2222222222222"}:
        print("ERROR: billing should query exactly the modelled import and export MPANs, got {}".format(mpans))
        return True
    state = my_predbat.get_state_wrapper("predbat.billing")
    legs = my_predbat.get_state_wrapper("predbat.billing", attribute="legs")
    if state is None or legs != {"import": True, "export": True}:
        print("ERROR: predbat.billing should be published with both legs, got state {} legs {}".format(state, legs))
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
    """The predbat.billing calls a mocked dashboard_item received."""
    return [call for call in api.dashboard_item.call_args_list if call.args and call.args[0] == "predbat.billing"]


def test_kraken_run_publishes_billing():
    """KrakenAPI.run() reads the import MPAN's billing and publishes predbat.billing for the supplier."""
    import asyncio

    api = _kraken_api_for_billing(provider="eon")
    api.import_mpan = "3333333333333"
    asyncio.run(api.run(60, False))
    calls = _billing_publishes(api)
    if not calls:
        print("ERROR: KrakenAPI.run() should publish predbat.billing")
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
    """A predbat.billing attributes dict holding the given slots."""
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
    """Billing whose average rate is nowhere near Predbat's (a units mix-up, say) is not shown at all."""
    from billing import attach_billing

    plan = {"rows": [{"slot_minute": m, "cost_change": 0.0, "import_rate": 25.0} for m in (600, 630)]}
    before = copy.deepcopy(plan)
    billing = _billing_attrs({_key(600): _slot(1.0, 2500.0), _key(630): _slot(1.0, 2500.0)})
    attach_billing(plan, billing, VIEW_MIDNIGHT, 30, {600: 25.0, 630: 25.0}, 55.52, {}, False, None, None)
    if plan != before:
        print("ERROR: billing at 100x Predbat's rate should not be attached")
        return True
    plan = {"rows": [{"slot_minute": m, "cost_change": 0.0, "import_rate": 36.28} for m in (600, 630)]}
    attach_billing(plan, _billing_attrs({_key(600): _slot(1.0, 6.6), _key(630): _slot(1.0, 36.28)}), VIEW_MIDNIGHT, 30, {600: 36.28, 630: 36.28}, 55.52, {}, False, None, None)
    if plan["rows"][0].get("billed") != 0.07:
        print("ERROR: one slot billed cheap against a peak rate is a real difference and must still show")
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
    for test in (
        test_reader_fetches_only_incomplete_days,
        test_reader_failed_read_marks_leg,
        test_reader_restart_restores_cache,
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
