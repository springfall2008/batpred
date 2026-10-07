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
    """Energy-Charts gives starts only: each interval gets the series resolution, a null price leaves a gap, the inclusive end is trimmed."""
    base = int(dt("2025-10-01T22:00Z").timestamp())
    data = {"unix_seconds": [base + 900 * i for i in range(6)], "price": [100.0, 110.0, None, 90.0, -5.0, 80.0], "unit": "EUR / MWh"}
    intervals = parse_energycharts_json(data)
    assert len(intervals) == 5
    assert intervals[0][1] - intervals[0][0] == timedelta(minutes=15)
    # The null at 22:30 leaves a gap: 22:15 is not stretched over it
    assert intervals[1][0] == dt("2025-10-01T22:15Z") and intervals[1][1] == dt("2025-10-01T22:30Z")
    assert intervals[2][0] == dt("2025-10-01T22:45Z")
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
    assert parse_days([1, 7]) == {0, 6}
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
            {"from": "01:00", "to": "02:00", "charge": 1, "days": [0]},
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
    """Poll for tomorrow's prices only after midday CET; otherwise refresh every few hours or when the data runs out."""
    api = make_api(provider="energycharts", entsoe_token=None)
    fetched = dt("2025-05-02T07:00Z")  # 09:00 CEST
    api.fetched["spot"] = fetched
    api.spot_intervals = spot_fixture(start="2025-05-01T22:00Z", count=96)  # today only (Berlin)

    assert not api.refresh_due(dt("2025-05-02T09:00Z"))  # 11:00 local - tomorrow not published yet
    api.fetched["spot"] = dt("2025-05-02T09:50Z")
    assert not api.refresh_due(dt("2025-05-02T10:00Z"))  # 12:00 local but fetched 10 minutes ago
    assert api.refresh_due(dt("2025-05-02T10:05Z"))  # 12:05 local, 15 minutes since the last fetch
    api.spot_intervals = spot_fixture(start="2025-05-01T22:00Z", count=192)  # tomorrow now held
    assert not api.refresh_due(dt("2025-05-02T10:05Z"))
    assert api.refresh_due(api.fetched_at + timedelta(hours=6))
    api.fetched["spot"] = dt("2025-05-03T21:00Z")
    assert api.refresh_due(dt("2025-05-03T22:00Z"))  # held data ends now


def test_spotprice_cache_round_trip(my_predbat=None):
    """Prices saved to storage are restored on the next start."""
    storage = FakeStorage()
    api = make_api(provider="energycharts", entsoe_token=None, storage=storage)
    api.spot_intervals = spot_fixture(count=4)
    api.spot_source = "energycharts"
    api.fetched["spot"] = dt("2025-05-02T08:00Z")
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
    assert "default" not in entry["args"]["provider"] and not entry["args"]["provider"]["required"]
    assert entry["required_or"] == ["zone", "tibber_token", "ostrom_client_id", "ostrom_client_secret", "octopus_de_api_key", "ews_api_key"]
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


def test_spotprice_no_stretch_over_missing_points(my_predbat=None):
    """Missing points stay gaps: a timestamp absent altogether, a null Tibber total, and an A01 hole are never covered by a neighbour."""
    from spotprice import intervals_from_starts

    t0 = dt("2025-10-01T22:00Z")
    quarter = timedelta(minutes=15)
    # 22:30 is absent altogether (not even a timestamp)
    intervals = intervals_from_starts([(t0, 1.0), (t0 + quarter, 2.0), (t0 + 3 * quarter, 4.0), (t0 + 4 * quarter, 5.0)])
    assert [(start, end) for start, end, _v in intervals][1] == (t0 + quarter, t0 + 2 * quarter), intervals
    assert all(end - start == quarter for start, end, _v in intervals)
    # Tibber null total
    data = {
        "data": {
            "viewer": {
                "homes": [
                    {
                        "id": "h",
                        "currentSubscription": {
                            "priceInfo": {"today": [{"total": 0.2, "startsAt": "2025-10-02T00:00:00+02:00"}, {"total": None, "startsAt": "2025-10-02T00:15:00+02:00"}, {"total": 0.3, "startsAt": "2025-10-02T00:30:00+02:00"}], "tomorrow": []}
                        },
                    }
                ]
            }
        }
    }
    tibber, _currency, _home = parse_tibber_json(data)
    assert [(start, end) for start, end, _v in tibber] == [(t0, t0 + quarter), (t0 + 2 * quarter, t0 + 3 * quarter)], tibber
    # A01: the missing position stays missing
    a01, _ = parse_entsoe_xml(make_a44([("2025-10-01T22:00Z", "2025-10-01T23:00Z", "PT15M", {1: 1, 2: 2, 4: 4})], curve="A01"))
    assert [(start, end) for start, end, _v in a01] == [(t0, t0 + quarter), (t0 + quarter, t0 + 2 * quarter), (t0 + 3 * quarter, t0 + 4 * quarter)]


def test_spotprice_parse_errors_wrapped(my_predbat=None):
    """Every malformed response becomes SpotPriceError, and a raw exception from a fetch still takes the back-off path."""
    bad_inputs = [
        (parse_energycharts_json, ({"unix_seconds": [10**20], "price": [1.0]},)),  # OverflowError/OSError from fromtimestamp
        (parse_energycharts_json, ({"unix_seconds": ["abc"], "price": [1.0]},)),
        (parse_energycharts_json, ({"unix_seconds": [1], "price": ["x"]},)),
        (parse_energycharts_json, ({"unix_seconds": 5, "price": 5},)),
        (parse_tibber_json, ({"data": {"viewer": {"homes": [{"id": "h", "currentSubscription": {"priceInfo": {"today": [{"total": 0.2, "startsAt": 1759356000}]}}}]}}},)),
        (parse_tibber_json, ({"data": {"viewer": {"homes": [{"id": "h", "currentSubscription": {"priceInfo": {"today": [{"total": 0.2}]}}}]}}},)),
        (parse_tibber_json, ({"data": {"viewer": {"homes": "nope"}}},)),
        (parse_tibber_json, ({"data": {"viewer": {"homes": [{"id": "h", "currentSubscription": {"priceInfo": {"today": "x"}}}]}}},)),
        (parse_entsoe_xml, (b"\xff\xfe<bad",)),
        (parse_entsoe_xml, (12345,)),
    ]
    for func, args in bad_inputs:
        try:
            func(*args)
        except SpotPriceError:
            continue
        raise AssertionError("{}{} did not raise SpotPriceError".format(func.__name__, args))

    api = make_api(provider="energycharts", entsoe_token=None)

    async def undecodable(url, params, expect_json):
        """A body that will not decode."""
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    api.http_get = undecodable
    # Each fetch converts it itself, not just refresh()'s safety net
    entsoe_api = make_api(provider="entsoe", entsoe_token="t")
    entsoe_api.http_get = undecodable
    tibber_api = make_api(provider="tibber", tibber_token="t", entsoe_token=None)

    async def undecodable_post(url, payload, headers):
        """A body that will not decode."""
        raise UnicodeDecodeError("utf-8", b"\\xff", 0, 1, "invalid start byte")

    tibber_api.http_post_json = undecodable_post
    for coro in (api.fetch_energycharts(dt("2025-05-01T22:00Z"), dt("2025-05-02T22:00Z")), entsoe_api.fetch_entsoe(dt("2025-05-01T22:00Z"), dt("2025-05-02T22:00Z")), tibber_api.fetch_tibber()):
        try:
            run(coro)
        except SpotPriceError:
            continue
        raise AssertionError("fetch did not convert UnicodeDecodeError")
    now = dt("2025-05-02T08:00Z")
    assert run(api.refresh(now)) is False
    assert api.failures == 1 and api.next_attempt == now + timedelta(minutes=5), api.last_error

    async def explodes(start, end):
        """An unexpected exception type from a fetch."""
        raise KeyError("price")

    other = make_api(provider="energycharts", entsoe_token=None)
    other.fetch_energycharts = explodes
    assert run(other.refresh(now)) is False and other.failures == 1


def test_spotprice_energycharts_never_tries_entsoe(my_predbat=None):
    """Provider energycharts uses Energy-Charts only, even with an ENTSO-E token set; the fallback runs entsoe to energycharts only."""
    calls = []

    async def entsoe(start, end):
        """Should not be called."""
        calls.append("entsoe")
        return spot_fixture()

    async def energycharts(start, end):
        """Return prices."""
        calls.append("energycharts")
        return spot_fixture()

    async def energycharts_down(start, end):
        """Energy-Charts outage."""
        calls.append("energycharts")
        raise SpotPriceError("Energy-Charts returned HTTP 500")

    api = make_api(provider="energycharts", entsoe_token="token")
    api.fetch_entsoe = entsoe
    api.fetch_energycharts = energycharts
    assert run(api.refresh(dt("2025-05-02T08:00Z"))) is True and calls == ["energycharts"]
    calls.clear()
    api.fetch_energycharts = energycharts_down
    api.next_attempt = None
    api.fetched["spot"] = None
    assert run(api.refresh(dt("2025-05-02T15:00Z"))) is False
    assert calls == ["energycharts"], calls


def test_spotprice_market_day_polling(my_predbat=None):
    """'Have tomorrow' follows the CET market day: IE-SEM ends 23:00 Irish time, ES at local midnight, FI at 01:00 local - and FI's window keeps that last hour."""
    cases = [
        # zone, local tz, now (UTC, after 12:00 CET), end of tomorrow's market day (UTC)
        ("IE-SEM", "Europe/Dublin", "2025-06-10T11:30Z", "2025-06-11T22:00Z"),
        ("ES", "Europe/Madrid", "2025-06-10T11:30Z", "2025-06-11T22:00Z"),
        ("FI", "Europe/Helsinki", "2025-06-10T11:30Z", "2025-06-11T22:00Z"),
        ("IE-SEM", "Europe/Dublin", "2025-12-10T12:30Z", "2025-12-11T23:00Z"),
    ]
    for zone, tz_name, now_text, market_end_text in cases:
        # IE-SEM is ENTSO-E only; the others can use Energy-Charts
        api = make_api(provider="entsoe" if zone == "IE-SEM" else "energycharts", entsoe_token="token" if zone == "IE-SEM" else None, zone=zone, local_tz=pytz.timezone(tz_name))
        now = dt(now_text)
        market_end = dt(market_end_text)
        start, end = api.fetch_window(now)
        assert end >= market_end, (zone, end)
        # Holding prices up to the end of tomorrow's market day counts as having tomorrow - no more polling
        api.spot_intervals = [(market_end - timedelta(hours=48), market_end, 50.0)]
        api.fetched["spot"] = now - timedelta(minutes=30)
        assert api.has_tomorrow(market_end, now), zone
        assert not api.refresh_due(now), zone
        # One quarter short of it is still missing tomorrow - poll
        api.spot_intervals = [(market_end - timedelta(hours=48), market_end - timedelta(minutes=15), 50.0)]
        assert api.refresh_due(now), zone
    # FI: the window must reach 01:00 local the day after tomorrow, not stop at local midnight
    fi = make_api(provider="energycharts", entsoe_token=None, zone="FI", local_tz=pytz.timezone("Europe/Helsinki"))
    assert fi.fetch_window(dt("2025-06-10T11:30Z"))[1] == dt("2025-06-11T22:00Z")
    # Before 12:00 CET there is no polling for tomorrow, whatever the local clock says
    fi.spot_intervals = [(dt("2025-06-09T22:00Z"), dt("2025-06-10T22:00Z"), 50.0)]
    fi.fetched["spot"] = dt("2025-06-10T09:00Z")
    assert not fi.refresh_due(dt("2025-06-10T09:30Z"))  # 12:30 Helsinki, 11:30 CET


def test_spotprice_status_states(my_predbat=None):
    """Status is error with no price for now, stale while a failing source runs out soon, ok otherwise."""
    api = make_api(provider="energycharts", entsoe_token=None)
    now = dt("2025-05-02T14:00Z")
    api.publish(now)
    assert api.base.entities["sensor.predbat_spotprice_status"]["state"] == "waiting"
    api.spot_intervals = spot_fixture(start="2025-05-01T22:00Z", count=96)  # ends 2025-05-02T22:00Z, 8 hours away
    api.publish(now)
    assert api.base.entities["sensor.predbat_spotprice_status"]["state"] == "ok"
    api.source_errors["spot"] = "down"
    api.last_error = "down"
    api.publish(now)
    assert api.base.entities["sensor.predbat_spotprice_status"]["state"] == "stale"
    api.spot_intervals = spot_fixture(start="2025-05-01T22:00Z", count=192)  # through tomorrow
    api.publish(now)
    assert api.base.entities["sensor.predbat_spotprice_status"]["state"] == "ok"
    api.publish(dt("2025-05-04T10:00Z"))  # past the held prices
    assert api.base.entities["sensor.predbat_spotprice_status"]["state"] == "error"
    gap = make_api(provider="energycharts", entsoe_token=None)
    gap.spot_intervals = [(dt("2025-05-02T13:00Z"), dt("2025-05-02T13:15Z"), 1.0), (dt("2025-05-02T14:15Z"), dt("2025-05-03T22:00Z"), 1.0)]
    gap.publish(now)  # no price for 14:00 itself
    assert gap.base.entities["sensor.predbat_spotprice_status"]["state"] == "error"


def test_spotprice_tibber_partial_failure(my_predbat=None):
    """With Tibber plus spot export, a spot failure still stores, timestamps and caches the Tibber prices, and shows as a partial failure."""
    storage = FakeStorage()
    api = make_api(provider="tibber", tibber_token="t", entsoe_token=None, zone="NL", export_mode="spot", storage=storage)
    now = dt("2025-05-02T14:00Z")
    tibber = [(dt("2025-05-01T22:00Z") + timedelta(minutes=15 * i), dt("2025-05-01T22:00Z") + timedelta(minutes=15 * (i + 1)), 0.25) for i in range(192)]

    async def fetch_tibber():
        """Tibber works."""
        return tibber

    async def spot_down(start, end):
        """Spot source down."""
        raise SpotPriceError("Energy-Charts returned HTTP 500")

    api.fetch_tibber = fetch_tibber
    api.fetch_energycharts = spot_down
    assert run(api.refresh(now)) is False
    assert api.tibber_intervals == tibber and api.fetched["tibber"] == now and api.fetched["spot"] is None
    assert "spot" in api.source_errors and api.failures == 1
    cached = storage.data[("spotprice", api.cache_filename())]
    assert cached["tibber_fetched_at"] == now.isoformat() and len(cached["tibber_intervals"]) == 192
    api.publish(now)
    status = api.base.entities["sensor.predbat_spotprice_status"]
    assert status["state"] == "stale" and "spot" in status["attributes"]["source_errors"], status
    # The retry after the back-off fetches only the failing spot source
    calls = []

    async def fetch_tibber_counted():
        """Count Tibber calls."""
        calls.append("tibber")
        return tibber

    async def spot_up(start, end):
        """Spot source back."""
        calls.append("spot")
        return spot_fixture()

    api.fetch_tibber = fetch_tibber_counted
    api.fetch_energycharts = spot_up
    later = api.next_attempt
    assert api.refresh_due(later)
    assert run(api.refresh(later)) is True and calls == ["spot"], calls
    api.publish(later)
    assert api.base.entities["sensor.predbat_spotprice_status"]["state"] == "ok"


def test_spotprice_days_follow_engine_numbering(my_predbat=None):
    """Numbered days are 1-7 with Monday = 1, as in rates_import day_of_week; 0 and 8 are rejected with a warning."""
    from spotprice import parse_days

    assert parse_days([1, 2, 3, 4, 5]) == {0, 1, 2, 3, 4}
    assert parse_days("6,7") == {5, 6}
    assert parse_days(["mon", 7]) == {0, 6}
    for bad in ([0], "0,1", [8], "8"):
        try:
            parse_days(bad)
        except ValueError:
            continue
        raise AssertionError("{} should be rejected".format(bad))
    logs = []
    zones = parse_charge_zones([{"from": "17:00", "to": "21:00", "charge": 1, "days": [0, 1]}, {"from": "17:00", "to": "21:00", "charge": 2, "days": [6, 7]}], log=logs.append)
    assert zones == [(1020, 1260, {5, 6}, 2.0)], zones
    assert len(logs) == 1 and "1-7 (Monday = 1)" in logs[0], logs
    # Saturday 17:30 Berlin is in the [6, 7] zone, Friday is not
    assert charge_zone_rate(zones, dt("2025-12-06T16:30Z").astimezone(BERLIN)) == 2.0
    assert charge_zone_rate(zones, dt("2025-12-05T16:30Z").astimezone(BERLIN)) == 0.0


def test_spotprice_tibber_token_only(my_predbat=None):
    """With spotprice_provider unset, a Tibber token implies provider tibber (zone or not); spot-dependent export without a zone is switched off once, not failed every refresh."""
    api = make_api(provider=None, entsoe_token=None, zone=None, tibber_token="t")
    assert api.provider == "tibber" and api.sources_needed() == ["tibber"]
    assert make_api(provider="energycharts", entsoe_token=None, tibber_token="t").provider == "energycharts"
    assert make_api(provider=None, entsoe_token=None, zone="NL").provider == "energycharts"

    for kwargs in ({"export_mode": "spot"}, {"export_mode": "fixed", "export_rate": 8.0, "export_zero_on_negative": True}):
        tib = make_api(provider="tibber", tibber_token="t", entsoe_token=None, zone=None, **kwargs)
        warnings = [line for line in tib.base.logs if "export rates disabled" in line]
        assert len(warnings) == 1, tib.base.logs
        assert tib.export_mode == "none" and not tib.needs_spot() and tib.sources_needed() == ["tibber"]

        async def fetch_tibber():
            """Tibber works."""
            return [(dt("2025-05-02T08:00Z"), dt("2025-05-02T08:15Z"), 0.25)]

        tib.fetch_tibber = fetch_tibber
        assert run(tib.refresh(dt("2025-05-02T08:00Z"))) is True and tib.failures == 0
        tib.publish(dt("2025-05-02T08:05Z"))
        assert "sensor.predbat_spotprice_export_rates" not in tib.base.entities
        assert not any(arg == "metric_octopus_export" for arg, _value in tib.base.set_args)
    # With a zone the spot-linked export stays on
    assert make_api(provider="tibber", tibber_token="t", entsoe_token=None, zone="NL", export_mode="spot").export_mode == "spot"


def test_spotprice_vat_one_or_more_is_percent(my_predbat=None):
    """spotprice_vat is a fraction: values of 1 or more are percentages (1 means 1%, not 100%) and warn."""
    for value, expected, warned in ((0.19, 0.19, False), (0.0, 0.0, False), (0.999, 0.999, False), (1, 0.01, True), (19, 0.19, True), (25.5, 0.255, True)):
        api = make_api(vat=value)
        assert abs(api.vat - expected) < 1e-12, (value, api.vat)
        assert any("is a fraction" in line for line in api.base.logs) == warned, (value, api.base.logs)
    assert spot_import_rate(100.0, 0.0, 0.0, make_api(vat=1).vat) == 10.1


def test_spotprice_charge_zone_unknown_keys(my_predbat=None):
    """Old-style start/end/rate entries are reported once, and day_of_week works as an alias for days with a warning."""
    logs = []
    zones = parse_charge_zones([{"start": "17:00", "end": "21:00", "rate": 12}, {"start": "00:00", "end": "06:00", "rate": 2}], log=logs.append)
    assert zones == []
    unknown = [line for line in logs if "unknown key" in line]
    assert len(unknown) == 1 and "end, rate, start" in unknown[0], logs

    logs = []
    zones = parse_charge_zones([{"from": "17:00", "to": "21:00", "charge": 1, "day_of_week": "6,7"}, {"from": "08:00", "to": "09:00", "charge": 2, "day_of_week": "1"}], log=logs.append)
    assert zones == [(1020, 1260, {5, 6}, 1.0), (480, 540, {0}, 2.0)], zones
    alias = [line for line in logs if "day_of_week" in line]
    assert len(alias) == 1 and not any("unknown key" in line for line in logs), logs

    # A clean configuration logs nothing
    logs = []
    parse_charge_zones([{"from": "17:00", "to": "21:00", "charge": 1, "days": ["sat"]}], log=logs.append)
    assert logs == []
    # Through the component the warning is emitted once at start-up
    api = make_api(charge_zones=[{"from": "17:00", "to": "21:00", "charge": 1, "rate": 5}])
    assert sum("unknown key" in line for line in api.base.logs) == 1


def test_spotprice_backoff_per_source(my_predbat=None):
    """A spot outage backs off on its own and never holds back Tibber's poll for tomorrow; each source recovers independently."""
    api = make_api(provider="tibber", tibber_token="t", entsoe_token=None, zone="DE-LU", export_mode="spot")
    tibber_calls = []

    async def tibber_today_only():
        """Tibber with today's prices only, so it keeps polling for tomorrow after 12:00 CET."""
        tibber_calls.append(1)
        return [(dt("2025-05-01T22:00Z") + timedelta(minutes=15 * i), dt("2025-05-01T22:15Z") + timedelta(minutes=15 * i), 0.3) for i in range(96)]

    async def spot_down(start, end):
        """Spot source down."""
        raise SpotPriceError("down")

    api.fetch_tibber = tibber_today_only
    api.fetch_energycharts = spot_down
    t = dt("2025-05-02T10:30Z")
    run(api.refresh(t))
    while api.source_failures["spot"] < 6:
        t = api.source_next_attempt["spot"]
        run(api.refresh(t))
    assert api.source_failures["tibber"] == 0 and api.source_next_attempt["tibber"] is None
    spot_retry = api.source_next_attempt["spot"]
    assert spot_retry - t == timedelta(minutes=120)
    # Tibber falls due 15 minutes after its last fetch and must be fetched then, deep inside the spot back-off
    tibber_due = api.fetched["tibber"] + timedelta(minutes=15)
    assert tibber_due < spot_retry
    assert api.refresh_due(tibber_due)
    before = len(tibber_calls)
    run(api.refresh(tibber_due))
    assert len(tibber_calls) == before + 1 and api.fetched["tibber"] == tibber_due
    # ...without touching the spot back-off
    assert api.source_next_attempt["spot"] == spot_retry and api.source_failures["spot"] == 6
    # Status attributes show the per-source state
    api.publish(tibber_due)
    attrs = api.base.entities["sensor.predbat_spotprice_status"]["attributes"]
    assert attrs["failures"] == {"spot": 6, "tibber": 0} and set(attrs["next_attempt"]) == {"spot"}, attrs


def test_spotprice_mixed_resolution_starts(my_predbat=None):
    """A start-only response mixing 60 and 15 minute points keeps each at its own length, at both boundaries, without stretching gaps."""
    base = int(dt("2025-10-01T22:00Z").timestamp())
    hour, quarter = 3600, 900
    # UTC: 22:00, 23:00, 00:00 hourly; 01:00..01:45 quarters; 02:00, 03:00, 04:00 hourly again; 05:00 missing;
    # 06:00 the last point, isolated by that gap, keeps the hourly resolution of the run before it
    stamps = [base, base + hour, base + 2 * hour] + [base + 3 * hour + quarter * k for k in range(4)] + [base + 4 * hour, base + 5 * hour, base + 6 * hour, base + 8 * hour]
    intervals = parse_energycharts_json({"unix_seconds": stamps, "price": [float(i) for i in range(len(stamps))]})
    lengths = [int((end - start).total_seconds() // 60) for start, end, _v in intervals]
    assert lengths == [60, 60, 60, 15, 15, 15, 15, 60, 60, 60, 60], lengths
    # 04:00 (index 9) runs to 05:00 only - not stretched over the missing 05:00..06:00 hour - and 06:00 keeps 60
    assert intervals[9][1] == dt("2025-10-02T05:00Z") and intervals[10][0] == dt("2025-10-02T06:00Z")
    # A 15 minute run with one quarter missing still leaves that quarter as a gap
    q = parse_energycharts_json({"unix_seconds": [base + quarter * k for k in (0, 1, 3, 4)], "price": [1.0, 2.0, 3.0, 4.0]})
    assert [int((e - s_).total_seconds() // 60) for s_, e, _v in q] == [15, 15, 15, 15], q


def test_spotprice_tibber_token_implies_tibber_with_zone(my_predbat=None):
    """With spotprice_provider unset, a Tibber token selects tibber even when a zone is also set; an explicit provider wins."""
    api = make_api(provider=None, entsoe_token=None, zone="DE-LU", tibber_token="t", export_mode="spot")
    assert api.provider == "tibber" and api.sources_needed() == ["spot", "tibber"]
    assert make_api(provider="entsoe", zone="DE-LU", tibber_token="t").provider == "entsoe"


def test_spotprice_fetch_error_categories(my_predbat=None):
    """Fetch failures record connection_error only for network errors; anything else is a decode_error. Both become SpotPriceError."""
    import aiohttp

    import spotprice as spotprice_module

    recorded = []
    original = spotprice_module.record_api_call
    spotprice_module.record_api_call = lambda service, success=True, reason=None: recorded.append((service, success, reason))
    try:
        window = (dt("2025-05-01T22:00Z"), dt("2025-05-02T22:00Z"))
        for exception, reason, phrase in (
            (aiohttp.ClientConnectionError("refused"), "connection_error", "request failed"),
            (asyncio.TimeoutError(), "connection_error", "request failed"),
            (UnicodeDecodeError("utf-8", b"\\xff", 0, 1, "bad"), "decode_error", "could not be read"),
            (KeyError("price"), "decode_error", "could not be read"),
        ):

            async def get(url, params, expect_json, exception=exception):
                """Raise the given exception."""
                raise exception

            async def post(url, payload, headers, exception=exception):
                """Raise the given exception."""
                raise exception

            ec = make_api(provider="energycharts", entsoe_token=None)
            ec.http_get = get
            en = make_api(provider="entsoe", entsoe_token="secret-token")
            en.http_get = get
            tb = make_api(provider="tibber", tibber_token="t", entsoe_token=None)
            tb.http_post_json = post
            for service, coro in (("energycharts", ec.fetch_energycharts(*window)), ("entsoe", en.fetch_entsoe(*window)), ("tibber", tb.fetch_tibber())):
                recorded.clear()
                try:
                    run(coro)
                    raise AssertionError("expected SpotPriceError")
                except SpotPriceError as e:
                    assert phrase in str(e) and "secret-token" not in str(e), (service, str(e))
                assert recorded == [(service, False, reason)], (service, type(exception).__name__, recorded)
    finally:
        spotprice_module.record_api_call = original


def test_spotprice_entsoe_only_zone(my_predbat=None):
    """IE-SEM has no Energy-Charts data: without an ENTSO-E token that is one clear error and no fetching; at runtime an ENTSO-E failure is not 'rescued' by Energy-Charts."""
    calls = []

    async def entsoe(start, end):
        """Record and fail."""
        calls.append("entsoe")
        raise SpotPriceError("ENTSO-E returned HTTP 503")

    async def energycharts(start, end):
        """Must never be called for IE-SEM."""
        calls.append("energycharts")
        raise SpotPriceError("Energy-Charts does not publish zone IE-SEM")

    api = make_api(provider="entsoe", entsoe_token=None, zone="IE-SEM", local_tz=pytz.timezone("Europe/Dublin"), storage=FakeStorage())
    api.fetch_entsoe = entsoe
    api.fetch_energycharts = energycharts
    assert api.config_error == "spotprice_entsoe_token is required for zone IE-SEM"
    errors = [line for line in api.base.logs if line.startswith("Error:")]
    assert errors == ["Error: SpotPrice: spotprice_entsoe_token is required for zone IE-SEM"], api.base.logs
    # ...and no misleading "using Energy-Charts" warning, since Energy-Charts cannot stand in here
    assert not any("using Energy-Charts" in line for line in api.base.logs), api.base.logs
    assert any("using Energy-Charts" in line for line in make_api(provider="entsoe", entsoe_token=None, zone="DE-LU").base.logs)
    now = dt("2025-05-02T08:00Z")
    assert not api.refresh_due(now) and run(api.refresh(now)) is False
    for minute in range(0, 300, 60):
        pin_now(api, now + timedelta(minutes=minute))
        assert run(api.run(minute, minute == 0)) is True
    assert calls == [], calls
    status = api.base.entities["sensor.predbat_spotprice_status"]
    assert status["state"] == "error" and status["attributes"]["last_error"] == api.config_error, status
    assert api.health_message() == api.config_error
    assert not any("refresh failed" in line or "does not publish" in line for line in api.base.logs), api.base.logs
    assert len([line for line in api.base.logs if line.startswith("Error:")]) == 1

    # Provider energycharts cannot serve IE-SEM at all
    assert "use spotprice_provider entsoe" in make_api(provider="energycharts", entsoe_token=None, zone="IE-SEM").config_error

    # Tibber with spot export in IE-SEM and no token: keep Tibber import, drop the export once
    tib = make_api(provider="tibber", tibber_token="t", entsoe_token=None, zone="IE-SEM", export_mode="spot")
    assert tib.config_error is None and tib.export_mode == "none" and tib.sources_needed() == ["tibber"]

    # With a token, an ENTSO-E failure is recorded as such and Energy-Charts is not tried
    live = make_api(provider="entsoe", entsoe_token="token", zone="IE-SEM")
    assert live.config_error is None
    live.fetch_entsoe = entsoe
    live.fetch_energycharts = energycharts
    calls.clear()
    assert run(live.refresh(now)) is False
    assert calls == ["entsoe"], calls
    assert "ENTSO-E returned HTTP 503" in live.last_error and "Energy-Charts" not in live.last_error, live.last_error


def test_spotprice_config_error_ignores_cache(my_predbat=None):
    """Behind a config error cached prices are not restored, published or wired into the plan."""
    from spotprice import serialise_intervals

    storage = FakeStorage()
    cached = {"spot_intervals": serialise_intervals(spot_fixture(start="2025-05-01T22:00Z", count=192)), "spot_fetched_at": "2025-05-02T08:00:00+00:00"}
    storage.data[("spotprice", "energycharts_ie_sem")] = cached
    storage.data[("spotprice", "entsoe_ie_sem")] = cached
    for provider in ("energycharts", "entsoe"):
        api = make_api(provider=provider, entsoe_token=None, zone="IE-SEM", storage=storage)
        assert api.config_error
        pin_now(api, dt("2025-05-02T09:00Z"))
        assert run(api.run(0, True)) is True
        assert api.spot_intervals == [] and api.import_rates == []
        assert api.base.set_args == [], api.base.set_args
        assert "sensor.predbat_spotprice_import_rates" not in api.base.entities
        assert api.base.entities["sensor.predbat_spotprice_status"]["state"] == "error"
        assert api.last_success_timestamp is None
        # Even with prices somehow held, a config error never wires them
        api.spot_intervals = spot_fixture(start="2025-05-01T22:00Z", count=192)
        api.publish(dt("2025-05-02T09:00Z"))
        assert api.base.set_args == []


def test_spotprice_refresh_all_blocked(my_predbat=None):
    """refresh() with every source inside its back-off fetches nothing, returns False and logs no 'fetched'."""
    api = make_api(provider="energycharts", entsoe_token=None)
    calls = []

    async def fetch(start, end):
        """Must not be called."""
        calls.append(1)
        return spot_fixture()

    api.fetch_energycharts = fetch
    api.source_next_attempt["spot"] = dt("2025-05-02T10:00Z")
    assert run(api.refresh(dt("2025-05-02T09:00Z"))) is False
    assert calls == [] and not any("fetched" in line for line in api.base.logs), api.base.logs
    tib = make_api(provider="tibber", tibber_token="t", entsoe_token=None, zone="NL", export_mode="spot")
    tib.source_next_attempt = {"spot": dt("2025-05-02T10:00Z"), "tibber": dt("2025-05-02T10:00Z")}
    assert run(tib.refresh(dt("2025-05-02T09:00Z"))) is False and not any("fetched" in line for line in tib.base.logs)
    # Once the back-off expires the fetch goes ahead
    assert run(api.refresh(dt("2025-05-02T10:00Z"))) is True and calls == [1]


def test_spotprice_omitted_timestamps(my_predbat=None):
    """Omitted (not null) timestamps never get covered: intervals are capped at the source's resolution unless an hourly run confirms 60 minutes."""
    from spotprice import intervals_from_starts

    base = dt("2025-05-02T00:00Z")

    def m(minutes):
        """base + minutes."""
        return base + timedelta(minutes=minutes)

    def spans(intervals):
        """(start, end) minute offsets."""
        return [(int((s_ - base).total_seconds() // 60), int((e - base).total_seconds() // 60)) for s_, e, _v in intervals]

    # d4: the first three quarters of hour 1 omitted - 00:00 must not stretch to 01:00
    assert spans(intervals_from_starts([(m(0), 1.0), (m(60), 2.0), (m(75), 3.0), (m(90), 4.0)])) == [(0, 15), (60, 75), (75, 90), (90, 105)]
    # d5: two single quarters dropped - no "30 minute" intervals over them
    assert spans(intervals_from_starts([(m(0), 1.0), (m(15), 1.0), (m(45), 2.0), (m(75), 3.0), (m(90), 4.0), (m(105), 5.0)])) == [(0, 15), (15, 30), (45, 60), (75, 90), (90, 105), (105, 120)]
    # A genuine mixed 60/15 response keeps its hours, at both boundaries
    mixed = intervals_from_starts([(m(60 * h), 1.0) for h in range(3)] + [(m(180 + 15 * q), 9.0) for q in range(8)])
    assert spans(mixed)[:4] == [(0, 60), (60, 120), (120, 180), (180, 195)]
    tail = intervals_from_starts([(m(15 * q), 1.0) for q in range(8)] + [(m(120 + 60 * h), 2.0) for h in range(3)])
    assert spans(tail)[-3:] == [(120, 180), (180, 240), (240, 300)]
    # Tibber: the resolution asked for caps every interval - quarter-hourly query, entry omitted
    data = {"data": {"viewer": {"homes": [{"id": "h", "currentSubscription": {"priceInfo": {"today": [{"total": 0.2, "startsAt": "2025-05-02T02:00:00+02:00"}, {"total": 0.3, "startsAt": "2025-05-02T03:00:00+02:00"}], "tomorrow": []}}}]}}}
    quarter, _c, _h = parse_tibber_json(data, resolution_minutes=15)
    assert spans(quarter) == [(0, 15), (60, 75)], spans(quarter)
    hourly, _c, _h = parse_tibber_json(data, resolution_minutes=60)
    assert spans(hourly) == [(0, 60), (60, 120)], spans(hourly)

    # fetch_tibber passes the resolution it actually asked for
    api = make_api(provider="tibber", tibber_token="t", entsoe_token=None)
    replies = [{"errors": [{"message": 'Unknown argument "resolution"'}]}, data]

    async def post(url, payload, headers):
        """First reject the resolution argument, then answer the hourly query."""
        return 200, replies.pop(0)

    api.http_post_json = post
    assert spans(run(api.fetch_tibber())) == [(0, 60), (60, 120)]
    api2 = make_api(provider="tibber", tibber_token="t", entsoe_token=None)

    async def post_quarter(url, payload, headers):
        """Answer the quarter-hourly query straight away."""
        return 200, data

    api2.http_post_json = post_quarter
    assert spans(run(api2.fetch_tibber())) == [(0, 15), (60, 75)]


def test_spotprice_tibber_cache_key(my_predbat=None):
    """The tibber cache name carries a salted digest of the token and configured home: changing either never restores the old home's prices, and neither value is written."""
    import hashlib

    from spotprice import CACHE_DIGEST_SALT

    now = dt("2025-05-02T08:00Z")
    storage = FakeStorage()

    def tibber(**kwargs):
        """A tibber component on the shared storage."""
        config = {"provider": "tibber", "entsoe_token": None, "zone": None, "tibber_token": "token-old", "storage": storage}
        config.update(kwargs)
        return make_api(**config)

    old = tibber(tibber_home_id="home-a")
    expected = hashlib.sha256((CACHE_DIGEST_SALT + "token-old|home-a").encode("utf-8")).hexdigest()[:12]
    assert old.cache_filename() == "tibber_none_{}".format(expected), old.cache_filename()
    old.tibber_intervals = [(now, now + timedelta(hours=1), 0.3)]
    old.fetched["tibber"] = now
    run(old.save_cache())
    for changed in (tibber(tibber_home_id="home-b"), tibber(tibber_token="token-new", tibber_home_id="home-a"), tibber()):
        assert changed.cache_filename() != old.cache_filename()
        run(changed.load_cache())
        assert changed.tibber_intervals == [] and changed.fetched_at is None and changed.refresh_due(now)
    same = tibber(tibber_home_id="home-a")
    run(same.load_cache())
    assert same.tibber_intervals == old.tibber_intervals
    for (_module, filename), blob in storage.data.items():
        for secret in ("token-old", "home-a"):
            assert secret not in filename and secret not in repr(blob), filename
    # A home picked at run time does not move the cache mid-run
    picked = tibber()
    before = picked.cache_filename()
    picked.tibber_home_id = "home-found"
    assert picked.cache_filename() == before
    # Other providers keep the plain name
    assert make_api(provider="energycharts", entsoe_token=None, tibber_token="t").cache_filename() == "energycharts_de_lu"


# ---------------------------------------------------------------------------
# Ostrom
# ---------------------------------------------------------------------------

OSTROM_CONTRACTS = {
    "data": [
        {"id": 100523456, "type": "ELECTRICITY", "productCode": "SIMPLY_DYNAMIC", "status": "ACTIVE", "address": {"zip": "10997", "city": "Berlin"}},
        {"id": 100523999, "type": "ELECTRICITY", "productCode": "SIMPLY_FAIR", "status": "TERMINATED", "address": {"zip": "10997"}},
    ]
}


def ostrom_prices(start="2025-05-01T22:00:00.000Z", hours=48, spot=10.0, levies=19.28):
    """An Ostrom /spot-prices reply: hourly entries of grossKwhPrice + grossKwhTaxAndLevies (cents incl. VAT)."""
    first = dt(start)
    return {
        "data": [
            {"date": (first + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S.000Z"), "netMwhPrice": spot * 10 / 1.19, "netKwhPrice": spot / 1.19, "grossKwhPrice": spot + h, "netKwhTaxAndLevies": levies / 1.19, "grossKwhTaxAndLevies": levies}
            for h in range(hours)
        ]
    }


class FakeOstrom:
    """Answers http_request for the Ostrom token, contracts and spot-prices endpoints, recording every call."""

    def __init__(self, contracts=None, prices=None):
        """Set the replies; each may be replaced or queued (a list of (status, body)) by a test."""
        self.calls = []
        self.token_replies = []
        self.api_replies = {}
        self.contracts = contracts if contracts is not None else OSTROM_CONTRACTS
        self.prices = prices if prices is not None else ostrom_prices()
        self.issued = 0

    async def __call__(self, method, url, params=None, headers=None, data=None):
        """Route a request to the matching fake endpoint."""
        self.calls.append((method, url, params, headers, data))
        if url.endswith("/oauth2/token"):
            if self.token_replies:
                return self.token_replies.pop(0)
            self.issued += 1
            return 200, {"access_token": "tok-{}".format(self.issued), "token_type": "Bearer", "expires_in": 3600}
        path = url.split("ostrom-api.io", 1)[1]
        queued = self.api_replies.get(path)
        if queued:
            return queued.pop(0)
        return 200, self.contracts if path == "/contracts" else self.prices

    def count(self, suffix):
        """How many requests went to a URL ending in suffix."""
        return sum(1 for call in self.calls if call[1].endswith(suffix))


def make_ostrom(**kwargs):
    """An Ostrom-provider component with a FakeOstrom behind http_request."""
    config = {"provider": "ostrom", "entsoe_token": None, "zone": None, "ostrom_client_id": "cid", "ostrom_client_secret": "the-secret"}
    config.update(kwargs)
    api = make_api(**config)
    fake = FakeOstrom()
    api.http_request = fake
    return api, fake


def test_spotprice_ostrom_parse(my_predbat=None):
    """Ostrom's price is grossKwhPrice + grossKwhTaxAndLevies per hour; a missing field is a gap; a reply with no levies at all (no postcode) is rejected."""
    from spotprice import parse_ostrom_json

    intervals = parse_ostrom_json(ostrom_prices(hours=3, spot=10.0, levies=19.28))
    assert [(start, end) for start, end, _v in intervals][0] == (dt("2025-05-01T22:00Z"), dt("2025-05-01T23:00Z"))
    assert [value for _s, _e, value in intervals] == [29.28, 30.28, 31.28], intervals
    gap = ostrom_prices(hours=3)
    gap["data"][1]["grossKwhTaxAndLevies"] = None
    gapped = parse_ostrom_json(gap)
    assert [start for start, _e, _v in gapped] == [dt("2025-05-01T22:00Z"), dt("2025-05-02T00:00Z")], gapped
    assert gapped[0][1] == dt("2025-05-01T23:00Z")
    # Negative spot still adds the levies
    negative = ostrom_prices(hours=1, spot=-5.0, levies=19.0)
    assert parse_ostrom_json(negative)[0][2] == 14.0
    for bad in (ostrom_prices(hours=2, levies=0.0), {"data": [{"date": 12345, "grossKwhPrice": 1, "grossKwhTaxAndLevies": 1}]}, {"data": [{"grossKwhPrice": 1}]}, {"data": "x"}, None):
        try:
            parse_ostrom_json(bad)
        except SpotPriceError:
            continue
        raise AssertionError("expected SpotPriceError for {}".format(bad))
    assert parse_ostrom_json({"data": []}) == []


def test_spotprice_ostrom_contract_selection(my_predbat=None):
    """The single active dynamic contract is used; an explicit id picks one; fixed products, unknown ids, several candidates and a missing postcode are errors."""
    from spotprice import select_ostrom_contract

    assert select_ostrom_contract(OSTROM_CONTRACTS)["id"] == 100523456
    assert select_ostrom_contract(OSTROM_CONTRACTS, 100523456)["id"] == 100523456
    assert select_ostrom_contract(OSTROM_CONTRACTS, "100523456")["id"] == 100523456
    v2 = {"data": [{"id": 1, "productCode": "SimplyDynamic_V2", "status": "ACTIVE", "address": {"zip": "80331"}}]}
    assert select_ostrom_contract(v2)["id"] == 1
    two_active = {"data": [dict(OSTROM_CONTRACTS["data"][0]), dict(OSTROM_CONTRACTS["data"][0], id=7)]}
    fixed_active = {"data": [dict(OSTROM_CONTRACTS["data"][1], status="ACTIVE")]}
    # Only active contracts are priced: a terminated or pending one is never a stand-in, even when it is the only one
    only_inactive = {"data": [dict(OSTROM_CONTRACTS["data"][0], status="TERMINATED"), dict(OSTROM_CONTRACTS["data"][0], id=8, status="PENDING")]}
    cases = [
        ((fixed_active, None), "not a dynamic tariff"),
        ((OSTROM_CONTRACTS, 100523999), "100523999 is TERMINATED, not active"),
        ((only_inactive, None), "no active Ostrom contract"),
        (({"data": [dict(OSTROM_CONTRACTS["data"][0], status=None)]}, None), "no active Ostrom contract"),
        ((OSTROM_CONTRACTS, 42), "not found"),
        ((two_active, None), "spotprice_ostrom_contract_id"),
        (({"data": [dict(OSTROM_CONTRACTS["data"][0], address={})]}, None), "no postcode"),
        (({"data": []}, None), "no contracts"),
        (({"nope": 1}, None), "unexpected"),
    ]
    for args, phrase in cases:
        try:
            select_ostrom_contract(*args)
        except SpotPriceError as e:
            assert phrase in str(e), (phrase, str(e))
            continue
        raise AssertionError("expected an error containing '{}'".format(phrase))


def test_spotprice_ostrom_fetch_and_token_cache(my_predbat=None):
    """Client credentials are sent as Basic auth; the token is reused until shortly before expiry; contracts are read once; prices are asked for by postcode, hourly, in UTC."""
    import base64

    api, fake = make_ostrom()
    now = dt("2025-05-02T08:00Z")
    pin_now(api, now)
    intervals = run(api.fetch_supplier(now))
    assert len(intervals) == 48 and intervals[0][2] == 29.28
    method, url, _params, headers, data = fake.calls[0]
    assert method == "POST" and url == "https://auth.production.ostrom-api.io/oauth2/token"
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"cid:the-secret").decode("ascii")
    assert data == {"grant_type": "client_credentials"}
    assert fake.calls[1][1] == "https://production.ostrom-api.io/contracts" and fake.calls[1][3]["Authorization"] == "Bearer tok-1"
    _m, url, params, headers, _d = fake.calls[2]
    assert url == "https://production.ostrom-api.io/spot-prices" and headers["Authorization"] == "Bearer tok-1"
    assert params == {"startDate": "2025-04-30T22:00:00.000Z", "endDate": "2025-05-03T22:00:00.000Z", "resolution": "HOUR", "zip": "10997"}, params

    # Within the token's life: no new token and no second contracts lookup
    later = now + timedelta(minutes=50)
    pin_now(api, later)
    run(api.fetch_supplier(later))
    assert fake.count("/oauth2/token") == 1 and fake.count("/contracts") == 1 and fake.count("/spot-prices") == 2
    # Inside the renewal margin (expires_in 3600 less 60 seconds) a new token is requested
    renew = now + timedelta(seconds=3540)
    pin_now(api, renew)
    run(api.fetch_supplier(renew))
    assert fake.count("/oauth2/token") == 2 and fake.calls[-1][3]["Authorization"] == "Bearer tok-2"


def test_spotprice_ostrom_auth_errors(my_predbat=None):
    """A 401 from the API renews the token and retries once; a second 401, or rejected client credentials, is an auth_error SpotPriceError."""
    import spotprice as spotprice_module

    recorded = []
    original = spotprice_module.record_api_call
    spotprice_module.record_api_call = lambda service, success=True, reason=None: recorded.append((service, success, reason))
    try:
        now = dt("2025-05-02T08:00Z")
        api, fake = make_ostrom()
        pin_now(api, now)
        fake.api_replies["/contracts"] = [(401, {"type": "unauthorized", "detail": "Unauthorized API access."})]
        assert len(run(api.fetch_supplier(now))) == 48
        assert fake.count("/oauth2/token") == 2 and fake.count("/contracts") == 2

        api, fake = make_ostrom()
        pin_now(api, now)
        fake.api_replies["/contracts"] = [(401, None), (401, {"detail": "Unauthorized API access."})]
        recorded.clear()
        try:
            run(api.fetch_supplier(now))
            raise AssertionError("second 401 should raise")
        except SpotPriceError as e:
            assert "Ostrom rejected the access token (HTTP 401)" in str(e), str(e)
        assert ("ostrom", False, "auth_error") in recorded
        assert fake.count("/contracts") == 2, fake.calls

        for status in (400, 401):
            api, fake = make_ostrom(ostrom_client_secret="wrong-secret")
            pin_now(api, now)
            fake.token_replies = [(status, {"error": "invalid_client"})]
            recorded.clear()
            try:
                run(api.fetch_supplier(now))
                raise AssertionError("bad credentials should raise")
            except SpotPriceError as e:
                assert "client ID and secret" in str(e) and "wrong-secret" not in str(e), str(e)
            assert recorded == [("ostrom", False, "auth_error")], recorded

        for status, reason in ((429, "rate_limit"), (503, "server_error"), (400, "client_error")):
            api, fake = make_ostrom()
            pin_now(api, now)
            fake.api_replies["/spot-prices"] = [(status, {"detail": "nope"})]
            recorded.clear()
            try:
                run(api.fetch_supplier(now))
                raise AssertionError("HTTP {} should raise".format(status))
            except SpotPriceError:
                pass
            assert recorded[-1] == ("ostrom", False, reason), recorded

        api, fake = make_ostrom()
        pin_now(api, now)
        fake.token_replies = [(200, {"token_type": "Bearer"})]
        try:
            run(api.fetch_supplier(now))
            raise AssertionError("a token reply without access_token should raise")
        except SpotPriceError as e:
            assert "access_token" in str(e)
    finally:
        spotprice_module.record_api_call = original


def test_spotprice_ostrom_all_in_and_refresh(my_predbat=None):
    """Ostrom's price is used as-is (no markup, charge zones or VAT); failures back off on the supplier source only and the prices survive a restart."""
    storage = FakeStorage()
    api, fake = make_ostrom(markup=15, vat=0.19, charge_zones=[{"from": "00:00", "to": "00:00", "charge": 9}], storage=storage)
    assert api.sources_needed() == ["supplier"] and not api.needs_spot()
    now = dt("2025-05-02T08:00Z")
    pin_now(api, now)
    assert run(api.refresh(now)) is True
    assert api.fetched["supplier"] == now and api.data_end() == dt("2025-05-03T22:00Z")
    rates = api.build_import_rates()
    assert rates[0] == (dt("2025-05-01T22:00Z"), dt("2025-05-01T23:00Z"), 29.28), rates[0]
    api.publish(now)
    assert api.base.entities["sensor.predbat_spotprice_import_rates"]["state"] == 39.28
    assert api.base.entities["sensor.predbat_spotprice_status"]["state"] == "ok"
    assert ("metric_octopus_import", "sensor.predbat_spotprice_import_rates") in api.base.set_args

    restored, _fake = make_ostrom(storage=storage)
    run(restored.load_cache())
    assert restored.supplier_intervals == api.supplier_intervals and restored.fetched_at == now

    fake.api_replies["/spot-prices"] = [(503, None)]
    later = now + timedelta(hours=7)
    pin_now(api, later)
    assert run(api.refresh(later)) is False
    assert api.source_failures == {"spot": 0, "tibber": 0, "supplier": 1} and api.source_next_attempt["supplier"] == later + timedelta(minutes=5)
    assert "Ostrom returned HTTP 503" in api.last_error

    # With spot-linked export the spot source is fetched alongside, and its failure does not block Ostrom
    both, both_fake = make_ostrom(zone="DE-LU", export_mode="spot")
    assert both.sources_needed() == ["spot", "supplier"]

    async def spot_down(start, end):
        """Spot source down."""
        raise SpotPriceError("Energy-Charts returned HTTP 500")

    both.fetch_energycharts = spot_down
    pin_now(both, now)
    assert run(both.refresh(now)) is False
    assert both.fetched["supplier"] == now and both.source_failures["spot"] == 1 and both.source_failures["supplier"] == 0


def test_spotprice_ostrom_provider_inference(my_predbat=None):
    """With the provider unset the one supplier with credentials is used; credentials for two suppliers, or half an Ostrom pair, are one clear config error."""
    assert make_api(provider=None, entsoe_token=None, zone=None, ostrom_client_id="i", ostrom_client_secret="s").provider == "ostrom"
    assert make_api(provider=None, entsoe_token=None, zone="DE-LU", ostrom_client_id="i", ostrom_client_secret="s").provider == "ostrom"
    # An explicit provider stays the authority
    explicit = make_api(provider="energycharts", entsoe_token=None, ostrom_client_id="i", ostrom_client_secret="s", tibber_token="t")
    assert explicit.provider == "energycharts" and explicit.config_error is None
    both = make_api(provider=None, entsoe_token=None, zone="DE-LU", tibber_token="t", ostrom_client_id="i", ostrom_client_secret="s")
    assert both.config_error == "credentials for several suppliers (tibber, ostrom) are set, set spotprice_provider to choose one", both.config_error
    assert both.sources_needed() == [] and sum(line.startswith("Error:") for line in both.base.logs) == 1
    half = make_api(provider=None, entsoe_token=None, zone=None, ostrom_client_id="i")
    assert half.provider == "ostrom" and "spotprice_ostrom_client_secret" in half.config_error and half.sources_needed() == []


# ---------------------------------------------------------------------------
# Octopus Energy Germany
# ---------------------------------------------------------------------------


def make_jwt(exp):
    """An unsigned JWT whose payload carries the given exp (an aware datetime)."""
    import base64
    import json

    def encode(obj):
        """Base64url without padding."""
        return base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii").rstrip("=")

    return "{}.{}.sig".format(encode({"alg": "HS256", "typ": "JWT"}), encode({"exp": int(exp.timestamp())}))


def octopus_de_agreement(forecast=None, info=None, active=True, valid_from="2025-01-01T00:00:00+01:00", valid_to=None, product=None):
    """An agreements response holding one market location with one agreement (dynamic product when a forecast is given)."""
    agreement = {
        "id": "4711",
        "isActive": active,
        "isRevoked": False,
        "validFrom": valid_from,
        "validTo": valid_to,
        "product": {"code": product or ("DYNAMIC-OCTOPUS" if forecast else "OCTOPUS-FIX-12M")},
        "unitRateInformation": info or {"__typename": "SimpleProductUnitRateInformation", "latestGrossUnitRateCentsPerKwh": "31.5"},
        "unitRateForecast": forecast or [],
    }
    return {"data": {"account": {"properties": [{"electricityMalos": [{"maloNumber": "50000000001", "agreements": [agreement]}]}]}}}


def octopus_de_forecast(start="2025-05-01T22:00:00+00:00", count=8, minutes=15, base=20.0):
    """unitRateForecast entries, one per slot, with gross rates as strings (as Kraken sends them)."""
    first = dt(start)
    return [
        {
            "validFrom": (first + timedelta(minutes=minutes * i)).isoformat(),
            "validTo": (first + timedelta(minutes=minutes * (i + 1))).isoformat(),
            "unitRateInformation": {"__typename": "TimeOfUseProductUnitRateInformation", "rates": [{"latestGrossUnitRateCentsPerKwh": "{:.2f}".format(base + i)}]},
        }
        for i in range(count)
    ]


class FakeKraken:
    """Answers http_post_json for the token mutation, the accounts lookup and the agreements query."""

    def __init__(self, agreements=None, accounts=("A-1234ABCD",)):
        """Set the replies; queued (status, body) tuples per operation override them."""
        self.calls = []
        self.queued = {"token": [], "accounts": [], "agreements": []}
        self.agreements = agreements or octopus_de_agreement(forecast=octopus_de_forecast())
        self.accounts = list(accounts)
        self.issued = 0
        self.token_exp = dt("2025-05-02T09:00Z")

    async def __call__(self, url, payload, headers):
        """Route by the GraphQL operation."""
        query = payload["query"]
        operation = "token" if "obtainKrakenToken" in query else ("accounts" if "viewer" in query else "agreements")
        self.calls.append((operation, url, payload, headers))
        if self.queued[operation]:
            return self.queued[operation].pop(0)
        if operation == "token":
            self.issued += 1
            return 200, {"data": {"obtainKrakenToken": {"token": make_jwt(self.token_exp)}}}
        if operation == "accounts":
            return 200, {"data": {"viewer": {"accounts": [{"number": number} for number in self.accounts]}}}
        return 200, self.agreements

    def count(self, operation):
        """How many requests of one operation were made."""
        return sum(1 for call in self.calls if call[0] == operation)


def make_octopus_de(**kwargs):
    """An octopus_de-provider component with a FakeKraken behind http_post_json."""
    config = {"provider": "octopus_de", "entsoe_token": None, "zone": None, "octopus_de_api_key": "sk_live_test"}
    config.update(kwargs)
    api = make_api(**config)
    fake = FakeKraken()
    api.http_post_json = fake
    return api, fake


def kraken_error(code, description="error"):
    """A Kraken GraphQL error reply (HTTP 200)."""
    return 200, {"errors": [{"message": description, "extensions": {"errorCode": code, "errorDescription": description}}], "data": None}


def test_spotprice_octopus_de_dynamic(my_predbat=None):
    """The API key is exchanged for a JWT sent without Bearer; the account is looked up once; dynamic forecast slots are used as-is with no markup or VAT."""
    from spotprice import OCTOPUS_DE_URL

    api, fake = make_octopus_de(markup=15, vat=0.19, charge_zones=[{"from": "00:00", "to": "00:00", "charge": 9}])
    assert api.sources_needed() == ["supplier"]
    now = dt("2025-05-02T08:00Z")
    pin_now(api, now)
    assert run(api.refresh(now)) is True
    operation, url, payload, headers = fake.calls[0]
    assert operation == "token" and url == OCTOPUS_DE_URL and payload["variables"] == {"input": {"APIKey": "sk_live_test"}} and "Authorization" not in headers
    assert [call[0] for call in fake.calls] == ["token", "accounts", "agreements"]
    token = fake.calls[1][3]["Authorization"]
    assert token == make_jwt(fake.token_exp) and not token.startswith("Bearer")
    assert fake.calls[2][2]["variables"] == {"accountNumber": "A-1234ABCD"}
    rates = api.build_import_rates()
    assert [rate for _s, _e, rate in rates] == [20.0 + i for i in range(8)], rates
    assert rates[0][:2] == (dt("2025-05-01T22:00Z"), dt("2025-05-01T22:15Z"))
    # Second refresh: the token is still valid and the account is known
    pin_now(api, now + timedelta(minutes=30))
    run(api.refresh(now + timedelta(minutes=30)))
    assert fake.count("token") == 1 and fake.count("accounts") == 1 and fake.count("agreements") == 2
    # An explicit account skips the lookup
    explicit, explicit_fake = make_octopus_de(octopus_de_account=" A-9999 ")
    pin_now(explicit, now)
    run(explicit.fetch_supplier(now))
    assert explicit_fake.count("accounts") == 0 and explicit_fake.calls[-1][2]["variables"] == {"accountNumber": "A-9999"}


def test_spotprice_octopus_de_fixed_and_time_of_use(my_predbat=None):
    """Without a forecast, a fixed rate spans the window (clipped to the agreement) and a time-of-use table repeats daily in local time, wrapping past midnight and across DST."""
    from spotprice import parse_octopus_de_agreement

    start, end = dt("2025-05-01T22:00Z"), dt("2025-05-03T22:00Z")
    fixed = octopus_de_agreement(valid_to="2025-05-03T00:00:00+02:00")["data"]["account"]["properties"][0]["electricityMalos"][0]["agreements"][0]
    assert parse_octopus_de_agreement(fixed, start, end, BERLIN) == [(start, dt("2025-05-02T22:00Z"), 31.5)]

    tou_info = {
        "__typename": "TimeOfUseProductUnitRateInformation",
        "rates": [
            {"latestGrossUnitRateCentsPerKwh": "22.0", "timeslotActivationRules": [{"activeFromTime": "22:00:00", "activeToTime": "06:00:00"}]},
            {"latestGrossUnitRateCentsPerKwh": "35.0", "timeslotActivationRules": [{"activeFromTime": "06:00:00", "activeToTime": "22:00:00"}]},
        ],
    }
    tou = octopus_de_agreement(info=tou_info)["data"]["account"]["properties"][0]["electricityMalos"][0]["agreements"][0]
    intervals = parse_octopus_de_agreement(tou, start, end, BERLIN)
    # 00:00-06:00 local (the tail of the previous evening's slot), then 06-22 and 22-06 alternating
    assert intervals[0] == (dt("2025-05-01T22:00Z"), dt("2025-05-02T04:00Z"), 22.0), intervals[0]
    assert intervals[1] == (dt("2025-05-02T04:00Z"), dt("2025-05-02T20:00Z"), 35.0)
    assert intervals[2] == (dt("2025-05-02T20:00Z"), dt("2025-05-03T04:00Z"), 22.0)
    assert intervals[-1][1] == end and len(intervals) == 5, intervals
    # Every minute of the window is covered exactly once
    assert all(intervals[i][1] == intervals[i + 1][0] for i in range(len(intervals) - 1))
    # Autumn DST: the night slot 22:00-06:00 local is 9 hours long that night
    dst = parse_octopus_de_agreement(tou, dt("2025-10-25T22:00Z"), dt("2025-10-26T23:00Z"), BERLIN)
    assert (dt("2025-10-25T20:00Z"), dt("2025-10-26T05:00Z")) not in [(s_, e) for s_, e, _v in dst]
    assert dst[0] == (dt("2025-10-25T22:00Z"), dt("2025-10-26T05:00Z"), 22.0), dst[0]
    assert dst[1] == (dt("2025-10-26T05:00Z"), dt("2025-10-26T21:00Z"), 35.0), dst[1]

    # An agreement that has ended before the window, or one with no rate at all
    ended = dict(fixed, validTo="2025-04-01T00:00:00+02:00")
    assert parse_octopus_de_agreement(ended, start, end, BERLIN) == []
    for bad in (
        dict(fixed, unitRateInformation={"__typename": "SimpleProductUnitRateInformation", "latestGrossUnitRateCentsPerKwh": None}),
        dict(tou, unitRateInformation={"__typename": "TimeOfUseProductUnitRateInformation", "rates": []}),
        dict(fixed, validFrom=12345),
    ):
        try:
            parse_octopus_de_agreement(bad, start, end, BERLIN)
        except SpotPriceError:
            continue
        raise AssertionError("expected SpotPriceError for {}".format(bad))


def test_spotprice_octopus_de_auth_and_errors(my_predbat=None):
    """The JWT is reused until shortly before its exp; a rejected token is replaced once; a bad key, rate limit, HTTP failure, several accounts or no active agreement are SpotPriceErrors with their category."""
    import spotprice as spotprice_module

    recorded = []
    original = spotprice_module.record_api_call
    spotprice_module.record_api_call = lambda service, success=True, reason=None: recorded.append((service, success, reason))
    try:
        now = dt("2025-05-02T08:00Z")
        api, fake = make_octopus_de()
        pin_now(api, now)
        run(api.fetch_supplier(now))
        pin_now(api, dt("2025-05-02T08:58Z"))
        run(api.fetch_supplier(dt("2025-05-02T08:58Z")))
        assert fake.count("token") == 1
        pin_now(api, dt("2025-05-02T08:59:30Z"))  # inside the 60 second margin before exp
        run(api.fetch_supplier(dt("2025-05-02T08:59:30Z")))
        assert fake.count("token") == 2

        # A token the API no longer accepts is replaced and the query retried once
        pin_now(api, now)
        fake.queued["agreements"] = [kraken_error("KT-CT-1124", "Token has expired.")]
        assert len(run(api.fetch_supplier(now))) == 8 and fake.count("token") == 3
        fake.queued["agreements"] = [kraken_error("KT-CT-1124"), kraken_error("KT-CT-1124", "Token has expired.")]
        recorded.clear()
        try:
            run(api.fetch_supplier(now))
            raise AssertionError("repeated token error should raise")
        except SpotPriceError as e:
            assert "Token has expired" in str(e)
        assert recorded[-1] == ("octopus_de", False, "auth_error")

        cases = [
            ("token", kraken_error("KT-CT-1139", "Authentication failed."), "rejected the API key", "auth_error"),
            ("token", kraken_error("KT-CT-1199", "Too many requests."), "rate limit", "rate_limit"),
            ("token", (502, None), "HTTP 502", "server_error"),
            ("token", (200, {"data": {"obtainKrakenToken": None}}), "did not issue a token", "decode_error"),
            ("agreements", kraken_error("KT-CT-4123", "Unauthorized."), "agreements query failed", "client_error"),
            ("agreements", (200, octopus_de_agreement(active=False)), "no active electricity agreement", "client_error"),
            ("agreements", (200, {"data": {"account": None}}), "account not found", "client_error"),
        ]
        for operation, reply, phrase, reason in cases:
            api, fake = make_octopus_de(octopus_de_api_key="sk_live_secret")
            pin_now(api, now)
            fake.queued[operation] = [reply]
            recorded.clear()
            try:
                run(api.fetch_supplier(now))
                raise AssertionError("expected an error for {}".format(phrase))
            except SpotPriceError as e:
                assert phrase in str(e) and "sk_live_secret" not in str(e), (phrase, str(e))
            assert recorded[-1] == ("octopus_de", False, reason), (phrase, recorded)

        several, several_fake = make_octopus_de()
        several_fake.accounts = ["A-1", "A-2"]
        pin_now(several, now)
        try:
            run(several.fetch_supplier(now))
            raise AssertionError("several accounts should raise")
        except SpotPriceError as e:
            assert "A-1, A-2" in str(e) and "spotprice_octopus_de_account" in str(e)

        # A refresh failure backs off the supplier source like any other
        failing, failing_fake = make_octopus_de()
        failing_fake.queued["token"] = [kraken_error("KT-CT-1139", "Authentication failed.")]
        assert run(failing.refresh(now)) is False and failing.source_failures["supplier"] == 1 and "rejected the API key" in failing.last_error
    finally:
        spotprice_module.record_api_call = original


def test_spotprice_octopus_de_inference_and_config(my_predbat=None):
    """An Octopus Energy Germany API key alone selects octopus_de; with another supplier's credentials the provider must be set; the provider without a key is a config error."""
    from spotprice import jwt_expiry

    assert make_api(provider=None, entsoe_token=None, zone=None, octopus_de_api_key="k").provider == "octopus_de"
    both = make_api(provider=None, entsoe_token=None, zone=None, octopus_de_api_key="k", tibber_token="t")
    assert "tibber, octopus_de" in both.config_error
    assert make_api(provider="octopus_de", entsoe_token=None, zone=None, octopus_de_api_key="k", tibber_token="t").config_error is None
    assert make_api(provider="octopus_de", entsoe_token=None, zone=None).config_error == "spotprice_octopus_de_api_key is required for provider octopus_de"
    assert jwt_expiry(make_jwt(dt("2025-05-02T09:00Z"))) == dt("2025-05-02T09:00Z")
    assert jwt_expiry("not-a-jwt") is None


# ---------------------------------------------------------------------------
# aWATTar / tado Energy
# ---------------------------------------------------------------------------


def awattar_reply(start="2025-05-01T22:00Z", hours=48, price=100.0, unit="Eur/MWh"):
    """An aWATTar /v1/marketdata reply: hourly entries with millisecond start/end timestamps."""
    first = int(dt(start).timestamp() * 1000)
    return {"object": "list", "data": [{"start_timestamp": first + 3600000 * h, "end_timestamp": first + 3600000 * (h + 1), "marketprice": price + h, "unit": unit} for h in range(hours)], "url": "/de/v1/marketdata"}


def test_spotprice_awattar_parse_and_fetch(my_predbat=None):
    """aWATTar's hourly EUR/MWh prices parse with their own start/end, are asked for by zone host and millisecond window, and failures map to SpotPriceError categories."""
    import spotprice as spotprice_module
    from spotprice import parse_awattar_json

    intervals = parse_awattar_json(awattar_reply(hours=3))
    assert intervals == [(dt("2025-05-01T22:00Z"), dt("2025-05-01T23:00Z"), 100.0), (dt("2025-05-01T23:00Z"), dt("2025-05-02T00:00Z"), 101.0), (dt("2025-05-02T00:00Z"), dt("2025-05-02T01:00Z"), 102.0)]
    with_null = awattar_reply(hours=3)
    with_null["data"][1]["marketprice"] = None
    assert [start for start, _e, _v in parse_awattar_json(with_null)] == [dt("2025-05-01T22:00Z"), dt("2025-05-02T00:00Z")]
    for bad in (awattar_reply(hours=1, unit="Eur/kWh"), {"data": [{"start_timestamp": "x", "end_timestamp": 1, "marketprice": 1}]}, {"data": [{"start_timestamp": 2000, "end_timestamp": 1000, "marketprice": 1}]}, {"object": "list"}, None):
        try:
            parse_awattar_json(bad)
        except SpotPriceError:
            continue
        raise AssertionError("expected SpotPriceError for {}".format(bad))

    seen = []
    for zone, host in (("DE-LU", "https://api.awattar.de/v1/marketdata"), ("AT", "https://api.awattar.at/v1/marketdata")):
        api = make_api(provider="awattar", entsoe_token=None, zone=zone)

        async def get(url, params, expect_json):
            """Return the fixture."""
            seen.append((url, params))
            return 200, awattar_reply()

        api.http_get = get
        fetched = run(api.fetch_awattar(dt("2025-05-01T22:00Z"), dt("2025-05-02T22:00Z")))
        assert len(fetched) == 24, len(fetched)  # trimmed to the window
        assert seen[-1] == (host, {"start": int(dt("2025-05-01T22:00Z").timestamp() * 1000), "end": int(dt("2025-05-02T22:00Z").timestamp() * 1000)}), seen[-1]

    recorded = []
    original = spotprice_module.record_api_call
    spotprice_module.record_api_call = lambda service, success=True, reason=None: recorded.append((service, success, reason))
    try:
        for reply, reason in (((429, None), "rate_limit"), ((503, None), "server_error"), ((404, None), "client_error"), ((200, {"data": []}), "client_error"), ((200, {"nope": 1}), "decode_error")):
            api = make_api(provider="awattar", entsoe_token=None, zone="DE-LU")

            async def bad_get(url, params, expect_json, reply=reply):
                """Return an error reply."""
                return reply

            api.http_get = bad_get
            recorded.clear()
            try:
                run(api.fetch_awattar(dt("2025-05-01T22:00Z"), dt("2025-05-02T22:00Z")))
                raise AssertionError("expected an error for {}".format(reply))
            except SpotPriceError:
                pass
            assert recorded == [("awattar", False, reason)], (reply, recorded)
    finally:
        spotprice_module.record_api_call = original


def test_spotprice_awattar_markup_percent(my_predbat=None):
    """The percentage markup is charged on the absolute spot price before VAT; awattar defaults it to 3, an explicit value (even 0) wins, other providers default to 0."""
    assert spot_import_rate(100.0, 1.5, 0.0, 0.19, markup_percent=3.0) == 14.042
    # Negative spot: 3% of |spot| is still a cost
    assert spot_import_rate(-50.0, 1.5, 0.0, 0.19, markup_percent=3.0) == -3.9865
    assert spot_import_rate(100.0, 1.5, 0.0, 0.19) == 13.685

    api = make_api(provider="awattar", entsoe_token=None, markup=1.5, vat=0.19, charge_zones=[{"from": "00:00", "to": "00:00", "charge": 10.0}])
    assert api.markup_percent == 3.0 and api.sources_needed() == ["spot"]
    api.spot_intervals = [(dt("2025-05-02T10:00Z"), dt("2025-05-02T11:00Z"), 100.0), (dt("2025-05-02T11:00Z"), dt("2025-05-02T12:00Z"), -50.0)]
    assert [rate for _s, _e, rate in api.build_import_rates()] == [25.942, 7.9135], api.build_import_rates()
    assert make_api(provider="awattar", entsoe_token=None, markup_percent=0).markup_percent == 0.0
    assert make_api(provider="energycharts", entsoe_token=None).markup_percent == 0.0
    linked = make_api(provider="entsoe", markup=0, vat=0, markup_percent=10)
    linked.spot_intervals = [(dt("2025-05-02T10:00Z"), dt("2025-05-02T11:00Z"), 100.0)]
    assert linked.build_import_rates()[0][2] == 11.0
    # Export is not touched by the import markup percentage
    linked.export_mode = "spot"
    assert linked.build_export_rates()[0][2] == 10.0


def test_spotprice_awattar_fallback(my_predbat=None):
    """aWATTar falls back to ENTSO-E (with a token) and then Energy-Charts, logging the outage and the recovery once; zones aWATTar does not publish go straight to the fallbacks; awattar is never inferred."""
    calls = []

    def source(name, fail):
        """A fake fetch that records its name and fails or returns spot prices."""

        async def fetch(start, end):
            """Fake fetch."""
            calls.append(name)
            if fail["on"]:
                raise SpotPriceError("{} down".format(name))
            return spot_fixture()

        return fetch

    awattar_state = {"on": True}
    api = make_api(provider="awattar", entsoe_token=None, zone="AT")
    api.fetch_awattar = source("awattar", awattar_state)
    api.fetch_energycharts = source("energycharts", {"on": False})
    assert run(api.refresh(dt("2025-05-02T08:00Z"))) is True
    assert calls == ["awattar", "energycharts"] and api.spot_source == "energycharts"
    assert run(api.refresh(dt("2025-05-02T14:00Z"))) is True
    assert sum("until aWATTar recovers" in line for line in api.base.logs) == 1, api.base.logs
    awattar_state["on"] = False
    assert run(api.refresh(dt("2025-05-02T20:00Z"))) is True and api.spot_source == "awattar"
    assert sum("aWATTar is working again" in line for line in api.base.logs) == 1

    calls.clear()
    with_token = make_api(provider="awattar", entsoe_token="t", zone="DE-LU")
    with_token.fetch_awattar = source("awattar", {"on": True})
    with_token.fetch_entsoe = source("entsoe", {"on": True})
    with_token.fetch_energycharts = source("energycharts", {"on": False})
    assert run(with_token.refresh(dt("2025-05-02T08:00Z"))) is True
    assert calls == ["awattar", "entsoe", "energycharts"], calls

    calls.clear()
    elsewhere = make_api(provider="awattar", entsoe_token=None, zone="NL")
    assert any("aWATTar publishes prices for DE-LU and AT only" in line for line in elsewhere.base.logs)
    elsewhere.fetch_awattar = source("awattar", {"on": False})
    elsewhere.fetch_energycharts = source("energycharts", {"on": False})
    assert run(elsewhere.refresh(dt("2025-05-02T08:00Z"))) is True and calls == ["energycharts"]

    assert make_api(provider=None, entsoe_token=None, zone="DE-LU").provider == "energycharts"


# ---------------------------------------------------------------------------
# EWS Schönau
# ---------------------------------------------------------------------------


def ews_reply(start="2025-05-02T00:00:00.000+02:00", quarters=8, base=30.0, tomorrow=0):
    """An EWS dynamic prices reply: today/tomorrow lists of quarter-hour {startsAt, total} with total in cents incl. VAT."""
    first = datetime.fromisoformat(start)

    def entries(offset, count):
        """count quarter hours from first + offset quarters."""
        return [{"startsAt": (first + timedelta(minutes=15 * (offset + i))).isoformat(timespec="milliseconds"), "total": base + offset + i} for i in range(count)]

    return {"today": entries(0, quarters), "tomorrow": entries(96, tomorrow)}


def test_spotprice_ews_parse(my_predbat=None):
    """EWS totals are quarter-hourly cents incl. VAT across today and tomorrow; a null total is a gap; euro-looking or malformed replies are rejected."""
    from spotprice import parse_ews_json

    intervals = parse_ews_json(ews_reply(quarters=3, tomorrow=2))
    assert [(start, end) for start, end, _v in intervals][:2] == [(dt("2025-05-01T22:00Z"), dt("2025-05-01T22:15Z")), (dt("2025-05-01T22:15Z"), dt("2025-05-01T22:30Z"))]
    assert [value for _s, _e, value in intervals] == [30.0, 31.0, 32.0, 126.0, 127.0], intervals
    assert intervals[3][0] == dt("2025-05-02T22:00Z")
    gap = ews_reply(quarters=3)
    gap["today"][1]["total"] = None
    assert [start for start, _e, _v in parse_ews_json(gap)] == [dt("2025-05-01T22:00Z"), dt("2025-05-01T22:30Z")]
    # Omitted quarters are not covered by their neighbour
    omitted = ews_reply(quarters=4)
    del omitted["today"][1]
    assert parse_ews_json(omitted)[0][1] == dt("2025-05-01T22:15Z")
    euros = ews_reply(quarters=4)
    for entry in euros["today"]:
        entry["total"] = 0.3
    for bad in (euros, {"today": [{"startsAt": 1746136800, "total": 30}]}, {"today": [{"total": 30}]}, {"today": "x"}, None):
        try:
            parse_ews_json(bad)
        except SpotPriceError:
            continue
        raise AssertionError("expected SpotPriceError for {}".format(bad))
    assert parse_ews_json({"today": [], "tomorrow": None}) == []


def test_spotprice_ews_fetch(my_predbat=None):
    """fetch_ews sends the API key as X-API-Key, uses the totals as-is (no markup or VAT), and maps failures to SpotPriceError categories with per-source back-off."""
    import spotprice as spotprice_module

    seen = []
    api = make_api(provider="ews", entsoe_token=None, zone=None, ews_api_key="pub_dpa_test", markup=15, vat=0.19, charge_zones=[{"from": "00:00", "to": "00:00", "charge": 9}])

    async def request(method, url, params=None, headers=None, data=None):
        """Return the fixture."""
        seen.append((method, url, headers))
        return 200, ews_reply(quarters=96, tomorrow=96)

    api.http_request = request
    now = dt("2025-05-02T08:00Z")
    pin_now(api, now)
    assert api.sources_needed() == ["supplier"]
    assert run(api.refresh(now)) is True
    assert seen == [("GET", "https://api.ews-schoenau.de/v1/dynamicprices/EWS-OEKO-DYN", {"X-API-Key": "pub_dpa_test", "Accept": "application/json"})], seen
    rates = api.build_import_rates()
    assert len(rates) == 192 and rates[0] == (dt("2025-05-01T22:00Z"), dt("2025-05-01T22:15Z"), 30.0)
    assert api.data_end() == dt("2025-05-03T22:00Z")

    recorded = []
    original = spotprice_module.record_api_call
    spotprice_module.record_api_call = lambda service, success=True, reason=None: recorded.append((service, success, reason))
    try:
        for reply, phrase, reason in (
            ((401, {"error": {"title": "Unauthorized", "status": 401, "detail": "Missing authorization"}}), "EWS rejected the API key (HTTP 401): Missing authorization", "auth_error"),
            ((429, None), "rate limit", "rate_limit"),
            ((500, None), "HTTP 500", "server_error"),
            ((200, {"today": [], "tomorrow": []}), "no prices", "client_error"),
            ((200, {"today": [{"startsAt": "2025-05-02T00:00:00.000+02:00", "total": 0.3}]}), "euros", "decode_error"),
        ):
            failing = make_api(provider="ews", entsoe_token=None, zone=None, ews_api_key="pub_dpa_secret")

            async def bad(method, url, params=None, headers=None, data=None, reply=reply):
                """Return an error reply."""
                return reply

            failing.http_request = bad
            recorded.clear()
            assert run(failing.refresh(now)) is False
            assert phrase in failing.last_error and "pub_dpa_secret" not in failing.last_error, (phrase, failing.last_error)
            assert recorded == [("ews", False, reason)], (phrase, recorded)
            assert failing.source_failures["supplier"] == 1 and failing.source_next_attempt["supplier"] == now + timedelta(minutes=5)

        async def network(method, url, params=None, headers=None, data=None):
            """A connection failure."""
            raise asyncio.TimeoutError()

        offline = make_api(provider="ews", entsoe_token=None, zone=None, ews_api_key="k")
        offline.http_request = network
        recorded.clear()
        assert run(offline.refresh(now)) is False and recorded == [("ews", False, "connection_error")] and "EWS request failed" in offline.last_error
    finally:
        spotprice_module.record_api_call = original


def test_spotprice_ews_inference_and_config(my_predbat=None):
    """An EWS API key alone selects ews; with another supplier's key the provider must be set; provider ews without a key is a config error."""
    assert make_api(provider=None, entsoe_token=None, zone=None, ews_api_key="k").provider == "ews"
    assert "octopus_de, ews" in make_api(provider=None, entsoe_token=None, zone=None, ews_api_key="k", octopus_de_api_key="o").config_error
    missing = make_api(provider="ews", entsoe_token=None, zone=None)
    assert "spotprice_ews_api_key is required" in missing.config_error and missing.sources_needed() == []


def agreement_of(response, index=0):
    """The index-th agreement of the first market location of an agreements response."""
    return response["data"]["account"]["properties"][0]["electricityMalos"][0]["agreements"][index]


TOU_DAY_NIGHT = {
    "__typename": "TimeOfUseProductUnitRateInformation",
    "rates": [
        {"latestGrossUnitRateCentsPerKwh": "22.0", "timeslotActivationRules": [{"activeFromTime": "22:00:00", "activeToTime": "06:00:00"}]},
        {"latestGrossUnitRateCentsPerKwh": "35.0", "timeslotActivationRules": [{"activeFromTime": "06:00:00", "activeToTime": "22:00:00"}]},
    ],
}


def test_spotprice_octopus_de_forecast_time_of_use(my_predbat=None):
    """A forecast entry carrying several time-of-use rates is expanded by their rules over that entry's period, never collapsed to one rate; forecast slots are clipped to the agreement's validity."""
    from spotprice import parse_octopus_de_agreement

    # One forecast entry for the whole of 2 May (Berlin) holding a night and a day rate
    entry = {"validFrom": "2025-05-02T00:00:00+02:00", "validTo": "2025-05-03T00:00:00+02:00", "unitRateInformation": TOU_DAY_NIGHT}
    agreement = agreement_of(octopus_de_agreement(forecast=[entry]))
    intervals = parse_octopus_de_agreement(agreement, dt("2025-05-01T22:00Z"), dt("2025-05-03T22:00Z"))
    assert intervals == [
        (dt("2025-05-01T22:00Z"), dt("2025-05-02T04:00Z"), 22.0),
        (dt("2025-05-02T04:00Z"), dt("2025-05-02T20:00Z"), 35.0),
        (dt("2025-05-02T20:00Z"), dt("2025-05-02T22:00Z"), 22.0),
    ], intervals
    # A single rate without rules covers its whole entry
    single = {"validFrom": "2025-05-02T00:00:00+02:00", "validTo": "2025-05-02T00:15:00+02:00", "unitRateInformation": {"__typename": "TimeOfUseProductUnitRateInformation", "rates": [{"latestGrossUnitRateCentsPerKwh": "27.5"}]}}
    assert parse_octopus_de_agreement(agreement_of(octopus_de_agreement(forecast=[single])), dt("2025-05-01T22:00Z"), dt("2025-05-03T22:00Z")) == [(dt("2025-05-01T22:00Z"), dt("2025-05-01T22:15Z"), 27.5)]
    # Several rates where one cannot be placed in time are an error, not a guess
    no_timeslot = dict(entry, unitRateInformation={"__typename": "TimeOfUseProductUnitRateInformation", "rates": [{"latestGrossUnitRateCentsPerKwh": "22.0"}, TOU_DAY_NIGHT["rates"][1]]})
    try:
        parse_octopus_de_agreement(agreement_of(octopus_de_agreement(forecast=[no_timeslot])), dt("2025-05-01T22:00Z"), dt("2025-05-03T22:00Z"))
        raise AssertionError("rates without timeslots should raise")
    except SpotPriceError as e:
        assert "without a price or timeslot" in str(e), str(e)

    # 30 minute forecast slots across an agreement that starts at 22:15 and ends at 22:45
    slots = octopus_de_forecast(start="2025-05-01T22:00:00+00:00", count=2, minutes=30)
    clipped = agreement_of(octopus_de_agreement(forecast=slots, valid_from="2025-05-01T22:15:00+00:00", valid_to="2025-05-01T22:45:00+00:00"))
    assert parse_octopus_de_agreement(clipped, dt("2025-05-01T22:00Z"), dt("2025-05-03T22:00Z")) == [
        (dt("2025-05-01T22:15Z"), dt("2025-05-01T22:30Z"), 20.0),
        (dt("2025-05-01T22:30Z"), dt("2025-05-01T22:45Z"), 21.0),
    ]


def test_spotprice_octopus_de_dynamic_without_forecast(my_predbat=None):
    """A dynamic agreement with no forecast publishes nothing - never its flat unitRateInformation - and keeps re-polling with an error."""
    from spotprice import octopus_de_is_dynamic, parse_octopus_de_agreement

    dynamic = octopus_de_agreement(product="DYNAMIC-OCTOPUS-24M")
    assert octopus_de_is_dynamic(agreement_of(dynamic)) and not octopus_de_is_dynamic(agreement_of(octopus_de_agreement()))
    assert octopus_de_is_dynamic(agreement_of(octopus_de_agreement(forecast=octopus_de_forecast(), product="SOMETHING-ELSE")))
    assert parse_octopus_de_agreement(agreement_of(dynamic), dt("2025-05-01T22:00Z"), dt("2025-05-03T22:00Z")) == []
    api, fake = make_octopus_de()
    fake.agreements = dynamic
    now = dt("2025-05-02T08:00Z")
    pin_now(api, now)
    assert run(api.refresh(now)) is False
    assert "has no forecast prices yet" in api.last_error and api.supplier_intervals == []
    api.publish(now)
    assert "sensor.predbat_spotprice_import_rates" not in api.base.entities
    assert api.base.entities["sensor.predbat_spotprice_status"]["state"] == "error"
    assert api.source_next_attempt["supplier"] == now + timedelta(minutes=5)
    # Once the forecast appears the next poll publishes it
    fake.agreements = octopus_de_agreement(forecast=octopus_de_forecast(start="2025-05-02T08:00:00+00:00"), product="DYNAMIC-OCTOPUS-24M")
    later = api.source_next_attempt["supplier"]
    pin_now(api, later)
    assert run(api.refresh(later)) is True and len(api.supplier_intervals) == 8


def test_spotprice_octopus_de_market_location_selection(my_predbat=None):
    """Active agreements on several market locations need spotprice_octopus_de_malo; the chosen location hands over to its next agreement inside the window; revoked agreements are skipped."""
    from spotprice import select_octopus_de_agreements

    fixed = agreement_of(octopus_de_agreement(valid_to="2025-05-03T00:00:00+02:00"))
    following = dict(fixed, id="4712", isActive=False, validFrom="2025-05-03T00:00:00+02:00", validTo=None, unitRateInformation={"__typename": "SimpleProductUnitRateInformation", "latestGrossUnitRateCentsPerKwh": "28.0"})
    revoked = dict(following, id="4713", isRevoked=True, unitRateInformation={"__typename": "SimpleProductUnitRateInformation", "latestGrossUnitRateCentsPerKwh": "99.0"})
    expired = dict(fixed, id="4700", isActive=False, validFrom="2024-01-01T00:00:00+01:00", validTo="2025-01-01T00:00:00+01:00")
    heat_pump = dict(fixed, id="5001", validTo=None, unitRateInformation=TOU_DAY_NIGHT)
    response = {
        "data": {
            "account": {
                "properties": [
                    {"electricityMalos": [{"maloNumber": "50000000001", "agreements": [expired, revoked, following, fixed]}]},
                    {"electricityMalos": [{"maloNumber": "50000000002", "agreements": [heat_pump]}]},
                ]
            }
        }
    }
    try:
        select_octopus_de_agreements(response)
        raise AssertionError("two market locations should raise")
    except SpotPriceError as e:
        assert "50000000001, 50000000002" in str(e) and "spotprice_octopus_de_malo" in str(e), str(e)
    malo, chain = select_octopus_de_agreements(response, "50000000001")
    assert malo == "50000000001" and [a["id"] for a in chain] == ["4711", "4712"], chain
    for bad, phrase in ((("50000000009",), "50000000009 has no active agreement"),):
        try:
            select_octopus_de_agreements(response, *bad)
            raise AssertionError(phrase)
        except SpotPriceError as e:
            assert phrase in str(e), str(e)
    two_active = {"data": {"account": {"properties": [{"electricityMalos": [{"maloNumber": "1", "agreements": [fixed, dict(fixed, id="9")]}]}]}}}
    try:
        select_octopus_de_agreements(two_active)
        raise AssertionError("two active agreements on one location should raise")
    except SpotPriceError as e:
        assert "several active agreements (4711, 9)" in str(e), str(e)

    api, fake = make_octopus_de(octopus_de_malo=50000000001)
    fake.agreements = response
    now = dt("2025-05-02T08:00Z")
    pin_now(api, now)
    assert run(api.refresh(now)) is True
    # The fixed agreement up to its end, then the following one; the revoked 99.0 never appears
    assert api.supplier_intervals == [(dt("2025-04-30T22:00Z"), dt("2025-05-02T22:00Z"), 31.5), (dt("2025-05-02T22:00Z"), dt("2025-05-03T22:00Z"), 28.0)], api.supplier_intervals
    unset, unset_fake = make_octopus_de()
    unset_fake.agreements = response
    pin_now(unset, now)
    assert run(unset.refresh(now)) is False and "set spotprice_octopus_de_malo" in unset.last_error


def test_spotprice_octopus_de_berlin_time_and_dst(my_predbat=None):
    """Timeslot rules are German local time whatever the Home Assistant timezone, through both DST changes."""
    from spotprice import parse_octopus_de_agreement

    tou = agreement_of(octopus_de_agreement(info=TOU_DAY_NIGHT))
    api, fake = make_octopus_de(local_tz=pytz.timezone("Europe/London"))
    fake.agreements = octopus_de_agreement(info=TOU_DAY_NIGHT)
    now = dt("2025-05-02T08:00Z")
    pin_now(api, now)
    assert run(api.refresh(now)) is True
    # 06:00 and 22:00 Berlin (CEST) are 04:00Z and 20:00Z - not London's 05:00Z and 21:00Z
    boundaries = {start for start, _e, _v in api.supplier_intervals}
    assert dt("2025-05-02T04:00Z") in boundaries and dt("2025-05-02T20:00Z") in boundaries and dt("2025-05-02T05:00Z") not in boundaries, sorted(boundaries)
    # Spring forward: 22:00 CET on 28 March to 06:00 CEST on 29 March is 7 hours
    spring = parse_octopus_de_agreement(tou, dt("2026-03-28T20:00Z"), dt("2026-03-29T22:00Z"))
    assert (dt("2026-03-28T21:00Z"), dt("2026-03-29T04:00Z"), 22.0) in spring, spring
    assert (dt("2026-03-29T04:00Z"), dt("2026-03-29T20:00Z"), 35.0) in spring, spring
    # Fall back: 22:00 CEST on 25 October to 06:00 CET on 26 October is 9 hours
    autumn = parse_octopus_de_agreement(tou, dt("2025-10-25T19:00Z"), dt("2025-10-26T23:00Z"))
    assert (dt("2025-10-25T20:00Z"), dt("2025-10-26T05:00Z"), 22.0) in autumn, autumn
    assert all(autumn[i][1] == autumn[i + 1][0] for i in range(len(autumn) - 1))


def test_spotprice_supplier_cache_key(my_predbat=None):
    """The cache name carries a digest of the configured Ostrom contract or Octopus Energy Germany account and market location, so changing them never restores another contract's prices."""
    names = {
        "plain": make_api(provider="ostrom", entsoe_token=None, zone=None, ostrom_client_id="i", ostrom_client_secret="s").cache_filename(),
        "contract_a": make_api(provider="ostrom", entsoe_token=None, zone=None, ostrom_client_id="i", ostrom_client_secret="s", ostrom_contract_id=1).cache_filename(),
        "contract_b": make_api(provider="ostrom", entsoe_token=None, zone=None, ostrom_client_id="i", ostrom_client_secret="s", ostrom_contract_id=2).cache_filename(),
        "octo": make_api(provider="octopus_de", entsoe_token=None, zone=None, octopus_de_api_key="k").cache_filename(),
        "octo_account": make_api(provider="octopus_de", entsoe_token=None, zone=None, octopus_de_api_key="k", octopus_de_account="A-1").cache_filename(),
        "octo_malo": make_api(provider="octopus_de", entsoe_token=None, zone=None, octopus_de_api_key="k", octopus_de_account="A-1", octopus_de_malo="5").cache_filename(),
    }
    assert names["plain"].startswith("ostrom_none_") and names["octo"].startswith("octopus_de_none_"), names
    assert len(set(names.values())) == len(names), names
    assert make_api(provider="ostrom", entsoe_token=None, zone=None, ostrom_client_id="i", ostrom_client_secret="s", ostrom_contract_id=1).cache_filename() == names["contract_a"]
    assert make_api(provider="energycharts", entsoe_token=None, ostrom_contract_id=1).cache_filename() == "energycharts_de_lu"

    # A looked-up account does not change the name mid-run
    api, _fake = make_octopus_de()
    before = api.cache_filename()
    pin_now(api, dt("2025-05-02T08:00Z"))
    run(api.fetch_supplier(dt("2025-05-02T08:00Z")))
    assert api.octopus_de_account == "A-1234ABCD" and api.cache_filename() == before

    storage = FakeStorage()
    saved = make_api(provider="ostrom", entsoe_token=None, zone=None, ostrom_client_id="i", ostrom_client_secret="s", ostrom_contract_id=1, storage=storage)
    saved.supplier_intervals = [(dt("2025-05-02T08:00Z"), dt("2025-05-02T09:00Z"), 30.0)]
    saved.fetched["supplier"] = dt("2025-05-02T08:00Z")
    run(saved.save_cache())
    other = make_api(provider="ostrom", entsoe_token=None, zone=None, ostrom_client_id="i", ostrom_client_secret="s", ostrom_contract_id=2, storage=storage)
    run(other.load_cache())
    assert other.supplier_intervals == []


def test_spotprice_awattar_fallback_hourly(my_predbat=None):
    """When aWATTar falls back, quarter-hourly spot prices are averaged to the hours the HOURLY tariff bills; part-covered hours are left out; aWATTar's own hours pass through."""
    from spotprice import average_to_hourly

    base = dt("2025-05-02T10:00Z")
    quarters = [(base + timedelta(minutes=15 * i), base + timedelta(minutes=15 * (i + 1)), float(10 * (i + 1))) for i in range(8)]
    assert average_to_hourly(quarters) == [(base, base + timedelta(hours=1), 25.0), (base + timedelta(hours=1), base + timedelta(hours=2), 65.0)]
    assert average_to_hourly(quarters[:7]) == [(base, base + timedelta(hours=1), 25.0)]
    # Time-weighted: a 30 minute interval counts twice a quarter hour
    mixed = [(base, base + timedelta(minutes=30), 10.0), (base + timedelta(minutes=30), base + timedelta(minutes=45), 40.0), (base + timedelta(minutes=45), base + timedelta(hours=1), 40.0)]
    assert average_to_hourly(mixed) == [(base, base + timedelta(hours=1), 25.0)], average_to_hourly(mixed)
    hourly = [(base, base + timedelta(hours=1), 5.0)]
    assert average_to_hourly(hourly) == hourly

    api = make_api(provider="awattar", entsoe_token=None, zone="DE-LU")

    async def down(start, end):
        """aWATTar outage."""
        raise SpotPriceError("aWATTar returned HTTP 503")

    async def quarter_hourly(start, end):
        """Energy-Charts quarter hours."""
        return quarters

    api.fetch_awattar = down
    api.fetch_energycharts = quarter_hourly
    assert run(api.refresh(dt("2025-05-02T08:00Z"))) is True
    # The quarter hours are held as published; only the import price is averaged
    assert api.spot_source == "energycharts" and api.spot_intervals == quarters
    assert [(s_, e) for s_, e, _r in api.build_import_rates()] == [(s_, e) for s_, e, _v in average_to_hourly(quarters)]
    # With aWATTar itself as the source nothing is averaged
    api.spot_source = "awattar"
    assert len(api.build_import_rates()) == 8
    # Other providers keep the quarter hours
    plain = make_api(provider="energycharts", entsoe_token=None)
    plain.fetch_energycharts = quarter_hourly
    run(plain.refresh(dt("2025-05-02T08:00Z")))
    assert len(plain.build_import_rates()) == 8


def test_spotprice_awattar_fallback_export_native(my_predbat=None):
    """In a -20/-10/40/50 EUR/MWh hour from a quarter-hourly fallback, import is billed at the hourly average while export keeps each quarter: negative ones are zeroed or priced on their own."""
    base = dt("2025-05-02T10:00Z")
    quarters = [(base + timedelta(minutes=15 * i), base + timedelta(minutes=15 * (i + 1)), price) for i, price in enumerate((-20.0, -10.0, 40.0, 50.0))]
    for export_mode, zero, expected in (("spot", True, [0.0, 0.0, 4.0, 5.0]), ("spot", False, [-2.0, -1.0, 4.0, 5.0]), ("fixed", True, [0.0, 0.0, 8.0, 8.0])):
        api = make_api(provider="awattar", entsoe_token=None, zone="DE-LU", markup=0, vat=0, markup_percent=0, export_mode=export_mode, export_rate=8.0, export_zero_on_negative=zero)
        api.spot_intervals = quarters
        api.spot_source = "energycharts"
        # Import: one hour at the average 15 EUR/MWh = 1.5 c/kWh
        assert api.build_import_rates() == [(base, base + timedelta(hours=1), 1.5)], api.build_import_rates()
        export = api.build_export_rates()
        assert [rate for _s, _e, rate in export] == expected, (export_mode, zero, export)
        assert all(end - start == timedelta(minutes=15) for start, end, _r in export)


def test_spotprice_octopus_de_handover_single_successor(my_predbat=None):
    """Each handover takes the one agreement starting at (or first after) the current end; two claiming the same start are an error naming both; agreements that would overlap are never chained."""
    from spotprice import select_octopus_de_agreements

    def simple(agreement_id, valid_from, valid_to, price, active=False, revoked=False):
        """A fixed-rate agreement."""
        return {
            "id": agreement_id,
            "isActive": active,
            "isRevoked": revoked,
            "validFrom": valid_from,
            "validTo": valid_to,
            "product": {"code": "OCTOPUS-FIX"},
            "unitRateInformation": {"__typename": "SimpleProductUnitRateInformation", "latestGrossUnitRateCentsPerKwh": str(price)},
            "unitRateForecast": [],
        }

    def response(*agreements):
        """One market location holding the agreements."""
        return {"data": {"account": {"properties": [{"electricityMalos": [{"maloNumber": "1", "agreements": list(agreements)}]}]}}}

    current = simple("A", "2025-01-01T00:00:00+00:00", "2025-05-02T22:00:00+00:00", 30, active=True)
    second = simple("B", "2025-05-02T22:00:00+00:00", "2025-05-03T10:00:00+00:00", 28)
    # C starts after B began - it overlaps B and must not be chained after A; D follows B
    overlapping = simple("C", "2025-05-03T00:00:00+00:00", None, 99)
    third = simple("D", "2025-05-03T10:00:00+00:00", None, 26)
    _malo, chain = select_octopus_de_agreements(response(third, overlapping, current, second))
    assert [a["id"] for a in chain] == ["A", "B", "D"], [a["id"] for a in chain]
    # A gap is allowed: the next agreement is the first that starts after the end
    late = simple("E", "2025-05-03T00:00:00+00:00", None, 25)
    assert [a["id"] for a in select_octopus_de_agreements(response(current, late))[1]] == ["A", "E"]
    # Two non-revoked agreements claiming the same start
    twin = simple("B2", "2025-05-02T22:00:00+00:00", None, 27)
    try:
        select_octopus_de_agreements(response(current, second, twin))
        raise AssertionError("two successors should raise")
    except SpotPriceError as e:
        assert "(B, B2)" in str(e), str(e)
    # ...unless one of them is revoked
    assert [a["id"] for a in select_octopus_de_agreements(response(current, second, dict(twin, isRevoked=True)))[1]] == ["A", "B"]

    api, fake = make_octopus_de()
    fake.agreements = response(third, overlapping, current, second)
    now = dt("2025-05-02T08:00Z")
    pin_now(api, now)
    assert run(api.refresh(now)) is True
    assert api.supplier_intervals == [(dt("2025-04-30T22:00Z"), dt("2025-05-02T22:00Z"), 30.0), (dt("2025-05-02T22:00Z"), dt("2025-05-03T10:00Z"), 28.0), (dt("2025-05-03T10:00Z"), dt("2025-05-03T22:00Z"), 26.0)], api.supplier_intervals
    assert all(api.supplier_intervals[i][1] <= api.supplier_intervals[i + 1][0] for i in range(len(api.supplier_intervals) - 1))

    # Overlapping rates from any source are refused outright
    import spotprice as spotprice_module

    original = spotprice_module.parse_octopus_de_agreement
    spotprice_module.parse_octopus_de_agreement = lambda agreement, start, end, tz=None: [(dt("2025-05-02T00:00Z"), dt("2025-05-02T02:00Z"), 1.0), (dt("2025-05-02T01:00Z"), dt("2025-05-02T03:00Z"), 2.0)]
    try:
        clash, clash_fake = make_octopus_de()
        pin_now(clash, now)
        assert run(clash.refresh(now)) is False and "overlapping rates" in clash.last_error and clash.supplier_intervals == []
    finally:
        spotprice_module.parse_octopus_de_agreement = original


def test_spotprice_octopus_de_flat_only_when_explicit(my_predbat=None):
    """Without a forecast a flat price comes only from an explicit fixed rate; a dynamic product (by code, name or contract type), a time-of-use product with a simple rate, or rates without a type are errors."""
    from spotprice import octopus_de_is_dynamic, parse_octopus_de_agreement

    window = (dt("2025-05-01T22:00Z"), dt("2025-05-03T22:00Z"))
    base = agreement_of(octopus_de_agreement())
    assert parse_octopus_de_agreement(base, *window) == [(window[0], window[1], 31.5)]
    for product in ({"code": "OCT-24M", "displayName": "Octopus Dynamic"}, {"code": "X", "fullName": "Strom dynamisch"}, {"code": "X", "termsContractType": "DYNAMIC"}):
        assert octopus_de_is_dynamic(dict(base, product=product)), product
        assert parse_octopus_de_agreement(dict(base, product=product), *window) == []
    for bad in (
        dict(base, product={"code": "X", "isTimeOfUse": True}),
        dict(base, unitRateInformation={"latestGrossUnitRateCentsPerKwh": "31.5"}),
        dict(base, unitRateInformation={"__typename": "TimeOfUseProductUnitRateInformation", "rates": [{"latestGrossUnitRateCentsPerKwh": "31.5"}]}),
        dict(base, unitRateInformation={"rates": TOU_DAY_NIGHT["rates"]}),
    ):
        try:
            parse_octopus_de_agreement(bad, *window)
            raise AssertionError("expected an error for {}".format(bad))
        except SpotPriceError as e:
            assert "no price forecast and no explicit fixed or time-of-use rates" in str(e), str(e)
    assert len(parse_octopus_de_agreement(dict(base, unitRateInformation=TOU_DAY_NIGHT, product={"code": "X", "isTimeOfUse": True}), *window)) == 5


def test_spotprice_octopus_de_autumn_repeated_hour(my_predbat=None):
    """On the autumn change a 02:00-03:00 rule covers both 02:00-03:00 hours (00:00Z-02:00Z), the first included, with no gap or overlap."""
    from spotprice import parse_octopus_de_agreement

    info = {
        "__typename": "TimeOfUseProductUnitRateInformation",
        "rates": [
            {"latestGrossUnitRateCentsPerKwh": "50.0", "timeslotActivationRules": [{"activeFromTime": "02:00:00", "activeToTime": "03:00:00"}]},
            {"latestGrossUnitRateCentsPerKwh": "20.0", "timeslotActivationRules": [{"activeFromTime": "03:00:00", "activeToTime": "02:00:00"}]},
        ],
    }
    agreement = agreement_of(octopus_de_agreement(info=info))
    intervals = parse_octopus_de_agreement(agreement, dt("2025-10-25T22:00Z"), dt("2025-10-26T23:00Z"))
    assert intervals[:3] == [(dt("2025-10-25T22:00Z"), dt("2025-10-26T00:00Z"), 20.0), (dt("2025-10-26T00:00Z"), dt("2025-10-26T02:00Z"), 50.0), (dt("2025-10-26T02:00Z"), dt("2025-10-26T23:00Z"), 20.0)], intervals
    # Spring: 02:00-03:00 does not exist, so that rule has no time and the day is contiguous
    spring = parse_octopus_de_agreement(agreement, dt("2026-03-28T23:00Z"), dt("2026-03-29T22:00Z"))
    assert all(spring[i][1] == spring[i + 1][0] for i in range(len(spring) - 1)), spring
    assert spring[0][0] == dt("2026-03-28T23:00Z") and spring[-1][1] == dt("2026-03-29T22:00Z")
    assert all(rate == 20.0 for _s, _e, rate in spring), spring


def test_spotprice_octopus_de_error_codes(my_predbat=None):
    """KT-CT-1125 (token keys rotated) gets a new token and one retry; KT-CT-1139 on a query is an auth_error at once, with no retry."""
    import spotprice as spotprice_module

    recorded = []
    original = spotprice_module.record_api_call
    spotprice_module.record_api_call = lambda service, success=True, reason=None: recorded.append((service, success, reason))
    try:
        now = dt("2025-05-02T08:00Z")
        api, fake = make_octopus_de()
        pin_now(api, now)
        fake.queued["agreements"] = [kraken_error("KT-CT-1125", "Token keys have rotated.")]
        assert len(run(api.fetch_supplier(now))) == 8 and fake.count("token") == 2 and fake.count("agreements") == 2

        api, fake = make_octopus_de()
        pin_now(api, now)
        fake.queued["agreements"] = [kraken_error("KT-CT-1139", "Authentication failed.")]
        recorded.clear()
        try:
            run(api.fetch_supplier(now))
            raise AssertionError("1139 should raise")
        except SpotPriceError as e:
            assert "Authentication failed" in str(e)
        assert fake.count("token") == 1 and fake.count("agreements") == 1, fake.calls
        assert recorded[-1] == ("octopus_de", False, "auth_error"), recorded

        # Even alongside a token code, a rejected key is not retried with a new token
        api, fake = make_octopus_de()
        pin_now(api, now)
        both = kraken_error("KT-CT-1139", "Authentication failed.")
        both[1]["errors"].append({"message": "expired", "extensions": {"errorCode": "KT-CT-1124"}})
        fake.queued["agreements"] = [both]
        try:
            run(api.fetch_supplier(now))
            raise AssertionError("1139 with 1124 should raise")
        except SpotPriceError:
            pass
        assert fake.count("token") == 1 and fake.count("agreements") == 1, fake.calls
    finally:
        spotprice_module.record_api_call = original


def test_spotprice_supplier_cache_identity(my_predbat=None):
    """Cached supplier prices remember the contract or market location they were for, and are dropped when the one detected now differs."""
    storage = FakeStorage()
    now = dt("2025-05-02T08:00Z")
    first, _fake = make_ostrom(storage=storage)
    pin_now(first, now)
    assert run(first.refresh(now)) is True
    cached = storage.data[("spotprice", first.cache_filename())]
    assert cached["supplier_identity"] == "ostrom:100523456"

    # Same contract detected after a restart: the cache stands
    same, same_fake = make_ostrom(storage=storage)
    run(same.load_cache())
    assert same.supplier_intervals and same.cached_supplier_identity == "ostrom:100523456"
    same.note_supplier_identity("ostrom:100523456")
    assert same.supplier_intervals

    # The account now has a different active contract: the restored prices are discarded on detection
    moved, moved_fake = make_ostrom(storage=storage)
    run(moved.load_cache())
    assert moved.supplier_intervals
    moved_fake.contracts = {"data": [dict(OSTROM_CONTRACTS["data"][0], id=200000001)]}
    moved_fake.api_replies["/spot-prices"] = [(503, None)]
    pin_now(moved, now)
    assert run(moved.refresh(now)) is False
    assert moved.supplier_intervals == [] and any("discarding" in line for line in moved.base.logs), moved.base.logs

    # Octopus: account and market location form the identity
    octo, octo_fake = make_octopus_de()
    octo.cached_supplier_identity = "octopus_de:A-1234ABCD:50000000009"
    octo.supplier_intervals = [(now, now + timedelta(hours=1), 99.0)]
    pin_now(octo, now)
    run(octo.fetch_supplier(now))
    assert octo.supplier_identity == "octopus_de:A-1234ABCD:50000000001" and any("discarding" in line for line in octo.base.logs)


def test_spotprice_ostrom_status_handling(my_predbat=None):
    """ACTIVE matches in any case; when no contract has a status field at all the single dynamic contract is used with one warning; otherwise a missing status is not active."""
    from spotprice import select_ostrom_contract

    lower = {"data": [dict(OSTROM_CONTRACTS["data"][0], status=" active "), OSTROM_CONTRACTS["data"][1]]}
    assert select_ostrom_contract(lower)["id"] == 100523456
    assert select_ostrom_contract(lower, "100523456")["id"] == 100523456

    def without_status(contract, **changes):
        """A copy of contract with no status key."""
        copy = {key: value for key, value in contract.items() if key != "status"}
        copy.update(changes)
        return copy

    warnings = []
    no_status = {"data": [without_status(OSTROM_CONTRACTS["data"][0]), without_status(OSTROM_CONTRACTS["data"][1])]}
    assert select_ostrom_contract(no_status, warn=warnings.append)["id"] == 100523456
    assert len(warnings) == 1 and "no status field" in warnings[0]
    assert select_ostrom_contract(no_status, 100523456, warn=warnings.append)["id"] == 100523456
    for data, contract_id in (({"data": [without_status(OSTROM_CONTRACTS["data"][0]), without_status(OSTROM_CONTRACTS["data"][0], id=5)]}, None), (no_status, 100523999)):
        try:
            select_ostrom_contract(data, contract_id)
            raise AssertionError("expected an error")
        except SpotPriceError as e:
            assert "carry no status" in str(e), str(e)
    # One contract with a status makes the others' missing status count as not active
    mixed = {"data": [without_status(OSTROM_CONTRACTS["data"][0]), OSTROM_CONTRACTS["data"][1]]}
    try:
        select_ostrom_contract(mixed)
        raise AssertionError("expected no active contract")
    except SpotPriceError as e:
        assert "no active Ostrom contract" in str(e), str(e)

    # Through the component the warning is logged once however often contracts are read
    api, fake = make_ostrom()
    fake.contracts = no_status
    now = dt("2025-05-02T08:00Z")
    pin_now(api, now)
    assert run(api.refresh(now)) is True
    api.ostrom_contract = None
    run(api.fetch_supplier(now))
    assert sum("no status field" in line for line in api.base.logs) == 1, api.base.logs


def test_spotprice_cache_credential_swap(my_predbat=None):
    """Swapping a supplier credential with no selector set changes the cache name, so the old account's prices are never restored; only a salted 12-character digest appears, never the credential."""
    from spotprice import CACHE_DIGEST_SALT
    import hashlib

    cases = [
        ("tibber", {"tibber_token": "tibber-old"}, {"tibber_token": "tibber-new"}),
        ("ostrom", {"ostrom_client_id": "client-old", "ostrom_client_secret": "s"}, {"ostrom_client_id": "client-new", "ostrom_client_secret": "s"}),
        ("octopus_de", {"octopus_de_api_key": "sk_live_old"}, {"octopus_de_api_key": "sk_live_new"}),
        ("ews", {"ews_api_key": "pub_dpa_old"}, {"ews_api_key": "pub_dpa_new"}),
    ]
    now = dt("2025-05-02T08:00Z")
    for provider, old, new in cases:
        storage = FakeStorage()
        before = make_api(provider=provider, entsoe_token=None, zone=None, storage=storage, **old)
        after = make_api(provider=provider, entsoe_token=None, zone=None, storage=storage, **new)
        same = make_api(provider=provider, entsoe_token=None, zone=None, storage=storage, **old)
        assert before.cache_filename() != after.cache_filename() and before.cache_filename() == same.cache_filename(), provider
        credential = list(old.values())[0]
        expected = hashlib.sha256((CACHE_DIGEST_SALT + credential + "|" * {"tibber": 1, "ostrom": 1, "octopus_de": 2}.get(provider, 0)).encode("utf-8")).hexdigest()[:12]
        assert before.cache_filename() == "{}_none_{}".format(provider, expected), (provider, before.cache_filename())
        # Prices cached under the old credential are not restored, nor treated as fresh, under the new one
        intervals = [(now, now + timedelta(hours=1), 0.3 if provider == "tibber" else 30.0)]
        if provider == "tibber":
            before.tibber_intervals = intervals
        else:
            before.supplier_intervals = intervals
        before.fetched[before.import_source()] = now
        run(before.save_cache())
        run(after.load_cache())
        assert after.source_intervals() == [] and after.fetched_at is None and after.refresh_due(now), provider
        run(same.load_cache())
        assert same.source_intervals() == intervals, provider
        # The credential itself is written nowhere: not in the name, not in the stored blob
        for (_module, filename), blob in storage.data.items():
            assert credential not in filename and credential not in repr(blob), (provider, filename)


def test_spotprice_octopus_de_bare_jwt(my_predbat=None):
    """The Kraken JWT is sent bare - no Bearer or JWT prefix - and the token request carries no Authorization at all."""
    api, fake = make_octopus_de()
    now = dt("2025-05-02T08:00Z")
    pin_now(api, now)
    run(api.fetch_supplier(now))
    token_headers = [call[3] for call in fake.calls if call[0] == "token"]
    query_headers = [call[3] for call in fake.calls if call[0] != "token"]
    assert all("Authorization" not in headers for headers in token_headers)
    assert query_headers and all(headers["Authorization"] == make_jwt(fake.token_exp) for headers in query_headers), query_headers
    assert not any(headers["Authorization"].split(" ")[0] in ("JWT", "Bearer") for headers in query_headers)


def test_spotprice_ostrom_move_house(my_predbat=None):
    """Without spotprice_ostrom_contract_id the contract is looked up again daily and straight after a 404/422 on the postcode; a move switches postcode and drops the old prices; no active contract is an error with nothing held."""
    moved = {"data": [dict(OSTROM_CONTRACTS["data"][0], status="TERMINATED"), dict(OSTROM_CONTRACTS["data"][0], id=200000001, address={"zip": "80331", "city": "Munich"})]}
    now = dt("2025-05-02T08:00Z")

    # Daily re-check: same day no lookup, next day the new contract and postcode
    api, fake = make_ostrom()
    pin_now(api, now)
    assert run(api.refresh(now)) is True and fake.count("/contracts") == 1
    pin_now(api, now + timedelta(hours=7))
    assert run(api.refresh(now + timedelta(hours=7))) is True and fake.count("/contracts") == 1
    fake.contracts = moved
    day_later = now + timedelta(days=1)
    pin_now(api, day_later)
    assert run(api.refresh(day_later)) is True
    assert fake.count("/contracts") == 2 and fake.calls[-1][2]["zip"] == "80331", fake.calls[-1]
    assert api.ostrom_contract["id"] == 200000001 and any("contract changed" in line for line in api.base.logs)

    # A move found while the new postcode's prices cannot be fetched yet: the old postcode's prices are not kept
    api, fake = make_ostrom()
    pin_now(api, now)
    run(api.refresh(now))
    assert api.supplier_intervals
    fake.contracts = moved
    fake.api_replies["/spot-prices"] = [(503, None)]
    pin_now(api, day_later)
    assert run(api.refresh(day_later)) is False and api.supplier_intervals == [] and api.ostrom_contract["id"] == 200000001
    # ...also when the contract keeps its number and only the address changes
    api, fake = make_ostrom()
    pin_now(api, now)
    run(api.refresh(now))
    fake.contracts = {"data": [dict(OSTROM_CONTRACTS["data"][0], address={"zip": "80331"})]}
    fake.api_replies["/spot-prices"] = [(503, None)]
    pin_now(api, day_later)
    assert run(api.refresh(day_later)) is False and api.supplier_intervals == [] and api.ostrom_contract["address"]["zip"] == "80331"

    # A 404 for the old postcode re-checks at once and retries with the new one in the same fetch
    api, fake = make_ostrom()
    pin_now(api, now)
    run(api.refresh(now))
    fake.contracts = moved
    fake.api_replies["/spot-prices"] = [(404, {"detail": "zip not found"})]
    later = now + timedelta(hours=1)
    pin_now(api, later)
    api.fetched["supplier"] = None
    assert run(api.refresh(later)) is True, api.last_error
    assert [call[2]["zip"] for call in fake.calls if call[1].endswith("/spot-prices")] == ["10997", "10997", "80331"]
    assert fake.count("/contracts") == 2

    # A 422 with the contract unchanged is reported, not retried in a loop
    api, fake = make_ostrom()
    pin_now(api, now)
    run(api.refresh(now))
    fake.api_replies["/spot-prices"] = [(422, {"detail": "bad zip"})]
    api.fetched["supplier"] = None
    assert run(api.refresh(later)) is False and "HTTP 422" in api.last_error and fake.count("/contracts") == 2 and fake.count("/spot-prices") == 2

    # Server errors do not trigger a re-check
    api, fake = make_ostrom()
    pin_now(api, now)
    run(api.refresh(now))
    fake.api_replies["/spot-prices"] = [(503, None)]
    api.fetched["supplier"] = None
    assert run(api.refresh(later)) is False and fake.count("/contracts") == 1

    # Moved out with no active contract: an error, and the old postcode's prices are no longer held
    api, fake = make_ostrom()
    pin_now(api, now)
    run(api.refresh(now))
    assert api.supplier_intervals
    fake.contracts = {"data": [dict(OSTROM_CONTRACTS["data"][0], status="TERMINATED")]}
    pin_now(api, day_later)
    assert run(api.refresh(day_later)) is False and "no active Ostrom contract" in api.last_error
    assert api.supplier_intervals == [] and api.ostrom_contract is None
    api.publish(day_later)
    assert api.base.entities["sensor.predbat_spotprice_status"]["state"] == "error"

    # A configured contract is not re-checked daily, nor after a 404
    fixed, fixed_fake = make_ostrom(ostrom_contract_id=100523456)
    pin_now(fixed, now)
    run(fixed.refresh(now))
    pin_now(fixed, day_later)
    run(fixed.refresh(day_later))
    fixed_fake.api_replies["/spot-prices"] = [(404, None)]
    fixed.fetched["supplier"] = None
    assert run(fixed.refresh(day_later + timedelta(hours=1))) is False and fixed_fake.count("/contracts") == 1


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
    test_spotprice_no_stretch_over_missing_points,
    test_spotprice_parse_errors_wrapped,
    test_spotprice_energycharts_never_tries_entsoe,
    test_spotprice_market_day_polling,
    test_spotprice_status_states,
    test_spotprice_tibber_partial_failure,
    test_spotprice_days_follow_engine_numbering,
    test_spotprice_tibber_token_only,
    test_spotprice_vat_one_or_more_is_percent,
    test_spotprice_charge_zone_unknown_keys,
    test_spotprice_backoff_per_source,
    test_spotprice_mixed_resolution_starts,
    test_spotprice_tibber_token_implies_tibber_with_zone,
    test_spotprice_fetch_error_categories,
    test_spotprice_entsoe_only_zone,
    test_spotprice_config_error_ignores_cache,
    test_spotprice_refresh_all_blocked,
    test_spotprice_omitted_timestamps,
    test_spotprice_tibber_cache_key,
    test_spotprice_ostrom_parse,
    test_spotprice_ostrom_contract_selection,
    test_spotprice_ostrom_fetch_and_token_cache,
    test_spotprice_ostrom_auth_errors,
    test_spotprice_ostrom_all_in_and_refresh,
    test_spotprice_ostrom_provider_inference,
    test_spotprice_octopus_de_dynamic,
    test_spotprice_octopus_de_fixed_and_time_of_use,
    test_spotprice_octopus_de_auth_and_errors,
    test_spotprice_octopus_de_inference_and_config,
    test_spotprice_awattar_parse_and_fetch,
    test_spotprice_awattar_markup_percent,
    test_spotprice_awattar_fallback,
    test_spotprice_ews_parse,
    test_spotprice_ews_fetch,
    test_spotprice_ews_inference_and_config,
    test_spotprice_octopus_de_forecast_time_of_use,
    test_spotprice_octopus_de_dynamic_without_forecast,
    test_spotprice_octopus_de_market_location_selection,
    test_spotprice_octopus_de_berlin_time_and_dst,
    test_spotprice_supplier_cache_key,
    test_spotprice_awattar_fallback_hourly,
    test_spotprice_awattar_fallback_export_native,
    test_spotprice_octopus_de_handover_single_successor,
    test_spotprice_octopus_de_flat_only_when_explicit,
    test_spotprice_octopus_de_autumn_repeated_hour,
    test_spotprice_octopus_de_error_codes,
    test_spotprice_supplier_cache_identity,
    test_spotprice_ostrom_status_handling,
    test_spotprice_cache_credential_swap,
    test_spotprice_octopus_de_bare_jwt,
    test_spotprice_ostrom_move_house,
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
