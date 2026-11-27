# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

"""The Octopus URL cache is stored as compact JSON, not as a per-minute YAML mapping."""

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta

import pytz

from octopus import OCTOPUS_URL_CACHE_MODULE, OCTOPUS_URL_CACHE_NAME, decode_octopus_url_cache, encode_octopus_url_cache
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
    else:
        print("PASS: newest-first download encoded compactly and reloads ascending")
    return failed


def _check_storage(my_predbat, failed):
    """The cache saves as JSON under its own name, reloads equal after a restart, ignores the old YAML file and skips bad entries."""
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
        else:
            print("PASS: cache saved as JSON and reloaded after a restart")

        # The cache Predbat wrote before this change: a YAML mapping under the old name. It is never parsed.
        shutil.rmtree(cache_dir)
        os.makedirs(cache_dir)
        run_async(storage.save(OCTOPUS_URL_CACHE_MODULE, "url_cache", _sample_cache(), format="yaml", expiry=None))
        _reset_free_cache(my_predbat)
        my_predbat._load_octopus_url_cache_from_storage()
        if my_predbat.octopus_url_cache != {}:
            print("FAIL: the old YAML cache was loaded")
            failed = True
        else:
            print("PASS: the old YAML cache is not read")

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


def test_octopus_url_cache(my_predbat):
    """The Octopus URL cache round-trips exactly through compact JSON storage."""
    print("**** Running Octopus URL cache JSON tests ****")
    failed = False
    failed = _check_round_trip(failed)
    failed = _check_compact(failed)
    failed = _check_newest_first_download(failed)
    failed = _check_storage(my_predbat, failed)
    print("**** Octopus URL cache JSON tests {} ****".format("FAILED" if failed else "PASSED"))
    return failed
