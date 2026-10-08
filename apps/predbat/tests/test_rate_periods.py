"""Tests for source boundaries separate from minute-expanded rate values."""

from datetime import datetime, timedelta, timezone

from rate_periods import extract_rate_periods, extend_rate_periods, period_time


def run_rate_periods_tests(my_predbat=None):
    """Run pure source metadata tests without changing the shared fixture."""
    failed = 0
    checks = [
        test_equal_adjacent_periods,
        test_missing_bounds,
        test_eds_cadence,
        test_declared_strom_bounds,
        test_dst_bounds,
        test_cache_metadata,
        test_source_readers,
        test_octopus_original_and_cache_bounds,
    ]
    for check in checks:
        try:
            check()
            print("PASS:", check.__name__)
        except AssertionError as error:
            print("ERROR:", check.__name__, error)
            failed += 1
    return failed


def test_equal_adjacent_periods():
    """Equal adjacent prices must preserve distinct provider boundaries."""
    rows = [
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T00:30:00Z", "rate": 7},
        {"from": "2026-01-01T00:30:00Z", "to": "2026-01-01T01:00:00Z", "rate": 7},
    ]
    periods = extract_rate_periods(rows, "octopus")
    assert len(periods) == 2
    assert periods[0]["end"] == periods[1]["start"]
    assert all(period["kind"] == "interval" for period in periods)
    assert len(extract_rate_periods(rows + rows, "octopus")) == 2
    private_url = "https://user:secret@example.com/rates?api_key=token"
    private_periods = extract_rate_periods(rows, private_url)
    assert all(period["source_id"].startswith("url:") and "secret" not in period["source_id"] and "token" not in period["source_id"] for period in private_periods)
    assert private_periods[0]["source_id"] == extract_rate_periods(rows, private_url)[0]["source_id"]
    assert private_periods[0]["source_id"] == extract_rate_periods(rows, "https://other:password@example.com/rates?api_key=different")[0]["source_id"]


def test_missing_bounds():
    """Unbounded validity must not become a fabricated settlement period."""
    rows = [
        {"valid_from": "2026-01-01T00:00:00Z", "valid_to": None},
        {"valid_from": "2026-01-02T00:00:00Z"},
        {"valid_from": None, "valid_to": "2026-01-03T00:00:00Z"},
        {"valid_from": "2026-01-04T00:00:00Z", "valid_to": "bad"},
        {"valid_from": "2026-01-05T00:00:00Z", "valid_to": "2026-01-05T00:00:00Z"},
        {"valid_from": "2026-01-06T00:00:00Z", "valid_to": "2026-04-01T00:00:00Z"},
    ]
    periods = extract_rate_periods(rows, "fixed")
    assert len(periods) == 3
    assert all(period["kind"] == "validity" for period in periods)
    assert periods[0]["end"] is None and periods[1]["end"] is None
    null = extract_rate_periods(rows[:1], "fixed", interval_minutes=30)
    assert null[0]["end"] is None
    assert period_time("2026-01-01T00:00:00") is None


def test_eds_cadence():
    """The EDS documented fifteen-minute cadence remains explicit metadata."""
    rows = [{"hour": "2026-01-01T00:00:00Z", "price": 1}, {"hour": "2026-01-01T00:15:00Z", "price": 1}]
    periods = extract_rate_periods(rows, "eds", from_key="hour", interval_minutes=15)
    assert len(periods) == 2
    assert all(period["end"] - period["start"] == timedelta(minutes=15) for period in periods)
    assert all(period["bounds"] == "cadence" for period in periods)


def test_declared_strom_bounds():
    """Use Strom declared end rather than a hard-coded expansion width."""
    rows = [{"start": "2026-01-01T01:00:00+01:00", "end": "2026-01-01T01:20:00+01:00", "price": 1}]
    periods = extract_rate_periods(rows, "strom", period_kind="interval")
    assert periods[0]["start"] == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert periods[0]["end"] - periods[0]["start"] == timedelta(minutes=20)
    till = extract_rate_periods([{"from": "2026-01-01T00:00:00Z", "till": "2026-01-01T00:15:00Z"}], "strom")
    assert till[0]["end"] is not None


def test_dst_bounds():
    """Offset-bearing bounds preserve actual instants through both DST changes."""
    autumn = extract_rate_periods(
        [
            {"start": "2026-10-25T01:00:00+01:00", "end": "2026-10-25T01:30:00+01:00"},
            {"start": "2026-10-25T01:00:00+00:00", "end": "2026-10-25T01:30:00+00:00"},
        ],
        "dst",
    )
    assert len(autumn) == 2
    assert autumn[1]["start"] - autumn[0]["start"] == timedelta(hours=1)
    spring = extract_rate_periods([{"start": "2026-03-29T00:30:00+00:00", "end": "2026-03-29T02:00:00+01:00"}], "dst")
    assert spring[0]["end"] - spring[0]["start"] == timedelta(minutes=30)


def test_cache_metadata():
    """Copy freshness labels without converting forecasts to observed evidence."""
    periods = extract_rate_periods([{"from": "2026-01-01T00:00:00Z", "to": "2026-01-01T00:30:00Z"}], "cache")
    collector = []
    extend_rate_periods(collector, periods, freshness="stale")
    assert collector[0]["freshness"] == "stale"
    assert periods[0]["freshness"] == "source"
    extend_rate_periods(None, periods)
    assert "observed" not in collector[0] and "confirmed" not in collector[0]


def test_source_readers():
    """Exercise real readers with equal rates, inferred cadence and DST bounds."""
    from energydataservice import Energidataservice
    from stromligning import Stromligning
    from octopus import Octopus

    class Reader(Energidataservice, Stromligning, Octopus):
        """Minimal reader host that does not touch shared Home Assistant state."""

        def __init__(self):
            """Supply the existing reader clock and sensor dependencies."""
            self.forecast_days = 2
            self.midnight_utc = datetime(2026, 10, 25, tzinfo=timezone.utc)
            self.prefix = "predbat"
            self.debug_enable = False
            self.attributes = {}

        def get_state_wrapper(self, entity_id=None, attribute=None):
            """Read isolated source attributes."""
            return self.attributes.get(attribute)

        def log(self, message):
            """Ignore diagnostic logging for pure fixtures."""
            pass

    reader = Reader()
    reader.attributes = {"raw_today": [{"hour": "2026-10-25T00:00:00Z", "price": 1}, {"hour": "2026-10-25T00:15:00Z", "price": 1}], "use_cent": True}
    periods = []
    rates = reader.fetch_energidataservice_rates("sensor.eds", periods_out=periods)
    assert len(periods) == 2 and rates[0] == rates[29] == 1
    assert periods[0]["end"] - periods[0]["start"] == timedelta(minutes=15)
    reader.attributes = {"prices_today": [{"start": "2026-10-25T01:00:00+01:00", "end": "2026-10-25T01:20:00+01:00", "price": 2}]}
    periods = []
    rates = reader.fetch_stromligning_rates("sensor.strom", None, periods_out=periods)
    assert len(periods) == 1 and len(rates) == 20
    assert periods[0]["end"] - periods[0]["start"] == timedelta(minutes=20)
    reader.attributes = {"rates": [{"from": "2026-10-25T01:00:00+01:00", "to": "2026-10-25T01:30:00+01:00", "rate": 7}, {"from": "2026-10-25T01:00:00+00:00", "to": "2026-10-25T01:30:00+00:00", "rate": 7}]}
    periods = []
    reader.fetch_octopus_rates("sensor.external", periods_out=periods)
    assert len(periods) == 2 and periods[1]["start"] - periods[0]["start"] == timedelta(hours=1)
    # Own-component display rows are not billing bounds: prefer separate metadata.
    reader.attributes["rate_periods"] = [{"start": "2026-10-25T00:00:00Z", "end": None, "kind": "validity", "source_id": "fixed"}]
    periods = []
    reader.fetch_octopus_rates("sensor.predbat_octopus_account_import_rates", periods_out=periods)
    assert len(periods) == 1 and periods[0]["end"] is None and periods[0]["source_id"] == "fixed"
    del reader.attributes["rate_periods"]
    periods = []
    reader.fetch_octopus_rates("sensor.predbat_octopus_account_import_rates", periods_out=periods)
    assert all(period["kind"] == "validity" and period["freshness"] == "legacy" for period in periods)


def test_octopus_original_and_cache_bounds():
    """Original null ends and metadata must survive getter and URL-cache paths."""
    from octopus import Octopus, OctopusAPI
    from unittest.mock import patch
    from types import SimpleNamespace

    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    raw = [{"valid_from": midnight.isoformat(), "valid_to": None, "value_inc_vat": 7}]
    tariff = {"data": raw, "tariffCode": "fixed"}
    api = SimpleNamespace(midnight_utc=midnight, get_tariff=lambda direction: tariff)
    periods = []
    assert OctopusAPI.get_octopus_rates_direct(api, "import", periods_out=periods)
    assert raw[0]["valid_to"] is not None and periods[0]["end"] is None
    repeated = []
    OctopusAPI.get_octopus_rates_direct(api, "import", periods_out=repeated)
    assert repeated[0]["end"] is None and tariff["rate_periods"][0]["end"] is None
    periods = extract_rate_periods([{"from": midnight, "to": midnight + timedelta(minutes=30)}], "url")
    host = SimpleNamespace(
        midnight_utc=midnight,
        minutes_now=0,
        debug_enable=False,
        failures_total=0,
        log=lambda message: None,
        record_status=lambda *args, **kwargs: None,
        _load_octopus_url_cache_from_storage=lambda: None,
        _save_octopus_url_cache_to_storage=lambda: None,
        octopus_url_cache={"url": {"stamp": datetime.now(), "midnight_utc": midnight, "data": {0: 7}, "periods": periods}},
    )
    collector = []
    assert Octopus.download_octopus_rates(host, "url", periods_out=collector) == {0: 7}
    assert collector[0]["freshness"] == "cache"
    host.octopus_url_cache["url"]["stamp"] -= timedelta(hours=1)
    host.download_octopus_rates_func = lambda url, periods_out=None: {}
    collector = []
    assert Octopus.download_octopus_rates(host, "url", periods_out=collector) == {0: 7}
    assert collector[0]["freshness"] == "stale" and periods[0]["freshness"] == "source"
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {
            "results": [
                {"valid_from": midnight.isoformat(), "valid_to": (midnight + timedelta(minutes=30)).isoformat(), "value_inc_vat": 7},
                {"valid_from": (midnight + timedelta(minutes=30)).isoformat(), "valid_to": (midnight + timedelta(minutes=60)).isoformat(), "value_inc_vat": 7},
            ],
            "next": None,
        },
    )
    collector = []
    with patch("octopus.requests.get", return_value=response):
        rates = Octopus.download_octopus_rates_func(host, "url", periods_out=collector)
    assert rates[0] == rates[59] == 7 and len(collector) == 2
    host.octopus_url_cache = {}
    host.download_octopus_rates_func = lambda url, periods_out=None: (extend_rate_periods(periods_out, collector), rates)[1]
    fresh = []
    assert Octopus.download_octopus_rates(host, "url", periods_out=fresh) == rates
    assert len(fresh) == 2 and len(host.octopus_url_cache["url"]["periods"]) == 2
    # Legacy minute-only cache has no provider-bound metadata to fabricate.
    del host.octopus_url_cache["url"]["periods"]
    legacy = []
    assert Octopus.download_octopus_rates(host, "url", periods_out=legacy) == rates
    assert legacy == []


if __name__ == "__main__":
    raise SystemExit(run_rate_periods_tests())
