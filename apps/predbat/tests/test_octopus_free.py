# -----------------------------------------------------------------------------
# Predbat Home Battery System
# Copyright Trefor Southwell 2026 - All Rights Reserved
# This application maybe used for personal use only and not for commercial use
# -----------------------------------------------------------------------------
# fmt off
# pylint: disable=consider-using-f-string
# pylint: disable=line-too-long
# pylint: disable=attribute-defined-outside-init

import json
import shutil
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from storage import StorageComponent, StorageLocalFiles
from tests.test_infra import run_async


class _MockComponents:
    """Minimal components registry that exposes a single storage instance."""

    def __init__(self, storage):
        """Initialise with a storage instance."""
        self._storage = storage

    def get_component(self, name):
        """Return the mocked storage for 'storage', None for others."""
        return self._storage if name == "storage" else None


def _attach_storage(predbat, temp_dir):
    """Attach a real StorageLocalFiles-backed StorageComponent to predbat, rooted at temp_dir."""
    # Point config_root at temp_dir first: StorageComponent.initialize() builds a StorageLocalFiles
    # in the constructor, which creates a 'cache' directory under config_root. Without this it would
    # default to './' and leak a ./cache directory into the repo before we overwrite the backend.
    predbat.config_root = temp_dir
    storage = StorageComponent(predbat)
    storage.backend = StorageLocalFiles(temp_dir, predbat.log)
    predbat.components = _MockComponents(storage)
    return storage


def _mock_http_response(body, status=200):
    """Return a minimal requests.Response-like mock."""
    resp = MagicMock()
    resp.status_code = status
    resp.text = body
    return resp


def _reset_free_cache(predbat):
    """Clear in-memory URL cache and the one-shot loaded flag to simulate a restart."""
    predbat.octopus_url_cache = {}
    predbat.octopus_url_cache_loaded = False


_URL = "https://octopus.energy/free-electricity"
_URL2 = "https://octopus.energy/free-electricity-b"

_JSON_BODY = json.dumps(
    {
        "sessions": [
            {"session_start": "2026-06-11T14:00:00Z", "session_end": "2026-06-11T16:00:00Z"},
        ]
    }
)


def test_octopus_free(my_predbat):
    """Test Octopus free electricity session download and storage persistence."""
    failed = False
    print("**** Running Octopus free electricity test ****")

    temp_dir = tempfile.mkdtemp()
    original_config_root = getattr(my_predbat, "config_root", None)
    try:
        # --- Test 1: no storage (components=None) — no exception, returns sessions ---
        _reset_free_cache(my_predbat)
        my_predbat.components = None
        with patch("octopus.requests.get", return_value=_mock_http_response(_JSON_BODY)) as mock_get:
            sessions = my_predbat.download_octopus_free(_URL)
        if mock_get.call_count != 1:
            print("FAIL: Expected 1 HTTP call without storage, got {}".format(mock_get.call_count))
            failed = True
        elif len(sessions) != 1:
            print("FAIL: Expected 1 session without storage, got {}".format(len(sessions)))
            failed = True
        else:
            print("PASS: Fresh download works without storage")

        # --- Test 2: fresh download is persisted to storage ---
        storage = _attach_storage(my_predbat, temp_dir)
        _reset_free_cache(my_predbat)
        with patch("octopus.requests.get", return_value=_mock_http_response(_JSON_BODY)) as mock_get:
            sessions = my_predbat.download_octopus_free(_URL)
        if mock_get.call_count != 1:
            print("FAIL: Expected 1 HTTP call on fresh download, got {}".format(mock_get.call_count))
            failed = True
        elif len(sessions) != 1:
            print("FAIL: Expected 1 parsed session, got {}".format(len(sessions)))
            failed = True
        else:
            print("PASS: Fresh download returned 1 session")
        saved = run_async(storage.load("octopus_free", "url_cache"))
        if not saved or _URL not in saved or saved[_URL].get("data") != sessions:
            print("FAIL: Cache not correctly persisted to storage")
            failed = True
        else:
            print("PASS: Cache persisted to storage")

        # --- Test 3: in-memory cache hit — no HTTP call ---
        with patch("octopus.requests.get", return_value=_mock_http_response(_JSON_BODY)) as mock_get:
            sessions2 = my_predbat.download_octopus_free(_URL)
        if mock_get.call_count != 0:
            print("FAIL: Expected 0 HTTP calls for in-memory cache hit, got {}".format(mock_get.call_count))
            failed = True
        elif sessions2 != sessions:
            print("FAIL: In-memory cache returned different data")
            failed = True
        else:
            print("PASS: In-memory cache hit, no HTTP call")

        # --- Test 4: cache restored from storage on simulated restart — no HTTP call ---
        _reset_free_cache(my_predbat)
        with patch("octopus.requests.get", return_value=_mock_http_response(_JSON_BODY)) as mock_get:
            sessions3 = my_predbat.download_octopus_free(_URL)
        if mock_get.call_count != 0:
            print("FAIL: Expected 0 HTTP calls after storage restore, got {}".format(mock_get.call_count))
            failed = True
        elif sessions3 != sessions:
            print("FAIL: Storage restore returned different sessions")
            failed = True
        else:
            print("PASS: Cache restored from storage, no HTTP call")

        # --- Test 5: stale midnight in storage triggers a re-fetch ---
        _reset_free_cache(my_predbat)
        old_midnight = my_predbat.midnight_utc - timedelta(days=1)
        stale = {_URL: {"stamp": datetime.now(), "midnight_utc": old_midnight, "data": sessions}}
        run_async(storage.save("octopus_free", "url_cache", stale, format="yaml", expiry=None))
        with patch("octopus.requests.get", return_value=_mock_http_response(_JSON_BODY)) as mock_get:
            my_predbat.download_octopus_free(_URL)
        if mock_get.call_count != 1:
            print("FAIL: Expected 1 HTTP call for stale midnight, got {}".format(mock_get.call_count))
            failed = True
        else:
            print("PASS: Stale midnight triggers re-fetch")

        # --- Test 6: expired stamp in storage triggers a re-fetch ---
        _reset_free_cache(my_predbat)
        old_stamp = datetime.now() - timedelta(hours=1)
        expired = {_URL: {"stamp": old_stamp, "midnight_utc": my_predbat.midnight_utc, "data": sessions}}
        run_async(storage.save("octopus_free", "url_cache", expired, format="yaml", expiry=None))
        with patch("octopus.requests.get", return_value=_mock_http_response(_JSON_BODY)) as mock_get:
            my_predbat.download_octopus_free(_URL)
        if mock_get.call_count != 1:
            print("FAIL: Expected 1 HTTP call for expired stamp, got {}".format(mock_get.call_count))
            failed = True
        else:
            print("PASS: Expired stamp triggers re-fetch")

        # --- Test 7: multiple URLs are all saved and restored together ---
        _reset_free_cache(my_predbat)
        _attach_storage(my_predbat, temp_dir)
        with patch("octopus.requests.get", return_value=_mock_http_response(_JSON_BODY)):
            my_predbat.download_octopus_free(_URL)
        with patch("octopus.requests.get", return_value=_mock_http_response(_JSON_BODY)):
            my_predbat.download_octopus_free(_URL2)
        saved_multi = run_async(storage.load("octopus_free", "url_cache"))
        if not saved_multi or _URL not in saved_multi or _URL2 not in saved_multi:
            print("FAIL: Both URLs not found in storage after multi-URL save")
            failed = True
        else:
            print("PASS: Multiple URLs persisted together in one storage entry")
        _reset_free_cache(my_predbat)
        with patch("octopus.requests.get", return_value=_mock_http_response(_JSON_BODY)) as mock_get:
            my_predbat.download_octopus_free(_URL)
            my_predbat.download_octopus_free(_URL2)
        if mock_get.call_count != 0:
            print("FAIL: Expected 0 HTTP calls restoring both URLs from storage, got {}".format(mock_get.call_count))
            failed = True
        else:
            print("PASS: Both URLs restored from storage with no HTTP calls")

        # --- Test 8: entries older than 2 days are pruned on save (in-memory and storage) ---
        storage = _attach_storage(my_predbat, temp_dir)
        _reset_free_cache(my_predbat)
        now = datetime.now()
        my_predbat.octopus_url_cache = {
            "https://octopus.energy/old": {"stamp": now - timedelta(days=3), "midnight_utc": my_predbat.midnight_utc, "data": sessions},
            "https://octopus.energy/edge": {"stamp": now - timedelta(days=2, minutes=1), "midnight_utc": my_predbat.midnight_utc, "data": sessions},
            "https://octopus.energy/legacy": {"data": sessions},  # malformed: no stamp
            _URL: {"stamp": now, "midnight_utc": my_predbat.midnight_utc, "data": sessions},  # fresh, must survive
        }
        my_predbat._save_octopus_url_cache_to_storage()
        if set(my_predbat.octopus_url_cache) != {_URL}:
            print("FAIL: Stale/malformed entries not pruned in memory, left {}".format(sorted(my_predbat.octopus_url_cache)))
            failed = True
        else:
            print("PASS: Stale and malformed entries pruned from in-memory cache")
        saved_pruned = run_async(storage.load("octopus_free", "url_cache"))
        if not saved_pruned or set(saved_pruned) != {_URL}:
            print("FAIL: Pruned cache not persisted correctly, storage has {}".format(sorted(saved_pruned) if saved_pruned else saved_pruned))
            failed = True
        else:
            print("PASS: Only fresh entry persisted to storage after prune")

    finally:
        shutil.rmtree(temp_dir)
        my_predbat.components = None
        my_predbat.config_root = original_config_root
        _reset_free_cache(my_predbat)

    if not failed:
        print("**** Octopus free electricity test PASSED ****")
    else:
        print("**** Octopus free electricity test FAILED ****")
    return failed
