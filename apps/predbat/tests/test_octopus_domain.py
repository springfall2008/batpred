# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long

"""Tests for the move of the Octopus API from api.octopus.energy to api.oegb-kraken.energy (GH#5786)."""

from datetime import datetime
from unittest.mock import patch

from annual_tariff import AnnualTariff
from const import OCTOPUS_API_URL, OCTOPUS_BACKEND_API_URL
from octopus import OctopusEnergyApiClient
from tariff_catalogue import IMPORT_TARIFFS, match_entry
from utils import migrate_octopus_url

OLD_AGILE_URL = "https://api.octopus.energy/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-A/standard-unit-rates/"
NEW_AGILE_URL = "https://api.oegb-kraken.energy/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-A/standard-unit-rates/"


def test_octopus_domain(my_predbat):
    """Old-domain URLs are moved to the new domain wherever they are fetched, and nothing else is touched."""
    failed = False
    print("**** Testing octopus_domain ****")

    print("Test: migrate_octopus_url moves the old host and leaves everything else alone")
    cases = [
        (OLD_AGILE_URL, NEW_AGILE_URL),
        ("http://api.octopus.energy/v1/products/", "https://api.oegb-kraken.energy/v1/products/"),
        ("https://API.Octopus.Energy/v1/products/", "https://api.oegb-kraken.energy/v1/products/"),
        ("https://api.octopus.energy", "https://api.oegb-kraken.energy"),
        ("https://api.octopus.energy?x=1", "https://api.oegb-kraken.energy?x=1"),
        ("https://api.octopus.energy:443/v1/products/", "https://api.oegb-kraken.energy:443/v1/products/"),
        ("https://api.octopus.energy#x", "https://api.oegb-kraken.energy#x"),
        (NEW_AGILE_URL, NEW_AGILE_URL),
        ("https://api.backend.octopus.energy/v1/graphql/", "https://api.backend.octopus.energy/v1/graphql/"),
        ("https://api.octopus.energy.example.com/v1/", "https://api.octopus.energy.example.com/v1/"),
        ("https://api.edfgb-kraken.energy/v1/products/", "https://api.edfgb-kraken.energy/v1/products/"),
        ("https://example.com/?next=https://api.octopus.energy/", "https://example.com/?next=https://api.octopus.energy/"),
        ("", ""),
        (None, None),
        (42, 42),
    ]
    for url, expected in cases:
        result = migrate_octopus_url(url)
        if result != expected:
            print("  ERROR: migrate_octopus_url({!r}) gave {!r}, expected {!r}".format(url, result, expected))
            failed = True

    print("Test: the API client talks to the new host, and keeps the backend host for saving sessions")
    client = OctopusEnergyApiClient("test-key", print)
    if client.base_url != OCTOPUS_API_URL or client.base_url != "https://api.oegb-kraken.energy":
        print("  ERROR: base_url is {}, expected the new domain".format(client.base_url))
        failed = True
    if client.backend_url != OCTOPUS_BACKEND_API_URL or client.backend_url != "https://api.backend.octopus.energy":
        print("  ERROR: backend_url is {}, expected the unchanged backend host".format(client.backend_url))
        failed = True

    print("Test: download_octopus_rates fetches an old-domain apps.yaml URL from the new domain and says so")
    my_predbat.octopus_url_cache = {}
    my_predbat.midnight_utc = datetime.strptime("2024-06-12T00:00:00+00:00", "%Y-%m-%dT%H:%M:%S%z")
    my_predbat.now_utc = my_predbat.midnight_utc
    my_predbat.minutes_now = 0
    my_predbat.debug_enable = False
    my_predbat.octopus_url_migrated = set()
    requested = []
    logged = []

    def fake_download(url):
        """Record the URL asked for and return one rate."""
        requested.append(url)
        return {0: 10.0, 60: 12.0}

    original_log = my_predbat.log
    with patch.object(my_predbat, "download_octopus_rates_func", side_effect=fake_download), patch.object(my_predbat, "_save_octopus_url_cache_to_storage"), patch.object(my_predbat, "_load_octopus_url_cache_from_storage"):
        my_predbat.log = lambda msg, *args, **kwargs: logged.append(msg)
        try:
            result = my_predbat.download_octopus_rates(OLD_AGILE_URL)
        finally:
            my_predbat.log = original_log
    if requested != [NEW_AGILE_URL]:
        print("  ERROR: expected one download from the new domain, got {}".format(requested))
        failed = True
    if result != {0: 10.0, 60: 12.0}:
        print("  ERROR: expected the downloaded rates back, got {}".format(result))
        failed = True
    if NEW_AGILE_URL not in my_predbat.octopus_url_cache or OLD_AGILE_URL in my_predbat.octopus_url_cache:
        print("  ERROR: the cache should be keyed by the new-domain URL, got keys {}".format(list(my_predbat.octopus_url_cache.keys())))
        failed = True
    if not any("Warn:" in msg and "old Octopus API address" in msg and "apps.yaml" in msg for msg in logged):
        print("  ERROR: expected a warning asking for apps.yaml to be updated, got {}".format(logged))
        failed = True

    print("Test: the old-address warning is given once per URL, not every cycle")
    logged.clear()
    my_predbat.octopus_url_cache = {}
    with patch.object(my_predbat, "download_octopus_rates_func", side_effect=fake_download), patch.object(my_predbat, "_save_octopus_url_cache_to_storage"), patch.object(my_predbat, "_load_octopus_url_cache_from_storage"):
        my_predbat.log = lambda msg, *args, **kwargs: logged.append(msg)
        try:
            my_predbat.download_octopus_rates(OLD_AGILE_URL)
        finally:
            my_predbat.log = original_log
    if any("old Octopus API address" in msg for msg in logged):
        print("  ERROR: the warning should not repeat for a URL already warned about, got {}".format(logged))
        failed = True

    print("Test: rates cached under the old address before the upgrade are still the fallback when the download fails")
    stale = {0: 7.0, 60: 8.0}
    my_predbat.octopus_url_cache = {OLD_AGILE_URL: {"stamp": datetime(2024, 6, 11), "midnight_utc": my_predbat.midnight_utc, "data": stale}}
    with patch.object(my_predbat, "download_octopus_rates_func", return_value={}), patch.object(my_predbat, "_save_octopus_url_cache_to_storage"), patch.object(my_predbat, "_load_octopus_url_cache_from_storage"):
        try:
            result = my_predbat.download_octopus_rates(OLD_AGILE_URL)
        except ValueError:
            result = "ValueError"
    if result != stale:
        print("  ERROR: expected the rates cached under the old address, got {}".format(result))
        failed = True
    if OLD_AGILE_URL in my_predbat.octopus_url_cache or NEW_AGILE_URL not in my_predbat.octopus_url_cache:
        print("  ERROR: the cache entry should have moved to the new address, got keys {}".format(list(my_predbat.octopus_url_cache.keys())))
        failed = True

    print("Test: a new-domain URL is fetched as given, with no hint logged")
    my_predbat.octopus_url_cache = {}
    requested.clear()
    logged.clear()
    with patch.object(my_predbat, "download_octopus_rates_func", side_effect=fake_download), patch.object(my_predbat, "_save_octopus_url_cache_to_storage"), patch.object(my_predbat, "_load_octopus_url_cache_from_storage"):
        my_predbat.log = lambda msg, *args, **kwargs: logged.append(msg)
        try:
            my_predbat.download_octopus_rates(NEW_AGILE_URL)
        finally:
            my_predbat.log = original_log
    if requested != [NEW_AGILE_URL] or any("old Octopus API address" in msg for msg in logged):
        print("  ERROR: a new-domain URL should be fetched unchanged with no hint, got {} / {}".format(requested, logged))
        failed = True

    print("Test: a further page the new host links on the old one is fetched from the new host")
    page_urls = []

    class FakeResponse:
        """A rate page whose next link points at the old host, as the new host's do."""

        status_code = 200

        def __init__(self, url):
            """Page 1 links page 2 on the old host; page 2 is the last."""
            self.url = url

        def json(self):
            """The page's rates and its next link."""
            rate = {"value_inc_vat": 10.0, "valid_from": "2024-06-12T00:00:00Z", "valid_to": "2024-06-12T00:30:00Z"}
            return {"results": [rate], "next": OLD_AGILE_URL + "?page=2" if "page=2" not in self.url else None}

    def fake_get(url, **kwargs):
        """Record the page asked for."""
        page_urls.append(url)
        return FakeResponse(url)

    with patch("octopus.requests.get", side_effect=fake_get):
        my_predbat.download_octopus_rates_func(NEW_AGILE_URL)
    if page_urls != [NEW_AGILE_URL, NEW_AGILE_URL + "?page=2"]:
        print("  ERROR: expected both pages from the new host, got {}".format(page_urls))
        failed = True

    print("Test: an annual tariff naming the old domain resolves its URLs to the new one, {dno_region} included")
    config = {
        "import_octopus_url": "https://api.octopus.energy/v1/products/AGILE-24-10-01/electricity-tariffs/E-1R-AGILE-24-10-01-{dno_region}/standard-unit-rates/",
        "export_octopus_url": "https://api.octopus.energy/v1/products/OUTGOING-VAR-24-10-26/electricity-tariffs/E-1R-OUTGOING-VAR-24-10-26-{dno_region}/standard-unit-rates/",
        "dno_region": "A",
    }
    tariff = AnnualTariff(config, log=print, predbat=my_predbat, fetch_json=None)
    if tariff.import_url != NEW_AGILE_URL:
        print("  ERROR: annual import URL is {}, expected {}".format(tariff.import_url, NEW_AGILE_URL))
        failed = True
    if tariff.export_url != "https://api.oegb-kraken.energy/v1/products/OUTGOING-VAR-24-10-26/electricity-tariffs/E-1R-OUTGOING-VAR-24-10-26-A/standard-unit-rates/":
        print("  ERROR: annual export URL is {}".format(tariff.export_url))
        failed = True

    print("Test: a saved annual run naming the old domain still matches its catalogue entry")
    agile = [entry for entry in IMPORT_TARIFFS if entry["id"] == "agile"][0]
    if not agile["import_octopus_url"].startswith(OCTOPUS_API_URL + "/"):
        print("  ERROR: the catalogue should list the new domain, got {}".format(agile["import_octopus_url"]))
        failed = True
    old_saved = {"import_octopus_url": agile["import_octopus_url"].replace(OCTOPUS_API_URL, "https://api.octopus.energy")}
    matched = match_entry(IMPORT_TARIFFS, old_saved, "import_octopus_url", "rates_import")
    if not matched or matched["id"] != "agile":
        print("  ERROR: an old-domain saved tariff should match Agile, got {}".format(matched))
        failed = True
    old_catalogue = [dict(agile, import_octopus_url=old_saved["import_octopus_url"])]
    matched = match_entry(old_catalogue, {"import_octopus_url": agile["import_octopus_url"]}, "import_octopus_url", "rates_import")
    if not matched or matched["id"] != "agile":
        print("  ERROR: a compare_list entry still naming the old domain should match a new-domain tariff, got {}".format(matched))
        failed = True

    return failed
