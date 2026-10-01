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
        api = make_api(provider="energycharts", entsoe_token=None, zone=zone, local_tz=pytz.timezone(tz_name))
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
