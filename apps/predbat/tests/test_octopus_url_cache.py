# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""The Octopus URL cache is stored as compact JSON, not as a per-minute YAML mapping, and held packed in memory."""

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

import pytz

from octopus import OCTOPUS_URL_CACHE_LEGACY_NAME, OCTOPUS_URL_CACHE_MODULE, OCTOPUS_URL_CACHE_NAME, PackedUrlCacheRates, adopt_legacy_octopus_url_cache, decode_octopus_url_cache, encode_octopus_url_cache
from utils import minute_data
from tests.test_infra import run_async
from tests.test_octopus_free import _attach_storage, _reset_free_cache

_RATE_URL = "https://api.octopus.energy/v1/products/AGILE/electricity-tariffs/E-1R-AGILE-A/standard-unit-rates/"
_GAP_URL = "https://api.octopus.energy/v1/products/GAPPY/electricity-tariffs/E-1R-GAPPY-A/standard-unit-rates/"
_FREE_URL = "https://octopus.energy/free-electricity"


def _sample_cache():
    """A cache holding a run of per-minute rates, a series with a gap, free sessions and an entry with no midnight."""
    midnight = pytz.utc.localize(datetime(2026, 11, 26, 0, 0, 0))
    stamp = datetime(2026, 11, 26, 10, 55, 1, 874521)
    rates = {minute: (36.01 if minute < 0 else 12.5 + minute / 1000.0) for minute in range(-4320, 2880)}
    gappy = {0: 10.0, 1: 10.0, 5: 11.25, 6: 7}
    sessions = [{"start": "2026-11-26 14:00:00+0000", "end": "2026-11-26 16:00:00+0000", "rate": 0.0}]
    return {
        _RATE_URL: {"stamp": stamp, "midnight_utc": midnight, "data": rates},
        _GAP_URL: {"stamp": stamp, "midnight_utc": midnight, "data": gappy},
        _FREE_URL: {"stamp": stamp, "midnight_utc": midnight, "data": sessions},
        "https://octopus.energy/empty": {"stamp": stamp, "midnight_utc": None, "data": []},
    }


def _check_round_trip(failed):
    """encode then JSON then decode gives back an equal cache, with int minutes, the same key order and the same value types."""
    cache = _sample_cache()
    decoded = decode_octopus_url_cache(json.loads(json.dumps(encode_octopus_url_cache(cache))))
    if decoded != cache:
        print("FAIL: decoded cache differs from the original")
        return True
    for url, entry in cache.items():
        got = decoded[url]["data"]
        if isinstance(entry["data"], dict):
            if list(got) != list(entry["data"]):
                print("FAIL: {} minute order changed".format(url))
                failed = True
            if [type(value) for value in got.values()] != [type(value) for value in entry["data"].values()]:
                print("FAIL: {} value types changed".format(url))
                failed = True
    if decoded[_RATE_URL]["midnight_utc"] != cache[_RATE_URL]["midnight_utc"] or decoded[_RATE_URL]["midnight_utc"].utcoffset() != timedelta(0):
        print("FAIL: midnight_utc did not come back as the same aware UTC time")
        failed = True
    if decoded[_RATE_URL]["stamp"].tzinfo is not None:
        print("FAIL: stamp should stay naive (it is compared with datetime.now())")
        failed = True
    if not failed:
        print("PASS: encode/decode round trip is exact")
    return failed


def _check_compact(failed):
    """A run of consecutive minutes is held as a start minute and a list of rates, not one key per minute."""
    encoded = encode_octopus_url_cache(_sample_cache())
    run = encoded[_RATE_URL]["data"]
    if set(run) != {"start", "rates"} or run["start"] != -4320 or len(run["rates"]) != 7200:
        print("FAIL: consecutive minutes not encoded as start + rates, got keys {}".format(sorted(run)))
        failed = True
    if set(encoded[_GAP_URL]["data"]) != {"minutes"}:
        print("FAIL: a series with a gap should be encoded as minute/rate pairs")
        failed = True
    if set(encoded[_FREE_URL]["data"]) != {"value"}:
        print("FAIL: free sessions should be stored as they are")
        failed = True
    if not failed:
        print("PASS: cache encoded compactly")
    return failed


def _check_packed_holder(failed):
    """pack_copy packs only a run of float minutes and never hands back the dict it was given; unpack always gives a new dict."""
    rates = _sample_cache()[_RATE_URL]["data"]
    packed = PackedUrlCacheRates.pack_copy(rates)
    if not isinstance(packed, PackedUrlCacheRates) or len(packed) != len(rates) or packed != rates or rates != packed:
        print("FAIL: a run of float minutes should pack and compare equal to its dict")
        failed = True
    unpacked = PackedUrlCacheRates.unpack(packed)
    if type(unpacked) is not dict or list(unpacked.items()) != list(rates.items()) or unpacked is PackedUrlCacheRates.unpack(packed):
        print("FAIL: unpack should give a new dict with the same minutes, rates and order each time")
        failed = True
    # A run stored out of order (a download is newest half hour first) still packs, and reads back ascending
    descending = {1: 10.0, 0: 11.0}
    held = PackedUrlCacheRates.pack_copy(descending)
    if not isinstance(held, PackedUrlCacheRates) or held.start != 0 or list(PackedUrlCacheRates.unpack(held).items()) != [(0, 11.0), (1, 10.0)]:
        print("FAIL: a run of minutes stored out of order should pack and read back ascending, got {!r}".format(held))
        failed = True
    for name, data in (("gap", {0: 10.0, 2: 11.0}), ("int rate", {0: 10.0, 1: 7}), ("out of order int rate", {1: 10.0, 0: 7}), ("str minute", {"0": 1.0}), ("empty", {})):
        held = PackedUrlCacheRates.pack_copy(data)
        if type(held) is not dict or held is data or held != data:
            print("FAIL: pack_copy of a {} series should be a separate copy of the dict".format(name))
            failed = True
        if PackedUrlCacheRates.unpack(held) is held:
            print("FAIL: unpack of a cached {} dict should be a new dict".format(name))
            failed = True
    sessions = [{"start": "2026-11-26 14:00:00+0000"}]
    if PackedUrlCacheRates.pack_copy(sessions) is not sessions or PackedUrlCacheRates.unpack(sessions) is not sessions:
        print("FAIL: the free session list should be held and returned as it is")
        failed = True
    # Decoding straight to the packed form keeps int rates as ints, so packing never changes a value's type
    mixed = decode_octopus_url_cache({_RATE_URL: {"stamp": "2026-11-26T10:00:00", "midnight_utc": None, "data": {"start": 5, "rates": [10.0, 7, 9.5]}}}, packed=True)[_RATE_URL]["data"]
    if type(mixed) is not dict or list(mixed.items()) != [(5, 10.0), (6, 7), (7, 9.5)] or type(mixed[6]) is not int:
        print("FAIL: a run with an int rate should decode to the dict, int intact, got {!r}".format(mixed))
        failed = True
    # A packed cache writes exactly the JSON the dict cache does
    cache = _sample_cache()
    packed_cache = decode_octopus_url_cache(json.loads(json.dumps(encode_octopus_url_cache(cache))), packed=True)
    if not isinstance(packed_cache[_RATE_URL]["data"], PackedUrlCacheRates) or type(packed_cache[_GAP_URL]["data"]) is not dict:
        print("FAIL: decode with packed=True should pack the run of minutes and leave the gappy series a dict")
        failed = True
    if json.dumps(encode_octopus_url_cache(packed_cache)) != json.dumps(encode_octopus_url_cache(cache)) or packed_cache != cache:
        print("FAIL: the packed cache should encode to the same JSON and compare equal to the dict cache")
        failed = True
    if not failed:
        print("PASS: cache data packed only where exact, and always handed out as a new dict")
    return failed


def _check_download_reads(my_predbat, failed):
    """download_octopus_rates() holds a download packed and returns a dict the caller owns, on every path."""
    url = "https://api.oegb-kraken.energy/v1/products/PACKED/electricity-tariffs/E-1R-PACKED-A/standard-unit-rates/"
    saved = (my_predbat.octopus_url_cache, my_predbat.octopus_url_cache_loaded)
    try:
        my_predbat.octopus_url_cache = {}
        my_predbat.octopus_url_cache_loaded = True
        # Built as download_octopus_rates_func() builds it: half hours newest first, minutes ascending within each
        first_minute = -60
        last_minute = (my_predbat.minutes_now + 2880) // 30 * 30
        downloaded = {}
        for block in range(last_minute - 30, first_minute - 1, -30):
            for minute in range(block, block + 30):
                downloaded[minute] = float(20.0 + (minute // 30) % 7)
        if list(downloaded) == sorted(downloaded):
            print("FAIL: test setup - the download should not be in ascending minute order")
            failed = True
        expected = dict(sorted(downloaded.items()))
        with patch.object(my_predbat, "download_octopus_rates_func", return_value=downloaded), patch.object(my_predbat, "_save_octopus_url_cache_to_storage"):
            first = my_predbat.download_octopus_rates(url)
        if not isinstance(my_predbat.octopus_url_cache[url]["data"], PackedUrlCacheRates):
            print("FAIL: a downloaded run of float minutes, newest half hour first, should be held packed")
            failed = True
        if first != expected:
            print("FAIL: the first read should return the downloaded rates")
            failed = True
        first[my_predbat.minutes_now] = 999.0  # a saving session added by the caller
        with patch.object(my_predbat, "download_octopus_rates_func", side_effect=AssertionError("cache hit expected")):
            second = my_predbat.download_octopus_rates(url)
            third = my_predbat.download_octopus_rates(url)
        if second != expected or second is third or list(second) != list(expected):
            print("FAIL: a cache hit should return the downloaded rates, unchanged by the caller, as a new dict each time")
            failed = True
        # A failed download falls back to the cache, also as a new dict
        my_predbat.octopus_url_cache[url]["stamp"] = datetime.now() - timedelta(hours=1)
        with patch.object(my_predbat, "download_octopus_rates_func", return_value={}), patch.object(my_predbat, "record_status"):
            fallback = my_predbat.download_octopus_rates(url)
        if fallback != expected or fallback is second:
            print("FAIL: a failed download should return the cached rates as a new dict")
            failed = True
        # A series that cannot be packed is held as a copy, so the caller's dict is still its own
        gappy = {my_predbat.minutes_now: 10.0, my_predbat.minutes_now + 30: 12}
        my_predbat.octopus_url_cache = {}
        with patch.object(my_predbat, "download_octopus_rates_func", return_value=gappy), patch.object(my_predbat, "_save_octopus_url_cache_to_storage"):
            got = my_predbat.download_octopus_rates(url)
        held = my_predbat.octopus_url_cache[url]["data"]
        if type(held) is not dict or held is got or held != {my_predbat.minutes_now: 10.0, my_predbat.minutes_now + 30: 12}:
            print("FAIL: a series that cannot be packed should be cached as a copy of the dict returned")
            failed = True
    finally:
        my_predbat.octopus_url_cache, my_predbat.octopus_url_cache_loaded = saved
    if not failed:
        print("PASS: downloads are held packed and every read is the caller's own dict")
    return failed


def _check_newest_first_download(failed):
    """Rates built the way download_octopus_rates_func() builds them, from rows newest first as Octopus returns them, still encode compactly."""
    midnight = pytz.utc.localize(datetime(2026, 11, 26, 0, 0, 0))
    rows = []
    for slot in range(4):
        valid_from = midnight + timedelta(minutes=30 * slot)
        valid_to = valid_from + timedelta(minutes=30)
        rows.append({"valid_from": valid_from.strftime("%Y-%m-%dT%H:%M:%SZ"), "valid_to": valid_to.strftime("%Y-%m-%dT%H:%M:%SZ"), "value_inc_vat": 10.0 + slot})
    rows.reverse()
    rates, _ = minute_data(rows, 3, midnight, "value_inc_vat", "valid_from", backwards=False, to_key="valid_to")
    if list(rates) == sorted(rates):
        print("FAIL: test setup - newest-first rows should give a dict whose minutes are not in ascending order")
        return True
    cache = {_RATE_URL: {"stamp": datetime(2026, 11, 26, 10, 0, 0), "midnight_utc": midnight, "data": rates}}
    encoded = encode_octopus_url_cache(cache)
    run = encoded[_RATE_URL]["data"]
    if set(run) != {"start", "rates"} or run["start"] != 0 or len(run["rates"]) != 120:
        print("FAIL: newest-first download not encoded as start + rates, got keys {}".format(sorted(run)))
        return True
    decoded = decode_octopus_url_cache(json.loads(json.dumps(encoded)))[_RATE_URL]["data"]
    if decoded != rates:
        print("FAIL: newest-first download did not round trip")
        failed = True
    elif list(decoded) != sorted(rates):
        print("FAIL: newest-first download should reload in ascending minute order")
        failed = True
    elif decoded[0] != 10.0 or decoded[119] != 13.0:
        print("FAIL: rates landed on the wrong minutes: minute 0 {} minute 119 {}".format(decoded[0], decoded[119]))
        failed = True
    elif not isinstance(PackedUrlCacheRates.pack_copy(rates), PackedUrlCacheRates) or PackedUrlCacheRates.pack_copy(rates) != rates:
        print("FAIL: newest-first download should be held packed in memory, equal to the downloaded dict")
        failed = True
    else:
        print("PASS: newest-first download encoded compactly and reloads ascending")
    return failed


def _check_storage(my_predbat, failed):
    """The cache saves as JSON under its own name, reloads equal after a restart and skips bad entries."""
    temp_dir = tempfile.mkdtemp()
    original_config_root = my_predbat.config_root
    try:
        storage = _attach_storage(my_predbat, temp_dir)
        _reset_free_cache(my_predbat)
        now = datetime.now()
        cache = _sample_cache()
        for entry in cache.values():
            entry["stamp"] = now
        my_predbat.octopus_url_cache = cache
        my_predbat._save_octopus_url_cache_to_storage()

        cache_dir = os.path.join(temp_dir, "cache")
        json_path = os.path.join(cache_dir, "{}_{}.json".format(OCTOPUS_URL_CACHE_MODULE, OCTOPUS_URL_CACHE_NAME))
        if not os.path.exists(json_path):
            print("FAIL: cache not written to {}".format(json_path))
            failed = True
        if any(name.endswith(".yaml") for name in os.listdir(cache_dir)):
            print("FAIL: a YAML cache file was written: {}".format(sorted(os.listdir(cache_dir))))
            failed = True

        _reset_free_cache(my_predbat)
        my_predbat._load_octopus_url_cache_from_storage()
        if my_predbat.octopus_url_cache != cache:
            print("FAIL: cache reloaded from storage differs from what was saved")
            failed = True
        elif not isinstance(my_predbat.octopus_url_cache[_RATE_URL]["data"], PackedUrlCacheRates):
            print("FAIL: the run of minutes should be held packed after loading from storage")
            failed = True
        else:
            print("PASS: cache saved as JSON and reloaded after a restart")

        # One bad entry is dropped, the rest load
        good = encode_octopus_url_cache({_FREE_URL: {"stamp": now, "midnight_utc": None, "data": []}})
        good["https://octopus.energy/broken"] = {"stamp": "not a time", "data": {"value": []}}
        good["https://octopus.energy/not-a-dict"] = [1, 2, 3]
        run_async(storage.save(OCTOPUS_URL_CACHE_MODULE, OCTOPUS_URL_CACHE_NAME, good, format="json", expiry=None))
        _reset_free_cache(my_predbat)
        my_predbat._load_octopus_url_cache_from_storage()
        if set(my_predbat.octopus_url_cache) != {_FREE_URL}:
            print("FAIL: expected only the good entry to load, got {}".format(sorted(my_predbat.octopus_url_cache)))
            failed = True
        else:
            print("PASS: malformed entries are skipped")
    finally:
        shutil.rmtree(temp_dir)
        my_predbat.components = None
        my_predbat.config_root = original_config_root
        _reset_free_cache(my_predbat)
    return failed


def _check_legacy_yaml(my_predbat, failed):
    """
    With no JSON cache, the YAML cache an earlier version wrote is read once: its entries are held as the JSON
    cache would hold them and saved straight away as JSON, so the next restart reads JSON. A JSON cache, when
    there is one, wins; an expired YAML file and its unreadable entries are ignored.
    """
    temp_dir = tempfile.mkdtemp()
    original_config_root = my_predbat.config_root
    try:
        storage = _attach_storage(my_predbat, temp_dir)
        cache_dir = os.path.join(temp_dir, "cache")
        json_path = os.path.join(cache_dir, "{}_{}.json".format(OCTOPUS_URL_CACHE_MODULE, OCTOPUS_URL_CACHE_NAME))
        now = datetime.now()

        def fresh_cache():
            """The sample cache stamped now, so the 2-day prune on save keeps every entry."""
            cache = _sample_cache()
            for entry in cache.values():
                entry["stamp"] = now
            return cache

        def reset_dir():
            """Start each case with an empty cache directory."""
            shutil.rmtree(cache_dir, ignore_errors=True)
            os.makedirs(cache_dir)

        # Upgrade from a version that wrote the YAML file under the old name: it is read and converted
        reset_dir()
        run_async(storage.save(OCTOPUS_URL_CACHE_MODULE, OCTOPUS_URL_CACHE_LEGACY_NAME, fresh_cache(), format="yaml", expiry=pytz.utc.localize(datetime.utcnow()) + timedelta(hours=8)))
        _reset_free_cache(my_predbat)
        my_predbat._load_octopus_url_cache_from_storage()
        if my_predbat.octopus_url_cache != fresh_cache():
            print("FAIL: the old YAML cache was not loaded when there is no JSON cache, got {}".format(sorted(my_predbat.octopus_url_cache)))
            failed = True
        elif not isinstance(my_predbat.octopus_url_cache[_RATE_URL]["data"], PackedUrlCacheRates):
            print("FAIL: the old YAML cache's run of minutes should be held packed")
            failed = True
        elif not os.path.exists(json_path):
            print("FAIL: the old YAML cache was not saved as JSON once loaded")
            failed = True
        else:
            print("PASS: the old YAML cache is read once when there is no JSON cache, and saved as JSON")

        # The next restart reads the JSON written above, even with the YAML file still there
        _reset_free_cache(my_predbat)
        with patch("octopus.adopt_legacy_octopus_url_cache", side_effect=AssertionError("legacy YAML read twice")) as adopt:
            my_predbat._load_octopus_url_cache_from_storage()
        if adopt.called or my_predbat.octopus_url_cache != fresh_cache():
            print("FAIL: the restart after converting should read the JSON cache, not the YAML")
            failed = True
        else:
            print("PASS: the restart after converting reads the JSON cache")

        # A JSON cache wins over a YAML file that is also there
        reset_dir()
        json_cache = {_FREE_URL: {"stamp": now, "midnight_utc": None, "data": []}}
        run_async(storage.save(OCTOPUS_URL_CACHE_MODULE, OCTOPUS_URL_CACHE_NAME, encode_octopus_url_cache(json_cache), format="json", expiry=None))
        run_async(storage.save(OCTOPUS_URL_CACHE_MODULE, OCTOPUS_URL_CACHE_LEGACY_NAME, fresh_cache(), format="yaml", expiry=None))
        _reset_free_cache(my_predbat)
        my_predbat._load_octopus_url_cache_from_storage()
        if my_predbat.octopus_url_cache != json_cache:
            print("FAIL: the JSON cache should win over the old YAML, got {}".format(sorted(my_predbat.octopus_url_cache)))
            failed = True
        else:
            print("PASS: a JSON cache wins over the old YAML file")

        # An expired YAML file is not used
        reset_dir()
        run_async(storage.save(OCTOPUS_URL_CACHE_MODULE, OCTOPUS_URL_CACHE_LEGACY_NAME, fresh_cache(), format="yaml", expiry=pytz.utc.localize(datetime.utcnow()) - timedelta(minutes=1)))
        _reset_free_cache(my_predbat)
        my_predbat._load_octopus_url_cache_from_storage()
        if my_predbat.octopus_url_cache != {} or os.path.exists(json_path):
            print("FAIL: an expired old YAML cache was used")
            failed = True
        else:
            print("PASS: an expired old YAML cache is ignored")

        # Unreadable YAML entries are skipped, the rest load
        reset_dir()
        legacy = fresh_cache()
        legacy["https://octopus.energy/no-stamp"] = {"data": []}
        legacy["https://octopus.energy/bad-stamp"] = {"stamp": "not a time", "data": []}
        legacy["https://octopus.energy/bad-midnight"] = {"stamp": now, "midnight_utc": 5, "data": []}
        legacy["https://octopus.energy/not-a-dict"] = [1, 2, 3]
        run_async(storage.save(OCTOPUS_URL_CACHE_MODULE, OCTOPUS_URL_CACHE_LEGACY_NAME, legacy, format="yaml", expiry=None))
        _reset_free_cache(my_predbat)
        my_predbat._load_octopus_url_cache_from_storage()
        if my_predbat.octopus_url_cache != fresh_cache():
            print("FAIL: expected only the readable old YAML entries, got {}".format(sorted(my_predbat.octopus_url_cache)))
            failed = True
        else:
            print("PASS: unreadable old YAML entries are skipped")

        # A YAML file that is not a mapping at all loads nothing
        if adopt_legacy_octopus_url_cache(["not", "a", "mapping"]) != {}:
            print("FAIL: an old YAML cache that is not a mapping should give an empty cache")
            failed = True
    finally:
        shutil.rmtree(temp_dir)
        my_predbat.components = None
        my_predbat.config_root = original_config_root
        _reset_free_cache(my_predbat)
    return failed


def test_octopus_url_cache(my_predbat):
    """The Octopus URL cache round-trips exactly through compact JSON storage and is held packed in memory."""
    print("**** Running Octopus URL cache JSON and packing tests ****")
    failed = False
    failed = _check_round_trip(failed)
    failed = _check_compact(failed)
    failed = _check_newest_first_download(failed)
    failed = _check_packed_holder(failed)
    failed = _check_download_reads(my_predbat, failed)
    failed = _check_storage(my_predbat, failed)
    failed = _check_legacy_yaml(my_predbat, failed)
    print("**** Octopus URL cache JSON and packing tests {} ****".format("FAILED" if failed else "PASSED"))
    return failed
