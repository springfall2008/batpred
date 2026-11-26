# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
"""Tests for the memory benchmark's cache export, on a small synthetic live cache directory."""

import asyncio
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import yaml

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "apps" / "predbat"))

import fixture_cache  # noqa: E402
import scrub  # noqa: E402

T0 = datetime(2026, 11, 26, 8, 0, 0, tzinfo=timezone.utc)

REAL_POSTCODE = "ZZ9 9ZZ"
REAL_KEY = "real-forecast-key"
REAL_LAT, REAL_LON = 52.123456, -1.654321
LIVE_CONFIG = [
    {"postcode": REAL_POSTCODE, "kwp": 2.0, "azimuth": 90, "declination": 30, "api_key": REAL_KEY, "efficiency": 0.9},
    {"postcode": REAL_POSTCODE, "kwp": 3.0, "azimuth": -90, "declination": 40, "api_key": REAL_KEY, "efficiency": 0.9},
]
FORECAST = {
    "result": {"watts": {"2026-11-26 09:00:00": 120}, "watt_hours_period": {"2026-11-26 09:00:00": 60}, "watt_hours": {"2026-11-26 09:00:00": 60}},
    "message": {"info": {"latitude": REAL_LAT, "longitude": REAL_LON, "distance": 0.1, "place": "Real Street, Real Town", "timezone": "Europe/London"}, "ratelimit": {"limit": 60}},
}


def write_live(cache, stem, data, fmt="yaml"):
    """Write one live cache entry and its meta sidecar, created long before T0."""
    ext = "json" if fmt == "json" else "yaml"
    text = json.dumps(data) if fmt == "json" else yaml.safe_dump(data)
    (cache / "{}.{}".format(stem, ext)).write_text(text)
    (cache / "{}.meta".format(stem)).write_text(json.dumps({"format": fmt, "expiry": None, "module": stem.split("_")[0], "created": "2026-01-01T00:00:00+00:00"}))


class FrozenDatetime(datetime):
    """datetime with now() pinned to T0, for checking a cache entry's age as the benchmark's clock sees it."""

    @classmethod
    def now(cls, tz=None):
        """Return T0 in tz."""
        return T0 if tz is None else T0.astimezone(tz)


class TestWriteEntry(unittest.TestCase):
    """write_entry writes what storage.py writes, aged zero at T0."""

    def test_write_entry_meta_is_fresh_at_t0(self):
        """Storage reads the entry back unchanged, and its age at T0 is zero."""
        import storage

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            cache.mkdir()
            fixture_cache.write_entry(cache, "solar", "abc123", {"x": [1, 2]}, "json", T0)
            store = storage.StorageLocalFiles(tmp, lambda *args, **kwargs: None)
            with mock.patch.object(storage, "datetime", FrozenDatetime):
                self.assertEqual(asyncio.run(store.load("solar", "abc123")), {"x": [1, 2]})
                self.assertEqual(asyncio.run(store.age("solar", "abc123")), 0.0)


class TestForecastExport(unittest.TestCase):
    """export_forecast_cache re-keys the live forecast.solar responses under neutral values."""

    def setUp(self):
        """Build a live cache holding a postcode lookup and one dual-plane forecast, as the component writes them."""
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.live = root / "live"
        self.out = root / "out"
        self.live.mkdir()
        self.out.mkdir()
        source = fixture_cache.solcast_source()
        postcode_url = fixture_cache.POSTCODE_URL.format(REAL_POSTCODE)
        write_live(self.live, "solar_" + source.cache_key(postcode_url, {}), {"status": 200, "result": {"postcode": REAL_POSTCODE, "latitude": REAL_LAT, "longitude": REAL_LON}}, "json")
        url, params = fixture_cache.forecast_requests(LIVE_CONFIG, {REAL_POSTCODE: (REAL_LAT, REAL_LON)}, source)[0]
        self.assertIn("{}/{}".format(REAL_LAT, REAL_LON), url)
        write_live(self.live, "solar_" + source.cache_key(url, params), FORECAST, "json")

    def tearDown(self):
        """Remove the temporary directory."""
        self.tmp.cleanup()

    def test_forecast_requests_pairs_planes_into_one_dual_call(self):
        """Two personal planes with the same key and location make one dual-plane request, as the component does."""
        requests = fixture_cache.forecast_requests(LIVE_CONFIG, {REAL_POSTCODE: (REAL_LAT, REAL_LON)}, fixture_cache.solcast_source())
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0][0].startswith("https://api.forecast.solar/{}/estimate/".format(REAL_KEY)))

    def test_forecast_export_scrubs_location_and_rekeys(self):
        """The forecast sits at the neutral request's key, its location is the fake one, and no real value survives."""
        neutral, secrets = fixture_cache.export_forecast_cache(self.live, self.out, T0, LIVE_CONFIG)
        source = fixture_cache.solcast_source()
        self.assertEqual([plane["postcode"] for plane in neutral], [scrub.FAKE_POSTCODE, scrub.FAKE_POSTCODE])
        self.assertEqual([plane["api_key"] for plane in neutral], [scrub.FAKE_FORECAST_KEY, scrub.FAKE_FORECAST_KEY])
        self.assertEqual([plane["kwp"] for plane in neutral], [2.0, 3.0])
        url, params = fixture_cache.forecast_requests(neutral, {scrub.FAKE_POSTCODE: (scrub.FAKE_LATITUDE, scrub.FAKE_LONGITUDE)}, source)[0]
        written = json.loads((self.out / "solar_{}.json".format(source.cache_key(url, params))).read_text())
        self.assertEqual(written["result"], FORECAST["result"])
        self.assertEqual(written["message"]["info"]["latitude"], scrub.FAKE_LATITUDE)
        self.assertEqual(written["message"]["info"]["place"], "Benchmark")
        self.assertEqual(scrub.find_leaks(self.out, secrets), [])
        for value in (REAL_POSTCODE, REAL_KEY, "Real Street, Real Town", str(REAL_LAT), str(REAL_LON)):
            self.assertIn(value, secrets)

    def test_forecast_export_synthesises_the_postcode_lookup(self):
        """The fake postcode's lookup is written so the component resolves it without a request."""
        fixture_cache.export_forecast_cache(self.live, self.out, T0, LIVE_CONFIG)
        source = fixture_cache.solcast_source()
        lookup = json.loads((self.out / "solar_{}.json".format(source.cache_key(fixture_cache.POSTCODE_URL.format(scrub.FAKE_POSTCODE), {}))).read_text())
        self.assertEqual((lookup["result"]["latitude"], lookup["result"]["longitude"]), (scrub.FAKE_LATITUDE, scrub.FAKE_LONGITUDE))

    def test_forecast_export_with_secret_keys_matches_by_recency(self):
        """When the api keys are !secret references the request keys cannot be computed, so the newest cached forecast is used."""
        import export_fixture

        config = [dict(plane, api_key=export_fixture.Secret("forecast_key")) for plane in LIVE_CONFIG]
        neutral, secrets = fixture_cache.export_forecast_cache(self.live, self.out, T0, config)
        source = fixture_cache.solcast_source()
        self.assertEqual([plane["api_key"] for plane in neutral], [scrub.FAKE_FORECAST_KEY, scrub.FAKE_FORECAST_KEY])
        url, params = fixture_cache.forecast_requests(neutral, {scrub.FAKE_POSTCODE: (scrub.FAKE_LATITUDE, scrub.FAKE_LONGITUDE)}, source)[0]
        written = json.loads((self.out / "solar_{}.json".format(source.cache_key(url, params))).read_text())
        self.assertEqual(written["result"], FORECAST["result"])
        self.assertEqual(scrub.find_leaks(self.out, secrets), [])

    def test_forecast_export_without_a_cached_forecast_fails(self):
        """A live cache with no forecast for the configured planes is an error, not an empty fixture."""
        for path in list(self.live.glob("solar_*.json")):
            if "watts" in path.read_text():
                path.unlink()
                path.with_suffix(".meta").unlink()
        with self.assertRaises(ValueError):
            fixture_cache.export_forecast_cache(self.live, self.out, T0, LIVE_CONFIG)


class TestOctopusExport(unittest.TestCase):
    """export_octopus_cache copies the Octopus entries with every identifier and the token scrubbed."""

    def setUp(self):
        """Build a live cache with an account entry, a rates URL entry and a billing entry."""
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.live = root / "live"
        self.out = root / "out"
        self.live.mkdir()
        self.out.mkdir()
        account = {
            "account_data": {"account": {"electricityAgreements": [{"meterPoint": {"mpan": "1900000000001", "meters": [{"serialNumber": "21E1234567"}], "agreements": [{"tariffCode": "E-1R-INTELLI-VAR-24-10-29-A"}]}}]}},
            "kraken_token": "secret-token",
            "note": "account A-9876ABCD",
            "tariff_fetched_at": datetime(2026, 1, 1),
            "device_fetched_at": datetime(2026, 1, 1),
        }
        write_live(self.live, "octopus_user_account", account)
        self.rates = {"results": [{"value_inc_vat": 24.5, "valid_from": "2026-11-26T00:00:00Z"}]}
        write_live(self.live, "octopus_0123456789abcdef", self.rates)
        write_live(self.live, "octopus_user_billing", {"account": "A-9876ABCD", "bills": [1, 2]})
        write_live(self.live, "unrelated_thing", {"keep": "out"})

    def tearDown(self):
        """Remove the temporary directory."""
        self.tmp.cleanup()

    def test_octopus_export_scrubs_identifiers_and_token(self):
        """No identifier or token survives, tariff codes stay, and the fetch times are T0."""
        replacements = fixture_cache.export_octopus_cache(self.live, self.out, T0)
        self.assertEqual(replacements["A-9876ABCD"], scrub.FAKE_ACCOUNT)
        self.assertIn("electricityAgreements", (self.out / "octopus_user_account.yaml").read_text())
        self.assertEqual(scrub.find_leaks(self.out, ["A-9876ABCD", "1900000000001", "21E1234567", "secret-token"]), [])
        account = yaml.safe_load((self.out / "octopus_user_account.yaml").read_text())
        self.assertIsNone(account["kraken_token"])
        self.assertEqual(account["tariff_fetched_at"], T0.replace(tzinfo=None))
        self.assertEqual(account["device_fetched_at"], T0.replace(tzinfo=None))
        self.assertIn("E-1R-INTELLI-VAR-24-10-29-A", (self.out / "octopus_user_account.yaml").read_text())
        self.assertFalse((self.out / "unrelated_thing.yaml").exists())

    def test_octopus_export_device_ids_keep_the_suffix_the_entity_names_were_renamed_to(self):
        """A device id's fake ends in the same fake its entity-name suffix got, so the component's names still match."""
        write_live(self.live, "octopus_user_account", {"intelligent_devices": {"00000000-0000-4000-8000-0000000d1d1d": {"device_id": "00000000-0000-4000-8000-0000000d1d1d"}}})
        replacements = fixture_cache.export_octopus_cache(self.live, self.out, T0, tokens={"0000000d1d1d": "benchid1"})
        text = (self.out / "octopus_user_account.yaml").read_text()
        self.assertIn("BENCH-DEVICE-benchid1", text)
        self.assertNotIn("0000000d1d1d", text)
        self.assertEqual(replacements["00000000-0000-4000-8000-0000000d1d1d"], "BENCH-DEVICE-benchid1")

    def test_octopus_export_scrubs_lower_case_serials(self):
        """A serial lower-cased by an integration is scrubbed too."""
        write_live(self.live, "octopus_user_billing", {"note": "meter 21e1234567"})
        fixture_cache.export_octopus_cache(self.live, self.out, T0)
        self.assertNotIn("21e1234567", (self.out / "octopus_user_billing.yaml").read_text())

    def test_octopus_export_keeps_url_cache_entries_byte_for_byte_except_meta(self):
        """A rates entry carries no identifiers, so its data is copied unchanged and only its meta is renewed."""
        fixture_cache.export_octopus_cache(self.live, self.out, T0)
        self.assertEqual((self.out / "octopus_0123456789abcdef.yaml").read_text(), (self.live / "octopus_0123456789abcdef.yaml").read_text())
        meta = json.loads((self.out / "octopus_0123456789abcdef.meta").read_text())
        self.assertEqual(meta["created"], T0.isoformat())


if __name__ == "__main__":
    unittest.main()
