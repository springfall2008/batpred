# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt: off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init
# fmt: on

"""Tests for the day-ahead spot price component (spotprice.py).

All fixtures are hand-written ENTSO-E XML and Energy-Charts / Tibber JSON - nothing here touches
the network. Each test raises AssertionError on failure; run_spotprice_tests() runs them in the
order listed in SPOTPRICE_TESTS and refuses to pass if a test_ function is missing from that list.
"""

import asyncio
import sys
import traceback
from datetime import datetime, timedelta, timezone

import pytz

from spotprice import (
    BIDDING_ZONES,
    SpotPriceAPI,
    SpotPriceError,
    charge_zone_rate,
    format_rates,
    parse_charge_zones,
    parse_energycharts_json,
    parse_entsoe_xml,
    parse_iso_duration_minutes,
    parse_tibber_json,
    spot_export_rate,
    spot_import_rate,
)

UTC = timezone.utc
BERLIN = pytz.timezone("Europe/Berlin")

# A realistic A44 reply (header trimmed to the fields that matter) for DE-LU, hourly, curve type A03.
# Positions 3, 4 and 6 are omitted because their price equals the previous position.
ENTSOE_A44_A03_HOURLY = """<?xml version="1.0" encoding="utf-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
  <mRID>4bd5fbd7cdb8411a8a6e1d3b3a3f1b11</mRID>
  <revisionNumber>1</revisionNumber>
  <type>A44</type>
  <sender_MarketParticipant.mRID codingScheme="A01">10X1001A1001A450</sender_MarketParticipant.mRID>
  <sender_MarketParticipant.marketRole.type>A32</sender_MarketParticipant.marketRole.type>
  <receiver_MarketParticipant.mRID codingScheme="A01">10X1001A1001A450</receiver_MarketParticipant.mRID>
  <receiver_MarketParticipant.marketRole.type>A33</receiver_MarketParticipant.marketRole.type>
  <createdDateTime>2025-03-01T12:00:00Z</createdDateTime>
  <period.timeInterval>
    <start>2025-03-01T23:00Z</start>
    <end>2025-03-02T05:00Z</end>
  </period.timeInterval>
  <TimeSeries>
    <mRID>1</mRID>
    <auction.type>A01</auction.type>
    <businessType>A62</businessType>
    <in_Domain.mRID codingScheme="A01">10Y1001A1001A82H</in_Domain.mRID>
    <out_Domain.mRID codingScheme="A01">10Y1001A1001A82H</out_Domain.mRID>
    <contract_MarketAgreement.type>A01</contract_MarketAgreement.type>
    <currency_Unit.name>EUR</currency_Unit.name>
    <price_Measure_Unit.name>MWH</price_Measure_Unit.name>
    <curveType>A03</curveType>
    <Period>
      <timeInterval>
        <start>2025-03-01T23:00Z</start>
        <end>2025-03-02T05:00Z</end>
      </timeInterval>
      <resolution>PT60M</resolution>
      <Point><position>1</position><price.amount>95.50</price.amount></Point>
      <Point><position>2</position><price.amount>90.00</price.amount></Point>
      <Point><position>5</position><price.amount>-3.20</price.amount></Point>
    </Period>
  </TimeSeries>
</Publication_MarketDocument>
"""

ENTSOE_NO_DATA = """<?xml version="1.0" encoding="utf-8"?>
<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0">
  <mRID>a1</mRID>
  <createdDateTime>2025-03-01T12:00:00Z</createdDateTime>
  <Reason>
    <code>999</code>
    <text>No matching data found for Data item Day-ahead Prices [12.1.D] (10Y1001A1001A82H, 10Y1001A1001A82H) and interval 2025-03-03T23:00:00.000Z/2025-03-04T23:00:00.000Z.</text>
  </Reason>
</Acknowledgement_MarketDocument>
"""

ENTSOE_BAD_TOKEN_ACK = """<?xml version="1.0" encoding="utf-8"?>
<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0">
  <Reason><code>999</code><text>Invalid query attributes or parameters.</text></Reason>
</Acknowledgement_MarketDocument>
"""


def make_a44(periods, curve="A03", currency="EUR", sequences=None):
    """Build an A44 document. periods is a list of (start, end, resolution, {position: price})."""
    series = []
    for index, (start, end, resolution, points) in enumerate(periods):
        sequence = ""
        if sequences:
            sequence = "<classificationSequence_AttributeInstanceComponent.position>{}</classificationSequence_AttributeInstanceComponent.position>".format(sequences[index])
        point_xml = "".join("<Point><position>{}</position><price.amount>{}</price.amount></Point>".format(pos, price) for pos, price in sorted(points.items()))
        series.append(
            "<TimeSeries><currency_Unit.name>{}</currency_Unit.name><curveType>{}</curveType>{}"
            "<Period><timeInterval><start>{}</start><end>{}</end></timeInterval><resolution>{}</resolution>{}</Period></TimeSeries>".format(currency, curve, sequence, start, end, resolution, point_xml)
        )
    return '<?xml version="1.0" encoding="utf-8"?><Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">{}</Publication_MarketDocument>'.format("".join(series))


def dt(text):
    """Parse an ISO string into an aware UTC datetime."""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


class FakeStorage:
    """In-memory stand-in for the storage component."""

    def __init__(self):
        """Start empty."""
        self.data = {}

    async def load(self, module, filename):
        """Return a stored blob or None."""
        return self.data.get((module, filename))

    async def save(self, module, filename, data, format="yaml", expiry=None):
        """Store a blob."""
        self.data[(module, filename)] = data
        return True


class FakeComponents:
    """Provides get_component('storage') for ComponentBase.storage."""

    def __init__(self, storage):
        """Hold the storage."""
        self.storage = storage

    def get_component(self, name):
        """Return the storage for 'storage', else None."""
        return self.storage if name == "storage" else None


class FakeBase:
    """Minimal PredBat base for constructing the component."""

    def __init__(self, local_tz=BERLIN, storage=None):
        """Set up logging, args and an entity store."""
        self.local_tz = local_tz
        self.prefix = "predbat"
        self.args = {}
        self.logs = []
        self.entities = {}
        self.set_args = []
        self.currency_symbols = "€c"
        self.components = FakeComponents(storage) if storage else None
        self.had_errors = False
        self.fatal_error = False

    def log(self, message, quiet=True):
        """Record a log line."""
        self.logs.append(message)

    def dashboard_item(self, entity, state, attributes, app=None):
        """Record a published entity."""
        self.entities[entity] = {"state": state, "attributes": attributes}

    def set_arg(self, arg, value):
        """Record an arg being set."""
        self.set_args.append((arg, value))
        self.args[arg] = value


def make_api(storage=None, local_tz=BERLIN, **kwargs):
    """Construct a SpotPriceAPI on a FakeBase; kwargs override the component config."""
    config = {"provider": "entsoe", "zone": "DE-LU", "entsoe_token": "token"}
    config.update(kwargs)
    base = FakeBase(local_tz=local_tz, storage=storage)
    return SpotPriceAPI(base, **config)


def run(coro):
    """Run a coroutine to completion."""
    return asyncio.run(coro)


def pin_now(api, when):
    """Pin the component's clock."""
    api.now = lambda: when


# ---------------------------------------------------------------------------
# ENTSO-E parsing
# ---------------------------------------------------------------------------


def test_spotprice_entsoe_a03_gap_fill(my_predbat=None):
    """A03 omits points equal to the previous price: missing positions repeat the last price."""
    intervals, currency = parse_entsoe_xml(ENTSOE_A44_A03_HOURLY)
    assert currency == "EUR"
    assert len(intervals) == 6, intervals
    prices = [price for _s, _e, price in intervals]
    assert prices == [95.5, 90.0, 90.0, 90.0, -3.2, -3.2], prices
    assert intervals[0][0] == dt("2025-03-01T23:00Z") and intervals[0][1] == dt("2025-03-02T00:00Z")
    assert intervals[-1][1] == dt("2025-03-02T05:00Z")


def test_spotprice_entsoe_a01_missing_point_not_filled(my_predbat=None):
    """A01 lists every position, so a missing one is genuinely missing and is not filled."""
    xml = make_a44([("2025-03-01T23:00Z", "2025-03-02T03:00Z", "PT60M", {1: 10, 2: 20, 4: 40})], curve="A01")
    intervals, _ = parse_entsoe_xml(xml)
    assert [price for _s, _e, price in intervals] == [10.0, 20.0, 40.0]
    assert intervals[2][0] == dt("2025-03-02T02:00Z")


def test_spotprice_entsoe_resolutions(my_predbat=None):
    """15, 30 (Ireland SEM) and 60 minute resolutions all yield intervals of the right length and count."""
    for resolution, minutes, count in (("PT15M", 15, 96), ("PT30M", 30, 48), ("PT60M", 60, 24), ("PT1H", 60, 24)):
        points = {pos: float(pos) for pos in range(1, count + 1)}
        intervals, _ = parse_entsoe_xml(make_a44([("2025-06-01T22:00Z", "2025-06-02T22:00Z", resolution, points)]))
        assert len(intervals) == count, (resolution, len(intervals))
        assert all(end - start == timedelta(minutes=minutes) for start, end, _p in intervals), resolution
        assert intervals[-1][1] == dt("2025-06-02T22:00Z")
    assert parse_iso_duration_minutes("PT15M") == 15
    assert parse_iso_duration_minutes("P1D") is None


def test_spotprice_entsoe_finest_resolution_wins(my_predbat=None):
    """A day published as both a 60 and a 15 minute series keeps only the 15 minute prices."""
    hourly = ("2025-10-01T22:00Z", "2025-10-02T22:00Z", "PT60M", {pos: 100.0 for pos in range(1, 25)})
    quarter = ("2025-10-01T22:00Z", "2025-10-02T22:00Z", "PT15M", {pos: 50.0 + pos for pos in range(1, 97)})
    intervals, _ = parse_entsoe_xml(make_a44([hourly, quarter]))
    assert len(intervals) == 96, len(intervals)
    assert intervals[0][2] == 51.0
    # Overlapping sequences of the same resolution: the lower sequence number wins
    seq_a = ("2025-10-01T22:00Z", "2025-10-01T23:00Z", "PT15M", {1: 1, 2: 1, 3: 1, 4: 1})
    seq_b = ("2025-10-01T22:00Z", "2025-10-01T23:00Z", "PT15M", {1: 2, 2: 2, 3: 2, 4: 2})
    intervals, _ = parse_entsoe_xml(make_a44([seq_b, seq_a], sequences=[2, 1]))
    assert [price for _s, _e, price in intervals] == [1.0, 1.0, 1.0, 1.0]


def test_spotprice_entsoe_dst_days(my_predbat=None):
    """The 25 hour autumn day and 23 hour spring day parse to 100 and 92 quarter hours, and the local fetch window follows DST."""
    autumn, _ = parse_entsoe_xml(make_a44([("2025-10-25T22:00Z", "2025-10-26T23:00Z", "PT15M", {1: 70.0})]))
    assert len(autumn) == 100, len(autumn)
    assert all(price == 70.0 for _s, _e, price in autumn)
    spring, _ = parse_entsoe_xml(make_a44([("2026-03-28T23:00Z", "2026-03-29T22:00Z", "PT15M", {1: 70.0, 50: 20.0})]))
    assert len(spring) == 92, len(spring)
    assert spring[48][2] == 70.0 and spring[49][2] == 20.0

    api = make_api()
    noon = dt("2025-10-26T11:00Z")
    assert api.local_midnight(noon) == dt("2025-10-25T22:00Z")
    assert api.local_midnight(noon, 1) == dt("2025-10-26T23:00Z")
    start, end = api.fetch_window(dt("2026-03-29T11:00Z"))
    assert start == dt("2026-03-27T23:00Z") and end == dt("2026-03-30T22:00Z"), (start, end)


def test_spotprice_entsoe_acknowledgement(my_predbat=None):
    """'No matching data' is an empty result; any other acknowledgement or a DTD is an error."""
    assert parse_entsoe_xml(ENTSOE_NO_DATA) == ([], None)
    for bad in (ENTSOE_BAD_TOKEN_ACK, "<not-xml", '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><Publication_MarketDocument/>', "<Other/>"):
        try:
            parse_entsoe_xml(bad)
        except SpotPriceError:
            continue
        raise AssertionError("expected SpotPriceError for {}".format(bad[:40]))


def test_spotprice_entsoe_fetch_http(my_predbat=None):
    """fetch_entsoe sends the A44 query in UTC and maps HTTP errors to SpotPriceError without leaking the token."""
    api = make_api(entsoe_token="secret-token")
    seen = {}

    async def ok_get(url, params, expect_json):
        """Return the hourly fixture."""
        seen.update(params)
        return 200, ENTSOE_A44_A03_HOURLY

    api.http_get = ok_get
    intervals = run(api.fetch_entsoe(dt("2025-03-01T23:00Z"), dt("2025-03-02T23:00Z")))
    assert len(intervals) == 6
    assert seen["documentType"] == "A44" and seen["in_Domain"] == BIDDING_ZONES["DE-LU"] and seen["out_Domain"] == BIDDING_ZONES["DE-LU"]
    assert seen["contract_MarketAgreement.type"] == "A01"
    assert seen["periodStart"] == "202503012300" and seen["periodEnd"] == "202503022300"

    for status, body in ((401, ""), (429, ""), (503, ""), (200, ENTSOE_NO_DATA)):

        async def bad_get(url, params, expect_json, status=status, body=body):
            """Return an error reply."""
            return status, body

        api.http_get = bad_get
        try:
            run(api.fetch_entsoe(dt("2025-03-01T23:00Z"), dt("2025-03-02T23:00Z")))
        except SpotPriceError as e:
            assert "secret-token" not in str(e)
            continue
        raise AssertionError("expected an error for HTTP {}".format(status))


# ---------------------------------------------------------------------------
# Energy-Charts and Tibber parsing
# ---------------------------------------------------------------------------


def test_spotprice_energycharts_parse(my_predbat=None):
    """Energy-Charts gives starts only: ends come from the next start, nulls are skipped, the inclusive end is trimmed."""
    base = int(dt("2025-10-01T22:00Z").timestamp())
    data = {"unix_seconds": [base + 900 * i for i in range(6)], "price": [100.0, 110.0, None, 90.0, -5.0, 80.0], "unit": "EUR / MWh"}
    intervals = parse_energycharts_json(data)
    assert len(intervals) == 5
    assert intervals[0][1] - intervals[0][0] == timedelta(minutes=15)
    # The null at 22:30 leaves a gap: 22:15 ends at the next known start (22:45 is within an hour)
    assert intervals[1][0] == dt("2025-10-01T22:15Z") and intervals[1][1] == dt("2025-10-01T22:45Z")
    assert intervals[-1][1] - intervals[-1][0] == timedelta(minutes=15)
    try:
        parse_energycharts_json({"unix_seconds": [1, 2], "price": [1.0]})
        raise AssertionError("mismatched lengths should fail")
    except SpotPriceError:
        pass

    api = make_api(provider="energycharts", entsoe_token=None)

    async def get(url, params, expect_json):
        """Return the fixture, with the inclusive end point included."""
        assert params["bzn"] == "DE-LU" and params["start"] == "2025-10-01T22:00Z"
        return 200, {"unix_seconds": [base + 900 * i for i in range(5)], "price": [1.0, 2.0, 3.0, 4.0, 5.0]}

    api.http_get = get
    intervals = run(api.fetch_energycharts(dt("2025-10-01T22:00Z"), dt("2025-10-01T23:00Z")))
    assert len(intervals) == 4, intervals


def test_spotprice_tibber_parse(my_predbat=None):
    """Tibber totals are picked from the right home and converted to minor units only - no markup or VAT added."""
    data = {
        "data": {
            "viewer": {
                "homes": [
                    {"id": "home-a", "currentSubscription": None},
                    {
                        "id": "home-b",
                        "currentSubscription": {
                            "priceInfo": {
                                "today": [
                                    {"total": 0.3012, "currency": "EUR", "startsAt": "2025-10-02T00:00:00.000+02:00"},
                                    {"total": 0.2988, "currency": "EUR", "startsAt": "2025-10-02T00:15:00.000+02:00"},
                                ],
                                "tomorrow": [{"total": 0.25, "currency": "EUR", "startsAt": "2025-10-03T00:00:00.000+02:00"}],
                            }
                        },
                    },
                ]
            }
        }
    }
    intervals, currency, home_id = parse_tibber_json(data)
    assert home_id == "home-b" and currency == "EUR"
    assert intervals[0][0] == dt("2025-10-01T22:00Z") and intervals[0][1] == dt("2025-10-01T22:15Z")
    assert len(intervals) == 3
    try:
        parse_tibber_json(data, home_id="home-a")
        raise AssertionError("home without a subscription should fail")
    except SpotPriceError:
        pass

    api = make_api(provider="tibber", tibber_token="t", entsoe_token=None, markup=15, vat=0.19, charge_zones=[{"from": "00:00", "to": "00:00", "charge": 9}])
    api.tibber_intervals = intervals
    rates = api.build_import_rates()
    assert [rate for _s, _e, rate in rates] == [30.12, 29.88, 25.0], rates


def test_spotprice_tibber_fetch(my_predbat=None):
    """fetch_tibber asks for quarter-hourly prices, retries without the argument if rejected, and maps auth errors."""
    api = make_api(provider="tibber", tibber_token="t", entsoe_token=None)
    queries = []
    good = {"data": {"viewer": {"homes": [{"id": "h", "currentSubscription": {"priceInfo": {"today": [{"total": 0.2, "startsAt": "2025-10-02T00:00:00+02:00"}], "tomorrow": []}}}]}}}

    async def post(url, payload, headers):
        """Reject the resolution argument the first time."""
        queries.append(payload["query"])
        assert headers["Authorization"] == "Bearer t"
        if len(queries) == 1:
            return 200, {"errors": [{"message": 'Unknown argument "resolution" on field "priceInfo"'}]}
        return 200, good

    api.http_post_json = post
    intervals = run(api.fetch_tibber())
    assert len(intervals) == 1 and "QUARTER_HOURLY" in queries[0] and "QUARTER_HOURLY" not in queries[1]
    assert api.tibber_home_id == "h"

    async def unauthorised(url, payload, headers):
        """Reject the token."""
        return 401, None

    api.http_post_json = unauthorised
    try:
        run(api.fetch_tibber())
        raise AssertionError("401 should raise")
    except SpotPriceError as e:
        assert "401" in str(e)


# ---------------------------------------------------------------------------
# Price formula, charge zones and export
# ---------------------------------------------------------------------------


def test_spotprice_formula(my_predbat=None):
    """(spot/10 + markup + zone) x (1 + VAT) in minor units, including negative spot and an exchange rate."""
    assert spot_import_rate(100.0, 15.0, 0.0, 0.19) == 29.75
    assert spot_import_rate(-50.0, 15.0, 0.0, 0.19) == 11.9
    assert spot_import_rate(100.0, 15.0, 5.0, 0.19) == 35.7
    assert spot_import_rate(100.0, 0.0, 0.0, 0.25, exchange_rate=11.0) == 137.5
    assert spot_export_rate(-20.0, 1.0) == -1.0
    assert spot_export_rate(80.0, -0.4) == 7.6

    api = make_api(markup=15, vat=0.19)
    api.spot_intervals = [(dt("2025-03-02T10:00Z"), dt("2025-03-02T10:15Z"), 100.0)]
    assert api.build_import_rates()[0][2] == 29.75
    published = format_rates(api.build_import_rates())
    assert published == [{"value_inc_vat": 29.75, "valid_from": "2025-03-02T10:00:00+0000", "valid_to": "2025-03-02T10:15:00+0000"}], published


def test_spotprice_charge_zones(my_predbat=None):
    """Charge zones use local time (across DST), honour days and wrap past midnight; the first match wins."""
    zones = parse_charge_zones(
        [
            {"from": "17:00", "to": "21:00", "charge": 12.0, "days": ["mon", "tue", "wed", "thu", "fri"]},
            {"from": "22:00", "to": "06:00", "charge": 2.0},
            {"from": "00:00", "to": "00:00", "charge": 8.0},
        ]
    )
    assert len(zones) == 3

    def at(text):
        """Zone rate at a UTC instant, seen in Berlin time."""
        return charge_zone_rate(zones, dt(text).astimezone(BERLIN))

    assert at("2025-07-01T15:00Z") == 12.0  # Tuesday 17:00 CEST
    assert at("2025-12-02T15:00Z") == 8.0  # Tuesday 16:00 CET - not yet in the peak zone
    assert at("2025-12-02T16:00Z") == 12.0  # Tuesday 17:00 CET
    assert at("2025-12-06T16:00Z") == 8.0  # Saturday 17:00 - weekday-only zone skipped
    assert at("2025-12-02T22:30Z") == 2.0  # 23:30 local, wraps past midnight
    assert at("2025-12-03T04:45Z") == 2.0  # 05:45 local
    assert at("2025-12-03T05:00Z") == 8.0  # 06:00 local, wrap zone ended
    assert at("2025-10-26T01:30Z") == 2.0  # 02:30 CET on the autumn DST day

    api = make_api(markup=10, vat=0, charge_zones=[{"from": "17:00", "to": "00:00", "charge": 12.0}])
    api.spot_intervals = [
        (dt("2025-12-02T15:45Z"), dt("2025-12-02T16:00Z"), 100.0),
        (dt("2025-12-02T16:00Z"), dt("2025-12-02T16:15Z"), 100.0),
        (dt("2025-12-02T22:45Z"), dt("2025-12-02T23:00Z"), 100.0),
        (dt("2025-12-02T23:00Z"), dt("2025-12-02T23:15Z"), 100.0),
    ]
    # to "00:00" runs up to local midnight (23:00Z in winter) and no further
    assert [rate for _s, _e, rate in api.build_import_rates()] == [20.0, 32.0, 32.0, 20.0]

    # days accepts names, full names, comma strings and 0-6 with Monday = 0
    from spotprice import parse_days

    assert parse_days(["mon", "Friday"]) == {0, 4}
    assert parse_days("sat,sun") == {5, 6}
    assert parse_days([0, 6]) == {0, 6}
    assert parse_days(None) is None

    # A value above 1 can only be a percentage: read 19 as 0.19, with a warning
    pct = make_api(markup=15, vat=19)
    assert pct.vat == 0.19 and any("is a fraction" in line for line in pct.base.logs)


def test_spotprice_charge_zone_validation(my_predbat=None):
    """Malformed charge zones are skipped with a warning rather than breaking the tariff."""
    logs = []
    zones = parse_charge_zones(
        [
            {"from": "25:00", "to": "06:00", "charge": 1},
            {"from": "01:00", "to": "02:00", "charge": "x"},
            {"from": "01:00", "to": "02:00", "charge": 1, "days": [7]},
            {"from": "01:00", "to": "02:00", "charge": 1, "days": ["someday"]},
            "junk",
            {"from": "01:00", "to": "02:00", "charge": 3},
        ],
        log=logs.append,
    )
    assert zones == [(60, 120, None, 3.0)], zones
    assert len(logs) == 5


def test_spotprice_export_negative_spot(my_predbat=None):
    """With export_zero_on_negative the export rate is 0 whenever spot < 0, for both fixed and spot-linked export."""
    spot = [(dt("2025-05-01T10:00Z"), dt("2025-05-01T10:15Z"), 30.0), (dt("2025-05-01T10:15Z"), dt("2025-05-01T10:30Z"), -12.0), (dt("2025-05-01T10:30Z"), dt("2025-05-01T10:45Z"), 0.0)]
    fixed = make_api(export_mode="fixed", export_rate=7.94, export_zero_on_negative=True)
    fixed.spot_intervals = spot
    assert [rate for _s, _e, rate in fixed.build_export_rates()] == [7.94, 0.0, 7.94]
    fixed.export_zero_on_negative = False
    assert [rate for _s, _e, rate in fixed.build_export_rates()] == [7.94, 7.94, 7.94]

    linked = make_api(export_mode="spot", export_markup=-0.5, export_zero_on_negative=True)
    linked.spot_intervals = spot
    assert [rate for _s, _e, rate in linked.build_export_rates()] == [2.5, 0.0, -0.5]
    linked.export_zero_on_negative = False
    assert [rate for _s, _e, rate in linked.build_export_rates()] == [2.5, -1.7, -0.5]

    assert make_api(export_mode="none").build_export_rates() == []


def test_spotprice_export_fixed_without_spot(my_predbat=None):
    """A fixed feed-in tariff with no spot data spans the fetch window, unless the negative-price rule needs spot data."""
    api = make_api(provider="tibber", tibber_token="t", entsoe_token=None, zone=None, export_mode="fixed", export_rate=8.0)
    assert not api.needs_spot()
    pin_now(api, dt("2025-05-01T10:00Z"))
    rates = api.build_export_rates()
    assert rates == [(dt("2025-04-29T22:00Z"), dt("2025-05-02T22:00Z"), 8.0)], rates
    api.export_zero_on_negative = True
    assert api.needs_spot()
    assert api.build_export_rates() == []


# ---------------------------------------------------------------------------
# Fetch orchestration, fallback, back-off and schedule
# ---------------------------------------------------------------------------


def spot_fixture(start="2025-05-01T22:00Z", count=192, price=50.0):
    """A run of 15 minute spot intervals."""
    first = dt(start)
    return [(first + timedelta(minutes=15 * i), first + timedelta(minutes=15 * (i + 1)), price) for i in range(count)]


def test_spotprice_provider_fallback(my_predbat=None):
    """When ENTSO-E fails (or has no token) the spot prices come from Energy-Charts instead."""
    api = make_api(entsoe_token="t")
    calls = []

    async def entsoe_fails(start, end):
        """Simulate an ENTSO-E outage."""
        calls.append("entsoe")
        raise SpotPriceError("ENTSO-E returned HTTP 503")

    async def energycharts_ok(start, end):
        """Return spot prices."""
        calls.append("energycharts")
        return spot_fixture()

    api.fetch_entsoe = entsoe_fails
    api.fetch_energycharts = energycharts_ok
    assert run(api.refresh(dt("2025-05-02T08:00Z"))) is True
    assert calls == ["entsoe", "energycharts"]
    assert api.spot_source == "energycharts" and len(api.spot_intervals) == 192
    # A second refresh during the same outage does not log the fallback again; recovery is logged once
    assert run(api.refresh(dt("2025-05-02T14:00Z"))) is True
    assert sum("until ENTSO-E recovers" in line for line in api.base.logs) == 1, api.base.logs

    async def entsoe_ok(start, end):
        """ENTSO-E is back."""
        calls.append("entsoe-ok")
        return spot_fixture()

    api.fetch_entsoe = entsoe_ok
    assert run(api.refresh(dt("2025-05-02T20:00Z"))) is True
    assert api.spot_source == "entsoe"
    assert sum("ENTSO-E is working again" in line for line in api.base.logs) == 1

    # No token at all: ENTSO-E is skipped without a warning on every refresh (initialize() warned once)
    no_token = make_api(entsoe_token=None)
    no_token.fetch_energycharts = energycharts_ok
    assert run(no_token.refresh(dt("2025-05-02T08:00Z"))) is True
    assert no_token.spot_source == "energycharts"
    assert sum("spotprice_entsoe_token is not set" in line for line in no_token.base.logs) == 1
    assert not any("until ENTSO-E recovers" in line for line in no_token.base.logs)

    # Both failing is a failure with both reasons
    both = make_api(entsoe_token="t")
    both.fetch_entsoe = entsoe_fails

    async def energycharts_fails(start, end):
        """Simulate an Energy-Charts outage."""
        raise SpotPriceError("Energy-Charts returned HTTP 500")

    both.fetch_energycharts = energycharts_fails
    assert run(both.refresh(dt("2025-05-02T08:00Z"))) is False
    assert "ENTSO-E" in both.last_error and "Energy-Charts" in both.last_error


def test_spotprice_error_backoff(my_predbat=None):
    """Failures back off exponentially (5, 10, 20 ... capped at 120 minutes); success resets the back-off."""
    api = make_api(provider="energycharts", entsoe_token=None)
    outcome = {"fail": True}

    async def fetch(start, end):
        """Fail or succeed on demand."""
        if outcome["fail"]:
            raise SpotPriceError("down")
        return spot_fixture()

    api.fetch_energycharts = fetch
    now = dt("2025-05-02T08:00Z")
    delays = []
    for _ in range(7):
        assert api.refresh_due(now)
        assert run(api.refresh(now)) is False
        delays.append(int((api.next_attempt - now).total_seconds() // 60))
        assert not api.refresh_due(now + timedelta(minutes=delays[-1]) - timedelta(seconds=1))
        now = api.next_attempt
    assert delays == [5, 10, 20, 40, 80, 120, 120], delays
    assert api.health_message() == "down"
    warnings = [line for line in api.base.logs if "refresh failed" in line]
    assert len(warnings) == 7

    outcome["fail"] = False
    assert run(api.refresh(now)) is True
    assert api.failures == 0 and api.next_attempt is None and api.last_error is None and api.health_message() is None


def test_spotprice_refresh_schedule(my_predbat=None):
    """Poll for tomorrow's prices only after midday local; otherwise refresh every few hours or when the data runs out."""
    api = make_api(provider="energycharts", entsoe_token=None)
    fetched = dt("2025-05-02T07:00Z")  # 09:00 CEST
    api.fetched_at = fetched
    api.spot_intervals = spot_fixture(start="2025-05-01T22:00Z", count=96)  # today only (Berlin)

    assert not api.refresh_due(dt("2025-05-02T09:00Z"))  # 11:00 local - tomorrow not published yet
    api.fetched_at = dt("2025-05-02T09:50Z")
    assert not api.refresh_due(dt("2025-05-02T10:00Z"))  # 12:00 local but fetched 10 minutes ago
    assert api.refresh_due(dt("2025-05-02T10:05Z"))  # 12:05 local, 15 minutes since the last fetch
    api.spot_intervals = spot_fixture(start="2025-05-01T22:00Z", count=192)  # tomorrow now held
    assert not api.refresh_due(dt("2025-05-02T10:05Z"))
    assert api.refresh_due(api.fetched_at + timedelta(hours=6))
    api.fetched_at = dt("2025-05-03T21:00Z")
    assert api.refresh_due(dt("2025-05-03T22:00Z"))  # held data ends now


def test_spotprice_cache_round_trip(my_predbat=None):
    """Prices saved to storage are restored on the next start."""
    storage = FakeStorage()
    api = make_api(provider="energycharts", entsoe_token=None, storage=storage)
    api.spot_intervals = spot_fixture(count=4)
    api.spot_source = "energycharts"
    api.fetched_at = dt("2025-05-02T08:00Z")
    run(api.save_cache())
    restored = make_api(provider="energycharts", entsoe_token=None, storage=storage)
    run(restored.load_cache())
    assert restored.spot_intervals == api.spot_intervals
    assert restored.fetched_at == api.fetched_at and restored.spot_source == "energycharts"


def test_spotprice_publish_and_wire(my_predbat=None):
    """Publishing wires metric_octopus_import once, and metric_octopus_export only when export rates exist."""
    api = make_api(provider="energycharts", entsoe_token=None, markup=10, vat=0)
    api.spot_intervals = spot_fixture(count=4, start="2025-05-02T08:00Z", price=100.0)
    api.publish(dt("2025-05-02T08:20Z"))
    api.publish(dt("2025-05-02T08:35Z"))
    assert api.base.set_args == [("metric_octopus_import", "sensor.predbat_spotprice_import_rates")], api.base.set_args
    sensor = api.base.entities["sensor.predbat_spotprice_import_rates"]
    assert sensor["state"] == 20.0 and len(sensor["attributes"]["rates"]) == 4
    assert sensor["attributes"]["unit_of_measurement"] == "c/kWh"
    assert "sensor.predbat_spotprice_export_rates" not in api.base.entities

    export = make_api(provider="energycharts", entsoe_token=None, export_mode="spot")
    export.spot_intervals = spot_fixture(count=4, start="2025-05-02T08:00Z")
    export.publish(dt("2025-05-02T08:20Z"))
    assert ("metric_octopus_export", "sensor.predbat_spotprice_export_rates") in export.base.set_args

    manual = make_api(provider="energycharts", entsoe_token=None, automatic=False)
    manual.spot_intervals = spot_fixture(count=4, start="2025-05-02T08:00Z")
    manual.publish(dt("2025-05-02T08:20Z"))
    assert manual.base.set_args == []


def test_spotprice_run_lifecycle(my_predbat=None):
    """The first run fails until prices exist; later failed refreshes do not flag an error every cycle."""
    api = make_api(provider="energycharts", entsoe_token=None, storage=FakeStorage())
    pin_now(api, dt("2025-05-02T08:00Z"))

    async def down(start, end):
        """Simulate an outage."""
        raise SpotPriceError("down")

    api.fetch_energycharts = down
    assert run(api.run(0, True)) is False
    assert api.base.entities["sensor.predbat_spotprice_status"]["state"] == "error"

    async def up(start, end):
        """Return prices."""
        return spot_fixture()

    api.fetch_energycharts = up
    api.next_attempt = None
    assert run(api.run(60, True)) is True
    assert api.last_success_timestamp is not None
    api.fetch_energycharts = down
    pin_now(api, dt("2025-05-02T15:00Z"))
    assert run(api.run(120, False)) is True
    assert api.base.entities["sensor.predbat_spotprice_status"]["state"] == "ok"


def test_spotprice_zones_and_registry(my_predbat=None):
    """Every EIC code is well formed, zones resolve by name or code, and the registry/schema entries line up."""
    from components import COMPONENT_LIST
    from config import APPS_SCHEMA

    assert all(len(eic) == 16 and eic.startswith("10Y") for eic in BIDDING_ZONES.values())
    assert len(set(BIDDING_ZONES.values())) == len(BIDDING_ZONES)
    api = make_api(zone="de-lu")
    assert (api.zone, api.zone_eic) == ("DE-LU", "10Y1001A1001A82H")
    assert make_api(zone="10Y1001A1001A59C").zone == "IE-SEM"
    try:
        run(make_api(zone="IE-SEM").fetch_energycharts(dt("2025-05-01T22:00Z"), dt("2025-05-02T22:00Z")))
        raise AssertionError("IE-SEM is not on Energy-Charts")
    except SpotPriceError:
        pass

    entry = COMPONENT_LIST["spotprice"]
    assert entry["class"] == "spotprice.SpotPriceAPI"
    for arg, info in entry["args"].items():
        assert info["config"] in APPS_SCHEMA, info["config"]
    assert entry["args"]["entsoe_token"]["secret"] and entry["args"]["tibber_token"]["secret"]
    # provider defaults to energycharts, so the component must be gated on a zone or a Tibber token
    assert entry["args"]["provider"]["default"] == "energycharts" and not entry["args"]["provider"]["required"]
    assert entry["required_or"] == ["zone", "tibber_token"]
    assert SpotPriceAPI(FakeBase(), zone="NL").provider == "energycharts"


def test_spotprice_engine_reads_rates(my_predbat=None):
    """The published sensor feeds Predbat's own rate reader unchanged, 15 minute steps intact."""
    if my_predbat is None:
        return
    old = (my_predbat.forecast_days, my_predbat.midnight_utc, my_predbat.io_adjusted, dict(my_predbat.args))
    try:
        my_predbat.forecast_days = 2
        my_predbat.midnight_utc = dt("2025-05-02T00:00Z")
        my_predbat.io_adjusted = {}
        api = SpotPriceAPI(my_predbat, provider="energycharts", zone="DE-LU", markup=10, vat=0)
        api.spot_intervals = [(dt("2025-05-02T00:00Z") + timedelta(minutes=15 * i), dt("2025-05-02T00:00Z") + timedelta(minutes=15 * (i + 1)), 10.0 * i) for i in range(8)]
        api.publish(dt("2025-05-02T00:05Z"))
        assert my_predbat.get_arg("metric_octopus_import", None, indirect=False) == "sensor.predbat_spotprice_import_rates"
        rates = my_predbat.fetch_octopus_rates("sensor.predbat_spotprice_import_rates")
        assert rates[0] == 10.0 and rates[14] == 10.0 and rates[15] == 11.0 and rates[105] == 17.0, [rates.get(m) for m in (0, 14, 15, 105)]
    finally:
        my_predbat.forecast_days, my_predbat.midnight_utc, my_predbat.io_adjusted = old[0], old[1], old[2]
        my_predbat.args.clear()
        my_predbat.args.update(old[3])


SPOTPRICE_TESTS = [
    test_spotprice_entsoe_a03_gap_fill,
    test_spotprice_entsoe_a01_missing_point_not_filled,
    test_spotprice_entsoe_resolutions,
    test_spotprice_entsoe_finest_resolution_wins,
    test_spotprice_entsoe_dst_days,
    test_spotprice_entsoe_acknowledgement,
    test_spotprice_entsoe_fetch_http,
    test_spotprice_energycharts_parse,
    test_spotprice_tibber_parse,
    test_spotprice_tibber_fetch,
    test_spotprice_formula,
    test_spotprice_charge_zones,
    test_spotprice_charge_zone_validation,
    test_spotprice_export_negative_spot,
    test_spotprice_export_fixed_without_spot,
    test_spotprice_provider_fallback,
    test_spotprice_error_backoff,
    test_spotprice_refresh_schedule,
    test_spotprice_cache_round_trip,
    test_spotprice_publish_and_wire,
    test_spotprice_run_lifecycle,
    test_spotprice_zones_and_registry,
    test_spotprice_engine_reads_rates,
]


def run_spotprice_tests(my_predbat=None):
    """Run every spot price test. Returns True on failure, False on success."""
    module = sys.modules[__name__]
    defined = {name for name in dir(module) if name.startswith("test_") and callable(getattr(module, name))}
    listed = {func.__name__ for func in SPOTPRICE_TESTS}
    if defined != listed:
        print("  FAIL: SPOTPRICE_TESTS is out of step with the module, missing {}".format(sorted(defined - listed)))
        return True
    for test_func in SPOTPRICE_TESTS:
        try:
            test_func(my_predbat)
        except Exception as e:
            print("  FAIL: {}: {}".format(test_func.__name__, e))
            traceback.print_exc()
            return True
        print("  OK: {}".format(test_func.__name__))
    return False
